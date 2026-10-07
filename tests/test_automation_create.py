"""Tests for the GUI-driven `create_automation` path (the "New automation" / template flow).

No network and no LLM: this exercises validation + that a valid create lands in the task store
with a freshly provisioned scratch workspace.
"""

from __future__ import annotations

from pathlib import Path

from coworker.server.manager import SessionManager


def _manager(tmp_path, monkeypatch) -> SessionManager:
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    return SessionManager(data_dir=tmp_path / "data")


def test_create_automation_success(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(
        {
            "title": "Morning news briefing",
            "instructions": "Search the web and write a 5-bullet briefing.",
            "cron": "0 8 * * *",
        }
    )
    assert out["ok"] is True
    task = out["task"]
    assert task["title"] == "Morning news briefing"
    assert task["schedule"] == "Every day at ~8:00 AM"
    # it really landed in the store and is bound to a fresh scratch workspace
    saved = manager.task_store.get(task["id"])
    assert saved is not None
    assert saved.agent == "cowork"
    assert Path(saved.workspace).is_dir()


def test_create_automation_invalid_cron(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(
        {
            "title": "Bad",
            "instructions": "do something",
            "cron": "not-a-cron",
        }
    )
    assert out["ok"] is False
    assert "invalid cron" in out["error"]
    assert manager.task_store.list() == []


def test_create_automation_missing_instructions(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(
        {
            "title": "No instructions",
            "instructions": "  ",
            "cron": "0 8 * * *",
        }
    )
    assert out["ok"] is False
    assert "instructions" in out["error"]
    assert manager.task_store.list() == []


def test_create_automation_requires_schedule(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(
        {"title": "No schedule", "instructions": "do something"}
    )
    assert out["ok"] is False
    assert manager.task_store.list() == []


# -- per-automation model ----------------------------------------------------------


def _base(**extra) -> dict:
    return {
        "title": "Nightly digest",
        "instructions": "Summarize the day.",
        "cron": "0 22 * * *",
        **extra,
    }


def test_create_automation_stores_model(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(_base(model="gpt-5.6-terra"))
    assert out["ok"] is True
    assert manager.task_store.get(out["task"]["id"]).model == "gpt-5.6-terra"
    # the UI reads it back off the public shape
    assert out["task"]["model"] == "gpt-5.6-terra"


def test_create_automation_defaults_to_no_override(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.create_automation(_base())["task"]["model"] is None


def test_update_automation_clears_the_model_override(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    task_id = manager.create_automation(_base(model="gpt-5.6-sol"))["task"]["id"]
    manager.update_automation(task_id, {"title": "Renamed"})
    assert manager.task_store.get(task_id).model == "gpt-5.6-sol"  # untouched
    # "" clears an override back to the app default
    manager.update_automation(task_id, {"model": ""})
    assert manager.task_store.get(task_id).model is None


def test_automation_saved_with_a_thinking_level_still_loads():
    """The fork's old per-automation level was dropped for upstream's per-model settings;
    tasks saved with it must not crash the store."""
    from coworker.automation.models import Schedule, ScheduledTask

    task = ScheduledTask(title="t", instructions="i", schedule=Schedule.from_dict({}), workspace="")
    loaded = ScheduledTask.from_dict({**task.to_dict(), "thinking": "high"})
    assert loaded.id == task.id and not hasattr(loaded, "thinking")


def test_manual_run_seeds_its_session_with_the_automation_model(tmp_path, monkeypatch):
    """The GUI opens a manual run as a normal session, and the composer pushes its own model
    with every turn — so the session record must already carry the automation's model, or
    the run silently executes on the app default (owner-hit 2026-07-28)."""
    manager = _manager(tmp_path, monkeypatch)
    manager.model = "app-default-model"
    task_id = manager.create_automation(_base(model="gpt-5.6-terra"))["task"]["id"]

    run = manager.prepare_manual_run(task_id)
    assert run["ok"] is True
    # returned to the GUI so the composer adopts it before the first turn
    assert run["model"] == "gpt-5.6-terra"
    record = manager.session_store.load(run["session_id"])
    assert record is not None and record.model == "gpt-5.6-terra"


def test_manual_run_without_overrides_uses_the_app_default(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager.model = "app-default-model"
    task_id = manager.create_automation(_base())["task"]["id"]
    assert manager.prepare_manual_run(task_id)["model"] == "app-default-model"


def test_bypass_approvals_is_on_by_default_and_drives_every_run(tmp_path, monkeypatch):
    """One switch per automation: on (the default, old records included) its scheduled and
    manual runs go through in bypass-approvals; off, they ask as before."""
    from coworker.automation.models import ScheduledTask
    from coworker.permissions import Mode

    manager = _manager(tmp_path, monkeypatch)
    manager.mode = Mode.INTERACTIVE  # the app default must not be what makes this pass
    task = manager.create_automation(_base())["task"]
    assert task["bypass_approvals"] is True
    saved = manager.task_store.get(task["id"])
    legacy = {k: v for k, v in saved.to_dict().items() if k != "bypass_approvals"}
    assert ScheduledTask.from_dict(legacy).bypass_approvals is True

    run_sid = manager.prepare_manual_run(task["id"])["session_id"]
    assert manager.session_store.load(run_sid).mode == Mode.BYPASS_APPROVALS.value
    engine = manager._build_task_engine(saved, session_id="s-on")
    assert engine.permissions.mode is Mode.BYPASS_APPROVALS

    manager.update_automation(task["id"], {"bypass_approvals": False})
    saved = manager.task_store.get(task["id"])
    assert saved.bypass_approvals is False
    engine = manager._build_task_engine(saved, session_id="s-off")
    assert engine.permissions.mode is Mode.INTERACTIVE
    run_sid = manager.prepare_manual_run(task["id"])["session_id"]
    assert manager.session_store.load(run_sid).mode == Mode.INTERACTIVE.value
