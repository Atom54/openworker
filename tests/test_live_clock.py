"""OPE-192: `live_clock` (config.toml / COWORKER_LIVE_CLOCK) controls the per-turn clock
line. On, the block changes every minute; off, it is static so a provider's prompt cache
stays reusable wherever the block lands. Default on: the owner ruling for scheduling stands."""

from __future__ import annotations

from coworker.config import LIVE_CLOCK_ENV, load_config


def test_live_clock_default_config_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv(LIVE_CLOCK_ENV, raising=False)
    (tmp_path / ".coworker").mkdir()
    cfg = load_config(tmp_path, global_path=tmp_path / "no-global.toml")
    assert cfg.live_clock is True
    (tmp_path / ".coworker" / "config.toml").write_text("live_clock = false\n", encoding="utf-8")
    cfg = load_config(tmp_path, global_path=tmp_path / "no-global.toml")
    assert cfg.live_clock is False
    monkeypatch.setenv(LIVE_CLOCK_ENV, "1")
    cfg = load_config(tmp_path, global_path=tmp_path / "no-global.toml")
    assert cfg.live_clock is True  # env wins
    monkeypatch.setenv(LIVE_CLOCK_ENV, "maybe")
    try:
        load_config(tmp_path, global_path=tmp_path / "no-global.toml")
    except ValueError as exc:
        assert "live_clock" in str(exc)
    else:
        raise AssertionError("a non-boolean live_clock must be rejected")


def test_context_block_has_a_clock_only_when_live(tmp_path, monkeypatch):
    from coworker.agent import build_engine
    from coworker.agents.registry import get_agent
    from coworker.permissions import Mode

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(tmp_path / "scratch"))
    ws = tmp_path / "ws"
    ws.mkdir()

    monkeypatch.setenv(LIVE_CLOCK_ENV, "0")
    engine = build_engine(agent=get_agent("cowork"), workspace=ws, model="gpt-5.5", mode=Mode("bypass-approvals"))
    first = engine.context_provider()
    assert "Now:" not in first
    assert str(ws) in first  # the folders are still there
    assert engine.context_provider() == first  # byte-identical turn to turn

    monkeypatch.setenv(LIVE_CLOCK_ENV, "1")
    engine = build_engine(agent=get_agent("cowork"), workspace=ws, model="gpt-5.5", mode=Mode("bypass-approvals"))
    assert engine.context_provider().startswith("Now: ")
