# Tests

Pytest suite for the babysit skill's Python DB helper.

## Running

```sh
python3 -m pytest tests/babysit/ -q
```

No pip install required — `pytest` is a dev-time dep only; the skill
itself runs on Python stdlib.

## Conftest fixtures

`tests/babysit/conftest.py` provides two fixtures used throughout the
suite:

- **`conn`** — an in-memory `sqlite3.Connection` with `schema.sql`
  already applied and `row_factory = sqlite3.Row` so tests can use
  dict-like column access. Use this for unit tests that only need a
  single connection.
- **`coordinator_db`** — a file-backed DB path under pytest's
  `tmp_path` with schema applied. Returns the path (not a connection),
  so tests can open multiple connections against it to exercise WAL
  behavior or multi-process coordination semantics.

Both fixtures depend only on stdlib `sqlite3` and the on-disk
`schema.sql`; they do not import the `db.py` helper under test.

## Why tests ship with the plugin

These tests are included in the published plugin bundle (~few KB). This
is **intentionally accepted bloat**, not an oversight.

Claude Code v2.1.148's plugin loader (`copyPluginToVersionedCache` →
`copyDir`) does an unconditional recursive copy of the marketplace
`source` directory. There is no `files` / `include` / `exclude` field
on `plugin.json`, no `.claudeignore` mechanism, and no callsite filter
— only `.git/` is stripped post-copy. MCPB extensions have
`.mcpbignore`, but plugins use a separate code path. See T1 findings in
`~/plans/babysit-db-helper-python.md` for the full investigation.

## Forward-looking note

If Claude Code adds a `files` allowlist or `.claudeignore`-style
mechanism in a future version, switch to it and drop tests from the
shipped bundle.
