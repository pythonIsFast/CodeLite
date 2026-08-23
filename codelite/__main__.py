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

"""``python3 -m codelite`` -- launch the Code Lite desktop app.

``python3 -m codelite.provider`` remains the way to run just the raw
OpenAI-compatible proxy, without the agent or the window.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .app.window import run
from .config import DEFAULT_HOST, DEFAULT_PORT, AppConfig
from .permission.modes import Mode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m codelite",
        description="Code Lite -- a lightweight coding agent on your ChatGPT subscription.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})"
    )
    parser.add_argument("--model", default=None, help="Default model for new conversations.")
    parser.add_argument(
        "--mode",
        default=None,
        choices=[mode.value for mode in Mode],
        help="Default permission mode for new conversations.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="Where to keep the SQLite database."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Serve the app without opening a window (open the URL yourself).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig(host=args.host, port=args.port)
    if args.model:
        config.agent_model = args.model
    if args.mode:
        config.default_permission_mode = Mode(args.mode)
    if args.data_dir:
        config.data_dir = Path(args.data_dir)

    try:
        run(config, headless=args.headless)
    except KeyboardInterrupt:
        return 0
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
