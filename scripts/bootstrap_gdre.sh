#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool_root="$repo_root/.tools/gdre-v2.6.4"
archive="$repo_root/.tools/GDRE_tools-v2.6.4-linux.zip"
url="https://github.com/GDRETools/gdsdecomp/releases/download/v2.6.4/GDRE_tools-v2.6.4-linux.zip"
expected="eda8cb09e64a060728fa371aa80ae148d3c5584a7de2f553699936daa84e7b4e"

mkdir -p "$repo_root/.tools" "$tool_root"
if [[ ! -f "$archive" ]]; then
  curl -fsSL "$url" -o "$archive"
fi

actual="$(sha256sum "$archive" | cut -d' ' -f1)"
if [[ "$actual" != "$expected" ]]; then
  echo "GDRETools archive hash mismatch: $actual" >&2
  exit 1
fi

unzip -q -o "$archive" -d "$tool_root"
chmod +x "$tool_root/gdre_tools.x86_64"
echo "$tool_root/gdre_tools.x86_64"
