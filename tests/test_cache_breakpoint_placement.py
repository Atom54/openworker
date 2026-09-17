"""OPE-192, Anthropic side. The engine sends its per-turn `<system-context>` note as a
trailing user message. On the Anthropic wire that shape defeats the prompt cache outright
(a cache entry made mid-message never matches a later request where the message ends
there — 27/27 misses measured), so this provider relocates the note onto the last user
message before it, i.e. exactly its previous shape, and keeps the breakpoint on the last
block of the final message. With `live_clock` off that shape is fully cacheable."""

from __future__ import annotations

from coworker.providers.anthropic_provider import _add_cache_breakpoints, convert_messages

NOTE = "<system-context>\n(automatic per-turn context, not a message from the user)\nNow: 07:04\n</system-context>"


def _tool_loop():
    return [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "run_shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"},
    ]


def test_trailing_note_is_glued_onto_the_last_user_message_in_a_tool_loop():
    system, conv = convert_messages(_tool_loop() + [{"role": "user", "content": NOTE}])
    assert [m["role"] for m in conv] == ["user", "assistant", "user"]
    # The task prompt carries the note (previous shape); the final message is pure tool
    # results, so the breakpoint sits on a block that is identical next turn.
    assert conv[0]["content"][0]["text"] == "task\n\n" + NOTE
    assert [b["type"] for b in conv[-1]["content"]] == ["tool_result"]
    kw = {"system": system or "s", "messages": conv}
    _add_cache_breakpoints(kw)
    assert "cache_control" in kw["messages"][-1]["content"][-1]


def test_trailing_note_is_glued_onto_the_newest_user_text_in_a_chat():
    system, conv = convert_messages(
        [
            {"role": "user", "content": "draft"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "shorter"},
            {"role": "user", "content": NOTE},
        ]
    )
    assert [m["role"] for m in conv] == ["user", "assistant", "user"]
    assert conv[-1]["content"][0]["text"] == "shorter\n\n" + NOTE
    assert conv[0]["content"][0]["text"] == "draft"  # earlier turns untouched


def test_content_parts_get_the_note_as_a_trailing_text_part():
    system, conv = convert_messages(
        [
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "text", "text": "closely"}]},
            {"role": "user", "content": NOTE},
        ]
    )
    assert len(conv) == 1
    assert [b["text"] for b in conv[0]["content"]] == ["look", "closely", "\n\n" + NOTE]


def test_without_a_trailing_note_nothing_moves():
    system, conv = convert_messages(_tool_loop())
    assert conv[0]["content"][0]["text"] == "task"
    assert [b["type"] for b in conv[-1]["content"]] == ["tool_result"]


def test_a_lone_note_with_no_user_message_is_left_alone():
    system, conv = convert_messages([{"role": "user", "content": NOTE}])
    assert conv[0]["content"][0]["text"] == NOTE
