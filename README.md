# Peer

Peer lets Codex, Claude Code, and Antigravity CLI consult or delegate work to one another through their local CLIs. It uses each CLI's own saved session history. Peer stores no conversation database or transcript.

## Quick start

Requires Python 3.9+ and the target CLIs installed and authenticated on the same machine.

```sh
printf '%s' 'Review this design for failure modes.' | ./peer ask --to claude --mode consult --cwd "$PWD"
printf '%s' 'Implement the parser and run its tests.' | ./peer ask --to agy --mode work --cwd "$PWD"
```

Each call prints one JSON object with `status`, `response`, and `session_id`. To continue a specific conversation, pass both the same provider and the returned ID:

```sh
printf '%s' 'Check the edge cases too.' | ./peer ask --to claude --mode work --cwd "$PWD" --session SESSION_ID
```

Use `./peer doctor` to see installed CLI paths and configured defaults. Use `./peer ask --help` for all flags. `--prompt` is available for short prompts; stdin keeps longer prompts out of shell arguments.

## Modes and permissions

| Target | `consult` | `work` |
| --- | --- | --- |
| Codex | Enforced `read-only` sandbox, no approval prompts | `workspace-write` sandbox, no approval prompts |
| Claude | `plan` with read/search tools | `acceptEdits`; optional `work_allowed_tools` for shell commands |
| Antigravity | `plan` behavior and terminal sandbox | `accept-edits` and terminal sandbox; command permissions come from Antigravity settings |

Peer never passes any provider's permission-bypass flag. A call is also limited by the **calling agent's** sandbox. If the caller cannot execute a CLI, access its saved sessions, or write to the target workspace, the child cannot override that boundary.

Host permissions need their own setup: a Codex host may need to approve the exact Peer command outside its sandbox so the child can write native session files and connect; a Claude host needs permission to run the Peer script. An Antigravity host needs scoped `command(...)` and `unsandboxed(...)` allowances for the installed launcher, because child CLIs use saved sessions and network access. Test the command from each host. A target permission flag alone cannot fix a blocked host command.

For Antigravity headless use, the optional rules in `~/.gemini/antigravity-cli/settings.json` are `command(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` and `unsandboxed(python3 ABSOLUTE_INSTALLED_PEER_SCRIPT)` in `permissions.allow`. Replace the placeholder with the installed skill's `scripts/peer.py` path. These rules let Antigravity run Peer outside its terminal sandbox without a prompt, including Peer `work` calls that may edit files in other workspaces. Add them only if that host access is intended. The skill invokes the script directly so a prefix rule can match; commands with shell substitution may require exact matching.

Claude and Antigravity distinguish editing files from running shell commands. Antigravity headless calls can report a denied tool on stderr while exiting successfully; Peer returns `status: "blocked"` when it detects that notice. Configure scoped command permissions in Antigravity's own settings for commands delegated work needs. Peer returns Git status before and after `work` calls so the caller can inspect the actual diff. A model's claim that it edited a file is not proof of a change.

Antigravity's file tool can otherwise choose its own scratch directory for a relative path. Peer includes the absolute workspace path in every work prompt and asks for absolute file paths under it. Check the returned Git status and diff; a successful model response alone does not establish that it wrote to the requested checkout.

For strict consultation isolation, verify your target's mode in a throwaway directory. Codex enforces a read-only sandbox; Claude and Antigravity plan modes are not equivalent OS-level file sandboxes. Keep one writer per checkout. Use separate worktrees for parallel editing tasks.

## Model and effort defaults

Copy `config.example.json` to `~/.config/peer/config.json`, or pass `--config PATH`. This file stores preferences, not sessions. The precedence is command flag, Peer config, then the provider's own default. Model names pass through unchanged. Effort values are checked against each CLI's supported vocabulary, then the CLI validates the model/effort pairing.

```sh
printf '%s' 'Find the bug and fix it.' | ./peer ask --to codex --mode work \
  --model gpt-6-sol --effort high --cwd "$PWD"
```

On every call, Peer passes the selected mode, model, and effort again, including when resuming a session. It always resumes by explicit ID. Do not use Codex `--ephemeral` or Claude `--no-session-persistence` with Peer.

## Debate

```sh
printf '%s' 'Should this cache be write-through or write-back?' | \
  ./peer debate --a codex --b claude --rounds 2 --cwd "$PWD"
```

`debate` alternates read-only consultations, returns the full exchange plus both session IDs, and stops after the requested rounds. Pass `--session-a` and `--session-b` to continue a previous pair. Peer caps recursive calls from an agent it launched, so the outer caller coordinates the discussion.

## Install the shared skill

The canonical skill is `skills/peer/`. From the repository root, install it for local use across projects:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills ~/.gemini/antigravity-cli/skills
ln -s "$PWD/skills/peer" ~/.agents/skills/peer
ln -s "$PWD/skills/peer" ~/.claude/skills/peer
cp -R skills/peer ~/.gemini/antigravity-cli/skills/peer
```

These are the personal skill locations:

- Codex: `~/.agents/skills/peer`
- Claude Code: `~/.claude/skills/peer`
- Antigravity CLI: `~/.gemini/antigravity-cli/skills/peer`

Codex and Claude document support for symlinked skill folders. Antigravity gets a copy because symlink discovery is not documented; copy it again after updating Peer. The skill points at its bundled `scripts/peer.py`, so the CLI does not need to be on `PATH`. Check each host's skill list after installation.

## Verification

`python3 -m unittest discover -s tests -v` runs adapter tests with fake CLIs. A live setup check should then use a disposable Git repository from each host to each target: make a fresh call, resume its ID, try a consultation that must not edit, delegate an edit inside the workspace, run one permitted command, and verify a denied command is reported as blocked. The host-to-target matrix is the real test of shell, credential, and sandbox access.

Provider references: [Codex sandbox and approvals](https://learn.chatgpt.com/docs/agent-approvals-security), [Claude CLI and permissions](https://code.claude.com/docs/en/cli-reference), [Antigravity headless permissions](https://www.agy.dev/docs/cli/headless/).
