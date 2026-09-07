"""Strict, dependency-free helpers for unencrypted Godot PCK v3 archives.

The reader intentionally supports only the subset used by this project: a
standalone PCK v3 archive with a relative file base, a clear-text directory,
and unencrypted files. Unsupported flags fail closed instead of producing
plausible but unsafe output.

All write operations require an explicit output path whose parent already
exists. Writes are staged beside the output and atomically replaced only
after the staged archive parses and passes MD5 validation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import struct
import tempfile
from typing import Iterable, Mapping, Sequence


PCK_MAGIC = b"GDPC"
PCK_FORMAT_VERSION = 3
PACK_DIR_ENCRYPTED = 1 << 0
PACK_REL_FILEBASE = 1 << 1
PACK_SPARSE_BUNDLE = 1 << 2
PACK_FILE_ENCRYPTED = 1 << 0
PACK_FILE_REMOVAL = 1 << 1
PACK_FILE_DELTA = 1 << 2

_HEADER = struct.Struct("<4s5IQQ")
_HEADER_SIZE = _HEADER.size + 64
_U32 = struct.Struct("<I")
_U64_PAIR = struct.Struct("<QQ")
_ENTRY_TAIL_SIZE = _U64_PAIR.size + 16 + _U32.size
_KNOWN_PACK_FLAGS = PACK_DIR_ENCRYPTED | PACK_REL_FILEBASE | PACK_SPARSE_BUNDLE
_KNOWN_FILE_FLAGS = PACK_FILE_ENCRYPTED | PACK_FILE_REMOVAL | PACK_FILE_DELTA
_MAX_PATH_BYTES = 1024 * 1024
_MAX_ALIGNMENT = 1024 * 1024


class PckError(ValueError):
    """Base class for PCK parsing, validation, and safety failures."""


class PckFormatError(PckError):
    """The input is not a supported, structurally valid PCK v3 archive."""


class PckIntegrityError(PckError):
    """Stored content does not match its directory metadata."""


class PckSafetyError(PckError):
    """A requested filesystem operation is unsafe or insufficiently explicit."""


@dataclass(frozen=True, slots=True)
class PckEntry:
    """One directory entry, preserving its original directory order."""

    path: str
    relative_offset: int
    offset: int
    size: int
    md5: bytes
    flags: int
    directory_index: int

    @property
    def md5_hex(self) -> str:
        return self.md5.hex()

    @property
    def is_removal(self) -> bool:
        return bool(self.flags & PACK_FILE_REMOVAL)


@dataclass(frozen=True, slots=True)
class PckValidation:
    """Summary of a completed archive validation."""

    file_count: int
    payload_size: int
    verified_md5_count: int
    inferred_alignment: int


@dataclass(frozen=True, slots=True)
class PckBuildEntry:
    """A file supplied to build_pck."""

    path: str
    data: bytes
    flags: int = 0


@dataclass(frozen=True, slots=True)
class PckArchive:
    """Parsed metadata for a standalone, unencrypted PCK v3 archive."""

    source_path: Path
    file_size: int
    format_version: int
    engine_version: tuple[int, int, int]
    flags: int
    file_base: int
    directory_offset: int
    reserved: bytes
    entries: tuple[PckEntry, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    @property
    def entries_by_path(self) -> Mapping[str, PckEntry]:
        return {entry.path: entry for entry in self.entries}

    @property
    def inferred_alignment(self) -> int:
        positions = [self.file_base, self.directory_offset]
        positions.extend(entry.offset for entry in self.entries if not entry.is_removal)
        return math.gcd(*positions) if positions else 1

    def get_entry(self, path: str) -> PckEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)

    def read_entry(self, entry_or_path: PckEntry | str, *, verify_md5: bool = True) -> bytes:
        entry = (
            entry_or_path
            if isinstance(entry_or_path, PckEntry)
            else self.get_entry(entry_or_path)
        )
        if entry not in self.entries:
            raise PckSafetyError("entry does not belong to this archive")
        if entry.is_removal:
            raise PckSafetyError(f"removal entry has no payload: {entry.path}")
        with self.source_path.open("rb") as source:
            source.seek(entry.offset)
            data = source.read(entry.size)
        if len(data) != entry.size:
            raise PckIntegrityError(f"short payload read for {entry.path!r}")
        if verify_md5 and hashlib.md5(data).digest() != entry.md5:
            raise PckIntegrityError(f"MD5 mismatch for {entry.path!r}")
        return data

    def validate(
        self,
        *,
        verify_md5: bool = True,
        alignment: int | None = None,
    ) -> PckValidation:
        alignment = self.inferred_alignment if alignment is None else _check_alignment(alignment)
        if self.file_base % alignment or self.directory_offset % alignment:
            raise PckIntegrityError(
                f"file base or directory offset is not aligned to {alignment} bytes"
            )
        verified = 0
        payload_size = 0
        for entry in self.entries:
            if not entry.is_removal and entry.offset % alignment:
                raise PckIntegrityError(
                    f"entry {entry.path!r} is not aligned to {alignment} bytes"
                )
            payload_size += entry.size
            if verify_md5 and not entry.is_removal:
                self.read_entry(entry, verify_md5=True)
                verified += 1
        return PckValidation(
            file_count=len(self.entries),
            payload_size=payload_size,
            verified_md5_count=verified,
            inferred_alignment=self.inferred_alignment,
        )

    def extract_entry(
        self,
        entry_or_path: PckEntry | str,
        output_path: os.PathLike[str] | str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Extract one entry atomically to an explicit existing parent directory."""

        output = _prepare_output_path(output_path, overwrite=overwrite)
        if _same_path(self.source_path, output):
            raise PckSafetyError("archive source and extraction output must differ")
        data = self.read_entry(entry_or_path, verify_md5=True)
        _atomic_write_bytes(output, data, overwrite=overwrite)
        return output


def read_pck(
    path: os.PathLike[str] | str,
    *,
    verify_md5: bool = False,
) -> PckArchive:
    """Parse a standalone Godot PCK v3 archive without changing it."""

    source_path = Path(path)
    try:
        file_size = source_path.stat().st_size
    except OSError as exc:
        raise PckFormatError(f"cannot stat PCK: {source_path}") from exc
    if not source_path.is_file():
        raise PckFormatError(f"PCK is not a regular file: {source_path}")
    if file_size < _HEADER_SIZE + _U32.size:
        raise PckFormatError("file is too small for a PCK v3 header and directory")

    with source_path.open("rb") as source:
        header = _read_exact(source, _HEADER.size, "PCK header")
        magic, version, major, minor, patch, flags, file_base, directory_offset = (
            _HEADER.unpack(header)
        )
        reserved = _read_exact(source, 64, "reserved header")

        if magic != PCK_MAGIC:
            raise PckFormatError("invalid PCK magic")
        if version != PCK_FORMAT_VERSION:
            raise PckFormatError(f"unsupported PCK format version: {version}")
        if flags & ~_KNOWN_PACK_FLAGS:
            raise PckFormatError(f"unknown PCK flags: 0x{flags:x}")
        if not flags & PACK_REL_FILEBASE:
            raise PckFormatError("PCK v3 archive does not use a relative file base")
        if flags & PACK_DIR_ENCRYPTED:
            raise PckFormatError("encrypted PCK directories are unsupported")
        if flags & PACK_SPARSE_BUNDLE:
            raise PckFormatError("sparse PCK bundles are unsupported")
        if not (_HEADER_SIZE <= file_base <= directory_offset):
            raise PckFormatError("invalid file base/directory ordering")
        if directory_offset > file_size - _U32.size:
            raise PckFormatError("directory offset is outside the archive")

        source.seek(directory_offset)
        file_count = _U32.unpack(_read_exact(source, _U32.size, "file count"))[0]
        directory_bytes = file_size - source.tell()
        if file_count > directory_bytes // (_U32.size + _ENTRY_TAIL_SIZE):
            raise PckFormatError("file count cannot fit in the directory")

        entries: list[PckEntry] = []
        seen_paths: set[str] = set()
        for index in range(file_count):
            path_size = _U32.unpack(_read_exact(source, _U32.size, "path size"))[0]
            if path_size == 0 or path_size > _MAX_PATH_BYTES or path_size % 4:
                raise PckFormatError(f"invalid padded path size at entry {index}: {path_size}")
            remaining = file_size - source.tell()
            if path_size + _ENTRY_TAIL_SIZE > remaining:
                raise PckFormatError(f"directory entry {index} exceeds archive bounds")
            raw_path = _read_exact(source, path_size, f"path for entry {index}")
            path_bytes = raw_path.rstrip(b"\0")
            if b"\0" in path_bytes or any(raw_path[len(path_bytes) :]):
                raise PckFormatError(f"invalid NUL padding in path at entry {index}")
            try:
                entry_path = path_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise PckFormatError(f"invalid UTF-8 path at entry {index}") from exc
            _validate_entry_path(entry_path)
            if entry_path in seen_paths:
                raise PckFormatError(f"duplicate PCK path: {entry_path!r}")
            seen_paths.add(entry_path)

            relative_offset, size = _U64_PAIR.unpack(
                _read_exact(source, _U64_PAIR.size, f"range for {entry_path!r}")
            )
            stored_md5 = _read_exact(source, 16, f"MD5 for {entry_path!r}")
            entry_flags = _U32.unpack(
                _read_exact(source, _U32.size, f"flags for {entry_path!r}")
            )[0]
            if entry_flags & ~_KNOWN_FILE_FLAGS:
                raise PckFormatError(f"unknown flags for {entry_path!r}: 0x{entry_flags:x}")
            if entry_flags & PACK_FILE_ENCRYPTED:
                raise PckFormatError(f"encrypted entry is unsupported: {entry_path!r}")
            if entry_flags & PACK_FILE_DELTA:
                raise PckFormatError(f"delta entry is unsupported: {entry_path!r}")
            is_removal = bool(entry_flags & PACK_FILE_REMOVAL)
            if is_removal and (size != 0 or stored_md5 != bytes(16)):
                raise PckFormatError(f"invalid removal entry metadata: {entry_path!r}")

            absolute_offset = file_base + relative_offset
            if absolute_offset < file_base or absolute_offset > directory_offset:
                raise PckFormatError(f"entry offset is outside data area: {entry_path!r}")
            if size > directory_offset - absolute_offset:
                raise PckFormatError(f"entry range overlaps the directory: {entry_path!r}")
            entries.append(
                PckEntry(
                    path=entry_path,
                    relative_offset=relative_offset,
                    offset=absolute_offset,
                    size=size,
                    md5=stored_md5,
                    flags=entry_flags,
                    directory_index=index,
                )
            )

        if source.tell() != file_size:
            raise PckFormatError("trailing bytes remain after the PCK directory")

    _validate_non_overlapping_ranges(entries)
    archive = PckArchive(
        source_path=source_path,
        file_size=file_size,
        format_version=version,
        engine_version=(major, minor, patch),
        flags=flags,
        file_base=file_base,
        directory_offset=directory_offset,
        reserved=reserved,
        entries=tuple(entries),
    )
    archive.validate(verify_md5=verify_md5)
    return archive


def validate_pck(
    path: os.PathLike[str] | str,
    *,
    verify_md5: bool = True,
    alignment: int | None = None,
) -> PckArchive:
    """Parse and validate an archive, returning its metadata on success."""

    archive = read_pck(path, verify_md5=False)
    archive.validate(verify_md5=verify_md5, alignment=alignment)
    return archive


def extract_entry(
    archive_path: os.PathLike[str] | str,
    entry_path: str,
    output_path: os.PathLike[str] | str,
    *,
    overwrite: bool = False,
) -> Path:
    """Convenience wrapper for validated one-entry extraction."""

    return read_pck(archive_path).extract_entry(entry_path, output_path, overwrite=overwrite)


def build_pck(
    output_path: os.PathLike[str] | str,
    entries: Mapping[str, bytes] | Iterable[PckBuildEntry | tuple[str, bytes]],
    *,
    engine_version: tuple[int, int, int] = (4, 6, 3),
    alignment: int = 16,
    reserved: bytes = bytes(64),
    overwrite: bool = False,
) -> PckArchive:
    """Build a deterministic standalone PCK v3 archive.

    Input order is preserved as both physical payload order and directory
    order. A mapping therefore relies on Python's defined insertion order.
    """

    build_entries = _coerce_build_entries(entries)
    return _build_archive(
        output_path,
        physical_entries=build_entries,
        directory_paths=[entry.path for entry in build_entries],
        engine_version=engine_version,
        alignment=alignment,
        reserved=reserved,
        overwrite=overwrite,
    )


def patch_pck(
    source_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    replacements: Mapping[str, bytes],
    *,
    alignment: int | None = None,
    overwrite: bool = False,
    verify_source: bool = True,
) -> PckArchive:
    """Rebuild a PCK with exact-path replacements, never in place.

    Original directory order is preserved. Payloads retain their original
    physical order, making an empty replacement set byte-identical for
    canonical Godot packs whose padding bytes are zero.
    """

    source = Path(source_path)
    output = Path(output_path)
    if _same_path(source, output):
        raise PckSafetyError("PCK source and output paths must differ")
    archive = validate_pck(source, verify_md5=verify_source)
    unknown_paths = set(replacements) - set(archive.paths)
    if unknown_paths:
        names = ", ".join(sorted(repr(path) for path in unknown_paths))
        raise PckSafetyError(f"replacement paths are not in the source PCK: {names}")
    for path, data in replacements.items():
        _validate_entry_path(path)
        if not isinstance(data, bytes):
            raise TypeError(f"replacement data for {path!r} must be bytes")
        if archive.get_entry(path).is_removal:
            raise PckSafetyError(f"cannot replace a removal entry: {path!r}")

    physical_source_entries = sorted(
        archive.entries,
        key=lambda entry: (entry.relative_offset, entry.directory_index),
    )
    physical_entries = [
        PckBuildEntry(
            path=entry.path,
            data=(
                replacements[entry.path]
                if entry.path in replacements
                else archive.read_entry(entry, verify_md5=verify_source)
            ),
            flags=entry.flags,
        )
        for entry in physical_source_entries
    ]
    chosen_alignment = archive.inferred_alignment if alignment is None else alignment
    return _build_archive(
        output,
        physical_entries=physical_entries,
        directory_paths=list(archive.paths),
        engine_version=archive.engine_version,
        alignment=chosen_alignment,
        reserved=archive.reserved,
        overwrite=overwrite,
    )


def _build_archive(
    output_path: os.PathLike[str] | str,
    *,
    physical_entries: Sequence[PckBuildEntry],
    directory_paths: Sequence[str],
    engine_version: tuple[int, int, int],
    alignment: int,
    reserved: bytes,
    overwrite: bool,
) -> PckArchive:
    output = _prepare_output_path(output_path, overwrite=overwrite)
    alignment = _check_alignment(alignment)
    engine_version = _check_engine_version(engine_version)
    if not isinstance(reserved, bytes) or len(reserved) != 64:
        raise PckFormatError("reserved header must be exactly 64 bytes")

    by_path: dict[str, PckBuildEntry] = {}
    for entry in physical_entries:
        _validate_entry_path(entry.path)
        if entry.path in by_path:
            raise PckFormatError(f"duplicate build path: {entry.path!r}")
        if not isinstance(entry.data, bytes):
            raise TypeError(f"entry data for {entry.path!r} must be bytes")
        if entry.flags & ~PACK_FILE_REMOVAL:
            raise PckFormatError(f"unsupported build flags for {entry.path!r}: 0x{entry.flags:x}")
        if entry.flags & PACK_FILE_REMOVAL and entry.data:
            raise PckFormatError(f"removal entry must have empty data: {entry.path!r}")
        by_path[entry.path] = entry
    if len(directory_paths) != len(by_path) or set(directory_paths) != set(by_path):
        raise PckFormatError("directory order must contain every build path exactly once")

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as staged:
            temp_path = Path(staged.name)
            staged.write(
                _HEADER.pack(
                    PCK_MAGIC,
                    PCK_FORMAT_VERSION,
                    *engine_version,
                    PACK_REL_FILEBASE,
                    0,
                    0,
                )
            )
            staged.write(reserved)
            _write_padding(staged, alignment)
            file_base = staged.tell()

            directory_metadata: dict[str, tuple[int, int, bytes, int]] = {}
            for entry in physical_entries:
                relative_offset = staged.tell() - file_base
                if not entry.flags & PACK_FILE_REMOVAL:
                    staged.write(entry.data)
                    stored_md5 = hashlib.md5(entry.data).digest()
                else:
                    stored_md5 = bytes(16)
                directory_metadata[entry.path] = (
                    relative_offset,
                    len(entry.data),
                    stored_md5,
                    entry.flags,
                )
                _write_padding(staged, alignment)

            _write_padding(staged, alignment)
            directory_offset = staged.tell()
            staged.seek(24)
            staged.write(struct.pack("<QQ", file_base, directory_offset))
            staged.seek(directory_offset)
            staged.write(_U32.pack(len(directory_paths)))
            for path in directory_paths:
                path_bytes = path.encode("utf-8")
                path_padding = (-len(path_bytes)) % 4
                padded_path = path_bytes + bytes(path_padding)
                staged.write(_U32.pack(len(padded_path)))
                staged.write(padded_path)
                relative_offset, size, stored_md5, entry_flags = directory_metadata[path]
                staged.write(_U64_PAIR.pack(relative_offset, size))
                staged.write(stored_md5)
                staged.write(_U32.pack(entry_flags))
            staged.flush()
            os.fsync(staged.fileno())

        validate_pck(temp_path, verify_md5=True, alignment=alignment)
        if output.exists() and not overwrite:
            raise PckSafetyError(f"output already exists: {output}")
        os.replace(temp_path, output)
        temp_path = None
        return validate_pck(output, verify_md5=True, alignment=alignment)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _coerce_build_entries(
    entries: Mapping[str, bytes] | Iterable[PckBuildEntry | tuple[str, bytes]],
) -> list[PckBuildEntry]:
    source: Iterable[PckBuildEntry | tuple[str, bytes]]
    source = entries.items() if isinstance(entries, Mapping) else entries
    result: list[PckBuildEntry] = []
    for item in source:
        if isinstance(item, PckBuildEntry):
            result.append(item)
            continue
        try:
            path, data = item
        except (TypeError, ValueError) as exc:
            raise TypeError("entries must contain PckBuildEntry or (path, bytes) pairs") from exc
        result.append(PckBuildEntry(path=path, data=data))
    return result


def _validate_entry_path(path: str) -> None:
    if not isinstance(path, str) or not path:
        raise PckFormatError("PCK entry path must be a non-empty string")
    if "\0" in path:
        raise PckFormatError("PCK entry path contains NUL")
    if "\\" in path:
        raise PckFormatError(f"PCK entry path uses backslashes: {path!r}")
    if path.startswith("/") or path.startswith("res://"):
        raise PckFormatError(f"PCK entry path must be archive-relative: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PckFormatError(f"PCK entry path is not canonical: {path!r}")
    encoded = path.encode("utf-8")
    if len(encoded) > _MAX_PATH_BYTES:
        raise PckFormatError(f"PCK entry path is too long: {path!r}")


def _validate_non_overlapping_ranges(entries: Sequence[PckEntry]) -> None:
    ranges = sorted(
        (entry.offset, entry.offset + entry.size, entry.path)
        for entry in entries
        if entry.size
    )
    previous_end = -1
    previous_path = ""
    for start, end, path in ranges:
        if start < previous_end:
            raise PckFormatError(
                f"overlapping payload ranges: {previous_path!r} and {path!r}"
            )
        previous_end = end
        previous_path = path


def _read_exact(source, size: int, description: str) -> bytes:
    data = source.read(size)
    if len(data) != size:
        raise PckFormatError(f"truncated {description}")
    return data


def _write_padding(output, alignment: int) -> None:
    padding = (-output.tell()) % alignment
    if padding:
        output.write(bytes(padding))


def _check_alignment(alignment: int) -> int:
    if isinstance(alignment, bool) or not isinstance(alignment, int):
        raise PckFormatError("alignment must be an integer")
    if alignment <= 0 or alignment > _MAX_ALIGNMENT:
        raise PckFormatError(f"alignment must be between 1 and {_MAX_ALIGNMENT}")
    return alignment


def _check_engine_version(version: tuple[int, int, int]) -> tuple[int, int, int]:
    if not isinstance(version, tuple) or len(version) != 3:
        raise PckFormatError("engine version must be a three-integer tuple")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in version):
        raise PckFormatError("engine version must be a three-integer tuple")
    if any(value < 0 or value > 0xFFFFFFFF for value in version):
        raise PckFormatError("engine version component is outside uint32 range")
    return version


def _prepare_output_path(
    output_path: os.PathLike[str] | str,
    *,
    overwrite: bool,
) -> Path:
    output = Path(output_path)
    if not output.name:
        raise PckSafetyError("an explicit output file path is required")
    if not output.parent.exists() or not output.parent.is_dir():
        raise PckSafetyError(f"output parent directory does not exist: {output.parent}")
    if output.exists():
        if output.is_dir():
            raise PckSafetyError(f"output path is a directory: {output}")
        if not overwrite:
            raise PckSafetyError(f"output already exists: {output}")
    return output


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return left.resolve(strict=False) == right.resolve(strict=False)


def _atomic_write_bytes(output: Path, data: bytes, *, overwrite: bool) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as staged:
            temp_path = Path(staged.name)
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
        if output.exists() and not overwrite:
            raise PckSafetyError(f"output already exists: {output}")
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "PACK_DIR_ENCRYPTED",
    "PACK_FILE_DELTA",
    "PACK_FILE_ENCRYPTED",
    "PACK_FILE_REMOVAL",
    "PACK_REL_FILEBASE",
    "PACK_SPARSE_BUNDLE",
    "PCK_FORMAT_VERSION",
    "PCK_MAGIC",
    "PckArchive",
    "PckBuildEntry",
    "PckEntry",
    "PckError",
    "PckFormatError",
    "PckIntegrityError",
    "PckSafetyError",
    "PckValidation",
    "build_pck",
    "extract_entry",
    "patch_pck",
    "read_pck",
    "validate_pck",
]
