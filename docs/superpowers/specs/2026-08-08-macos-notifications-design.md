# macOS system notifications for attention events

**Date:** 2026-08-08
**Status:** approved, implementing

## Problem

Nothing tells the user when work finishes or when an agent needs them. Today:

- `_notify_task_done` (`manager.py:3250`) broadcasts `task_done` on the **run's own session socket**. The GUI only holds a socket for the session it is currently showing, and a scheduled run has a session id the user is never viewing. The frame goes nowhere. No frontend handler for `task_done` exists either.
- Pending Inbox items (approval / question / directory / plan) produce **no** app-wide signal at all. In an attended session `_mirror(item)` (`app.py:1671`) is called only when `visibility == VIS_INBOX`, so inline prompts never leave the session socket.
- The desktop shell has no notification capability whatsoever: no `tauri-plugin-notification`, no web `Notification` use, no dock/tray badge. That is why the app has never asked for notification permission — there was nothing to grant.
- `InboxStore.add_notification` exists (`inbox.py:256`) but is called only from tests.

Additionally, `_notify_task_done` is invoked **inside** the `try`, after `run.status = "ok"`. A run that raises jumps to `except`, so a failed run notifies nobody — including over the existing Telegram `notify_target` path.

## Goal

A macOS system notification every time something wants the user's attention, clickable to open the originating session, with per-category and per-automation opt-outs.

## Scope

Four categories, each independently switchable:

| category | source |
|---|---|
| `automation_done` | a scheduled run finished, `status == "ok"` |
| `errors` | a scheduled run finished, `status == "error"` |
| `attention` | any Inbox item created pending (approval / question / directory / plan) |
| `turn_done` | a chat turn finished in a non-automation session |

Clicking a notification focuses the window and opens the originating session.

## macOS findings (macOS 26.6, Darwin 25.6, rustc 1.96)

Measured on a real bundle, not assumed. Three separate locks had to be opened before a
banner appeared; each one failed silently or misleadingly on its own.

- `tauri-plugin-notification`'s desktop path cannot report clicks. `desktop.rs` is `spawn(async move { let _ = notification.show(); })` — fire and forget. `onAction` is Android/iOS only. **The plugin is therefore not used.**
- **The default backend is a dead end.** `notify-rust`'s macOS default is `mac-notification-sys` → `NSUserNotificationCenter` (deprecated). On macOS 26.6 it *accepts* notifications — `show()` returns `Ok`, records land in `~/Library/Group Containers/group.com.apple.usernoted/db2/db` attributed to `com.openworker.desktop` — and **no banner is ever drawn**. A spike that only checks the database reads as success; it is not. The fix is the `preview-macos-un` feature, which swaps in `UNUserNotificationCenter`.
- **The UN backend needs the main run loop idle.** Off the main thread it proceeds only when `CFRunLoop::main().is_waiting()`, and the notification is triggered by the very JS event the main thread is still processing — so the first attempt loses that race with `Mainthread not running`. Hence the retry loop in `notify`.
- **The bundle's code signature identifier must match its `CFBundleIdentifier`.** `npm run tauri build` produces an ad-hoc, linker-signed bundle whose identifier is `openworker_desktop-<hash>`; `UNUserNotificationCenter` answers every request with `macOS rejected the notification request`. Re-signing with `codesign --force --deep --sign - --identifier com.openworker.desktop` makes it accept. **This affects shipped builds, not just local ones** — `/Applications/OpenWorker.app` has the same mismatch, so notifications cannot work there until the signing config is fixed. See "Open issue" below.
- A notification with no action button returns `Closed(Expired)` in ~175 ms. `.action("default", …)` is what arms the response channel — load-bearing, not decoration.
- The closure passed to `wait_for_response` needs an explicit `&NotificationResponse` annotation; inference pins it to one lifetime and fails the higher-ranked bound.
- `record.presented` in the usernoted database is **not** a reliable "was it shown" flag: Slack notifications the user demonstrably sees are also stored with `presented=0`. Only a human confirming the banner counts.

## Open issue: signing

The feature works, but only on a bundle whose signature identifier matches the bundle id.
`tauri.conf.json` sets no `bundle.macOS.signingIdentity`, so builds are ad-hoc/linker-signed
and notifications are rejected. This needs a decision: a Developer ID certificate (also
required for notarised distribution), or a post-build ad-hoc re-sign with an explicit
`--identifier`. Until then a freshly built app will not notify.

## Architecture

### 1. Event contract

One new app-wide event on the existing `/ws/events` stream. One type, not four, so the frontend has one handler and one settings lookup.

```json
{"type": "attention", "data": {
  "reason": "inbox" | "task_done" | "turn_done",
  "session_id": "...",
  "title": "...",
  "body": "...",
  "kind": "approval|question|directory|plan",
  "status": "ok" | "error",
  "task_id": "...",
  "run_id": "...",
  "workspace": "...",
  "agent": "..."
}}
```

`kind` is present only for `reason=inbox`; `status`/`task_id`/`run_id` only for `reason=task_done`. The existing `automation_run_started` event is untouched.

### 2. Backend emit points

**a. `InboxStore.add()` — `on_add` callback.** `inbox.py:124` is the single funnel every `add_*` routes through, covering all nine creation sites in `app.py` and `manager.py`. The callback fires after `_save()`, outside the lock. It sits **after** the `tool_call_id` idempotency early-return, so a durable resume re-raising a known prompt does not re-notify — that falls out of the existing code shape rather than needing a guard.

Fires for `VIS_INLINE` items too. Deliberate: the user wants asks everywhere, and the focus rule (§3) is what suppresses noise, not the server.

**b. `_notify_task_done` — restructured.** Move the call out of the `try` into the `finally`, after `run.finished_at` is set, so failed runs notify. Pass `run.status`. Add `broadcast_event` alongside the existing `broadcast_session` (the latter stays — it is the live-view path). The Telegram sender prefixes `✓` or `✗` by status.

**c. `mark_idle()` — turn finished.** `manager.py:2966`, described in its own comment as "the one shared post-turn moment" — WS, background delivery and durable resume all pass through. Two guards copied from `_maybe_autotitle`: skip `session_id.startswith("__")`, and skip when `task_store.task_for_run_session(session_id)` is set (that session is an automation run and already gets `task_done`; without this the user would get two notifications for one event).

All three are sync callers. Fire-and-forget uses the pattern already at `manager.py:3591`: `asyncio.get_running_loop()` in a `try`, return if there is no loop, `create_task`, and retain the task reference because the loop holds only a weak one.

### 3. Frontend

`surfaces/gui/src/notifications.ts`:

- `shouldNotify(ev, prefs, ctx) -> {title, body} | null` — pure, no I/O, no Tauri, fully testable. Order: master off → null; the category's toggle off → null; `ctx.focused && ev.session_id === ctx.currentSessionId` → null.
- `handleAttentionEvent(msg, state)` — applies `shouldNotify` to one frame and sends.

It deliberately does **not** own a subscription. The first cut called `connectEvents()` itself, which opened a *second* `/ws/events` socket alongside the automation toast's; the e2e fixture registers one socket per page, so pushes reached only one of the two consumers and the toast specs went red. App.tsx's single existing `connectEvents` effect now feeds both consumers.

Focus is read at event time via `document.hasFocus()`. No listener, no state.

**No npm dependency.** `tauri.ts:2` states the convention: the SPA uses the injected `window.__TAURI__` global (`withGlobalTauri: true`, `tauri.conf.json:13`) rather than `@tauri-apps/*` packages, so the browser build carries no Tauri deps. Notifications follow it — `__TAURI__.core.invoke("notify", …)`, optional-chained, inert in browser dev like the other bridges in that file.

Click return: the shell emits a Tauri event, the frontend listens with the existing `__TAURI__.event.listen` bridge (`tauri.ts:98`) and calls `selectSession(sid, ws, agent)` — the function the Inbox already uses via `openSessionFromInbox`. No new navigation logic.

### 4. Rust shell

`notify-rust = { version = "4", features = ["preview-macos-un"] }` in `Cargo.toml`. No plugin, no capability entry, no JS package.

A `notify` command spawns a `std::thread`, builds the notification **with a main action button**, shows it, then blocks on `wait_for_response`. On `Default` or an action, it emits `ow://notification-click` carrying `{session_id, workspace, agent}`; the frontend focuses the window and opens the session.

Bringing the window forward needs no new permission: `core:window:allow-set-focus`, `allow-show` and `allow-unminimize` are already in `capabilities/default.json`.

Ceiling, to be marked with a `ponytail:` comment: one blocked thread per pending notification, ending on click or dismissal. Bounded by notification volume; a pool is the upgrade path if that ever matters.

### 5. Settings

**Global.** `_prefs["notifications"]` = `{enabled, automation_done, turn_done, attention, errors}`, with a getter and `set_notifications(patch)`, surfaced in the `getSettings` payload beside `context_bar` (`manager.py:1879`), behind `POST /v1/settings/notifications`. This mirrors the existing `context_bar` / `nav_layout` / `sessions_peek` prefs, which are already stored server-side despite being pure UI state.

`enabled` defaults to **false**; the four categories default to true. Turning the master on is what triggers the permission request — never startup. macOS has no revocable prompt for this API path, so the UI links to System Settings ▸ Notifications rather than pretending it can re-ask.

**Per-automation.** `notify_on_completion` already exists (`automation/models.py:174`), is persisted (`:255`) and is honoured (`manager.py:3253`). Two gaps: `update_automation` ignores the key, and `ScheduledView` has no control. Both are small additions; the API type is already declared at `api.ts:1686`.

### 6. Testing

- `tests/test_notifications_events.py` — a fake client via `register_event_client`: one `attention` event per Inbox add; **zero** second event for a duplicate `tool_call_id`; `task_done` emitted with `status="error"` when the run raises (locking the bug fixed in §2b).
- `surfaces/gui/src/notifications.test.ts` — `shouldNotify` truth table: master off, each category off, focused + same session, focused + different session, unfocused.
- The Rust/macOS layer has no automated test; it needs a real signed bundle. Covered by the spike above and manual verification.

Also added: a `data-testid` on the automation enable switch. The detail pane now holds two `label.switch` elements, and `e2e/automations-manage.spec.ts` located the first by bare CSS.

## Out of scope

- Windows and Linux notifications. `notify-rust` is cross-platform and the command will compile everywhere, but only macOS is verified here.
- Notification grouping, sounds, and per-persona routing.
- Reviving `InboxStore.add_notification`. It stays unused; `KIND_NOTIFICATION` maps to `reason=inbox` if anything ever creates one.
