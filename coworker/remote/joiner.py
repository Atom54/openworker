"""The box side of remote homes: `openworker join / up / status / leave`.

Joined mode runs the ENTIRE existing server — state dir, engines, the full
ASGI app — with no listener at all. `joiner` dials the controller, proves its
identity, then serves RPC frames by dispatching them into its own app
in-process. The box listens on nothing; the controller is the only party it
ever talks to (one-arrow transport, remote-home-design.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import channel as ch
from .identity import KEY_FILENAME, SEAL_KEY_FILENAME, MachineIdentity, load_or_create

CONFIG_FILENAME = "remote.json"

_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0
_WS_MAX_SIZE = 16 * 1024 * 1024  # match the server's inbound frame cap

# Handshake rejections that retrying can never fix — surface and exit.
_FATAL_REJECTS = {
    "not-enrolled",
    "bad-signature",
    "bad-seal-attestation",
    "protocol-mismatch",
    "already-connected",
    "bad-handshake",
    "wrong-machine",
}


class JoinRejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _config_path(state: Path) -> Path:
    return state / CONFIG_FILENAME


def load_remote_config(state: Path) -> Optional[dict[str, Any]]:
    path = _config_path(state)
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text())
        return cfg if isinstance(cfg, dict) and cfg.get("controller") else None
    except Exception:
        return None


def save_remote_config(state: Path, cfg: dict[str, Any]) -> None:
    from ..secrets import write_private_text

    write_private_text(_config_path(state), json.dumps(cfg, indent=2) + "\n")


def parse_join_url(join_url: str) -> tuple[str, str]:
    """Split http(s)://host:port/j/TOKEN into (controller base, token)."""
    parts = urlsplit(join_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not a join URL: {join_url}")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2 or segments[0] != "j" or not segments[1]:
        raise ValueError(f"not a join URL (expected …/j/<token>): {join_url}")
    return f"{parts.scheme}://{parts.netloc}", segments[1]


def _ws_url(controller: str) -> str:
    parts = urlsplit(controller)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}/ws/machine"


def default_name() -> str:
    return socket.gethostname().split(".")[0] or "machine"


async def _handshake(
    ws, identity: MachineIdentity, name: str, token: Optional[str]
) -> dict[str, Any]:
    hello: dict[str, Any] = {
        "type": "hello",
        "protocol_version": ch.PROTOCOL_VERSION,
        "app_version": ch.app_version(),
        "pubkey": identity.public_key_b64,
        # Sealing key (wallet): pinned at enrollment; deploys are encrypted to it.
        # Attested by the identity key — the acceptor won't pin it otherwise.
        "seal_pubkey": identity.seal_public_key_b64,
        "seal_sig": identity.attest_seal_key_b64(),
        "name": name,
    }
    if token:
        hello["token"] = token
    await ws.send(json.dumps(hello))
    frame = json.loads(await ws.recv())
    if frame.get("type") == "reject":
        raise JoinRejected(str(frame.get("reason", "")), str(frame.get("detail", "")))
    if frame.get("type") != "challenge":
        raise JoinRejected("bad-handshake", f"unexpected frame: {frame.get('type')}")
    signature = identity.sign_b64(ch.decode_body(frame.get("nonce")))
    await ws.send(json.dumps({"type": "auth", "signature": signature}))
    frame = json.loads(await ws.recv())
    if frame.get("type") == "reject":
        raise JoinRejected(str(frame.get("reason", "")), str(frame.get("detail", "")))
    if frame.get("type") != "welcome":
        raise JoinRejected("bad-handshake", f"unexpected frame: {frame.get('type')}")
    return frame


async def _serve_channel(
    ws,
    dispatcher: ch.AsgiDispatcher,
    app,
    identity: Optional[MachineIdentity] = None,
    exporter=None,
    state: Optional[Path] = None,
) -> None:
    """Steady state: dispatch each RPC concurrently so a slow request never
    blocks the next one; bridged WebSocket streams (live sessions) run as their
    own tasks; all replies share one send lock."""
    send_lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()
    streams: dict[str, ch.AsgiWsBridge] = {}

    async def send_frame(frame: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send(json.dumps(frame))

    async def handle(frame: dict[str, Any]) -> None:
        await send_frame(await dispatcher.dispatch(frame))

    async def reply(frame_id: str, status: int, body: dict[str, Any]) -> None:
        await send_frame(
            {
                "type": "rpc_result",
                "id": frame_id,
                "status": status,
                "body_b64": ch.encode_body(json.dumps(body).encode()),
                "content_type": "application/json",
            }
        )

    async def handle_secret_deploy(frame: dict[str, Any]) -> None:
        """Wallet deploy: unseal with OUR private key, write to OUR SecretStore.
        Deploy = copy — from here on this home owns its copy."""
        frame_id = str(frame.get("id"))
        if identity is None:
            await reply(frame_id, 400, {"error": "no identity; cannot unseal"})
            return
        try:
            payload = json.loads(identity.unseal_b64(str(frame.get("sealed_b64", ""))))
            profiles = payload.get("profiles")
            if not isinstance(profiles, dict) or not profiles:
                raise ValueError("no profiles in payload")
            secrets = app.state.manager.secrets
            for profile, data in profiles.items():
                if not isinstance(data, dict):
                    raise ValueError(f"profile '{profile}' is not an object")
                secrets.put(str(profile), data)
            # A deployed provider key behaves like one pasted in Settings:
            # client rebuilt, recommended model surfaced, and it becomes the
            # default when the current one cannot run (fresh sandboxes).
            adopt = getattr(app.state.manager, "adopt_provider_default", None)
            if adopt is not None:
                for profile in profiles:
                    if str(profile).startswith("provider:"):
                        try:
                            adopt(str(profile)[len("provider:"):])
                        except Exception:
                            pass
            # Hot-add listeners: a deployed Slack/GitHub grant (machine
            # handoff, spec §Managed events) should start polling without a
            # restart — the same gateway refresh a local connect triggers.
            if any(
                str(p).startswith(("slack:", "github:")) for p in profiles
            ):
                spawn(app.state.manager.refresh_gateway())
            await reply(frame_id, 200, {"ok": True, "profiles": sorted(profiles)})
        except (ValueError, json.JSONDecodeError) as exc:
            await reply(frame_id, 400, {"error": str(exc)})

    async def handle_managed_grant_sealed(frame: dict[str, Any]) -> None:
        """Browser connect-direct (spec §Cloud-dashboard connect-direct): the
        dashboard tab sealed the broker's callback result — grant fields plus
        the delegation (broker user id, machine credential) — to OUR key. We
        unseal and run the same bundle routine the desktop uses to stage a
        grant for a machine, into our own store, then refresh listeners."""
        frame_id = str(frame.get("id"))
        if identity is None:
            await reply(frame_id, 400, {"error": "no identity; cannot unseal"})
            return
        name = str(frame.get("connector") or "")
        try:
            payload = json.loads(identity.unseal_b64(str(frame.get("sealed_b64", ""))))
            form = payload.get("form")
            if not name or not isinstance(form, dict):
                raise ValueError("connector name and form required")
        except (ValueError, json.JSONDecodeError) as exc:
            await reply(frame_id, 400, {"error": str(exc)})
            return
        from ..connectors.setup import store_managed_grant_bundle

        grant = {
            "user_id": str(payload.get("broker_user_id") or ""),
            "machine_credential": str(payload.get("machine_credential") or ""),
        }
        manager = app.state.manager
        try:
            result = store_managed_grant_bundle(manager.secrets, name, form, grant)
        except Exception as exc:  # noqa: BLE001 — the tab shows the reason
            await reply(frame_id, 400, {"error": str(exc)[:200]})
            return
        if result.get("ok"):
            spawn(manager.refresh_gateway())
            await reply(frame_id, 200, {"ok": True, "account": result.get("account", "")})
        else:
            await reply(frame_id, 400, {"error": str(result.get("error") or "could not store the grant")})

    async def handle_secret_revoke(frame: dict[str, Any]) -> None:
        frame_id = str(frame.get("id"))
        removed = []
        secrets = app.state.manager.secrets
        for profile in frame.get("profiles") or []:
            if secrets.delete(str(profile)):
                removed.append(str(profile))
        await reply(frame_id, 200, {"ok": True, "removed": removed})

    async def handle_connector_connect_sealed(frame: dict[str, Any]) -> None:
        """Remote manual connect (union view): the controller relays
        CIPHERTEXT; the fields unseal here and run this box's own connect
        endpoint — full validation and listener refresh, exactly as if typed
        locally. Credentials never transit readable."""
        frame_id = str(frame.get("id"))
        if identity is None:
            await reply(frame_id, 400, {"error": "no identity; cannot unseal"})
            return
        name = str(frame.get("connector") or "")
        try:
            payload = json.loads(identity.unseal_b64(str(frame.get("sealed_b64", ""))))
            fields = payload.get("fields")
            if not name or not isinstance(fields, dict):
                raise ValueError("connector name and fields required")
        except (ValueError, json.JSONDecodeError) as exc:
            await reply(frame_id, 400, {"error": str(exc)})
            return
        body = json.dumps(
            {"fields": fields, "acknowledge_risk": bool(payload.get("acknowledge_risk"))}
        ).encode()
        await send_frame(
            await dispatcher.dispatch(
                {
                    "id": frame_id,
                    "method": "POST",
                    "path": f"/v1/connectors/{name}/connect",
                    "body_b64": ch.encode_body(body),
                    "content_type": "application/json",
                }
            )
        )

    async def handle_policy(frame: dict[str, Any]) -> None:
        """A newer org policy document (answer to our check, or a controller-
        initiated send): persist, apply, CONFIRM the version now held. The
        machine keeps the record so it can SHOW what governs it offline."""
        frame_id = str(frame.get("id"))
        policy = frame.get("policy")
        if not isinstance(policy, dict):
            await reply(frame_id, 400, {"error": "policy must be an object"})
            return
        try:
            applied, version = apply_policy(app, policy, state=state, exporter=exporter)
        except Exception as exc:  # a bad document must never take the channel down
            await reply(frame_id, 500, {"ok": False, "error": str(exc), "version": policy_version(held_policy(app))})
            return
        await reply(
            frame_id,
            200,
            {"ok": True, "applied": applied, "version": version, "keys": sorted(policy)},
        )

    async def policy_checks() -> None:
        """Tell the controller which version we hold — once right after the
        welcome (so a fresh connection shows truthful status at once), then
        every POLICY_CHECK_SECONDS (jittered). It answers `policy_current` or
        a newer `policy`."""
        import random

        while True:
            await send_frame(
                {
                    "type": "policy_check",
                    "version": policy_version(held_policy(app)),
                    "app_version": ch.app_version(),
                }
            )
            await asyncio.sleep(POLICY_CHECK_SECONDS * random.uniform(0.9, 1.1))

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    check_task = asyncio.create_task(policy_checks())
    tasks.add(check_task)
    check_task.add_done_callback(tasks.discard)

    export_task: Optional[asyncio.Task] = None
    if exporter is not None:
        # The cloud sink for THIS connection's lifetime: audit frames ride the
        # same socket, acks come back through the loop below.
        export_task = asyncio.create_task(exporter.run(exporter.channel_sender(send_frame)))
        tasks.add(export_task)
        export_task.add_done_callback(tasks.discard)

    try:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = frame.get("type")
            if kind == "rpc":
                spawn(handle(frame))
            elif kind == "secret_deploy":
                spawn(handle_secret_deploy(frame))
            elif kind == "secret_revoke":
                spawn(handle_secret_revoke(frame))
            elif kind == "connector_connect_sealed":
                spawn(handle_connector_connect_sealed(frame))
            elif kind == "managed_grant_sealed":
                spawn(handle_managed_grant_sealed(frame))
            elif kind == "policy":
                spawn(handle_policy(frame))
            elif kind == "policy_current":
                pass  # our check found nothing newer
            elif kind == "audit_ack":
                if exporter is not None:
                    exporter.ack(frame)
            elif kind == "ws_open":
                stream_id = str(frame.get("stream"))
                bridge = ch.AsgiWsBridge(
                    app,
                    stream_id,
                    str(frame.get("path", "/")),
                    send_frame,
                    actor=str(frame.get("actor") or ""),
                )
                streams[stream_id] = bridge

                async def run_bridge(b=bridge, sid=stream_id):
                    try:
                        await b.run()
                    finally:
                        streams.pop(sid, None)

                spawn(run_bridge())
            elif kind == "ws_msg":
                bridge = streams.get(str(frame.get("stream")))
                if bridge is not None:
                    bridge.feed_text(str(frame.get("text", "")))
            elif kind == "ws_close":
                bridge = streams.pop(str(frame.get("stream")), None)
                if bridge is not None:
                    bridge.feed_close()
    finally:
        for task in tasks:
            task.cancel()


POLICY_CHECK_SECONDS = 300.0  # the box asks for a newer document this often (spec: 5 min)


def policy_version(doc: Optional[dict[str, Any]]) -> int:
    """The document's version; 0 when unversioned (desktop controllers, tests)."""
    try:
        return max(0, int((doc or {}).get("version") or 0))
    except (TypeError, ValueError):
        return 0


def held_policy(app) -> dict[str, Any]:
    manager = getattr(app.state, "manager", None)
    return dict(getattr(manager, "org_policy", None) or {})


def apply_policy(
    app, policy: dict[str, Any], *, state: Optional[Path] = None, exporter=None
) -> tuple[bool, int]:
    """Persist + apply an org policy DOCUMENT on this box (spec §Versioned org
    policy). Today one field acts: `audit_export` (remote/audit_export.py).
    Unknown keys are kept verbatim so the machine can display them; nothing
    turns on by accident. A document OLDER than the one held is ignored — a
    stale controller never rolls a machine back. Returns (applied, version
    now held)."""
    from .audit_export import ExportPolicy, save_policy

    incoming, current = policy_version(policy), policy_version(held_policy(app))
    if incoming and current and incoming < current:
        return False, current
    if state is not None:
        save_policy(state, policy)
    manager = getattr(app.state, "manager", None)
    if manager is not None:
        manager.org_policy = dict(policy)
    if exporter is not None:
        exporter.set_policy(ExportPolicy.from_policy(policy))
    return True, incoming


def _build_exporter(state: Path, app, welcome: dict[str, Any]):
    from .audit_export import AuditExporter, ExportPolicy, load_policy

    manager = getattr(app.state, "manager", None)
    store = getattr(manager, "audit_store", None)
    if store is None:
        return None
    exporter = AuditExporter(
        state,
        store,
        machine_id=str(welcome.get("machine_id") or ""),
        machine_name=str(welcome.get("name") or ""),
        org_id=str(welcome.get("org_id") or ""),
        policy=ExportPolicy.from_policy(load_policy(state)),
    )
    manager.audit_exporter = exporter
    # The stored document is what governs us until a newer one arrives —
    # restore it so version compare and the settings display see it.
    stored = load_policy(state)
    if stored and not getattr(manager, "org_policy", None):
        manager.org_policy = stored
    return exporter


async def run_joined(
    *,
    state: Path,
    controller: str,
    name: str,
    token: Optional[str] = None,
    app=None,
    once: bool = False,
    on_welcome=None,
    log=print,
) -> None:
    """Dial the controller and serve until stopped.

    `app`/`once`/`on_welcome` exist for tests and for `join` (which must save
    config after the first welcome). With `once=False` this reconnects forever
    with capped exponential backoff — the systemd story is just `up`.
    """
    import websockets

    identity = load_or_create(state)
    if app is None:
        app = _build_local_app(state)
    # Machine-held event queues (machines spec §Managed events) are sealed to
    # this box's pinned key — hand the gateway the unseal so poll transports
    # can open them. Only joined boxes ever have this attribute.
    manager = getattr(app.state, "manager", None)
    if manager is not None:
        manager.machine_unseal = identity.unseal_b64

    exporter = None
    async with _lifespan(app):
        dispatcher = ch.AsgiDispatcher(app)
        try:
            backoff = _BACKOFF_START
            while True:
                try:
                    async with websockets.connect(
                        _ws_url(controller), max_size=_WS_MAX_SIZE
                    ) as ws:
                        welcome = await _handshake(ws, identity, name, token)
                        token = None  # single-use by design; reconnects sign the challenge
                        backoff = _BACKOFF_START
                        log(
                            f"[openworker] joined {controller} as "
                            f"'{welcome.get('name')}' (machine {welcome.get('machine_id')})"
                        )
                        if on_welcome is not None:
                            on_welcome(welcome)
                            on_welcome = None
                        if exporter is None:
                            exporter = _build_exporter(state, app, welcome)
                        # The welcome may carry the org policy (hosted controllers);
                        # a desktop controller sends none and the stored record stands.
                        if isinstance(welcome.get("policy"), dict):
                            apply_policy(app, welcome["policy"], state=state, exporter=exporter)
                        await _serve_channel(
                            ws, dispatcher, app, identity, exporter=exporter, state=state
                        )
                        log("[openworker] controller closed the connection")
                except JoinRejected as exc:
                    if exc.reason in _FATAL_REJECTS:
                        raise
                    log(f"[openworker] rejected ({exc.reason}); retrying")
                except (OSError, websockets.WebSocketException) as exc:
                    log(f"[openworker] connection failed: {exc}")
                if once:
                    return
                log(f"[openworker] reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)
        finally:
            await dispatcher.aclose()


def _build_local_app(state: Path):
    """The full server app over this state dir — engines, sessions, everything.

    No listener exists, so no sidecar token: the only way in is the channel,
    which authenticated at the handshake.
    """
    import os

    from ..config import load_config
    from ..permissions import Mode
    from ..server.app import create_app
    from ..server.manager import SessionManager

    os.environ.pop("COWORKER_API_TOKEN", None)
    cfg = load_config()
    manager = SessionManager(data_dir=state, model=cfg.model, mode=Mode(cfg.mode))
    return create_app(manager)


def _lifespan(app):
    """Run the app's lifespan (gateway start / MCP close) — uvicorn isn't here to do it."""
    return app.router.lifespan_context(app)


# -- CLI ----------------------------------------------------------------------


def cli(argv: Optional[list[str]] = None, prog: str = "openworker machine") -> int:
    from ..secrets import state_dir

    argv = list(sys.argv[1:] if argv is None else argv)
    # `keys` is the public name; `secrets` is the older spelling and keeps working.
    if argv and argv[0] == "secrets":
        argv[0] = "keys"
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run this OpenWorker headless, joined to a controller (the desktop app or OpenWorker Cloud).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_join = sub.add_parser("join", help="enroll with a controller and start serving")
    p_join.add_argument("url", help="join URL from the controller (…/j/<token>)")
    p_join.add_argument("--name", default=None, help="machine name (default: hostname)")

    # `auth join`: no token in hand — ask the controller for approval (device
    # flow), then run the SAME enrollment with the token the approval minted.
    p_auth = sub.add_parser("auth", help="account-approved enrollment (no join URL needed)")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_auth_join = auth_sub.add_parser(
        "join", help="request approval from the controller, then enroll and serve"
    )
    p_auth_join.add_argument("url", help="controller base URL (e.g. http://127.0.0.1:8765)")
    p_auth_join.add_argument("--name", default=None, help="machine name (default: hostname)")

    sub.add_parser("up", help="start serving with the stored identity")
    sub.add_parser("status", help="show this machine's remote-home state")
    # Box-side provisioning (remote-home-design.md §Keys wallet, door 2): write
    # secrets straight into THIS machine's store — the wallet never in the path.
    p_secrets = sub.add_parser("keys", help="manage provider keys stored on this machine")
    secrets_sub = p_secrets.add_subparsers(dest="secrets_command", required=True)
    p_set = secrets_sub.add_parser("set", help="write one profile: NAME key=value …")
    p_set.add_argument("profile")
    p_set.add_argument("pairs", nargs="+", help="key=value fields (e.g. api_key=sk-…)")
    secrets_sub.add_parser("list", help="list profiles (names only, never values)")
    p_leave = sub.add_parser("leave", help="forget enrollment AND identity on this box")
    p_leave.add_argument("--yes", action="store_true", help="skip confirmation")
    # The systemd story: `up` under a unit, restarted forever (P1d).
    p_service = sub.add_parser("service", help="run this joined machine under systemd")
    service_sub = p_service.add_subparsers(dest="service_command", required=True)
    p_install = service_sub.add_parser(
        "install", help="write + enable a user unit that keeps this machine serving"
    )
    p_install.add_argument(
        "--force", action="store_true", help="replace another unit already pinned to this state dir"
    )
    p_uninstall = service_sub.add_parser("uninstall", help="disable and remove the unit")
    p_uninstall.add_argument("--unit", default=None, help="unit file name (default: this machine's)")
    service_sub.add_parser("list", help="show every openworker unit on this box and its state dir")

    args = parser.parse_args(argv)
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)

    if args.command == "join":
        return _cmd_join(state, args.url, args.name)
    if args.command == "auth":
        return _cmd_auth_join(state, args.url, args.name)
    if args.command == "up":
        return _cmd_up(state)
    if args.command == "status":
        return _cmd_status(state)
    if args.command == "leave":
        return _cmd_leave(state, args.yes)
    if args.command == "keys":
        return _cmd_secrets(state, args)
    if args.command == "service":
        return _cmd_service(state, args)
    return 2


def _cmd_join(state: Path, url: str, name: Optional[str]) -> int:
    try:
        controller, token = parse_join_url(url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    chosen = name or default_name()

    def on_welcome(welcome: dict[str, Any]) -> None:
        save_remote_config(
            state,
            {
                "controller": controller,
                "name": welcome.get("name", chosen),
                "machine_id": welcome.get("machine_id", ""),
            },
        )

    return _run(state, controller, chosen, token=token, on_welcome=on_welcome)


def _cmd_auth_join(
    state: Path, url: str, name: Optional[str], poll_interval: Optional[float] = None
) -> int:
    """Device flow: print a code, wait for approval in the controller UI, then
    run the ordinary token join. `poll_interval` overrides the server's pacing
    (tests only)."""
    import httpx

    base = url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        print("error: controller URL must be http(s)", file=sys.stderr)
        return 2
    chosen = name or default_name()
    identity = load_or_create(state)
    try:
        resp = httpx.post(
            f"{base}/v1/remote/device/start",
            json={"name": chosen, "fingerprint": identity.fingerprint},
            timeout=10,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"error: could not reach controller: {exc}", file=sys.stderr)
        return 1
    start = resp.json()
    hint = start.get("verification_url") or start.get("verification_hint") or ""
    print(f"[openworker] this machine: {chosen} (fingerprint {identity.fingerprint})")
    print(f"[openworker] approval code: {start['user_code']}")
    if hint:
        print(f"[openworker] {hint}")
    print("[openworker] waiting for approval…")

    interval = poll_interval if poll_interval is not None else float(start.get("interval") or 5)
    deadline = time.monotonic() + float(start.get("expires_in") or 900)
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            poll = httpx.post(
                f"{base}/v1/remote/device/poll",
                json={"device_code": start["device_code"]},
                timeout=10,
            ).json()
        except httpx.HTTPError:
            continue  # transient (tunnel blip): the grant waits on the controller
        status = poll.get("status")
        if status == "pending":
            continue
        if status == "approved":
            # Join against the URL the user gave us — the base the controller
            # printed into join_url may not be reachable from here (tunnels).
            _, token = parse_join_url(str(poll.get("join_url", "")))
            print("[openworker] approved — enrolling")
            return _cmd_join(state, f"{base}/j/{token}", chosen)
        print(f"error: approval {status or 'failed'}", file=sys.stderr)
        return 1
    print("error: approval timed out", file=sys.stderr)
    return 1


def _cmd_up(state: Path) -> int:
    cfg = load_remote_config(state)
    if cfg is None:
        print(
            "error: this machine has not joined a controller (run `openworker machine join <link>`)",
            file=sys.stderr,
        )
        return 2
    return _run(state, cfg["controller"], str(cfg.get("name") or default_name()))


def _run(state: Path, controller: str, name: str, token=None, on_welcome=None) -> int:
    from ..statelock import EngineBusy, acquire

    # One engine per state dir (statelock.py). A supervisor restart may find
    # the previous process still tearing down, so wait a little before refusing.
    try:
        _lock = acquire(state, timeout=10.0)  # noqa: F841 — held for the process's life
    except EngineBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    try:
        asyncio.run(
            run_joined(
                state=state,
                controller=controller,
                name=name,
                token=token,
                on_welcome=on_welcome,
            )
        )
        return 0
    except KeyboardInterrupt:
        print("\n[openworker] stopped")
        return 0
    except JoinRejected as exc:
        print(f"error: {exc.reason}: {exc.detail or 'rejected by controller'}", file=sys.stderr)
        return 1


def _cmd_secrets(state: Path, args) -> int:
    from ..secrets import SecretStore

    store = SecretStore(state / "secrets.json")
    if args.secrets_command == "list":
        rows = store.status()
        if not rows:
            print("no secrets stored")
        for row in rows:
            print(row["profile"] + (f" ({row['type']})" if row.get("type") else ""))
        return 0
    data: dict[str, str] = {}
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            print(f"error: expected key=value, got '{pair}'", file=sys.stderr)
            return 2
        data[key] = value
    store.put(args.profile, data)
    print(f"stored profile '{args.profile}' ({', '.join(sorted(data))})")
    return 0


SERVICE_UNIT_NAME = "openworker.service"  # legacy default (pre-2026-09-02 installs)
_UNIT_PREFIX = "openworker"


def unit_name_for(machine_name: str) -> str:
    """`openworker-<machine>.service` — one unit per joined identity, so two
    installs on one box can never silently share a name (or a state dir)."""
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (machine_name or "")).strip("-")
    return f"{_UNIT_PREFIX}-{slug}.service" if slug else SERVICE_UNIT_NAME


def _systemd_unit(state: Path, exe: str, name: str = "") -> str:
    """A user unit running `up`: restart forever, state dir pinned explicitly so
    the unit survives shells/relogins with a different environment."""
    label = f" ({name})" if name else ""
    return (
        "[Unit]\n"
        f"Description=OpenWorker headless (joined machine{label})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exe} up\n"
        f"Environment=COWORKER_STATE_DIR={state}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def installed_units(unit_dir: Path) -> list[dict]:
    """Every openworker*.service user unit on this box with the state dir it
    pins — what `service install` reports so a stale twin is visible, not a
    mystery process that returns after every kill."""
    rows = []
    for path in sorted(unit_dir.glob(f"{_UNIT_PREFIX}*.service")):
        state = ""
        try:
            for line in path.read_text().splitlines():
                if line.startswith("Environment=COWORKER_STATE_DIR="):
                    state = line.split("=", 2)[2].strip()
        except OSError:
            continue
        rows.append({"unit": path.name, "state_dir": state, "path": str(path)})
    return rows


def _systemctl_user(*args: str) -> bool:
    import subprocess

    try:
        return (
            subprocess.run(
                ["systemctl", "--user", *args], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, Exception):
        return False


def _cmd_service(state: Path, args, platform: Optional[str] = None, home: Optional[Path] = None) -> int:
    platform = platform or sys.platform
    home = home or Path.home()
    if platform != "linux":
        print(
            "error: `service install` targets Linux/systemd (the VM/server the box "
            "runs on). On macOS just run `openworker machine up` in a terminal or via launchd.",
            file=sys.stderr,
        )
        return 2
    unit_dir = home / ".config" / "systemd" / "user"
    cfg = load_remote_config(state)
    derived = unit_name_for(str(cfg.get("name") or "")) if cfg else SERVICE_UNIT_NAME
    if args.service_command == "uninstall":
        wanted = getattr(args, "unit", None) or derived
        unit_path = unit_dir / wanted
        if not unit_path.exists() and (unit_dir / SERVICE_UNIT_NAME).exists():
            wanted, unit_path = SERVICE_UNIT_NAME, unit_dir / SERVICE_UNIT_NAME
        _systemctl_user("disable", "--now", wanted)
        unit_path.unlink(missing_ok=True)
        _systemctl_user("daemon-reload")
        print(f"removed {unit_path}")
        return 0
    if args.service_command == "list":
        rows = installed_units(unit_dir)
        if not rows:
            print("no openworker units installed")
        for row in rows:
            print(f"{row['unit']}  state={row['state_dir'] or '?'}")
        return 0
    if cfg is None:
        print(
            "error: this machine has not joined a controller yet — run "
            "`openworker machine join <link>` first, then install the service.",
            file=sys.stderr,
        )
        return 2
    import shutil

    # Other units on this box: a stale twin on the SAME state dir is refused
    # (two engines, one SQLite); twins on other state dirs are reported so the
    # operator knows a second engine will be running here.
    others = [r for r in installed_units(unit_dir) if r["unit"] != derived]
    same = [r for r in others if r["state_dir"] and Path(r["state_dir"]) == Path(state)]
    if same and not getattr(args, "force", False):
        for r in same:
            print(
                f"error: {r['unit']} already runs on this state dir "
                f"({state}). Remove it first (`openworker machine service uninstall --unit "
                f"{r['unit']}`) or pass --force to replace it.",
                file=sys.stderr,
            )
        return 2
    for r in same:
        _systemctl_user("disable", "--now", r["unit"])
        Path(r["path"]).unlink(missing_ok=True)
        print(f"replaced {r['unit']}")
    for r in others:
        if r not in same:
            print(
                f"note: {r['unit']} is also installed (state {r['state_dir'] or '?'}) — "
                "a second engine on this box; remove it if it is stale."
            )

    exe = shutil.which("openworker") or f"{sys.executable} -m coworker.cli"
    unit_path = unit_dir / derived
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit(state, exe, str(cfg.get("name") or "")))
    print(f"wrote {unit_path}")
    if _systemctl_user("daemon-reload") and _systemctl_user("enable", "--now", derived):
        print(f"service enabled and started (systemctl --user status {derived}).")
        print(
            "To keep it running after logout/reboot without a login session:\n"
            f"  sudo loginctl enable-linger {__import__('getpass').getuser()}"
        )
    else:
        print(
            "systemd user session not reachable from here — enable it manually:\n"
            "  systemctl --user daemon-reload\n"
            f"  systemctl --user enable --now {derived}"
        )
    return 0

def _cmd_status(state: Path) -> int:
    cfg = load_remote_config(state)
    key_path = state / KEY_FILENAME
    print(f"state dir:   {state}")
    if key_path.exists():
        identity = load_or_create(state)
        print(f"identity:    {identity.fingerprint} (ed25519)")
        # The dashboard's Keys card shows this same fingerprint — comparing
        # the two is how a user verifies a browser-sealed deploy targets THIS
        # box and not a middlebox's substituted key.
        print(f"sealing key: {identity.seal_fingerprint} (x25519)")
    else:
        print("identity:    none (created on first join)")
    if cfg is None:
        print("controller:  not joined")
    else:
        print(f"controller:  {cfg['controller']}")
        print(f"name:        {cfg.get('name', '')}")
        print(f"machine id:  {cfg.get('machine_id', '')}")
    return 0


def _cmd_leave(state: Path, yes: bool) -> int:
    cfg = load_remote_config(state)
    key_path = state / KEY_FILENAME
    if cfg is None and not key_path.exists():
        print("nothing to forget — this machine never joined a controller")
        return 0
    if not yes:
        answer = input(
            "Forget enrollment AND this machine's identity? Re-joining will need a "
            "fresh join URL. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    _config_path(state).unlink(missing_ok=True)
    key_path.unlink(missing_ok=True)
    (state / SEAL_KEY_FILENAME).unlink(missing_ok=True)
    print("left. Remove the machine from the controller's Machines panel too.")
    return 0
