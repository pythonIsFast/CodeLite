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
# Stale bytecode from a local run would otherwise be shipped, and it is both
# dead weight and pinned to whatever interpreter produced it.
find "$stage/codelite" -name '__pycache__' -type d -prune -exec rm -rf {} +

# --no-compile for the same reason: .pyc files inside the archive would only be
# valid for the build machine's Python version.
python3 -m pip install --quiet --no-compile --target "$stage" \
    -r "$root/requirements.txt"

# pip leaves metadata and console-script stubs that nothing in a zipapp reads.
find "$stage" -maxdepth 1 -name '*.dist-info' -type d -prune -exec rm -rf {} +
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
