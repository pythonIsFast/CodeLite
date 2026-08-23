#!/usr/bin/env python3
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

"""Start Code Lite.

    python3 run.py

Works from any working directory -- it puts its own directory on the import
path first, so the ``codelite`` package is always found. All command line
flags from ``python3 -m codelite`` are accepted here too, for example:

    python3 run.py --mode permit_writes
    python3 run.py --headless
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        from codelite.__main__ import main as run_codelite
    except ImportError as error:
        # Almost always a missing dependency rather than a broken checkout,
        # so name the likely fix instead of just re-raising the traceback.
        missing = getattr(error, "name", None) or "a dependency"
        return _fail(
            f"could not import Code Lite -- {missing} is missing.\n"
            f"  Install the app dependencies:  pip install -r "
            f"{PROJECT_ROOT / 'requirements.txt'}\n"
            f"  (details: {error})"
        )
    return run_codelite(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
