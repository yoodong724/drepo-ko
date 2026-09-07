#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool_root="$repo_root/.tools/godot-v4.6.3"
archive="$repo_root/.tools/Godot_v4.6.3-stable_linux.x86_64.zip"
binary="$tool_root/Godot_v4.6.3-stable_linux.x86_64"
url="https://github.com/godotengine/godot-builds/releases/download/4.6.3-stable/Godot_v4.6.3-stable_linux.x86_64.zip"
expected="d0bc2113065e481c9c2c2b2c37daa4e8be3fe9e27f0ab9ab0b6096e9a37907f3"

mkdir -p "$repo_root/.tools" "$tool_root"
if [[ ! -f "$archive" ]]; then
  curl -fsSL "$url" -o "$archive"
fi

actual="$(sha256sum "$archive" | cut -d' ' -f1)"
if [[ "$actual" != "$expected" ]]; then
  echo "Godot archive hash mismatch: $actual" >&2
  exit 1
fi

unzip -q -o "$archive" -d "$tool_root"
chmod +x "$binary"
version="$($binary --version)"
if [[ "$version" != 4.6.3.stable.official.* ]]; then
  echo "unexpected Godot version: $version" >&2
  exit 1
fi

echo "$binary"
