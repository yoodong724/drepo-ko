#!/usr/bin/env python3
"""Fail-closed end-to-end builder for a generated Korean game PCK.

Only reviewed canonical targets are eligible for reinsertion.  The supplied
game, recovered project, canonical TSVs, and component outputs are immutable
inputs; all work happens below a fresh generated output root.  This is an
integration artifact builder, not the original-free distribution delta.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping, Sequence

try:
    from tools import coverage as coverage_validator
    from tools import fonts, korean_code, qa, resources, runtime_capture, segments
    from tools.pck import PckBuildEntry, build_pck, patch_pck, read_pck
except ModuleNotFoundError:  # Direct execution: python tools/integrate.py
    import coverage as coverage_validator  # type: ignore[no-redef]
    import fonts  # type: ignore[no-redef]
    import korean_code  # type: ignore[no-redef]
    import qa  # type: ignore[no-redef]
    import resources  # type: ignore[no-redef]
    import runtime_capture  # type: ignore[no-redef]
    import segments  # type: ignore[no-redef]
    from pck import PckBuildEntry, build_pck, patch_pck, read_pck


INTEGRATION_SCHEMA_VERSION = 1
TARGET_BUILD_ID = "DGR-WIN-1.0.5-b3e7048e"
TARGET_EXE_SHA256 = "1de3edb8e10c66412a5e0d76ca44a2b70ad791d8b08f17135aae36b9bc369bf5"
TARGET_PCK_SHA256 = "b3e7048e62421e74b8b485f242fbec3f14cde94d452309f6d57d957623821399"
PRODUCTION_STATUSES = frozenset({"APPROVED", "INTEGRATED", "RUNTIME_VALIDATED"})
PILOT_STATUSES = PRODUCTION_STATUSES | {"REVIEWED"}
SOURCE_EXTENSIONS = frozenset({".gd", ".tscn", ".txt", ".json"})
NON_INTEGRATABLE_TEXT_TYPES = frozenset({"debug", "unknown"})
INTEGRATABLE_CREDIT_SEGMENTS = frozenset({
    (
        "SEG-BASE-3E1D8EA0BC05FEB13E72",
        "scripts/windows/ending_credits.gd",
        "literal/000001",
        "The Death Game Report",
    ),
    (
        "SEG-BASE-8321671767523B8CAFE2",
        "scripts/windows/ending_credits.gd",
        "literal/000004",
        "― STAFF ―",
    ),
    (
        "SEG-BASE-5A94D829986A1BA3577D",
        "scripts/windows/ending_credits.gd",
        "literal/000006",
        "Game Design / Writing / Art",
    ),
    (
        "SEG-BASE-106568973144AD5ACE61",
        "scripts/windows/ending_credits.gd",
        "literal/000009",
        "Programming / Music / Sound",
    ),
    (
        "SEG-BASE-F1743A871281C989CBB5",
        "scripts/windows/ending_credits.gd",
        "literal/000013",
        "― PLAYTESTING ―",
    ),
    (
        "SEG-BASE-219F66D4F4ACD8B1CCDC",
        "scripts/windows/ending_credits.gd",
        "literal/000018",
        "― INSPIRED BY ―",
    ),
    (
        "SEG-BASE-51ABBC64404E3F7171D3",
        "scripts/windows/ending_credits.gd",
        "literal/000025",
        "― THE END ―",
    ),
})
MANIFEST_NAME = "integration-manifest.json"
PCK_NAME = "drepo.ko.pck"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JAPANESE_OR_CJK = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)


class IntegrationError(ValueError):
    """An input, approval pin, generated component, or PCK check failed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_file(path: Path | str, label: str, *, executable: bool = False) -> Path:
    result = Path(path).absolute()
    if result.is_symlink() or not result.is_file():
        raise IntegrationError(f"{label} must be a regular non-symlink file: {result}")
    if executable and not os.access(result, os.X_OK):
        raise IntegrationError(f"{label} is not executable: {result}")
    return result.resolve()


def _strict_directory(path: Path | str, label: str) -> Path:
    result = Path(path).absolute()
    if result.is_symlink() or not result.is_dir():
        raise IntegrationError(f"{label} must be a real directory: {result}")
    resolved = result.resolve()
    symlink = next((item for item in resolved.rglob("*") if item.is_symlink()), None)
    if symlink is not None:
        raise IntegrationError(f"{label} contains a symlink: {symlink}")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fresh_output(output_root: Path | str, protected: Sequence[Path]) -> Path:
    output = Path(output_root).absolute()
    if output.exists() or output.is_symlink():
        raise IntegrationError(f"output root already exists: {output}")
    if not output.name or not output.parent.is_dir():
        raise IntegrationError(f"output root requires an existing explicit parent: {output}")
    resolved = output.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == output.parent.resolve():
        raise IntegrationError(f"unsafe output root: {output}")
    for item in protected:
        protected_path = item.resolve()
        protected_root = protected_path if protected_path.is_dir() else protected_path.parent
        if _inside(resolved, protected_root) or _inside(protected_path, resolved):
            raise IntegrationError(f"output root overlaps protected input: {item}")
    return output


def _require_hash_pin(actual_path: Path, expected: str, label: str) -> str:
    if not _SHA256.fullmatch(expected):
        raise IntegrationError(f"{label} expected SHA-256 is malformed: {expected!r}")
    actual = sha256_file(actual_path)
    if actual != expected:
        raise IntegrationError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def _read_manifest(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        header, rows = segments.read_tsv(path)
    except (OSError, UnicodeError, segments.SegmentError) as exc:
        raise IntegrationError(f"source manifest is invalid: {exc}") from exc
    required = {
        "resource_id", "content_pack_id", "source_path", "source_size",
        "source_sha256", "extracted_segment_count", "scope_status",
    }
    missing = sorted(required - set(header))
    if missing:
        raise IntegrationError(f"source manifest omits required columns: {missing}")
    return header, rows


def _verify_source_manifest(
    manifest_path: Path,
    recovered: Path,
    canonical_rows: Sequence[Mapping[str, str]],
) -> dict[str, Mapping[str, str]]:
    _, manifest_rows = _read_manifest(manifest_path)
    by_resource: dict[str, Mapping[str, str]] = {}
    by_path: dict[str, Mapping[str, str]] = {}
    segment_counts = Counter(row["resource_id"] for row in canonical_rows)
    for number, row in enumerate(manifest_rows, 2):
        resource_id = row["resource_id"]
        source_path = row["source_path"]
        if not resource_id or resource_id in by_resource:
            raise IntegrationError(f"source manifest row {number} has duplicate/empty resource_id")
        if not source_path or source_path in by_path:
            raise IntegrationError(f"source manifest row {number} has duplicate/empty source_path")
        relative = PurePosixPath(source_path)
        if relative.is_absolute() or "\\" in source_path or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise IntegrationError(f"source manifest row {number} has unsafe source_path")
        source = recovered.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise IntegrationError(f"source manifest input is missing: {source_path}")
        data = source.read_bytes()
        if row["source_sha256"] != sha256_bytes(data):
            raise IntegrationError(f"source manifest hash drift: {source_path}")
        if row["source_size"] != str(len(data)):
            raise IntegrationError(f"source manifest size drift: {source_path}")
        try:
            recorded_count = int(row["extracted_segment_count"])
        except ValueError as exc:
            raise IntegrationError(
                f"source manifest segment count is invalid: {source_path}"
            ) from exc
        if recorded_count != segment_counts.get(resource_id, 0):
            raise IntegrationError(f"source manifest segment count drift: {source_path}")
        by_resource[resource_id] = row
        by_path[source_path] = row
    for row in canonical_rows:
        manifest_row = by_resource.get(row["resource_id"])
        if manifest_row is None or manifest_row["source_path"] != row["source_path"]:
            raise IntegrationError(
                f"canonical resource pin is absent or mismatched: {row['segment_id']}"
            )
    return by_resource


def _read_canonical(
    segments_path: Path,
    assignments_path: Path,
    recovered: Path,
    *,
    build_id: str,
    pilot: bool,
    japanese_allowlist: Mapping[str, Sequence[str]] | None = None,
    preserved_fragments: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    try:
        header, rows = segments.read_tsv(segments_path)
        _, assignment_rows = segments.read_tsv(assignments_path)
    except (OSError, UnicodeError, segments.SegmentError) as exc:
        raise IntegrationError(f"canonical TSV is invalid: {exc}") from exc
    missing = [column for column in segments.SEGMENT_COLUMNS if column not in header]
    if missing:
        raise IntegrationError(f"canonical segments omit columns: {missing}")
    structural = segments.validate_segments(segments_path, recovered)
    if not structural["valid"]:
        raise IntegrationError(
            "canonical source validation failed: " + "; ".join(structural["errors"])
        )
    report = qa.validate_batch(
        segments_path, assignments_path, japanese_allowlist=japanese_allowlist,
    )
    if not report.ok:
        messages = "; ".join(f"{issue.code}: {issue.message}" for issue in report.errors)
        raise IntegrationError(f"canonical QA failed: {messages}")
    coverage_report = coverage_validator.validate_coverage(
        rows,
        allowlist=japanese_allowlist,
        preserved_fragments=preserved_fragments,
    )
    if not coverage_report.ok:
        messages = "; ".join(
            f"{issue.code}: {issue.message}" for issue in coverage_report.errors
        )
        raise IntegrationError(f"canonical Japanese/CJK coverage failed: {messages}")

    allowed = PILOT_STATUSES if pilot else PRODUCTION_STATUSES
    integrated: list[dict[str, str]] = []
    effective: list[dict[str, str]] = []
    assignments_by_segment: dict[str, list[dict[str, str]]] = {}
    for assignment in assignment_rows:
        assignments_by_segment.setdefault(assignment.get("segment_id", ""), []).append(assignment)
    for row in rows:
        target = row.get("target_text", "")
        status = row.get("status", "").upper()
        projected = dict(row)
        projected["target_text"] = ""
        if target:
            if status not in allowed:
                raise IntegrationError(
                    f"segment {row['segment_id']} has target_text in non-integratable status {status!r}"
                )
            text_type = row.get("text_type", "").lower()
            credit_identity = (
                row["segment_id"], row["source_path"], row["source_key"], row["source_text"],
            )
            if text_type in NON_INTEGRATABLE_TEXT_TYPES or (
                text_type == "credits" and credit_identity not in INTEGRATABLE_CREDIT_SEGMENTS
            ):
                raise IntegrationError(
                    f"segment {row['segment_id']} has non-integratable text_type "
                    f"{row.get('text_type', '')!r}"
                )
            if target == row["source_text"]:
                raise IntegrationError(f"segment {row['segment_id']} target is unchanged source text")
            residual_target = qa.strip_allowlisted_japanese(
                target, row["segment_id"], japanese_allowlist,
            )
            residual_target = coverage_validator.strip_allowlisted_fragments(
                residual_target, row["segment_id"], preserved_fragments or {},
            )
            if _JAPANESE_OR_CJK.search(residual_target):
                raise IntegrationError(
                    f"segment {row['segment_id']} target retains Japanese/CJK characters"
                )
            try:
                revision = int(row["target_revision"])
            except ValueError as exc:
                raise IntegrationError(
                    f"segment {row['segment_id']} has invalid target_revision"
                ) from exc
            if revision <= 0:
                raise IntegrationError(
                    f"segment {row['segment_id']} target_revision must be positive"
                )
            batch_id = row.get("batch_id", "")
            if not batch_id:
                raise IntegrationError(f"segment {row['segment_id']} has no pinned batch_id")
            candidates = [
                assignment for assignment in assignments_by_segment.get(row["segment_id"], [])
                if assignment.get("batch_id") == batch_id
                and assignment.get("source_build") == build_id
                and assignment.get("status", "").upper() in allowed
                and assignment.get("base_commit")
                and assignment.get("guidelines_revision")
            ]
            if not candidates:
                raise IntegrationError(
                    f"segment {row['segment_id']} lacks a matching approved assignment/revision pin"
                )
            projected["target_text"] = target
            integrated.append(row)
        elif status in allowed:
            raise IntegrationError(
                f"segment {row['segment_id']} is {status} but has an empty target_text"
            )
        effective.append(projected)
    return header, rows, effective, integrated


def _overlay_tree(source_root: Path, destination_root: Path) -> None:
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise IntegrationError(f"generated component contains a symlink: {source}")
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _changed_sources(recovered: Path, localized: Path) -> list[str]:
    changed: list[str] = []
    recovered_paths: set[str] = set()
    localized_paths: set[str] = set()
    for root, collector in ((recovered, recovered_paths), (localized, localized_paths)):
        for path in root.rglob("*"):
            if path.is_symlink():
                raise IntegrationError(f"source tree contains a symlink: {path}")
            if not path.is_file() or ".autoconverted" in path.parts:
                continue
            if path.suffix.lower() in SOURCE_EXTENSIONS:
                collector.add(path.relative_to(root).as_posix())
    if recovered_paths != localized_paths:
        raise IntegrationError(
            "localized source set drifted: "
            f"missing={sorted(recovered_paths - localized_paths)}, "
            f"extra={sorted(localized_paths - recovered_paths)}"
        )
    for relative in sorted(recovered_paths):
        if (recovered / relative).read_bytes() != (localized / relative).read_bytes():
            changed.append(relative)
    return changed


def _runtime_font_entries(font_root: Path, font_manifest: Mapping[str, Any]) -> dict[str, bytes]:
    expected: set[str] = set()
    for entry in font_manifest.get("fonts", []):
        expected.add(str(entry["runtime_import_path"]))
        expected.add(str(entry["runtime_fontdata_path"]))
    for entry in font_manifest.get("licenses", []):
        expected.add(str(entry["package_path"]))
    result: dict[str, bytes] = {}
    for archive_path in sorted(expected):
        path = PurePosixPath(archive_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise IntegrationError(f"font manifest contains unsafe runtime path: {archive_path!r}")
        source = font_root.joinpath(*path.parts)
        if source.is_symlink() or not source.is_file():
            raise IntegrationError(f"font runtime artifact is missing: {archive_path}")
        result[archive_path] = source.read_bytes()
    actual_runtime = {
        path.relative_to(font_root).as_posix()
        for path in font_root.rglob("*")
        if path.is_file()
        and path.name not in {"font_manifest.json", "ui_kit.gd"}
        and "scripts" not in path.relative_to(font_root).parts
    }
    if actual_runtime != expected:
        raise IntegrationError(
            f"font runtime artifact set mismatch: expected {sorted(expected)}, got {sorted(actual_runtime)}"
        )
    return result


def _resource_entries(result: Any) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for built in result.built:
        archive_path = str(built.archive_path)
        source = result.resources_root.joinpath(*PurePosixPath(archive_path).parts)
        if source.is_symlink() or not source.is_file():
            raise IntegrationError(f"compiled resource is missing: {archive_path}")
        data = source.read_bytes()
        if sha256_bytes(data) != built.output_sha256:
            raise IntegrationError(f"compiled resource hash drift: {archive_path}")
        if archive_path in entries:
            raise IntegrationError(f"duplicate compiled resource path: {archive_path}")
        entries[archive_path] = data
    actual = {
        path.relative_to(result.resources_root).as_posix()
        for path in result.resources_root.rglob("*") if path.is_file()
    }
    if actual != set(entries):
        raise IntegrationError(
            f"compiled resource set mismatch: expected {sorted(entries)}, got {sorted(actual)}"
        )
    return entries


def _build_output_pck(source_pck: Path, output_pck: Path, entries: Mapping[str, bytes]) -> Any:
    source = read_pck(source_pck, verify_md5=True)
    existing = {path: data for path, data in entries.items() if path in source.entries_by_path}
    additions = {path: data for path, data in entries.items() if path not in source.entries_by_path}
    intermediate = output_pck.parent / ".replaced-existing.pck"
    patch_pck(source_pck, intermediate, existing, verify_source=True)
    if additions:
        patched = read_pck(intermediate, verify_md5=True)
        build_entries = [
            PckBuildEntry(entry.path, patched.read_entry(entry), entry.flags)
            for entry in patched.entries
        ]
        build_entries.extend(
            PckBuildEntry(path, additions[path]) for path in sorted(additions)
        )
        build_pck(
            output_pck,
            build_entries,
            engine_version=patched.engine_version,
            alignment=patched.inferred_alignment,
            reserved=patched.reserved,
        )
        intermediate.unlink()
    else:
        os.replace(intermediate, output_pck)
    return read_pck(output_pck, verify_md5=True)


def _verify_pck_diff(
    source_pck: Path,
    output_pck: Path,
    intended: Mapping[str, bytes],
) -> list[dict[str, Any]]:
    source = read_pck(source_pck, verify_md5=True)
    output = read_pck(output_pck, verify_md5=True)
    expected_paths = set(source.paths) | set(intended)
    if set(output.paths) != expected_paths:
        raise IntegrationError("generated PCK path set differs from source plus intended additions")
    changes: list[dict[str, Any]] = []
    for path in sorted(expected_paths):
        new = output.read_entry(path)
        if path in source.entries_by_path:
            old = source.read_entry(path)
            if old == new:
                if path in intended:
                    raise IntegrationError(f"intended PCK replacement is byte-identical: {path}")
                continue
            if path not in intended:
                raise IntegrationError(f"unintended PCK entry changed: {path}")
            action = "replace"
            old_sha = sha256_bytes(old)
        else:
            if path not in intended:
                raise IntegrationError(f"unintended PCK entry was added: {path}")
            action = "add"
            old_sha = None
        if new != intended[path]:
            raise IntegrationError(f"generated PCK payload differs from staged bytes: {path}")
        changes.append(
            {
                "path": path,
                "action": action,
                "source_sha256": old_sha,
                "output_sha256": sha256_bytes(new),
                "output_size": len(new),
            }
        )
    if {item["path"] for item in changes} != set(intended):
        raise IntegrationError("verified changed-entry set does not equal intended-entry set")
    return changes


FontBuilder = Callable[..., Mapping[str, Any]]
ResourceBuilder = Callable[..., Any]


def integrate_localization(
    *,
    build_id: str,
    source_exe: Path | str,
    source_pck: Path | str,
    recovered_dir: Path | str,
    segments_path: Path | str,
    source_manifest_path: Path | str,
    assignments_path: Path | str,
    expected_segments_sha256: str,
    expected_manifest_sha256: str,
    expected_assignments_sha256: str,
    assets_root: Path | str,
    godot_binary: Path | str,
    gdre_binary: Path | str,
    output_root: Path | str,
    pilot: bool = False,
    japanese_allowlist_path: Path | str | None = None,
    preserved_fragments_path: Path | str | None = None,
    _expected_exe_sha256: str = TARGET_EXE_SHA256,
    _expected_pck_sha256: str = TARGET_PCK_SHA256,
    _font_builder: FontBuilder = fonts.build_font_bundle,
    _resource_builder: ResourceBuilder = resources.build_changed_resources,
) -> dict[str, Any]:
    """Build and validate a fresh generated PCK and deterministic manifest."""

    if build_id != TARGET_BUILD_ID:
        raise IntegrationError(
            f"target build ID mismatch: expected {TARGET_BUILD_ID}, got {build_id!r}"
        )
    exe = _strict_file(source_exe, "source executable")
    pck = _strict_file(source_pck, "source PCK")
    recovered = _strict_directory(recovered_dir, "recovered project")
    canonical = _strict_file(segments_path, "canonical segments")
    source_manifest = _strict_file(source_manifest_path, "source manifest")
    assignments = _strict_file(assignments_path, "canonical assignments")
    japanese_allowlist_file = (
        _strict_file(japanese_allowlist_path, "Japanese allowlist")
        if japanese_allowlist_path is not None else None
    )
    japanese_allowlist_bytes: bytes | None = None
    japanese_allowlist_hash: str | None = None
    preserved_fragments_file = (
        _strict_file(preserved_fragments_path, "preserved Japanese fragments")
        if preserved_fragments_path is not None else None
    )
    preserved_fragments_bytes: bytes | None = None
    preserved_fragments_hash: str | None = None
    try:
        if japanese_allowlist_file is not None:
            # Parse and hash one read snapshot.  The final immutable-input
            # check below rejects any path mutation after this snapshot.
            japanese_allowlist_bytes = japanese_allowlist_file.read_bytes()
            japanese_allowlist_hash = sha256_bytes(japanese_allowlist_bytes)
            japanese_allowlist = qa.load_japanese_allowlist_bytes(
                japanese_allowlist_bytes, source=str(japanese_allowlist_file),
            )
        else:
            japanese_allowlist = None
        if preserved_fragments_file is not None:
            preserved_fragments_bytes = preserved_fragments_file.read_bytes()
            if preserved_fragments_bytes.startswith(b"\xef\xbb\xbf"):
                raise ValueError("preserved-fragment map forbids UTF-8 BOM")
            preserved_fragments_hash = sha256_bytes(preserved_fragments_bytes)
            preserved_fragments = coverage_validator._normalise_preserved_fragments(
                json.loads(preserved_fragments_bytes.decode("utf-8"))
            )
        else:
            preserved_fragments = None
    except ValueError as exc:
        raise IntegrationError(str(exc)) from exc
    assets = _strict_directory(assets_root, "font assets")
    godot = _strict_file(godot_binary, "Godot binary", executable=True)
    gdre = _strict_file(gdre_binary, "GDRETools binary", executable=True)
    protected_inputs = [
        exe, pck, recovered, canonical, source_manifest, assignments, assets, godot, gdre,
    ]
    if japanese_allowlist_file is not None:
        protected_inputs.append(japanese_allowlist_file)
    if preserved_fragments_file is not None:
        protected_inputs.append(preserved_fragments_file)
    output = _fresh_output(
        output_root,
        tuple(protected_inputs),
    )
    input_hashes = {
        "executable": _require_hash_pin(exe, _expected_exe_sha256, "source executable"),
        "pck": _require_hash_pin(pck, _expected_pck_sha256, "source PCK"),
        "segments": _require_hash_pin(canonical, expected_segments_sha256, "canonical segments"),
        "source_manifest": _require_hash_pin(
            source_manifest, expected_manifest_sha256, "source manifest"
        ),
        "assignments": _require_hash_pin(
            assignments, expected_assignments_sha256, "canonical assignments"
        ),
        "godot": sha256_file(godot),
        "gdre": sha256_file(gdre),
    }
    if japanese_allowlist_file is not None:
        assert japanese_allowlist_hash is not None
        input_hashes["japanese_allowlist"] = japanese_allowlist_hash
    if preserved_fragments_file is not None:
        assert preserved_fragments_hash is not None
        input_hashes["preserved_japanese_fragments"] = preserved_fragments_hash
    source_archive = read_pck(pck, verify_md5=True)
    if source_archive.engine_version != (4, 6, 3):
        raise IntegrationError(
            f"source PCK engine version mismatch: {source_archive.engine_version}"
        )
    header, all_rows, effective_rows, integrated_rows = _read_canonical(
        canonical,
        assignments,
        recovered,
        build_id=build_id,
        pilot=pilot,
        japanese_allowlist=japanese_allowlist,
        preserved_fragments=preserved_fragments,
    )
    _verify_source_manifest(source_manifest, recovered, all_rows)

    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    immutable_inputs = [exe, pck, canonical, source_manifest, assignments]
    if japanese_allowlist_file is not None:
        immutable_inputs.append(japanese_allowlist_file)
    if preserved_fragments_file is not None:
        immutable_inputs.append(preserved_fragments_file)
    source_before = {path: sha256_file(path) for path in immutable_inputs}
    if japanese_allowlist_file is not None:
        assert japanese_allowlist_hash is not None
        # Use the same bytes that were parsed above as the pin.  Hashing the
        # path again here would permit a pre-check mutation to pair a stale
        # parsed map with a newer manifest hash.
        source_before[japanese_allowlist_file] = japanese_allowlist_hash
    if preserved_fragments_file is not None:
        assert preserved_fragments_hash is not None
        source_before[preserved_fragments_file] = preserved_fragments_hash
    try:
        localized = stage / "localized-source"
        shutil.copytree(recovered, localized)
        effective_tsv = stage / "effective-segments.tsv"
        segments.write_tsv_atomic(effective_tsv, effective_rows, header)
        reinserted = stage / "reinserted"
        reinsertion = segments.build_resources(recovered, effective_tsv, reinserted)
        _overlay_tree(reinserted, localized)

        generated_code = stage / "korean-code" / "data" / "game_data.gd"
        code_patch = korean_code.patch_generated_file(
            localized / "data" / "game_data.gd", generated_code
        )
        shutil.copyfile(generated_code, localized / "data" / "game_data.gd")

        hint_answer_row_integrated = any(
            row["source_path"] == "data/hint.gd"
            and row["source_text"] == "答え ── "
            for row in integrated_rows
        )
        hint_code_patch: dict[str, str] | None = None
        if hint_answer_row_integrated:
            generated_hint = stage / "korean-code" / "data" / "hint.gd"
            hint_code_patch = korean_code.patch_hint_generated_file(
                localized / "data" / "hint.gd", generated_hint
            )
            shutil.copyfile(generated_hint, localized / "data" / "hint.gd")

        generated_desktop = stage / "runtime-capture" / "scripts" / "desktop.gd"
        runtime_capture_patch = runtime_capture.patch_generated_file(
            localized / "scripts" / "desktop.gd", generated_desktop
        )
        shutil.copyfile(generated_desktop, localized / "scripts" / "desktop.gd")

        font_root = stage / "font-bundle"
        font_manifest = _font_builder(
            assets_root=assets,
            recovered_ui_kit=localized / "scripts" / "ui" / "ui_kit.gd",
            godot_binary=godot,
            output_root=font_root,
        )
        on_disk_font_manifest = json.loads(
            (font_root / "font_manifest.json").read_text(encoding="utf-8")
        )
        if on_disk_font_manifest != font_manifest:
            raise IntegrationError("font component manifest did not round-trip")
        shutil.copyfile(
            font_root / "scripts" / "ui" / "ui_kit.gd",
            localized / "scripts" / "ui" / "ui_kit.gd",
        )

        changed_sources = _changed_sources(recovered, localized)
        required_code_units = {
            "data/game_data.gd", "scripts/desktop.gd", "scripts/ui/ui_kit.gd",
        }
        if hint_answer_row_integrated:
            required_code_units.add("data/hint.gd")
        if not required_code_units.issubset(changed_sources):
            raise IntegrationError("mandatory Korean code/font source changes are missing")
        resource_root = stage / "resource-build"
        resource_result = _resource_builder(
            recovered_dir=recovered,
            localized_root=localized,
            source_pck=pck,
            output_root=resource_root,
            gdre_path=gdre,
            changed_paths=changed_sources,
        )
        resource_manifest = json.loads(
            resource_result.manifest_path.read_text(encoding="utf-8")
        )
        compiled_entries = _resource_entries(resource_result)
        font_entries = _runtime_font_entries(font_root, font_manifest)
        collisions = set(compiled_entries) & set(font_entries)
        if collisions:
            raise IntegrationError(f"component archive-path collision: {sorted(collisions)}")
        intended_entries = {**compiled_entries, **font_entries}
        if not intended_entries:
            raise IntegrationError("integration emitted no PCK entries")

        output_pck = stage / PCK_NAME
        output_archive = _build_output_pck(pck, output_pck, intended_entries)
        changes = _verify_pck_diff(pck, output_pck, intended_entries)
        pck_validation = output_archive.validate(verify_md5=True)
        source_after = {path: sha256_file(path) for path in source_before}
        if source_after != source_before:
            raise IntegrationError("an immutable input changed during integration")

        status_counts = Counter(row["status"].upper() for row in integrated_rows)
        manifest: dict[str, Any] = {
            "schema_version": INTEGRATION_SCHEMA_VERSION,
            "tool": "tools/integrate.py",
            "build_id": build_id,
            "mode": "pilot" if pilot else "production",
            "inputs": {
                "sha256": input_hashes,
                "source_pck_entries": len(source_archive.entries),
                "source_pck_engine_version": list(source_archive.engine_version),
            },
            "merge_counts": {
                "canonical_rows": len(all_rows),
                "integrated_rows": len(integrated_rows),
                "integrated_by_status": dict(sorted(status_counts.items())),
                "changed_source_paths": len(changed_sources),
                "compiled_resource_entries": len(compiled_entries),
                "font_runtime_entries": len(font_entries),
                "pck_replaced_entries": sum(item["action"] == "replace" for item in changes),
                "pck_added_entries": sum(item["action"] == "add" for item in changes),
            },
            "integrated_segment_ids": sorted(row["segment_id"] for row in integrated_rows),
            "modified_source_paths": changed_sources,
            "mandatory_code_patch": {
                "source_sha256": code_patch["source_sha256"],
                "output_sha256": code_patch["output_sha256"],
            },
            "hint_answer_period_patch": (
                None
                if hint_code_patch is None
                else {
                    "source_sha256": hint_code_patch["source_sha256"],
                    "output_sha256": hint_code_patch["output_sha256"],
                }
            ),
            "runtime_capture_patch": {
                "path": "scripts/desktop.gd",
                "source_sha256": runtime_capture_patch["source_sha256"],
                "output_sha256": runtime_capture_patch["output_sha256"],
                "tool_sha256": sha256_file(Path(runtime_capture.__file__)),
                "report_cases_default": runtime_capture_patch["report_cases_default"],
            },
            "components": {
                "font_manifest_sha256": sha256_file(font_root / "font_manifest.json"),
                "resource_manifest_sha256": sha256_file(resource_result.manifest_path),
                "font": font_manifest,
                "resources": resource_manifest,
            },
            "changed_entries": changes,
            "output": {
                "pck_path": PCK_NAME,
                "pck_sha256": sha256_file(output_pck),
                "pck_entries": len(output_archive.entries),
                "verified_md5_entries": pck_validation.verified_md5_count,
                "alignment": pck_validation.inferred_alignment,
            },
        }
        manifest_path = stage / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # Intermediate source trees contain original data and are never part of
        # the published generated integration root.
        for temporary_path in (
            localized, reinserted, effective_tsv, stage / "korean-code",
            stage / "runtime-capture", font_root, resource_root,
        ):
            if temporary_path.is_dir():
                shutil.rmtree(temporary_path)
            else:
                temporary_path.unlink(missing_ok=True)
        stage.rename(output)
    except BaseException as exc:
        shutil.rmtree(stage, ignore_errors=True)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, IntegrationError):
            raise
        raise IntegrationError(f"integration component failed: {exc}") from exc

    result = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    result["manifest_path"] = str(output / MANIFEST_NAME)
    result["manifest_sha256"] = sha256_file(output / MANIFEST_NAME)
    result["output_pck"] = str(output / PCK_NAME)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--source-exe", required=True, type=Path)
    parser.add_argument("--source-pck", required=True, type=Path)
    parser.add_argument("--recovered-dir", required=True, type=Path)
    parser.add_argument("--segments", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--assignments", required=True, type=Path)
    parser.add_argument("--expected-segments-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-assignments-sha256", required=True)
    parser.add_argument("--assets-root", required=True, type=Path)
    parser.add_argument("--godot", required=True, type=Path)
    parser.add_argument("--gdre", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument(
        "--japanese-allowlist", type=Path,
        help="JSON object mapping segment IDs to exact preserved Japanese fragments",
    )
    parser.add_argument(
        "--preserved-fragments", type=Path,
        help="JSON object mapping segment IDs to intentional exact source-script fragments",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = integrate_localization(
            build_id=args.build_id,
            source_exe=args.source_exe,
            source_pck=args.source_pck,
            recovered_dir=args.recovered_dir,
            segments_path=args.segments,
            source_manifest_path=args.source_manifest,
            assignments_path=args.assignments,
            expected_segments_sha256=args.expected_segments_sha256,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_assignments_sha256=args.expected_assignments_sha256,
            assets_root=args.assets_root,
            godot_binary=args.godot,
            gdre_binary=args.gdre,
            output_root=args.output_root,
            pilot=args.pilot,
            japanese_allowlist_path=args.japanese_allowlist,
            preserved_fragments_path=args.preserved_fragments,
        )
    except IntegrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
