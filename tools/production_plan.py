#!/usr/bin/env python3
"""Validate the pinned production-batch plan against localization state."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Sequence


EXPECTED_MAPPING_HEADER = [
    "wave", "batch_id", "segment_id", "source_path", "source_key",
    "story_state_id", "text_type", "status", "source_hash",
]
EXPECTED_SNAPSHOT_KIND = "INITIAL_PREASSIGNMENT_IMMUTABLE"
EXPECTED_MAPPING_STATUS_SEMANTICS = "initial_source_status_not_current_lifecycle"
ACTIVE_TRANSLATION_STATUSES = {
    "ASSIGNED", "DRAFTED", "AUTO_VALIDATED", "REVIEWED", "APPROVED",
    "INTEGRATED", "RUNTIME_VALIDATED", "QA_FAILED", "NEEDS_RECHECK",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise ValueError(f"missing TSV header: {path}")
        return list(reader.fieldnames), list(reader)


def validate(
    segments_path: Path,
    assignments_path: Path,
    plan_path: Path,
    mapping_path: Path,
    migration_path: Path | Sequence[Path] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "drepo-production-batch-proposal-v1":
        errors.append("unexpected plan schema")
    if plan.get("snapshot_kind") != EXPECTED_SNAPSHOT_KIND:
        errors.append("production plan must declare an immutable initial snapshot")
    if plan.get("mapping_status_semantics") != EXPECTED_MAPPING_STATUS_SEMANTICS:
        errors.append("production mapping status semantics are not explicit")

    mapping_header, mapping_rows = read_tsv(mapping_path)
    if mapping_header != EXPECTED_MAPPING_HEADER:
        errors.append("mapping header differs from required schema")

    _, segments = read_tsv(segments_path)
    _, assignments = read_tsv(assignments_path)
    segment_by_id = {row["segment_id"]: row for row in segments}
    if len(segment_by_id) != len(segments):
        errors.append("canonical segments contain duplicate IDs")

    migration: dict[str, Any] | None = None
    migration_paths = (
        [] if migration_path is None else
        [migration_path] if isinstance(migration_path, Path) else list(migration_path)
    )
    migrations: list[dict[str, Any]] = []
    migrated_ids: dict[str, str] = {}
    expected_build = plan.get("target_build")
    for path in migration_paths:
        migration = json.loads(path.read_text(encoding="utf-8"))
        migrations.append(migration)
        if migration.get("schema") != "drepo-build-migration-report-v1":
            errors.append("unexpected build migration schema")
        if migration.get("from_build") != expected_build:
            errors.append("build migration chain is discontinuous")
        expected_build = migration.get("to_build")
        counts = migration.get("counts")
        if not isinstance(counts, dict):
            errors.append("build migration counts are missing")
        else:
            changed_count = counts.get("CHANGED_SAME_ID")
            moved_count = counts.get("MOVED_SAME_SOURCE")
            added_count = counts.get("ADDED")
            removed_count = counts.get("REMOVED")
            if not all(isinstance(value, int) for value in (
                changed_count, moved_count, added_count, removed_count,
            )):
                errors.append("build migration changed/moved counts are invalid")
            elif changed_count + moved_count + added_count + removed_count != len(migration.get("details", [])):
                errors.append("build migration detail count does not match classification counts")
        step_moves: dict[str, str] = {}
        removed_ids: set[str] = set()
        added_ids: set[str] = set()
        for item in migration.get("details", []):
            classification = item.get("classification", "")
            if classification == "ADDED":
                added_ids.add(item.get("new_segment_id", ""))
                continue
            if classification == "REMOVED":
                removed_ids.add(item.get("old_segment_id", ""))
                continue
            if classification not in {"CHANGED_SAME_ID", "MOVED_SAME_SOURCE"}:
                errors.append(f"unexpected migration detail classification: {classification!r}")
                continue
            old_id = item.get("old_segment_id", "")
            new_id = item.get("new_segment_id", "")
            if not old_id or not new_id or old_id in step_moves:
                errors.append(f"invalid or duplicate migration mapping: {old_id!r} -> {new_id!r}")
                continue
            old_hash = item.get("old_source_hash", "")
            new_hash = item.get("new_source_hash", "")
            if classification == "MOVED_SAME_SOURCE" and (
                old_id == new_id or not old_hash or old_hash != new_hash
            ):
                errors.append(f"invalid same-source move proof: {old_id!r} -> {new_id!r}")
                continue
            if classification == "CHANGED_SAME_ID" and (
                old_id != new_id or not old_hash or not new_hash or old_hash == new_hash
            ):
                errors.append(f"invalid changed-source proof: {old_id!r}")
                continue
            step_moves[old_id] = new_id
        replacements: dict[str, str] = {}
        for item in migration.get("logical_replacements", []):
            old_id = item.get("old_segment_id", "")
            new_id = item.get("new_segment_id", "")
            if old_id not in removed_ids or new_id not in added_ids or old_id in replacements:
                errors.append(f"invalid logical replacement: {old_id!r} -> {new_id!r}")
            else:
                replacements[old_id] = new_id
        for original, current in list(migrated_ids.items()):
            migrated_ids[original] = step_moves.get(current, replacements.get(current, current))
        for old_id, new_id in {**step_moves, **replacements}.items():
            migrated_ids.setdefault(old_id, new_id)

    plan_rows: dict[str, tuple[str, str]] = {}
    context_ids: set[str] = set()
    for batch in plan.get("batches", []):
        batch_id = batch.get("batch_id", "")
        wave = str(batch.get("wave", ""))
        ids = batch.get("segment_ids", [])
        if batch.get("count") != len(ids):
            errors.append(f"{batch_id}: count does not match segment_ids")
        for segment_id in ids:
            if segment_id in plan_rows:
                errors.append(f"duplicate planned segment: {segment_id}")
            plan_rows[segment_id] = (wave, batch_id)
        context_ids.update(batch.get("required_context_segment_ids", []))

    mapping_by_id: dict[str, dict[str, str]] = {}
    for row in mapping_rows:
        segment_id = row.get("segment_id", "")
        if segment_id in mapping_by_id:
            errors.append(f"duplicate mapping segment: {segment_id}")
        mapping_by_id[segment_id] = row
    if set(plan_rows) != set(mapping_by_id):
        errors.append("JSON plan and TSV mapping ID sets differ")

    assignment_by_segment: dict[str, list[dict[str, str]]] = {}
    for row in assignments:
        if row.get("role") == "translation_worker":
            assignment_by_segment.setdefault(row.get("segment_id", ""), []).append(row)

    for segment_id, (wave, batch_id) in plan_rows.items():
        current_id = migrated_ids.get(segment_id, segment_id)
        current = segment_by_id.get(current_id)
        mapped = mapping_by_id.get(segment_id)
        if current is None or mapped is None:
            errors.append(f"planned segment missing from current data: {segment_id}")
            continue
        if mapped.get("wave") != wave or mapped.get("batch_id") != batch_id:
            errors.append(f"wave/batch mismatch: {segment_id}")
        if mapped.get("status") != plan.get("editable_status"):
            errors.append(f"{segment_id}: mapping has unexpected source status")
        tracked_id = segment_id
        evolved_source_keys = {mapped.get("source_key", "")}
        evolved_source_hashes = {mapped.get("source_hash", "")}
        logical_replacement = False
        for step in migrations:
            replacement_by_old = {
                item.get("old_segment_id", ""): item.get("new_segment_id", "")
                for item in step.get("logical_replacements", [])
            }
            detail = next(
                (item for item in step.get("details", []) if item.get("old_segment_id") == tracked_id),
                None,
            )
            if detail is not None and detail.get("classification") != "REMOVED":
                evolved_source_keys.add(detail.get("new_source_key", ""))
                evolved_source_hashes.add(detail.get("new_source_hash", ""))
                tracked_id = detail.get("new_segment_id", tracked_id)
            if tracked_id in replacement_by_old:
                tracked_id = replacement_by_old[tracked_id]
                logical_replacement = True
                replacement_row = segment_by_id.get(tracked_id, {})
                evolved_source_keys.add(replacement_row.get("source_key", ""))
                evolved_source_hashes.add(replacement_row.get("source_hash", ""))
        for field in ("source_path", "source_key", "story_state_id", "text_type", "source_hash"):
            if mapped.get(field, "") == current.get(field, ""):
                continue
            allowed_values = (
                evolved_source_keys if field == "source_key" else
                evolved_source_hashes if field == "source_hash" else
                {mapped.get(field, "")}
            )
            if current.get(field, "") not in allowed_values:
                errors.append(f"{segment_id}: mapping drift in {field}")
        status = current.get("status", "")
        current_batch = current.get("batch_id", "")
        if status == "EXTRACTED" and not current_batch:
            if assignment_by_segment.get(segment_id):
                errors.append(f"{segment_id}: assignment exists before canonical ownership")
        elif status in ACTIVE_TRANSLATION_STATUSES and (
            current_batch == batch_id or logical_replacement
        ):
            rows = [
                row for row in assignment_by_segment.get(current_id, [])
                if row.get("batch_id") == current_batch
            ]
            if len(rows) != 1:
                errors.append(f"{segment_id}: expected one matching translation assignment")
        else:
            errors.append(
                f"{segment_id}: invalid lifecycle ownership {status!r}/{current_batch!r}"
            )

    extra_extracted = {
        row["segment_id"] for row in segments if row.get("status") == "EXTRACTED"
    } - {migrated_ids.get(segment_id, segment_id) for segment_id in plan_rows}
    if extra_extracted:
        errors.append(f"unplanned EXTRACTED segments: {len(extra_extracted)}")

    for segment_id in context_ids:
        row = segment_by_id.get(migrated_ids.get(segment_id, segment_id))
        if row is None:
            errors.append(f"missing context segment: {segment_id}")
        elif row.get("status") == "OUT_OF_SCOPE":
            errors.append(f"OUT_OF_SCOPE segment used as context: {segment_id}")

    pinned_hash = plan.get("initial_segments_sha256", "")
    legacy_pinned_hash = plan.get("canonical_segments_sha256", "")
    if legacy_pinned_hash and legacy_pinned_hash != pinned_hash:
        errors.append("legacy and explicit initial segment hashes differ")
    pinned_assignments_hash = plan.get("initial_assignments_sha256", "")
    legacy_assignments_hash = plan.get("canonical_assignments_sha256", "")
    if legacy_assignments_hash and legacy_assignments_hash != pinned_assignments_hash:
        errors.append("legacy and explicit initial assignment hashes differ")
    current_hash = sha256_file(segments_path)
    if migration is not None:
        outputs = migration.get("final_outputs", migration.get("outputs"))
        if not isinstance(outputs, dict):
            errors.append("build migration output hashes are missing")
        else:
            if outputs.get("segments_sha256") != current_hash:
                errors.append("build migration segments output hash differs from current canonical")
            if outputs.get("assignments_sha256") != sha256_file(assignments_path):
                errors.append("build migration assignments output hash differs from current canonical")
        assignment_builds = {
            row.get("source_build", "") for row in assignments
            if row.get("role") == "translation_worker"
            and row.get("status") in ACTIVE_TRANSLATION_STATUSES
            and "source_build" in row
        }
        if assignment_builds and assignment_builds != {migration.get("to_build", "")}:
            errors.append("translation assignments are not pinned to the migration target build")
    initial_snapshot = current_hash == pinned_hash
    if not initial_snapshot and all(
        segment_by_id[migrated_ids.get(sid, sid)].get("status") == "EXTRACTED"
        for sid in plan_rows
    ):
        errors.append("canonical SHA drifted before any production assignment")

    expected_count = int(plan.get("editable_count", -1))
    if expected_count != len(plan_rows) or len(mapping_rows) != len(plan_rows):
        errors.append("editable count differs across plan artifacts")

    current_status_counts: dict[str, int] = {}
    for segment_id in plan_rows:
        current_id = migrated_ids.get(segment_id, segment_id)
        status = segment_by_id.get(current_id, {}).get("status", "MISSING")
        current_status_counts[status] = current_status_counts.get(status, 0) + 1

    current_assignments_hash = sha256_file(assignments_path)

    result = {
        "ok": not errors,
        "error_count": len(errors),
        "errors": errors,
        "plan_segment_count": len(plan_rows),
        "context_segment_count": len(context_ids),
        "mapping_row_count": len(mapping_rows),
        "snapshot_kind": plan.get("snapshot_kind", ""),
        "mapping_status_semantics": plan.get("mapping_status_semantics", ""),
        "current_plan_status_counts": current_status_counts,
        "initial_snapshot_hash_match": initial_snapshot,
        "initial_segments_sha256": pinned_hash,
        "current_segments_sha256": current_hash,
        "initial_assignments_sha256": pinned_assignments_hash,
        "current_assignments_sha256": current_assignments_hash,
        "segments_sha256": current_hash,
        "assignments_sha256": current_assignments_hash,
        "plan_sha256": sha256_file(plan_path),
        "mapping_sha256": sha256_file(mapping_path),
    }
    if migration_paths:
        result["migration_sha256"] = [sha256_file(path) for path in migration_paths]
        result["migration_from_build"] = migrations[0].get("from_build", "")
        result["migration_to_build"] = "" if migration is None else migration.get("to_build", "")
        result["migration_mapping_count"] = len(migrated_ids)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--segments", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--migration-report", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        result = validate(
            args.segments, args.assignments, args.plan, args.mapping, args.migration_report
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result = {"ok": False, "error_count": 1, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
