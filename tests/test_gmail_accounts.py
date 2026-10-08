"""Gmail multi-account + "Never show agents" filters (M3.6 Step 3).

Accounts live at `gmail:account:<email>` (managed OAuth and manual paste are
field-compatible); `gmail:default` is just the default pointer + filters. The
filters are enforced in the DESKTOP tool layer, silently: matching messages
are omitted (no tombstone), a direct fetch reads like a real 404, and the
count travels on the `_display` sidecar — stripped from every provider feed.
"""

from __future__ import annotations

import json
import time

import pytest

from coworker.connectors import gmail_accounts
from coworker.connectors.integration_tools import make_integration_tools
from coworker.connectors.setup import connector_list, disconnect_connector
from coworker.secrets import SecretStore


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    return SecretStore()


def _account(email: str, **extra) -> dict:
    return {
        "type": "oauth",
        "enabled": True,
        "managed": True,
        "access_token": f"tok-{email}",
        "account": email,
        **extra,
    }


def _tool(secrets, name: str):
    tools = make_integration_tools(secrets)
    return next(t for t in tools if t.__name__ == name)


# --- accounts: migration / default / disconnect ------------------------------


def test_legacy_default_migrates_to_one_account(secrets):
    secrets.put(
        "gmail:default",
        {
            "type": "oauth",
            "enabled": True,
            "access_token": "ya29",
            "account": "Old@X.com",
        },
    )
    accounts = gmail_accounts.list_accounts(secrets)
    assert [e for e, _ in accounts] == ["old@x.com"]
    assert accounts[0][1]["access_token"] == "ya29"
    pointer = secrets.get("gmail:default")
    assert pointer["default_account"] == "old@x.com"
    assert "access_token" not in pointer  # tokens moved, not copied
    assert gmail_accounts.default_account(secrets) == "old@x.com"


def test_migration_preserves_filters_and_is_idempotent(secrets):
    secrets.put(
        "gmail:default",
        {
            "access_token": "t",
            "account": "a@x.com",
            "filters": {"senders": ["ceo@x.com"]},
        },
    )
    gmail_accounts.migrate_legacy_default(secrets)
    gmail_accounts.migrate_legacy_default(secrets)
    assert gmail_accounts.get_filters(secrets)["senders"] == ["ceo@x.com"]
    assert len(gmail_accounts.list_accounts(secrets)) == 1


def test_second_account_added_first_stays_default(secrets):
    gmail_accounts.managed_connect_account(secrets, _account("one@x.com"))
    gmail_accounts.managed_connect_account(secrets, _account("two@y.com"))
    assert gmail_accounts.default_account(secrets) == "one@x.com"
    listed = {c["name"]: c for c in connector_list(secrets)}
    emails = [a["email"] for a in listed["gmail"]["accounts"]]
    assert emails == ["one@x.com", "two@y.com"]
    assert listed["gmail"]["connected"]


def test_set_default_and_disconnect_repoints(secrets):
    gmail_accounts.managed_connect_account(secrets, _account("one@x.com"))
    gmail_accounts.managed_connect_account(secrets, _account("two@y.com"))
    assert gmail_accounts.set_default(secrets, "two@y.com")["ok"]
    assert gmail_accounts.default_account(secrets) == "two@y.com"
    # dropping the default moves the pointer to the remaining account
    assert gmail_accounts.disconnect_account(secrets, "two@y.com")["ok"]
    assert gmail_accounts.default_account(secrets) == "one@x.com"


def test_last_disconnect_keeps_filters_only(secrets):
    gmail_accounts.managed_connect_account(secrets, _account("one@x.com"))
    gmail_accounts.set_filters(secrets, senders=["@spam.com"])
    gmail_accounts.disconnect_account(secrets, "one@x.com")
    listed = {c["name"]: c for c in connector_list(secrets)}
    assert not listed["gmail"]["connected"]
    assert gmail_accounts.get_filters(secrets)["senders"] == [
        "@spam.com"
    ]  # policy survives


def test_full_disconnect_drops_every_account(secrets):
    gmail_accounts.managed_connect_account(secrets, _account("one@x.com"))
    gmail_accounts.managed_connect_account(secrets, _account("two@y.com"))
    assert disconnect_connector(secrets, "gmail")["ok"]
    assert gmail_accounts.list_accounts(secrets) == []
    assert secrets.get("gmail:default") is None


# --- tools: per-account resolution -------------------------------------------


def _fake_gmail(monkeypatch, responses: dict[str, dict]):
    """Route _request by URL suffix; records the bearer token used."""
    from coworker.connectors import integration_tools

    calls: list[tuple[str, str]] = []

    def fake_request(method, url, *, headers=None, params=None, json=None, auth=None):
        token = (headers or {}).get("Authorization", "")
        calls.append((url, token))
        for suffix, resp in responses.items():
            if suffix in url:
                return resp
        return {"error": "HTTP 404", "details": "no fake route"}

    monkeypatch.setattr(integration_tools, "_request", fake_request)
    return calls


def test_tools_pick_the_requested_account_token(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("one@x.com"))
    gmail_accounts.managed_connect_account(secrets, _account("two@y.com"))
    calls = _fake_gmail(
        monkeypatch, {"/messages": {"ok": True, "data": {"messages": []}}}
    )
    search = _tool(secrets, "gmail_search_messages")

    out = search("from:bob")  # default account
    assert out["ok"] and out["account"] == "one@x.com"
    out = search("from:bob", account="two@y.com")
    assert out["ok"] and out["account"] == "two@y.com"
    # each search: the list call + the label-name lookup
    assert [t for _, t in calls] == ["Bearer tok-one@x.com"] * 2 + [
        "Bearer tok-two@y.com"
    ] * 2

    out = search("x", account="nobody@z.com")
    assert "no gmail account" in out["error"]


def test_legacy_single_account_still_works_via_tools(secrets, monkeypatch):
    # Pre-migration store: tokens on gmail:default (manual paste era).
    secrets.put("gmail:default", {"access_token": "legacy-tok", "account": "me@x.com"})
    calls = _fake_gmail(
        monkeypatch, {"/messages": {"ok": True, "data": {"messages": []}}}
    )
    out = _tool(secrets, "gmail_search_messages")("q")
    assert out["ok"] and out["account"] == "me@x.com"
    assert calls[0][1] == "Bearer legacy-tok"


# --- filters: silent omission -------------------------------------------------


def _raw(**headers_and_body: str) -> str:
    """base64url RFC 822 message, the shape Gmail returns for format=raw."""
    import base64
    from email.message import EmailMessage

    msg = EmailMessage()
    body = headers_and_body.pop("body", "hi")
    for name, value in headers_and_body.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def _msg(mid: str, sender: str, labels: list[str] | None = None) -> dict:
    """Serves both format=metadata (payload headers) and format=raw (raw)."""
    return {
        "id": mid,
        "labelIds": labels or [],
        "payload": {"headers": [{"name": "From", "value": f"Some One <{sender}>"}]},
        "raw": _raw(From=f"Some One <{sender}>"),
    }


def test_search_omits_filtered_senders_and_counts_hidden(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    gmail_accounts.set_filters(secrets, senders=["ceo@corp.com", "@secret.org"])
    _fake_gmail(
        monkeypatch,
        {
            "/messages/m1": {"ok": True, "data": _msg("m1", "ceo@corp.com")},
            "/messages/m2": {"ok": True, "data": _msg("m2", "pal@ok.com")},
            "/messages/m3": {"ok": True, "data": _msg("m3", "x@secret.org")},
            "/messages": {
                "ok": True,
                "data": {
                    "messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                    "resultSizeEstimate": 3,
                },
            },
        },
    )
    out = _tool(secrets, "gmail_search_messages")("q")
    assert [m["id"] for m in out["data"]["messages"]] == ["m2"]
    assert out["data"]["resultSizeEstimate"] == 1
    assert out["_display"] == {"hidden_by_filters": 2, "connector": "gmail"}
    # no tombstone anywhere in the agent-visible payload
    assert "hidden" not in json.dumps(out["data"]).lower()


def test_get_filtered_message_reads_like_a_real_404(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    gmail_accounts.set_filters(secrets, senders=["ceo@corp.com"])
    _fake_gmail(
        monkeypatch, {"/messages/m1": {"ok": True, "data": _msg("m1", "ceo@corp.com")}}
    )
    out = _tool(secrets, "gmail_get_message")("m1")
    assert out["error"] == "HTTP 404"
    assert out["_display"] == {"hidden_by_filters": 1, "connector": "gmail"}
    assert (
        "filter"
        not in json.dumps({k: v for k, v in out.items() if k != "_display"}).lower()
    )


def test_label_filter_uses_label_names(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    gmail_accounts.set_filters(secrets, labels=["Personal"])
    _fake_gmail(
        monkeypatch,
        {
            "/labels": {
                "ok": True,
                "data": {"labels": [{"id": "Label_7", "name": "Personal"}]},
            },
            "/messages/m1": {"ok": True, "data": _msg("m1", "a@b.com", ["Label_7"])},
            "/messages/m2": {"ok": True, "data": _msg("m2", "a@b.com", ["INBOX"])},
            "/messages": {
                "ok": True,
                "data": {"messages": [{"id": "m1"}, {"id": "m2"}]},
            },
        },
    )
    out = _tool(secrets, "gmail_search_messages")("q")
    assert [m["id"] for m in out["data"]["messages"]] == ["m2"]
    assert out["_display"]["hidden_by_filters"] == 1


def test_search_returns_envelopes_not_bare_ids(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    meta = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "Bonjour",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Amélie <a@x.com>"},
                {"name": "Subject", "value": "Devis"},
            ]
        },
    }
    _fake_gmail(
        monkeypatch,
        {
            "/messages/m1": {"ok": True, "data": meta},
            "/messages": {"ok": True, "data": {"messages": [{"id": "m1"}]}},
        },
    )
    out = _tool(secrets, "gmail_search_messages")("q")
    [m] = out["data"]["messages"]
    assert m["from"] == "Amélie <a@x.com>" and m["subject"] == "Devis"
    assert m["thread_id"] == "t1" and m["unread"] and m["snippet"] == "Bonjour"


def test_sender_rule_matching():
    assert gmail_accounts.sender_matches("ceo@corp.com", ["ceo@corp.com"])
    assert gmail_accounts.sender_matches("CEO@Corp.com", ["ceo@corp.com"])
    assert gmail_accounts.sender_matches("a@secret.org", ["@secret.org"])
    assert not gmail_accounts.sender_matches("a@notsecret.org", ["@secret.org"])
    assert not gmail_accounts.sender_matches("other@corp.com", ["ceo@corp.com"])
    assert not gmail_accounts.sender_matches("", ["@x.com"])


# --- managed refresh targets the account profile ------------------------------


def test_account_profile_refreshes_in_place(secrets, monkeypatch):
    from coworker import cloud

    secrets.put(
        cloud.CLOUD_AUTH_PROFILE, {"access_token": "jwt", "expires": time.time() + 3600}
    )
    gmail_accounts.managed_connect_account(
        secrets,
        _account(
            "me@x.com",
            provider="google",
            refresh_token="1//r",
            connection_id="conn_9",
            expires=time.time() - 10,
        ),
    )

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "fresh", "expires_in": 3600}

    monkeypatch.setattr(cloud.httpx, "post", lambda *a, **k: _Resp())
    _fake_gmail(monkeypatch, {"/messages": {"ok": True, "data": {"messages": []}}})
    out = _tool(secrets, "gmail_search_messages")("q")
    assert out["ok"]
    assert secrets.get("gmail:account:me@x.com")["access_token"] == "fresh"
    assert not (secrets.get("gmail:default") or {}).get("access_token")


# --- reading / composing / organizing ------------------------------------------


def _fake_api(monkeypatch, routes: dict[str, dict]) -> list[tuple[str, str, object]]:
    """Exact routing on "METHOD /path" under users/me; records (method, path, json).
    Unrouted calls answer an empty ok (labels, signature, …)."""
    from coworker.connectors import integration_tools

    calls: list[tuple[str, str, object]] = []

    def fake_request(method, url, *, headers=None, params=None, json=None, auth=None):
        path = url.split("/users/me", 1)[1]
        calls.append((method, path, json))
        return routes.get(f"{method} {path}", {"ok": True, "data": {}})

    monkeypatch.setattr(integration_tools, "_request", fake_request)
    return calls


def _decoded(payload: dict):
    import base64
    import email

    raw = payload["raw"]
    from email import policy

    return email.message_from_bytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default
    )


def test_get_message_decodes_body_and_lists_attachments(secrets, monkeypatch):
    import base64
    from email.message import EmailMessage

    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    m = EmailMessage()
    m["From"], m["Subject"] = "a@x.com", "=?utf-8?q?R=C3=A9union?="
    m.set_content("Le corps complet du message, bien au-delà du snippet.")
    m.add_attachment(
        b"%PDF", maintype="application", subtype="pdf", filename="devis.pdf"
    )
    raw = base64.urlsafe_b64encode(m.as_bytes()).decode().rstrip("=")
    _fake_api(
        monkeypatch,
        {
            "GET /messages/m1": {
                "ok": True,
                "data": {"id": "m1", "threadId": "t1", "raw": raw},
            }
        },
    )
    data = _tool(secrets, "gmail_get_message")("m1")["data"]
    assert "bien au-delà du snippet" in data["body"]
    assert data["subject"] == "Réunion" and data["thread_id"] == "t1"
    assert data["attachments"] == [
        {"filename": "devis.pdf", "content_type": "application/pdf", "size": 4}
    ]


def test_thread_reads_every_message_and_hides_filtered_ones(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    gmail_accounts.set_filters(secrets, senders=["ceo@corp.com"])
    _fake_api(
        monkeypatch,
        {
            "GET /threads/t1": {
                "ok": True,
                "data": {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]},
            },
            "GET /messages/m1": {"ok": True, "data": {"id": "m1", "raw": _raw(From="a@x.com", body="un")}},
            "GET /messages/m2": {"ok": True, "data": {"id": "m2", "raw": _raw(From="ceo@corp.com", body="secret")}},
            "GET /messages/m3": {"ok": True, "data": {"id": "m3", "raw": _raw(From="me@x.com", body="deux")}},
        },
    )  # fmt: skip
    out = _tool(secrets, "gmail_get_thread")("t1")
    assert [m["body"].strip() for m in out["data"]["messages"]] == ["un", "deux"]
    assert out["_display"]["hidden_by_filters"] == 1
    assert "secret" not in json.dumps(out["data"])


def test_draft_reply_threads_under_original(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    original = _raw(From="amelie@x.com", Subject="Devis", Message_ID="<abc@mail>")
    calls = _fake_api(
        monkeypatch,
        {"GET /messages/m1": {"ok": True, "data": {"threadId": "t1", "raw": original}}},
    )
    out = _tool(secrets, "gmail_create_draft")(
        "amelie@x.com", "ignored", "Merci !", reply_to_message_id="m1"
    )
    assert out["ok"]
    [posted] = [j for method, path, j in calls if (method, path) == ("POST", "/drafts")]
    assert posted["message"]["threadId"] == "t1"
    sent = _decoded(posted["message"])
    assert sent["In-Reply-To"] == "<abc@mail>" and sent["Subject"] == "Re: Devis"


def test_update_draft_keeps_its_conversation(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    current = _raw(
        Subject="Re: Devis", In_Reply_To="<abc@mail>", References="<abc@mail>"
    )
    calls = _fake_api(
        monkeypatch,
        {"GET /drafts/d1": {"ok": True, "data": {"message": {"threadId": "t1", "raw": current}}}},
    )  # fmt: skip
    assert _tool(secrets, "gmail_update_draft")("d1", "a@x.com", "whatever", "v2")["ok"]
    [put] = [j for method, path, j in calls if (method, path) == ("PUT", "/drafts/d1")]
    sent = _decoded(put["message"])
    assert put["message"]["threadId"] == "t1"
    assert sent["In-Reply-To"] == "<abc@mail>" and sent["Subject"] == "Re: Devis"
    assert "v2" in sent.get_body(("plain",)).get_content()


def test_signature_html_and_bcc(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    calls = _fake_api(
        monkeypatch,
        {
            "GET /settings/sendAs": {
                "ok": True,
                "data": {"sendAs": [{"isDefault": True, "signature": "<b>Thomas</b>"}]},
            }
        },
    )
    _tool(secrets, "gmail_send_email")(
        "a@x.com", "Hi", "Line 1\nLine 2", bcc="boss@x.com", body_html="<p>Hi</p>"
    )
    [posted] = [j for method, path, j in calls if path == "/messages/send"]
    sent = _decoded(posted)
    assert sent["Bcc"] == "boss@x.com"
    assert "Thomas" in sent.get_body(("plain",)).get_content()
    html = sent.get_body(("html",)).get_content()
    assert "<p>Hi</p>" in html and "<b>Thomas</b>" in html


def test_attachments_must_live_in_session_roots(secrets, monkeypatch, tmp_path):
    from coworker.connectors.integration_tools import make_integration_tools
    from coworker.roots import RootDir

    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    root = tmp_path / "work"
    root.mkdir()
    (root / "devis.pdf").write_bytes(b"%PDF")
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    calls = _fake_api(monkeypatch, {})
    tools = make_integration_tools(secrets, roots=[RootDir(path=root, writable=True)])
    draft = next(t for t in tools if t.__name__ == "gmail_create_draft")

    assert "outside" in draft("a@x.com", "s", "b", attachments=[str(outside)])["error"]
    assert draft("a@x.com", "s", "b", attachments=[str(root / "devis.pdf")])["ok"]
    [posted] = [j for method, path, j in calls if path == "/drafts"]
    names = [p.get_filename() for p in _decoded(posted["message"]).iter_attachments()]
    assert names == ["devis.pdf"]


def test_modify_labels_resolves_names_and_rejects_unknown(secrets, monkeypatch):
    gmail_accounts.managed_connect_account(secrets, _account("me@x.com"))
    labels = {
        "ok": True,
        "data": {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Clients"}]},
    }  # fmt: skip
    calls = _fake_api(
        monkeypatch,
        {
            "GET /labels": labels,
            "GET /messages/m1": {"ok": True, "data": {"raw": _raw()}},
        },
    )
    modify = _tool(secrets, "gmail_modify_labels")
    assert modify(message_id="m1", add_labels=["clients"], remove_labels=["INBOX"])[
        "ok"
    ]
    [body] = [j for method, path, j in calls if path == "/messages/m1/modify"]
    assert body == {"addLabelIds": ["Label_7"], "removeLabelIds": ["INBOX"]}
    assert "Clients" in modify(message_id="m1", add_labels=["Nope"])["error"]
    assert "exactly one" in modify(add_labels=["Clients"])["error"]
