#!/usr/bin/env bash
# Links external programs into tools/. It only creates symlinks: installations are never
# copied or moved. A missing tool is a warning, because each one is optional.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/tools"

link_tool() {
  local name="$1"
  shift

  local target=""
  local candidate
  for candidate in "$@"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      target="$candidate"
      break
    fi
  done

  if [ -z "$target" ]; then
    echo "missing: $name (not found, features that need it stay unavailable)"
    return 0
  fi

  local link="$TOOLS_DIR/$name"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "error: $link is not a symlink. Refusing to replace it." >&2
    return 1
  fi

  ln -sfn "$target" "$link"
  echo "linked: tools/$name -> $target"
}

link_tool unrar \
  "${UNRAR_BIN:-}" \
  "$(command -v unrar || true)"

link_tool 7zz \
  "${SEVEN_ZIP_BIN:-}" \
  "$(command -v 7zz || true)" \
  "$(command -v 7z || true)" \
  "$(command -v 7za || true)"

link_tool blender \
  "${BLENDER_BIN:-}" \
  "$(command -v blender || true)" \
  "/Applications/Blender.app/Contents/MacOS/Blender" \
  /Applications/Blender*.app/Contents/MacOS/Blender
