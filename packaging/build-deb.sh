#!/usr/bin/env bash
# Build the Code Lite Debian package around the zipapp.
#
# Usage: packaging/build-deb.sh <version> <pyz-path> <output-dir>
#
# The package is `Architecture: all` because it contains no compiled code: the
# app is pure Python and the window comes from the distribution's WebKitGTK.
# Installing a newer .deb upgrades an existing install in place.
#
# The binary is called `code-lite`, not `codelite`, on purpose: Debian and
# Ubuntu already ship an unrelated C++ IDE under the name `codelite`, and this
# package must not collide with it.
set -euo pipefail

version="${1:?usage: build-deb.sh <version> <pyz> <outdir>}"
pyz="${2:?missing pyz path}"
outdir="${3:?missing output dir}"

root="$(cd "$(dirname "$0")/.." && pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

install -Dm755 "$pyz" "$stage/opt/code-lite/code-lite.pyz"
install -Dm755 "$root/packaging/linux/code-lite" "$stage/usr/bin/code-lite"
install -Dm644 "$root/packaging/linux/code-lite.desktop" \
    "$stage/usr/share/applications/code-lite.desktop"
install -Dm644 "$root/codelite/app/static/icon.png" \
    "$stage/usr/share/icons/hicolor/256x256/apps/code-lite.png"
install -Dm644 "$root/LICENSE" "$stage/usr/share/doc/code-lite/copyright"
install -Dm644 "$root/NOTICE" "$stage/usr/share/doc/code-lite/NOTICE"

# The typelib package is `gir1.2-webkit2-4.1`, not `gir1.2-webkit2gtk-4.1`:
# the *library* is libwebkit2gtk, but the introspection data drops the "gtk".
# Getting this wrong makes the package uninstallable with a confusing "is but
# not installable" message, so it is spelled out here rather than guessed.
# 4.0 stays as an alternative for older releases; pywebview asks for WebKit2
# 4.1 first and falls back to 4.0.
mkdir -p "$stage/DEBIAN"
cat > "$stage/DEBIAN/control" <<EOF
Package: code-lite
Version: $version
Section: devel
Priority: optional
Architecture: all
Maintainer: pythonIsFast <pythonIsFast@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0
Homepage: https://github.com/pythonIsFast/CodeLite
Description: Lightweight coding agent for your ChatGPT subscription
 Code Lite is a small coding agent that opens in a native window and works on
 your files and shell through a permission system you control. It runs on an
 existing ChatGPT subscription rather than API credits.
 .
 The window is drawn by the system's own WebKitGTK engine, so nothing like
 Electron is bundled and the install stays in the low single-digit megabytes.
EOF

package="$outdir/code-lite_${version}_all.deb"
mkdir -p "$outdir"
# --root-owner-group avoids baking the CI runner's uid into the archive.
dpkg-deb --build --root-owner-group "$stage" "$package"

echo "built $package ($(du -h "$package" | cut -f1))"
