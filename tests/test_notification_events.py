"""App-wide `attention` events — the feed behind the desktop system notification.

Three emit points, one event type discriminated by `reason`. What matters here is that an
event reaches the app-wide stream at all: before this, a finished scheduled run only ever
broadcast to its own session socket, which the GUI never holds open.
"""

from __future__ import annotations

import asyncio

import pytest

from coworker.automation import Schedule, ScheduledTask
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.server.manager import SessionManager


class ScriptedProvider(ProviderClient):
    """Returns queued AssistantTurns; an empty queue means the turn is never taken."""

    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _task(**kw) -> ScheduledTask:
    kw.setdefault("title", "Daily brief")
    kw.setdefault("instructions", "brief me")
    kw.setdefault("schedule", Schedule(kind="cron", cron="10 19 * * *"))
    kw.setdefault("workspace", "/tmp/cw-notify")
    return ScheduledTask(**kw)


def _collector(manager) -> list[dict]:
    """Register a fake /ws/events client and return the list it fills."""
    seen: list[dict] = []

    async def send(msg):
        seen.append(msg)

    manager.register_event_client(send)
    return seen


def _attention(seen: list[dict]) -> list[dict]:
    return [m["data"] for m in seen if m.get("type") == "attention"]


@pytest.mark.asyncio
async def test_inbox_item_announces_attention(tmp_path):
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    seen = _collector(manager)

    manager.inbox.add_approval("s1", "Run `write_file`?", body="path: /tmp/x")
    await asyncio.sleep(0)  # let the fire-and-forget task run

    events = _attention(seen)
    assert len(events) == 1
    assert events[0]["reason"] == "inbox"
    assert events[0]["kind"] == "approval"
    assert events[0]["session_id"] == "s1"
    assert events[0]["title"] == "Run `write_file`?"


@pytest.mark.asyncio
async def test_inline_visibility_still_announces(tmp_path):
    """An attended session parks prompts as VIS_INLINE and never mirrors them. They must
    still reach the app-wide stream — suppressing noise is the client's focus rule, not
    the server's job."""
    from coworker.inbox import VIS_INLINE

    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    seen = _collector(manager)

    manager.inbox.add_question("s1", "Which region?", visibility=VIS_INLINE)
    await asyncio.sleep(0)

    assert len(_attention(seen)) == 1


@pytest.mark.asyncio
async def test_duplicate_tool_call_does_not_reannounce(tmp_path):
    """A durable resume re-raises the same prompt for the same tool_call_id. The store
    returns the existing item, so the user must not be pinged twice for one question."""
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    seen = _collector(manager)

    first = manager.inbox.add_approval("s1", "Run `shell`?", tool_call_id="call-1")
    await asyncio.sleep(0)
    again = manager.inbox.add_approval("s1", "Run `shell`?", tool_call_id="call-1")
    await asyncio.sleep(0)

    assert again.id == first.id
    assert len(_attention(seen)) == 1


@pytest.mark.asyncio
async def test_failed_run_still_notifies(tmp_path, monkeypatch):
    """The regression this whole feature uncovered: `_notify_task_done` used to sit inside
    the try, after `run.status = "ok"`, so a run that raised told nobody at all."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()

    # The engine swallows provider errors into an `error` event, so failing the provider
    # would still finish "ok". Blow up the run itself — that is the path `except` guards.
    class BoomEngine:
        messages: list = []

        async def run(self, *a, **kw):
            raise RuntimeError("run exploded")
            yield  # unreachable; makes this an async generator

    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    monkeypatch.setattr(manager, "_build_task_engine", lambda *a, **kw: BoomEngine())
    seen = _collector(manager)
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    run = await manager._run_scheduled_task(task, trigger="manual")

    assert run.status == "error"
    done = [e for e in _attention(seen) if e["reason"] == "task_done"]
    assert len(done) == 1
    assert done[0]["status"] == "error"
    assert done[0]["task_id"] == task.id


@pytest.mark.asyncio
async def test_successful_run_announces_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(
        data_dir=tmp_path / "data",
        provider=ScriptedProvider(
            [AssistantTurn(text="All quiet.", finish_reason="stop")]
        ),
    )
    seen = _collector(manager)
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    run = await manager._run_scheduled_task(task, trigger="manual")

    assert run.status == "ok"
    done = [e for e in _attention(seen) if e["reason"] == "task_done"]
    assert len(done) == 1 and done[0]["status"] == "ok"
    # The run's own session is a `__run__…` id — carried so a click can open it.
    assert done[0]["session_id"] == run.session_id


@pytest.mark.asyncio
async def test_silenced_automation_announces_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(
        data_dir=tmp_path / "data",
        provider=ScriptedProvider(
            [AssistantTurn(text="All quiet.", finish_reason="stop")]
        ),
    )
    seen = _collector(manager)
    task = _task(workspace=str(ws), agent="cowork", notify_on_completion=False)
    manager.task_store.save(task)

    await manager._run_scheduled_task(task, trigger="manual")

    assert [e for e in _attention(seen) if e["reason"] == "task_done"] == []


@pytest.mark.asyncio
async def test_automation_run_session_skips_turn_done(tmp_path, monkeypatch):
    """mark_idle fires for the run's session too. Without the guard the user would get
    both a turn_done and a task_done for one finished automation."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(
        data_dir=tmp_path / "data",
        provider=ScriptedProvider(
            [AssistantTurn(text="All quiet.", finish_reason="stop")]
        ),
    )
    seen = _collector(manager)
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    run = await manager._run_scheduled_task(task, trigger="manual")
    manager.mark_idle(run.session_id)
    await asyncio.sleep(0)

    assert [e for e in _attention(seen) if e["reason"] == "turn_done"] == []


@pytest.mark.asyncio
async def test_internal_sessions_skip_turn_done(tmp_path):
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    seen = _collector(manager)

    manager.mark_idle("__task__abc")
    await asyncio.sleep(0)

    assert _attention(seen) == []


def test_notification_prefs_merge_partially(tmp_path):
    """Toggling one switch must not clear the others."""
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))

    assert manager.notifications() == {
        "enabled": False,
        "automation_done": True,
        "turn_done": True,
        "attention": True,
        "errors": True,
    }

    manager.set_notifications({"enabled": True})
    manager.set_notifications({"turn_done": False})

    prefs = manager.notifications()
    assert prefs["enabled"] is True
    assert prefs["turn_done"] is False
    assert prefs["attention"] is True


def test_update_automation_sets_notify_on_completion(tmp_path):
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider([]))
    task = _task()
    manager.task_store.save(task)

    manager.update_automation(task.id, {"notify_on_completion": False})
    assert manager.task_store.get(task.id).notify_on_completion is False

    # A PATCH that doesn't mention the key leaves it alone.
    manager.update_automation(task.id, {"title": "Renamed"})
    assert manager.task_store.get(task.id).notify_on_completion is False
