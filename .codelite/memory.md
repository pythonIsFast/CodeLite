# Code Lite project memory

- Optimize for fast startup, low idle overhead, and responsive local execution.
- Keep model context and ChatGPT usage small; prefer bounded context, lazy discovery, and concise tool results.
- Keep the installed application small. Prefer the Python standard library and optional external tools over bundled runtimes or large dependencies.
- New integrations should start only when used and reuse long-running processes when that improves performance.
