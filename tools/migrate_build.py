#!/usr/bin/env python3
"""Fail-closed canonical segment migration between exact game builds.

The fresh extractor output is always authoritative for source identity and
structure.  Translation/lifecycle data is carried only when an old row has an
unchanged stable ID or an unambiguous same-path, same-source move.  A row whose
source changed under the same ID retains its old target only as review input
and is forced to NEEDS_RECHECK.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence

try:
    from tools import segments
except ModuleNotFoundError:  # Direct execution: python tools/migrate_build.py
    import segments  # type: ignore[no-redef]


CLASS_UNCHANGED = "UNCHANGED"
CLASS_CHANGED_SAME_ID = "CHANGED_SAME_ID"
CLASS_MOVED_SAME_SOURCE = "MOVED_SAME_SOURCE"
CLASS_ADDED = "ADDED"
CLASS_REMOVED = "REMOVED"

CLASS_ORDER = (
    CLASS_UNCHANGED,
    CLASS_CHANGED_SAME_ID,
    CLASS_MOVED_SAME_SOURCE,
    CLASS_ADDED,
    CLASS_REMOVED,
)

# These are the only canonical fields whose old-build values can be carried.
# Every other known field comes from the fresh extractor output.
CARRY_FIELDS = (
    "target_text",
    "batch_id",
    "status",
    "target_revision",
    "decision_ids",
    "uncertainty_ids",
    "notes",
)

REQUIRED_FIELDS = frozenset(segments.SEGMENT_COLUMNS)
ASSIGNMENT_FIELDS = tuple(
    "assignment_id batch_id segment_id role assignee model reasoning_effort "
    "base_commit source_build guidelines_revision status assigned_at completed_at "
    "input_path output_path reviewer_id notes".split()
)


class MigrationError(ValueError):
    """The inputs cannot be migrated without guessing."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_rows(
    label: str,
    header: Sequence[str],
    rows: Sequence[Mapping[str, str]],
) -> dict[str, Mapping[str, str]]:
    missing = sorted(REQUIRED_FIELDS - set(header))
    if missing:
        raise MigrationError(f"{label}: missing required columns: {', '.join(missing)}")
    by_id: dict[str, Mapping[str, str]] = {}
    for number, row in enumerate(rows, 2):
        segment_id = row.get("segment_id", "")
        if not segment_id:
            raise MigrationError(f"{label}:{number}: missing segment_id")
        if segment_id in by_id:
            raise MigrationError(f"{label}:{number}: duplicate segment_id {segment_id}")
        expected = segments.segment_id(row.get("source_path", ""), row.get("source_key", ""))
        if segment_id != expected:
            raise MigrationError(
                f"{label}:{number}: segment_id does not match source path/key: "
                f"{segment_id} != {expected}"
            )
        expected_hash = segments.source_hash(row.get("source_text", ""))
        if row.get("source_hash", "") != expected_hash:
            raise MigrationError(
                f"{label}:{number}: source_hash does not match source_text for {segment_id}"
            )
        by_id[segment_id] = row
    return by_id


def _move_signature(row: Mapping[str, str]) -> tuple[str, str, str, str]:
    """A conservative proof that a string only moved within one resource.

    The exact decoded source is included alongside its SHA so the proof never
    depends on a hash alone.  Cross-resource moves are intentionally not
    inferred.
    """
    return (
        row.get("content_pack_id", ""),
        row.get("source_path", ""),
        row.get("source_hash", ""),
        row.get("source_text", ""),
    )


def _source_technical_signature(row: Mapping[str, str]) -> tuple[str, str]:
    return (row.get("protected_tokens", ""), row.get("text_type", ""))


def _detail(
    classification: str,
    *,
    old: Mapping[str, str] | None,
    new: Mapping[str, str] | None,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "old_segment_id": "" if old is None else old.get("segment_id", ""),
        "new_segment_id": "" if new is None else new.get("segment_id", ""),
        "source_path": (new or old or {}).get("source_path", ""),
        "old_source_key": "" if old is None else old.get("source_key", ""),
        "new_source_key": "" if new is None else new.get("source_key", ""),
        "old_source_hash": "" if old is None else old.get("source_hash", ""),
        "new_source_hash": "" if new is None else new.get("source_hash", ""),
        "old_status": "" if old is None else old.get("status", ""),
        "new_status": "" if new is None else new.get("status", ""),
    }


_ORDINAL_KEY_RE = re.compile(r"^literal/\d{6}$")

# All carried lifecycle/translation/history fields that must be byte-identical
# across an old duplicate group before order-preserving pairing is allowed.
# When every row in the group carries the same bytes, the choice of bijection
# is unobservable in the output, so pairing k-th old with k-th new occurrence
# cannot misattribute a translation.
_DUPLICATE_CARRY_CONSISTENCY_FIELDS = (
    "target_text",
    "batch_id",
    "status",
    "target_revision",
    "decision_ids",
    "uncertainty_ids",
    "notes",
)


def _source_order(row: Mapping[str, str]) -> int:
    try:
        return int(str(row.get("source_order", "0")).strip() or "0")
    except ValueError as exc:
        raise MigrationError(
            f"segment {row.get('segment_id', '')} has a non-integer source_order"
        ) from exc


def _pair_identical_duplicate_groups(
    old_rows: Sequence[Mapping[str, str]],
    new_rows: Sequence[Mapping[str, str]],
) -> tuple[
    dict[str, tuple[str, Mapping[str, str] | None]],
    set[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Pair ordinal-keyed duplicate-source groups in file order.

    Literal ordinals shift whenever upstream inserts a string literal, so
    same-ID matching would misreport every shifted duplicate as
    CHANGED_SAME_ID and the remainder as ambiguous moves.  When a full
    (content pack, path, hash, text) group on both sides has equal size and
    every old row carries byte-identical lifecycle/translation bytes with a
    nonempty target, the rows are indistinguishable and k-th to k-th pairing
    is exact.  Anything else is left for the standard fail-closed path.
    Returns (classifications, matched_old_ids, details, resolutions).
    """
    old_groups: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    new_groups: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in old_rows:
        old_groups[_move_signature(row)].append(row)
    for row in new_rows:
        new_groups[_move_signature(row)].append(row)

    classifications: dict[str, tuple[str, Mapping[str, str] | None]] = {}
    matched_old_ids: set[str] = set()
    details: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    for signature in sorted(set(old_groups) & set(new_groups)):
        olds, news = old_groups[signature], new_groups[signature]
        if len(olds) == 1 and len(news) == 1:
            continue
        if not all(
            _ORDINAL_KEY_RE.fullmatch(row.get("source_key", "") or "")
            for row in (*olds, *news)
        ):
            continue
        if len(olds) != len(news):
            continue
        first = olds[0]
        if not first.get("target_text"):
            continue
        if any(
            row.get(field, "") != first.get(field, "")
            for row in olds
            for field in _DUPLICATE_CARRY_CONSISTENCY_FIELDS
        ):
            continue
        expected_technical = _source_technical_signature(first)
        if any(
            _source_technical_signature(row) != expected_technical
            for row in (*olds, *news)
        ):
            raise MigrationError(
                "identical-duplicate group changes protected tokens or text type: "
                + ", ".join(sorted(row.get("segment_id", "") for row in olds))
            )
        olds_sorted = sorted(olds, key=lambda row: (_source_order(row), row.get("segment_id", "")))
        news_sorted = sorted(news, key=lambda row: (_source_order(row), row.get("segment_id", "")))
        paired: list[dict[str, str]] = []
        for old, new in zip(olds_sorted, news_sorted):
            if old["segment_id"] == new["segment_id"]:
                classifications[new["segment_id"]] = (CLASS_UNCHANGED, old)
            else:
                classifications[new["segment_id"]] = (CLASS_MOVED_SAME_SOURCE, old)
                details.append(_detail(CLASS_MOVED_SAME_SOURCE, old=old, new=new))
            matched_old_ids.add(old["segment_id"])
            paired.append({"old_segment_id": old["segment_id"], "new_segment_id": new["segment_id"]})
        resolutions.append({
            "content_pack_id": signature[0],
            "source_path": signature[1],
            "source_hash": signature[2],
            "method": "identical-duplicate-order-carry",
            "pairs": paired,
        })
    return classifications, matched_old_ids, details, resolutions


def _copy_unknown_old_columns(
    result: dict[str, str],
    old: Mapping[str, str],
    old_header: Sequence[str],
    new_header: Sequence[str],
) -> None:
    # A column unknown to the fresh extractor must not disappear merely
    # because the game build changed.  Columns present in the fresh file remain
    # fresh-source authoritative unless explicitly listed in CARRY_FIELDS.
    for column in old_header:
        if column not in REQUIRED_FIELDS and column not in new_header:
            result[column] = old.get(column, "")


def _merge_row(
    new: Mapping[str, str],
    old: Mapping[str, str] | None,
    classification: str,
    old_header: Sequence[str],
    new_header: Sequence[str],
) -> dict[str, str]:
    result = dict(new)
    if old is None:
        return result
    _copy_unknown_old_columns(result, old, old_header, new_header)
    if classification in {CLASS_UNCHANGED, CLASS_MOVED_SAME_SOURCE}:
        for field in CARRY_FIELDS:
            result[field] = old.get(field, "")
    elif classification == CLASS_CHANGED_SAME_ID:
        # The previous target is useful review evidence and required by current
        # QA for NEEDS_RECHECK rows, but it is not approved for the new source.
        for field in CARRY_FIELDS:
            result[field] = old.get(field, "")
        result["status"] = "NEEDS_RECHECK"
    else:
        raise AssertionError(f"unexpected matched classification: {classification}")
    return result


def plan_migration(
    old_header: Sequence[str],
    old_rows: Sequence[Mapping[str, str]],
    new_header: Sequence[str],
    new_rows: Sequence[Mapping[str, str]],
) -> tuple[list[str], list[dict[str, str]], dict[str, Any]]:
    old_by_id = _validate_rows("old segments", old_header, old_rows)
    new_by_id = _validate_rows("new segments", new_header, new_rows)

    classifications: dict[str, tuple[str, Mapping[str, str] | None]] = {}
    details: list[dict[str, Any]] = []
    matched_old_ids: set[str] = set()

    pre_classified, pre_matched, pre_details, resolutions = (
        _pair_identical_duplicate_groups(old_rows, new_rows)
    )
    classifications.update(pre_classified)
    matched_old_ids.update(pre_matched)
    details.extend(pre_details)

    # Same-ID pairs with different sources are NOT classified here.  Under
    # ordinal identity a shifted string collides with its successor's old ID,
    # so such pairs are only genuine replacements when neither side resolves
    # elsewhere (unique move or duplicate carry) first.
    deferred_same_id: list[tuple[Mapping[str, str], Mapping[str, str]]] = []
    for new in new_rows:
        segment_id = new["segment_id"]
        if segment_id in classifications:
            continue
        old = old_by_id.get(segment_id)
        if old is None:
            continue
        if old["segment_id"] in matched_old_ids:
            # The old row with this ID was already paired by identical-duplicate
            # order carry to a different new ID: this ordinal collided with an
            # unrelated string, so the new row must resolve via move/added path.
            continue
        if (
            old["source_hash"] == new["source_hash"]
            and old["source_text"] == new["source_text"]
        ):
            matched_old_ids.add(segment_id)
            classifications[segment_id] = (CLASS_UNCHANGED, old)
        else:
            deferred_same_id.append((old, new))

    unmatched_old = [row for row in old_rows if row["segment_id"] not in matched_old_ids]
    unmatched_new = [row for row in new_rows if row["segment_id"] not in classifications]
    old_groups: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    new_groups: dict[tuple[str, str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    for row in unmatched_old:
        old_groups[_move_signature(row)].append(row)
    for row in unmatched_new:
        new_groups[_move_signature(row)].append(row)

    ambiguous: list[dict[str, Any]] = []
    for signature in sorted(set(old_groups) & set(new_groups)):
        olds, news = old_groups[signature], new_groups[signature]
        if len(olds) != 1 or len(news) != 1:
            ambiguous.append({
                "content_pack_id": signature[0],
                "source_path": signature[1],
                "source_hash": signature[2],
                "old_segment_ids": sorted(row["segment_id"] for row in olds),
                "new_segment_ids": sorted(row["segment_id"] for row in news),
            })
            continue
        old, new = olds[0], news[0]
        if _source_technical_signature(old) != _source_technical_signature(new):
            raise MigrationError(
                "same-source move changes protected tokens or text type: "
                f"{old['segment_id']} -> {new['segment_id']}"
            )
        classifications[new["segment_id"]] = (CLASS_MOVED_SAME_SOURCE, old)
        matched_old_ids.add(old["segment_id"])
        details.append(_detail(CLASS_MOVED_SAME_SOURCE, old=old, new=new))

    if ambiguous:
        raise MigrationError(
            "ambiguous same-path/source move candidates; refusing to guess: "
            + json.dumps(ambiguous, ensure_ascii=False, sort_keys=True)
        )

    for old, new in deferred_same_id:
        if old["segment_id"] in matched_old_ids:
            continue
        if new["segment_id"] in classifications:
            continue
        matched_old_ids.add(old["segment_id"])
        classifications[new["segment_id"]] = (CLASS_CHANGED_SAME_ID, old)
        details.append(_detail(CLASS_CHANGED_SAME_ID, old=old, new=new))

    for new in new_rows:
        if new["segment_id"] not in classifications:
            classifications[new["segment_id"]] = (CLASS_ADDED, None)
            details.append(_detail(CLASS_ADDED, old=None, new=new))
    for old in old_rows:
        if old["segment_id"] not in matched_old_ids:
            details.append(_detail(CLASS_REMOVED, old=old, new=None))

    output_header = list(new_header)
    for column in old_header:
        if column not in output_header:
            output_header.append(column)

    output_rows: list[dict[str, str]] = []
    for new in new_rows:
        classification, old = classifications[new["segment_id"]]
        output_rows.append(
            _merge_row(new, old, classification, old_header, new_header)
        )

    counts = {name: 0 for name in CLASS_ORDER}
    for classification, _ in classifications.values():
        counts[classification] += 1
    counts[CLASS_REMOVED] = len(old_rows) - len(matched_old_ids)
    details.sort(key=lambda item: (
        CLASS_ORDER.index(item["classification"]),
        item["source_path"],
        item["old_segment_id"],
        item["new_segment_id"],
    ))
    report = {
        "schema": "drepo-build-migration-report-v1",
        "policy": {
            "fresh_source_fields_authoritative": True,
            "move_proof": "unique content_pack_id + source_path + exact decoded source_text + source_hash",
            "identical_duplicate_groups": "order-preserving carry only for ordinal keys with equal size and byte-identical nonempty carried bytes; otherwise FAIL",
            "same_id_different_source": "deferred until moves resolve; only a pair with neither side resolved elsewhere is a genuine replacement",
            "changed_same_id_status": "NEEDS_RECHECK",
            "changed_same_id_target": "old target retained only as stale review input",
            "ambiguous_moves": "FAIL",
        },
        "counts": counts,
        "old_row_count": len(old_rows),
        "new_row_count": len(new_rows),
        "output_row_count": len(output_rows),
        "duplicate_group_resolutions": resolutions,
        "details": details,
    }
    return output_header, output_rows, report


def apply_approved_changes(
    rows: Sequence[Mapping[str, str]],
    report: Mapping[str, Any],
    approvals: Mapping[str, Any],
    *,
    from_build: str,
    to_build: str,
) -> list[dict[str, str]]:
    """Apply explicit, independently reviewed targets to changed-source rows."""

    if approvals.get("schema") != "drepo-build-migration-approvals-v1":
        raise MigrationError("unexpected migration approval schema")
    if approvals.get("from_build") != from_build or approvals.get("to_build") != to_build:
        raise MigrationError("migration approval build IDs do not match the requested migration")
    changes = approvals.get("changes")
    if not isinstance(changes, list):
        raise MigrationError("migration approvals changes must be a list")
    expected = {
        item["new_segment_id"]: item
        for item in report["details"]
        if item["classification"] == CLASS_CHANGED_SAME_ID
    }
    supplied: dict[str, Mapping[str, Any]] = {}
    for item in changes:
        if not isinstance(item, Mapping):
            raise MigrationError("migration approval entries must be objects")
        segment_id = str(item.get("segment_id", ""))
        if not segment_id or segment_id in supplied:
            raise MigrationError(f"duplicate or empty approved segment ID: {segment_id!r}")
        supplied[segment_id] = item
    if set(supplied) != set(expected):
        raise MigrationError(
            "approved changed-segment set differs from migration report: "
            f"expected={sorted(expected)}, supplied={sorted(supplied)}"
        )

    result: list[dict[str, str]] = []
    for original in rows:
        row = dict(original)
        item = supplied.get(row["segment_id"])
        if item is None:
            result.append(row)
            continue
        if row.get("status") != "NEEDS_RECHECK":
            raise MigrationError(f"approved row is not NEEDS_RECHECK: {row['segment_id']}")
        if item.get("source_hash") != row.get("source_hash"):
            raise MigrationError(f"approved source hash drift: {row['segment_id']}")
        target = item.get("target_text")
        if not isinstance(target, str) or not target:
            raise MigrationError(f"approved target must be nonempty: {row['segment_id']}")
        translator = str(item.get("translator", ""))
        reviewers = item.get("reviewers")
        if not isinstance(reviewers, list) or len(set(map(str, reviewers))) < 2:
            raise MigrationError(f"approved change requires two independent reviewers: {row['segment_id']}")
        if not translator or translator in set(map(str, reviewers)):
            raise MigrationError(f"translator must be independent from reviewers: {row['segment_id']}")
        if not str(item.get("evidence", "")):
            raise MigrationError(f"approved change requires review evidence: {row['segment_id']}")
        status = str(item.get("status", ""))
        if status not in {"APPROVED", "INTEGRATED"}:
            raise MigrationError(f"unsupported approved status {status!r}: {row['segment_id']}")
        try:
            old_revision = int(row.get("target_revision", "0"))
            new_revision = int(str(item.get("target_revision", "")))
        except ValueError as exc:
            raise MigrationError(f"invalid approved target revision: {row['segment_id']}") from exc
        if new_revision != old_revision + 1:
            raise MigrationError(f"approved target revision must increment by one: {row['segment_id']}")
        decision_id = str(item.get("decision_id", ""))
        if not decision_id.startswith("D-"):
            raise MigrationError(f"approved change requires a decision ID: {row['segment_id']}")
        row["target_text"] = target
        row["target_revision"] = str(new_revision)
        row["status"] = status
        row["decision_ids"] = ",".join(filter(None, (row.get("decision_ids", ""), decision_id)))
        note = (
            f"build migration {from_build} -> {to_build}; translator={translator}; "
            f"reviewers={','.join(map(str, reviewers))}; evidence={item.get('evidence', '')}"
        )
        row["notes"] = "; ".join(filter(None, (row.get("notes", ""), note)))
        result.append(row)
    return result


def migrate_assignments(
    header: Sequence[str],
    rows: Sequence[Mapping[str, str]],
    report: Mapping[str, Any],
    *,
    from_build: str,
    to_build: str,
    approved_statuses: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Re-pin assignment authority while retaining exact old pins in notes."""

    missing = sorted(set(ASSIGNMENT_FIELDS) - set(header))
    if missing:
        raise MigrationError(f"assignments: missing required columns: {', '.join(missing)}")
    moves = {
        item["old_segment_id"]: item["new_segment_id"]
        for item in report["details"]
        if item["classification"] == CLASS_MOVED_SAME_SOURCE
    }
    changed_ids = {
        item["new_segment_id"]
        for item in report["details"]
        if item["classification"] == CLASS_CHANGED_SAME_ID
    }
    removed_ids = {
        item["old_segment_id"]
        for item in report["details"]
        if item["classification"] == CLASS_REMOVED
    }
    counts = {
        "repinned": 0,
        "moved_segment_id": 0,
        "changed_status": 0,
        "removed_cancelled": 0,
    }
    output: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for original in rows:
        row = dict(original)
        assignment_id = row.get("assignment_id", "")
        if not assignment_id or assignment_id in seen_ids:
            raise MigrationError(f"duplicate or empty assignment_id: {assignment_id!r}")
        seen_ids.add(assignment_id)
        source_build = row.get("source_build", "")
        if source_build != from_build:
            raise MigrationError(
                f"assignment {assignment_id} has unexpected source_build {source_build!r}"
            )
        old_segment_id = row.get("segment_id", "")
        removed = old_segment_id in removed_ids
        if removed:
            row["status"] = "CANCELLED"
            counts["removed_cancelled"] += 1
        else:
            row["source_build"] = to_build
            counts["repinned"] += 1
        if old_segment_id in moves:
            row["segment_id"] = moves[old_segment_id]
            counts["moved_segment_id"] += 1
        if approved_statuses and row["segment_id"] in changed_ids:
            requested = approved_statuses.get(row["segment_id"])
            if requested:
                row["status"] = requested
                counts["changed_status"] += 1
        try:
            note_data = json.loads(row.get("notes", "") or "{}")
        except json.JSONDecodeError as exc:
            raise MigrationError(f"assignment {assignment_id} notes are not JSON") from exc
        if not isinstance(note_data, dict):
            raise MigrationError(f"assignment {assignment_id} notes must be a JSON object")
        migration_note: dict[str, str] = {"from_build": from_build, "to_build": to_build}
        if removed:
            migration_note["removed_from_target"] = "true"
        if old_segment_id != row["segment_id"]:
            migration_note["from_segment_id"] = old_segment_id
        note_data["build_migration"] = migration_note
        row["notes"] = json.dumps(note_data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        output.append(row)
    return output, counts


def _atomic_write(path: Path, data: bytes, *, allow_overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not allow_overwrite:
        raise MigrationError(f"output exists (use --allow-overwrite): {path}")
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


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def run(
    *,
    old_segments: Path,
    new_segments: Path,
    old_manifest: Path,
    new_manifest: Path,
    old_assignments: Path | None,
    approvals_path: Path | None,
    output_segments: Path | None,
    output_manifest: Path | None,
    output_assignments: Path | None,
    report_path: Path,
    from_build: str,
    to_build: str,
    allow_overwrite: bool = False,
) -> dict[str, Any]:
    old_header, old_rows = segments.read_tsv(old_segments)
    new_header, new_rows = segments.read_tsv(new_segments)
    output_header, output_rows, report = plan_migration(
        old_header, old_rows, new_header, new_rows
    )
    report["from_build"] = from_build
    report["to_build"] = to_build
    report["inputs"] = {
        "old_segments_sha256": sha256_file(old_segments),
        "new_segments_sha256": sha256_file(new_segments),
        "old_manifest_sha256": sha256_file(old_manifest),
        "new_manifest_sha256": sha256_file(new_manifest),
    }

    approvals: Mapping[str, Any] | None = None
    approved_statuses: dict[str, str] = {}
    if approvals_path is not None:
        approvals = json.loads(approvals_path.read_text(encoding="utf-8"))
        output_rows = apply_approved_changes(
            output_rows, report, approvals, from_build=from_build, to_build=to_build
        )
        approved_statuses = {
            str(item["segment_id"]): str(item["status"])
            for item in approvals["changes"]
        }
        report["inputs"]["approvals_sha256"] = sha256_file(approvals_path)

    assignment_header: list[str] | None = None
    assignment_rows: list[dict[str, str]] | None = None
    if old_assignments is not None:
        assignment_header, raw_assignments = segments.read_tsv(old_assignments)
        assignment_rows, assignment_counts = migrate_assignments(
            assignment_header,
            raw_assignments,
            report,
            from_build=from_build,
            to_build=to_build,
            approved_statuses=approved_statuses,
        )
        report["inputs"]["old_assignments_sha256"] = sha256_file(old_assignments)
        report["assignment_counts"] = assignment_counts

    if (output_segments is None) != (output_manifest is None):
        raise MigrationError("output segments and output manifest must be provided together")
    if (old_assignments is None) != (output_assignments is None):
        raise MigrationError("old and output assignments must be provided together")
    if output_assignments is not None and output_segments is None:
        raise MigrationError("output assignments require output segments and manifest")
    if output_segments is not None and output_manifest is not None:
        if not allow_overwrite:
            output_paths = [output_segments, output_manifest, report_path]
            if output_assignments is not None:
                output_paths.append(output_assignments)
            for path in output_paths:
                if path.exists():
                    raise MigrationError(f"output exists (use --allow-overwrite): {path}")
        segments.write_tsv_atomic(output_segments, output_rows, output_header)
        # The fresh manifest is authoritative.  Copy exact bytes rather than
        # reserializing it, preserving all columns, escaping, and row order.
        _atomic_write(
            output_manifest,
            new_manifest.read_bytes(),
            allow_overwrite=allow_overwrite,
        )
        report["outputs"] = {
            "segments_sha256": sha256_file(output_segments),
            "manifest_sha256": sha256_file(output_manifest),
        }
        if output_assignments is not None:
            assert assignment_header is not None and assignment_rows is not None
            segments.write_tsv_atomic(output_assignments, assignment_rows, assignment_header)
            report["outputs"]["assignments_sha256"] = sha256_file(output_assignments)
    _atomic_write(report_path, _json_bytes(report), allow_overwrite=allow_overwrite)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-segments", type=Path, required=True)
    parser.add_argument("--new-segments", type=Path, required=True)
    parser.add_argument("--old-manifest", type=Path, required=True)
    parser.add_argument("--new-manifest", type=Path, required=True)
    parser.add_argument("--old-assignments", type=Path)
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--output-segments", type=Path)
    parser.add_argument("--output-manifest", type=Path)
    parser.add_argument("--output-assignments", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--from-build", required=True)
    parser.add_argument("--to-build", required=True)
    parser.add_argument("--allow-overwrite", action="store_true")
    args = parser.parse_args()
    try:
        report = run(
            old_segments=args.old_segments,
            new_segments=args.new_segments,
            old_manifest=args.old_manifest,
            new_manifest=args.new_manifest,
            old_assignments=args.old_assignments,
            approvals_path=args.approvals,
            output_segments=args.output_segments,
            output_manifest=args.output_manifest,
            output_assignments=args.output_assignments,
            report_path=args.report,
            from_build=args.from_build,
            to_build=args.to_build,
            allow_overwrite=args.allow_overwrite,
        )
    except (OSError, UnicodeError, segments.SegmentError, MigrationError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "counts": report["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
