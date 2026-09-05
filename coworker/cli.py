"""CLI entry point. `coworker` launches the TUI; `coworker code` boots the code skill."""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path
from typing import Optional

from .config import load_config
from .conversations import ConversationStore
from .memory import MemorySettingsStore, SQLiteMemoryStore
from .permissions import Mode
from .secrets import state_dir


REMOTE_COMMANDS = ("join", "auth", "up", "status", "leave", "secrets", "service")


def main(argv: Optional[list[str]] = None) -> None:
    import sys

    # Remote-home verbs run headless (no TUI) and must win over the positional
    # `skill` argument — `openworker join <url>` is a command, not a skill.
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in REMOTE_COMMANDS:
        from .remote.joiner import cli as remote_cli

        raise SystemExit(remote_cli(args))

    cfg = load_config()
    parser = argparse.ArgumentParser(
        prog="openworker", description="Agent coworker (TUI)."
    )
    parser.add_argument(
        "skill", nargs="?", default="code", help="skill to launch (default: code)"
    )
    parser.add_argument("--cwd", default=".", help="workspace directory")
    parser.add_argument(
        "--model", default=cfg.model, help="model id, e.g. openai gpt-5.5"
    )
    parser.add_argument(
        "--mode",
        default=cfg.mode,
        choices=["plan", "interactive", "auto", "bypass-approvals", "auto-approve"],
        help="permission mode",
    )
    parser.add_argument("--resume", default=None, help="resume a session id")
    args = parser.parse_args(argv)

    workspace = Path(args.cwd).expanduser().resolve()
    # Unified global store shared with the GUI/server (one place for all conversations).
    data_dir = state_dir()
    # Same on/off switch and user rules the GUI manages (MEMORY-SPEC §4.3/§6). The
    # store is always wired: off means "stop learning", so saved facts stay usable.
    memory_settings = MemorySettingsStore(data_dir / "memory-settings.json")
    memory_store = SQLiteMemoryStore(data_dir / "coworker.db")
    session_store = ConversationStore(data_dir)
    session_store.touch_workspace(os.path.realpath(str(workspace)))

    resume_messages = None
    session_id = args.resume or uuid.uuid4().hex[:12]
    model, mode = args.model, args.mode
    if args.resume:
        record = session_store.load(args.resume)
        if record is not None:
            resume_messages = record.messages
            model, mode = record.model, record.mode

    from .tui.app import CoworkerApp

    app = CoworkerApp(
        workspace=workspace,
        model=model,
        mode=Mode(mode),
        memory_store=memory_store,
        memory_off=not memory_settings.enabled,
        user_rules=memory_settings.user_rules,
        session_store=session_store,
        session_id=session_id,
        resume_messages=resume_messages,
    )
    app.run()


if __name__ == "__main__":
    main()
