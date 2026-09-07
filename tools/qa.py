"""Validation helpers for the localization TSV files.

The module intentionally has no knowledge of the game resource format.  It
validates the canonical, decoded representation and can also perform the
strict (unquoted, LF-delimited) read required by DATA_SCHEMA.md.  Functions
return :class:`ValidationReport` objects so callers can combine checks without
having to parse human-oriented command output.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SEGMENTS_HEADERS = (
    "segment_id", "content_pack_id", "resource_id", "source_path",
    "source_key", "source_order", "scene_id", "route_id", "story_state_id",
    "speaker_id", "text_type", "source_text", "target_text",
    "protected_tokens", "context_before", "context_after",
    "developer_comment", "char_limit", "byte_limit", "line_limit", "batch_id",
    "status", "source_hash", "target_revision", "decision_ids",
    "uncertainty_ids", "notes",
)

ASSIGNMENTS_HEADERS = (
    "assignment_id", "batch_id", "segment_id", "role", "assignee", "model",
    "reasoning_effort", "base_commit", "source_build", "guidelines_revision",
    "status", "assigned_at", "completed_at", "input_path", "output_path",
    "reviewer_id", "notes",
)

ACTIVE_STATUSES = frozenset({
    "ASSIGNED", "DRAFTED", "AUTO_VALIDATED", "REVIEWED", "APPROVED",
    "INTEGRATED", "RUNTIME_VALIDATED",
})
TERMINAL_STATUSES = frozenset({"BLOCKED", "CANCELLED", "DEFERRED", "OUT_OF_SCOPE"})
TARGET_OPTIONAL_STATUSES = frozenset({
    "EXTRACTED", "READY", "ASSIGNED", "BLOCKED", "CANCELLED", "DEFERRED", "OUT_OF_SCOPE",
})
WRITE_ROLE_NAMES = frozenset({
    "translation_worker", "integration_worker", "translator", "writer",
})

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INVALID_ESCAPE = re.compile(r"\\(?![\\trn])")
_JAPANESE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\uff66-\uff9f\u30fc]")
_ALLOWLIST_SEGMENT_ID = re.compile(r"^SEG-[A-Z0-9][A-Z0-9-]*$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HALFWIDTH_KATAKANA = re.compile(r"[\uff66-\uff9f]")
_MARKUP = re.compile(r"\[(/?)(b|link|sys)([^\]]*)\]")


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    row: int | None = None
    segment_id: str | None = None
    severity: str = "ERROR"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "row": self.row,
            "segment_id": self.segment_id,
            "severity": self.severity,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity != "ERROR"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> "ValidationReport":
        self.issues.extend(other.issues)
        return self

    def add(self, code: str, message: str, *, row: int | None = None,
            segment_id: str | None = None, severity: str = "ERROR") -> None:
        self.issues.append(ValidationIssue(code, message, row, segment_id, severity))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.as_dict() for i in self.issues],
        }


def decode_tsv_escapes(value: str) -> str:
    """Decode DATA_SCHEMA escapes exactly once.

    Unknown escapes are retained.  Callers that need to reject them can use
    :func:`validate_decoded_rows`; retaining them makes diagnostics lossless.
    """
    result: list[str] = []
    i = 0
    replacements = {"\\": "\\", "t": "\t", "r": "\r", "n": "\n"}
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value) and value[i + 1] in replacements:
            result.append(replacements[value[i + 1]])
            i += 2
        else:
            result.append(value[i])
            i += 1
    return "".join(result)


def encode_tsv_escapes(value: str) -> str:
    """Encode a decoded value using the canonical escape order."""
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def _schema_for(kind: str) -> tuple[str, ...]:
    if kind == "segments":
        return SEGMENTS_HEADERS
    if kind == "assignments":
        return ASSIGNMENTS_HEADERS
    raise ValueError(f"unknown TSV kind: {kind}")


def read_tsv(path: str | Path, *, kind: str = "segments") -> tuple[list[dict[str, str]], ValidationReport]:
    """Read an unquoted canonical TSV and return decoded rows plus diagnostics."""
    report = ValidationReport()
    expected = _schema_for(kind)
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        report.add("READ_ERROR", f"cannot read {path}: {exc}")
        return [], report
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.add("UTF8_ERROR", f"{path} is not valid UTF-8: {exc}")
        return [], report
    if text.startswith("\ufeff"):
        report.add("BOM", "UTF-8 BOM is forbidden")
        text = text[1:]
    if "\r\n" in text:
        report.add("CRLF", "repository TSV must use LF line endings")
    if "\r" in text.replace("\r\n", ""):
        report.add("RAW_CR", "raw carriage return in TSV")

    # QUOTE_NONE is deliberate: canonical TSV does not use CSV quoting.
    try:
        parsed = list(csv.reader(io.StringIO(text), delimiter="\t", quotechar="\0", quoting=csv.QUOTE_NONE))
    except csv.Error as exc:
        report.add("TSV_PARSE", str(exc))
        return [], report
    if not parsed or not parsed[0]:
        report.add("MISSING_HEADER", "TSV has no header")
        return [], report
    headers = parsed[0]
    if len(set(headers)) != len(headers):
        report.add("DUPLICATE_HEADER", "TSV header contains duplicate field names", row=1)
    missing = [h for h in expected if h not in headers]
    if missing:
        report.add("MISSING_HEADER", "missing required headers: " + ", ".join(missing), row=1)
    rows: list[dict[str, str]] = []
    for line_no, values in enumerate(parsed[1:], 2):
        if not values or (len(values) == 1 and values[0] == ""):
            continue
        if len(values) != len(headers):
            report.add(
                "ROW_WIDTH",
                f"row has {len(values)} columns; expected {len(headers)}",
                row=line_no,
            )
            continue
        row = {header: decode_tsv_escapes(value) for header, value in zip(headers, values)}
        rows.append(row)
        for header, value in zip(headers, values):
            if _INVALID_ESCAPE.search(value):
                report.add("INVALID_ESCAPE", f"unknown escape in field {header}", row=line_no,
                           segment_id=row.get("segment_id"))
    return rows, report


def _coerce_rows(rows_or_path: Iterable[Mapping[str, Any]] | str | Path, *, kind: str,
                 report: ValidationReport | None = None) -> list[dict[str, Any]]:
    if isinstance(rows_or_path, (str, Path)):
        rows, loaded = read_tsv(rows_or_path, kind=kind)
        if report is not None:
            report.extend(loaded)
        return rows
    return [dict(row) for row in rows_or_path]


def validate_required_headers(path: str | Path, *, kind: str) -> ValidationReport:
    """Validate only the file/header layer, including raw TSV safeguards."""
    _, loaded = read_tsv(path, kind=kind)
    return loaded


def validate_decoded_rows(rows: Iterable[Mapping[str, Any]], *, allow_multiline: bool = False) -> ValidationReport:
    """Catch raw delimiters in programmatically supplied rows.

    Canonical serialization must escape these characters.  Multiline decoded
    text is valid when escaped in the file; ``allow_multiline=False`` is useful
    for metadata-only fields and tests that require a single logical line.
    """
    report = ValidationReport()
    for number, row in enumerate(rows, 2):
        sid = str(row.get("segment_id", "")) or None
        for key, value in row.items():
            if not isinstance(value, str):
                continue
            # A decoded tab/newline is safe only when the writer re-escapes it.
            # This check is intended for values being passed to a raw TSV writer.
            if "\t" in value:
                report.add("RAW_DELIMITER", f"field {key} contains an unescaped delimiter/newline",
                           row=number, segment_id=sid)
            if "\r" in value or (not allow_multiline and "\n" in value):
                report.add("RAW_DELIMITER", f"field {key} contains an unescaped delimiter/newline",
                           row=number, segment_id=sid)
    return report


def _required_value(row: Mapping[str, Any], field_name: str) -> str:
    value = row.get(field_name, "")
    return "" if value is None else str(value)


def validate_segments(rows_or_path: Iterable[Mapping[str, Any]] | str | Path,
                      *, japanese_allowlist: Any = None) -> ValidationReport:
    """Validate canonical segments and all per-segment format invariants."""
    report = ValidationReport()
    rows = _coerce_rows(rows_or_path, kind="segments", report=report)
    if rows:
        missing_headers = [name for name in SEGMENTS_HEADERS if name not in rows[0]]
        if missing_headers:
            report.add("MISSING_HEADER", "missing required headers: " + ", ".join(missing_headers))
    seen: set[str] = set()
    for number, row in enumerate(rows, 2):
        sid = _required_value(row, "segment_id") or None
        if not sid:
            report.add("MISSING_SEGMENT_ID", "segment_id is empty", row=number)
        elif sid in seen:
            report.add("DUPLICATE_SEGMENT_ID", f"duplicate segment_id: {sid}", row=number, segment_id=sid)
        seen.add(sid or f"<row-{number}>")
        source_hash = _required_value(row, "source_hash")
        if not _SHA256.fullmatch(source_hash):
            report.add("SOURCE_HASH", "source_hash must be exactly 64 lowercase hexadecimal characters",
                       row=number, segment_id=sid)
        if not _required_value(row, "source_text"):
            report.add("MISSING_SOURCE_TEXT", "source_text is empty", row=number, segment_id=sid)
        tokens = _parse_protected_tokens(row, report, number, sid)
        source = _required_value(row, "source_text")
        target = _required_value(row, "target_text")
        # Extraction/ready rows intentionally have no draft target yet.  Keep
        # validating source metadata, but do not mistake the absent target for
        # token/markup loss.  Once a row has a non-empty target (or is beyond
        # the pre-translation lifecycle), all target checks remain strict.
        target_pending = (
            not target
            and _required_value(row, "status").upper() in TARGET_OPTIONAL_STATUSES
        )
        # Canonical TSV escapes are decoded exactly once by ``read_tsv``.  A
        # remaining backslash-n in translated display text therefore means a
        # worker wrote ``\\\\n`` to the raw TSV and the game would show the
        # escape spelling instead of a line break.  No in-scope source string
        # uses a visible backslash-n sequence, so fail closed here.
        if target and "\\n" in target:
            report.add(
                "DOUBLE_ESCAPED_NEWLINE",
                "target_text contains a literal backslash-n after one TSV decode",
                row=number,
                segment_id=sid,
            )
        _validate_tokens(tokens, source, target, report, number, sid, check_target=not target_pending)
        _validate_markup(source, report, number, sid, "source_text")
        if not target_pending:
            _validate_markup(target, report, number, sid, "target_text")
    strict_allowlist, allowlist_issues = _strict_allowlist_for_rows(rows, japanese_allowlist)
    report.extend(allowlist_issues)
    report.extend(scan_untranslated_japanese(rows, allowlist=strict_allowlist))
    return report


def _parse_protected_tokens(row: Mapping[str, Any], report: ValidationReport,
                            number: int, sid: str | None) -> list[dict[str, Any]]:
    raw_value = row.get("protected_tokens", "[]")
    if isinstance(raw_value, list):
        value = raw_value
    else:
        raw = _required_value(row, "protected_tokens") or "[]"
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            report.add("TOKENS_JSON", f"protected_tokens is not valid JSON: {exc}", row=number, segment_id=sid)
            return []
    if not isinstance(value, list):
        report.add("TOKENS_TYPE", "protected_tokens must be a JSON array", row=number, segment_id=sid)
        return []
    tokens: list[dict[str, Any]] = []
    for index, token in enumerate(value):
        if not isinstance(token, dict) or not isinstance(token.get("raw"), str):
            report.add("TOKEN_OBJECT", f"token {index} must contain string raw", row=number, segment_id=sid)
            continue
        tokens.append(token)
    return tokens


def _validate_tokens(tokens: Sequence[Mapping[str, Any]], source: str, target: str,
                    report: ValidationReport, number: int, sid: str | None,
                    *, check_target: bool = True) -> None:
    expected_order: list[str] = []
    target_order: list[str] = []
    for token in tokens:
        raw = str(token["raw"])
        expected = sum(1 for item in tokens if item.get("raw") == raw)
        source_count = source.count(raw)
        target_count = target.count(raw)
        if source_count != expected:
            report.add("SOURCE_TOKEN_COUNT", f"{raw!r}: metadata expects {expected}, source has {source_count}",
                       row=number, segment_id=sid)
        if check_target and target_count != expected:
            report.add("TARGET_TOKEN_COUNT", f"{raw!r}: expected {expected}, target has {target_count}",
                       row=number, segment_id=sid)
        if bool(token.get("order_sensitive", False)):
            expected_order.append(raw)
    if expected_order:
        # Record token order without treating ordinary text as a token.
        # ``protected_tokens`` contains one metadata object per occurrence.
        # Searching once per object would multiply every repeated raw token
        # (for example four ``[b]`` objects would each rediscover all four
        # target occurrences) and falsely report a reorder.  Scan each unique
        # raw spelling once while preserving occurrence positions.
        matches: list[tuple[int, str]] = []
        scanned_raw: set[str] = set()
        for token in tokens:
            raw = str(token["raw"])
            if raw in scanned_raw:
                continue
            scanned_raw.add(raw)
            matches.extend((m.start(), raw) for m in re.finditer(re.escape(raw), target))
        target_order = [raw for _, raw in sorted(matches)]
        if check_target and target_order != expected_order:
            report.add("TOKEN_ORDER", "order-sensitive protected tokens were reordered", row=number, segment_id=sid)


def _validate_markup(text: str, report: ValidationReport, number: int, sid: str | None, field_name: str) -> None:
    stack: list[str] = []
    for match in _MARKUP.finditer(text):
        closing, tag, suffix = match.groups()
        if closing:
            if not stack or stack[-1] != tag:
                report.add("MARKUP_NESTING", f"{field_name}: unexpected [/{tag}]", row=number, segment_id=sid)
            else:
                stack.pop()
        else:
            stack.append(tag)
    # A known tag with no closing bracket is always malformed.  This catches
    # accidental truncation while avoiding assumptions about unrelated [text].
    if re.search(r"\[(?:/?(?:b|link|sys))[^\]]*$", text):
        report.add("MARKUP_SYNTAX", f"{field_name}: unterminated markup tag", row=number, segment_id=sid)
    for tag in reversed(stack):
        report.add("MARKUP_UNBALANCED", f"{field_name}: unclosed [{tag}]", row=number, segment_id=sid)


def validate_assignments(rows_or_path: Iterable[Mapping[str, Any]] | str | Path) -> ValidationReport:
    """Validate active write ownership, allowing independent read-only reviews."""
    report = ValidationReport()
    rows = _coerce_rows(rows_or_path, kind="assignments", report=report)
    if rows:
        missing_headers = [name for name in ASSIGNMENTS_HEADERS if name not in rows[0]]
        if missing_headers:
            report.add("MISSING_HEADER", "missing required headers: " + ", ".join(missing_headers))
    active: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for number, row in enumerate(rows, 2):
        sid = _required_value(row, "segment_id")
        role = _required_value(row, "role").lower()
        status = _required_value(row, "status").upper()
        if not sid:
            report.add("MISSING_ASSIGNMENT_SEGMENT", "segment_id is empty", row=number)
        if not role:
            report.add("MISSING_ASSIGNMENT_ROLE", "role is empty", row=number, segment_id=sid or None)
        if status in ACTIVE_STATUSES and _is_write_role(role):
            active.setdefault(sid, []).append((number, row))
    for sid, entries in active.items():
        if sid and len(entries) > 1:
            assignments = ", ".join(_required_value(row, "assignment_id") or f"row {line}" for line, row in entries)
            report.add("OVERLAPPING_ASSIGNMENT", f"segment {sid} has multiple active write assignments: {assignments}",
                       row=entries[1][0], segment_id=sid)
    return report


def _is_write_role(role: str) -> bool:
    if role in WRITE_ROLE_NAMES:
        return True
    # Review roles are explicitly read-only by project policy. Unknown roles
    # are treated as write-capable so an ownership collision cannot be missed.
    return not ("review" in role or role in {"qa", "validator", "validation_worker"})


def _allowlist_for(allowlist: Any, sid: str) -> Any:
    if allowlist is None:
        return None
    if isinstance(allowlist, Mapping):
        return allowlist.get(sid)
    if isinstance(allowlist, (set, frozenset, list, tuple)):
        return True if sid in allowlist else None
    return None


def _parse_japanese_allowlist(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("Japanese allowlist must be a JSON object")
    result: dict[str, list[str]] = {}
    for segment_id, fragments in value.items():
        if not isinstance(segment_id, str) or not _ALLOWLIST_SEGMENT_ID.fullmatch(segment_id):
            raise ValueError(f"invalid Japanese allowlist segment ID: {segment_id!r}")
        if not isinstance(fragments, list) or not fragments:
            raise ValueError(f"Japanese allowlist entry must be a nonempty array: {segment_id}")
        if any(not isinstance(fragment, str) or not fragment for fragment in fragments):
            raise ValueError(
                f"Japanese allowlist fragments must be exact nonempty strings: {segment_id}"
            )
        if len(fragments) != len(set(fragments)):
            raise ValueError(f"Japanese allowlist contains duplicate fragments: {segment_id}")
        for fragment in fragments:
            # A checked-in exception is only for observed mojibake/corruption,
            # never a normal Japanese phrase.  The accepted forms are tied to
            # the evidence in the source corpus: U+FFFD replacement characters,
            # or CJK mojibake mixed with halfwidth katakana (e.g. ``繝ｧ-``).
            if "\ufffd" not in fragment and not (
                _CJK.search(fragment) and _HALFWIDTH_KATAKANA.search(fragment)
            ):
                raise ValueError(
                    f"Japanese allowlist fragment lacks corruption evidence: "
                    f"{segment_id}: {fragment!r}"
                )
        result[segment_id] = list(fragments)
    return result


def load_japanese_allowlist_bytes(data: bytes, *, source: str = "<bytes>") -> dict[str, list[str]]:
    """Parse one immutable allowlist byte snapshot."""
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot load Japanese allowlist {source}: {exc}") from exc
    return _parse_japanese_allowlist(value)


def load_japanese_allowlist(path: str | Path) -> dict[str, list[str]]:
    """Load a checked-in, fragment-only Japanese exception map.

    File-backed exceptions deliberately reject the broad ``*`` and whole-row
    forms supported by the low-level scanner API. Each exception must name the
    exact corrupted source fragment preserved in the Korean target.
    """
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot load Japanese allowlist {source}: {exc}") from exc
    return load_japanese_allowlist_bytes(data, source=str(source))


def _normalize_strict_allowlist(allowlist: Any) -> dict[str, tuple[str, ...]] | None:
    """Normalize only the file-backed mapping shape for workflow callers."""
    if allowlist is None:
        return None
    if not isinstance(allowlist, Mapping):
        raise ValueError("Japanese allowlist must be a mapping of segment IDs to fragment arrays")
    normalized: dict[str, tuple[str, ...]] = {}
    for segment_id, fragments in allowlist.items():
        if not isinstance(segment_id, str) or not _ALLOWLIST_SEGMENT_ID.fullmatch(segment_id):
            raise ValueError(f"invalid Japanese allowlist segment ID: {segment_id!r}")
        if isinstance(fragments, (str, bytes, bytearray, set, frozenset)):
            raise ValueError(f"Japanese allowlist entry must be an ordered fragment array: {segment_id}")
        if not isinstance(fragments, (list, tuple)) or not fragments:
            raise ValueError(f"Japanese allowlist entry must be a nonempty array: {segment_id}")
        values = tuple(fragments)
        if any(not isinstance(fragment, str) or not fragment for fragment in values):
            raise ValueError(f"Japanese allowlist fragments must be exact nonempty strings: {segment_id}")
        if len(values) != len(set(values)):
            raise ValueError(f"Japanese allowlist contains duplicate fragments: {segment_id}")
        if any(
            "\ufffd" not in fragment
            and not (_CJK.search(fragment) and _HALFWIDTH_KATAKANA.search(fragment))
            for fragment in values
        ):
            raise ValueError(f"Japanese allowlist fragment lacks corruption evidence: {segment_id}")
        normalized[segment_id] = values
    return normalized


def _strict_allowlist_for_rows(
    rows: Sequence[Mapping[str, Any]], allowlist: Any,
) -> tuple[dict[str, tuple[str, ...]] | None, ValidationReport]:
    report = ValidationReport()
    try:
        normalized = _normalize_strict_allowlist(allowlist)
    except ValueError as exc:
        report.add("ALLOWLIST_INVALID", str(exc))
        return None, report
    if normalized is None:
        return None, report
    row_by_id = {_required_value(row, "segment_id"): row for row in rows}
    for segment_id, fragments in normalized.items():
        row = row_by_id.get(segment_id)
        if row is None:
            # A checked-in map may be shared by the canonical and individual
            # batch validators.  Entries for another batch are inert because
            # lookup is always keyed by the current row's segment_id.
            continue
        source = _required_value(row, "source_text")
        target = _required_value(row, "target_text")
        counts_match = True
        for fragment in fragments:
            source_count = source.count(fragment)
            if source_count == 0:
                counts_match = False
                report.add("ALLOWLIST_SOURCE_MISMATCH", f"allowlist fragment is absent from source: {fragment!r}", segment_id=segment_id)
                continue
            if target and target.count(fragment) != source_count:
                counts_match = False
                report.add(
                    "ALLOWLIST_TARGET_COUNT",
                    f"allowlist fragment {fragment!r}: source has {source_count}, target has {target.count(fragment)}",
                    segment_id=segment_id,
                )
        if target and counts_match:
            def occurrence_order(text: str) -> list[int]:
                found: list[tuple[int, int]] = []
                for fragment_index, fragment in enumerate(fragments):
                    start = 0
                    while True:
                        position = text.find(fragment, start)
                        if position < 0:
                            break
                        found.append((position, fragment_index))
                        start = position + len(fragment)
                return [fragment_index for _, fragment_index in sorted(found)]

            if occurrence_order(target) != occurrence_order(source):
                report.add(
                    "ALLOWLIST_TARGET_ORDER",
                    "allowlisted corruption fragments changed relative order",
                    segment_id=segment_id,
                )
            if target.count("\ufffd") != source.count("\ufffd"):
                report.add(
                    "ALLOWLIST_TARGET_REPLACEMENT_COUNT",
                    "U+FFFD replacement-character count differs from source",
                    segment_id=segment_id,
                )

            source_lines = source.splitlines()
            target_lines = target.splitlines()
            for fragment in fragments:
                source_line_positions = [
                    index for index, line in enumerate(source_lines) if line == fragment
                ]
                # Whole-line corruption is a layout/reveal invariant. Partial
                # spans (such as 00-SYSTEM) remain governed by count/order.
                if source_line_positions and all(
                    fragment not in line or line == fragment for line in source_lines
                ):
                    target_line_positions = [
                        index for index, line in enumerate(target_lines) if line == fragment
                    ]
                    if target_line_positions != source_line_positions:
                        report.add(
                            "ALLOWLIST_TARGET_LINE_POSITION",
                            f"whole-line corruption fragment moved: {fragment!r}",
                            segment_id=segment_id,
                        )
    return normalized, report


def _remaining_japanese(text: str, allowed: Any) -> str:
    if allowed is True or allowed == "*":
        return ""
    if isinstance(allowed, str):
        allowed = [allowed]
    if isinstance(allowed, (list, tuple, set, frozenset)):
        for fragment in allowed:
            text = text.replace(str(fragment), "")
    return text


def strip_allowlisted_japanese(text: str, segment_id: str, allowlist: Any) -> str:
    """Remove exact fragments from a strict workflow allowlist mapping."""
    try:
        normalized = _normalize_strict_allowlist(allowlist)
    except ValueError:
        return text
    return _remaining_japanese(text, normalized.get(segment_id, ()) if normalized else ())


def _strip_legacy_allowlisted_japanese(text: str, segment_id: str, allowlist: Any) -> str:
    """Retain the permissive scanner-only API for historical callers."""
    return _remaining_japanese(text, _allowlist_for(allowlist, segment_id))


def scan_untranslated_japanese(rows: Iterable[Mapping[str, Any]], *, allowlist: Any = None) -> ValidationReport:
    """Find kana left in targets; only explicit allowlist entries suppress findings.

    ``allowlist`` may be a set of segment IDs (allow all Japanese in those
    segments), or a mapping of segment ID to a list/string of approved Japanese
    fragments.  Kanji-only strings are intentionally not flagged because they
    are indistinguishable from ordinary Chinese-character use without locale
    or source-format evidence.
    """
    report = ValidationReport()
    for number, row in enumerate(rows, 2):
        sid = _required_value(row, "segment_id") or None
        target = _required_value(row, "target_text")
        remaining = _strip_legacy_allowlisted_japanese(target, sid or "", allowlist)
        found = sorted(set(_JAPANESE.findall(remaining)))
        if found:
            report.add("UNTRANSLATED_JAPANESE", "Japanese kana remains in target: " + " ".join(found),
                       row=number, segment_id=sid)
    return report


@dataclass(frozen=True)
class GlyphCoverage:
    available: bool
    missing: tuple[str, ...] = ()
    covered: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.available and not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {"available": self.available, "ok": self.ok, "missing": list(self.missing),
                "covered": list(self.covered), "error": self.error}


def check_glyph_coverage(font_path: str | Path, text_or_rows: str | Iterable[Mapping[str, Any]]) -> GlyphCoverage:
    """Return Hangul/Unicode coverage using fontTools when installed.

    Missing fontTools is a non-fatal unavailable result, allowing CI to run
    schema/token checks on minimal environments while making font validation
    explicit rather than falsely passing it.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        return GlyphCoverage(False, error=f"fontTools unavailable: {exc}")
    chars: set[str] = set()
    if isinstance(text_or_rows, str):
        chars.update(text_or_rows)
    else:
        for row in text_or_rows:
            chars.update(_required_value(row, "target_text"))
    chars.difference_update({"\n", "\r", "\t"})
    try:
        font = TTFont(str(font_path), lazy=True)
        try:
            cmap: set[int] = set()
            for table in font["cmap"].tables:
                cmap.update(table.cmap)
        finally:
            font.close()
    except Exception as exc:  # fontTools raises several format-specific errors
        return GlyphCoverage(False, error=f"cannot read font: {exc}")
    covered = tuple(sorted((ch for ch in chars if ord(ch) in cmap), key=ord))
    missing = tuple(sorted((ch for ch in chars if ord(ch) not in cmap), key=ord))
    return GlyphCoverage(True, missing=missing, covered=covered)


# Short alias used by callers that describe the operation as font coverage.
glyph_coverage = check_glyph_coverage


def validate_batch(segments: Iterable[Mapping[str, Any]] | str | Path,
                   assignments: Iterable[Mapping[str, Any]] | str | Path | None = None,
                   *, japanese_allowlist: Any = None, font_path: str | Path | None = None) -> ValidationReport:
    """Run canonical segment, assignment, and optional font checks together."""
    report = validate_segments(segments, japanese_allowlist=japanese_allowlist)
    if assignments is not None:
        report.extend(validate_assignments(assignments))
        segment_rows = _coerce_rows(segments, kind="segments")
        assignment_rows = _coerce_rows(assignments, kind="assignments")
        segment_ids = {_required_value(row, "segment_id") for row in segment_rows}
        for number, row in enumerate(assignment_rows, 2):
            sid = _required_value(row, "segment_id")
            role = _required_value(row, "role").lower()
            status = _required_value(row, "status").upper()
            if status in ACTIVE_STATUSES and _is_write_role(role) and sid not in segment_ids:
                report.add(
                    "ORPHANED_ACTIVE_ASSIGNMENT",
                    f"active write assignment references missing segment {sid}",
                    row=number,
                    segment_id=sid or None,
                )
    if font_path is not None:
        rows = _coerce_rows(segments, kind="segments")
        coverage = check_glyph_coverage(font_path, rows)
        if not coverage.available:
            report.add("FONT_UNAVAILABLE", coverage.error or "font coverage unavailable", severity="WARNING")
        elif coverage.missing:
            report.add("FONT_GLYPH_MISSING", "font lacks glyphs: " + " ".join(coverage.missing))
    return report


def validate_batch_output(source: Iterable[Mapping[str, Any]] | str | Path,
                          output: Iterable[Mapping[str, Any]] | str | Path,
                          *, batch_id: str | None = None) -> ValidationReport:
    """Check that a worker output preserves source identity and row ownership.

    Worker proposals may change only ``target_text`` and ``notes``.  This
    function deliberately does not validate the proposed target linguistically;
    callers can feed the output to :func:`validate_segments` separately.
    """
    report = ValidationReport()
    source_rows = _coerce_rows(source, kind="segments", report=report)
    output_rows = _coerce_rows(output, kind="segments", report=report)
    if batch_id is not None:
        source_rows = [row for row in source_rows if _required_value(row, "batch_id") == batch_id]
        output_rows = [row for row in output_rows if _required_value(row, "batch_id") == batch_id]
    source_by_id = {_required_value(row, "segment_id"): row for row in source_rows}
    output_by_id = {_required_value(row, "segment_id"): row for row in output_rows}
    missing = sorted(set(source_by_id) - set(output_by_id))
    extra = sorted(set(output_by_id) - set(source_by_id))
    for sid in missing:
        report.add("BATCH_MISSING_ID", f"worker output is missing {sid}", segment_id=sid)
    for sid in extra:
        report.add("BATCH_EXTRA_ID", f"worker output contains unexpected {sid}", segment_id=sid)
    immutable = set(SEGMENTS_HEADERS) - {"target_text", "notes"}
    for sid in sorted(set(source_by_id) & set(output_by_id)):
        before, after = source_by_id[sid], output_by_id[sid]
        if _required_value(before, "source_hash") != _required_value(after, "source_hash"):
            report.add("BATCH_SOURCE_HASH", "worker output changed source_hash", segment_id=sid)
        for field_name in sorted(immutable - {"source_hash"}):
            if _required_value(before, field_name) != _required_value(after, field_name):
                report.add("BATCH_IMMUTABLE_FIELD", f"worker output changed {field_name}", segment_id=sid)
    return report


# Descriptive aliases for integrations that use the validation catalog names.
validate_canonical_tsv = validate_batch
check_font_coverage = check_glyph_coverage
scan_japanese = scan_untranslated_japanese


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Validate localization canonical TSV files")
    parser.add_argument("segments", type=Path)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--allowlist", type=Path, help="JSON object mapping segment IDs to allowed Japanese fragments")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    allowlist = None
    if args.allowlist:
        try:
            allowlist = load_japanese_allowlist(args.allowlist)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": f"allowlist: {exc}"}, ensure_ascii=False))
            return 2
    report = validate_batch(args.segments, args.assignments, japanese_allowlist=allowlist, font_path=args.font)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
