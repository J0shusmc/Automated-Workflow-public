# Codex Script Builder And Interactive Terminal

## Goal

Add a second top dock agent named Codex. Claudia remains the workflow prompt/work-order agent. Codex becomes the local project/script builder that can create Python scripts and plug them into the workflow dashboard as task nodes.

## Codex Agent Flow

1. User opens the Codex menu.
2. User selects Create Script.
3. A draft Codex/script-builder node appears.
4. User describes the needed script, such as "Create a Python script to monitor my email for new sales."
5. Backend creates a new project folder under `generated-projects/<project-name>/`.
6. Backend creates `venv/` with `python3 -m venv venv`.
7. Codex streams questions, plans, file creation, and setup output into the dashboard terminal.
8. User answers Codex questions through the same terminal.
9. Codex writes the script and support files inside that project folder.
10. Dependencies are installed into that project venv.
11. The dashboard creates a normal Python task node pointed at the generated script.

## Project Layout

Each generated script is treated as a small standalone project:

```text
generated-projects/
  email-sales-monitor/
    README.md
    main.py
    requirements.txt
    .env.example
    venv/
    codex-transcript.log
```

The workflow should store the task node configuration with:

- `scriptPath`
- `workingDirectory`
- `pythonExecutable`
- `usesDetectedVenv`
- `kind`

## Terminal Modes

The lower-right dashboard terminal should support two modes.

### Run Log Mode

- Appends workflow run logs.
- Does not clear when a new run starts.
- Shows script output, errors, image/video generation progress, and workflow events.

### Interactive Command Mode

- Accepts keyboard input.
- Sends input to the active backend process stdin.
- Supports Codex questions and setup confirmations.
- Supports command sessions such as starting/stopping the LLM when sudo asks for a password.

## Terminal Lifecycle

- Terminal stream starts when the dashboard starts.
- Terminal output is append-only by default.
- Starting a workflow appends a run header instead of clearing output.
- Starting Codex appends a Codex session header.
- Terminal clears/closes only when:
  - the user closes the terminal,
  - a new workflow/project is started,
  - or an explicit clear action is added later.

## Backend Session API

Suggested API shape:

```text
POST /api/terminal/start
{
  "kind": "codex-script",
  "command": ["codex", "..."],
  "cwd": ".../generated-projects/email-sales-monitor"
}

POST /api/terminal/<sessionId>/input
{
  "text": "answer\n"
}

GET /api/terminal/<sessionId>/events
```

The backend should hold process sessions, stream stdout/stderr through server-sent events, and write user input to stdin.

## Safety Rules

- Codex-generated files must stay inside `generated-projects/<project-name>/`.
- Project folder names must be sanitized and unique.
- Do not save password input to logs.
- Mask terminal input when a password prompt is active.
- Do not execute generated scripts automatically until the user intentionally runs the workflow node.
- Store Codex transcript separately from secrets.
- Use per-project venvs only; do not install generated-script dependencies into the dashboard venv.

## Workstation Behavior With Breaks

When a workflow break exists:

- Left controls operate only before the break.
- Left play runs to the break and saves breakpoint output.
- Left reset clears pre-break state and clears the break cache.
- Right controls operate only after the break.
- Right play refreshes saved breakpoint output and runs only post-break nodes.
- Right reset clears only post-break run/output state.

The breakpoint output should be disk-backed in the saved workflow so generated images and files can feed later workflow sections without rerunning earlier nodes.
