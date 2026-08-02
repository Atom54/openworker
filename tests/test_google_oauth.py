"""Local one-click Google sign-in: the flow that stays signed in.

Everything is offline — Google's token and userinfo endpoints are stubbed at the
httpx boundary. The invariants under test are the promises the feature makes:
one client covers every Google connector, the consent always asks for a refresh
token, a stored account renews itself without any user action, and a grant Google
has killed degrades to "sign in again" instead of retrying forever.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

from coworker import cloud
from coworker.config import Config
from coworker.connectors import google_oauth
from coworker.connectors.setup import connector_list
from coworker.secrets import SecretStore


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("COWORKER_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("COWORKER_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("COWORKER_PORT", "8765")
    google_oauth._pending.clear()
    return SecretStore(path=tmp_path / "state" / "secrets.json")


@pytest.fixture
def client(secrets):
    google_oauth.set_client(secrets, "abc.apps.googleusercontent.com", "GOCSPX-shh")
    return secrets


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _query(url: str) -> dict[str, str]:
    return {
        k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
    }


# --- the client ---------------------------------------------------------------


def test_client_must_look_like_a_google_client(secrets):
    assert not google_oauth.set_client(secrets, "nope", "GOCSPX-shh")["ok"]
    assert not google_oauth.set_client(secrets, "abc.apps.googleusercontent.com", "")["ok"]
    assert not google_oauth.configured(secrets)


def test_env_client_overrides_the_store_and_is_flagged_readonly(secrets, monkeypatch):
    google_oauth.set_client(secrets, "stored.apps.googleusercontent.com", "GOCSPX-a")
    monkeypatch.setenv("COWORKER_GOOGLE_CLIENT_ID", "env.apps.googleusercontent.com")
    monkeypatch.setenv("COWORKER_GOOGLE_CLIENT_SECRET", "GOCSPX-b")
    status = google_oauth.client_status(secrets)
    assert status["client_id"] == "env.apps.googleusercontent.com"
    assert status["from_env"] is True


def test_one_client_unpauses_every_google_connector(secrets):
    paused = {c["name"]: c["managed_paused"] for c in connector_list(secrets)}
    assert all(paused[name] for name in google_oauth.CONNECTORS)  # broker CASA pause

    google_oauth.set_client(secrets, "abc.apps.googleusercontent.com", "GOCSPX-shh")
    entries = {c["name"]: c for c in connector_list(secrets)}
    for name in google_oauth.CONNECTORS:
        assert entries[name]["managed_paused"] is False
        assert entries[name]["google_client_ready"] is True
    # Non-Google connectors are untouched by any of it.
    assert "google_client_ready" not in entries["slack"]


# --- consent ------------------------------------------------------------------


def test_begin_requires_a_client(secrets):
    out = google_oauth.begin(secrets, "gmail")
    assert not out["ok"] and "not set up" in out["error"]


def test_begin_asks_for_a_refresh_token_every_time(client):
    query = _query(google_oauth.begin(client, "gmail")["authorize_url"])
    # offline + consent together are what guarantee a refresh token even on a
    # reconnect — without them the connection would silently last one hour.
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256"
    assert query["include_granted_scopes"] == "true"
    assert query["redirect_uri"] == "http://127.0.0.1:8765/google/oauth/callback"


def test_each_connector_asks_only_for_its_own_scopes(client):
    scopes = {
        name: set(_query(google_oauth.begin(client, name)["authorize_url"])["scope"].split())
        for name in google_oauth.CONNECTORS
    }
    assert "https://www.googleapis.com/auth/gmail.send" in scopes["gmail"]
    assert not any("gmail" in s for s in scopes["google_calendar"])
    assert scopes["google_drive"] & {"https://www.googleapis.com/auth/drive.readonly"}
    # Drive never asks for write access — the tool layer has no write path.
    assert not any(s == "https://www.googleapis.com/auth/drive" for s in scopes["google_drive"])


def test_begin_rejects_a_non_google_connector(client):
    assert not google_oauth.begin(client, "slack")["ok"]


# --- callback -----------------------------------------------------------------


def _stub_exchange(monkeypatch, token: dict, email: str = "me@eloqa.app"):
    monkeypatch.setattr(
        google_oauth.httpx, "post", lambda *a, **k: FakeResponse(200, token)
    )
    monkeypatch.setattr(
        google_oauth.httpx, "get", lambda *a, **k: FakeResponse(200, {"email": email})
    )


@pytest.mark.parametrize(
    "connector,key",
    [
        ("gmail", "gmail:account:me@eloqa.app"),
        ("google_calendar", "google_calendar:account:me@eloqa.app"),
        ("google_drive", "google_drive:account:me@eloqa.app"),
    ],
)
def test_callback_stores_a_refreshable_account(client, monkeypatch, connector, key):
    state = google_oauth.begin(client, connector)["state"]
    _stub_exchange(
        monkeypatch,
        {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600},
    )
    out = google_oauth.complete(client, "code-1", state)
    assert out["ok"] and out["account"] == "me@eloqa.app"

    profile = client.get(key)
    assert profile["access_token"] == "at-1"
    assert profile["refresh_token"] == "rt-1"
    # Local grants must never be routed to the broker's refresh endpoint.
    assert profile["local_oauth"] is True and not profile.get("managed")
    # The connector reads as connected through the normal listing path.
    entry = {c["name"]: c for c in connector_list(client)}[connector]
    assert entry["connected"] is True
    assert entry["accounts"][0]["needs_reauth"] is False


def test_callback_state_is_single_use(client, monkeypatch):
    state = google_oauth.begin(client, "gmail")["state"]
    _stub_exchange(monkeypatch, {"access_token": "at-1", "refresh_token": "rt-1"})
    assert google_oauth.complete(client, "code-1", state)["ok"]
    assert not google_oauth.complete(client, "code-1", state)["ok"]


def test_reconsent_without_a_refresh_token_keeps_the_stored_one(client, monkeypatch):
    state = google_oauth.begin(client, "gmail")["state"]
    _stub_exchange(monkeypatch, {"access_token": "at-1", "refresh_token": "rt-1"})
    google_oauth.complete(client, "code-1", state)

    state = google_oauth.begin(client, "gmail")["state"]
    _stub_exchange(monkeypatch, {"access_token": "at-2"})  # no refresh_token echoed
    google_oauth.complete(client, "code-2", state)
    profile = client.get("gmail:account:me@eloqa.app")
    assert profile["access_token"] == "at-2"
    assert profile["refresh_token"] == "rt-1"  # not downgraded to a 1-hour connection


# --- staying signed in ---------------------------------------------------------


def _connected(secrets, *, expires: float, refresh_token: str = "rt-1") -> str:
    key = "gmail:account:me@eloqa.app"
    secrets.put(
        key,
        {
            "type": "oauth",
            "enabled": True,
            "local_oauth": True,
            "access_token": "at-old",
            "refresh_token": refresh_token,
            "account": "me@eloqa.app",
            "expires": expires,
        },
    )
    return key


def test_expired_token_renews_itself(client, monkeypatch):
    key = _connected(client, expires=time.time() - 10)
    monkeypatch.setattr(
        google_oauth.httpx,
        "post",
        lambda *a, **k: FakeResponse(200, {"access_token": "at-new", "expires_in": 3600}),
    )
    google_oauth.ensure_fresh(client, key)
    profile = client.get(key)
    assert profile["access_token"] == "at-new"
    assert profile["refresh_token"] == "rt-1"  # kept when Google doesn't rotate it
    assert profile["expires"] > time.time()


def test_live_token_is_left_alone(client, monkeypatch):
    key = _connected(client, expires=time.time() + 3600)
    monkeypatch.setattr(
        google_oauth.httpx,
        "post",
        lambda *a, **k: pytest.fail("refreshed a token that was still valid"),
    )
    google_oauth.ensure_fresh(client, key)


def test_revoked_grant_becomes_sign_in_again(client, monkeypatch):
    key = _connected(client, expires=time.time() - 10)
    monkeypatch.setattr(
        google_oauth.httpx,
        "post",
        lambda *a, **k: FakeResponse(400, {"error": "invalid_grant"}),
    )
    google_oauth.ensure_fresh(client, key)
    assert not client.get(key).get("refresh_token")
    row = {c["name"]: c for c in connector_list(client)}["gmail"]["accounts"][0]
    assert row["needs_reauth"] is True


def test_the_tool_layer_funnel_refreshes_local_grants(client, monkeypatch):
    """cloud.ensure_fresh_connector_token is what every tool call goes through:
    it must renew a local grant against GOOGLE, never the broker."""
    key = _connected(client, expires=time.time() - 10)
    called: list[str] = []

    def post(url, *a, **k):
        called.append(url)
        return FakeResponse(200, {"access_token": "at-new", "expires_in": 3600})

    monkeypatch.setattr(google_oauth.httpx, "post", post)
    cloud.ensure_fresh_connector_token(client, Config(), "gmail", profile_key=key)
    assert called == [google_oauth.TOKEN_ENDPOINT]
    assert client.get(key)["access_token"] == "at-new"


def test_routes_wire_the_whole_loop(tmp_path, monkeypatch):
    """Save a client over REST, start a connect, land the loopback callback —
    the three routes the GUI and the browser actually use."""
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("COWORKER_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("COWORKER_PORT", "8765")
    google_oauth._pending.clear()
    manager = SessionManager(workspace=tmp_path)
    api = TestClient(create_app(manager))

    assert api.get("/v1/google/oauth-client").json()["configured"] is False
    saved = api.post(
        "/v1/google/oauth-client",
        json={"client_id": "abc.apps.googleusercontent.com", "client_secret": "GOCSPX-shh"},
    ).json()
    assert saved["ok"] and saved["configured"] is True

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    started = api.post("/v1/connectors/gmail/connect-managed", json={}).json()
    assert started["ok"] and opened and opened[0].startswith(google_oauth.AUTH_ENDPOINT)

    _stub_exchange(monkeypatch, {"access_token": "at-1", "refresh_token": "rt-1"})
    landed = api.get(
        "/google/oauth/callback", params={"code": "c", "state": started["state"]}
    )
    assert landed.status_code == 200 and "Gmail connected" in landed.text
    assert manager.secrets.get("gmail:account:me@eloqa.app")["refresh_token"] == "rt-1"

    assert api.delete("/v1/google/oauth-client").json()["configured"] is False


def test_manual_profiles_are_never_touched(client, monkeypatch):
    key = "gmail:account:pasted@eloqa.app"
    client.put(key, {"type": "oauth", "access_token": "pasted", "expires": 0})
    monkeypatch.setattr(
        google_oauth.httpx, "post", lambda *a, **k: pytest.fail("refreshed a manual paste")
    )
    google_oauth.ensure_fresh(client, key)
    assert client.get(key)["access_token"] == "pasted"
