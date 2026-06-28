"""Detection tests for the screen-scraped confirmation/question prompts.

Fixtures are real renders captured via MVIBE_DEBUG_SCREEN from a live Claude
Code TUI (the AskUserQuestion carousel) plus minimal reconstructions of the
permission / submit / y-n screens.
"""

from __future__ import annotations

from mvibe.prompt_detect import detect


def _lines(block: str) -> list[str]:
    return block.split("\n")


# Real AskUserQuestion question screen (captured: screen-0006.txt). The option
# list is split by a horizontal rule between option 3 and option 4 — the case
# that breaks the generic box extractor.
ASK_SCREEN = """\
❯   用 AskUserQuestion
  工具同时问我两个二选一的问题，内容随意，等我作答
─────────────────────────────────────────────────────────────────
←  ☐ 饮品  ☐ 配菜  ✔ Submit  →

喝热还是冰？

❯ 1. 热
     热饮
  2. 冰
     冰饮
  3. Type something.
─────────────────────────────────────────────────────────────────
  4. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""


SUBMIT_SCREEN = """\
─────────────────────────────────────────────────────────────────
Ready to submit your answers?

❯ 1. Submit answers
  2. Cancel"""


PERMISSION_SCREEN = """\
─────────────────────────────────────────────────────────────────
Do you want to proceed?

❯ 1. Yes
  2. No, and tell Claude what to do differently"""


# An ordinary numbered menu (slash-command autocomplete): no confirm phrase, no
# yes/no options, no ask anchors -> must NOT be treated as a confirmation.
PLAIN_MENU = """\
❯ 1. /add-dir
  2. /agents
  3. /bug"""


YN_SCREEN = """\
Continue? (y/n)"""


def test_ask_screen_detected_as_ask():
    p = detect(_lines(ASK_SCREEN))
    assert p is not None
    assert p.kind == "ask"


def test_ask_screen_captures_all_numbered_options():
    p = detect(_lines(ASK_SCREEN))
    digits = [d for d, _ in p.options]
    assert digits == ["1", "2", "3", "4"]


def test_ask_text_has_question_and_real_options_only():
    p = detect(_lines(ASK_SCREEN))
    # Question line preserved.
    assert "喝热还是冰？" in p.text
    # Real options included.
    assert "1. 热" in p.text
    assert "2. 冰" in p.text
    # Auto-appended anchor options dropped from the forwarded text...
    assert "Type something" not in p.text
    assert "Chat about this" not in p.text
    # ...and the description sub-lines are not mistaken for the question.
    assert "热饮" not in p.text.splitlines()[0]


def test_ask_text_first_line_is_question_not_tabbar_or_rule():
    p = detect(_lines(ASK_SCREEN))
    first = p.text.splitlines()[0]
    assert first == "喝热还是冰？"
    assert "Submit" not in first
    assert "─" not in first


def test_submit_screen_is_ask():
    # The final AskUserQuestion screen is the same arrow-driven widget, so it is
    # tagged "ask" (driven by arrows+Enter), not "numbered" (digit-selectable).
    p = detect(_lines(SUBMIT_SCREEN))
    assert p is not None
    assert p.kind == "ask"
    assert [d for d, _ in p.options] == ["1", "2"]
    assert p.text.splitlines()[0] == "Ready to submit your answers?"


def test_permission_screen_numbered():
    p = detect(_lines(PERMISSION_SCREEN))
    assert p is not None
    assert p.kind == "numbered"


def test_plain_menu_not_a_prompt():
    assert detect(_lines(PLAIN_MENU)) is None


def test_yn_screen():
    p = detect(_lines(YN_SCREEN))
    assert p is not None
    assert p.kind == "yn"


def test_empty_screen_is_none():
    assert detect([]) is None
    assert detect(["", "   ", ""]) is None


def test_key_bytes_are_terminal_sequences():
    from mvibe.wrapper import _KEY_BYTES

    assert _KEY_BYTES["down"] == b"\x1b[B"
    assert _KEY_BYTES["up"] == b"\x1b[A"
    assert _KEY_BYTES["enter"] == b"\r"
    assert _KEY_BYTES["space"] == b" "


# --- ask reply mapping (arrow-key sequences) -------------------------------- #
OPTS = [("1", "热"), ("2", "冰"), ("3", "Type something."), ("4", "Chat about this")]


def test_ask_keys_first_option_is_just_enter():
    from mvibe.bridge import _ask_keys

    assert _ask_keys("1", OPTS) == ["enter"]


def test_ask_keys_nth_option_is_downs_then_enter():
    from mvibe.bridge import _ask_keys

    assert _ask_keys("2", OPTS) == ["down", "enter"]
    assert _ask_keys("3", OPTS) == ["down", "down", "enter"]


def test_ask_keys_yes_no_select_first_last():
    from mvibe.bridge import _ask_keys

    sub = [("1", "Submit answers"), ("2", "Cancel")]
    assert _ask_keys("yes", sub) == ["enter"]
    assert _ask_keys("no", sub) == ["down", "enter"]


def test_ask_keys_invalid_returns_none():
    from mvibe.bridge import _ask_keys

    assert _ask_keys("99", OPTS) is None
    assert _ask_keys("おはよう", OPTS) is None
    assert _ask_keys("1", []) is None  # no options parsed -> cannot map
