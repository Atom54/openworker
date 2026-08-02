"""One-click Google sign-in that runs entirely on this computer — and stays signed in.

Why this exists beside the managed (broker) path: the broker's Google app is
parked behind Google's CASA review, and even once it clears, a consent screen
whose publishing status is "Testing" hands out refresh tokens that die after
7 days. Neither gives the "connect once, never again" the daily-driver Gmail
use needs.

So this flow uses the USER'S OWN Google Cloud OAuth client (application type
"Desktop app" — loopback redirect on any port, no redirect URI to register)
with `access_type=offline`. What makes the refresh token permanent is the
consent screen's PUBLISHING STATUS, not verification: "Testing" expires it in
7 days, "In production" doesn't — unverified is fine (one warning screen, a
100-user lifetime cap that a personal client never approaches). An Internal
Workspace app skips the warning too.

What still ends a grant (Google, unavoidable): the user revoking access, six
months with no use at all, a password change while Gmail scopes are granted,
>100 live tokens for the same client, or an admin restricting the service.

Shape-compatible with the managed path on purpose — same
`<connector>:account:<email>` profiles, same access_token/refresh_token/
expires/account fields — so tools, filters, approvals and the accounts GUI
can't tell the two apart. `local_oauth: True` only says WHO renews the token
(Google directly, not the broker).

One client covers every Google connector: Gmail, Calendar and Drive each ask
for their own scopes against the same client, and `include_granted_scopes`
makes the second connector an incremental consent rather than a fresh grant.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
import time
import urllib.parse
from typing import Any, Optional

import httpx

from ..secrets import SecretStore

CLIENT_PROFILE = "google:oauth_client"
CALLBACK_PATH = "/google/oauth/callback"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# Identity only — the email that names the account profile.
_BASE_SCOPES = ("openid", "email")

# Exactly what each connector's tools call, nothing wider. Drive stays
# read-only (the tool layer has no write path); Calendar needs the read/write
# scope because gcal_create/update/delete_event exist.
SCOPES: dict[str, tuple[str, ...]] = {
    "gmail": (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ),
    "google_calendar": ("https://www.googleapis.com/auth/calendar",),
    "google_drive": ("https://www.googleapis.com/auth/drive.readonly",),
}
CONNECTORS = tuple(SCOPES)

_pending: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 600


def _now() -> float:
    return time.time()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# --- the user's OAuth client --------------------------------------------------


def client_creds(secrets: SecretStore) -> tuple[str, str]:
    """(client_id, client_secret) from the environment, else the stored client.
    Empty strings when one-click hasn't been set up on this computer."""
    env_id = os.environ.get("COWORKER_GOOGLE_CLIENT_ID", "").strip()
    if env_id:
        return env_id, os.environ.get("COWORKER_GOOGLE_CLIENT_SECRET", "").strip()
    profile = secrets.get(CLIENT_PROFILE) or {}
    return (
        str(profile.get("client_id") or "").strip(),
        str(profile.get("client_secret") or "").strip(),
    )


def configured(secrets: SecretStore) -> bool:
    return bool(client_creds(secrets)[0])


def client_status(secrets: SecretStore) -> dict[str, Any]:
    """What the GUI shows: whether one-click is live and which client id serves
    it (an id is not a secret — the secret never leaves the store)."""
    client_id, _ = client_creds(secrets)
    return {
        "configured": bool(client_id),
        "client_id": client_id,
        "from_env": bool(os.environ.get("COWORKER_GOOGLE_CLIENT_ID", "").strip()),
        "redirect_uri": redirect_uri(),
    }


def set_client(
    secrets: SecretStore, client_id: str, client_secret: str
) -> dict[str, Any]:
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not client_id:
        return {"ok": False, "error": "client id required"}
    if not client_id.endswith(".apps.googleusercontent.com"):
        return {
            "ok": False,
            "error": "that doesn't look like a Google client id (it ends in .apps.googleusercontent.com)",
        }
    if not client_secret:
        # Desktop-app clients are issued a secret and Google's token endpoint
        # wants it back; a missing one is a copy/paste slip, not a public client.
        return {"ok": False, "error": "client secret required"}
    secrets.put(
        CLIENT_PROFILE,
        {"type": "oauth", "client_id": client_id, "client_secret": client_secret},
    )
    return {"ok": True, **client_status(secrets)}


def clear_client(secrets: SecretStore) -> dict[str, Any]:
    """Forget the OAuth client. Connected accounts keep working until their
    refresh needs it — disconnect them from their pages to fully sign out."""
    secrets.delete(CLIENT_PROFILE)
    return {"ok": True, **client_status(secrets)}


def redirect_uri() -> str:
    """This sidecar's own loopback landing. Desktop-app clients accept any
    loopback port, which is what makes the packaged app's random port work."""
    port = os.environ.get("COWORKER_PORT") or "8765"
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


# --- the browser flow ---------------------------------------------------------


def begin(secrets: SecretStore, connector: str) -> dict[str, Any]:
    """Authorization URL for one connector's consent, PKCE pair held in memory
    until the loopback callback lands (a flow that outlives the sidecar simply
    gets restarted)."""
    if connector not in SCOPES:
        return {"ok": False, "error": f"{connector} is not a Google connector"}
    client_id, _ = client_creds(secrets)
    if not client_id:
        return {"ok": False, "error": "google one-click is not set up on this computer"}

    verifier = _b64url(_secrets.token_bytes(48))
    state = _secrets.token_urlsafe(16)
    for key, pending in list(_pending.items()):  # expire stale attempts
        if float(pending["created"]) < _now() - _PENDING_TTL:
            _pending.pop(key, None)
    _pending[state] = {
        "verifier": verifier,
        "connector": connector,
        "created": _now(),
        "redirect_uri": redirect_uri(),
    }
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(_BASE_SCOPES + SCOPES[connector]),
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        # offline + consent is what mints a refresh token EVERY time: Google
        # withholds it on a re-authorization that only re-uses an existing grant,
        # which would leave a reconnect unable to stay signed in.
        "access_type": "offline",
        "prompt": "consent",
        # Second Google connector = incremental consent on the same grant, so
        # connecting Drive doesn't silently drop the Gmail scopes.
        "include_granted_scopes": "true",
    }
    return {
        "ok": True,
        "authorize_url": AUTH_ENDPOINT + "?" + urllib.parse.urlencode(query),
        "state": state,
    }


def complete(secrets: SecretStore, code: str, state: str) -> dict[str, Any]:
    """Exchange the callback code and store the mailbox/account profile.
    Returns {ok, connector, account} — the browser page reports it back."""
    pending = _pending.pop(state, None)
    if pending is None or float(pending["created"]) < _now() - _PENDING_TTL:
        return {"ok": False, "error": "unknown or expired sign-in attempt"}
    connector = str(pending["connector"])
    client_id, client_secret = client_creds(secrets)
    if not client_id:
        return {"ok": False, "error": "google one-click is not set up on this computer"}

    try:
        resp = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"],
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"google unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": _google_error(resp)}
    token = resp.json()
    if not token.get("access_token"):
        return {"ok": False, "error": "google returned no access token"}

    email = _account_email(token.get("access_token", ""))
    if not email:
        return {"ok": False, "error": "could not read the Google account email"}
    profile = {
        "type": "oauth",
        "enabled": True,
        # Not `managed`: this grant is local, so cloud.refresh_managed_token must
        # never try to renew it through the broker.
        "local_oauth": True,
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "scope": token.get("scope", ""),
        "provider": "google",
        "account": email,
        "expires": _now() + int(token.get("expires_in") or 3600) - 60,
    }
    if not profile["refresh_token"]:
        # Re-consent that returned no refresh token: keep the one already stored
        # rather than downgrading a permanent connection to a one-hour one.
        existing = secrets.get(profile_key(connector, email)) or {}
        profile["refresh_token"] = existing.get("refresh_token", "")
    return {**_store(secrets, connector, profile), "connector": connector}


def _google_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("error_description") or body.get("error") or ""
    except ValueError:
        detail = ""
    return f"google rejected the request ({resp.status_code})" + (
        f": {detail}" if detail else ""
    )


def _account_email(access_token: str) -> str:
    try:
        resp = httpx.get(
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    return str(resp.json().get("email") or "").strip().lower()


# --- storage (one writer per connector's account layer) -----------------------


def profile_key(connector: str, email: str) -> str:
    return f"{connector}:account:{str(email or '').strip().lower()}"


def _store(secrets: SecretStore, connector: str, profile: dict[str, Any]) -> dict:
    if connector == "gmail":
        from . import gmail_accounts

        return gmail_accounts.managed_connect_account(secrets, profile)
    if connector == "google_calendar":
        from . import gcal_accounts

        return gcal_accounts.managed_connect_account(secrets, profile)
    # Drive rides the generic accounts layer (account_field="@identity").
    from . import accounts as _accounts

    email = str(profile.get("account") or "")
    result = _accounts.add_account(secrets, connector, email, profile)
    return {**result, "account": email} if result.get("ok") else result


# --- staying signed in --------------------------------------------------------


def refresh(secrets: SecretStore, key: str) -> Optional[dict[str, Any]]:
    """Renew one locally-granted profile straight against Google. Returns the
    updated profile, or None when it can't be renewed (no refresh token, no
    client, revoked grant) — the GUI's `needs_reauth` covers that case."""
    profile = secrets.get(key) or {}
    if not (profile.get("local_oauth") and profile.get("refresh_token")):
        return None
    client_id, client_secret = client_creds(secrets)
    if not client_id:
        return None
    try:
        resp = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": profile["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        if resp.status_code in (400, 401):
            # invalid_grant: revoked, expired, or password-changed. Drop the dead
            # refresh token so the account surfaces as "Sign in again" instead of
            # retrying a doomed exchange on every tool call. `expires` stays in
            # the past (never 0) — that pair is what the GUI reads as needs_reauth.
            profile.pop("refresh_token", None)
            profile["expires"] = _now() - 1
            secrets.put(key, profile)
        return None
    fresh = resp.json()
    if not fresh.get("access_token"):
        return None
    profile["access_token"] = fresh["access_token"]
    if fresh.get("refresh_token"):  # Google rotates rarely; honor it when it does
        profile["refresh_token"] = fresh["refresh_token"]
    profile["expires"] = _now() + int(fresh.get("expires_in") or 3600) - 60
    secrets.put(key, profile)
    return profile


def ensure_fresh(secrets: SecretStore, key: str, *, leeway: int = 120) -> None:
    """Refresh-on-expiry hook for the tool layer. No-op for broker/manual profiles."""
    profile = secrets.get(key) or {}
    if not profile.get("local_oauth"):
        return
    expires = float(profile.get("expires") or 0)
    if expires and expires > _now() + leeway:
        return
    refresh(secrets, key)
