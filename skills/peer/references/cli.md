# Peer CLI reference

Read this when you need a flag or field that SKILL.md does not cover. Every command also accepts `-h`.

Providers are `codex`, `claude`, and `agy`. Every command prints text unless `--json` is given; `doctor` and `models` always print JSON. Exit codes: `0` ok, `2` anything else, `3` from `wait` when the run is still going.

## ask

Start a conversation, or send one unnamed turn.

| Flag | Default and behavior |
| --- | --- |
| `--to PROVIDER` | Required unless `--session-name` or `--fork-from` is given (which infer the saved provider). |
| `--mode consult\|work\|auto` | `consult` (advice; editing forbidden) for a new session. `work` permits edits in `--cwd`, subject to host and target permissions. `auto` requests each CLI's auto-reviewed permission mode (`auto` for Claude/Antigravity, `auto_review` for Codex; providers may fall back if unsupported by the account) so delegated work can also run safe commands. Claude headless calls also pass `--permission-prompts none` (requires Claude Code `2.1.259+`). A continued named session keeps its last mode. |
| `--new-session NAME` | Start fresh (or save a forked session) under NAME for this provider and workspace, with the call's mode, model, effort, word limit, and worktree. NAME is 1–64 letters, digits, `.`, `_`, or `-`, starting with a letter or digit. Repointing an existing name prints `replaced:`. |
| `--session-name NAME` | Continue NAME (same as `reply NAME --to PROVIDER`). |
| `--session ID` | Continue a native session ID. No settings are remembered for raw IDs. |
| `--fork-from NAME` | Fork the session saved as NAME into a new native session (Codex and Claude only), inheriting its saved workspace and settings unless overridden. Can be paired with `--new-session NEW_NAME` and `--worktree [WT]`. |
| `--fork-session ID` | Fork a native session ID into a new session (Codex and Claude only). |
| `--worktree [NAME]` | Create or reuse an isolated Git worktree under `$XDG_STATE_HOME/peer/worktrees/` on branch `peer/<NAME>` (defaults to `--new-session` or a timestamp; starts from committed `HEAD` when creating a new branch, or reattaches to an existing `peer/<NAME>` branch) and run the call there. Saved on named sessions so `reply NAME` continues in the worktree. |
| `--model NAME` | Flag, then the session's saved model, then provider config, then the provider default. Config aliases resolve case-insensitively. |
| `--effort LEVEL` | Same precedence as `--model`; checked against the provider's accepted levels. |

`--session`, `--session-name`, `--fork-from`, and `--fork-session` are mutually exclusive; `--new-session` can be used alone or with `--fork-from` / `--fork-session`.

## reply

`reply NAME [--to PROVIDER] [--mode M] [--model M] [--effort E] [RUN OPTIONS]`

Continue the session saved as NAME. It runs in the session's own workspace with its saved provider, mode, model, effort, and word limit. A flag overrides one setting, and the override is saved for later replies. NAME is looked up from `--cwd` (default: the shell's directory): the same workspace first, then the nearest parent workspace, then a single child workspace. Sibling projects never match. If NAME has sessions with more than one provider (for example after a named debate), pass `--to`.

## debate

`debate --a PROVIDER --b PROVIDER [--rounds N] [RUN OPTIONS]`

| Flag | Default and behavior |
| --- | --- |
| `--a`, `--b` | Required and different. A speaks first. All turns use consult mode. |
| `--rounds N` | `2`, allowed 1–10. One round is A → B. |
| `--session-a ID`, `--session-b ID` | Continue known native IDs. |
| `--new-session NAME` / `--session-name NAME` | Start and name both sides, or continue both. Not combined with `--session-a/-b`. |
| `--model-a`, `--model-b`, `--effort-a`, `--effort-b` | Per-side model and effort; otherwise config applies. |

Both sides are checked before the first call. A failed turn stops the debate and keeps completed turns and session IDs. The other side receives each whole reply, details included, up to 12,000 characters with a truncation marker; you see each turn's answer and a details count. A side's first turn includes the brief; later turns send only the newest exchange.

## Run options (ask, reply, debate)

| Flag | Default and behavior |
| --- | --- |
| `--cwd DIR` | The shell's directory. For `reply`, only used to look up the name. |
| `--prompt TEXT` | Otherwise stdin. Blank prompts fail. |
| `--words N` | Config `reply_words`, default 400. Peer appends a reply format to the prompt: an answer of at most N words, optional `QUESTIONS:` for the caller (not in debates), and optional details after a `---details---` line. `0` turns the format off and returns the whole reply as the answer. |
| `--timeout SECONDS` | Config `timeout_seconds`, otherwise 600, per provider call. |
| `--attach FILE` | Repeatable. Codex receives images natively; other files, and all Claude/Antigravity attachments, are passed as paths to open. Outside directories are added for Claude/Antigravity. |
| `--output FILE` | Save the record there (and link it from the run directory so names, `show`, `wait`, and `sessions` still find it). Otherwise saved under config `output_dir` or `$XDG_STATE_HOME/peer/runs/` (default `~/.local/state/peer/runs/`). |
| `--no-save` | Do not save; print everything. Named sessions still record their ID and settings. |
| `--json` | Print compact JSON instead of text. |
| `--full` | Print everything: details, every warning, full diagnostics, and the file path. |
| `--config FILE` | Otherwise `PEER_CONFIG`, then `$XDG_CONFIG_HOME/peer/config.json` (default `~/.config/peer/config.json`). |

## show, wait, sessions

| Command | Behavior |
| --- | --- |
| `show [NAME\|RUN_FILE] [--cwd DIR]` | Print the latest run for NAME, for a run file, or (no target) for this workspace tree. |
| `show NAME --details [--cwd DIR]` | Print the details part of the newest run that has details, with the prompt it answered. |
| `show NAME --last N [--cwd DIR]` | Print the last N runs: your prompt's first line and each answer. Use it to catch up after your context was compacted. |
| `wait [NAME\|RUN_FILE] [--timeout S] [--cwd DIR]` | Block until the run finishes (default 540 s), then print it like `ask`. Waits up to 10 s for a just-started run's file to appear. Still running: prints elapsed time and last activity, exit 3. Stale (the Peer process died or stopped updating for 5 minutes): exit 2. With no target, waits for the one run in progress in this workspace tree. |
| `sessions [--cwd DIR] [--all]` | Up to 20 recent sessions (both named and `(unnamed)`, including debate sessions) in this workspace tree (or everywhere): names, provider, saved settings, run count, last use, and first prompt. |
| `--to PROVIDER` | For `show`/`wait`, picks the provider when a name has several. |

## Text output

```text
peer: STATUS | PROVIDER MODEL/EFFORT | MODE | session NAME ID | DURATION
[error: ...]                        failures only
[blocked by: ...]                   permission notices that blocked the call
[questions: N (at the end of the answer)]
[changed: N paths / nothing / unknown (reason)]   work only, then indented paths
[note: ...]                         how changes were compared, when unusual
[warnings: N in the saved file]     denial-like text or signal-killed commands
[answer: N words, over the M asked for]
[details: N characters not shown (show NAME --details)]
[attached: ...]
[worktree: PATH (branch peer/NAME)]
[forked from: ID]
[replaced: the name pointed to ID before]
[diagnostics: ...]                  failures only, last 1,500 characters, lines not already shown
[file: PATH]                        unnamed runs, failures, or --full
---
ANSWER
```

`STATUS` is `ok`, `blocked`, `error`, `timeout`, `interrupted`, and for `show`/`wait` also `running` or `stale`. A debate prints one header, the sessions, and `=== N · PROVIDER MODEL ===` before each turn's answer.

## JSON output (`--json`)

A successful call prints compact JSON: `status`, `provider`, `mode`, `cwd`, `session_id`, `session_name`, `model`, `model_alias`, `effort`, `response` (the answer), `response_chars`, `response_words`, `questions` (when any), `details_chars`, worktree fields, `attachments`, `output_file`, `duration_seconds`, and `permission_warnings` as a count. Failures and `--full` print every field, including `details`, `error`, `permission_notices`, `permission_warnings`, `exit_code`, and `diagnostics`. A debate gives `sessions` and a `transcript` of `{provider, response, details_chars}` turns.

## Work results

Git comparisons cover dirty and untracked content, permissions, index state, and `HEAD`, so further edits to already-modified files, staging, and commits made by the child all count. Paths are relative to the repository root. A history change with the same tree reports a change with no paths. Outside Git, Peer compares file sizes and modification times (skipping `.git`, `node_modules`, virtualenvs, caches) up to 20,000 files; past that, the change is unknown.

## Saved state

- Run records: one JSON file per call, mode `0600` in a `0700` directory, including the prompt and the full reply. While a call runs, the record holds `status: "running"`, `pid`, `started_at`, `updated_at`, and every ~15 s `elapsed_seconds`, `output_events`, `last_activity`, and any `session_id` seen (a debate keeps these under `current`). Peer never deletes records.
- Session names: `$XDG_STATE_HOME/peer/session-names.json`, mapping provider, workspace, and name to a session ID and its last mode, model, effort, and word limit.

## doctor and models

`doctor` shows executable paths, effective model/effort/timeout, accepted effort levels, `reply_words`, and the output directory. It does not check sign-in or sandbox permissions. `models` shows configured models and aliases and lists Antigravity's models live; Codex and Claude Code have no model-list command, so ask the user about interactive `/model` choices.

## Live smoke test

From the repository, `PEER_PYTHON tests/live_smoke.py --to PROVIDER [--timeout SECONDS] [--config FILE] [--model NAME] [--effort LEVEL] [--output-dir DIR]` makes four real calls: a fresh consultation, a named resume that recovers a token, a consultation that must not change fixture files, and a work call that writes a verified file. Run it only when live provider use is authorized, in the background like a work call. It proves the current host-to-target route, not every provider combination or OS sandbox enforcement.
