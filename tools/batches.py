#!/usr/bin/env python3
"""Deterministic, no-overwrite tooling for localization batch snapshots.

The worker-facing files produced here are proposals, never canonical data.
Approved proposals are merged into a *new* canonical TSV only after the
source snapshot, worker output, current canonical file, and per-row revisions
have all been pinned and revalidated.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.qa import (
    ACTIVE_STATUSES,
    ASSIGNMENTS_HEADERS,
    SEGMENTS_HEADERS,
    ValidationReport,
    load_japanese_allowlist,
    validate_assignments,
    validate_batch_output,
    validate_segments,
)
from tools.segments import escape_tsv, read_tsv


MERGE_MUTABLE_FIELDS = frozenset({
    "target_text", "notes", "status", "batch_id", "target_revision",
})
WORKER_MUTABLE_FIELDS = frozenset({"target_text", "notes"})
APPROVAL_STATUS = "APPROVED"
APPROVAL_TRANSITIONS = {"REVIEWED": "APPROVED"}
ASSIGNABLE_STATUSES = frozenset({"EXTRACTED", "READY"})
ASSIGNMENT_ROLE = "translation_worker"
LIFECYCLE_TRANSITIONS = frozenset({
    ("ASSIGNED", "DRAFTED"),
    ("DRAFTED", "AUTO_VALIDATED"),
    ("AUTO_VALIDATED", "REVIEWED"),
    ("APPROVED", "INTEGRATED"),
    ("INTEGRATED", "RUNTIME_VALIDATED"),
    ("APPROVED", "NEEDS_RECHECK"),
    ("NEEDS_RECHECK", "DRAFTED"),
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_BATCH_ID = re.compile(r"^BATCH-[A-Z0-9][A-Z0-9-]*$")


class BatchError(ValueError):
    """A batch operation would violate ownership or reproducibility rules."""


@dataclass(frozen=True)
class ArtifactResult:
    paths: tuple[str, ...]
    row_count: int
    sha256: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "row_count": self.row_count,
            "sha256": dict(self.sha256),
        }


@dataclass(frozen=True)
class MergeResult:
    output_path: str
    merged_count: int
    unchanged_count: int
    input_sha256: str
    worker_output_sha256: str
    canonical_input_sha256: str
    canonical_output_sha256: str
    assignments_output_path: str | None = None
    assignments_input_sha256: str | None = None
    assignments_output_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "output_path": self.output_path,
            "merged_count": self.merged_count,
            "unchanged_count": self.unchanged_count,
            "input_sha256": self.input_sha256,
            "worker_output_sha256": self.worker_output_sha256,
            "canonical_input_sha256": self.canonical_input_sha256,
            "canonical_output_sha256": self.canonical_output_sha256,
        }
        if self.assignments_output_path is not None:
            result.update({
                "assignments_output_path": self.assignments_output_path,
                "assignments_input_sha256": self.assignments_input_sha256,
                "assignments_output_sha256": self.assignments_output_sha256,
            })
        return result


@dataclass(frozen=True)
class AssignmentResult:
    canonical_output_path: str
    assignments_output_path: str
    input_path: str
    assigned_count: int
    canonical_input_sha256: str
    assignments_input_sha256: str
    canonical_output_sha256: str
    assignments_output_sha256: str
    input_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_output_path": self.canonical_output_path,
            "assignments_output_path": self.assignments_output_path,
            "input_path": self.input_path,
            "assigned_count": self.assigned_count,
            "canonical_input_sha256": self.canonical_input_sha256,
            "assignments_input_sha256": self.assignments_input_sha256,
            "canonical_output_sha256": self.canonical_output_sha256,
            "assignments_output_sha256": self.assignments_output_sha256,
            "input_sha256": self.input_sha256,
        }


@dataclass(frozen=True)
class TransitionResult:
    canonical_output_path: str
    assignments_output_path: str
    batch_id: str
    from_status: str
    to_status: str
    transitioned_count: int
    canonical_input_sha256: str
    assignments_input_sha256: str
    canonical_output_sha256: str
    assignments_output_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_output_path": self.canonical_output_path,
            "assignments_output_path": self.assignments_output_path,
            "batch_id": self.batch_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "transitioned_count": self.transitioned_count,
            "canonical_input_sha256": self.canonical_input_sha256,
            "assignments_input_sha256": self.assignments_input_sha256,
            "canonical_output_sha256": self.canonical_output_sha256,
            "assignments_output_sha256": self.assignments_output_sha256,
        }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_path(left: str | Path, right: str | Path) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    try:
        return left_path.samefile(right_path)
    except (FileNotFoundError, OSError):
        return left_path.resolve(strict=False) == right_path.resolve(strict=False)


def _require_new_path(path: str | Path, *inputs: str | Path) -> Path:
    destination = Path(path)
    for source in inputs:
        if _same_path(destination, source):
            raise BatchError(f"output must be a new path, not an input path: {destination}")
    if destination.exists():
        raise BatchError(f"refusing to overwrite existing path: {destination}")
    return destination


def _serialize_tsv(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    if not columns or len(columns) != len(set(columns)) or any(not name for name in columns):
        raise BatchError("TSV columns must be nonempty and unique")
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(escape_tsv(row.get(column, "")) for column in columns))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_new_atomic(path: str | Path, data: bytes) -> None:
    """Atomically publish bytes without an overwrite race (POSIX hard link)."""
    destination = _require_new_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise BatchError(f"refusing to overwrite existing path: {destination}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _publish_new_directory(destination: Path, files: Mapping[str, bytes]) -> None:
    """Publish individually atomic files inside an exclusively created directory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.mkdir()
    except FileExistsError as exc:
        raise BatchError(f"refusing to overwrite existing output directory: {destination}") from exc
    published: list[Path] = []
    try:
        for name, data in files.items():
            path = destination / name
            _write_new_atomic(path, data)
            published.append(path)
    except BaseException:
        # The directory was created by this call and contains only the known
        # files above, so cleanup cannot remove caller-owned data.
        for path in published:
            path.unlink(missing_ok=True)
        try:
            destination.rmdir()
        except OSError:
            pass
        raise


def _publish_new_files(files: Sequence[tuple[Path, bytes]]) -> None:
    """Publish a set of new files with rollback if any publication fails.

    POSIX has no transaction spanning directories.  Each destination is linked
    atomically from a fully synced sibling temporary file, and any links made
    by this call are removed if a later link fails.  Pre-existing paths are
    never replaced.
    """
    resolved = [path.resolve(strict=False) for path, _ in files]
    if len(resolved) != len(set(resolved)):
        raise BatchError("assignment output paths must be distinct")
    for destination, _ in files:
        _require_new_path(destination)

    temporaries: list[Path] = []
    published: list[Path] = []
    try:
        for destination, data in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent,
            )
            temporary = Path(temporary_name)
            temporaries.append(temporary)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        for (destination, _), temporary in zip(files, temporaries):
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise BatchError(f"refusing to overwrite existing path: {destination}") from exc
            published.append(destination)
    except BaseException as exc:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        if isinstance(exc, OSError) and not isinstance(exc, BatchError):
            raise BatchError(f"cannot publish assignment artifacts: {exc}") from exc
        raise
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)


def _load_segments(
    path: str | Path,
    *,
    strict_ids: bool = True,
    verify_source_hash: bool = True,
) -> tuple[list[str], list[dict[str, str]]]:
    try:
        columns, rows = read_tsv(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BatchError(f"cannot read segment TSV {path}: {exc}") from exc
    missing = [column for column in SEGMENTS_HEADERS if column not in columns]
    if missing:
        raise BatchError("missing required segment columns: " + ", ".join(missing))
    seen: set[str] = set()
    for number, row in enumerate(rows, 2):
        segment_id = row.get("segment_id", "")
        if not segment_id:
            raise BatchError(f"{path}:{number}: empty segment_id")
        if strict_ids and segment_id in seen:
            raise BatchError(f"{path}:{number}: duplicate segment_id {segment_id}")
        seen.add(segment_id)
        expected_hash = hashlib.sha256(row.get("source_text", "").encode("utf-8")).hexdigest()
        if verify_source_hash and row.get("source_hash", "") != expected_hash:
            raise BatchError(f"{path}:{number}: source_hash does not match source_text for {segment_id}")
    return columns, rows


def _load_assignments(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        columns, rows = read_tsv(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BatchError(f"cannot read assignments TSV {path}: {exc}") from exc
    missing = [column for column in ASSIGNMENTS_HEADERS if column not in columns]
    if missing:
        raise BatchError("missing required assignment columns: " + ", ".join(missing))
    seen: set[str] = set()
    for number, row in enumerate(rows, 2):
        assignment_id = row.get("assignment_id", "")
        if not assignment_id:
            raise BatchError(f"{path}:{number}: empty assignment_id")
        if assignment_id in seen:
            raise BatchError(f"{path}:{number}: duplicate assignment_id {assignment_id}")
        seen.add(assignment_id)
    report = validate_assignments(rows)
    if not report.ok:
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in report.errors)
        raise BatchError(f"invalid assignments TSV: {details}")
    return columns, rows


def _require_canonical_serialization(
    path: str | Path,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    """Ensure rewriting changed rows cannot normalize unrelated input bytes."""
    if Path(path).read_bytes() != _serialize_tsv(columns, rows):
        raise BatchError(f"{label} TSV is not in canonical LF/UTF-8/escape serialization")


def _normalize_ids(segment_ids: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in segment_ids:
        segment_id = str(raw).strip()
        if not segment_id:
            raise BatchError("segment ID list contains an empty ID")
        if segment_id in seen:
            raise BatchError(f"duplicate requested segment ID: {segment_id}")
        seen.add(segment_id)
        result.append(segment_id)
    if not result:
        raise BatchError("segment ID list is empty")
    return result


def _selected_rows(canonical_path: str | Path, segment_ids: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    columns, rows = _load_segments(canonical_path)
    requested = _normalize_ids(segment_ids)
    requested_set = set(requested)
    available = {row["segment_id"] for row in rows}
    missing = [segment_id for segment_id in requested if segment_id not in available]
    if missing:
        raise BatchError("requested segment IDs are missing from canonical TSV: " + ", ".join(missing))
    # Canonical order, rather than request order, is the deterministic order.
    selected = [dict(row) for row in rows if row["segment_id"] in requested_set]
    return columns, selected


def create_batch_snapshot(
    canonical_path: str | Path,
    segment_ids: Iterable[str],
    output_path: str | Path,
) -> ArtifactResult:
    """Create an immutable worker input snapshot at a new explicit path."""
    destination = _require_new_path(output_path, canonical_path)
    columns, rows = _selected_rows(canonical_path, segment_ids)
    _write_new_atomic(destination, _serialize_tsv(columns, rows))
    digest = sha256_file(destination)
    return ArtifactResult((str(destination),), len(rows), {str(destination): digest})


def _report_template(batch_id: str, input_hash: str, output_hash: str, row_count: int) -> str:
    return f"""# Worker Report — {batch_id}

- `batch_id`: {batch_id}
- `approval_status`: NOT_APPROVED
- `input_sha256`: {input_hash}
- `output_sha256`: {output_hash}

## Counts

| metric | count |
|---|---:|
| input | {row_count} |
| translated | 0 |
| blocked | 0 |
| provisional | 0 |

## Modified paths

```text
output.tsv
REPORT.md
```

## Glossary proposals

| source | proposed_target | segment_ids | rationale |
|---|---|---|---|

## Uncertainties

| segment_id | question | provisional_target | risk |
|---|---|---|---|

## Format or extraction issues

| segment_id | issue | evidence | blocking |
|---|---|---|---:|

## Validation

| validation | result | evidence_path |
|---|---|---|

## Handoff summary

- `ready_for_review`: false
- `blockers`:
- `recommended_next_action`:
"""


def prepare_worker_templates(
    input_path: str | Path,
    output_dir: str | Path,
    batch_id: str,
) -> ArtifactResult:
    """Create output.tsv and REPORT.md in a wholly new directory."""
    if not batch_id or not batch_id.startswith("BATCH-"):
        raise BatchError("batch_id must start with BATCH-")
    destination = Path(output_dir)
    if destination.exists():
        raise BatchError(f"refusing to overwrite existing output directory: {destination}")
    columns, rows = _load_segments(input_path)
    output_data = _serialize_tsv(columns, rows)
    input_hash = sha256_file(input_path)
    output_hash = hashlib.sha256(output_data).hexdigest()
    report_data = _report_template(batch_id, input_hash, output_hash, len(rows)).encode("utf-8")

    _publish_new_directory(destination, {
        "output.tsv": output_data,
        "REPORT.md": report_data,
    })
    paths = (str(destination / "output.tsv"), str(destination / "REPORT.md"))
    return ArtifactResult(paths, len(rows), {
        paths[0]: sha256_file(destination / "output.tsv"),
        paths[1]: sha256_file(destination / "REPORT.md"),
    })


def create_batch_workspace(
    canonical_path: str | Path,
    segment_ids: Iterable[str],
    output_dir: str | Path,
    batch_id: str,
) -> ArtifactResult:
    """Atomically create input.tsv, output.tsv, and REPORT.md together."""
    if not batch_id or not batch_id.startswith("BATCH-"):
        raise BatchError("batch_id must start with BATCH-")
    destination = Path(output_dir)
    if destination.exists():
        raise BatchError(f"refusing to overwrite existing output directory: {destination}")
    if _same_path(destination, canonical_path):
        raise BatchError("workspace directory cannot be the canonical TSV path")
    columns, rows = _selected_rows(canonical_path, segment_ids)
    input_data = _serialize_tsv(columns, rows)
    input_hash = hashlib.sha256(input_data).hexdigest()
    report_data = _report_template(batch_id, input_hash, input_hash, len(rows)).encode("utf-8")

    _publish_new_directory(destination, {
        "input.tsv": input_data,
        "output.tsv": input_data,
        "REPORT.md": report_data,
    })
    paths = tuple(str(destination / name) for name in ("input.tsv", "output.tsv", "REPORT.md"))
    return ArtifactResult(paths, len(rows), {path: sha256_file(path) for path in paths})


def _is_write_assignment(row: Mapping[str, str]) -> bool:
    role = row.get("role", "").strip().lower()
    if "review" in role or role in {"qa", "validator", "validation_worker"}:
        return False
    return True


def _require_timezone_timestamp(value: str, label: str) -> None:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BatchError(f"{label} must be an ISO-8601 timestamp with an explicit timezone") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BatchError(f"{label} must include an explicit timezone")


def _translation_batch_rows(
    canonical_rows: Sequence[Mapping[str, str]],
    assignment_rows: Sequence[Mapping[str, str]],
    batch_id: str,
) -> tuple[list[Mapping[str, str]], list[Mapping[str, str]]]:
    """Return the exact canonical/write-assignment row set for one batch."""
    if not _BATCH_ID.fullmatch(batch_id):
        raise BatchError("batch_id must match BATCH-[A-Z0-9-]+")
    canonical_batch = [row for row in canonical_rows if row.get("batch_id", "") == batch_id]
    translation_assignments = [
        row for row in assignment_rows
        if row.get("batch_id", "") == batch_id
        and row.get("role", "").strip().lower() == ASSIGNMENT_ROLE
    ]
    if not canonical_batch:
        raise BatchError(f"canonical TSV has no rows owned by {batch_id}")
    if not translation_assignments:
        raise BatchError(f"assignments TSV has no translation_worker rows for {batch_id}")
    canonical_ids = [row.get("segment_id", "") for row in canonical_batch]
    assignment_ids = [row.get("segment_id", "") for row in translation_assignments]
    if len(assignment_ids) != len(set(assignment_ids)):
        raise BatchError(f"batch {batch_id} has duplicate translation_worker segment IDs")
    canonical_id_set = set(canonical_ids)
    assignment_id_set = set(assignment_ids)
    canonical_by_id = {row.get("segment_id", ""): row for row in canonical_rows}
    allowed_reclassified_ids = {
        row.get("segment_id", "")
        for row in translation_assignments
        if row.get("segment_id", "") not in canonical_id_set
        and row.get("status", "") == "OUT_OF_SCOPE"
        and canonical_by_id.get(row.get("segment_id", ""), {}).get("status") == "OUT_OF_SCOPE"
    }
    translation_assignments = [
        row for row in translation_assignments
        if row.get("segment_id", "") not in allowed_reclassified_ids
    ]
    assignment_id_set -= allowed_reclassified_ids
    if canonical_id_set != assignment_id_set:
        missing_assignments = sorted(canonical_id_set - assignment_id_set)
        extra_assignments = sorted(assignment_id_set - canonical_id_set)
        raise BatchError(
            f"batch {batch_id} canonical/translation_worker ID set mismatch; "
            f"missing assignments={missing_assignments}, extra assignments={extra_assignments}"
        )
    return canonical_batch, translation_assignments


def _validate_assignment_metadata(
    *,
    batch_id: str,
    assignee: str,
    model: str,
    reasoning_effort: str,
    base_commit: str,
    source_build: str,
    guidelines_revision: str,
    schema_version: str | int,
    assigned_at: str,
) -> str:
    if not _BATCH_ID.fullmatch(batch_id):
        raise BatchError("batch_id must match BATCH-[A-Z0-9-]+")
    for label, value in (
        ("assignee", assignee),
        ("model", model),
        ("reasoning_effort", reasoning_effort),
        ("source_build", source_build),
        ("guidelines_revision", guidelines_revision),
    ):
        if not value or value != value.strip():
            raise BatchError(f"{label} must be a nonempty, trimmed value")
    if not _COMMIT.fullmatch(base_commit):
        raise BatchError("base_commit must be a 7-64 character lowercase hexadecimal revision")
    schema = str(schema_version)
    if not schema.isascii() or not schema.isdigit() or int(schema) <= 0:
        raise BatchError("schema_version must be a positive integer")
    _require_timezone_timestamp(assigned_at, "assigned_at")
    return schema


def assign_batch(
    canonical_path: str | Path,
    assignments_path: str | Path,
    segment_ids: Iterable[str],
    new_canonical_path: str | Path,
    new_assignments_path: str | Path,
    input_path: str | Path,
    *,
    batch_id: str,
    assignee: str,
    model: str,
    reasoning_effort: str,
    base_commit: str,
    source_build: str,
    guidelines_revision: str,
    schema_version: str | int,
    assigned_at: str,
    worker_output_path: str | Path,
    expected_canonical_sha256: str,
    expected_assignments_sha256: str,
    japanese_allowlist: Any = None,
) -> AssignmentResult:
    """Assign exact rows and publish three mutually consistent new artifacts.

    The input files are SHA-pinned and never modified.  Selected rows must be
    ``EXTRACTED`` or ``READY`` and unowned.  The returned canonical contains
    the lifecycle/ownership transition, the assignments file records one
    translation write lock per selected row, and the worker input is an
    immutable snapshot of those transitioned rows in canonical order.
    """
    schema = _validate_assignment_metadata(
        batch_id=batch_id,
        assignee=assignee,
        model=model,
        reasoning_effort=reasoning_effort,
        base_commit=base_commit,
        source_build=source_build,
        guidelines_revision=guidelines_revision,
        schema_version=schema_version,
        assigned_at=assigned_at,
    )
    for label, digest in (
        ("canonical", expected_canonical_sha256),
        ("assignments", expected_assignments_sha256),
    ):
        if not _SHA256.fullmatch(digest):
            raise BatchError(f"expected {label} SHA-256 must be 64 lowercase hexadecimal characters")

    canonical_input_hash = sha256_file(canonical_path)
    assignments_input_hash = sha256_file(assignments_path)
    for label, expected, actual in (
        ("canonical", expected_canonical_sha256, canonical_input_hash),
        ("assignments", expected_assignments_sha256, assignments_input_hash),
    ):
        if expected != actual:
            raise BatchError(f"{label} SHA-256 pin mismatch: expected {expected}, got {actual}")

    canonical_columns, canonical_rows = _load_segments(canonical_path)
    assignment_columns, assignment_rows = _load_assignments(assignments_path)
    _require_canonical_serialization(
        canonical_path, canonical_columns, canonical_rows, label="canonical segments",
    )
    _require_canonical_serialization(
        assignments_path, assignment_columns, assignment_rows, label="assignments",
    )
    segment_report = validate_segments(
        canonical_rows, japanese_allowlist=japanese_allowlist,
    )
    if not segment_report.ok:
        details = "; ".join(
            f"{issue.code}{'/' + issue.segment_id if issue.segment_id else ''}: {issue.message}"
            for issue in segment_report.errors[:10]
        )
        raise BatchError(f"invalid canonical segment schema/data: {details}")

    requested = _normalize_ids(segment_ids)
    requested_set = set(requested)
    canonical_by_id = {row["segment_id"]: row for row in canonical_rows}
    missing = [segment_id for segment_id in requested if segment_id not in canonical_by_id]
    if missing:
        raise BatchError("requested segment IDs are missing from canonical TSV: " + ", ".join(missing))

    active_by_segment: dict[str, list[str]] = {}
    for row in assignment_rows:
        if row.get("status", "").upper() in ACTIVE_STATUSES and _is_write_assignment(row):
            active_by_segment.setdefault(row.get("segment_id", ""), []).append(row["assignment_id"])
    overlaps = [segment_id for segment_id in requested if segment_id in active_by_segment]
    if overlaps:
        details = ", ".join(
            f"{segment_id} ({'/'.join(active_by_segment[segment_id])})" for segment_id in overlaps
        )
        raise BatchError("active write assignment overlap: " + details)

    updated_by_id: dict[str, dict[str, str]] = {}
    for segment_id in requested:
        row = canonical_by_id[segment_id]
        status = row.get("status", "")
        if status not in ASSIGNABLE_STATUSES:
            raise BatchError(
                f"segment {segment_id} has non-assignable status {status!r}; "
                "expected EXTRACTED or READY"
            )
        if row.get("batch_id", ""):
            raise BatchError(f"segment {segment_id} already has batch_id {row['batch_id']!r}")
        updated = dict(row)
        updated["batch_id"] = batch_id
        updated["status"] = "ASSIGNED"
        updated_by_id[segment_id] = updated

    new_canonical_rows = [
        updated_by_id.get(row["segment_id"], dict(row)) for row in canonical_rows
    ]
    selected_rows = [
        dict(row) for row in new_canonical_rows if row["segment_id"] in requested_set
    ]
    if len(selected_rows) != len(requested):
        raise BatchError("internal assignment row-count mismatch")
    input_data = _serialize_tsv(canonical_columns, selected_rows)
    input_hash = hashlib.sha256(input_data).hexdigest()

    pin_note = json.dumps({
        "assignment_schema_version": schema,
        "assignments_revision": assignments_input_hash,
        "input_sha256": input_hash,
        "segments_revision": canonical_input_hash,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    existing_assignment_ids = {row["assignment_id"] for row in assignment_rows}
    generated_assignment_rows: list[dict[str, str]] = []
    for row in selected_rows:
        segment_id = row["segment_id"]
        identity = "\0".join((
            "assignment-v1", batch_id, segment_id, canonical_input_hash,
            assignments_input_hash, assignee, model, reasoning_effort, assigned_at,
        ))
        assignment_id = "ASN-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20].upper()
        if assignment_id in existing_assignment_ids:
            raise BatchError(f"generated assignment_id already exists: {assignment_id}")
        existing_assignment_ids.add(assignment_id)
        assignment = {column: "" for column in assignment_columns}
        assignment.update({
            "assignment_id": assignment_id,
            "batch_id": batch_id,
            "segment_id": segment_id,
            "role": ASSIGNMENT_ROLE,
            "assignee": assignee,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "base_commit": base_commit,
            "source_build": source_build,
            "guidelines_revision": guidelines_revision,
            "status": "ASSIGNED",
            "assigned_at": assigned_at,
            "completed_at": "",
            "input_path": str(input_path),
            "output_path": str(worker_output_path),
            "reviewer_id": "",
            "notes": pin_note,
        })
        generated_assignment_rows.append(assignment)

    new_assignment_rows = [dict(row) for row in assignment_rows] + generated_assignment_rows
    assignment_report = validate_assignments(new_assignment_rows)
    if not assignment_report.ok:
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in assignment_report.errors)
        raise BatchError(f"generated assignments fail validation: {details}")

    canonical_data = _serialize_tsv(canonical_columns, new_canonical_rows)
    assignments_data = _serialize_tsv(assignment_columns, new_assignment_rows)
    destinations = (
        Path(new_canonical_path), Path(new_assignments_path), Path(input_path),
    )
    for destination in destinations:
        _require_new_path(destination, canonical_path, assignments_path, worker_output_path)
    if Path(worker_output_path).exists():
        raise BatchError(f"worker output path must not already exist: {worker_output_path}")
    _publish_new_files(tuple(zip(destinations, (canonical_data, assignments_data, input_data))))

    return AssignmentResult(
        canonical_output_path=str(destinations[0]),
        assignments_output_path=str(destinations[1]),
        input_path=str(destinations[2]),
        assigned_count=len(selected_rows),
        canonical_input_sha256=canonical_input_hash,
        assignments_input_sha256=assignments_input_hash,
        canonical_output_sha256=hashlib.sha256(canonical_data).hexdigest(),
        assignments_output_sha256=hashlib.sha256(assignments_data).hexdigest(),
        input_sha256=input_hash,
    )


def transition_batch(
    canonical_path: str | Path,
    assignments_path: str | Path,
    new_canonical_path: str | Path,
    new_assignments_path: str | Path,
    *,
    batch_id: str,
    from_status: str,
    to_status: str,
    expected_canonical_sha256: str,
    expected_assignments_sha256: str,
    completed_at: str | None = None,
) -> TransitionResult:
    """Advance one exact batch by one lifecycle edge in both canonical TSVs."""
    if (from_status, to_status) not in LIFECYCLE_TRANSITIONS:
        raise BatchError(f"disallowed lifecycle transition: {from_status} -> {to_status}")
    if from_status == "ASSIGNED":
        if not completed_at:
            raise BatchError("ASSIGNED -> DRAFTED requires completed_at")
        _require_timezone_timestamp(completed_at, "completed_at")
    elif completed_at is not None:
        raise BatchError("completed_at may only be supplied for ASSIGNED -> DRAFTED")
    for label, digest in (
        ("canonical", expected_canonical_sha256),
        ("assignments", expected_assignments_sha256),
    ):
        if not _SHA256.fullmatch(digest):
            raise BatchError(f"expected {label} SHA-256 must be 64 lowercase hexadecimal characters")

    canonical_input_hash = sha256_file(canonical_path)
    assignments_input_hash = sha256_file(assignments_path)
    for label, expected, actual in (
        ("canonical", expected_canonical_sha256, canonical_input_hash),
        ("assignments", expected_assignments_sha256, assignments_input_hash),
    ):
        if expected != actual:
            raise BatchError(f"{label} SHA-256 pin mismatch: expected {expected}, got {actual}")

    canonical_columns, canonical_rows = _load_segments(canonical_path)
    assignment_columns, assignment_rows = _load_assignments(assignments_path)
    _require_canonical_serialization(
        canonical_path, canonical_columns, canonical_rows, label="canonical segments",
    )
    _require_canonical_serialization(
        assignments_path, assignment_columns, assignment_rows, label="assignments",
    )
    canonical_batch, translation_assignments = _translation_batch_rows(
        canonical_rows, assignment_rows, batch_id,
    )
    canonical_statuses = {row.get("status", "") for row in canonical_batch}
    assignment_statuses = {row.get("status", "") for row in translation_assignments}
    if canonical_statuses != {from_status}:
        raise BatchError(
            f"batch {batch_id} canonical rows have mixed/wrong status: {sorted(canonical_statuses)}; "
            f"expected {from_status}"
        )
    if assignment_statuses != {from_status}:
        raise BatchError(
            f"batch {batch_id} translation_worker assignments have mixed/wrong status: "
            f"{sorted(assignment_statuses)}; expected {from_status}"
        )
    if from_status == "ASSIGNED":
        nonempty_completion = [
            row["assignment_id"] for row in translation_assignments
            if row.get("completed_at", "")
        ]
        if nonempty_completion:
            raise BatchError(
                "ASSIGNED translation_worker assignments already have completed_at: "
                + ", ".join(nonempty_completion)
            )
    else:
        missing_completion = [
            row["assignment_id"] for row in translation_assignments
            if not row.get("completed_at", "")
        ]
        if missing_completion:
            raise BatchError(
                f"{from_status} translation_worker assignments are missing completed_at: "
                + ", ".join(missing_completion)
            )
        for row in translation_assignments:
            _require_timezone_timestamp(row["completed_at"], "assignment completed_at")

    canonical_ids = {row["segment_id"] for row in canonical_batch}
    assignment_ids = {row["assignment_id"] for row in translation_assignments}
    new_canonical_rows: list[dict[str, str]] = []
    for row in canonical_rows:
        updated = dict(row)
        if row["segment_id"] in canonical_ids:
            updated["status"] = to_status
            changed = {column for column in canonical_columns if updated.get(column, "") != row.get(column, "")}
            if changed != {"status"}:
                raise BatchError(f"internal transition changed forbidden canonical fields: {sorted(changed)}")
        new_canonical_rows.append(updated)
    new_assignment_rows: list[dict[str, str]] = []
    for row in assignment_rows:
        updated = dict(row)
        if row["assignment_id"] in assignment_ids:
            updated["status"] = to_status
            if from_status == "ASSIGNED":
                updated["completed_at"] = completed_at or ""
            changed = {column for column in assignment_columns if updated.get(column, "") != row.get(column, "")}
            allowed = {"status", "completed_at"} if from_status == "ASSIGNED" else {"status"}
            if changed != allowed:
                raise BatchError(f"internal transition changed forbidden assignment fields: {sorted(changed)}")
        new_assignment_rows.append(updated)

    canonical_data = _serialize_tsv(canonical_columns, new_canonical_rows)
    assignments_data = _serialize_tsv(assignment_columns, new_assignment_rows)
    destinations = (Path(new_canonical_path), Path(new_assignments_path))
    for destination in destinations:
        _require_new_path(destination, canonical_path, assignments_path)
    _publish_new_files(tuple(zip(destinations, (canonical_data, assignments_data))))
    return TransitionResult(
        canonical_output_path=str(destinations[0]),
        assignments_output_path=str(destinations[1]),
        batch_id=batch_id,
        from_status=from_status,
        to_status=to_status,
        transitioned_count=len(canonical_ids),
        canonical_input_sha256=canonical_input_hash,
        assignments_input_sha256=assignments_input_hash,
        canonical_output_sha256=hashlib.sha256(canonical_data).hexdigest(),
        assignments_output_sha256=hashlib.sha256(assignments_data).hexdigest(),
    )


def validate_worker_output(
    input_path: str | Path,
    output_path: str | Path,
    *,
    japanese_allowlist: Any = None,
) -> ValidationReport:
    """Apply existing QA rules plus exact column, row-order, and extension checks."""
    report = validate_batch_output(input_path, output_path)
    # Malformed worker data belongs in a ValidationReport, not an exception;
    # existing QA detects duplicate IDs, changed hashes, and bad hash syntax.
    try:
        input_columns, input_rows = _load_segments(input_path)
    except BatchError as exc:
        report.add("BATCH_INPUT_INVALID", str(exc))
        return report
    try:
        output_columns, output_rows = _load_segments(
            output_path, strict_ids=False, verify_source_hash=False,
        )
    except BatchError as exc:
        report.add("BATCH_OUTPUT_INVALID", str(exc))
        return report
    report.extend(validate_segments(output_rows, japanese_allowlist=japanese_allowlist))
    if output_columns != input_columns:
        report.add("BATCH_COLUMNS", "worker output columns/order differ from input snapshot")
    input_ids = [row["segment_id"] for row in input_rows]
    output_ids = [row["segment_id"] for row in output_rows]
    if output_ids != input_ids:
        report.add("BATCH_ROW_ORDER", "worker output row order differs from input snapshot")
    if output_columns == input_columns:
        input_by_id = {row["segment_id"]: row for row in input_rows}
        for row in output_rows:
            segment_id = row["segment_id"]
            before = input_by_id.get(segment_id)
            if before is None:
                continue
            for column in input_columns:
                if column not in WORKER_MUTABLE_FIELDS and before.get(column, "") != row.get(column, ""):
                    report.add(
                        "BATCH_IMMUTABLE_FIELD",
                        f"worker output changed {column}",
                        segment_id=segment_id,
                    )
    return report


def _raise_for_report(report: ValidationReport) -> None:
    if report.ok:
        return
    details = "; ".join(
        f"{issue.code}{'/' + issue.segment_id if issue.segment_id else ''}: {issue.message}"
        for issue in report.errors[:10]
    )
    if len(report.errors) > 10:
        details += f"; ... {len(report.errors) - 10} more"
    raise BatchError(f"worker output validation failed: {details}")


def _parse_revision(raw: str, segment_id: str) -> int:
    if not raw or not raw.isascii() or not raw.isdigit():
        raise BatchError(f"invalid target_revision for {segment_id}: {raw!r}")
    revision = int(raw)
    if revision < 0:
        raise BatchError(f"negative target_revision for {segment_id}")
    return revision


def merge_approved_output(
    canonical_path: str | Path,
    input_path: str | Path,
    worker_output_path: str | Path,
    new_canonical_path: str | Path,
    *,
    batch_id: str,
    approval_status: str,
    expected_canonical_sha256: str,
    expected_input_sha256: str,
    expected_output_sha256: str,
    assignments_path: str | Path | None = None,
    new_assignments_path: str | Path | None = None,
    expected_assignments_sha256: str | None = None,
    japanese_allowlist: Any = None,
) -> MergeResult:
    """Merge an explicitly approved proposal to a new canonical TSV.

    Every selected current canonical row must still be owned by ``batch_id``
    and be ``REVIEWED``.  The merge changes only the five fields enumerated by
    :data:`MERGE_MUTABLE_FIELDS`; ``target_revision`` increments exactly once.
    """
    destination = _require_new_path(
        new_canonical_path, canonical_path, input_path, worker_output_path,
    )
    synchronization_values = (
        assignments_path, new_assignments_path, expected_assignments_sha256,
    )
    synchronize_assignments = all(value is not None for value in synchronization_values)
    if any(value is not None for value in synchronization_values) and not synchronize_assignments:
        raise BatchError(
            "assignments_path, new_assignments_path, and expected_assignments_sha256 "
            "must be supplied together"
        )
    if approval_status != APPROVAL_STATUS:
        raise BatchError("approval_status must be exactly APPROVED")
    if not batch_id or not batch_id.startswith("BATCH-"):
        raise BatchError("batch_id must start with BATCH-")

    actual_canonical_hash = sha256_file(canonical_path)
    actual_input_hash = sha256_file(input_path)
    actual_output_hash = sha256_file(worker_output_path)
    pins = (
        ("canonical", expected_canonical_sha256, actual_canonical_hash),
        ("input", expected_input_sha256, actual_input_hash),
        ("worker output", expected_output_sha256, actual_output_hash),
    )
    for label, expected, actual in pins:
        if expected != actual:
            raise BatchError(f"{label} SHA-256 pin mismatch: expected {expected}, got {actual}")

    actual_assignments_hash: str | None = None
    assignment_destination: Path | None = None
    assignment_columns: list[str] | None = None
    assignment_rows: list[dict[str, str]] | None = None
    if synchronize_assignments:
        assert assignments_path is not None
        assert new_assignments_path is not None
        assert expected_assignments_sha256 is not None
        if not _SHA256.fullmatch(expected_assignments_sha256):
            raise BatchError("expected assignments SHA-256 must be 64 lowercase hexadecimal characters")
        actual_assignments_hash = sha256_file(assignments_path)
        if expected_assignments_sha256 != actual_assignments_hash:
            raise BatchError(
                "assignments SHA-256 pin mismatch: "
                f"expected {expected_assignments_sha256}, got {actual_assignments_hash}"
            )
        assignment_destination = _require_new_path(
            new_assignments_path,
            canonical_path,
            input_path,
            worker_output_path,
            assignments_path,
            new_canonical_path,
        )
        assignment_columns, assignment_rows = _load_assignments(assignments_path)
        _require_canonical_serialization(
            assignments_path, assignment_columns, assignment_rows, label="assignments",
        )

    _raise_for_report(validate_worker_output(
        input_path, worker_output_path, japanese_allowlist=japanese_allowlist,
    ))
    canonical_columns, canonical_rows = _load_segments(canonical_path)
    input_columns, input_rows = _load_segments(input_path)
    output_columns, output_rows = _load_segments(worker_output_path)
    _require_canonical_serialization(
        canonical_path, canonical_columns, canonical_rows, label="canonical segments",
    )
    if input_columns != canonical_columns or output_columns != canonical_columns:
        raise BatchError("canonical, input, and worker output columns/order must match exactly")

    canonical_by_id = {row["segment_id"]: row for row in canonical_rows}
    input_by_id = {row["segment_id"]: row for row in input_rows}
    output_by_id = {row["segment_id"]: row for row in output_rows}
    selected_ids = set(input_by_id)
    missing = sorted(selected_ids - set(canonical_by_id))
    if missing:
        raise BatchError("snapshot rows no longer exist in canonical TSV: " + ", ".join(missing))

    replacements: dict[str, dict[str, str]] = {}
    immutable_current_fields = set(canonical_columns) - MERGE_MUTABLE_FIELDS
    for segment_id in (row["segment_id"] for row in input_rows):
        current = canonical_by_id[segment_id]
        snapshot = input_by_id[segment_id]
        proposal = output_by_id[segment_id]
        for field in immutable_current_fields:
            if current.get(field, "") != snapshot.get(field, ""):
                raise BatchError(f"canonical drift in {field} for {segment_id}")
        if current["source_hash"] != snapshot["source_hash"]:
            raise BatchError(f"source_hash drift for {segment_id}")
        if current.get("target_text", "") != snapshot.get("target_text", ""):
            raise BatchError(f"canonical target_text drift for {segment_id}")
        if current.get("notes", "") != snapshot.get("notes", ""):
            raise BatchError(f"canonical notes drift for {segment_id}")
        if current.get("target_revision", "") != snapshot.get("target_revision", ""):
            raise BatchError(f"canonical target_revision drift for {segment_id}")
        if current.get("batch_id", "") != batch_id or snapshot.get("batch_id", "") != batch_id:
            raise BatchError(f"segment {segment_id} is not owned by {batch_id}")
        current_status = current.get("status", "")
        if APPROVAL_TRANSITIONS.get(current_status) != APPROVAL_STATUS:
            raise BatchError(f"disallowed lifecycle transition for {segment_id}: {current_status} -> APPROVED")
        snapshot_status = snapshot.get("status", "")
        if snapshot_status not in {"ASSIGNED", current_status}:
            raise BatchError(
                f"canonical status drift for {segment_id}: "
                f"snapshot {snapshot_status!r}, current {current_status!r}"
            )
        if not proposal.get("target_text", ""):
            raise BatchError(f"approved target_text is empty for {segment_id}")

        updated = dict(current)
        updated["target_text"] = proposal.get("target_text", "")
        updated["notes"] = proposal.get("notes", "")
        updated["status"] = APPROVAL_STATUS
        updated["batch_id"] = batch_id
        updated["target_revision"] = str(_parse_revision(current["target_revision"], segment_id) + 1)
        changed = {field for field in canonical_columns if updated.get(field, "") != current.get(field, "")}
        forbidden = changed - MERGE_MUTABLE_FIELDS
        if forbidden:
            raise BatchError(f"internal merge attempted forbidden fields for {segment_id}: {sorted(forbidden)}")
        replacements[segment_id] = updated

    merged_rows = [replacements.get(row["segment_id"], dict(row)) for row in canonical_rows]
    canonical_data = _serialize_tsv(canonical_columns, merged_rows)
    assignments_output_hash: str | None = None
    if synchronize_assignments:
        assert assignment_columns is not None
        assert assignment_rows is not None
        assert assignment_destination is not None
        canonical_batch, translation_assignments = _translation_batch_rows(
            canonical_rows, assignment_rows, batch_id,
        )
        canonical_batch_ids = {row["segment_id"] for row in canonical_batch}
        if canonical_batch_ids != selected_ids:
            raise BatchError(
                f"approved merge must cover the exact batch ID set; "
                f"canonical batch={sorted(canonical_batch_ids)}, snapshot={sorted(selected_ids)}"
            )
        assignment_statuses = {row.get("status", "") for row in translation_assignments}
        if assignment_statuses != {"REVIEWED"}:
            raise BatchError(
                "translation_worker assignments must all be REVIEWED before approval; "
                f"got {sorted(assignment_statuses)}"
            )
        for row in translation_assignments:
            if not row.get("completed_at", ""):
                raise BatchError(
                    f"REVIEWED translation_worker assignment is missing completed_at: "
                    f"{row['assignment_id']}"
                )
            _require_timezone_timestamp(row["completed_at"], "assignment completed_at")
        translation_assignment_ids = {row["assignment_id"] for row in translation_assignments}
        approved_assignment_rows: list[dict[str, str]] = []
        for row in assignment_rows:
            updated = dict(row)
            if row["assignment_id"] in translation_assignment_ids:
                updated["status"] = APPROVAL_STATUS
                changed = {
                    column for column in assignment_columns
                    if updated.get(column, "") != row.get(column, "")
                }
                if changed != {"status"}:
                    raise BatchError(
                        f"internal approval changed forbidden assignment fields: {sorted(changed)}"
                    )
            approved_assignment_rows.append(updated)
        assignments_data = _serialize_tsv(assignment_columns, approved_assignment_rows)
        _publish_new_files(((destination, canonical_data), (assignment_destination, assignments_data)))
        assignments_output_hash = hashlib.sha256(assignments_data).hexdigest()
    else:
        _write_new_atomic(destination, canonical_data)
    return MergeResult(
        str(destination),
        len(replacements),
        len(canonical_rows) - len(replacements),
        actual_input_hash,
        actual_output_hash,
        actual_canonical_hash,
        hashlib.sha256(canonical_data).hexdigest(),
        str(assignment_destination) if assignment_destination is not None else None,
        actual_assignments_hash,
        assignments_output_hash,
    )


def _ids_from_args(args: argparse.Namespace) -> list[str]:
    ids = list(args.segment_id or [])
    if args.segment_ids_file:
        try:
            lines = args.segment_ids_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise BatchError(f"cannot read segment ID file: {exc}") from exc
        ids.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))
    return _normalize_ids(ids)


def _add_id_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--segment-id", action="append", help="exact editable segment ID; repeatable")
    parser.add_argument("--segment-ids-file", type=Path, help="UTF-8 file with one exact segment ID per line")


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Create, validate, and safely merge localization batches")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="create a new input TSV snapshot")
    snapshot.add_argument("--canonical", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    _add_id_args(snapshot)

    prepare = subparsers.add_parser("prepare", help="create output/report templates in a new directory")
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--batch-id", required=True)

    create = subparsers.add_parser("create", help="atomically create a complete batch workspace")
    create.add_argument("--canonical", type=Path, required=True)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--batch-id", required=True)
    _add_id_args(create)

    assign = subparsers.add_parser(
        "assign", help="publish a pinned canonical/assignments/input assignment transaction",
    )
    assign.add_argument("--canonical", type=Path, required=True)
    assign.add_argument("--assignments", type=Path, required=True)
    assign.add_argument("--new-canonical", type=Path, required=True)
    assign.add_argument("--new-assignments", type=Path, required=True)
    assign.add_argument("--input-output", type=Path, required=True)
    assign.add_argument("--worker-output", type=Path, required=True)
    assign.add_argument("--batch-id", required=True)
    assign.add_argument("--assignee", required=True)
    assign.add_argument("--model", required=True)
    assign.add_argument("--reasoning-effort", required=True)
    assign.add_argument("--base-commit", required=True)
    assign.add_argument("--source-build", required=True)
    assign.add_argument("--guidelines-revision", required=True)
    assign.add_argument("--schema-version", required=True)
    assign.add_argument("--assigned-at", required=True)
    assign.add_argument("--expected-canonical-sha256", required=True)
    assign.add_argument("--expected-assignments-sha256", required=True)
    assign.add_argument(
        "--allowlist", type=Path,
        help="JSON object mapping segment IDs to exact preserved Japanese fragments",
    )
    _add_id_args(assign)

    transition = subparsers.add_parser(
        "transition", help="advance one exact batch in canonical and assignments TSVs",
    )
    transition.add_argument("--canonical", type=Path, required=True)
    transition.add_argument("--assignments", type=Path, required=True)
    transition.add_argument("--new-canonical", type=Path, required=True)
    transition.add_argument("--new-assignments", type=Path, required=True)
    transition.add_argument("--batch-id", required=True)
    transition.add_argument("--from-status", required=True)
    transition.add_argument("--to-status", required=True)
    transition.add_argument("--completed-at")
    transition.add_argument("--expected-canonical-sha256", required=True)
    transition.add_argument("--expected-assignments-sha256", required=True)

    validate = subparsers.add_parser("validate", help="validate a worker output against its input")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    validate.add_argument(
        "--allowlist", type=Path,
        help="JSON object mapping segment IDs to exact preserved Japanese fragments",
    )

    merge = subparsers.add_parser("merge-approved", help="merge an approved output to a new canonical path")
    merge.add_argument("--canonical", type=Path, required=True)
    merge.add_argument("--input", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--new-canonical", type=Path, required=True)
    merge.add_argument("--batch-id", required=True)
    merge.add_argument("--approval-status", required=True)
    merge.add_argument("--expected-canonical-sha256", required=True)
    merge.add_argument("--expected-input-sha256", required=True)
    merge.add_argument("--expected-output-sha256", required=True)
    merge.add_argument("--assignments", type=Path)
    merge.add_argument("--new-assignments", type=Path)
    merge.add_argument("--expected-assignments-sha256")
    merge.add_argument(
        "--allowlist", type=Path,
        help="JSON object mapping segment IDs to exact preserved Japanese fragments",
    )

    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result: Any = create_batch_snapshot(args.canonical, _ids_from_args(args), args.output)
        elif args.command == "prepare":
            result = prepare_worker_templates(args.input, args.output_dir, args.batch_id)
        elif args.command == "create":
            result = create_batch_workspace(args.canonical, _ids_from_args(args), args.output_dir, args.batch_id)
        elif args.command == "assign":
            allowlist = load_japanese_allowlist(args.allowlist) if args.allowlist else None
            result = assign_batch(
                args.canonical,
                args.assignments,
                _ids_from_args(args),
                args.new_canonical,
                args.new_assignments,
                args.input_output,
                batch_id=args.batch_id,
                assignee=args.assignee,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                base_commit=args.base_commit,
                source_build=args.source_build,
                guidelines_revision=args.guidelines_revision,
                schema_version=args.schema_version,
                assigned_at=args.assigned_at,
                worker_output_path=args.worker_output,
                expected_canonical_sha256=args.expected_canonical_sha256,
                expected_assignments_sha256=args.expected_assignments_sha256,
                japanese_allowlist=allowlist,
            )
        elif args.command == "transition":
            result = transition_batch(
                args.canonical,
                args.assignments,
                args.new_canonical,
                args.new_assignments,
                batch_id=args.batch_id,
                from_status=args.from_status,
                to_status=args.to_status,
                completed_at=args.completed_at,
                expected_canonical_sha256=args.expected_canonical_sha256,
                expected_assignments_sha256=args.expected_assignments_sha256,
            )
        elif args.command == "validate":
            allowlist = load_japanese_allowlist(args.allowlist) if args.allowlist else None
            report = validate_worker_output(
                args.input, args.output, japanese_allowlist=allowlist,
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return 0 if report.ok else 1
        else:
            allowlist = load_japanese_allowlist(args.allowlist) if args.allowlist else None
            result = merge_approved_output(
                args.canonical,
                args.input,
                args.output,
                args.new_canonical,
                batch_id=args.batch_id,
                approval_status=args.approval_status,
                expected_canonical_sha256=args.expected_canonical_sha256,
                expected_input_sha256=args.expected_input_sha256,
                expected_output_sha256=args.expected_output_sha256,
                assignments_path=args.assignments,
                new_assignments_path=args.new_assignments,
                expected_assignments_sha256=args.expected_assignments_sha256,
                japanese_allowlist=allowlist,
            )
    except (BatchError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    payload = {"ok": True, **result.as_dict()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
