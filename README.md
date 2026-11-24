# Peer

Peer lets Codex, Claude Code, and Antigravity CLI consult or delegate work to one another through their local CLIs. It uses each CLI's own saved session history. Peer itself keeps only two things: each run's result in a private file (see [Saved results](#saved-results)), and a small map from session names you choose to native session IDs (see [Resuming](#resuming)).

## Quick start

Requires Python 3.9+ and the target CLIs installed and authenticated on the same machine.

```sh
printf '%s' 'Review this design for failure modes.' | ./peer ask --to claude --mode consult --cwd "$PWD"
printf '%s' 'Implement the parser and run its tests.' | ./peer ask --to agy --mode work --cwd "$PWD"
```

Each call prints a short text result: a header line, then the answer. `--json` prints JSON instead. To hold a conversation, name it when you start it and continue it with `reply`:

```sh
printf '%s' 'Review the parser.' | ./peer ask --to claude --new-session parser --cwd "$PWD"
printf '%s' 'Check the edge cases too.' | ./peer reply parser
./peer show parser --last 3
```

See [Conversations](#conversations) for the reply format, `show`, and `wait`. The skill's [CLI reference](skills/peer/references/cli.md) lists every flag and output field.

Use `./peer doctor` to see installed CLI paths and configured defaults, `./peer models` for models and aliases, and `./peer sessions` for recent sessions in this directory. Use `./peer ask --help` for all flags. `--prompt` is available for short prompts; stdin keeps longer prompts out of shell arguments.

## Modes and permissions

| Target | `consult` | `work` | `auto` |
| --- | --- | --- | --- |
| Codex | Enforced `read-only` sandbox, no approval prompts | `workspace-write` sandbox, no approval prompts | `workspace-write` sandbox with `auto_review` approval routing |
| Claude | `plan` with read/search tools, `--permission-prompts none` | `acceptEdits`, `--permission-prompts none`; optional `work_allowed_tools` | `auto` permission classifier, `--permission-prompts none` |
| Antigravity | `plan` behavior and terminal sandbox | `accept-edits`, terminal sandbox, and session-only `toolPermission: "proceed-in-sandbox"` | `auto` mode, terminal sandbox, and session-only `toolPermission: "proceed-in-sandbox"` |

Peer does not pass any provider's permission-bypass flag by default. When needed (for example, to avoid headless permission denials in Antigravity or to pass provider-specific flags), you can pass extra CLI flags per call with `--extra-arg=--flag` (repeatable) or after `--` on `ask`/`reply` (and `--extra-arg-a` / `--extra-arg-b` on `debate`), or persistently per provider via `providers.<provider>.extra_args` in `~/.config/peer/config.json`. Passing a bypass flag (`--dangerously-skip-permissions` for `agy` or `claude`, `--dangerously-bypass-approvals-and-sandbox` for `codex`) automatically skips Peer's conflicting sandbox/permission flags for that call (for example, Peer omits `--sandbox` and its default `--settings` on `agy` unless you explicitly pass them). Extra CLI flags given on `ask --new-session` are remembered for `reply`.

A call is still limited by the **calling agent's** sandbox. If the caller cannot execute a CLI, access its saved sessions, or write to the target workspace, the child cannot override that boundary.

For Antigravity (`agy`) targets specifically, Peer prevents the most common headless permission failures automatically:
- In `work` and `auto` modes, Peer passes session-only `--settings '{"toolPermission": "proceed-in-sandbox"}'` (never saved to disk) unless you pass your own `--settings` or `--dangerously-skip-permissions`. Without this, headless `agy` inherits `request-review` (or `strict`) from `settings.json` and soft-denies even safe sandboxed commands like `pytest` or `git status`.
- Inside a linked Git worktree (`--worktree` or a manual worktree), Peer automatically passes `--add-dir` for the repository's `.git` common directory so `agy` can run Git commands inside its sandbox without exposing the main checkout's working files.
- If a task still needs commands outside Antigravity's terminal sandbox (network access, local apps, or external paths), pass `-- --dangerously-skip-permissions` (or set `"extra_args": ["--dangerously-skip-permissions"]` under `providers.agy` in `~/.config/peer/config.json`), or pass scoped `-- --settings '{"permissions":{"allow":[...]}}'` rules.

Host permissions need their own setup: a Codex host may need to approve the exact Peer command outside its sandbox so the child can write native session files and connect; a Claude host needs permission to run the Peer script. An Antigravity host needs scoped `command(...)` and `unsandboxed(...)` allowances for the installed launcher, because child CLIs use saved sessions and network access. Test the command from each host. A target permission flag alone cannot fix a blocked host command.

For Antigravity headless use, the optional rules in `~/.gemini/antigravity-cli/settings.json` are `command(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` and `unsandboxed(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` in `permissions.allow`. Replace the placeholder with the installed skill's `scripts/peer.py` path. These rules let Antigravity run Peer outside its terminal sandbox without a prompt, including Peer `work` calls that may edit files in other workspaces. Add them only if that host access is intended. The skill invokes the script directly so a prefix rule can match; commands with shell substitution may require exact matching.

Claude and Antigravity distinguish editing files from running shell commands. Antigravity headless calls can report a denied tool on stderr while exiting successfully; Peer returns `status: "blocked"` when it detects that notice. Only structured denials, failed commands, and stderr set `blocked`. Denial-like text in successful command output or in the reply, such as a grep hit on the phrase "permission denied", goes to `permission_warnings` and leaves `status` alone. A Codex command killed by a signal (exit code 128 or higher, such as Chrome exiting with 134 inside the sandbox) also becomes a warning, because it usually means the target could not run a local app. Configure scoped command permissions in Antigravity's own settings for commands delegated work needs.

After a `work` call, `changed_paths` lists what changed during the call and `worktree_changed` summarizes it. In Git, Peer compares dirty/untracked file content and permissions, index state, and `HEAD`. This detects further edits to already-modified files, staging changes, and edits committed by the child, including a first commit. Paths are relative to the repository root even when `--cwd` is a subdirectory. A history change with the same tree reports true with an empty path list and a note. Outside Git, Peer compares the size and modification time of every file (skipping `.git`, `node_modules`, virtualenvs, and caches), up to 20,000 files. Past that limit, `worktree_changed` is `null` and `worktree_note` says why; `null` means unknown, not unchanged. A model's claim that it edited a file is not proof of a change.

Antigravity's file tool can otherwise choose its own scratch directory for a relative path. Peer includes the absolute workspace path in every work prompt and asks for absolute file paths under it. Check the returned `changed_paths` and the diff; a successful model response alone does not establish that it wrote to the requested checkout.

For strict consultation isolation, verify your target's mode in a throwaway directory. Codex enforces a read-only sandbox; Claude and Antigravity plan modes are not equivalent OS-level file sandboxes. Keep one writer per checkout. Use separate worktrees for parallel editing tasks.

## Model and effort defaults

Copy `config.example.json` to `~/.config/peer/config.json`, or pass `--config PATH`. This file stores preferences, not sessions. Unknown keys are rejected so that typos do not pass silently. The precedence is command flag, Peer config, then the provider's own default. Model names pass through unchanged unless they match an alias in `providers.PROVIDER.aliases`, for example `"aliases": {"sol": "gpt-6.2-sol"}`. Aliases are case-insensitive, come only from your config, and the result reports both the resolved `model` and the `model_alias`. `./peer models` shows aliases and lists Antigravity's models live; Codex and Claude Code have no CLI command that lists models. Effort values are checked against each CLI's supported vocabulary, then the CLI validates the model/effort pairing. When a CLI adds levels before Peer knows them, set `providers.PROVIDER.efforts` to the full list.

Model guidance was checked on November 18, 2026. For Codex, [OpenAI recommends `gpt-6.2-sol` / `gpt-6.1-sol`](https://learn.chatgpt.com/docs/models) for complex coding when available to your account. See [the model reference](skills/peer/references/models.md) for current examples, other providers, and how to check live choices. Peer has no built-in model allowlist and does not automatically replace an explicit model or alias.

```sh
printf '%s' 'Find the bug and fix it.' | ./peer ask --to codex --mode work \
  --model gpt-6.2-sol --effort high --cwd "$PWD"
```

## Timeouts and interruption

`--timeout` (default 600 seconds) applies to each provider call. When it expires, or when Peer receives SIGINT, SIGTERM, or SIGHUP, Peer sends SIGTERM to the child's process group, waits 5 seconds so the agent can save its session, then sends SIGKILL to anything left. The result is `timeout` or `interrupted`, with any session ID already returned. SIGKILL to Peer itself cannot be caught and can leave the child running. Give the host's own command timeout more time than Peer's, or run Peer in the background.

`--attach FILE` (repeatable) gives the target a file. Codex receives images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) natively through `--image`. Other files, and all files for Claude and Antigravity, are listed by absolute path in the prompt for the target to open; directories outside `--cwd` are added with `--add-dir` for Claude and Antigravity. The result's `attachments` says which were passed natively. PDFs are never attached natively.

## Saved results

`ask` and `debate` save the full result, plus the prompt, to `$XDG_STATE_HOME/peer/runs/` (default `~/.local/state/peer/runs/`) as `TIMESTAMP-COMMAND-PROVIDERS-PID-NANOSECONDS.json`, and return the path as `output_file`. The nanosecond suffix distinguishes fast calls from the same process. Files are mode `0600` in a `0700` directory. Set `output_dir` in config to change the location, pass `--output FILE` for one run (useful for background runs, since you know the path in advance), or `--no-save` to skip writing. Peer never deletes old files; prune the directory yourself.

The file exists from the start of the run. Until the call finishes it holds `status: "running"` with `pid`, `started_at`, and `updated_at`, and about every 15 seconds Peer adds `elapsed_seconds`, `output_events`, `last_activity` (the child's latest streamed event, such as a Codex command), and any `session_id` seen so far. A debate keeps these under `current` and adds each finished turn. A `running` file whose `updated_at` stopped changing, or whose `pid` no longer exists, is stale: Peer was killed.

With `--output FILE`, Peer also links the file from the run directory, so session names, `show`, `wait`, and `sessions` still find the run.

Peer writes only to stdout, never stderr, so a background run's output is exactly its result. By default that result is text: one header line (`peer: STATUS | PROVIDER MODEL/EFFORT | MODE | session NAME ID | DURATION`), lines that need attention (error, permission notices, questions, changed paths, warning count, details not shown), `---`, and the answer. The saved-file path is printed for unnamed runs and failures, since a named run is found with `show NAME`. Argument and configuration errors exit with code 2 in the same format. `--json` prints compact JSON (the answer plus the fields needed to resume or check work, with `permission_warnings` as a count and `changed_paths` capped at 30 entries); `doctor` and `models` always print JSON. Diagnostics, notices, exit codes, details, and full lists stay in the saved file; failures show the error, notices, and the last 1,500 characters of diagnostics. `--full` prints everything, as does `--no-save`, since there is no file to hold the rest. All configured providers are validated before launch, including field types and finite positive timeouts.

Measured on a 13-round review, the reply text was about 60% of what Peer added to the caller's context, and JSON wrapping cost about 6% more tokens than plain text. The reply format below is what keeps that small.

## Conversations

**Reply format.** Peer appends a short format request to every prompt: answer in at most `reply_words` words (default 400; `--words N` per call, `0` to turn it off), end with `QUESTIONS:` and one `- ` line per question when the caller's input is needed, and put longer evidence after a line containing only `---details---`. Peer splits the reply: the caller sees the answer and a `questions:` count, while the details are saved and reported as `details: N characters not shown`. Debates use the same format without questions, and each side receives the other's whole reply. The request costs the caller no context, since Peer adds it after the caller's prompt. Models do not always obey a word limit; the header reports an answer more than 25% over it.

**Names, `reply`, forking, and worktrees.** `--new-session NAME` starts a conversation and records NAME for that provider and workspace in `$XDG_STATE_HOME/peer/session-names.json`, together with the call's mode, model, effort, word limit, and any managed worktree. `reply NAME` continues it with those settings, in the session's own workspace or worktree, so a follow-up needs only the name and the new message. A flag on `reply` overrides one setting and the override is saved. `--fork-from NAME` (or `--fork-session ID`) branches an existing Codex or Claude session into a new session without altering the original conversation, and can be paired with `--new-session NEW_NAME`. `--worktree [WT]` runs `ask` in an isolated Git worktree under `$XDG_STATE_HOME/peer/worktrees/` on branch `peer/<WT>`, keeping the main checkout untouched during parallel delegated edits. NAME is found from the session's workspace (or worktree) or any folder inside or above it, never from a sibling project; when a name has sessions with several providers (after a named debate), pass `--to`. If NAME already pointed at another session, `--new-session` repoints it and reports `replaced`. `ask --session-name NAME` is equivalent to `reply NAME --to PROVIDER`; names are 1–64 letters, digits, `.`, `_`, or `-`, and an unknown name fails instead of starting a new conversation.

**`show` and `wait`.** `show NAME` prints the latest result again, `show NAME --details` prints the newest saved details with the prompt they answered, and `show NAME --last N` prints the last N prompts and answers, which lets a caller catch up after its context is compacted. `wait NAME` (or `wait FILE` for a run started with `--output FILE`) blocks until the run finishes, 540 seconds by default, and prints it. Exit code 3 means it is still running; `stale` means the Peer process died or stopped updating for five minutes.

On every call, Peer passes the selected mode, model, and effort again, including when resuming a session. It always resumes by explicit ID or by a name you created. Do not use Codex `--ephemeral` or Claude `--no-session-persistence` with Peer.

`./peer sessions` lists up to 20 recent sessions in the current workspace tree from the saved run files: names, provider, saved settings, ID, number of runs, last use, and the first line of the first prompt. `--all` includes every directory.

## Debate

```sh
printf '%s' 'Should this cache be write-through or write-back?' | \
  ./peer debate --a codex --b claude --rounds 2 --cwd "$PWD"
```

`debate` alternates read-only consultations, returns the exchange plus both session IDs, and stops after the requested rounds or at the first failed turn. It checks both providers before starting and retains completed turns and known session IDs on later launch or session-name errors, including with `--no-save`. Successful output shows each turn's answer and how many details characters were saved; failures and the saved file keep full turn details. Pass `--session-a` and `--session-b` to continue a previous pair, or use `--new-session NAME` and `--session-name NAME` to name both sides at once. `--model-a`, `--model-b`, `--effort-a`, and `--effort-b` set each side's model and effort; otherwise config applies. Attachments go with each side's first turn. Each side receives the current brief on its first turn, then only the latest exchange on later turns. Peer caps recursive calls from an agent it launched, so the outer caller coordinates the discussion.

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

The opt-in `tests/live_smoke.py` automates fresh consultation, named-session memory recovery, unchanged consultation fixtures, and an actual delegated file write. Select your project's interpreter or the host's Python skill interpreter, then run:

```sh
/absolute/path/to/python tests/live_smoke.py --to claude --timeout 180
```

It accepts `--to`, `--timeout`, `--config`, `--model`, `--effort`, and `--output-dir` (an existing parent for a new fixture directory). It uses normal permissions, may incur provider usage, and retains fixtures, per-call results, and `summary.json` in the reported artifact directory. Run it in the background. These checks prove the tested host-to-target behavior; unchanged consultation files do not prove OS sandbox enforcement. Command permission/denial probes remain a separate live check.

Provider references: [Codex sandbox and approvals](https://learn.chatgpt.com/docs/agent-approvals-security), [Claude CLI and permissions](https://code.claude.com/docs/en/cli-reference), [Antigravity CLI reference](https://antigravity.google/docs/cli/reference), [Antigravity permissions](https://antigravity.google/docs/permissions).
