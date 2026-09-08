"""Regression tests for the executor/auditor pairing surface (M1).

`/jador:execute` pairs every task worker 1:1 with a live `jador:comment-auditor`
instance, gating the worker's commit on a comment audit requested at a
pre-commit checkpoint. The mechanism spans several files that must stay in
sync with each other:

- ``agents/comment-auditor.md`` — the auditor's own definition, which must
  keep its default-mode (one-shot) behavior intact while adding a paired
  mode.
- ``skills/execute/assets/auditor-prompt.md`` — the prompt used to spawn the
  auditor in paired mode. Its trigger phrase must match the auditor
  definition's trigger phrase exactly, or the auditor silently falls back to
  default (one-shot) mode instead of waiting to be resumed.
- ``skills/execute/assets/agent-prompt.md`` — the worker prompt, which must
  run its comment-audit checkpoint strictly before its commit step, and must
  report the audit outcome back to the orchestrator.
- ``skills/execute/SKILL.md`` — the orchestrator, which must spawn workers
  and auditors with the documented naming and isolation, and mint a fresh
  auditor on every retry.
- ``skills/execute/assets/pairing-protocol.md`` — the protocol doc, which
  documents this as the only pairing mechanism (no alternate branching).

These tests assert on the markdown content directly (no YAML parsing, no new
dependencies) so a future edit can't silently break any of these contracts.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AUDITOR_AGENT = REPO_ROOT / "agents" / "comment-auditor.md"
EXECUTE_ASSETS = REPO_ROOT / "skills" / "execute" / "assets"
AGENT_PROMPT = EXECUTE_ASSETS / "agent-prompt.md"
AUDITOR_PROMPT = EXECUTE_ASSETS / "auditor-prompt.md"
PAIRING_PROTOCOL = EXECUTE_ASSETS / "pairing-protocol.md"
EXECUTE_SKILL = REPO_ROOT / "skills" / "execute" / "SKILL.md"


def test_comment_auditor_agent_keeps_default_mode_and_gains_paired_mode():
    text = AUDITOR_AGENT.read_text()
    assert "tools: [Read, Grep, Glob, Bash]" in text, (
        "comment-auditor.md must declare exactly this tool list — a change "
        "here silently grants or revokes capabilities for both the one-shot "
        "and paired auditor"
    )
    assert "end your turn" in text, (
        "default-mode 'end your turn' language must survive — the paired "
        "mode addition must not have replaced or removed it"
    )
    assert "Do not linger" in text, (
        "default-mode 'Do not linger' language must survive"
    )
    assert "## Paired mode" in text, (
        "comment-auditor.md must retain its paired-mode section"
    )


def test_paired_mode_trigger_phrase_matches_byte_for_byte():
    auditor_text = AUDITOR_AGENT.read_text()
    prompt_text = AUDITOR_PROMPT.read_text()
    trigger = "Mode: paired"
    assert trigger in auditor_text, (
        "comment-auditor.md must reference the exact trigger phrase it "
        "checks for on spawn"
    )
    assert trigger in prompt_text, (
        "auditor-prompt.md must open with the exact trigger phrase the "
        "auditor checks for, or a spawned auditor silently falls back to "
        "default (one-shot) mode instead of waiting to be resumed"
    )


def test_agent_prompt_orders_comment_audit_before_commit():
    text = AGENT_PROMPT.read_text()
    checkpoint_idx = text.find("### 4. Comment Audit Checkpoint")
    commit_idx = text.find("### 5. Commit Changes")
    assert checkpoint_idx != -1, "agent-prompt.md missing the comment audit checkpoint step"
    assert commit_idx != -1, "agent-prompt.md missing the commit step"
    assert checkpoint_idx < commit_idx, (
        "the comment-audit checkpoint must precede the commit step — a "
        "renumbering that reorders these would let the worker commit before "
        "the audit gates it"
    )


def test_agent_prompt_invokes_prune_comments_report_and_non_interactive():
    text = AGENT_PROMPT.read_text()
    assert "/jador:prune-comments" in text, (
        "agent-prompt.md must invoke prune-comments to apply the auditor's "
        "findings"
    )
    assert "--report" in text, (
        "prune-comments must be invoked with --report so it consumes the "
        "auditor's report instead of re-running its own audit"
    )
    assert "--non-interactive" in text, (
        "prune-comments must be invoked with --non-interactive so it never "
        "blocks the worker waiting on user input"
    )


def test_agent_prompt_report_results_requires_comment_audit_line():
    text = AGENT_PROMPT.read_text()
    report_idx = text.find("### 6. Report Results")
    assert report_idx != -1, "agent-prompt.md missing the Report Results section"
    report_section = text[report_idx:]
    assert "Comment audit:" in report_section, (
        "the Report Results section must require a `Comment audit:` line "
        "summarizing the checkpoint outcome"
    )


def test_execute_skill_documents_auditor_naming_and_spawn_contract():
    text = EXECUTE_SKILL.read_text()
    assert "auditor-task-<N>" in text, (
        "SKILL.md must document the auditor-task-<N> naming convention"
    )
    assert "subagent_type: jador:comment-auditor" in text, (
        "SKILL.md must spawn the auditor with subagent_type: "
        "jador:comment-auditor"
    )
    assert "model: sonnet`, no isolation" in text, (
        "SKILL.md must spawn the auditor with no isolation — giving it its "
        "own worktree would strand it away from the worker's files"
    )
    assert "fresh auditor" in text, (
        "SKILL.md must state that every retry gets a fresh auditor rather "
        "than reusing or resuming a prior one"
    )


def test_pairing_protocol_exists_with_no_fallback_branching():
    assert PAIRING_PROTOCOL.exists(), (
        "skills/execute/assets/pairing-protocol.md must exist"
    )
    text = PAIRING_PROTOCOL.read_text()
    lowered = text.lower()
    assert "if m1" not in lowered, (
        "pairing-protocol.md must not branch on whether mechanism M1 is "
        "active — it is the only pairing mechanism, not an optional path"
    )
    assert "orchestrator-mediated fallback" not in lowered or (
        "no orchestrator-mediated fallback" in lowered
        or "there is no orchestrator-mediated fallback" in lowered
    ), (
        "any mention of an orchestrator-mediated fallback must be to deny "
        "its existence, not to define one"
    )
