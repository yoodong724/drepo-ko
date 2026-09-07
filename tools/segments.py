#!/usr/bin/env python3
"""Deterministic text extraction and reinsertion for The Death Game Report.

The module deliberately operates on a recovered, disposable Godot project.  It
never opens the shipped PCK for writing.  PCK patching is an optional final
step, imported lazily from :mod:`tools.pck`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


EXTRACTOR_VERSION = "segments-v2"

SEGMENT_COLUMNS = (
    "segment_id", "content_pack_id", "resource_id", "source_path",
    "source_key", "source_order", "scene_id", "route_id", "story_state_id",
    "speaker_id", "text_type", "source_text", "target_text",
    "protected_tokens", "context_before", "context_after",
    "developer_comment", "char_limit", "byte_limit", "line_limit",
    "batch_id", "status", "source_hash", "target_revision", "decision_ids",
    "uncertainty_ids", "notes",
)

MANIFEST_COLUMNS = (
    "resource_id", "content_pack_id", "source_path", "container_path",
    "resource_type", "format", "encoding", "newline", "source_size",
    "source_sha256", "extractor_version", "extracted_segment_count",
    "scope_status", "investigation_status", "notes",
)

EXPECTED_STORY_BLOCKS = {"deaths.txt": 41, "messages.txt": 7, "system.txt": 12}
DISPLAY_JSON_FIELDS = frozenset({"club"})

# ASCII is intentionally not extracted generally.  These exact literals were
# verified to flow to runtime UI sinks in the recovered project.
ASCII_RUNTIME_LITERALS = {
    "scenes/title_screen.tscn": frozenset({"The Death Game Report", "created by shu3"}),
    "scripts/desktop.gd": frozenset({"YASHIMA EMPIRE", "AUTOSAVING", "AUTOSAVING...", "CONFIG", "MENU"}),
    "scripts/ui/alert_dialog.gd": frozenset({"OK"}),
    "scripts/ui/config_dialog.gd": frozenset({"OFF", "ON"}),
    "scripts/ui/title_bar.gd": frozenset({"Untitled"}),
    "scripts/windows/boot_screen.gd": frozenset({
        "YASHIMA-OS  Rev 2.3",
        "CPU .......................... OK",
        "MEMORY  16384K ............... OK",
        "NEURAL-LINK CTRL ............. OK",
        "MOUNT ARCHIVE:/NL ............ OK",
        "LOAD ORDER:[00-ORDER] ........ OK",
        "INDEX INTEGRITY CHECK ........ FAIL",
        "RECORDS:40   STATUS: RECOVER",
    }),
    "scripts/windows/confirm_cutscene.gd": frozenset({"OK"}),
    "scripts/windows/ending_credits.gd": frozenset({
        "The Death Game Report",
        "― STAFF ―",
        "Game Design / Writing / Art",
        "shu3",
        "Programming / Music / Sound",
        "― PLAYTESTING ―",
        "mochi, tellyam, HEIDI, MadaraUsi, km69",
        "― INSPIRED BY ―",
        "Return of the Obra Dinn ― Lucas Pope",
        "Type Help ― William Rous",
        "Battle Royale ― Koushun Takami",
        "― THE END ―",
    }),
    "scripts/windows/log_viewer.gd": frozenset({"SYSTEM"}),
    "scripts/windows/title_screen.gd": frozenset({"ver "}),
}
DEBUG_LITERAL_ALLOWLIST = {
    # Returned as the reason interpolated into SteamManager's debug-only
    # initialization message at line 50.
    "scripts/steam_manager.gd": frozenset({"撮影モード"}),
}

# Code-only Japanese-matching literals that must never be translated because
# they are program data, not display text.  The hyphen set below is consumed
# by GameData.normalize_record_key (1.0.3+); translating it would corrupt
# record-name lookup.  Exact value plus exact path only (D-0043).
_CODE_ONLY_LITERAL_SCOPE = {
    "data/game_data.gd": frozenset({"－−‐‑‒–—―ーｰ"}),
}

# These are the only recovered GodotSteam resources proven to be editor-only
# in the pinned build.  Do not turn this into an ``addons/godotsteam/**`` rule:
# GodotSteam runtime scripts remain part of the game's runtime surface.
_EDITOR_ONLY_GODOTSTEAM_PATHS = frozenset({
    "addons/godotsteam/editor/steamworks_panel.gd",
    "addons/godotsteam/editor/steamworks_panel.tscn",
    "addons/godotsteam/editor/updates/updates.gd",
    "addons/godotsteam/godotsteam_plugin.gd",
})
_GODOT_TOOL_DECL_RE = re.compile(r"(?m)^[ \t]*@tool[ \t]*\r?$")
_EDITOR_PLUGIN_EXTENDS_RE = re.compile(
    r"(?m)^[ \t]*extends[ \t]+EditorPlugin[ \t]*\r?$"
)
_EDITOR_PANEL_SCRIPT_REFERENCES = (
    'path="res://addons/godotsteam/editor/steamworks_panel.gd"',
    'path="res://addons/godotsteam/editor/updates/updates.gd"',
)

_STATE_GROUPS = {
    "STATE-RESTORE-INITIAL": ("05-D2-29", "06-D2-11", "07-D1-24"),
    "STATE-RESTORE-A": (
        "03-E4-27", "09-B2-07", "14-C3-30", "13-C3-34", "10-D4-20",
        "11-D4-16", "12-D4-31", "08-C2-37", "02-E4-04", "01-E4-02", "04-C4-23",
    ),
    "STATE-RESTORE-B": (
        "30-D1-32", "19-E2-15", "20-E2-41", "18-E2-28", "17-E2-08",
        "15-C3-13", "16-C3-01",
    ),
    "STATE-RESTORE-C": ("28-B3-22", "24-B3-06", "26-B3-26", "25-B3-36", "27-B3-35"),
    "STATE-RESTORE-D": ("29-B3-18", "23-B1-21"),
    "STATE-RESTORE-E": (
        "21-E2-17", "22-E2-10", "32-B2-12", "31-B2-19", "33-B2-03",
        "39-D1-38", "37-D1-25", "38-D1-40", "40-D1-33", "36-B4-39",
        "35-B4-09", "34-B2-14",
    ),
    "STATE-FINAL": ("41-A1-05",),
}
RECORD_STATES = {
    record_key: state_id
    for state_id, record_keys in _STATE_GROUPS.items()
    for record_key in record_keys
}
STATE_SCENES = {
    "STATE-RESTORE-INITIAL": "SCENE-RECORD-INITIAL",
    "STATE-RESTORE-A": "SCENE-RECORD-A",
    "STATE-RESTORE-B": "SCENE-RECORD-B",
    "STATE-RESTORE-C": "SCENE-RECORD-C",
    "STATE-RESTORE-D": "SCENE-RECORD-D",
    "STATE-RESTORE-E": "SCENE-RECORD-E",
    "STATE-FINAL": "SCENE-FINAL",
}
MESSAGE_STATES = {
    1: "STATE-RESTORE-A", 2: "STATE-RESTORE-B", 3: "STATE-RESTORE-C",
    4: "STATE-RESTORE-D", 5: "STATE-RESTORE-E", 6: "STATE-RESTORE-E",
    7: "STATE-FINAL",
}
SYSTEM_STATES = {
    "00-ORDER": "STATE-RESTORE-INITIAL",
    "00-SYSTEM": "STATE-RESTORE-INITIAL",
    "00-REPORT": "STATE-RESTORE-INITIAL",
    "00-DECREE": "STATE-FINAL",
    "00-SEALED": "STATE-FINAL",
}
MULTISTATE_SYSTEM_DOCS = frozenset({
    "00-ZONE", "00-REFERENCE", "00-CHIP", "00-MOTIVE",
    "00-ANNOUNCE", "00-ROLL", "00-MAP",
})

_JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff65-\uff9f\ufffd]")
_STORY_HEADER_RE = re.compile(
    r"(?m)^=== (?P<key>[^|\r\n]+?) \| (?P<title>[^|\r\n]*?) \| "
    r"(?P<state>[^\r\n]*?)(?=\r?$)"
)
_REF_LINE_RE = re.compile(r"(?m)^@refs:[^\r\n]*(?:\r?\n)?")
_TOKEN_RE = re.compile(
    r"\[[^\]\r\n]+\]"
    r"|%(?:%|[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[diouxXeEfFgGcrs])"
    r"|\{[A-Za-z_][A-Za-z0-9_.:-]*\}"
    r"|(?<![A-Za-z0-9])(?:\d{2}|\?{2})-(?:[A-Z]\d|[A-Z]+|\?{2})"
    r"-(?:\d{2}|\?{2})(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])00-[A-Z]+(?![A-Za-z0-9])"
)


class SegmentError(ValueError):
    """Input violates a structural or localization safety invariant."""


@dataclass(frozen=True)
class TextSpan:
    source_key: str
    start: int
    end: int
    source_text: str
    text_type: str
    scene_id: str = ""
    story_state_id: str = ""
    speaker_id: str = ""
    developer_comment: str = ""
    route_id: str = ""
    status: str = "EXTRACTED"
    scope_status: str = "IN_SCOPE"
    decision_ids: str = ""
    uncertainty_ids: str = ""
    notes: str = ""


@dataclass
class Resource:
    source_path: str
    format: str
    resource_type: str
    text: str
    raw: bytes
    spans: list[TextSpan] = field(default_factory=list)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_hash(value: str) -> str:
    """Hash the exact decoded source string, with no Unicode normalization."""
    return sha256_bytes(value.encode("utf-8"))


def resource_id(source_path: str) -> str:
    digest = hashlib.sha256(("BASE\0" + source_path).encode("utf-8")).hexdigest()
    return "RES-" + digest[:16].upper()


def segment_id(source_path: str, source_key: str) -> str:
    material = "BASE\0" + source_path + "\0" + source_key
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "SEG-BASE-" + digest[:20].upper()


def contextual_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16].upper()}"


def escape_tsv(value: Any) -> str:
    """Encode TSV field escapes exactly once (DATA_SCHEMA.md section 1)."""
    text = "" if value is None else str(value)
    return (text.replace("\\", "\\\\")
                .replace("\t", "\\t")
                .replace("\r", "\\r")
                .replace("\n", "\\n"))


def unescape_tsv(value: str) -> str:
    out: list[str] = []
    i = 0
    escapes = {"\\": "\\", "t": "\t", "r": "\r", "n": "\n"}
    while i < len(value):
        if value[i] != "\\":
            out.append(value[i])
            i += 1
            continue
        if i + 1 >= len(value) or value[i + 1] not in escapes:
            bad = value[i:i + 2]
            raise SegmentError(f"invalid TSV escape {bad!r}")
        out.append(escapes[value[i + 1]])
        i += 2
    return "".join(out)


def read_tsv(path: Path | str) -> tuple[list[str], list[dict[str, str]]]:
    path = Path(path)
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SegmentError(f"UTF-8 BOM is forbidden: {path}")
    text = raw.decode("utf-8")
    if "\r" in text:
        raise SegmentError(f"TSV must use LF line endings: {path}")
    reader = csv.reader(text.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise SegmentError(f"empty TSV: {path}") from exc
    if not header or len(header) != len(set(header)) or any(not h for h in header):
        raise SegmentError(f"invalid or duplicate TSV header: {path}")
    rows: list[dict[str, str]] = []
    for number, values in enumerate(reader, 2):
        if len(values) != len(header):
            raise SegmentError(
                f"{path}:{number}: expected {len(header)} fields, got {len(values)}"
            )
        rows.append({key: unescape_tsv(value) for key, value in zip(header, values)})
    return header, rows


def write_tsv_atomic(
    path: Path | str,
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
) -> None:
    """Write LF/UTF-8/no-BOM TSV atomically, retaining caller columns."""
    path = Path(path)
    if len(columns) != len(set(columns)) or any(not c for c in columns):
        raise SegmentError("TSV columns must be nonempty and unique")
    materialized = list(rows)
    lines = ["\t".join(columns)]
    for row in materialized:
        lines.append("\t".join(escape_tsv(row.get(column, "")) for column in columns))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _token_class(raw: str) -> str:
    if raw.startswith("["):
        if raw.startswith("[/"):
            return "markup_close"
        return "markup_open" if raw not in ("[br]",) else "control_code"
    if raw.startswith("%"):
        return "placeholder"
    if raw.startswith("{"):
        return "variable"
    return "record_key"


def protected_tokens(text: str) -> str:
    tokens = [
        {
            "raw": match.group(0),
            "class": _token_class(match.group(0)),
            "movable": False,
            "order_sensitive": True,
        }
        for match in _TOKEN_RE.finditer(text)
    ]
    return json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))


def _token_signature(text: str) -> list[tuple[str, str]]:
    return [(m.group(0), _token_class(m.group(0))) for m in _TOKEN_RE.finditer(text)]


def validate_target_tokens(source: str, target: str, location: str = "") -> None:
    expected = _token_signature(source)
    actual = _token_signature(target)
    if expected != actual:
        prefix = f"{location}: " if location else ""
        raise SegmentError(prefix + f"protected tokens changed: {expected!r} != {actual!r}")


def _read_utf8(path: Path) -> tuple[str, bytes]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SegmentError(f"source UTF-8 BOM is not supported: {path}")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError as exc:
        raise SegmentError(f"source is not valid UTF-8: {path}") from exc


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in " \t\r\n":
        start += 1
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    return start, end


def _story_context(
    source_path: str,
    key: str,
    title: str,
) -> tuple[str, str, str, str]:
    """Return evidence-backed scene, route, state and speaker IDs."""
    if source_path.endswith("deaths.txt"):
        state_id = RECORD_STATES.get(key, "")
        if not state_id:
            raise SegmentError(f"{source_path}: death record is absent from CLEAR_ROUTE: {key}")
        return (
            STATE_SCENES[state_id], "ROUTE-COMMON", state_id,
            contextual_id("SPK", title) if title and title != "--" else "",
        )
    if source_path.endswith("messages.txt"):
        number_match = re.search(r"MSG(?P<number>\d{2})$", key)
        state_id = MESSAGE_STATES.get(int(number_match.group("number")), "") if number_match else ""
        scene_id = STATE_SCENES.get(state_id, "")
        return scene_id, "ROUTE-COMMON", state_id, "SPK-D9F45EE0EA471EAE"
    if source_path.endswith("system.txt"):
        state_id = SYSTEM_STATES.get(key, "")
        return STATE_SCENES.get(state_id, ""), "ROUTE-COMMON", state_id, ""
    return contextual_id("SCENE-BASE", source_path + "\0" + key), "", "", ""


def parse_story(source_path: str, text: str) -> list[TextSpan]:
    headers = list(_STORY_HEADER_RE.finditer(text))
    spans: list[TextSpan] = []
    seen: set[str] = set()
    for index, header in enumerate(headers):
        key = header.group("key").strip()
        if not key or key in seen:
            raise SegmentError(f"{source_path}: duplicate/empty story key {key!r}")
        seen.add(key)
        title = header.group("title")
        state = header.group("state")
        scene, route, story_state, speaker = _story_context(source_path, key, title)
        comment_data: dict[str, Any] = {
            "record_key": key, "title": title, "state": state,
            "context_evidence": "GameData.CLEAR_ROUTE/STORY_STATES" if route else "resource-local",
        }
        span_status = "EXTRACTED"
        span_uncertainty_ids = ""
        span_notes = ""
        if source_path.endswith("system.txt") and key in MULTISTATE_SYSTEM_DOCS:
            comment_data.update({
                "story_state_mapping_status": "MULTI_STATE_VERIFIED",
                "state_is_intentionally_blank": True,
                "runtime_evidence": (
                    "log_viewer accepts any existing 00-[A-Z]+ record outside the separately gated "
                    "00-DECREE/00-SEALED pair"
                ),
            })
            span_notes = "Runtime-restorable in multiple states; no single story_state_id is valid."
        comment = json.dumps(
            comment_data,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if _JP_RE.search(title):
            spans.append(TextSpan(
                f"record/{key}/title", header.start("title"), header.end("title"),
                title, "ui", scene, story_state, speaker, comment,
                route_id=route,
                status=span_status,
                uncertainty_ids=span_uncertainty_ids,
                notes=span_notes,
            ))
        if _JP_RE.search(state):
            spans.append(TextSpan(
                f"record/{key}/state", header.start("state"), header.end("state"),
                state, "ui", scene, story_state, speaker, comment,
                route_id=route,
                status=span_status,
                uncertainty_ids=span_uncertainty_ids,
                notes=span_notes,
            ))
        block_start = header.end()
        if text.startswith("\r\n", block_start):
            block_start += 2
        elif block_start < len(text) and text[block_start] == "\n":
            block_start += 1
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        ref = _REF_LINE_RE.match(text, block_start, block_end)
        if ref:
            block_start = ref.end()
        body_start, body_end = _trimmed_span(text, block_start, block_end)
        if body_start < body_end:
            spans.append(TextSpan(
                f"record/{key}/body", body_start, body_end,
                text[body_start:body_end],
                "system" if source_path.endswith("system.txt") else "narration",
                scene, story_state, speaker, comment,
                route_id=route,
                status=span_status,
                uncertainty_ids=span_uncertainty_ids,
                notes=span_notes,
            ))
    return spans


def _json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


_JSON_CLUB_RE = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])+")\s*:\s*\{(?P<object>.*?)\}', re.DOTALL
)
_JSON_FIELD_RE = re.compile(
    r'(?P<name>"(?:\\.|[^"\\])+")\s*:\s*(?P<value>"(?:\\.|[^"\\])*")'
)


def parse_solutions(source_path: str, text: str) -> list[TextSpan]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SegmentError(f"{source_path}: invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SegmentError(f"{source_path}: top-level JSON must be an object")
    spans: list[TextSpan] = []
    located: set[tuple[str, str]] = set()
    for object_match in _JSON_CLUB_RE.finditer(text):
        record_key = json.loads(object_match.group("key"))
        if record_key not in parsed or not isinstance(parsed[record_key], dict):
            continue
        object_text = object_match.group("object")
        object_offset = object_match.start("object")
        for field_match in _JSON_FIELD_RE.finditer(object_text):
            name = json.loads(field_match.group("name"))
            if name not in DISPLAY_JSON_FIELDS:
                continue
            value = json.loads(field_match.group("value"))
            if not isinstance(value, str):
                continue
            start = object_offset + field_match.start("value")
            end = object_offset + field_match.end("value")
            key = (record_key, name)
            located.add(key)
            # Empty display values are valid structural data, but they contain
            # nothing to translate and canonical source_text must be nonempty.
            # Keep them in ``located`` so the lexical-vs-parsed safety check
            # below still proves that every configured display field was found.
            if value == "":
                continue
            spans.append(TextSpan(
                f"/{_json_pointer(record_key)}/{_json_pointer(name)}",
                start, end, value, "unknown",
                contextual_id("SCENE-BASE", source_path + "\0" + record_key),
                "", "",
                json.dumps(
                    {
                        "record_key": record_key,
                        "field": name,
                        "scope_reason": "source-derived solution metadata; no runtime read consumer in this build",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                status="OUT_OF_SCOPE",
                scope_status="OUT_OF_SCOPE",
                notes="No runtime read consumer in this build; display labels are sourced from game_data.gd.",
            ))
    expected = {
        (record_key, name)
        for record_key, obj in parsed.items() if isinstance(obj, dict)
        for name in DISPLAY_JSON_FIELDS if isinstance(obj.get(name), str)
    }
    if located != expected:
        raise SegmentError(f"{source_path}: could not locate all display fields")
    return spans


@dataclass(frozen=True)
class _StringLiteral:
    start: int
    end: int
    quote: str
    value: str
    ordinal: int


def _decode_godot_string(raw: str, quote: str) -> str:
    out: list[str] = []
    i = 0
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
              "v": "\v", "a": "\a", "\\": "\\", '"': '"', "'": "'"}
    while i < len(raw):
        if raw[i] != "\\":
            out.append(raw[i])
            i += 1
            continue
        if i + 1 >= len(raw):
            raise SegmentError("unterminated escape in Godot string")
        esc = raw[i + 1]
        if esc in simple:
            out.append(simple[esc])
            i += 2
        elif esc in ("u", "U"):
            width = 4 if esc == "u" else 6
            digits = raw[i + 2:i + 2 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                raise SegmentError("invalid Unicode escape in Godot string")
            out.append(chr(int(digits, 16)))
            i += 2 + width
        else:
            # Retain an unrecognized Godot escape as literal data.  If translated,
            # the encoder will escape the backslash and preserve its runtime text.
            out.extend(("\\", esc))
            i += 2
    return "".join(out)


def _encode_godot_string(value: str, quote: str = '"') -> str:
    replacements = {
        "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t",
        "\b": "\\b", "\f": "\\f", "\v": "\\v", "\a": "\\a",
        quote: "\\" + quote,
    }
    out: list[str] = []
    for char in value:
        code = ord(char)
        if char in replacements:
            out.append(replacements[char])
        elif code == 0:
            out.append("\\u0000")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def scan_godot_strings(text: str) -> list[_StringLiteral]:
    """Lex ordinary GDScript/Godot resource string literals, skipping comments."""
    literals: list[_StringLiteral] = []
    i = 0
    ordinal = 0
    while i < len(text):
        char = text[i]
        if char == "#":
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline + 1
            continue
        if char not in ('"', "'"):
            i += 1
            continue
        quote = char
        content_start = i + 1
        j = content_start
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == quote:
                break
            if text[j] in "\r\n":
                raise SegmentError(f"newline in ordinary Godot string at offset {i}")
            j += 1
        if j >= len(text):
            raise SegmentError(f"unterminated Godot string at offset {i}")
        ordinal += 1
        literals.append(_StringLiteral(
            content_start, j, quote,
            _decode_godot_string(text[content_start:j], quote), ordinal,
        ))
        i = j + 1
    return literals


def _tscn_key(text: str, literal: _StringLiteral, duplicate: int) -> str:
    before = text[:literal.start - 1]
    section_matches = list(re.finditer(r"(?m)^\[(node|resource|sub_resource)[^\r\n]*\]$", before))
    section = section_matches[-1].group(0) if section_matches else "root"
    node_name = re.search(r'\bname="([^"]+)"', section)
    node_parent = re.search(r'\bparent="([^"]+)"', section)
    node_id = (node_parent.group(1) + "/" if node_parent else "") + (
        node_name.group(1) if node_name else section.strip("[]")
    )
    line_start = before.rfind("\n") + 1
    prefix = text[line_start:literal.start - 1]
    prop = re.search(r"([A-Za-z_][A-Za-z0-9_/]*)\s*=\s*$", prefix)
    base = f"node/{node_id}/property/{prop.group(1) if prop else 'literal'}"
    return base if duplicate == 1 else f"{base}#{duplicate}"


def _hint_override_key(text: str, literal: _StringLiteral) -> tuple[str, str] | None:
    """Resolve a TABLE value to record/mode/level without ordinal identity."""
    quote_start = literal.start - 1
    line_start = text.rfind("\n", 0, quote_start) + 1
    value_prefix = text[line_start:quote_start]
    level_match = re.fullmatch(r"\s*(?P<level>\d+)\s*:\s*", value_prefix)
    if not level_match:
        return None
    before_line = text[:line_start]
    record_matches = list(re.finditer(
        r'(?m)^\s*"(?P<record>\d{2}-[A-Z]\d-\d{2})"\s*:\s*\{\s*$',
        before_line,
    ))
    if not record_matches:
        return None
    record_match = record_matches[-1]
    mode_matches = list(re.finditer(
        r'(?m)^\s*"(?P<mode>restore|confirm)"\s*:\s*\{\s*$',
        text[record_match.end():line_start],
    ))
    if not mode_matches:
        return None
    record = record_match.group("record")
    mode = mode_matches[-1].group("mode")
    level = level_match.group("level")
    return f"table/{record}/{mode}/{level}", record


def _is_console_sink(text: str, literal: _StringLiteral) -> bool:
    quote_start = literal.start - 1
    line_start = text.rfind("\n", 0, quote_start) + 1
    prefix = text[line_start:quote_start]
    return re.search(r"\b(?:print|printerr|push_warning|push_error)\s*\(", prefix) is not None


def _ascii_metadata(source_path: str, value: str) -> dict[str, Any]:
    if value == "created by shu3":
        return {
            "ascii_runtime_sink": True,
            "content_kind": "credit",
            "coordinator_policy_hint": "DO_NOT_TRANSLATE_CREDIT",
        }
    if value == "The Death Game Report":
        return {
            "ascii_runtime_sink": True,
            "content_kind": "branding",
            "translation_policy": "TRANSLATABLE",
        }
    return {"ascii_runtime_sink": True, "content_kind": "runtime_ui"}


_CREDIT_PRESERVE_LITERALS = frozenset({
    "shu3",
    "mochi, tellyam, HEIDI, MadaraUsi, km69",
    "Return of the Obra Dinn ― Lucas Pope",
    "Type Help ― William Rous",
    "Battle Royale ― Koushun Takami",
})


_PRESERVED_ENGLISH_SUBTITLE = (
    "scenes/title_screen.tscn",
    "node/FG/TitleBlock/En/property/text",
    "The Death Game Report",
)


_DYNAMIC_REPORT_FRAGMENTS = frozenset({
    "%sにハーモニカの紐で絞殺された",
    "で",
    "%sに突き落とされ、落下死",
    "%sに毒を盛られ、死亡",
    "%sに%s%sされた",
    "%sに%s殺害された",
    "%sにより死亡",
})


def parse_godot(source_path: str, text: str) -> list[TextSpan]:
    spans: list[TextSpan] = []
    duplicates: dict[str, int] = {}
    is_tscn = source_path.endswith(".tscn")
    for literal in scan_godot_strings(text):
        is_ascii_runtime = literal.value in ASCII_RUNTIME_LITERALS.get(source_path, ())
        if not _JP_RE.search(literal.value) and not is_ascii_runtime:
            continue
        hint_context = (
            _hint_override_key(text, literal)
            if source_path == "data/hint_overrides.gd" else None
        )
        if is_tscn:
            provisional = _tscn_key(text, literal, 1)
            duplicates[provisional] = duplicates.get(provisional, 0) + 1
            key = _tscn_key(text, literal, duplicates[provisional])
        elif hint_context is not None:
            key = hint_context[0]
        else:
            key = f"literal/{literal.ordinal:06d}"
        line = text.count("\n", 0, literal.start) + 1
        metadata: dict[str, Any] = {"line": line, "literal_ordinal": literal.ordinal}
        if is_ascii_runtime:
            metadata.update(_ascii_metadata(source_path, literal.value))

        text_type = "ui"
        route_id = ""
        story_state_id = ""
        scene_id = ""
        status = "EXTRACTED"
        scope_status = "IN_SCOPE"
        uncertainty_ids = ""
        decision_ids = ""
        notes = ""
        if source_path in ("data/hint.gd", "data/hint_overrides.gd"):
            text_type = "tutorial"
            route_id = "ROUTE-COMMON"
            if hint_context is not None:
                record = hint_context[1]
                story_state_id = RECORD_STATES.get(record, "")
                scene_id = STATE_SCENES.get(story_state_id, "")
                metadata["record_key"] = record
                metadata["semantic_key_evidence"] = "HintOverrides.TABLE record/mode/level"
        elif source_path == "scripts/windows/ending_credits.gd":
            text_type = "credits"
            route_id = "ROUTE-COMMON"
            story_state_id = "STATE-FINAL"
            scene_id = "SCENE-FINAL"
        elif any(name in source_path for name in ("intro_sequence", "ending_digest")):
            text_type = "narration"
            route_id = "ROUTE-COMMON"
            story_state_id = "STATE-BOOT" if "intro_sequence" in source_path else "STATE-FINAL"
            scene_id = "SCENE-BOOT" if story_state_id == "STATE-BOOT" else "SCENE-FINAL"
        elif literal.value == "created by shu3":
            text_type = "credits"
            status = "OUT_OF_SCOPE"
            scope_status = "OUT_OF_SCOPE"
            decision_ids = "D-0019"
            notes = "Creator credit is preserved verbatim by decision D-0019."

        if (source_path, key, literal.value) == _PRESERVED_ENGLISH_SUBTITLE:
            text_type = "credits"
            status = "OUT_OF_SCOPE"
            scope_status = "OUT_OF_SCOPE"
            decision_ids = "D-0037"
            notes = "Title-screen English subtitle is preserved verbatim by decision D-0037."
            metadata.update({
                "content_kind": "english_subtitle",
                "translation_policy": "PRESERVE_ENGLISH_SUBTITLE",
                "preservation_evidence": "exact source path and node/property path",
            })

        if source_path == "scripts/windows/ending_credits.gd" and literal.value in _CREDIT_PRESERVE_LITERALS:
            status = "OUT_OF_SCOPE"
            scope_status = "OUT_OF_SCOPE"
            notes = "Creator/person/handle or attributed work credit; preserve source spelling verbatim."
            metadata.update({
                "credit_preservation": "VERBATIM",
                "contains_creator_person_or_handle": True,
                "decision_id": "",
            })

        if literal.value in _CODE_ONLY_LITERAL_SCOPE.get(source_path, ()):
            status = "OUT_OF_SCOPE"
            scope_status = "OUT_OF_SCOPE"
            decision_ids = "D-0043"
            notes = "Code-only program data, not display text; translating it would corrupt runtime behavior."
            metadata.update({
                "code_only_literal": True,
                "translation_policy": "DO_NOT_TRANSLATE_CODE_DATA",
            })

        if (_is_console_sink(text, literal)
                or literal.value in DEBUG_LITERAL_ALLOWLIST.get(source_path, ())):
            text_type = "debug"
            status = "OUT_OF_SCOPE"
            scope_status = "OUT_OF_SCOPE"
            notes = "Developer console/log text; excluded from player-facing localization scope."
            metadata["scope_evidence"] = (
                "verified console interpolation source"
                if literal.value in DEBUG_LITERAL_ALLOWLIST.get(source_path, ())
                else "print/printerr/push_warning/push_error sink"
            )

        if source_path == "data/game_data.gd" and literal.value in _DYNAMIC_REPORT_FRAGMENTS:
            metadata.update({
                "dynamic_composition_risk": "HIGH",
                "composition_family": "GameData.sentence_for",
                "uncertainty_hint": "KOREAN_PARTICLE_AND_FRAGMENT_REVIEW_REQUIRED",
                "independent_translation_allowed": False,
            })
            status = "BLOCKED"
            uncertainty_ids = "U-0009"
            notes = "High-risk runtime composition fragment; review assembled Korean sentence before translation."

        spans.append(TextSpan(
            key, literal.start, literal.end, literal.value,
            text_type, scene_id, story_state_id, "",
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            route_id=route_id,
            status=status,
            scope_status=scope_status,
            decision_ids=decision_ids,
            uncertainty_ids=uncertainty_ids,
            notes=notes,
        ))
    return spans


def discover_paths(source_root: Path | str) -> list[Path]:
    root = Path(source_root)
    paths: set[Path] = set()
    for name in EXPECTED_STORY_BLOCKS:
        path = root / "story" / "logs" / name
        if path.is_file():
            paths.add(path)
    solution = root / "data" / "solutions.json"
    if solution.is_file():
        paths.add(solution)
    for pattern in ("*.gd", "*.tscn"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            paths.add(path)
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def load_resource(source_root: Path | str, path: Path) -> Resource:
    root = Path(source_root)
    source_path = path.relative_to(root).as_posix()
    text, raw = _read_utf8(path)
    if source_path.startswith("story/logs/") and path.name in EXPECTED_STORY_BLOCKS:
        return Resource(source_path, "story_blocks", "dialogue", text, raw,
                        parse_story(source_path, text))
    if source_path == "data/solutions.json":
        return Resource(source_path, "json", "table", text, raw,
                        parse_solutions(source_path, text))
    if path.suffix in (".gd", ".tscn"):
        return Resource(source_path, path.suffix[1:], "script" if path.suffix == ".gd" else "scene",
                        text, raw, parse_godot(source_path, text))
    raise SegmentError(f"unsupported resource: {source_path}")


def load_resources(source_root: Path | str) -> list[Resource]:
    return [load_resource(source_root, path) for path in discover_paths(source_root)]


def _newline_kind(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    cr = raw.count(b"\r") - crlf
    kinds = sum(bool(n) for n in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "CRLF"
    if lf:
        return "LF"
    if cr:
        return "CR"
    return "none"


def _editor_only_godotsteam_evidence(
    resource: Resource,
    resources_by_path: Mapping[str, Resource],
) -> str | None:
    """Return exact source evidence for the four verified editor resources.

    A matching directory prefix is intentionally insufficient.  Each exact
    path must also retain the source declaration/reference that proves its
    editor role; source drift therefore falls back to ``IN_SCOPE`` instead of
    silently excluding a possibly runtime resource.
    """
    source_path = resource.source_path
    if source_path not in _EDITOR_ONLY_GODOTSTEAM_PATHS:
        return None
    if source_path == "addons/godotsteam/godotsteam_plugin.gd":
        if (_GODOT_TOOL_DECL_RE.search(resource.text)
                and _EDITOR_PLUGIN_EXTENDS_RE.search(resource.text)):
            return "exact path + @tool + extends EditorPlugin"
        return None
    if source_path.endswith(".gd"):
        if _GODOT_TOOL_DECL_RE.search(resource.text):
            return "exact addons/godotsteam/editor script path + @tool"
        return None
    if source_path == "addons/godotsteam/editor/steamworks_panel.tscn":
        referenced_paths = (
            "addons/godotsteam/editor/steamworks_panel.gd",
            "addons/godotsteam/editor/updates/updates.gd",
        )
        referenced_resources = [resources_by_path.get(path) for path in referenced_paths]
        if (all(marker in resource.text for marker in _EDITOR_PANEL_SCRIPT_REFERENCES)
                and all(referenced_resources)
                and all(_GODOT_TOOL_DECL_RE.search(item.text)
                        for item in referenced_resources if item is not None)):
            return "exact editor panel scene path + verified @tool script sources"
        return None
    return None


def rows_from_resources(resources: Sequence[Resource]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    segment_rows: list[dict[str, str]] = []
    manifest_rows: list[dict[str, str]] = []
    issued_ids: set[str] = set()
    resources_by_path = {resource.source_path: resource for resource in resources}
    for resource in resources:
        rid = resource_id(resource.source_path)
        editor_only_evidence = _editor_only_godotsteam_evidence(
            resource, resources_by_path,
        )
        scope_counts: dict[str, int] = {}
        for span in resource.spans:
            scope_counts[span.scope_status] = scope_counts.get(span.scope_status, 0) + 1
        if editor_only_evidence is not None:
            manifest_scope = "OUT_OF_SCOPE"
        elif len(scope_counts) == 1:
            manifest_scope = next(iter(scope_counts))
        else:
            manifest_scope = "IN_SCOPE"
        manifest_notes: list[str] = []
        if editor_only_evidence is not None:
            manifest_notes.append(
                "GodotSteam editor-only resource; " + editor_only_evidence
            )
        if scope_counts.get("DEFERRED"):
            manifest_notes.append(f"{scope_counts['DEFERRED']} segments deferred")
        debug_out = sum(
            span.scope_status == "OUT_OF_SCOPE" and span.text_type == "debug"
            for span in resource.spans
        )
        credit_out = sum(
            span.scope_status == "OUT_OF_SCOPE" and span.text_type == "credits"
            for span in resource.spans
        )
        other_out = scope_counts.get("OUT_OF_SCOPE", 0) - debug_out - credit_out
        if debug_out:
            manifest_notes.append(
                f"{debug_out} developer console/log segments out of scope"
            )
        if credit_out:
            manifest_notes.append(f"{credit_out} verbatim credit segments out of scope")
        if other_out:
            if resource.source_path == "data/solutions.json":
                manifest_notes.append(
                    f"{other_out} source-derived solution metadata segments out of scope; "
                    "no runtime read consumer in this build"
                )
            else:
                manifest_notes.append(f"{other_out} other segments out of scope")
        manifest_rows.append({
            "resource_id": rid,
            "content_pack_id": "BASE",
            "source_path": resource.source_path,
            "container_path": "drepo.pck",
            "resource_type": resource.resource_type,
            "format": resource.format,
            "encoding": "UTF-8",
            "newline": _newline_kind(resource.raw),
            "source_size": str(len(resource.raw)),
            "source_sha256": sha256_bytes(resource.raw),
            "extractor_version": EXTRACTOR_VERSION,
            "extracted_segment_count": str(len(resource.spans)),
            "scope_status": manifest_scope,
            "investigation_status": "EXTRACTABLE",
            "notes": "; ".join(manifest_notes),
        })
        for order, span in enumerate(resource.spans, 1):
            sid = segment_id(resource.source_path, span.source_key)
            if sid in issued_ids:
                raise SegmentError(f"segment ID collision: {sid}")
            issued_ids.add(sid)
            segment_rows.append({
                "segment_id": sid,
                "content_pack_id": "BASE",
                "resource_id": rid,
                "source_path": resource.source_path,
                "source_key": span.source_key,
                "source_order": str(order),
                "scene_id": span.scene_id,
                "route_id": span.route_id,
                "story_state_id": span.story_state_id,
                "speaker_id": span.speaker_id,
                "text_type": span.text_type,
                "source_text": span.source_text,
                "target_text": "",
                "protected_tokens": protected_tokens(span.source_text),
                "context_before": resource.spans[order - 2].source_text if order > 1 else "",
                "context_after": resource.spans[order].source_text if order < len(resource.spans) else "",
                "developer_comment": span.developer_comment,
                "char_limit": "", "byte_limit": "", "line_limit": "",
                "batch_id": "", "status": span.status,
                "source_hash": source_hash(span.source_text),
                "target_revision": "0", "decision_ids": span.decision_ids,
                "uncertainty_ids": span.uncertainty_ids, "notes": span.notes,
            })
    return manifest_rows, segment_rows


def extract(
    source_root: Path | str,
    segments_path: Path | str,
    manifest_path: Path | str,
    *,
    require_expected_counts: bool = True,
) -> dict[str, Any]:
    resources = load_resources(source_root)
    inspection = inspect_source(source_root)
    if require_expected_counts and inspection["story_counts"] != EXPECTED_STORY_BLOCKS:
        raise SegmentError(
            f"story block counts differ: {inspection['story_counts']} != {EXPECTED_STORY_BLOCKS}"
        )
    manifests, segments = rows_from_resources(resources)
    write_tsv_atomic(manifest_path, manifests, MANIFEST_COLUMNS)
    write_tsv_atomic(segments_path, segments, SEGMENT_COLUMNS)
    return {
        "resources": len(resources),
        "segments": len(segments),
        "manifest_sha256": sha256_bytes(Path(manifest_path).read_bytes()),
        "segments_sha256": sha256_bytes(Path(segments_path).read_bytes()),
        **inspection,
    }


def inspect_source(source_root: Path | str) -> dict[str, Any]:
    root = Path(source_root)
    counts: dict[str, int] = {}
    for name in EXPECTED_STORY_BLOCKS:
        path = root / "story" / "logs" / name
        counts[name] = len(_STORY_HEADER_RE.findall(_read_utf8(path)[0])) if path.is_file() else 0
    resources = load_resources(root)
    return {
        "source_root": str(root),
        "resource_count": len(resources),
        "segment_count": sum(len(resource.spans) for resource in resources),
        "story_counts": counts,
        "story_counts_match": counts == EXPECTED_STORY_BLOCKS,
    }


def _validate_rows(rows: Sequence[Mapping[str, str]], source_root: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    ids: set[str] = set()
    locations: set[tuple[str, str]] = set()
    resource_cache: dict[str, Resource] = {}
    for number, row in enumerate(rows, 2):
        missing = [column for column in SEGMENT_COLUMNS if column not in row]
        if missing:
            errors.append(f"row {number}: missing columns {missing}")
            continue
        sid = row["segment_id"]
        location = (row["source_path"], row["source_key"])
        expected_sid = segment_id(*location)
        if sid in ids:
            errors.append(f"row {number}: duplicate segment_id {sid}")
        ids.add(sid)
        if location in locations:
            errors.append(f"row {number}: duplicate source location {location}")
        locations.add(location)
        if sid != expected_sid:
            errors.append(f"row {number}: unstable segment_id {sid}; expected {expected_sid}")
        if row["source_hash"] != source_hash(row["source_text"]):
            errors.append(f"row {number}: source_hash mismatch")
        try:
            revision = int(row["target_revision"])
            if revision < 0:
                raise ValueError
        except ValueError:
            errors.append(f"row {number}: invalid target_revision")
        try:
            recorded_tokens = json.loads(row["protected_tokens"])
            if not isinstance(recorded_tokens, list):
                raise ValueError
            if row["protected_tokens"] != protected_tokens(row["source_text"]):
                errors.append(f"row {number}: protected_tokens mismatch")
        except (json.JSONDecodeError, ValueError):
            errors.append(f"row {number}: invalid protected_tokens JSON")
        if row["target_text"]:
            try:
                validate_target_tokens(row["source_text"], row["target_text"], sid)
            except SegmentError as exc:
                errors.append(str(exc))
        if source_root is not None:
            try:
                resource = resource_cache.get(row["source_path"])
                if resource is None:
                    resource = load_resource(source_root, source_root / row["source_path"])
                    resource_cache[row["source_path"]] = resource
                span = {item.source_key: item for item in resource.spans}.get(row["source_key"])
                if span is None:
                    errors.append(f"row {number}: source key not found")
                elif span.source_text != row["source_text"]:
                    errors.append(f"row {number}: source text changed")
            except (OSError, SegmentError) as exc:
                errors.append(f"row {number}: source verification failed: {exc}")
    return {"rows": len(rows), "errors": errors, "valid": not errors}


def validate_segments(path: Path | str, source_root: Path | str | None = None) -> dict[str, Any]:
    header, rows = read_tsv(path)
    missing = [column for column in SEGMENT_COLUMNS if column not in header]
    if missing:
        return {"rows": len(rows), "errors": [f"missing required columns: {missing}"], "valid": False}
    return _validate_rows(rows, Path(source_root) if source_root else None)


def _replacement_text(resource: Resource, span: TextSpan, target: str) -> str:
    if resource.format == "json":
        return json.dumps(target, ensure_ascii=False)
    if resource.format in ("gd", "tscn"):
        # Spans cover literal contents, not quote characters.
        quote = resource.text[span.start - 1]
        return _encode_godot_string(target, quote)
    return target


def reinsert_resource(resource: Resource, rows: Sequence[Mapping[str, str]]) -> bytes:
    by_key = {span.source_key: span for span in resource.spans}
    replacements: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for row in rows:
        key = row["source_key"]
        if key in seen:
            raise SegmentError(f"duplicate row for {resource.source_path}:{key}")
        seen.add(key)
        span = by_key.get(key)
        if span is None:
            raise SegmentError(f"source key not found: {resource.source_path}:{key}")
        if row["source_text"] != span.source_text:
            raise SegmentError(f"source text drift: {resource.source_path}:{key}")
        if row["source_hash"] != source_hash(span.source_text):
            raise SegmentError(f"source hash drift: {resource.source_path}:{key}")
        target = row.get("target_text", "") or span.source_text
        validate_target_tokens(span.source_text, target, row.get("segment_id", key))
        if target != span.source_text:
            replacements.append((span.start, span.end, _replacement_text(resource, span, target)))
    missing = set(by_key) - seen
    if missing:
        raise SegmentError(f"TSV omits {resource.source_path} keys: {sorted(missing)[:5]}")
    text = resource.text
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text.encode("utf-8")


def build_resources(
    source_root: Path | str,
    segments_path: Path | str,
    output_root: Path | str,
) -> dict[str, Any]:
    source_root = Path(source_root).resolve()
    output_root = Path(output_root).resolve()
    if (output_root == source_root or source_root in output_root.parents
            or output_root in source_root.parents):
        raise SegmentError("output root and source root must be separate directory trees")
    header, rows = read_tsv(segments_path)
    missing_columns = [column for column in SEGMENT_COLUMNS if column not in header]
    if missing_columns:
        raise SegmentError(f"segments TSV missing columns: {missing_columns}")
    validation = _validate_rows(rows, source_root)
    if not validation["valid"]:
        raise SegmentError("segments validation failed: " + "; ".join(validation["errors"]))
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["source_path"], []).append(row)
    hashes: dict[str, dict[str, str]] = {}
    for source_path in sorted(grouped):
        resource = load_resource(source_root, source_root / source_path)
        output = reinsert_resource(resource, grouped[source_path])
        destination = output_root / source_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(output)
        hashes[source_path] = {
            "input": sha256_bytes(resource.raw),
            "output": sha256_bytes(output),
            "equal": str(output == resource.raw).lower(),
        }
    return {"resources": len(grouped), "hashes": hashes}


def _load_pck_module():
    try:
        return importlib.import_module("tools.pck")
    except ModuleNotFoundError:
        try:
            return importlib.import_module("pck")
        except ModuleNotFoundError as exc:
            raise SegmentError("tools.pck is unavailable; resource output was not packaged") from exc


def package_pck(
    source_pck: Path,
    output_pck: Path,
    resource_root: Path,
    *,
    resource_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    source_pck = source_pck.resolve()
    output_pck = output_pck.resolve()
    if source_pck == output_pck:
        raise SegmentError("refusing to overwrite the original PCK")
    if resource_paths is None:
        selected = [
            path.relative_to(resource_root).as_posix()
            for path in sorted(resource_root.rglob("*")) if path.is_file()
        ]
    else:
        selected = sorted(set(resource_paths))
    replacements = {path: (resource_root / path).read_bytes() for path in selected}
    module = _load_pck_module()
    module.patch_pck(source_pck, output_pck, replacements)
    return {
        "replacement_count": len(replacements),
        "output_pck": str(output_pck),
        "output_sha256": sha256_bytes(output_pck.read_bytes()),
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gdre-binary", default=os.environ.get("GDRE_BINARY", ""),
                        help="reserved GDRETools path; PCK helpers may consume it")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_cmd = sub.add_parser("inspect")
    inspect_cmd.add_argument("--recovered-dir", required=True, type=Path)
    extract_cmd = sub.add_parser("extract")
    extract_cmd.add_argument("--recovered-dir", required=True, type=Path)
    extract_cmd.add_argument("--segments", required=True, type=Path)
    extract_cmd.add_argument("--manifest", required=True, type=Path)
    extract_cmd.add_argument("--allow-partial", action="store_true")
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("--segments", required=True, type=Path)
    validate_cmd.add_argument("--recovered-dir", type=Path)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--recovered-dir", required=True, type=Path)
    build_cmd.add_argument("--segments", required=True, type=Path)
    build_cmd.add_argument("--output-dir", required=True, type=Path)
    build_cmd.add_argument("--source-pck", type=Path)
    build_cmd.add_argument("--output-pck", type=Path)
    package_cmd = sub.add_parser("package")
    package_cmd.add_argument("--source-pck", required=True, type=Path)
    package_cmd.add_argument("--output-pck", required=True, type=Path)
    package_cmd.add_argument("--resource-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_source(args.recovered_dir)
        elif args.command == "extract":
            result = extract(args.recovered_dir, args.segments, args.manifest,
                             require_expected_counts=not args.allow_partial)
        elif args.command == "validate":
            result = validate_segments(args.segments, args.recovered_dir)
            if not result["valid"]:
                _print_json(result)
                return 1
        elif args.command == "build":
            if bool(args.source_pck) != bool(args.output_pck):
                raise SegmentError("--source-pck and --output-pck must be supplied together")
            result = build_resources(args.recovered_dir, args.segments, args.output_dir)
            if args.source_pck:
                changed = [
                    path for path, hashes in result["hashes"].items()
                    if hashes["equal"] != "true"
                ]
                # Recovered .gd/.tscn paths are source representations.  The
                # shipped archive contains compiled .gdc/.scn entries, so never
                # pretend that raw source can replace them.  A GDRE compilation
                # step must first materialize exact archive-relative outputs.
                unsupported = [path for path in changed if path.endswith((".gd", ".tscn"))]
                if unsupported:
                    raise SegmentError(
                        "changed recovered Godot sources require GDRE compile/export before PCK patch: "
                        + ", ".join(unsupported)
                    )
                result["pck"] = package_pck(
                    args.source_pck, args.output_pck, args.output_dir,
                    resource_paths=changed,
                )
        elif args.command == "package":
            result = package_pck(args.source_pck, args.output_pck, args.resource_dir)
        else:  # pragma: no cover - argparse enforces this
            raise AssertionError(args.command)
    except (OSError, SegmentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
