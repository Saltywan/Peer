# Model selection

Checked on **2026-10-13**. Codex and Claude entries below come from official documentation; Antigravity IDs come from this machine's live `agy models` output. Availability depends on the account, provider, client version, and workspace settings. Check live choices before promising a model.

Peer passes full model IDs through unchanged. Its aliases come only from the user's config. Preserve an explicitly requested model; do not silently substitute another.

## Codex

- `gpt-6.1-sol`: current recommended choice for complex coding when available.
- `gpt-6-astra`: most demanding reasoning and coding work.
- `gpt-6-luna`: focused, repeatable tasks.
- `gpt-6-sol`: previous Sol model. The `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` models remain available during the rollout.

`gpt-5.5` retires from ChatGPT, ChatGPT Work, and Codex with ChatGPT sign-in on **2026-10-14**. That retirement does not apply to the OpenAI API. Refresh saved settings that explicitly select it.

Check `/model` in a signed-in interactive Codex session. Reasoning choices are model/client-specific; Peer validates the vocabulary, then Codex validates the combination. [Official Codex model guidance](https://learn.chatgpt.com/docs/models).

## Claude Code

Moving aliases include `best`, `fable`, `opus`, `sonnet`, and `haiku`. On the Anthropic API, the documented pinned examples are `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, and `claude-haiku-5-5`. Alias resolution can differ on gateways and other providers, and older CLI versions may not support newer IDs.

Check `/model` in a signed-in interactive session. Use a full ID to pin a version or an alias when the user wants its moving target. [Official Claude model configuration](https://code.claude.com/docs/en/model-config).

## Antigravity CLI

Live catalog checked on 2026-10-13:

- `gemini-3.8-flash-{high,medium,low}`
- `gemini-3.7-flash-{high,medium,low}`
- `gemini-3.6-flash-{high,medium,low}`
- `gemini-3.1-pro-{high,low}`
- `claude-sonnet-4-6`
- `claude-opus-4-6-thinking`
- `gpt-oss-120b-medium`

Expand braces to one exact ID, such as `gemini-3.8-flash-high`. Refresh with `agy models` or `peer models`; a catalog entry does not guarantee an account can execute it.
