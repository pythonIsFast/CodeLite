#!/usr/bin/env bash
# Build the Code Lite zipapp: one executable file, run by the system Python.
#
# Usage: packaging/build-pyz.sh <output-path>
#
# Everything Code Lite needs on Linux is pure Python (Flask, pywebview and
# their dependencies), so the app can be bundled without freezing an
# interpreter. The native window still comes from the system's WebKitGTK via
# the distribution's own PyGObject -- which a frozen build could not use,
# because that binding is compiled against the system interpreter.
set -euo pipefail

output="${1:?usage: build-pyz.sh <output-path>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp -r "$root/codelite" "$stage/codelite"
# git ignores these, `cp -r` does not. A virtualenv someone created inside the
# package directory is 25 MB of another interpreter, and it would ride along
# silently -- the archive still works, so nothing would flag it.
find "$stage/codelite" -maxdepth 2 \
    \( -name '__pycache__' -o -name 'venv' -o -name '.venv' -o -name '*.egg-info' \) \
    -type d -prune -exec rm -rf {} +
# Any remaining bytecode is dead weight pinned to the interpreter that made it.
find "$stage/codelite" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$stage/codelite" -name '*.pyc' -delete

# --no-compile for the same reason: .pyc files inside the archive would only be
# valid for the build machine's Python version.
python3 -m pip install --quiet --no-compile --target "$stage" \
    -r "$root/requirements.txt"

# The *.dist-info directories have to stay. Werkzeug asks
# importlib.metadata for its own version while building a server, so deleting
# them as dead weight makes the app import fine and then fail to serve.
# Console-script stubs are genuinely unreachable from a zipapp, so those go.
rm -rf "$stage/bin"

cat > "$stage/__main__.py" <<'PY'
"""Zipapp entry point: hand straight over to the normal CLI."""
import sys

from codelite.__main__ import main

sys.exit(main(sys.argv[1:]))
PY

mkdir -p "$(dirname "$output")"
python3 -m zipapp "$stage" \
    --output "$output" \
    --python "/usr/bin/env python3" \
    --compress
chmod +x "$output"

echo "built $output ($(du -h "$output" | cut -f1))"
