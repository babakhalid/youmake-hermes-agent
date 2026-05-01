# Changelog — Youmake fork

This file documents changes that diverge from upstream
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
The fork is maintained at
[babakhalid/youmake-hermes-agent](https://github.com/babakhalid/youmake-hermes-agent).

Upstream is licensed MIT (Copyright (c) 2025 Nous Research). The full
upstream license is preserved verbatim in `LICENSE`. All changes below
are released under the same MIT terms.

## [youmake-v1.0.0] — 2026-05-01

Forked from upstream's `main` to customize the agent for production
deployment inside the Youmake platform: per-user Hetzner VMs serving an
OpenAI-compatible API to a Node/Express orchestrator.

### Added

- **`event: tool_use.started` / `tool_use.completed` SSE events** on
  `/v1/chat/completions`. Carry `id`, `tool`, `input`, `ts`,
  `output_preview`, `duration_ms` so the orchestrator can render tool
  state in real time without parsing assistant text. Stock OpenAI SDKs
  ignore unknown `event:` types, so the additions are non-breaking.
- **`event: token_usage.delta` / `token_usage.final` SSE events**.
  Mid-stream incremental and final cumulative records of input, output,
  cache-read, and cache-write tokens. Wired through a new optional
  `token_usage_callback` on `AIAgent` that fires once per LLM API call.
- **`event: approval.request` SSE event** + new endpoint
  `POST /v1/sessions/{session_id}/approval`. Resolves dangerous-command
  approvals out-of-band so the agent doesn't surface
  `approval_required` text the model would otherwise try to recover
  from in-band, bloating context. Body:
  `{request_id, decision: "approve"|"deny", remember?: bool}`.
  Maps to `tools.approval.resolve_gateway_approval`'s `once`/`session`/
  `deny` choices.
- **`GET /v1/sessions/{session_id}/usage`**. Returns cumulative token
  counts and tool-call list for a session by reading the persistent
  SessionDB. Used to render a live token-spend badge independent of
  any active SSE stream.
- **Slim identity template** (`templates/soul-youmake-minimal.md`,
  ~2K tokens) selected by env var `HERMES_IDENTITY_TEMPLATE`. Default
  is `youmake_minimal`; set to `hermes_full` for upstream behaviour.
  Under `youmake_minimal` the system prompt skips the multi-K-token
  skill catalog, tool-use enforcement guidance, model-specific operator
  guidance, and project context-files block — cutting the cached
  prefix from ~13K tokens to under 6K (closer to ~2-3K in practice).
- **`youmake-chat` toolset preset** in `toolsets.py`, composing
  `[web, terminal, file, todo]` so newly provisioned VMs ship with
  the chat-product toolset baked in.

### Changed

- **Anthropic cache-token fields preserved** in the OpenAI-compat
  `usage` object on both streaming and non-streaming
  `/v1/chat/completions` responses (also pushed upstream — see PR).
  `cache_read_input_tokens` and `cache_creation_input_tokens` are
  now passed through, so the per-message cost surfaced to API callers
  matches what Anthropic billed (10% on cache reads).
- **User-facing brand** ("Hermes Agent" → "Youmake Agent") only when
  `HERMES_IDENTITY_TEMPLATE=youmake_minimal`. Internal package names
  (`hermes-agent`, the Python module names, the OpenRouter referrer
  header `X-OpenRouter-Title`, the `/v1/models` `owned_by` field, the
  capabilities object's `platform` field) intentionally untouched so
  imports and external orchestrators that key off them keep working.

### Build

- `templates/` ships in sdist and wheel via `[tool.setuptools]`.
  `pip install git+https://github.com/babakhalid/youmake-hermes-agent.git@youmake-v1.0.0`
  resolves the slim template at runtime. Verified end-to-end on a
  fresh Python 3.11 venv.

### Notes

- The package name in `pyproject.toml` stays `hermes-agent` so that
  `from run_agent import AIAgent` and similar imports continue to
  work in user code, plugins, and skills written against upstream.
- The Hermes setting `HERMES_GATEWAY_SESSION` is set to `"1"` once
  when the API server's `connect()` runs. Per-session approval
  callback registration still gates which sessions actually block;
  unregistered sessions fall through to the legacy `approval_required`
  text path.
