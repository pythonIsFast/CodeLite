# Code Lite

A lightweight, from-scratch coding agent -- currently at its **first build
stage**: a pure-Python, stdlib-only proxy that lets you call OpenAI-shaped
APIs (`/v1/chat/completions`, `/v1/responses`, `/v1/models`,
`/v1/images/generations`, `/v1/images/edits`) using your existing **ChatGPT
Plus** subscription instead of API credits, by reusing the OAuth tokens the
official [Codex CLI](https://github.com/openai/codex) already stores at
`~/.codex/auth.json`.

Later stages will add an agent loop, tool use (reading/writing files,
running shell commands, ...), context management, and a UI on top of this
provider layer -- aiming for something in the spirit of
[opencode](https://github.com/sst/opencode), but smaller on disk and with
its own UI.

This is a from-scratch Python reimplementation of the ideas in
[EvanZhouDev/openai-oauth](https://github.com/EvanZhouDev/openai-oauth)
(TypeScript/Bun); see [NOTICE](NOTICE) for attribution details. Both
projects are Apache-2.0.

## Why "provider layer" and not just "a proxy"?

The HTTP proxy (`codelite.provider.server`) is one *caller* of a small,
in-process interface -- it is not the interface itself. A future agent loop
can build a `codelite.provider.Session` directly and call `send_chat`,
`send_responses`, `generate_image`, `edit_image`, `list_models` in-process,
without spinning up a local HTTP server at all:

```python
from codelite.provider import load_session

session = load_session()
print(session.list_models())
result = session.send_chat({
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "Say hi in one word."}],
})
print(result["choices"][0]["message"]["content"])
```

## Running the local proxy

Requires only the Python 3 standard library -- no `pip install`, no
`requirements.txt`. You do need existing ChatGPT/Codex OAuth tokens (run
`codex login` once, using the official Codex CLI, if you haven't).

```bash
python3 -m codelite.provider --port 10531
```

Then point any OpenAI-client library at `http://127.0.0.1:10531/v1` with any
placeholder API key (the proxy ignores it -- auth comes from `auth.json`).

## What works

- **Token refresh**: reads `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`),
  refreshes the access token via the OAuth `refresh_token` grant when it's
  close to expiry (or hasn't been refreshed in the last 55 minutes), and
  writes the refreshed tokens straight back to the same file.
- **`/v1/responses`**: passthrough to Codex's `/responses`, with the same
  body normalization the reference implementation applies (forces
  `stream=true` upstream, `store=false`, adds
  `reasoning.encrypted_content` to `include`, applies model-specific
  reasoning/verbosity defaults from Codex's own model catalog). If the
  caller asked for a non-streaming response, the SSE reply is buffered
  into one JSON object; `previous_response_id` / `item_reference` are
  rejected up front since Codex's OAuth backend is stateless (`store: false`
  is forced) and can't replay history server-side -- replay the full
  conversation in `input` instead.
- **`/v1/chat/completions`** (streaming and non-streaming, including tool
  calls): translated to and from `/responses` directly, since Code Lite
  doesn't depend on the Vercel AI SDK the reference implementation uses for
  this. See the "Known limitations" note below.
- **`/v1/images/generations`** and **`/v1/images/edits`**: same field
  allow-list and restrictions as the reference (JSON-only, `b64_json`-only
  responses, no streaming, no masks, up to 5 reference images for edits,
  50 MB per file, default model `gpt-image-2`).
- **`/v1/models`**: Codex's own model catalog, filtered to publicly listed
  models, via the same `client_version` handshake the official CLI uses
  (auto-discovered from the `@openai/codex` npm package, with a pinned
  fallback if that lookup fails).

## Known limitations

- **No interactive login flow.** Code Lite only *refreshes* tokens that
  already exist in `auth.json`; it does not implement the browser-based
  OAuth authorization-code exchange. Use the official Codex CLI
  (`codex login`) to sign in the first time.
- **Chat Completions streaming event mapping is not verified against a live
  stream.** The reference implementation delegates all Chat Completions
  translation to the Vercel AI SDK (`ai` / `@ai-sdk/openai`), a third-party
  npm package. Since Code Lite is stdlib-only, `codelite/provider/chat.py`
  reimplements that translation directly against the public Responses API
  request/event shapes instead. The non-streaming path is on firm ground
  (it only depends on the final `response.output` shape, which is confirmed
  against the reference project's own test fixtures). The *streaming* event
  names (`response.output_item.added`, `response.function_call_arguments.delta`,
  `response.output_text.delta`, `response.completed`, ...) are implemented
  from general knowledge of OpenAI's public Responses API docs and have
  only been tested against hand-written fixtures, not a real streaming
  Codex response -- test before relying on it.
- **`stop` (stop sequences) and `max_tokens` are silently dropped** when
  translating a Chat Completions request: the Responses API has no direct
  equivalent, and Codex's own body normalization removes
  `max_output_tokens` from every request unconditionally regardless.
- Not hardened for exposure beyond `127.0.0.1`.

## Project layout

```
codelite/
  provider/          # this build stage
    config.py         # every endpoint/model/path default, in one place
    auth.py           # auth.json load / refresh / persist, JWT claim helpers
    transport.py       # authenticated Codex requests, /responses body normalization
    sse.py              # Server-Sent-Events parsing
    chat.py              # Chat Completions <-> Responses API translation
    images.py             # /v1/images/* request normalization
    multipart.py           # stdlib multipart/form-data decoder (no `cgi` dependency)
    models.py                # Codex model catalog + client-version resolution
    session.py                # the in-process "send a request, get an answer" interface
    server.py                  # the local HTTP proxy (one more Session caller)
    __main__.py                 # `python -m codelite.provider` CLI entry point
```

Later stages (`codelite/agent/`, `codelite/tools/`, `codelite/ui/`, ...)
will sit alongside `codelite/provider/` without needing to change it.

## License

Apache License 2.0 -- see [LICENSE](LICENSE) and [NOTICE](NOTICE).
