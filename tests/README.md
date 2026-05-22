# Tests

Pytest suite for the babysit skill's Python DB helper.

## Running

```sh
python3 -m pytest tests/babysit/ -q
```

No pip install required — `pytest` is a dev-time dep only; the skill
itself runs on Python stdlib.

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
