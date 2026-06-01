"""Regression tests for poll.sh self-marker filter (finding #3).

GitHub's "Quote reply" feature copies the parent comment into the new
comment's body, each line prefixed with `> `. A substring-based filter
that looked for `<!-- babysit-agent -->` anywhere in the body would
match the quoted marker and silently drop the reviewer's reply.

Contract under test:

1. A self-authored comment whose body STARTS with the marker is filtered
   out (the original purpose of the marker check).
2. A reviewer comment whose body QUOTES the marker (`> <!-- babysit-agent
   -->\n\ntheir reply`) is NOT filtered.
3. The literal `startswith("<!-- babysit-agent -->")` snippet is present
   in poll.sh — guards against accidental regression to a `contains(…)`
   substring match.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
POLL_SH = REPO_ROOT / "skills" / "babysit" / "assets" / "poll.sh"

# The exact filter shape used in poll.sh, isolated for unit testing.
# If poll.sh changes the marker-match semantics, the assertion at the
# end of this file fails so the test must be updated alongside the code.
MARKER_FILTER = (
    '[.[] | select( (.body // "") '
    '| startswith("<!-- babysit-agent -->") | not )]'
)


def _run_jq(body_lines: list[str]) -> list[dict]:
    """Pipe a list of comment bodies through MARKER_FILTER. Returns the
    bodies that survive (i.e. would be processed as new events).
    """
    payload = [{"body": b} for b in body_lines]
    p = subprocess.run(
        ["jq", "-c", MARKER_FILTER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0, p.stderr
    return [c["body"] for c in json.loads(p.stdout)]


def test_self_authored_marker_at_body_start_is_filtered():
    survivors = _run_jq([
        "<!-- babysit-agent -->\n> [!NOTE]\n> bot reply body",
    ])
    assert survivors == []


def test_quote_reply_with_quoted_marker_is_not_filtered():
    survivors = _run_jq([
        "> <!-- babysit-agent -->\n>\n> previous bot text\n\n"
        "actual human reply with feedback",
    ])
    assert len(survivors) == 1
    assert "actual human reply" in survivors[0]


def test_marker_anywhere_below_first_line_is_not_filtered():
    # Even without quote-reply, a body that happens to mention the
    # literal marker string further down (e.g. discussing the marker
    # in a meta-comment) must pass through.
    survivors = _run_jq([
        "hey can we change the marker?\n"
        "the current one is `<!-- babysit-agent -->`",
    ])
    assert len(survivors) == 1


def test_unrelated_comments_pass_through():
    survivors = _run_jq([
        "regular review comment, no marker anywhere",
        "another comment",
    ])
    assert len(survivors) == 2


def test_poll_sh_uses_startswith_not_contains():
    src = POLL_SH.read_text()
    assert 'startswith("<!-- babysit-agent -->")' in src
    # Belt-and-braces: the old substring form must not have crept back
    # in for this same marker.
    assert 'contains("<!-- babysit-agent -->")' not in src
