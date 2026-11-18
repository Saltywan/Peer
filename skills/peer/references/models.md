# Model selection

Checked on **2026-11-18**. Codex and Claude entries below come from official documentation and local CLI catalogs; Antigravity IDs come from this machine's live `agy models` output. Availability depends on the account, provider, client version, and workspace settings. Check live choices before promising a model.

Peer passes full model IDs through unchanged. Its aliases come only from the user's config. Preserve an explicitly requested model; do not silently substitute another.

## Codex

- `gpt-6.2-sol` / `gpt-6.1-sol`: current recommended workhorse models for complex coding and everyday engineering when available.
- `gpt-6.2-astra` / `gpt-6-astra`: flagship models for the most demanding reasoning and coding work.
- `gpt-6.1-terra`: fast, efficient model for visual reasoning, computer use, and scoped work.
- `gpt-6-luna`: fast, affordable model for focused, repeatable tasks.
- `gpt-6-sol`: previous Sol model. `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` also remain in the catalog during the rollout.

`gpt-5.5` retired from ChatGPT, ChatGPT Work, and Codex with ChatGPT sign-in on **2026-10-14**. That retirement does not apply to the OpenAI API. Refresh saved settings (including `~/.config/peer/config.json`) that still select it.

Check `/model` in a signed-in interactive Codex session. Built-in effort levels are `low`, `medium`, `high`, `xhigh`, `max`, and `ultra` (`gpt-6-luna` caps at `max`); Peer validates the vocabulary, then Codex validates the combination. [Official Codex model guidance](https://learn.chatgpt.com/docs/models).

## Claude Code

Moving aliases include `best`, `fable`, `opus`, `sonnet`, and `haiku` (plus `opus[1m]` and `sonnet[1m]` for 1M-context sessions where supported). On the Anthropic API, the documented pinned examples are `claude-fable-5-2` (with earlier `claude-fable-5-1` and `claude-fable-5`), `claude-opus-5-5`, `claude-sonnet-5-5`, and `claude-haiku-5-5`. Alias resolution can differ on gateways and other providers (for example, `fable` resolves to Fable 5 in gateway sessions and Fable 5.2 elsewhere unless `ANTHROPIC_DEFAULT_FABLE_MODEL` is set), and older CLI versions may not support newer IDs.

Check `/model` in a signed-in interactive session. Built-in `--effort` levels are `low`, `medium`, `high`, `xhigh`, and `max`. Use a full ID to pin a version or an alias when the user wants its moving target. [Official Claude model configuration](https://code.claude.com/docs/en/model-config).

## Antigravity CLI

Live catalog checked on 2026-11-18 (`agy` 1.5.9):

- `gemini-4-argon-{high,medium,low}`
- `gemini-4-hydrogen-{xhigh,high,medium,low}`
- `gemini-pro-orchestrator-g1-pro`
- `gemini-3.8-flash-{high,medium,low}`
- `claude-opus-5-5-{high,medium,low}`
- `claude-sonnet-5-5-{high,medium,low}`
- `gpt-oss-120b-medium`

`gemini-4-carbon-{high,medium,low}` is also available when connected via `GEMINI_API_KEY` or `AGY_ADC_AUTH`. Expand braces to one exact ID, such as `gemini-4-argon-high`, or pass a base model name such as `gemini-4-argon` (with or without `--effort`, which accepts `low`, `medium`, `high`, `xhigh`, and `max`). Refresh with `agy models` or `peer models`; a catalog entry does not guarantee an account can execute it.
