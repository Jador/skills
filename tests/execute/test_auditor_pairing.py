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

Two more contracts run through all five files and are pinned here as exact
substrings:

- **Worker naming.** The orchestrator names each worker `worker-task-<N>`
  (or `worker-task-<N>-retry<k>` for a retry) and passes it to the paired
  auditor as `{{WORKER_NAME}}`, so the auditor can address it by name.
- **Reply transport and turn model.** The auditor replies to the worker with
  `SendMessage` (never plain output, which goes to the orchestrator), and
  both agents load it via `ToolSearch("select:SendMessage")` before their
  first send. Neither agent waits inside a turn — each ends its turn after
  sending, using the exact sentence "end your turn now; the next message
  resumes you", and the worker's post-checkpoint turn carries the fixed
  line `Awaiting audit from {{AUDITOR_NAME}}.` so the orchestrator can tell
  a waiting worker (that line present, no `**Status:**` block) from a
  finished one, without relying on the line being the literal last thing
  printed.
- **Done guard and single-report rule.** Once a worker has sent its step-6
  `**Status:**` report it is done — a later message (e.g. a duplicate
  report) gets no action and an immediate end of turn. Symmetrically, the
  auditor sends exactly one report per checkpoint. And if the auditor's
  report already arrived before the worker gets to close its checkpoint
  turn, the worker skips the `Awaiting audit from {{AUDITOR_NAME}}.` line
  and continues straight into applying the report.

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
    assert "tools: [Read, Grep, Glob, Bash, ToolSearch, SendMessage]" in text, (
        "comment-auditor.md must declare exactly this tool list — this "
        "documents the pairing's tool dependency (the paired auditor needs "
        "ToolSearch to load SendMessage, and SendMessage itself, to reply to "
        "its worker). It does not assert that the harness enforces this "
        "allowlist; that is a separate, unresolved question"
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


def _comment_auditor_paired_slice() -> str:
    """The ``## Paired mode`` section of comment-auditor.md, excluding the
    unrelated default-mode section (``## How you end``) that follows it, so
    default-mode text can't accidentally satisfy a paired-mode assertion."""
    text = AUDITOR_AGENT.read_text()
    start = text.find("## Paired mode")
    end = text.find("## How you end")
    assert start != -1 and end != -1 and start < end, (
        "comment-auditor.md must have a '## Paired mode' section followed "
        "later by '## How you end'"
    )
    return text[start:end]


def test_auditor_prompt_names_worker_and_replies_via_sendmessage():
    prompt_text = AUDITOR_PROMPT.read_text()
    assert "{{WORKER_NAME}}" in prompt_text, (
        "auditor-prompt.md must receive the paired worker's name as "
        "{{WORKER_NAME}} so it can address that worker directly"
    )
    assert "SendMessage" in prompt_text, (
        "auditor-prompt.md must instruct the auditor to reply with "
        "SendMessage, not plain output, which the worker never sees"
    )

    skill_text = EXECUTE_SKILL.read_text()
    assert "worker-task-<N>" in skill_text, (
        "SKILL.md must document the worker-task-<N> naming convention"
    )
    assert "worker-task-<N>-retry<k>" in skill_text, (
        "SKILL.md must document the retry worker naming convention, sharing "
        "its <k> with the retry auditor"
    )
    assert "{{WORKER_NAME}}" in skill_text, (
        "SKILL.md must document that the worker's name is passed to the "
        "auditor as {{WORKER_NAME}}"
    )


def test_paired_prompts_load_sendmessage_via_toolsearch():
    needle = 'ToolSearch("select:SendMessage")'
    for path in (AUDITOR_PROMPT, AGENT_PROMPT, AUDITOR_AGENT, PAIRING_PROTOCOL):
        text = path.read_text()
        assert needle in text, (
            f"{path.name} must load SendMessage via {needle} before its "
            "first send — SendMessage is a deferred tool that isn't "
            "callable until its schema is fetched"
        )


def test_end_turn_sentence_is_verbatim():
    sentence = "end your turn now; the next message resumes you"
    assert sentence in AUDITOR_PROMPT.read_text(), (
        "auditor-prompt.md must use the exact end-turn sentence so the "
        "auditor never tries to wait within a turn"
    )
    assert sentence in AGENT_PROMPT.read_text(), (
        "agent-prompt.md must use the exact end-turn sentence after the "
        "worker sends its checkpoint"
    )
    assert sentence in _comment_auditor_paired_slice(), (
        "comment-auditor.md's paired-mode section must use the exact "
        "end-turn sentence"
    )
    assert sentence in PAIRING_PROTOCOL.read_text(), (
        "pairing-protocol.md must document the exact end-turn sentence as "
        "the shared turn model for both agents"
    )


def test_worker_checkpoint_closing_line():
    line = "Awaiting audit from {{AUDITOR_NAME}}."
    assert line in AGENT_PROMPT.read_text(), (
        "agent-prompt.md must close the worker's checkpoint turn with this "
        "exact line, which is how the orchestrator tells a waiting worker "
        "from a finished one"
    )
    assert line in PAIRING_PROTOCOL.read_text(), (
        "pairing-protocol.md must document the exact worker checkpoint "
        "closing line"
    )


def test_no_in_turn_waiting_language():
    forbidden = ("do not end your turn", "rather than ending your turn")
    sources = {
        "auditor-prompt.md": AUDITOR_PROMPT.read_text().lower(),
        "agent-prompt.md": AGENT_PROMPT.read_text().lower(),
        "pairing-protocol.md": PAIRING_PROTOCOL.read_text().lower(),
        "comment-auditor.md (paired mode)": _comment_auditor_paired_slice().lower(),
    }
    for name, lowered in sources.items():
        for phrase in forbidden:
            assert phrase not in lowered, (
                f"{name} must not tell an agent to keep a turn open while "
                f"waiting (found {phrase!r}) — the end-turn model requires "
                "ending the turn and resuming on the next message"
            )
    assert "await the reply" not in AGENT_PROMPT.read_text().lower(), (
        "agent-prompt.md must not tell the worker to await the reply in "
        "place — it ends its turn instead"
    )


def test_spawn_ack_dropped():
    assert "**Ack.**" not in AUDITOR_PROMPT.read_text(), (
        "auditor-prompt.md must not ack on spawn — the spawn-time ack was "
        "dropped under the end-turn model"
    )
    assert "acknowledging the task" not in _comment_auditor_paired_slice(), (
        "comment-auditor.md's paired-mode section must not describe "
        "acknowledging the task on spawn"
    )


def test_worker_ends_turn_after_checkpoint_before_writing_report():
    text = AGENT_PROMPT.read_text()
    section_idx = text.find("### 4. Comment Audit Checkpoint")
    next_section_idx = text.find("### 5. Commit Changes")
    assert section_idx != -1 and next_section_idx != -1, (
        "agent-prompt.md missing the comment audit checkpoint or commit step"
    )
    section = text[section_idx:next_section_idx]
    end_turn_idx = section.find("end your turn")
    mktemp_idx = section.find("mktemp")
    assert end_turn_idx != -1, (
        "the checkpoint step must tell the worker to end its turn after "
        "sending"
    )
    assert mktemp_idx != -1, (
        "the checkpoint step must still write the auditor's reply to a "
        "temp file via mktemp"
    )
    assert end_turn_idx < mktemp_idx, (
        "the worker must end its turn (to be resumed by the auditor's "
        "reply) before it writes that reply to a temp file — the mktemp "
        "sub-step happens only after resume"
    )


def test_stand_down_retained():
    assert "stand-down" in AUDITOR_PROMPT.read_text().lower(), (
        "auditor-prompt.md must still document stand-down handling"
    )
    assert "stand-down" in PAIRING_PROTOCOL.read_text().lower(), (
        "pairing-protocol.md must still document stand-down handling"
    )
    assert "stand-down" in _comment_auditor_paired_slice().lower(), (
        "comment-auditor.md's paired-mode section must still document "
        "stand-down handling"
    )


def test_worker_has_done_guard_after_reporting():
    text = AGENT_PROMPT.read_text()
    report_idx = text.find("### 6. Report Results")
    assert report_idx != -1, "agent-prompt.md missing the Report Results section"
    report_section = text[report_idx:]
    assert "you are done" in report_section.lower(), (
        "step 6 must tell the worker that once it has sent its report it is "
        "done — otherwise a late duplicate message (e.g. a resent auditor "
        "report) can revive it into a second prune/commit pass"
    )
    assert "end your turn immediately" in report_section.lower(), (
        "step 6's done guard must tell the worker to end its turn "
        "immediately on any later message, taking no further action"
    )


def test_worker_skips_closing_line_if_report_already_arrived():
    text = AGENT_PROMPT.read_text()
    section_idx = text.find("### 4. Comment Audit Checkpoint")
    next_section_idx = text.find("### 5. Commit Changes")
    assert section_idx != -1 and next_section_idx != -1, (
        "agent-prompt.md missing the comment audit checkpoint or commit step"
    )
    section = text[section_idx:next_section_idx].lower()
    assert "already arrived" in section, (
        "the checkpoint step must tell the worker to check whether the "
        "auditor's report already arrived before it closes its turn — the "
        "end-turn model only narrows, not closes, the window where a report "
        "sent near turn-end fails to resume the worker"
    )
    assert "skip the closing line" in section, (
        "when the report has already arrived, the worker must skip the "
        "'Awaiting audit from {{AUDITOR_NAME}}.' closing line and continue "
        "straight into applying the report, rather than emitting a "
        "closing line no one will ever answer"
    )
