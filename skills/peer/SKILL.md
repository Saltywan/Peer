---
name: peer
description: Consult or delegate bounded work to Codex, Claude Code, or Antigravity CLI when the user requests another provider's perspective or implementation.
---

# Peer

Use the bundled `scripts/peer.py` with an absolute path derived from this skill folder. Pass the project directory with `--cwd` and the task prompt through stdin. The target CLI must be installed and available to this host's shell.

- Use `--mode consult` for advice, reviews, and debates. Use `--mode work` when the user asks the other provider to implement or edit. The target may edit files in its workspace in work mode.
- Set `--to codex`, `--to claude`, or `--to agy`. Pass `--model` and `--effort` when the user chooses them; otherwise Peer uses its config or the provider's defaults.
- To continue, keep the returned provider, `session_id`, and `cwd`, then pass `--session ID`. Never substitute a "continue latest" command.
- For a two-provider discussion, use `debate --a PROVIDER --b PROVIDER --rounds N`. The outer caller synthesizes the exchange.
- If this host blocks the Peer command, use its normal scoped permission or approval flow. If Peer returns `blocked`, `error`, or `timeout`, report what happened and preserve any returned session ID. Do not switch to a permission-bypass flag. A called agent cannot exceed this host's sandbox.
- After `work`, inspect the actual diff and status. Do not run concurrent editors in one checkout; use separate worktrees for parallel work.

Run `python3 <this-skill-folder>/scripts/peer.py --help` for arguments. Do not call Peer recursively from an agent launched by Peer; the wrapper rejects it.
