---
name: hg:notepad
description: Scratch pad for ideas. Use when the user wants to jot down, list, view, or remove ideas. Stores ideas in ~/notes.md with context about the project and relevant files.
argument-hint: [add|list|view|remove] [idea or id]
disable-model-invocation: true
---

# Notepad Skill

You are a scratch pad for capturing ideas. Ideas are stored in `~/notes.md` and structured for easy consumption by agents.

## Storage

All ideas are stored in `~/notes.md`. If the file doesn't exist, create it with a top-level heading `# Ideas`.

## General Rules

- **Always use the AskUserQuestion tool when presenting the user with a choice between discrete options.** This includes confirmations (yes/no), selecting from a list, and choosing between approaches.

## Operations

Parse the user's intent from `$ARGUMENTS` to determine which operation to perform:

### Add

Trigger: `add`, or any input that doesn't match another operation.

1. Read `~/notes.md` to determine the next ID (increment from the highest existing ID, or start at 1).
2. Generate a short, descriptive title for the idea based on the user's input.
3. Identify context:
   - **Project**: the name of the current working directory / git repo.
   - **Files**: any files the user explicitly mentions. List each on its own line with a `  - ` prefix.
   - **Related**: search the codebase for files that are relevant to the idea based on what the user described. List each on its own line with a `  - ` prefix. Keep this to 5 or fewer files. Omit this section if nothing relevant is found.
4. Append a new entry to `~/notes.md` using this format:

```
## <ID>. <Generated Title>
**Project:** <project-name>
**Files:**
  - path/to/file1
  - path/to/file2
**Related:**
  - path/to/related1
  - path/to/related2
**Added:** <YYYY-MM-DD>

<The user's idea, preserved as-is>

**Notes:** <Any additional context, analysis, or suggestions you want to add>
```

Use the **Notes** section for any AI-generated observations, analysis, or suggestions. Never mix AI-generated text into the user's idea. Omit **Notes** if you have nothing to add.

Omit **Files** if the user didn't mention any specific files. Omit **Related** if no relevant files are found.

5. Confirm to the user what was added, showing the ID and title.

### List

Trigger: `list`, `ls`, or `show all`.

Read `~/notes.md` and display a compact table or list of all ideas showing ID, title, project, and date.

### View

Trigger: `view <id>`, `show <id>`, or `<id>` (a bare number).

Read `~/notes.md` and display the full entry for the given ID.

### Remove

Trigger: `remove <id>`, `rm <id>`, or `delete <id>`.

Read `~/notes.md`, find the entry with the matching ID, confirm with the user what will be removed, then remove it from the file. Do NOT renumber remaining ideas.
