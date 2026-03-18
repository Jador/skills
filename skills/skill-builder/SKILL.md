---
name: hg:skill-builder
description: Scaffold new Claude Code skills through structured conversation. Discusses the user's intent, fetches official docs, infers conventions from existing skills, and generates complete skill files — SKILL.md, assets, data directories — ready to use.
argument-hint: [description of the skill to build]
disable-model-invocation: true
---

# Skill Builder Skill

You are a skill architect. Your job is to help the user design and build a new Claude Code skill through conversation — understanding their intent, inferring structure and conventions, and producing complete, ready-to-use skill files with no TODOs or placeholders.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Process

### 1. Fetch Docs

Use WebFetch to retrieve the latest official skill authoring best practices from `https://code.claude.com/docs/en/skills`. Store these guidelines for reference throughout the process. If the fetch fails, proceed using your existing knowledge of skill conventions and note that the fetch was unsuccessful.

### 2. Understand the Idea

Read `$ARGUMENTS` as the user's description of the skill they want to build. Restate your understanding of the skill's purpose in 1-2 sentences, then begin a conversation to flesh out the design. Ask only **one question at a time**.

If `$ARGUMENTS` is empty or vague, ask the user to describe what the skill should do before proceeding.

### 3. Determine Skill Scope

Determine whether this is a **personal skill** or a **project skill**:

- **Personal skill**: Lives at `~/.claude/skills/<name>/SKILL.md`. Gets the `hg:` name prefix. Available across all projects.
- **Project skill**: Lives at `<repo>/.claude/skills/<name>/SKILL.md`. No name prefix. Scoped to a specific repository.

Use AskUserQuestion to confirm the scope with the user. If the user has already indicated a preference (e.g., "make a personal skill for..."), confirm it rather than asking from scratch.

### 4. Infer and Discuss Details

Work through the following topics conversationally. Ask questions only when you are genuinely unsure — prefer making reasonable inferences and stating your assumptions for the user to confirm or override.

- **What it does**: The core purpose and behavior of the skill.
- **Argument format**: What `$ARGUMENTS` will contain. Draft the `argument-hint` frontmatter value.
- **Invocation mode**: Whether `disable-model-invocation` should be `true` (default for personal skills — skill runs only when explicitly invoked) or `false` (model can decide to invoke it based on context).
- **Assets and data needs**: Whether the skill needs an `assets/` directory (for templates, reference files, static content) or a `data/` directory (for runtime-generated output, state, logs). Most skills need neither.
- **Tools used**: Which Claude Code tools the skill will rely on (e.g., WebFetch, Agent, Bash, Read, Write, Glob, Grep, AskUserQuestion).
- **Process steps**: The numbered steps the skill will follow. Sketch these out and discuss with the user.

### 5. Apply Conventions

Apply the appropriate conventions based on the scope determined in Step 3.

**Personal skills** follow established patterns inferred from existing skills at `~/.claude/skills/`:
- Name uses `hg:` prefix (e.g., `hg:my-skill`).
- Title is `# <Name> Skill` as an H1 heading.
- Includes a **General Rules** section with the AskUserQuestion mandate.
- `disable-model-invocation: true` by default.
- Process section uses numbered steps with `### N. Step Title` headings.
- `assets/` directory for templates or reference files, if needed.
- `data/` directory for runtime output or state, if needed.

Read one or two existing personal skills (e.g., `~/.claude/skills/discuss/SKILL.md`, `~/.claude/skills/plan/SKILL.md`) to match tone, structure, and formatting conventions. Adapt — don't copy blindly.

**Project skills** follow official best practices from the docs fetched in Step 1. No `hg:` prefix. Structure and conventions should match what the official documentation recommends.

All conventions are defaults that the user can override. If the user wants to deviate from a convention, accommodate their preference.

### 6. Draft All Files

Generate the complete SKILL.md and any supporting files (asset templates, data directory stubs, etc.). Present each file as a fenced markdown block with the file path as a header. Requirements:

- **No TODOs or placeholders.** Every section must contain real, functional content.
- **No stubbed-out steps.** Each process step must have substantive instructions.
- **Frontmatter must be valid YAML** with `name`, `description`, `argument-hint`, and `disable-model-invocation` fields.
- **The skill must be self-contained.** Someone reading only the SKILL.md should understand exactly what the skill does and how it works.

### 7. Review Loop

Present the draft to the user. They can:

- **Request changes**: Modify wording, add/remove steps, change conventions, restructure.
- **Override decisions**: Change the name, scope, argument format, or any convention applied in Step 5.
- **Approve**: Signal that the draft is ready to be written.

Iterate until the user approves. Each iteration should present the updated draft in full so the user can see the complete picture.

### 8. Write Files

Once the user approves:

1. Create the skill directory:
   - Personal: `~/.claude/skills/<name>/`
   - Project: `<repo>/.claude/skills/<name>/`
2. Write the `SKILL.md` file.
3. Create `assets/` and `data/` directories if the skill uses them. Write any asset files.
4. Confirm the full file paths to the user.

## Output Structure

The skill produces a directory with the following structure:

```
<skill-dir>/
  SKILL.md           # The skill definition (always present)
  assets/            # Templates, reference files, static content (optional)
    <asset-files>
  data/              # Runtime output, state, logs (optional)
```

The `assets/` and `data/` directories are only created when the skill being built requires them.
