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


# -- per-automation model + thinking level ---------------------------------------


def _base(**extra) -> dict:
    return {
        "title": "Nightly digest",
        "instructions": "Summarize the day.",
        "cron": "0 22 * * *",
        **extra,
    }


def test_create_automation_stores_model_and_thinking(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(_base(model="gpt-5.6-terra", thinking="High"))
    assert out["ok"] is True
    saved = manager.task_store.get(out["task"]["id"])
    assert saved.model == "gpt-5.6-terra"
    assert saved.thinking == "high"  # normalized
    # the UI reads both back off the public shape
    assert out["task"]["model"] == "gpt-5.6-terra"
    assert out["task"]["thinking"] == "high"


def test_create_automation_defaults_to_no_override(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    task = manager.create_automation(_base())["task"]
    assert task["model"] is None and task["thinking"] is None


def test_create_automation_rejects_unknown_thinking(tmp_path, monkeypatch):
    """An unsupported level would only surface as a provider 400 mid-run."""
    manager = _manager(tmp_path, monkeypatch)
    out = manager.create_automation(_base(thinking="ludicrous"))
    assert out["ok"] is False and "thinking" in out["error"]
    assert manager.task_store.list() == []


def test_update_automation_changes_one_field_at_a_time(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    task_id = manager.create_automation(_base(model="gpt-5.6-sol", thinking="low"))[
        "task"
    ]["id"]

    manager.update_automation(task_id, {"thinking": "high"})
    saved = manager.task_store.get(task_id)
    assert saved.thinking == "high" and saved.model == "gpt-5.6-sol"  # model untouched

    # "" clears an override back to the app default
    manager.update_automation(task_id, {"model": ""})
    saved = manager.task_store.get(task_id)
    assert saved.model is None and saved.thinking == "high"

    assert manager.update_automation(task_id, {"thinking": "nope"})["ok"] is False
    assert manager.task_store.get(task_id).thinking == "high"  # unchanged


def test_manual_run_seeds_its_session_with_the_automation_settings(tmp_path, monkeypatch):
    """The GUI opens a manual run as a normal session, and the composer pushes its own model
    with every turn — so the session record must already carry the automation's model and
    level, or the run silently executes on the app default (owner-hit 2026-07-28)."""
    manager = _manager(tmp_path, monkeypatch)
    manager.model = "app-default-model"
    task_id = manager.create_automation(
        _base(model="gpt-5.6-terra", thinking="high")
    )["task"]["id"]

    run = manager.prepare_manual_run(task_id)
    assert run["ok"] is True
    # returned to the GUI so the composer adopts them before the first turn
    assert run["model"] == "gpt-5.6-terra" and run["thinking"] == "high"

    record = manager.session_store.load(run["session_id"])
    assert record is not None
    assert record.model == "gpt-5.6-terra" and record.thinking == "high"


def test_manual_run_without_overrides_uses_the_app_default(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager.model = "app-default-model"
    task_id = manager.create_automation(_base())["task"]["id"]

    run = manager.prepare_manual_run(task_id)
    assert run["model"] == "app-default-model" and run["thinking"] is None
    assert manager.session_store.load(run["session_id"]).thinking is None


def test_session_engine_carries_the_level_across_follow_ups(tmp_path, monkeypatch):
    """Reopening an automation's run thread must keep reasoning the same way — the level
    lives on the session record, not just on the run's first engine."""
    manager = _manager(tmp_path, monkeypatch)
    task_id = manager.create_automation(
        _base(model="gpt-5.6-terra", thinking="low")
    )["task"]["id"]
    session_id = manager.prepare_manual_run(task_id)["session_id"]

    engine = manager.get_engine(session_id, agent="cowork")
    assert engine is not None
    assert engine.model == "gpt-5.6-terra"
    assert engine.model_settings.get("reasoning_effort") == "low"

    # and it survives a save/rebuild cycle (the engine cache dropped between turns)
    manager.save(session_id, engine)
    manager._engines.pop(session_id, None)
    rebuilt = manager.get_engine(session_id, agent="cowork")
    assert rebuilt.model_settings.get("reasoning_effort") == "low"
