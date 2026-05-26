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
