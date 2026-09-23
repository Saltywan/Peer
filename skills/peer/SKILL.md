---
name: peer
description: Consult or delegate bounded work to Codex, Claude Code, or Antigravity CLI when the user requests another provider's perspective or implementation.
---

# Peer

Use the bundled `scripts/peer.py` with an absolute path derived from this skill folder. The target CLI must be installed and available to this host's shell.

A user can ask in ordinary language: “Ask Claude to review this design,” “Have Codex fix the parser using gpt-5.5 at high effort,” or “Let Codex and Antigravity debate this twice.” Choose the command and flags below; users do not need to write shell commands themselves.

## Commands and arguments

Replace `PEER_SCRIPT` below with the absolute path to this skill's `scripts/peer.py`. Run these from the project directory or pass its absolute path with `--cwd`.

```text
python3 PEER_SCRIPT ask --to {codex|claude|agy} [--mode {consult|work}] [--cwd DIR] [--session ID] [--model NAME] [--effort LEVEL] [--timeout SECONDS] [--config FILE] [--prompt TEXT]
python3 PEER_SCRIPT debate --a {codex|claude|agy} --b {codex|claude|agy} [--rounds N] [--session-a ID] [--session-b ID] [--cwd DIR] [--timeout SECONDS] [--config FILE] [--prompt TEXT]
python3 PEER_SCRIPT doctor [--config FILE]
```

`ask` makes one provider call. All its arguments are:

| Argument | Meaning and default |
| --- | --- |
| `--to` | **Required.** Target CLI: `codex`, `claude`, or `agy`. |
| `--mode` | `consult` (default, read/review) or `work` (may edit the workspace). |
| `--cwd` | Project directory. Relative paths are resolved to absolute paths; default is the shell's current directory. Use the same directory when resuming. |
| `--session` | Native session ID returned by an earlier `ask` for this provider. Omit to start a new session. |
| `--model` | Provider model name or alias for this call. Overrides Peer config, then the provider default. |
| `--effort` | Reasoning effort for this call. Overrides Peer config, then the provider default; valid values are below. |
| `--timeout` | Positive seconds allowed for this provider call. Defaults to `timeout_seconds` in config, or 600 seconds. |
| `--config` | JSON config path. If omitted, Peer uses `$PEER_CONFIG` when set; otherwise `$XDG_CONFIG_HOME/peer/config.json` when XDG is set, or `~/.config/peer/config.json`. |
| `--prompt` | Task text. If omitted, Peer reads stdin. An empty prompt is rejected. |

`debate` makes sequential `consult` calls to two **different** providers. One **round** means A answers, then B answers after seeing A's answer. In round 2, A sees B's previous answer, then B sees A's new answer. Thus `--rounds 2` makes A → B → A → B (four provider calls, unless one fails). Each handoff includes up to 12,000 characters of the previous answer. The outer caller reads the transcript and synthesizes it; Peer does not add a third summarizing call.

| Argument | Meaning and default |
| --- | --- |
| `--a`, `--b` | **Required.** First and second provider, each `codex`, `claude`, or `agy`; they must differ. |
| `--rounds` | Complete A-then-B rounds, integer 1–10; default 2. Plans `2 × N` calls; stops early if one fails. |
| `--session-a`, `--session-b` | Native session IDs for A and B if continuing an earlier debate. Omit either to start that provider's new session. |
| `--cwd` | Project directory for both providers; defaults to the shell's current directory. Reuse the original directory when resuming. |
| `--timeout` | Positive seconds **per provider call**, not for the entire debate. Defaults to config `timeout_seconds` or 600. |
| `--config` | Same config resolution as `ask`. Sets each provider's model and effort; `debate` has no per-call model or effort flags. |
| `--prompt` | Question or topic. If omitted, Peer reads stdin. |

`doctor` checks which CLIs are on `PATH` and reports configured model, effort, timeout, and Claude work-tool settings. Its only argument is `--config FILE`, with the same default resolution. Every command accepts `-h` or `--help` to show CLI help.

## Models

Run `python3 PEER_SCRIPT doctor` to see Peer’s configured model and effort for each provider. `null` means Peer passes no override, so the provider chooses its own default. To set a lasting Peer preference, edit `~/.config/peer/config.json` under `providers.PROVIDER.model` and `providers.PROVIDER.effort` (see the repository’s `config.example.json`). For one `ask`, use `--model NAME --effort LEVEL`. Debate uses the configured defaults for both providers.

For example, a config file can contain `{"providers":{"codex":{"model":"gpt-5.5","effort":"high"},"agy":{"model":"gemini-3.8-flash-high","effort":"medium"}}}`. For Claude work-mode shell access, its optional `work_allowed_tools` config list passes tool rules to Claude; leave it empty unless the task needs specific commands.

Valid `--effort` values are Codex `low|medium|high|xhigh|max|ultra`, Claude `low|medium|high|xhigh|max`, and Antigravity `low|medium|high`. The selected model must support the selected effort; the provider CLI makes the final check.

These model names are a **2026-09-24 snapshot**, not a guarantee that this account can use them. Check live choices before promising a model, and report a provider rejection rather than silently replacing the requested model:

- **Codex:** `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5` appeared in the local catalog. Use `/model` in interactive Codex to inspect available choices. This machine's live CLI accepted `gpt-5.5`; it rejected `gpt-6-sol` despite its catalog entry. [Codex model guidance](https://learn.chatgpt.com/docs/models).
- **Claude Code:** use moving aliases `fable`, `opus`, `sonnet`, or `haiku`, or a full model ID. Open `/model` in an authenticated interactive Claude session to see eligible choices. Claude is not signed in on this machine, so none has been live-tested through Peer. [Claude model configuration](https://code.claude.com/docs/en/model-config).
- **Antigravity CLI:** run `agy models` for the live account list. The local list was `gemini-3.8-flash-{high,medium,low}`, `gemini-3.7-flash-{high,medium,low}`, `gemini-3.6-flash-{high,medium,low}`, `gemini-3.1-pro-{high,low}`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`. Expand braces to a single ID, such as `gemini-3.8-flash-high`.

```sh
python3 PEER_SCRIPT ask --to claude --mode consult --cwd "$PWD" --prompt 'Review this design.'
python3 PEER_SCRIPT ask --to codex --mode work --cwd "$PWD" --model gpt-5.5 --effort high --prompt 'Fix the parser.'
python3 PEER_SCRIPT ask --to agy --mode work --cwd "$PWD" --prompt "Fix the bug in $PWD/src/parser.py and run its tests."
python3 PEER_SCRIPT ask --to codex --cwd "$PWD" --session SESSION_ID --prompt 'Check one more edge case.'
python3 PEER_SCRIPT debate --a codex --b agy --rounds 2 --cwd "$PWD" --prompt 'Which design is simpler?'
```

Each command prints JSON. For `ask`, use `status`, `response`, and `session_id`; for `debate`, use `transcript` and `sessions`. A non-`ok` status exits with code 2. Keep the returned provider, session ID, and cwd to resume that provider explicitly.

In Antigravity, invoke `python3 <absolute-path-to-this-skill-folder>/scripts/peer.py ask ... --prompt '...'` directly, without a shell pipeline. This lets Antigravity match a scoped `command(...)` permission rule for the Peer launcher. Quote the prompt as one shell argument.

- Use `--mode consult` for advice, reviews, and debates. Use `--mode work` when the user asks the other provider to implement or edit. The target may edit files in its workspace in work mode.
- Pass `--model` and `--effort` when the user chooses them; otherwise Peer uses its config or the provider's defaults.
- To continue, keep the returned provider, `session_id`, and `cwd`, then pass `--session ID`. If the ID or cwd is missing from the current context, ask for it or identify one unique matching native session before resuming. Never guess or substitute a "continue latest" command.
- For a two-provider discussion, use `debate --a PROVIDER --b PROVIDER --rounds N`. The outer caller synthesizes the exchange.
- If this host blocks the Peer command, use its normal scoped permission or approval flow. If Peer returns `blocked`, `error`, or `timeout`, inspect `permission_notices`, `error`, and `diagnostics`; report what happened and preserve any returned session ID. Do not switch to a permission-bypass flag. A called agent cannot exceed this host's sandbox.
- After `work`, inspect the actual diff and status. Do not run concurrent editors in one checkout; use separate worktrees for parallel work.

Run `python3 PEER_SCRIPT --help` for additional CLI help. Do not call Peer recursively from an agent launched by Peer; the wrapper rejects it.
