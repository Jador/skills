"""Regression tests for worker prompt JSON-parsing pattern (finding #10).

The prior worker prompts told the worker to wrap the event JSON in a
single-quoted shell variable:

    EVENT_JSON='<paste the full JSON object from the user message here>'

Review comment bodies almost always contain apostrophes (English
contractions: don't, it's, won't). The first apostrophe in the pasted
JSON terminates the outer shell quote, the rest of the JSON is parsed
as shell command tokens, and `$EVENT_JSON` ends up empty. Every
subsequent `jq -r '.foo' <<<"$EVENT_JSON"` returns "null" and the
worker silently malfunctions.

The fix replaces the single-quote pattern with a heredoc that uses a
single-quoted DELIMITER (which suppresses shell expansion inside the
body) and routes downstream jq calls through a temp file:

    EVENT_FILE=$(mktemp)
    cat > "$EVENT_FILE" <<'JSON_EOF'
    <paste full JSON>
    JSON_EOF

Contract under test:

1. The heredoc pattern preserves apostrophe-laden bodies through to jq.
2. Neither worker prompt still suggests the broken
   ``EVENT_JSON='...'`` form.
3. Both prompts route jq through ``"$EVENT_FILE"``, not
   ``<<<"$EVENT_JSON"``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_DIR = REPO_ROOT / "skills" / "babysit" / "assets"
COMMENT_PROMPT = PROMPT_DIR / "comment-check-prompt.md"
BUILD_PROMPT = PROMPT_DIR / "build-check-prompt.md"


def test_heredoc_pattern_round_trips_apostrophe_body(tmp_path: Path):
    """Simulate what a worker would do following the new instructions."""
    # An apostrophe-laden review comment body — the failure case from
    # finding #10.
    event = {
        "pr": 42,
        "repo": "org-a/foo",
        "comments": [
            {"body": "don't do this; it's wrong"},
        ],
    }
    payload = json.dumps(event)

    script = f"""
set -euo pipefail
EVENT_FILE=$(mktemp)
cat > "$EVENT_FILE" <<'JSON_EOF'
{payload}
JSON_EOF
echo "pr=$(jq -r .pr "$EVENT_FILE")"
echo "body=$(jq -r '.comments[0].body' "$EVENT_FILE")"
"""

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "pr=42" in result.stdout
    assert "body=don't do this; it's wrong" in result.stdout


def test_worker_prompts_use_heredoc_not_single_quote_eventjson():
    for prompt in (COMMENT_PROMPT, BUILD_PROMPT):
        text = prompt.read_text()
        # The new pattern must be present.
        assert "EVENT_FILE=$(mktemp)" in text, f"{prompt.name} missing heredoc setup"
        assert "<<'JSON_EOF'" in text, f"{prompt.name} missing single-quoted heredoc delimiter"
        # The broken literal pattern must not be present as an example to
        # copy. The prompt may still *describe* the antipattern when
        # explaining why the heredoc is preferred — that's the inline
        # form ``EVENT_JSON='…'`` with an ellipsis. The fail-fast string
        # is the literal placeholder a worker would actually paste over.
        assert "EVENT_JSON='<paste" not in text, (
            f"{prompt.name} still includes the literal single-quoted "
            "EVENT_JSON='<paste …>' example"
        )


def _fenced_code_blocks(text: str) -> list[str]:
    """Return the bodies of all triple-fenced code blocks in `text`.

    Fences may be indented (e.g. inside a markdown numbered-list item).
    """
    blocks: list[str] = []
    in_block = False
    buf: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if in_block:
                blocks.append("\n".join(buf))
                buf = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            buf.append(line)
    return blocks


def test_worker_prompts_use_prose_stop_not_bash_exit():
    # `exit 0` in a fenced bash block only exits the shell subprocess.
    # The LLM agent keeps reading the prompt and runs the rest of the
    # steps (commit, push, post replies) anyway. The prompt must drive
    # the abort through prose, not shell exit.
    for prompt in (COMMENT_PROMPT, BUILD_PROMPT):
        text = prompt.read_text()
        for block in _fenced_code_blocks(text):
            # `exit 0` appearing as an actual shell statement (line by
            # itself, possibly indented) is the antipattern. Inline
            # backtick-quoted mentions in prose are fine.
            for line in block.splitlines():
                stripped = line.strip()
                assert stripped != "exit 0", (
                    f"{prompt.name} still contains a bare `exit 0` shell "
                    "statement in a fenced code block; the LLM agent will "
                    "continue past it. Use a prose STOP instruction instead."
                )
        # The new pattern uses an emphasized "STOP" keyword in the
        # prose so the LLM agent treats it as a hard stop.
        assert "STOP" in text, f"{prompt.name} missing STOP keyword"


def test_comment_prompt_extracts_identifiers_before_gh_calls():
    # Step 1.6 in the prior version invoked `gh pr view "$PR" --repo
    # "$REPO" ...` before Step 3 extracted those variables. Hoist the
    # extraction to a dedicated step that precedes every gh call.
    text = COMMENT_PROMPT.read_text()
    step_zero_idx = text.find("## Step 0:")
    assert step_zero_idx != -1, "missing Step 0 identifier extraction"

    # Find the first `gh ` invocation that lives inside a fenced code
    # block (i.e. a real shell command the worker will run), not a
    # prose mention.
    first_gh_invocation_idx = -1
    in_block = False
    block_start = 0
    for line_idx, line in enumerate(text.splitlines(keepends=True)):
        pass  # placeholder; we iterate by position instead below.

    pos = 0
    in_block = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_block = not in_block
        elif in_block and ("gh pr " in line or "gh api " in line):
            first_gh_invocation_idx = pos
            break
        pos += len(line)
    assert first_gh_invocation_idx != -1, "no gh invocation found in fenced block"
    assert first_gh_invocation_idx > step_zero_idx, (
        "first `gh` invocation precedes the Step 0 identifier extraction "
        "— PR/REPO would be unset when gh runs"
    )

    # And the extraction must actually assign PR and REPO.
    step_zero_section = text[step_zero_idx:text.find("## Step 1", step_zero_idx)]
    assert 'PR=$(jq -r ' in step_zero_section
    assert 'REPO=$(jq -r ' in step_zero_section


def test_worker_prompts_route_jq_through_event_file():
    for prompt in (COMMENT_PROMPT, BUILD_PROMPT):
        text = prompt.read_text()
        # Downstream jq commands must use the temp file, not the fragile
        # herestring against the broken shell variable.
        assert '<<<"$EVENT_JSON"' not in text, (
            f"{prompt.name} still feeds jq via <<<\"$EVENT_JSON\""
        )
        # And the file form must show up at least once.
        assert '"$EVENT_FILE"' in text, (
            f"{prompt.name} has no jq invocation that reads $EVENT_FILE"
        )
