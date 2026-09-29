# Peer

Peer lets Codex, Claude Code, and Antigravity CLI consult or delegate work to one another through their local CLIs. It uses each CLI's own saved session history. Peer itself keeps only two things: each run's result in a private file (see [Saved results](#saved-results)), and a small map from session names you choose to native session IDs (see [Resuming](#resuming)).

## Quick start

Requires Python 3.9+ and the target CLIs installed and authenticated on the same machine.

```sh
printf '%s' 'Review this design for failure modes.' | ./peer ask --to claude --mode consult --cwd "$PWD"
printf '%s' 'Implement the parser and run its tests.' | ./peer ask --to agy --mode work --cwd "$PWD"
```

Each call prints one compact JSON object with `status`, `response`, `session_id`, and `output_file`. To continue a conversation, name it when you start it and resume it by name, or pass the returned ID with the same provider:

```sh
printf '%s' 'Review the parser.' | ./peer ask --to claude --new-session parser --cwd "$PWD"
printf '%s' 'Check the edge cases too.' | ./peer ask --to claude --session-name parser --cwd "$PWD"
printf '%s' 'Check the edge cases too.' | ./peer ask --to claude --session SESSION_ID --cwd "$PWD"
```

Use `./peer doctor` to see installed CLI paths and configured defaults, `./peer models` for models and aliases, and `./peer sessions` for recent sessions in this directory. Use `./peer ask --help` for all flags. `--prompt` is available for short prompts; stdin keeps longer prompts out of shell arguments.

## Modes and permissions

| Target | `consult` | `work` |
| --- | --- | --- |
| Codex | Enforced `read-only` sandbox, no approval prompts | `workspace-write` sandbox, no approval prompts |
| Claude | `plan` with read/search tools | `acceptEdits`; optional `work_allowed_tools` for shell commands |
| Antigravity | `plan` behavior and terminal sandbox | `accept-edits` and terminal sandbox; command permissions come from Antigravity settings |

Peer never passes any provider's permission-bypass flag. A call is also limited by the **calling agent's** sandbox. If the caller cannot execute a CLI, access its saved sessions, or write to the target workspace, the child cannot override that boundary.

Host permissions need their own setup: a Codex host may need to approve the exact Peer command outside its sandbox so the child can write native session files and connect; a Claude host needs permission to run the Peer script. An Antigravity host needs scoped `command(...)` and `unsandboxed(...)` allowances for the installed launcher, because child CLIs use saved sessions and network access. Test the command from each host. A target permission flag alone cannot fix a blocked host command.

For Antigravity headless use, the optional rules in `~/.gemini/antigravity-cli/settings.json` are `command(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` and `unsandboxed(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` in `permissions.allow`. Replace the placeholder with the installed skill's `scripts/peer.py` path. These rules let Antigravity run Peer outside its terminal sandbox without a prompt, including Peer `work` calls that may edit files in other workspaces. Add them only if that host access is intended. The skill invokes the script directly so a prefix rule can match; commands with shell substitution may require exact matching.

Claude and Antigravity distinguish editing files from running shell commands. Antigravity headless calls can report a denied tool on stderr while exiting successfully; Peer returns `status: "blocked"` when it detects that notice. Only structured denials, failed commands, and stderr set `blocked`. Denial-like text in successful command output or in the reply, such as a grep hit on the phrase "permission denied", goes to `permission_warnings` and leaves `status` alone. A Codex command killed by a signal (exit code 128 or higher, such as Chrome exiting with 134 inside the sandbox) also becomes a warning, because it usually means the target could not run a local app. Configure scoped command permissions in Antigravity's own settings for commands delegated work needs.

After a `work` call, `changed_paths` lists what changed during the call and `worktree_changed` summarizes it. In a Git repository, Peer hashes the content of every dirty or untracked path before and after, so a further edit to an already-modified file still counts. Outside Git, it compares the size and modification time of every file (skipping `.git`, `node_modules`, virtualenvs, and caches), up to 20,000 files. Past that limit, `worktree_changed` is `null` and `worktree_note` says why; `null` means unknown, not unchanged. A model's claim that it edited a file is not proof of a change.

Antigravity's file tool can otherwise choose its own scratch directory for a relative path. Peer includes the absolute workspace path in every work prompt and asks for absolute file paths under it. Check the returned `changed_paths` and the diff; a successful model response alone does not establish that it wrote to the requested checkout.

For strict consultation isolation, verify your target's mode in a throwaway directory. Codex enforces a read-only sandbox; Claude and Antigravity plan modes are not equivalent OS-level file sandboxes. Keep one writer per checkout. Use separate worktrees for parallel editing tasks.

## Model and effort defaults

Copy `config.example.json` to `~/.config/peer/config.json`, or pass `--config PATH`. This file stores preferences, not sessions. Unknown keys are rejected so that typos do not pass silently. The precedence is command flag, Peer config, then the provider's own default. Model names pass through unchanged unless they match an alias in `providers.PROVIDER.aliases`, for example `"aliases": {"sol": "gpt-6-sol"}`. Aliases are case-insensitive, come only from your config, and the result reports both the resolved `model` and the `model_alias`. `./peer models` shows aliases and lists Antigravity's models live; Codex and Claude Code have no CLI command that lists models. Effort values are checked against each CLI's supported vocabulary, then the CLI validates the model/effort pairing. When a CLI adds levels before Peer knows them, set `providers.PROVIDER.efforts` to the full list.

```sh
printf '%s' 'Find the bug and fix it.' | ./peer ask --to codex --mode work \
  --model gpt-5.5 --effort high --cwd "$PWD"
```

## Timeouts and interruption

`--timeout` (default 600 seconds) applies to each provider call. When it expires, or when Peer receives SIGINT, SIGTERM, or SIGHUP, Peer sends SIGTERM to the child's process group, waits 5 seconds so the agent can save its session, then sends SIGKILL to anything left. The result is `timeout` or `interrupted`, with any session ID already returned. SIGKILL to Peer itself cannot be caught and can leave the child running. Give the host's own command timeout more time than Peer's, or run Peer in the background.

`--attach FILE` (repeatable) gives the target a file. Codex receives images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) natively through `--image`. Other files, and all files for Claude and Antigravity, are listed by absolute path in the prompt for the target to open; directories outside `--cwd` are added with `--add-dir` for Claude and Antigravity. The result's `attachments` says which were passed natively. PDFs are never attached natively.

## Saved results

`ask` and `debate` save the full result, plus the prompt, to `$XDG_STATE_HOME/peer/runs/` (default `~/.local/state/peer/runs/`) as `TIMESTAMP-COMMAND-PROVIDERS-PID.json`, and return the path as `output_file`. Files are mode `0600` in a `0700` directory. Set `output_dir` in config to change the location, pass `--output FILE` for one run (useful for background runs, since you know the path in advance), or `--no-save` to skip writing. Peer never deletes old files; prune the directory yourself.

The file exists from the start of the run. Until the call finishes it holds `status: "running"` with `pid`, `started_at`, and `updated_at`, and about every 15 seconds Peer adds `elapsed_seconds`, `output_events`, `last_activity` (the child's latest streamed event, such as a Codex command), and any `session_id` seen so far. A debate keeps these under `current` and adds each finished turn. A `running` file whose `updated_at` stopped changing, or whose `pid` no longer exists, is stale: Peer was killed.

Stdout carries only the JSON result; Peer writes nothing to stderr, so background output parses as JSON. A successful call prints a compact result: the response and the fields needed to resume or check work, with `permission_warnings` as a count and `changed_paths` capped at 30 entries. Diagnostics, notices, exit codes, and full lists stay in the saved file. Failures print the full record. `--full` prints everything, as does `--no-save`, since there is no file to hold the rest.

## Resuming

On every call, Peer passes the selected mode, model, and effort again, including when resuming a session. It always resumes by explicit ID or by a name you created. Do not use Codex `--ephemeral` or Claude `--no-session-persistence` with Peer.

`--new-session NAME` starts a conversation and records NAME for that provider and directory in `$XDG_STATE_HOME/peer/session-names.json`. If NAME already pointed at another session, it is repointed and the result reports `replaced_session_id`. `--session-name NAME` resumes the recorded session, and fails if the name does not exist, so a reused name never silently continues an unrelated conversation. Names are 1–64 letters, digits, `.`, `_`, or `-`.

`./peer sessions` lists up to 20 recent sessions for the current directory from the saved run files: provider, ID, names, number of runs, last use, and the first line of the first prompt. `--all` includes every directory.

## Debate

```sh
printf '%s' 'Should this cache be write-through or write-back?' | \
  ./peer debate --a codex --b claude --rounds 2 --cwd "$PWD"
```

`debate` alternates read-only consultations, returns the exchange plus both session IDs, and stops after the requested rounds or at the first failed turn. Stdout shows each successful turn as `{provider, response}`; the saved file keeps every field. Pass `--session-a` and `--session-b` to continue a previous pair, or use `--new-session NAME` and `--session-name NAME` to name both sides at once. `--model-a`, `--model-b`, `--effort-a`, and `--effort-b` set each side's model and effort; otherwise config applies. Attachments go with each side's first turn. Peer caps recursive calls from an agent it launched, so the outer caller coordinates the discussion.

One round means A speaks once, then B replies to A. Two rounds make four provider calls: A → B → A → B. Peer passes up to 12,000 characters of each answer to the next provider and marks the cut when it truncates. Each provider call has its own timeout; the outer caller synthesizes the returned transcript.

## Install the shared skill

The canonical skill is `skills/peer/`. From the repository root, install it for local use across projects:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills ~/.gemini/antigravity-cli/skills
ln -s "$PWD/skills/peer" ~/.agents/skills/peer
ln -s "$PWD/skills/peer" ~/.claude/skills/peer
ln -s "$PWD/skills/peer" ~/.gemini/antigravity-cli/skills/peer
```

These are the personal skill locations:

- Codex: `~/.agents/skills/peer`
- Claude Code: `~/.claude/skills/peer`
- Antigravity CLI: `~/.gemini/antigravity-cli/skills/peer`

Codex and Claude document support for symlinked skill folders. Antigravity does not document it, but it loaded a symlinked `peer` skill in a 2026-09-24 test: `agy -p '/peer ...'` answered from this SKILL.md. With symlinks, all three hosts use whichever branch this checkout has checked out. The skill points at its bundled `scripts/peer.py`, so the CLI does not need to be on `PATH`. Check that each host loads the skill after installation; for Antigravity, `agy -p '/peer Reply with the first section title.'` should answer from SKILL.md.

## Verification

`python3 -m unittest discover -s tests -v` runs adapter tests with fake CLIs. A live setup check should then use a disposable Git repository from each host to each target: make a fresh call, resume its ID, try a consultation that must not edit, delegate an edit inside the workspace, run one permitted command, and verify a denied command is reported as blocked. The host-to-target matrix is the real test of shell, credential, and sandbox access.

Provider references: [Codex sandbox and approvals](https://learn.chatgpt.com/docs/agent-approvals-security), [Claude CLI and permissions](https://code.claude.com/docs/en/cli-reference), [Antigravity headless permissions](https://www.agy.dev/docs/cli/headless/).
