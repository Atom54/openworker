"""OPE-192: the per-turn `<system-context>` block (live clock, folders, skills) must land on
the NEWEST outbound message. Attaching it to the last *user* message put it on the task
prompt in every tool loop, so each minute crossing changed message one and the provider
could reuse none of its cached prefix. No engine loop here: `_outbound_messages` alone."""

from __future__ import annotations

from coworker.engine import TurnEngine
from coworker.permissions import PermissionEngine
from coworker.tools import ToolRegistry


def _engine(tmp_path, messages, clock):
    return TurnEngine(
        provider=object(),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="x",
        messages=messages,
        context_provider=lambda: f"Now: 2026-09-17 {clock[0]} (UTC)\nAvailable directories: /app",
    )


def _tool_loop(turns=3):
    msgs = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "Reconstruct the scene file from this image."},
    ]
    for i in range(turns):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{i}", "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}}],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"output {i}"})
    return msgs


def test_tool_loop_block_rides_as_trailing_message_not_on_the_task_prompt(tmp_path):
    clock = ["07:03"]
    eng = _engine(tmp_path, _tool_loop(), clock)
    out = eng._outbound_messages()
    # The task prompt is exactly what the user wrote — no clock glued onto message one.
    assert out[1]["content"] == "Reconstruct the scene file from this image."
    assert "<system-context>" not in out[1]["content"]
    # The block is the final message, framed as automatic context.
    assert out[-1]["role"] == "user"
    assert out[-1]["content"].startswith("<system-context>\n(automatic per-turn context")
    assert "Now: 2026-09-17 07:03" in out[-1]["content"]
    # Everything the model actually said and ran is untouched and in place before it.
    assert out[-2]["role"] == "tool" and out[-2]["content"] == "output 2"
    # Ephemeral: the canonical history has no such message.
    assert eng.messages[-1]["role"] == "tool"


def test_tool_loop_prefix_is_byte_stable_when_only_the_clock_changes(tmp_path):
    """The property that keeps the prompt cache warm: two consecutive turns differ only in
    the trailing block, however the clock moves."""
    clock = ["07:03"]
    eng = _engine(tmp_path, _tool_loop(), clock)
    first = eng._outbound_messages()
    clock[0] = "07:04"  # the minute ticks over between requests
    second = eng._outbound_messages()
    assert first[:-1] == second[:-1]  # identical prefix, byte for byte
    assert first[-1] != second[-1]  # only the ephemeral tail moved
    assert "07:03" in first[-1]["content"] and "07:04" in second[-1]["content"]


def test_tool_loop_next_turn_keeps_the_previous_turns_verbatim(tmp_path):
    """After the loop appends a reply and a new tool result, the earlier turns still match
    what was sent before — the previous trailing block does not linger anywhere."""
    clock = ["07:03"]
    eng = _engine(tmp_path, _tool_loop(turns=2), clock)
    before = eng._outbound_messages()
    eng.messages.append(
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c9", "function": {"name": "run_shell", "arguments": "{}"}}]}
    )
    eng.messages.append({"role": "tool", "tool_call_id": "c9", "content": "output 9"})
    clock[0] = "07:04"
    after = eng._outbound_messages()
    # Everything sent last time, minus its ephemeral tail, is a prefix of this request.
    assert after[: len(before) - 1] == before[:-1]
    assert after[-1]["content"].startswith("<system-context>")
    assert after[-2]["content"] == "output 9"


def test_chat_shape_unchanged_block_still_on_the_newest_user_message(tmp_path):
    clock = ["07:03"]
    msgs = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "draft an email"},
        {"role": "assistant", "content": "draft"},
        {"role": "user", "content": "make it shorter"},
    ]
    eng = _engine(tmp_path, msgs, clock)
    out = eng._outbound_messages()
    # In a chat too, the user's newest message is sent exactly as typed and the note
    # follows as its own message: a provider then caches through the user's text and
    # keeps only the note outside the prefix (the Anthropic converter folds the two into
    # one user message on the wire, so the model sees what it always saw).
    assert len(out) == len(msgs) + 1
    assert out[-2]["content"] == "make it shorter"
    assert out[-1]["role"] == "user"
    assert out[-1]["content"].startswith("<system-context>\n(automatic per-turn context")
    assert "Now: 2026-09-17 07:03" in out[-1]["content"]
    assert out[1]["content"] == "draft an email"  # earlier user turns untouched


def test_chat_prefix_is_byte_stable_when_only_the_clock_changes(tmp_path):
    clock = ["07:03"]
    msgs = [
        {"role": "user", "content": "draft an email"},
        {"role": "assistant", "content": "draft"},
        {"role": "user", "content": "make it shorter"},
    ]
    eng = _engine(tmp_path, msgs, clock)
    first = eng._outbound_messages()
    clock[0] = "07:04"
    second = eng._outbound_messages()
    assert first[:-1] == second[:-1] and first[-1] != second[-1]


def test_chat_shape_with_content_parts_left_untouched(tmp_path):
    clock = ["07:03"]
    # Text-only parts: an image part would route through the provider's capability check,
    # which is a separate concern from placement.
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "text", "text": "closely"}]},
    ]
    eng = _engine(tmp_path, msgs, clock)
    out = eng._outbound_messages()
    assert len(out) == 2
    assert out[0]["content"] == msgs[0]["content"]  # parts message sent as is
    assert out[1]["content"].startswith("<system-context>")


def test_no_context_means_no_block_anywhere(tmp_path):
    eng = TurnEngine(
        provider=object(),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="x",
        messages=_tool_loop(),
        context_provider=lambda: "",
    )
    out = eng._outbound_messages()
    assert out[-1]["role"] == "tool" and "<system-context>" not in str(out)
