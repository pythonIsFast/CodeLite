# Code Lite

A lightweight, resource-efficient coding agent — built to stay small on disk
and dependencies while doing the job of heavier tools.

Code Lite runs on your existing **ChatGPT** subscription instead of API
credits. It signs you in directly and stores its own OAuth tokens separately
from Codex. It opens in a **native window** (the OS's own webview —
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
```

```bash
python3 run.py
```

`run.py` works from any directory. (`python3 -m codelite` does the same thing,
but only from the project root.)

On first launch, choose **Sign in with ChatGPT**. Code Lite opens the ChatGPT
login in your browser, receives the local OAuth callback, and stores the tokens
in its own data directory (`~/.local/share/codelite/auth.json` by default).
It never reads or overwrites the Codex CLI's auth file. You can switch accounts
later from Settings.

Useful flags:

```bash
python3 run.py --mode permit_writes --model gpt-5.6-sol
```

```bash
python3 run.py --headless
```

The model picker also offers **Auto**. Before each message,
`gpt-5.6-luna` routes with low reasoning to the lowest capable model: Auto for
focused routine work, Terra for normal multi-file work and debugging, and Sol
only for genuinely complex or high-risk tasks. Code Lite shows Auto's decision
and the role and limits of all three models in the chat before the main agent
starts. If routing is unavailable, it falls back to Terra rather than Sol.

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
`code_intelligence`, `extensions`, `project_memory`, `generate_image`,
`showcase_file`, `view_image`, `web_search`, `web_fetch`, `shell`,
`todo_write`, plus project-local tools.

Search is implemented in pure Python rather than shelling out to
`grep`/`ripgrep`, so it needs no permission prompt and behaves the same on
every machine. Every path the model supplies is resolved against the
conversation's workspace and rejected if it escapes it.

`todo_write` is how the agent records a multi-step plan; the list is rendered
in the conversation, so "what is it doing and how far along is it" has a real
answer instead of a spinner.

`generate_image` uses the current ChatGPT sign-in to create an image. The
agent supplies a prompt and a workspace-relative output path; saving it uses
the same file-write permission policy as other generated files.

`showcase_file` embeds a workspace file in the chat (with native previews for
images, video, and audio). `view_image` supplies the actual image pixels to
the agent for one turn without persisting or repeatedly resending the data.

`web_search` uses Bing's public HTML results and `web_fetch` converts public
HTML, text, or JSON pages into bounded text. Both use only Python's standard
library. Downloads are capped at 2 MB, tool output is capped, and private,
local, credential-bearing, and reserved network destinations are rejected.

### Local tools and plugin hooks

Project-local Python extensions live in `.codelite/tools/*.py` or
`.codelite/plugins/*.py`. Code Lite reads only literal `TOOL` and `HOOKS`
metadata during discovery. The module itself is imported only when its tool or
hook is first needed, and that import goes through the shell-command permission
policy because local extension code has the same power as any Python program.

```python
TOOL = {
    "name": "hello",
    "description": "Return a project-specific greeting.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    },
}

def run(arguments, context):
    return f"Hello {arguments['name']}"
```

Plugins declare `HOOKS = ["before_tool", "after_tool"]` and implement
`before_tool(name, arguments, context)` and/or
`after_tool(name, arguments, output, context)`. A before hook may return a
replacement argument dictionary; an after hook may return replacement output.
Returning `None` leaves the value unchanged. Extensions are limited to 32
files per project and cannot replace built-in tool names.

## Project intelligence

Code Lite keeps these features file-based, bounded, and lazy so they add
almost nothing to startup time, install size, or token usage:

- **Global memory** lives in the Code Lite data directory's `memory.md`, is
  limited to 2,500 characters, and enters every project. Use it for personal
  preferences and conventions that should apply everywhere.
- **Project memory** lives in `.codelite/memory.md`, has the same limit, and is
  shared by every chat in that workspace. Use it for project-specific commands,
  conventions, and architecture decisions. The `project_memory` tool accepts a
  `global` or `project` scope.
- **Repository instructions** are automatically collected from `AGENTS.md` and
  `CLAUDE.md` before each run, including applicable nested folders. Generated
  directories such as `node_modules` are skipped, and the combined text is
  bounded to protect startup time and token usage.
- **Skills** are Markdown files at `.codelite/skills/*.md` or
  `.codelite/skills/<name>/SKILL.md`. User-wide skills can use the same layout
  under the Code Lite data directory's `skills/` folder. Only names and short
  descriptions enter the system prompt; full instructions load on demand.
- **MCP servers** are configured in `.codelite/mcp.json` and start only when
  the agent lists or calls one of their tools. One stdio process is then reused.
- **LSP intelligence** automatically uses installed `pyright-langserver`,
  `typescript-language-server`, `rust-analyzer`, `gopls`, or `clangd`.
  Custom servers go in `.codelite/lsp.json` and start only on first use.

Example MCP configuration:

```json
{
  "mcpServers": {
    "example": {
      "command": "example-mcp-server",
      "args": ["--stdio"]
    }
  }
}
```

Example custom LSP configuration:

```json
{
  "servers": {
    "custom-python": {
      "command": ".venv/bin/pyright-langserver",
      "args": ["--stdio"],
      "extensions": [".py"],
      "languageId": "python"
    }
  }
}
```

The tabbed Settings panel separates the ChatGPT account, global memory, and
project intelligence. Global memory is editable without an active project;
project memory, MCP, and LSP configuration appear in the Project tab. Starting
an external MCP/LSP process goes through the existing command permission policy.

## What works

- **Token refresh** — reads Code Lite's own `auth.json` (under
  `$CODELITE_HOME`, `$XDG_DATA_HOME/codelite`, or the default data directory),
  refreshes the access token when it nears expiry, and writes it straight back.
- **Streaming agent loop** — model turn → tool calls → results → repeat, with
  text, tool calls and results streaming into the window live. There is no
  turn limit: a run ends when the model stops calling tools, when you stop it,
  or when the context window is nearly full.
- **Four permission modes**, including the judge-model path described above.
  Write approvals show a unified diff of the pending change, not just a path.
- **Persistence** — conversations and full history in SQLite, so restarting
  the app doesn't lose anything. Token usage is stored per conversation, so
  the context ring reflects the chat's real size the moment you open it.
- **Automatic context compaction** — when a chat approaches its input budget,
  Code Lite replaces older working context with a self-contained summary and
  keeps the newest exchanges intact. The full original transcript remains
  visible and stored locally.
- **Manual context compaction** — the `Compact` button beside the permission
  mode frees working context on demand while preserving the complete visible
  transcript.
- **Lazy project intelligence** — bounded repository instructions and project
  memory, on-demand skills, optional MCP stdio tools, and persistent
  language-server intelligence without adding Python dependencies.
- **Plan usage** — how much of your weekly ChatGPT allowance is gone, read
  from the `x-codex-*` response headers Codex attaches to every `/responses`
  call. There is no endpoint for this, so the figure only refreshes when a
  request goes out; the last one is cached in SQLite to survive a restart.
- **OpenAI-compatible endpoints** via the provider layer:
  `/v1/chat/completions`, `/v1/responses`, `/v1/models`,
  `/v1/images/generations`, `/v1/images/edits`.

## Known limitations

- **Shell commands are separate processes.** The working directory carries
  over between calls, but nothing else does — no environment variables, no
  shell functions, no background jobs.
- **Context-window sizes come from Codex's own catalog** (`context_window`
  per model). `codelite/config.py` carries a static table as an offline
  fallback only, so the percentage is real unless the catalog is unreachable.
- **Session allowances are narrow.** A write allowance applies only to the
  displayed directory; a shell allowance applies only to the exact command in
  its current working directory. A different path or command asks again.
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
run.py            start the app from anywhere
codelite/
  provider/       stdlib-only: OAuth, Codex transport, SSE, images, limits, proxy
  agent/          loop.py, system_prompt.py, judge.py
  tools/          built-in tools plus memory, extensions, and code intelligence
  integrations/   dependency-free lazy MCP and LSP stdio clients
  project/        bounded project memory and skill discovery
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
