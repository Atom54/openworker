"""The public CLI surface: `openworker machine …`, `version`, and help that lists them.
Older top-level spellings stay working (enrolled boxes and their service units use them)."""

import pytest

from coworker import cli as ow_cli
from coworker.remote import joiner


def test_no_arguments_prints_help_that_lists_the_machine_commands(capsys):
    ow_cli.main([])
    out = capsys.readouterr().out
    assert "usage: openworker <command>" in out
    for verb in ("machine", "join", "up", "status", "keys", "service", "leave", "version"):
        assert verb in out
    # Unlisted until tested as a product surface.
    for hidden in ("tui", "sessions", "inbox", "doctor"):
        assert hidden not in out


@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_flags_print_the_same_help(flag, capsys):
    ow_cli.main([flag])
    assert "usage: openworker <command>" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["version", "--version", "-V"])
def test_version(flag, capsys):
    ow_cli.main([flag])
    out = capsys.readouterr().out.strip()
    assert out.startswith("openworker ") and out.split()[1][0].isdigit()


def test_machine_group_runs_the_headless_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        ow_cli.main(["machine", "status"])
    assert exc.value.code == 0
    assert "not joined" in capsys.readouterr().out


def test_machine_with_no_command_shows_its_help(capsys):
    with pytest.raises(SystemExit) as exc:
        ow_cli.main(["machine"])
    assert exc.value.code == 0
    assert "usage: openworker machine" in capsys.readouterr().out


def test_older_top_level_spelling_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        ow_cli.main(["status"])
    assert exc.value.code == 0
    assert "not joined" in capsys.readouterr().out


def test_keys_is_the_public_name_and_secrets_still_works(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    assert joiner.cli(["keys", "set", "openai", "api_key=sk-local"]) == 0
    assert joiner.cli(["secrets", "list"]) == 0
    out = capsys.readouterr().out
    assert "openai" in out and "sk-local" not in out
