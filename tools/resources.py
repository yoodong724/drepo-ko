#!/usr/bin/env python3
"""Fail-closed compiler for generated Godot localization resources.

The localized source tree is a generated input.  This module never edits it,
the recovered project, or the source PCK.  It writes a fresh output directory
whose ``resources`` child mirrors archive-relative PCK paths and records every
emitted byte in a deterministic JSON manifest.

Only explicitly named text, GDScript, and scene sources are accepted.  Godot
source files are converted with one GDRETools invocation per source and per
temporary output directory because GDRETools flattens output basenames.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Iterable, Sequence

try:
    from tools.pck import PckArchive, read_pck
except ModuleNotFoundError:  # Direct execution: python tools/resources.py
    from pck import PckArchive, read_pck


RESOURCE_BUILD_SCHEMA = 1
DEFAULT_BYTECODE_REVISION = "ebc36a7"
_TEXT_EXTENSIONS = frozenset({".json", ".txt"})
_SOURCE_EXTENSIONS = _TEXT_EXTENSIONS | {".gd", ".tscn"}
_SCENE_EXPORT_RE = re.compile(
    r"(?:^|/)export-(?P<digest>[0-9a-f]{32})-(?P<basename>[^/]+\.scn)$"
)


class ResourceBuildError(ValueError):
    """An input, mapping, tool result, or output location is unsafe."""


@dataclass(frozen=True, slots=True)
class BuiltResource:
    source_path: str
    archive_path: str
    resource_kind: str
    recovered_sha256: str
    localized_sha256: str
    output_sha256: str
    output_size: int


@dataclass(frozen=True, slots=True)
class VerifiedResource:
    source_path: str
    archive_path: str
    resource_kind: str
    source_sha256: str
    compiled_sha256: str
    result: str


@dataclass(frozen=True, slots=True)
class ResourceBuildResult:
    output_root: Path
    resources_root: Path
    manifest_path: Path
    built: tuple[BuiltResource, ...]
    verified_unchanged: tuple[VerifiedResource, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_source_path(value: str | os.PathLike[str]) -> str:
    """Return a strict, archive-compatible relative POSIX source path."""

    raw = os.fspath(value)
    if not raw or "\\" in raw or "\0" in raw:
        raise ResourceBuildError(f"invalid source path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("res://"):
        raise ResourceBuildError(f"source path must be relative, not {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ResourceBuildError(f"unsafe source path: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise ResourceBuildError(f"source path is not in canonical POSIX form: {raw!r}")
    if Path(normalized).suffix.lower() not in _SOURCE_EXTENSIONS:
        raise ResourceBuildError(f"unsupported changed resource type: {normalized!r}")
    return normalized


def scene_archive_path(source_path: str, archive: PckArchive) -> str:
    """Resolve and verify a Godot exported-scene mapping from archive entries."""

    source = normalize_source_path(source_path)
    if not source.endswith(".tscn"):
        raise ResourceBuildError(f"not a text scene path: {source!r}")
    res_path = f"res://{source}"
    expected_digest = hashlib.md5(res_path.encode("utf-8")).hexdigest()
    expected_basename = f"{PurePosixPath(source).stem}.scn"
    candidates: list[str] = []
    for archive_path in archive.paths:
        match = _SCENE_EXPORT_RE.search(archive_path)
        if match is None:
            continue
        if (
            match.group("digest") == expected_digest
            and match.group("basename") == expected_basename
        ):
            candidates.append(archive_path)
    if len(candidates) != 1:
        raise ResourceBuildError(
            f"expected exactly one verified export mapping for {source!r}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def script_archive_path(source_path: str, archive: PckArchive) -> str:
    source = normalize_source_path(source_path)
    if not source.endswith(".gd"):
        raise ResourceBuildError(f"not a GDScript source path: {source!r}")
    destination = str(PurePosixPath(source).with_suffix(".gdc"))
    if destination not in archive.entries_by_path:
        raise ResourceBuildError(f"script destination is absent from source PCK: {destination!r}")
    return destination


def build_changed_resources(
    *,
    recovered_dir: str | os.PathLike[str],
    localized_root: str | os.PathLike[str],
    source_pck: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    gdre_path: str | os.PathLike[str],
    changed_paths: Iterable[str | os.PathLike[str]],
    bytecode_revision: str = DEFAULT_BYTECODE_REVISION,
    verify_unchanged_scripts: bool = False,
) -> ResourceBuildResult:
    """Compile and stage explicitly listed generated localization resources.

    The output root must not exist.  It is assembled beside its final path and
    atomically renamed only after all mappings, baselines, compiler results,
    magic bytes, and deterministic second builds have passed.
    """

    recovered = _require_directory(recovered_dir, "recovered project")
    localized = _require_directory(localized_root, "localized source root")
    source_archive_path = _require_regular_file(source_pck, "source PCK")
    tool = _require_regular_file(gdre_path, "GDRETools executable")
    if not os.access(tool, os.X_OK):
        raise ResourceBuildError(f"GDRETools path is not executable: {tool}")
    if not bytecode_revision or not re.fullmatch(r"[A-Za-z0-9._-]+", bytecode_revision):
        raise ResourceBuildError(f"invalid bytecode revision: {bytecode_revision!r}")

    output = Path(output_root).absolute()
    _validate_fresh_output(output, protected=(recovered, localized, source_archive_path))
    normalized_paths = tuple(normalize_source_path(path) for path in changed_paths)
    if len(set(normalized_paths)) != len(normalized_paths):
        raise ResourceBuildError("changed_paths contains a duplicate source path")
    paths = tuple(sorted(normalized_paths))
    _precheck_collisions(paths)

    archive = read_pck(source_archive_path, verify_md5=True)
    source_pck_sha = sha256_file(source_archive_path)
    gdre_sha = sha256_file(tool)

    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    resources_root = stage / "resources"
    resources_root.mkdir()
    built: list[BuiltResource] = []
    verified: list[VerifiedResource] = []
    try:
        for source_path in paths:
            recovered_path = _strict_input_file(recovered, source_path, "recovered")
            localized_path = _strict_input_file(localized, source_path, "localized")
            recovered_data = recovered_path.read_bytes()
            localized_data = localized_path.read_bytes()
            archive_path = _resolve_and_verify_mapping(
                source_path, recovered, recovered_data, archive
            )

            if localized_data == recovered_data:
                if verify_unchanged_scripts and source_path.endswith(".gd"):
                    compiled = _compile_twice(
                        tool=tool,
                        project_root=localized,
                        source_path=source_path,
                        kind="script",
                        bytecode_revision=bytecode_revision,
                    )
                    original = archive.read_entry(archive_path)
                    if compiled != original:
                        raise ResourceBuildError(
                            f"unchanged script does not compile byte-identically: {source_path!r}"
                        )
                    verified.append(
                        VerifiedResource(
                            source_path=source_path,
                            archive_path=archive_path,
                            resource_kind="gdscript",
                            source_sha256=sha256_bytes(recovered_data),
                            compiled_sha256=sha256_bytes(compiled),
                            result="BYTE_IDENTICAL",
                        )
                    )
                continue

            suffix = PurePosixPath(source_path).suffix.lower()
            if suffix == ".gd":
                output_data = _compile_twice(
                    tool=tool,
                    project_root=localized,
                    source_path=source_path,
                    kind="script",
                    bytecode_revision=bytecode_revision,
                )
                kind = "gdscript"
            elif suffix == ".tscn":
                output_data = _compile_twice(
                    tool=tool,
                    project_root=localized,
                    source_path=source_path,
                    kind="scene",
                    bytecode_revision=bytecode_revision,
                )
                kind = "scene"
            else:
                localized_data.decode("utf-8", errors="strict")
                output_data = localized_data
                kind = "text"

            destination = resources_root.joinpath(*PurePosixPath(archive_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(output_data)
            built.append(
                BuiltResource(
                    source_path=source_path,
                    archive_path=archive_path,
                    resource_kind=kind,
                    recovered_sha256=sha256_bytes(recovered_data),
                    localized_sha256=sha256_bytes(localized_data),
                    output_sha256=sha256_bytes(output_data),
                    output_size=len(output_data),
                )
            )

        manifest = {
            "schema_version": RESOURCE_BUILD_SCHEMA,
            "source_pck_sha256": source_pck_sha,
            "gdre_sha256": gdre_sha,
            "bytecode_revision": bytecode_revision,
            "built_resources": [asdict(item) for item in built],
            "verified_unchanged": [asdict(item) for item in verified],
        }
        manifest_path = stage / "resource-build-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        _verify_stage(stage, built, manifest)
        stage.rename(output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return ResourceBuildResult(
        output_root=output,
        resources_root=output / "resources",
        manifest_path=output / "resource-build-manifest.json",
        built=tuple(built),
        verified_unchanged=tuple(verified),
    )


def _resolve_and_verify_mapping(
    source_path: str,
    recovered_root: Path,
    recovered_data: bytes,
    archive: PckArchive,
) -> str:
    suffix = PurePosixPath(source_path).suffix.lower()
    if suffix == ".gd":
        archive_path = script_archive_path(source_path, archive)
        original = archive.read_entry(archive_path)
        if not original.startswith(b"GDSC"):
            raise ResourceBuildError(f"original GDScript has invalid magic: {archive_path!r}")
        recovered_compiled = _strict_input_file(
            recovered_root, f".autoconverted/{archive_path}", "recovered compiled"
        ).read_bytes()
        if recovered_compiled != original:
            raise ResourceBuildError(
                f"recovered compiled baseline differs from source PCK: {archive_path!r}"
            )
        return archive_path
    if suffix == ".tscn":
        archive_path = scene_archive_path(source_path, archive)
        original = archive.read_entry(archive_path)
        if not original.startswith(b"RSRC"):
            raise ResourceBuildError(f"original scene has invalid magic: {archive_path!r}")
        recovered_export = _strict_input_file(
            recovered_root, archive_path, "recovered exported scene"
        ).read_bytes()
        if recovered_export != original:
            raise ResourceBuildError(
                f"recovered scene baseline differs from source PCK: {archive_path!r}"
            )
        return archive_path

    archive_path = source_path
    if archive_path not in archive.entries_by_path:
        raise ResourceBuildError(f"text resource is absent from source PCK: {archive_path!r}")
    original = archive.read_entry(archive_path)
    if recovered_data != original:
        raise ResourceBuildError(
            f"recovered text baseline differs from source PCK: {archive_path!r}"
        )
    recovered_data.decode("utf-8", errors="strict")
    return archive_path


def _compile_twice(
    *,
    tool: Path,
    project_root: Path,
    source_path: str,
    kind: str,
    bytecode_revision: str,
) -> bytes:
    first = _run_gdre(
        tool=tool,
        project_root=project_root,
        source_path=source_path,
        kind=kind,
        bytecode_revision=bytecode_revision,
    )
    second = _run_gdre(
        tool=tool,
        project_root=project_root,
        source_path=source_path,
        kind=kind,
        bytecode_revision=bytecode_revision,
    )
    if first != second:
        raise ResourceBuildError(f"non-deterministic {kind} compiler output: {source_path!r}")
    return first


def _run_gdre(
    *,
    tool: Path,
    project_root: Path,
    source_path: str,
    kind: str,
    bytecode_revision: str,
) -> bytes:
    output_suffix = ".gdc" if kind == "script" else ".scn"
    expected_name = f"{PurePosixPath(source_path).stem}{output_suffix}"
    with tempfile.TemporaryDirectory(prefix="dgr-gdre-run-") as run_dir_raw:
        run_dir = Path(run_dir_raw)
        output_dir = run_dir / "output"
        output_dir.mkdir()
        home = run_dir / "home"
        config = run_dir / "config"
        cache = run_dir / "cache"
        data = run_dir / "data"
        for directory in (home, config, cache, data):
            directory.mkdir()
        if kind == "script":
            command = [
                str(tool),
                "--headless",
                f"--compile=res://{source_path}",
                f"--bytecode={bytecode_revision}",
                f"--output={output_dir}",
            ]
            magic = b"GDSC"
        elif kind == "scene":
            command = [
                str(tool),
                "--headless",
                f"--txt-to-bin=res://{source_path}",
                f"--output={output_dir}",
            ]
            magic = b"RSRC"
        else:
            raise AssertionError(kind)
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(cache),
                "XDG_DATA_HOME": str(data),
            }
        )
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ResourceBuildError(f"GDRETools could not compile {source_path!r}: {exc}") from exc
        if completed.returncode != 0:
            log = completed.stdout.decode("utf-8", errors="replace")[-4000:]
            raise ResourceBuildError(
                f"GDRETools failed for {source_path!r} with exit {completed.returncode}:\n{log}"
            )
        outputs = sorted(path for path in output_dir.rglob("*") if path.is_file())
        expected = output_dir / expected_name
        if outputs != [expected]:
            relative = [path.relative_to(output_dir).as_posix() for path in outputs]
            raise ResourceBuildError(
                f"unexpected GDRETools outputs for {source_path!r}: {relative!r}"
            )
        result = expected.read_bytes()
        if not result.startswith(magic):
            raise ResourceBuildError(
                f"GDRETools output has invalid {kind} magic for {source_path!r}"
            )
        if kind == "scene" and f"res://{source_path}".encode("utf-8") not in result:
            raise ResourceBuildError(
                f"compiled scene does not retain its res:// source identity: {source_path!r}"
            )
        return result


def _strict_input_file(root: Path, relative: str, label: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise ResourceBuildError(f"{label} input is not a regular non-symlink file: {relative!r}")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ResourceBuildError(f"{label} input escapes its root: {relative!r}") from exc
    return path


def _precheck_collisions(paths: Sequence[str]) -> None:
    for suffix in (".gd", ".tscn"):
        seen: dict[str, str] = {}
        for path in paths:
            if PurePosixPath(path).suffix.lower() != suffix:
                continue
            basename = PurePosixPath(path).name.casefold()
            if basename in seen:
                raise ResourceBuildError(
                    f"GDRETools flattened basename collision: {seen[basename]!r} and {path!r}"
                )
            seen[basename] = path


def _verify_stage(
    stage: Path, built: Sequence[BuiltResource], manifest: dict[str, object]
) -> None:
    expected = {item.archive_path for item in built}
    resources = stage / "resources"
    actual = {
        path.relative_to(resources).as_posix()
        for path in resources.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ResourceBuildError(
            f"staged resource set mismatch: expected {sorted(expected)!r}, got {sorted(actual)!r}"
        )
    for item in built:
        path = resources.joinpath(*PurePosixPath(item.archive_path).parts)
        if sha256_file(path) != item.output_sha256 or path.stat().st_size != item.output_size:
            raise ResourceBuildError(f"staged resource hash mismatch: {item.archive_path!r}")
    manifest_path = stage / "resource-build-manifest.json"
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    if parsed != manifest:
        raise ResourceBuildError("resource build manifest did not round-trip exactly")


def _require_directory(path: str | os.PathLike[str], label: str) -> Path:
    result = Path(path).absolute()
    if result.is_symlink() or not result.is_dir():
        raise ResourceBuildError(f"{label} is not a regular directory: {result}")
    return result.resolve()


def _require_regular_file(path: str | os.PathLike[str], label: str) -> Path:
    result = Path(path).absolute()
    if result.is_symlink() or not result.is_file():
        raise ResourceBuildError(f"{label} is not a regular non-symlink file: {result}")
    return result.resolve()


def _validate_fresh_output(output: Path, *, protected: Sequence[Path]) -> None:
    if output.exists() or output.is_symlink():
        raise ResourceBuildError(f"output root already exists: {output}")
    if not output.parent.is_dir():
        raise ResourceBuildError(f"output parent does not exist: {output.parent}")
    resolved_parent = output.parent.resolve()
    candidate = resolved_parent / output.name
    for item in protected:
        if item.is_file():
            if candidate == item:
                raise ResourceBuildError(f"output root equals protected input: {item}")
            try:
                item.relative_to(candidate)
            except ValueError:
                continue
            raise ResourceBuildError(f"output root would contain protected input: {item}")
        root = item
        try:
            candidate.relative_to(root)
        except ValueError:
            pass
        else:
            raise ResourceBuildError(f"output root is inside protected input: {root}")
        try:
            root.relative_to(candidate)
        except ValueError:
            pass
        else:
            raise ResourceBuildError(f"output root would contain protected input: {root}")


def _read_changes_file(path: Path) -> list[str]:
    values: list[str] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        try:
            values.append(normalize_source_path(value))
        except ResourceBuildError as exc:
            raise ResourceBuildError(f"{path}:{line_number}: {exc}") from exc
    return values


def _all_recovered_scripts(recovered: Path) -> list[str]:
    result: list[str] = []
    for path in recovered.rglob("*.gd"):
        if path.is_symlink() or ".autoconverted" in path.parts:
            continue
        result.append(path.relative_to(recovered).as_posix())
    return sorted(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--recovered-dir", required=True, type=Path)
        subparser.add_argument("--source-pck", required=True, type=Path)
        subparser.add_argument("--output-root", required=True, type=Path)
        subparser.add_argument("--gdre", required=True, type=Path)
        subparser.add_argument("--bytecode", default=DEFAULT_BYTECODE_REVISION)

    build_parser = subparsers.add_parser("build", help="compile explicitly changed resources")
    common(build_parser)
    build_parser.add_argument("--localized-root", required=True, type=Path)
    build_parser.add_argument("--changed-path", action="append", default=[])
    build_parser.add_argument("--changes-file", type=Path)
    build_parser.add_argument("--verify-unchanged-scripts", action="store_true")

    verify_parser = subparsers.add_parser(
        "verify-scripts", help="compile every recovered GDScript and compare to the source PCK"
    )
    common(verify_parser)

    args = parser.parse_args(argv)
    if args.command == "build":
        changes = list(args.changed_path)
        if args.changes_file is not None:
            changes.extend(_read_changes_file(args.changes_file))
        result = build_changed_resources(
            recovered_dir=args.recovered_dir,
            localized_root=args.localized_root,
            source_pck=args.source_pck,
            output_root=args.output_root,
            gdre_path=args.gdre,
            changed_paths=changes,
            bytecode_revision=args.bytecode,
            verify_unchanged_scripts=args.verify_unchanged_scripts,
        )
    else:
        recovered = _require_directory(args.recovered_dir, "recovered project")
        result = build_changed_resources(
            recovered_dir=recovered,
            localized_root=recovered,
            source_pck=args.source_pck,
            output_root=args.output_root,
            gdre_path=args.gdre,
            changed_paths=_all_recovered_scripts(recovered),
            bytecode_revision=args.bytecode,
            verify_unchanged_scripts=True,
        )
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "built": len(result.built),
                "verified_unchanged": len(result.verified_unchanged),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
