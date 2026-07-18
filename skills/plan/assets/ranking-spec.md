# Shared Ranking Spec — current-repo-first ordering

This is the single source of truth for how the doc-listing skills (plan, execute, backlog, notepad) order shared-directory docs. Any skill that presents a list of shared-directory docs references this file rather than restating the rules.

When a skill presents a list of shared-directory docs (ideas, plans, notes) for the user to pick from or browse, order that list so docs belonging to the **current repo** come first, then by recency. Nothing is hidden — every doc stays reachable.

**Current-repo identity.** The current repo is `basename "$(git rev-parse --show-toplevel 2>/dev/null)"`. If that command produces nothing (not inside a git repo), fall back to the basename of the current working directory. This is the same convention notepad uses to set a doc's `**Project:**`.

**Matching a doc to its project.** Compare the current-repo identity (case-sensitive, exact string match) against the doc's stored project:
- **Ideas** — the `**Project:**` value in the idea's Context section.
- **Notes** — the `**Project:**` field on the individual note entry.
- **Plans** — the `project:` frontmatter field. For **legacy plans that lack `project:`**, resolve it by tracing the `idea:` frontmatter path to the source idea file and reading that idea's `**Project:**`. If the project still cannot be determined (missing field, unresolvable `idea:` path, or idea with no project), the doc falls into the **"other"** bucket.

A doc whose project cannot be determined is treated as "other" (not current-repo).

**Recency.** Order by each doc's **embedded date**, descending — NOT filesystem mtime (edits would reorder; the shared notes file has no per-note mtime). The embedded date is:
- **Ideas** — the `YYYY-MM-DD-` filename prefix.
- **Plans** — the `created:` frontmatter field.
- **Notes** — the note's `**Added:**` field.

Ties (equal dates) are broken by filename, ascending. Notes have no filename (they are entries in the shared `~/notes.md`); break equal-date note ties by note **ID, ascending**.

**Sort order (apply in full).**
1. Current-repo docs first, then "other" docs.
2. Within each bucket, most-recent embedded date first (descending).
3. Within equal dates, filename ascending (notes: ID ascending).

**Presentation.** A single flat list in the sorted order above — no section headers. Mark each current-repo doc with a leading **★** glyph so its origin is legible; other-repo docs get no marker. Each row still shows enough (title + date) that its origin is clear.

**Overflow (pick UI only).** AskUserQuestion caps at 4 options. When the list is used as a *pick UI* and has more than 4 docs, show the **top 3 ranked docs** as options plus a 4th **"Show more…"** option that re-prompts (AskUserQuestion again) with the next page of 3 + "Show more…", continuing until the docs are exhausted (the final page needs no "Show more…" if it fits). This surfaces current-repo/recent docs first while keeping every doc reachable across pages. A plain **full listing** (e.g. a table, not a pick UI) is not subject to this cap — render all rows in ranked order.
