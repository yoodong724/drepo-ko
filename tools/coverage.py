#!/usr/bin/env python3
"""Coverage and display-limit validation for canonical localization TSVs.

This validator is deliberately separate from :mod:`tools.qa`: QA checks the
resource/TSV mechanics, while this module answers the production questions
"is this row translated?" and "does its target fit the declared limits?".
It operates on decoded TSV values and never edits the input file.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

# ``python tools/coverage.py ...`` puts ``tools/`` (rather than the project
# root) on ``sys.path``.  Keep both module and direct-script invocation
# supported, matching the command form used by the validation catalog.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import qa


# A target is required only once translation work has produced a draft.  READY
# and ASSIGNED are intentionally pre-translation states.  Terminal/non-text
# states are excluded before any target inspection.
DEFAULT_TARGET_REQUIRED_STATUSES = frozenset({
    "DRAFTED",
    "AUTO_VALIDATED",
    "REVIEWED",
    "APPROVED",
    "INTEGRATED",
    "RUNTIME_VALIDATED",
})
SKIPPED_STATUSES = frozenset({
    "BLOCKED",
    "CANCELLED",
    "DEFERRED",
    "OUT_OF_SCOPE",
})

_JAPANESE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_REPLACEMENT = "\ufffd"
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class CoverageIssue:
    """One deterministic, machine-readable coverage finding."""

    code: str
    message: str
    row: int | None = None
    segment_id: str | None = None
    severity: str = "ERROR"
    field: str | None = None
    observed: int | None = None
    limit: int | None = None
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "row": self.row,
            "segment_id": self.segment_id,
            "severity": self.severity,
            "field": self.field,
            "observed": self.observed,
            "limit": self.limit,
            "unit": self.unit,
        }


@dataclass
class CoverageReport:
    """Result returned by the API and serialized by the CLI."""

    issues: list[CoverageIssue] = field(default_factory=list)
    row_count: int = 0
    checked_row_count: int = 0
    skipped_row_count: int = 0
    target_required_statuses: tuple[str, ...] = tuple(
        sorted(DEFAULT_TARGET_REQUIRED_STATUSES)
    )

    @property
    def errors(self) -> list[CoverageIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> list[CoverageIssue]:
        return [issue for issue in self.issues if issue.severity != "ERROR"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        issues = sorted(
            self.issues,
            key=lambda issue: (
                issue.row if issue.row is not None else 0,
                issue.segment_id or "",
                issue.code,
                issue.field or "",
            ),
        )
        return {
            "schema_version": 1,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "row_count": self.row_count,
            "checked_row_count": self.checked_row_count,
            "skipped_row_count": self.skipped_row_count,
            "target_required_statuses": list(self.target_required_statuses),
            "issues": [issue.as_dict() for issue in issues],
        }


def _value(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key, "")
    return "" if value is None else str(value)


def _normalise_allowlist(allowlist: Any) -> dict[str, tuple[str, ...]]:
    """Return exact fragment entries, rejecting permissive wildcard forms.

    File-backed allowlists are parsed by the established strict loader.  A
    mapping is accepted for library callers and receives the same validation
    by using the workflow normalizer from ``tools.qa``.
    """

    if allowlist is None:
        return {}
    if isinstance(allowlist, (str, Path)):
        value = qa.load_japanese_allowlist(allowlist)
    else:
        # This is the strict, source-bound normalizer used by canonical QA.
        value = qa._normalize_strict_allowlist(allowlist)  # type: ignore[attr-defined]
    return {str(segment_id): tuple(fragments) for segment_id, fragments in value.items()}


def _normalise_preserved_fragments(value: Any) -> dict[str, tuple[str, ...]]:
    """Load intentional source-script fragments preserved for visual matching.

    Unlike the corruption allowlist, this map may contain normal Japanese, but
    remains exact, segment-bound, ordered, nonempty, and wildcard-free.  It is
    kept separate so canonical QA never treats intentional source script as
    encoding corruption.
    """

    if value is None:
        return {}
    if isinstance(value, (str, Path)):
        source = Path(value)
        try:
            raw = source.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise ValueError("UTF-8 BOM is forbidden")
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"cannot load preserved-fragment map {source}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("preserved-fragment map must be a JSON object")
    result: dict[str, tuple[str, ...]] = {}
    segment_id_pattern = re.compile(r"^SEG-[A-Z0-9][A-Z0-9._-]*$")
    for segment_id, fragments in value.items():
        if not isinstance(segment_id, str) or not segment_id_pattern.fullmatch(segment_id):
            raise ValueError(f"invalid preserved-fragment segment ID: {segment_id!r}")
        if not isinstance(fragments, (list, tuple)) or not fragments:
            raise ValueError(
                f"preserved-fragment entry must be a nonempty ordered array: {segment_id}"
            )
        if any(not isinstance(fragment, str) or not fragment for fragment in fragments):
            raise ValueError(
                f"preserved fragments must be exact nonempty strings: {segment_id}"
            )
        if len(fragments) != len(set(fragments)):
            raise ValueError(f"preserved-fragment entry contains duplicates: {segment_id}")
        result[segment_id] = tuple(fragments)
    return result


def _merge_fragment_maps(
    first: Mapping[str, Sequence[str]], second: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    merged = {segment_id: tuple(fragments) for segment_id, fragments in first.items()}
    for segment_id, fragments in second.items():
        values = merged.get(segment_id, ()) + tuple(fragments)
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate exact exception across maps: {segment_id}")
        merged[segment_id] = values
    return merged


def strip_allowlisted_fragments(
    text: str, segment_id: str, allowlist: Mapping[str, Sequence[str]],
) -> str:
    """Remove only the exact, row-bound allowlist fragments."""

    for fragment in allowlist.get(segment_id, ()):
        text = text.replace(fragment, "")
    return text


def residual_japanese_cjk(
    target: str, segment_id: str = "", allowlist: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Find residual Japanese/CJK characters after exact allowlist removal."""

    remaining = strip_allowlisted_fragments(target, segment_id, allowlist or {})
    found: dict[str, tuple[str, ...]] = {}
    kana = tuple(sorted(set(_JAPANESE.findall(remaining)), key=ord))
    ideographs = tuple(sorted(set(_CJK.findall(remaining)), key=ord))
    replacement = (_REPLACEMENT,) if _REPLACEMENT in remaining else ()
    if kana:
        found["japanese"] = kana
    if ideographs:
        found["cjk"] = ideographs
    if replacement:
        found["replacement"] = replacement
    return found


def _validate_allowlist_bindings(
    rows: Sequence[Mapping[str, Any]],
    allowlist: Mapping[str, Sequence[str]],
    report: CoverageReport,
) -> None:
    """Ensure exact exceptions are present once and in source order.

    Without this binding check a typo or duplicated exception could turn a
    broad untranslated scan into an accidental wildcard.  Entries for other
    batches are intentionally inert, matching the canonical QA contract.
    """

    rows_by_id = {_value(row, "segment_id"): row for row in rows}
    for sid in sorted(allowlist):
        row = rows_by_id.get(sid)
        if row is None:
            continue
        source = _value(row, "source_text")
        target = _value(row, "target_text")
        source_order: list[tuple[int, int]] = []
        target_order: list[tuple[int, int]] = []
        for index, fragment in enumerate(allowlist[sid]):
            source_positions = [match.start() for match in re.finditer(re.escape(fragment), source)]
            if not source_positions:
                report.issues.append(CoverageIssue(
                    "ALLOWLIST_SOURCE_MISMATCH",
                    f"allowlist fragment is absent from source: {fragment!r}",
                    segment_id=sid,
                ))
                continue
            source_order.extend((position, index) for position in source_positions)
            if target:
                target_positions = [match.start() for match in re.finditer(re.escape(fragment), target)]
                if len(target_positions) != len(source_positions):
                    report.issues.append(CoverageIssue(
                        "ALLOWLIST_TARGET_COUNT",
                        f"allowlist fragment {fragment!r}: source has {len(source_positions)}, "
                        f"target has {len(target_positions)}",
                        segment_id=sid,
                    ))
                target_order.extend((position, index) for position in target_positions)
        if target and [index for _, index in sorted(source_order)] != [
            index for _, index in sorted(target_order)
        ]:
            report.issues.append(CoverageIssue(
                "ALLOWLIST_TARGET_ORDER",
                "allowlisted fragments changed relative order",
                segment_id=sid,
            ))


def count_lines(text: str) -> int:
    """Count logical lines, including a final empty line after a newline."""

    if not text:
        return 0
    # Canonical resources use LF.  Counting CRLF as one separator keeps this
    # helper precise for synthetic/API input as well.
    return len(re.split(r"\r\n|\r|\n", text))


def _parse_limit(raw: Any, field_name: str) -> tuple[int | None, str | None]:
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool):
        return None, f"{field_name} must be a non-negative decimal integer"
    value = str(raw)
    if not _INTEGER.fullmatch(value):
        return None, f"{field_name} must be a non-negative decimal integer: {value!r}"
    return int(value), None


def validate_coverage(
    rows_or_path: Iterable[Mapping[str, Any]] | str | Path,
    *,
    allowlist: Any = None,
    preserved_fragments: Any = None,
    target_required_statuses: Iterable[str] = DEFAULT_TARGET_REQUIRED_STATUSES,
) -> CoverageReport:
    """Validate translation coverage and declared target limits.

    ``rows_or_path`` may be decoded row mappings or a canonical TSV path.  A
    row in a skipped status is not considered a missing translation and is not
    checked for residual Japanese/CJK or limits.  Rows in a pre-translation
    status may have an empty target; if they do have a target, it is still
    scanned for residual source text and limits.
    """

    required = frozenset(str(status).upper() for status in target_required_statuses)
    report = CoverageReport(target_required_statuses=tuple(sorted(required)))
    rows: list[Mapping[str, Any]]
    if isinstance(rows_or_path, (str, Path)):
        rows, read_report = qa.read_tsv(rows_or_path)
        for issue in read_report.issues:
            report.issues.append(CoverageIssue(
                code=issue.code,
                message=issue.message,
                row=issue.row,
                segment_id=issue.segment_id,
                severity=issue.severity,
            ))
    else:
        rows = list(rows_or_path)
    report.row_count = len(rows)

    try:
        strict_allowlist = _merge_fragment_maps(
            _normalise_allowlist(allowlist),
            _normalise_preserved_fragments(preserved_fragments),
        )
    except (OSError, ValueError, TypeError) as exc:
        report.issues.append(CoverageIssue("ALLOWLIST_INVALID", str(exc)))
        strict_allowlist = {}
    _validate_allowlist_bindings(rows, strict_allowlist, report)

    for number, row in enumerate(rows, 2):
        sid = _value(row, "segment_id") or None
        status = _value(row, "status").upper()
        if status in SKIPPED_STATUSES:
            report.skipped_row_count += 1
            continue
        report.checked_row_count += 1
        target = _value(row, "target_text")
        if status in required and not target:
            report.issues.append(CoverageIssue(
                "EMPTY_TARGET", "target_text is required for an integratable status",
                number, sid, field="target_text",
            ))
            # No target exists to inspect further, but metadata limits remain
            # parseable and are checked below for deterministic diagnostics.
        elif target:
            residual = residual_japanese_cjk(target, sid or "", strict_allowlist)
            if "japanese" in residual:
                report.issues.append(CoverageIssue(
                    "UNTRANSLATED_JAPANESE",
                    "Japanese kana remains after exact allowlist removal: "
                    + " ".join(residual["japanese"]),
                    number, sid,
                ))
            if "cjk" in residual:
                report.issues.append(CoverageIssue(
                    "UNTRANSLATED_CJK",
                    "CJK ideographs remain after exact allowlist removal: "
                    + " ".join(residual["cjk"]),
                    number, sid,
                ))
            if "replacement" in residual:
                report.issues.append(CoverageIssue(
                    "UNTRANSLATED_REPLACEMENT",
                    "replacement character remains after exact allowlist removal",
                    number, sid,
                ))

            source = _value(row, "source_text")
            # Exact ASCII labels (for example a title or OK) can legitimately
            # remain unchanged.  A non-ASCII unchanged target is suspicious;
            # residual-language findings above provide the hard failure where
            # it is actually Japanese/CJK.
            if target == source and any(ord(char) > 127 for char in source):
                report.issues.append(CoverageIssue(
                    "UNCHANGED_TARGET",
                    "target_text is identical to non-ASCII source_text",
                    number, sid, severity="WARNING", field="target_text",
                ))

        for field_name, unit, measure in (
            ("char_limit", "characters", len),
            ("byte_limit", "bytes", lambda value: len(value.encode("utf-8"))),
            ("line_limit", "lines", count_lines),
        ):
            limit, error = _parse_limit(row.get(field_name, ""), field_name)
            if error:
                report.issues.append(CoverageIssue(
                    "INVALID_LIMIT", error, number, sid, field=field_name,
                ))
                continue
            if limit is None or not target:
                continue
            observed = measure(target)
            if observed > limit:
                report.issues.append(CoverageIssue(
                    "LIMIT_OVERFLOW",
                    f"{field_name} exceeded: observed {observed}, limit {limit}",
                    number, sid, field=field_name, observed=observed,
                    limit=limit, unit=unit,
                ))
    return report


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Validate in-scope Korean translation coverage and text limits"
    )
    parser.add_argument("segments", type=Path)
    parser.add_argument(
        "--allowlist", type=Path,
        help="JSON object mapping segment IDs to exact preserved corruption fragments",
    )
    parser.add_argument(
        "--preserved-fragments", type=Path,
        help="JSON object mapping segment IDs to intentional exact source-script fragments",
    )
    parser.add_argument(
        "--require-target-status", action="append", dest="required_statuses",
        metavar="STATUS", help="status requiring a non-empty target (repeatable)",
    )
    args = parser.parse_args()
    allowlist: Any = None
    if args.allowlist:
        allowlist = args.allowlist
    statuses = (
        args.required_statuses
        if args.required_statuses is not None
        else DEFAULT_TARGET_REQUIRED_STATUSES
    )
    report = validate_coverage(
        args.segments,
        allowlist=allowlist,
        preserved_fragments=args.preserved_fragments,
        target_required_statuses=statuses,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(_cli())
