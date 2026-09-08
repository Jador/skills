"""Regression tests for `prune-comments`'s `--report` and `--non-interactive`
modes (Tasks 4-5).

Contract under test:

1. `disable-model-invocation: false` is set in the frontmatter — this is the
   exact regression that broke handoff/critique in v3.1.0. Reverting it
   fails silently at runtime (the worker's Skill-tool call just doesn't
   work) with no static signal, which is why it gets its own assertion.
2. `argument-hint` advertises both `--report` and `--non-interactive`.
3. The mode-resolution step exists and states that `--report` skips the
   auditor spawn.
4. The negotiate step states that ambiguous entries are auto-kept and
   logged in non-interactive mode, and that no `AskUserQuestion` is used
   in that mode.
5. The `### Audit open items (` report block template is present.
6. Step headings are a contiguous `### 1.` … `### 8.` sequence (catches a
   botched renumber).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_MD = REPO_ROOT / "skills" / "prune-comments" / "SKILL.md"


def test_disable_model_invocation_is_false():
    # This is the exact regression that broke handoff/critique in v3.1.0.
    # It fails silently at runtime — the worker's Skill-tool call to
    # invoke this skill just doesn't work — with no static signal, so it
    # gets its own dedicated assertion.
    text = SKILL_MD.read_text()
    assert "disable-model-invocation: false" in text, (
        "SKILL.md must set disable-model-invocation: false so the Skill "
        "tool can invoke this skill programmatically"
    )


def test_argument_hint_advertises_both_flags():
    text = SKILL_MD.read_text()
    frontmatter = text.split("---", 2)[1]
    hint_line = next(
        line for line in frontmatter.splitlines() if line.startswith("argument-hint:")
    )
    assert "--report" in hint_line, "argument-hint must advertise --report"
    assert "--non-interactive" in hint_line, (
        "argument-hint must advertise --non-interactive"
    )


def test_mode_resolution_step_skips_auditor_spawn_for_report_mode():
    text = SKILL_MD.read_text()
    assert "Resolve Invocation Mode" in text, (
        "missing a mode-resolution step"
    )
    assert "--report" in text and "skip step 3" in text, (
        "--report mode must state that it skips spawning the auditor "
        "(step 3)"
    )
    assert "comment-auditor" in text


def test_non_interactive_negotiate_step_auto_keeps_and_logs():
    text = SKILL_MD.read_text()
    assert "auto-kept" in text, (
        "non-interactive negotiate step must describe ambiguous entries "
        "as auto-kept"
    )
    assert "No `AskUserQuestion` call is made" in text or (
        "no `AskUserQuestion`" in text.lower()
    ), (
        "non-interactive mode must state that no AskUserQuestion call is "
        "made"
    )


def test_report_step_has_audit_open_items_template():
    text = SKILL_MD.read_text()
    assert "### Audit open items (" in text, (
        "report step must include the machine-consumable "
        "'### Audit open items (' block template"
    )


def test_step_headings_are_contiguous_one_through_eight():
    text = SKILL_MD.read_text()
    headings = re.findall(r"^### (\d+)\.", text, flags=re.MULTILINE)
    numbers = [int(n) for n in headings]
    assert numbers == list(range(1, 9)), (
        f"expected contiguous step headings ### 1. through ### 8., got {numbers}"
    )
