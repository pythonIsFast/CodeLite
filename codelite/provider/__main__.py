# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI entry point: ``python -m codelite.provider``.

Starts the local OpenAI-compatible proxy in the foreground. Existing
ChatGPT/Codex OAuth tokens at ``~/.codex/auth.json`` (or ``$CODEX_HOME``)
are required -- run `codex login` first if you haven't already.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, ProviderConfig
from .server import run_server


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codelite.provider",
        description="Local OpenAI-compatible proxy backed by a ChatGPT Plus OAuth session.",
    )
    parser.add_argument("--host", default=DEFAULT_SERVER_HOST, help=f"Bind host (default: {DEFAULT_SERVER_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT, help=f"Bind port (default: {DEFAULT_SERVER_PORT})")
    parser.add_argument("--auth-file", type=Path, default=None, help="Path to auth.json (default: ~/.codex/auth.json)")
    parser.add_argument("--codex-version", default=None, help="Override the Codex client version used for model discovery.")
    parser.add_argument("--instructions", default="", help="Default system instructions sent with every /responses request.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = ProviderConfig(
        codex_client_version=args.codex_version,
        instructions=args.instructions,
    )
    if args.auth_file is not None:
        config.auth_file_path = args.auth_file

    try:
        run_server(config, host=args.host, port=args.port)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
