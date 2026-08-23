# Code Lite

A lightweight, resource-efficient coding agent — built to stay small on disk
and dependencies while doing the job of heavier tools.

Code Lite runs on your existing **ChatGPT** subscription instead of API
credits, by reusing the OAuth tokens the official
[Codex CLI](https://github.com/openai/codex) already stores at
`~/.codex/auth.json`. It opens in a **native window** (the OS's own webview —
no bundled Chromium, no Electron), talks to your files and shell through a
small set of tools, and gates risky actions behind a permission system you
control.

Two layers, kept deliberately separate:

| Layer | What it is | Dependencies |
|---|---|---|
| `codelite.provider` | ChatGPT-OAuth → OpenAI-compatible API (auth, transport, image handling, optional local proxy) | **none** — Python standard library only |
| `codelite.agent` + `codelite.app` | The agent loop, tools, permissions, persistence, and the desktop window | Flask + pywebview |

The provider layer is a from-scratch Python reimplementation of the ideas in
[EvanZhouDev/openai-oauth](https://github.com/EvanZhouDev/openai-oauth)
(TypeScript/Bun); the agent loop's structure took inspiration from
[sst/opencode](https://github.com/sst/opencode). See [NOTICE](NOTICE) for
attribution. All three projects are Apache-2.0.

## Running it

```bash
pip install -r requirements.txt
python3 -m codelite
```

You need ChatGPT/Codex OAuth tokens already on disk — run `codex login` once
with the official Codex CLI if you haven't. Code Lite refreshes those tokens
itself but does not implement the interactive login flow.

Useful flags:

```bash
python3 -m codelite --mode permit_writes --model gpt-5.6-sol
```

```bash
python3 -m codelite --headless
```

`--headless` serves the UI without opening a window, for when you'd rather
use your own browser. To run *only* the raw OpenAI-compatible proxy, with no
agent and no dependencies at all:

```bash
python3 -m codelite.provider
```

## Permission modes

Reads are never gated — they can't break anything. What changes per mode is
how **file writes** and **shell commands** are handled:

| Mode | File writes | Shell commands |
|---|---|---|
| `ask` | you confirm each one | you confirm each one |
| `permit_writes` | run automatically | you confirm each one |
| `auto` | run automatically | a second model reviews each one |
| `bypass` | run automatically | run automatically |

In **`auto`** mode, a separate judge model (`gpt-5.6-luna` by default) sees
both the shell command *and* the task you actually asked for — a command can
look harmless on its own while being unrelated to the job. If the judge
blocks a command, that's not a dead end: it explains why, the agent is told
the reason, and the question is escalated to you with the judge's reasoning
shown, so you make the final call.

You can switch modes mid-conversation from the window's header, and grant
"allow for the rest of this session" from any prompt.

## Tools

`read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `find_files`,
`shell`.

Search is implemented in pure Python rather than shelling out to
`grep`/`ripgrep`, so it needs no permission prompt and behaves the same on
every machine. Every path the model supplies is resolved against the
conversation's workspace and rejected if it escapes it.

## What works

- **Token refresh** — reads `~/.codex/auth.json` (or `$CODEX_HOME`), refreshes
  the access token when it nears expiry, writes it straight back.
- **Streaming agent loop** — model turn → tool calls → results → repeat, with
  text, tool calls and results streaming into the window live.
- **Four permission modes**, including the judge-model path described above.
- **Persistence** — conversations and full history in SQLite, so restarting
  the app doesn't lose anything.
- **OpenAI-compatible endpoints** via the provider layer:
  `/v1/chat/completions`, `/v1/responses`, `/v1/models`,
  `/v1/images/generations`, `/v1/images/edits`.

## Known limitations

- **No interactive login.** Only token *refresh* is implemented; sign in with
  `codex login` first.
- **Shell commands are one-shot.** Each `shell` call is a fresh process, so
  `cd` doesn't persist between calls — chain with `&&` when order matters.
- **"Allow for session" is coarse.** It grants the whole category (all writes,
  or all shell commands) for the rest of the conversation, not a specific
  path or command pattern.
- **Chat Completions streaming translation is unverified against a live
  stream.** The provider's `/v1/chat/completions` path exists for external
  OpenAI-client compatibility; its non-streaming path is solid, but the
  streaming event mapping was written from the public Responses API docs and
  tested only against hand-written fixtures. The agent itself doesn't use
  this path — it speaks the Responses API directly.
- **pywebview needs a system webview backend** (WebKitGTK on Linux, usually
  already present; WebView2 on Windows). On a minimal system this may need a
  one-time system package.
- **On reload, a tool card's success/failure is inferred** from its stored
  output text rather than a persisted flag — cosmetic only.
- Localhost only, no authentication: it's the private back end of a desktop
  window, not a service to expose.

## Project layout

```
codelite/
  provider/       stdlib-only: OAuth, Codex transport, SSE, images, proxy
  agent/          loop.py, system_prompt.py, judge.py
  tools/          base, context, files, search, shell, registry
  permission/     modes.py (the four modes), manager.py (the gate)
  db/             SQLite store; history is stored as Responses-API items
  app/            runtime.py (live state), server.py (Flask), window.py,
                  static/ + templates/ (vanilla HTML/JS/CSS, no build step)
  config.py       app-wide defaults
  __main__.py     python3 -m codelite
```

History is persisted in the same shape the model consumes (Responses API
input items), so there's no translation layer between storage, the model
call, and the UI.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
