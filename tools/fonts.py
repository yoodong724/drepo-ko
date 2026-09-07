"""Deterministic Korean font importer and generated UI font-role patcher.

This module never edits the supplied game, recovered sources, or canonical
localization data.  It imports pinned, licensed TTF inputs in a disposable
Godot project and publishes only the runtime remap stubs, ``.fontdata``
objects, license, patched *copy* of ``ui_kit.gd``, and a hash manifest into a
new, explicit generated output directory.

The supported inputs are intentionally narrow.  A different Godot binary,
font release, import mapping, or recovered UI assignment fails closed and
must be reviewed as a deliberate pipeline update.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Callable, Mapping, Sequence


GODOT_VERSION = "4.6.3.stable.official.7d41c59c4"
GODOT_BINARY_SHA256 = "f64d4ed19fc9df9440321653fcc80df8c6e365ba7b6de0a29e2cfa9fa71bfeb3"
HANGUL_FIRST = 0xAC00
HANGUL_LAST = 0xD7A3
HANGUL_SYLLABLE_COUNT = HANGUL_LAST - HANGUL_FIRST + 1
FONTDATA_MAGIC = b"RSCC"
RUNTIME_FONT_DIR = PurePosixPath("assets/fonts/ko")
LICENSE_OUTPUT = PurePosixPath("licenses/fonts/galmuri/OFL-1.1.txt")
MANIFEST_OUTPUT = PurePosixPath("font_manifest.json")
UI_OUTPUT = PurePosixPath("scripts/ui/ui_kit.gd")


class FontBuildError(ValueError):
    """A pinned input, import artifact, or generated output is unsafe."""


@dataclass(frozen=True, slots=True)
class FontSpec:
    filename: str
    source_sha256: str
    uid: str
    imported_filename: str
    fontdata_sha256: str
    roles: tuple[str, ...]

    @property
    def runtime_source(self) -> str:
        return f"res://{RUNTIME_FONT_DIR.as_posix()}/{self.filename}"

    @property
    def runtime_import(self) -> PurePosixPath:
        return RUNTIME_FONT_DIR / f"{self.filename}.import"

    @property
    def runtime_fontdata(self) -> PurePosixPath:
        return PurePosixPath(".godot/imported") / self.imported_filename


FONT_SPECS: tuple[FontSpec, ...] = (
    FontSpec(
        "Galmuri9.ttf",
        "e84e821b18be15b9e3a907ceb83cfba25fabf51c80b7edf0d2921cf8f8e1a11d",
        "uid://dee6k0ux0040g",
        "Galmuri9.ttf-69ce1261dd2d5d89681ddfe6fe1e6d47.fontdata",
        "dd1129e61ec156baf6b6c3f189ce0dc1eb2e602731fd9cdb5bd904b3f7313665",
        ("small",),
    ),
    FontSpec(
        "Galmuri11.ttf",
        "e24256f42e43713d2ea086a1e1669d78b968f5b3cc547e5c157f0606ffa5def1",
        "uid://dkajk6l7xit8o",
        "Galmuri11.ttf-d198ff4153c04770e112c11ee6e15a2d.fontdata",
        "5d93e46e6f76e463deccba20a9d68ed8462aedda3fa661f6ed0abc6971c534b9",
        ("body", "record", "system", "mincho_fallback"),
    ),
    FontSpec(
        "Galmuri11-Bold.ttf",
        "45d901e379138dd91873af157640f60aa4cf6beaa885c3efd2a8c279e039a237",
        "uid://85cd5g5b7gso",
        "Galmuri11-Bold.ttf-0ae5814d224cfd935618e1dc1b82c434.fontdata",
        "670ad3f56366f2d1a847df8b9fb18eec19c632e778b11eae319924eaacdc76a3",
        ("bold", "small_bold", "record_bold", "system_bold"),
    ),
    FontSpec(
        "Galmuri14.ttf",
        "6fe6c3fe4369e3837ac348431e8670733d67aa4bd550982baa72cc93c81a1c68",
        "uid://b4wvpvufcnl7k",
        "Galmuri14.ttf-af42a8f28c845d820a11bc5bc31f6890.fontdata",
        "ac9794b0ea3e1f24e0ada9db199fb8e008a715c419e60ec4af219918c7802fe4",
        ("heading", "title"),
    ),
    FontSpec(
        "Galmuri11-Condensed.ttf",
        "613d5de93abdbfca203be1bb05c756d792ba8c141ae831607feeba62279d07b9",
        "uid://ft8lsca31dtc",
        "Galmuri11-Condensed.ttf-208a6eaed2f3a25f0feac77fefc49b62.fontdata",
        "3b44ba5c0791269b96c389c79e1d8cd0dad14da566f06a4d01ada272b5891f15",
        ("overflow_optional_unassigned",),
    ),
)

GALMURI_LICENSE_SHA256 = "86a3ee9495f942f0243f18c103da9faca27adb88142613edb8bb852e56c892c1"

_PROJECT_GODOT = """; Disposable localization font import project.\nconfig_version=5\n\n[application]\n\nconfig/name=\"DGR Korean Font Import\"\nconfig/features=PackedStringArray(\"4.6\", \"Forward Plus\")\n"""

_REQUIRED_IMPORT_VALUES: Mapping[str, str] = {
    "importer": '"font_data_dynamic"',
    "type": '"FontFile"',
    "antialiasing": "1",
    "generate_mipmaps": "false",
    "disable_embedded_bitmaps": "true",
    "multichannel_signed_distance_field": "false",
    "allow_system_fallback": "true",
    "force_autohinter": "false",
    "hinting": "1",
    "subpixel_positioning": "0",
    "compress": "true",
}


def _full_import_metadata(spec: FontSpec) -> str:
    """Return the reviewed Godot 4.6.3 dynamic-font import configuration."""

    return f'''[remap]\n\nimporter="font_data_dynamic"\ntype="FontFile"\nuid="{spec.uid}"\npath="res://.godot/imported/{spec.imported_filename}"\n\n[deps]\n\nsource_file="{spec.runtime_source}"\ndest_files=["res://.godot/imported/{spec.imported_filename}"]\n\n[params]\n\nRendering=null\nantialiasing=1\ngenerate_mipmaps=false\ndisable_embedded_bitmaps=true\nmultichannel_signed_distance_field=false\nmsdf_pixel_range=8\nmsdf_size=48\nallow_system_fallback=true\nforce_autohinter=false\nmodulate_color_glyphs=false\nhinting=1\nsubpixel_positioning=0\nkeep_rounding_remainders=true\noversampling=0.0\nFallbacks=null\nfallbacks=[]\nCompress=null\ncompress=true\npreload=[]\nlanguage_support={{}}\nscript_support={{}}\nopentype_features={{}}\n'''


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _regular_pinned_file(path: Path, expected_sha256: str, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise FontBuildError(f"{label} must be a regular, non-symlink file: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise FontBuildError(
            f"{label} SHA-256 mismatch: expected {expected_sha256}, got {actual}"
        )


def _read_u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise FontBuildError("truncated TTF cmap")
    return struct.unpack_from(">H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise FontBuildError("truncated TTF cmap")
    return struct.unpack_from(">I", data, offset)[0]


def _cmap_subtables(font: bytes) -> tuple[bytes, ...]:
    if len(font) < 12 or font[:4] not in (b"\x00\x01\x00\x00", b"true", b"typ1"):
        raise FontBuildError("input is not a supported TrueType sfnt font")
    table_count = _read_u16(font, 4)
    directory_end = 12 + table_count * 16
    if directory_end > len(font):
        raise FontBuildError("truncated TTF table directory")
    cmap: bytes | None = None
    for index in range(table_count):
        base = 12 + index * 16
        tag = font[base : base + 4]
        offset = _read_u32(font, base + 8)
        length = _read_u32(font, base + 12)
        if offset > len(font) or length > len(font) - offset:
            raise FontBuildError(f"TTF table {tag!r} exceeds input bounds")
        if tag == b"cmap":
            if cmap is not None:
                raise FontBuildError("duplicate TTF cmap table")
            cmap = font[offset : offset + length]
    if cmap is None or len(cmap) < 4:
        raise FontBuildError("TTF has no valid cmap table")
    count = _read_u16(cmap, 2)
    if 4 + count * 8 > len(cmap):
        raise FontBuildError("truncated TTF cmap directory")
    result: list[bytes] = []
    seen: set[int] = set()
    for index in range(count):
        sub_offset = _read_u32(cmap, 4 + index * 8 + 4)
        if sub_offset in seen:
            continue
        seen.add(sub_offset)
        if sub_offset + 4 > len(cmap):
            raise FontBuildError("TTF cmap subtable offset exceeds input bounds")
        fmt = _read_u16(cmap, sub_offset)
        if fmt in (12, 13):
            length = _read_u32(cmap, sub_offset + 4)
        else:
            length = _read_u16(cmap, sub_offset + 2)
        if length < 4 or sub_offset + length > len(cmap):
            raise FontBuildError("TTF cmap subtable exceeds input bounds")
        result.append(cmap[sub_offset : sub_offset + length])
    return tuple(result)


def _format4_has(subtable: bytes, codepoint: int) -> bool:
    if len(subtable) < 16:
        raise FontBuildError("truncated cmap format 4 subtable")
    seg_count = _read_u16(subtable, 6) // 2
    if seg_count == 0:
        raise FontBuildError("empty cmap format 4 subtable")
    end_base = 14
    start_base = end_base + seg_count * 2 + 2
    delta_base = start_base + seg_count * 2
    range_base = delta_base + seg_count * 2
    if range_base + seg_count * 2 > len(subtable):
        raise FontBuildError("truncated cmap format 4 arrays")
    for index in range(seg_count):
        end = _read_u16(subtable, end_base + index * 2)
        if codepoint > end:
            continue
        start = _read_u16(subtable, start_base + index * 2)
        if codepoint < start:
            return False
        delta = _read_u16(subtable, delta_base + index * 2)
        word_offset = range_base + index * 2
        glyph_range = _read_u16(subtable, word_offset)
        if glyph_range == 0:
            return ((codepoint + delta) & 0xFFFF) != 0
        glyph_offset = word_offset + glyph_range + (codepoint - start) * 2
        glyph = _read_u16(subtable, glyph_offset)
        return glyph != 0 and ((glyph + delta) & 0xFFFF) != 0
    return False


def _format12_or_13_has(subtable: bytes, codepoint: int) -> bool:
    if len(subtable) < 16:
        raise FontBuildError("truncated cmap format 12/13 subtable")
    fmt = _read_u16(subtable, 0)
    count = _read_u32(subtable, 12)
    if 16 + count * 12 > len(subtable):
        raise FontBuildError("truncated cmap format 12/13 groups")
    for index in range(count):
        base = 16 + index * 12
        start = _read_u32(subtable, base)
        end = _read_u32(subtable, base + 4)
        if codepoint < start:
            return False
        if codepoint <= end:
            glyph = _read_u32(subtable, base + 8)
            if fmt == 12:
                glyph += codepoint - start
            return glyph != 0
    return False


def _subtables_have_codepoint(subtables: Sequence[bytes], codepoint: int) -> bool:
    for subtable in subtables:
        fmt = _read_u16(subtable, 0)
        if fmt == 4 and codepoint <= 0xFFFF and _format4_has(subtable, codepoint):
            return True
        if fmt in (12, 13) and _format12_or_13_has(subtable, codepoint):
            return True
    return False


def font_has_codepoint(font: bytes, codepoint: int) -> bool:
    """Return whether a supported Unicode cmap maps ``codepoint`` to a glyph."""

    return _subtables_have_codepoint(_cmap_subtables(font), codepoint)


def _coverage_in_range(subtables: Sequence[bytes], first: int, last: int) -> int:
    covered = bytearray(last - first + 1)
    for subtable in subtables:
        fmt = _read_u16(subtable, 0)
        if fmt == 4:
            if len(subtable) < 16:
                raise FontBuildError("truncated cmap format 4 subtable")
            seg_count = _read_u16(subtable, 6) // 2
            end_base = 14
            start_base = end_base + seg_count * 2 + 2
            delta_base = start_base + seg_count * 2
            range_base = delta_base + seg_count * 2
            if range_base + seg_count * 2 > len(subtable):
                raise FontBuildError("truncated cmap format 4 arrays")
            for index in range(seg_count):
                start = max(first, _read_u16(subtable, start_base + index * 2))
                end = min(last, _read_u16(subtable, end_base + index * 2))
                if start > end:
                    continue
                delta = _read_u16(subtable, delta_base + index * 2)
                word_offset = range_base + index * 2
                glyph_range = _read_u16(subtable, word_offset)
                segment_start = _read_u16(subtable, start_base + index * 2)
                for codepoint in range(start, end + 1):
                    if glyph_range == 0:
                        present = ((codepoint + delta) & 0xFFFF) != 0
                    else:
                        glyph_offset = (
                            word_offset + glyph_range + (codepoint - segment_start) * 2
                        )
                        glyph = _read_u16(subtable, glyph_offset)
                        present = glyph != 0 and ((glyph + delta) & 0xFFFF) != 0
                    if present:
                        covered[codepoint - first] = 1
        elif fmt in (12, 13):
            if len(subtable) < 16:
                raise FontBuildError("truncated cmap format 12/13 subtable")
            count = _read_u32(subtable, 12)
            if 16 + count * 12 > len(subtable):
                raise FontBuildError("truncated cmap format 12/13 groups")
            for index in range(count):
                base = 16 + index * 12
                group_start = _read_u32(subtable, base)
                group_end = _read_u32(subtable, base + 4)
                start = max(first, group_start)
                end = min(last, group_end)
                if start > end:
                    continue
                first_glyph = _read_u32(subtable, base + 8)
                for codepoint in range(start, end + 1):
                    glyph = first_glyph
                    if fmt == 12:
                        glyph += codepoint - group_start
                    if glyph != 0:
                        covered[codepoint - first] = 1
    return sum(covered)


def hangul_coverage(path: Path) -> int:
    subtables = _cmap_subtables(path.read_bytes())
    return _coverage_in_range(subtables, HANGUL_FIRST, HANGUL_LAST)


def _parse_import(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            if name in sections:
                raise FontBuildError(f"duplicate import section: {name}")
            current = {}
            sections[name] = current
            continue
        if current is None or "=" not in line:
            raise FontBuildError(f"malformed Godot import line: {line!r}")
        key, value = line.split("=", 1)
        if not key or key in current:
            raise FontBuildError(f"duplicate or empty import key: {key!r}")
        current[key] = value
    return sections


def _validate_and_trim_import(full_import: Path, spec: FontSpec) -> bytes:
    raw = full_import.read_bytes()
    if b"\0" in raw:
        raise FontBuildError(f"Godot import metadata contains an unexpected NUL: {full_import}")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FontBuildError(f"Godot import metadata is not UTF-8: {full_import}") from exc
    if "\r" in text or not text.endswith("\n"):
        raise FontBuildError(f"Godot import metadata must use terminal LF: {full_import}")
    sections = _parse_import(text)
    if set(sections) != {"remap", "deps", "params"}:
        raise FontBuildError(f"unexpected Godot import sections: {sorted(sections)}")
    expected_path = f'"res://.godot/imported/{spec.imported_filename}"'
    remap = sections["remap"]
    expected_remap = {
        "importer": '"font_data_dynamic"',
        "type": '"FontFile"',
        "uid": f'"{spec.uid}"',
        "path": expected_path,
    }
    if remap != expected_remap:
        raise FontBuildError(
            f"unexpected remap for {spec.filename}: expected {expected_remap}, got {remap}"
        )
    deps = sections["deps"]
    expected_deps = {
        "source_file": f'"{spec.runtime_source}"',
        "dest_files": f"[{expected_path}]",
    }
    if deps != expected_deps:
        raise FontBuildError(
            f"unexpected dependencies for {spec.filename}: expected {expected_deps}, got {deps}"
        )
    combined: dict[str, str] = {}
    for values in sections.values():
        combined.update(values)
    for key, value in _REQUIRED_IMPORT_VALUES.items():
        if combined.get(key) != value:
            raise FontBuildError(
                f"unexpected import setting for {spec.filename}: {key}={combined.get(key)!r}"
            )
    marker = "\n[deps]\n"
    if text.count(marker) != 1 or not text.startswith("[remap]\n"):
        raise FontBuildError(f"cannot isolate runtime remap section: {full_import}")
    # Godot exported PCKs retain only this remap prefix and one terminal NUL.
    remap_prefix = text.split(marker, 1)[0]
    if not remap_prefix.endswith("\n") or remap_prefix.endswith("\n\n"):
        raise FontBuildError(f"unexpected remap section terminator: {full_import}")
    return remap_prefix.encode("utf-8") + b"\0"


_UI_REPLACEMENTS: tuple[tuple[bytes, bytes], ...] = tuple(
    (old.encode("utf-8"), new.encode("utf-8"))
    for old, new in (
        ('\t"record": "res://assets/fonts/KH-Dot-Ningyouchou-16.ttf", ', '\t"record": "res://assets/fonts/ko/Galmuri11.ttf", '),
        ('\t"record_bold": "res://assets/fonts/KH-Dot-Dougenzaka-16.ttf", ', '\t"record_bold": "res://assets/fonts/ko/Galmuri11-Bold.ttf", '),
        ('\t"sys": "res://assets/fonts/JF-Dot-Izumi16.ttf", ', '\t"sys": "res://assets/fonts/ko/Galmuri11.ttf", '),
        ('\t"sys_bold": "res://assets/fonts/JF-Dot-Izumi16B.ttf", ', '\t"sys_bold": "res://assets/fonts/ko/Galmuri11-Bold.ttf", '),
        ('\t"record": "res://assets/fonts/KH-Dot-Hibiya-24.ttf", ', '\t"record": "res://assets/fonts/ko/Galmuri11.ttf", '),
        ('\t"record_bold": "res://assets/fonts/KH-Dot-Hibiya-24.ttf", ', '\t"record_bold": "res://assets/fonts/ko/Galmuri11-Bold.ttf", '),
        ('\t"sys": "res://assets/fonts/JF-Dot-Shinonome12.ttf", ', '\t"sys": "res://assets/fonts/ko/Galmuri11.ttf", '),
        ('\t"sys_bold": "res://assets/fonts/JF-Dot-Shinonome12B.ttf", ', '\t"sys_bold": "res://assets/fonts/ko/Galmuri11-Bold.ttf", '),
        ('\tfont_reg = load_pixel_font("res://assets/fonts/PixelMplus12-Regular.ttf")', '\tfont_reg = load_pixel_font("res://assets/fonts/ko/Galmuri11.ttf")'),
        ('\tfont_small = load_pixel_font("res://assets/fonts/PixelMplus10-Regular.ttf")', '\tfont_small = load_pixel_font("res://assets/fonts/ko/Galmuri9.ttf")'),
        ('\tfont_small_bold = load_pixel_font("res://assets/fonts/PixelMplus10-Bold.ttf")', '\tfont_small_bold = load_pixel_font("res://assets/fonts/ko/Galmuri11-Bold.ttf")'),
        ('\tfont_bold = load_pixel_font("res://assets/fonts/PixelMplus12-Bold.ttf")', '\tfont_bold = load_pixel_font("res://assets/fonts/ko/Galmuri11-Bold.ttf")'),
        ('\tfont_title = load_pixel_font("res://assets/fonts/KH-Dot-Hibiya-24.ttf")', '\tfont_title = load_pixel_font("res://assets/fonts/ko/Galmuri14.ttf")'),
        ('\tfont_head = load_pixel_font("res://assets/fonts/KH-Dot-Ningyouchou-16.ttf")', '\tfont_head = load_pixel_font("res://assets/fonts/ko/Galmuri14.ttf")'),
        ('\tfont_mincho = load_pixel_font("res://assets/fonts/wapuro-mincho-tate2x.otf")', '\tfont_mincho = load_pixel_font("res://assets/fonts/ko/Galmuri11.ttf")'),
    )
)


def patch_ui_kit(source: Path, destination: Path) -> dict[str, object]:
    """Patch one generated copy, requiring every known old assignment once."""

    if source.is_symlink() or not source.is_file():
        raise FontBuildError(f"ui_kit source must be a regular, non-symlink file: {source}")
    if destination.exists() or destination.is_symlink():
        raise FontBuildError(f"generated ui_kit destination must not exist: {destination}")
    if source.resolve() == destination.resolve(strict=False):
        raise FontBuildError("ui_kit source and generated destination must differ")
    original = source.read_bytes()
    if original.startswith(b"\xef\xbb\xbf") or b"\r" in original or b"\0" in original:
        raise FontBuildError("ui_kit source must be UTF-8 without BOM/NUL and LF-only")
    try:
        original.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FontBuildError("ui_kit source is not strict UTF-8") from exc
    patched = original
    for old, new in _UI_REPLACEMENTS:
        count = patched.count(old)
        if count != 1:
            raise FontBuildError(
                f"expected one exact ui_kit assignment, found {count}: {old.decode()}"
            )
        patched = patched.replace(old, new, 1)
    old_font_pattern = re.compile(rb'res://assets/fonts/(?!ko/)[^"\r\n]+\.(?:ttf|otf)')
    leftovers = sorted(set(match.group().decode() for match in old_font_pattern.finditer(patched)))
    if leftovers:
        raise FontBuildError(f"unmapped original font assignments remain: {leftovers}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patched)
    return {
        "path": UI_OUTPUT.as_posix(),
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "output_sha256": hashlib.sha256(patched).hexdigest(),
        "replacement_count": len(_UI_REPLACEMENTS),
    }


ImporterRunner = Callable[[Path, Path, Mapping[str, str]], tuple[str, str]]


def _run_godot_import(
    godot_binary: Path,
    project_dir: Path,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                str(godot_binary),
                "--headless",
                "--editor",
                "--path",
                str(project_dir),
                "--import",
                "--quit",
            ],
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FontBuildError(f"Godot font import could not run: {exc}") from exc
    if result.returncode != 0:
        raise FontBuildError(
            f"Godot font import failed with {result.returncode}: {result.stderr[-2000:]}"
        )
    return result.stdout, result.stderr


def _godot_version(godot_binary: Path, environment: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(
            [str(godot_binary), "--version"],
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FontBuildError(f"Godot version check could not run: {exc}") from exc
    if result.returncode != 0:
        raise FontBuildError(f"Godot version check failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


def _prepare_output(output_root: Path, protected_roots: Sequence[Path]) -> None:
    if output_root.exists() or output_root.is_symlink():
        raise FontBuildError(f"generated output must not already exist: {output_root}")
    if not output_root.parent.is_dir():
        raise FontBuildError(f"generated output parent must exist: {output_root.parent}")
    resolved = output_root.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == output_root.parent.resolve():
        raise FontBuildError(f"unsafe generated output path: {output_root}")
    for protected in protected_roots:
        protected_resolved = protected.resolve()
        if _inside(resolved, protected_resolved) or _inside(protected_resolved, resolved):
            raise FontBuildError(
                f"generated output overlaps protected input {protected}: {output_root}"
            )


def build_font_bundle(
    *,
    assets_root: Path,
    recovered_ui_kit: Path,
    godot_binary: Path,
    output_root: Path,
    importer_runner: ImporterRunner | None = None,
    expected_godot_sha256: str = GODOT_BINARY_SHA256,
    expected_godot_version: str = GODOT_VERSION,
) -> dict[str, object]:
    """Build and atomically publish one pinned generated Korean font bundle."""

    assets_root = Path(assets_root)
    recovered_ui_kit = Path(recovered_ui_kit)
    godot_binary = Path(godot_binary)
    output_root = Path(output_root)
    if not assets_root.is_dir() or assets_root.is_symlink():
        raise FontBuildError(f"font assets root must be a real directory: {assets_root}")
    if assets_root.name != "fonts" or assets_root.parent.name != "assets":
        raise FontBuildError(
            f"font assets root must be the project assets/fonts directory: {assets_root}"
        )
    if (
        recovered_ui_kit.name != "ui_kit.gd"
        or recovered_ui_kit.parent.name != "ui"
        or recovered_ui_kit.parent.parent.name != "scripts"
    ):
        raise FontBuildError(
            f"recovered UI source must end in scripts/ui/ui_kit.gd: {recovered_ui_kit}"
        )
    project_root = assets_root.parent.parent
    recovered_root = recovered_ui_kit.parent.parent.parent
    _regular_pinned_file(godot_binary, expected_godot_sha256, "Godot binary")
    _prepare_output(
        output_root,
        (
            assets_root,
            project_root / "drepo",
            project_root / "data" / "localization",
            recovered_root,
            godot_binary,
        ),
    )

    galmuri_root = assets_root / "third_party" / "galmuri"
    license_source = galmuri_root / "LICENSE.txt"
    _regular_pinned_file(license_source, GALMURI_LICENSE_SHA256, "Galmuri OFL")
    coverage: dict[str, int] = {}
    for spec in FONT_SPECS:
        source = galmuri_root / spec.filename
        _regular_pinned_file(source, spec.source_sha256, spec.filename)
        count = hangul_coverage(source)
        if count != HANGUL_SYLLABLE_COUNT:
            raise FontBuildError(
                f"{spec.filename} Hangul coverage is {count}/{HANGUL_SYLLABLE_COUNT}"
            )
        coverage[spec.filename] = count

    runner = importer_runner or _run_godot_import
    with tempfile.TemporaryDirectory(
        prefix=".drepo-font-build-", dir=output_root.parent
    ) as temporary:
        temp_root = Path(temporary)
        project = temp_root / "project"
        project.mkdir()
        (project / "project.godot").write_text(_PROJECT_GODOT, encoding="utf-8", newline="\n")
        project_fonts = project / RUNTIME_FONT_DIR
        project_fonts.mkdir(parents=True)
        for spec in FONT_SPECS:
            shutil.copyfile(galmuri_root / spec.filename, project_fonts / spec.filename)
            (project_fonts / f"{spec.filename}.import").write_text(
                _full_import_metadata(spec), encoding="utf-8", newline="\n"
            )

        xdg_root = temp_root / "xdg"
        environment = os.environ.copy()
        for key, child in (
            ("XDG_DATA_HOME", "data"),
            ("XDG_CONFIG_HOME", "config"),
            ("XDG_CACHE_HOME", "cache"),
            ("XDG_STATE_HOME", "state"),
            ("XDG_RUNTIME_DIR", "runtime"),
        ):
            path = xdg_root / child
            path.mkdir(parents=True)
            if key == "XDG_RUNTIME_DIR":
                path.chmod(0o700)
            environment[key] = str(path)

        if importer_runner is None:
            version = _godot_version(godot_binary, environment)
            if version != expected_godot_version:
                raise FontBuildError(
                    f"Godot version mismatch: expected {expected_godot_version}, got {version}"
                )
        else:
            version = expected_godot_version
        stdout, stderr = runner(godot_binary, project, environment)
        # Headless Godot can emit non-import desktop/socket ``ERROR:`` lines in
        # isolated Linux environments while returning success.  Treat actual
        # importer/script diagnostics as fatal, then require every expected
        # artifact, mapping, magic, and pinned hash below.
        fatal_markers = ("SCRIPT ERROR:", "Failed to import", "Error importing")
        error_lines = [
            line for line in (stdout + "\n" + stderr).splitlines()
            if any(marker in line for marker in fatal_markers)
        ]
        if error_lines:
            raise FontBuildError(f"Godot import reported errors: {error_lines[-5:]}")

        staging = temp_root / "staging"
        staging.mkdir()
        entries: list[dict[str, object]] = []
        for spec in FONT_SPECS:
            full_import = project_fonts / f"{spec.filename}.import"
            fontdata = project / ".godot" / "imported" / spec.imported_filename
            if not full_import.is_file() or full_import.is_symlink():
                raise FontBuildError(f"Godot did not create import metadata: {full_import}")
            if not fontdata.is_file() or fontdata.is_symlink():
                raise FontBuildError(f"Godot did not create fontdata: {fontdata}")
            data = fontdata.read_bytes()
            if not data.startswith(FONTDATA_MAGIC):
                raise FontBuildError(f"invalid fontdata magic for {spec.filename}")
            actual_fontdata_sha = hashlib.sha256(data).hexdigest()
            if actual_fontdata_sha != spec.fontdata_sha256:
                raise FontBuildError(
                    f"fontdata SHA-256 mismatch for {spec.filename}: "
                    f"expected {spec.fontdata_sha256}, got {actual_fontdata_sha}"
                )
            remap = _validate_and_trim_import(full_import, spec)
            remap_output = staging / spec.runtime_import
            remap_output.parent.mkdir(parents=True, exist_ok=True)
            remap_output.write_bytes(remap)
            fontdata_output = staging / spec.runtime_fontdata
            fontdata_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fontdata, fontdata_output)
            entries.append(
                {
                    "filename": spec.filename,
                    "source_path": f"assets/fonts/third_party/galmuri/{spec.filename}",
                    "source_sha256": spec.source_sha256,
                    "hangul_coverage": coverage[spec.filename],
                    "hangul_required": HANGUL_SYLLABLE_COUNT,
                    "roles": list(spec.roles),
                    "uid": spec.uid,
                    "runtime_import_path": spec.runtime_import.as_posix(),
                    "runtime_import_sha256": hashlib.sha256(remap).hexdigest(),
                    "runtime_import_terminal_nul": True,
                    "runtime_fontdata_path": spec.runtime_fontdata.as_posix(),
                    "runtime_fontdata_size": len(data),
                    "runtime_fontdata_sha256": actual_fontdata_sha,
                }
            )

        ui_patch = patch_ui_kit(recovered_ui_kit, staging / UI_OUTPUT)
        license_output = staging / LICENSE_OUTPUT
        license_output.parent.mkdir(parents=True)
        shutil.copyfile(license_source, license_output)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "tool": "tools/fonts.py",
            "godot": {
                "version": version,
                "binary_sha256": expected_godot_sha256,
            },
            "role_policy": {
                "small": "Galmuri9.ttf",
                "body": "Galmuri11.ttf",
                "bold": "Galmuri11-Bold.ttf",
                "heading": "Galmuri14.ttf",
                "title": "Galmuri14.ttf",
                "overflow_optional_unassigned": "Galmuri11-Condensed.ttf",
            },
            "fonts": entries,
            "licenses": [
                {
                    "family": "Galmuri",
                    "license": "SIL Open Font License 1.1",
                    "source_path": "assets/fonts/third_party/galmuri/LICENSE.txt",
                    "source_sha256": GALMURI_LICENSE_SHA256,
                    "package_path": LICENSE_OUTPUT.as_posix(),
                    "package_sha256": sha256_file(license_output),
                }
            ],
            "ui_patch": ui_patch,
        }
        manifest_path = staging / MANIFEST_OUTPUT
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(staging, output_root)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build a new generated Korean font bundle")
    build.add_argument("--assets-root", type=Path, required=True)
    build.add_argument("--recovered-ui-kit", type=Path, required=True)
    build.add_argument("--godot", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_font_bundle(
            assets_root=args.assets_root,
            recovered_ui_kit=args.recovered_ui_kit,
            godot_binary=args.godot,
            output_root=args.output_root,
        )
    except FontBuildError as exc:
        raise SystemExit(f"font build failed: {exc}") from exc
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
