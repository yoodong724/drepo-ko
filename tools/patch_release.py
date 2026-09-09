#!/usr/bin/env python3
"""Build, audit, install, and remove an exact-build Korean PCK delta.

The release payload contains a deterministic, base-dependent delta rather
than a replacement PCK.  Installation is deliberately strict: both the game
executable and PCK must match the pinned Windows build, and every generated
file is hash checked before the original PCK is atomically replaced.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence, Union
import zlib


TARGET_BUILD_ID = "DGR-WIN-1.0.5-b3e7048e"
TARGET_EXE_SHA256 = "1de3edb8e10c66412a5e0d76ca44a2b70ad791d8b08f17135aae36b9bc369bf5"
TARGET_PCK_SHA256 = "b3e7048e62421e74b8b485f242fbec3f14cde94d452309f6d57d957623821399"
RELEASE_SCHEMA_VERSION = 2
DELTA_VERSION = 1
DELTA_MAGIC = b"DREPO-KO-DELTA01"
DEFAULT_BLOCK_SIZE = 64 * 1024
MIN_BLOCK_SIZE = 4 * 1024
MIN_COPY_RATIO = 0.10
MAX_DELTA_OUTPUT_SIZE = 256 * 1024 * 1024
MAX_DELTA_OPERATIONS = 10_000
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_DELTA_FILE_SIZE = 64 * 1024 * 1024
MANIFEST_NAME = "manifest.json"
DELTA_NAME = "drepo.pck.delta"
INSTALLER_PS1_NAME = "patch_release.ps1"
INSTALL_CMD_NAME = "install.cmd"
UNINSTALL_CMD_NAME = "uninstall.cmd"
INSTALLER_NAMES = (INSTALLER_PS1_NAME, INSTALL_CMD_NAME, UNINSTALL_CMD_NAME)
INSTALLER_SOURCE_DIR = Path(__file__).resolve().parent / "installer"
# Pinned so a packaged installer can never drift from the reviewed source.
# tests/test_patch_release.py fails if these stop matching tools/installer/.
INSTALLER_SHA256 = {
    INSTALLER_PS1_NAME: "fe5838bb2fa1d196f048b3e903485a4bd5f85975cc943715af417a48733b9d54",
    INSTALL_CMD_NAME: "95c3d2a846bfe992f7a9be3072d28bc4231389d6e137338e554e39dd6fa5454d",
    UNINSTALL_CMD_NAME: "de5566f5bd5f58344ea425fc09e505d4351e98e8d111604da5af31304e7e9705",
}
INSTALL_DOC_NAME = "INSTALL_KO.md"
FONT_LICENSE_NAME = "FONT_LICENSE_OFL-1.1.txt"
FONT_LICENSE_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "assets/fonts/third_party/galmuri/LICENSE.txt"
)
FONT_LICENSE_SHA256 = "86a3ee9495f942f0243f18c103da9faca27adb88142613edb8bb852e56c892c1"
BACKUP_DIR_NAME = ".drepo-ko-backup"
BACKUP_PCK_NAME = "drepo.pck.original"
STATE_NAME = "install-state.json"
GAME_EXE_NAME = "drepo.exe"
GAME_PCK_NAME = "drepo.pck"

INSTALL_DOC = f"""# 데스게임 보고서 한국어 패치 설치 안내

이 패치는 `{TARGET_BUILD_ID}` 전용이며 원본 게임 파일을 포함하지 않습니다.
설치기는 `drepo.exe`와 `drepo.pck`의 SHA-256을 정확히 확인한 뒤에만 동작합니다.

## 요구 사항

- Windows판 원본 게임: `drepo.exe`, `drepo.pck`
- Windows에 기본 포함된 Windows PowerShell 5.1 이상 (별도 설치 없음)
- 설치·제거 전에 게임을 완전히 종료

Python을 비롯한 어떤 프로그램도 따로 설치하지 않습니다.

## 설치

1. 게임을 완전히 종료합니다.
2. `{GAME_EXE_NAME}`와 `{GAME_PCK_NAME}`가 있는 게임 설치 폴더에 압축을 풉니다.
   패치 파일들은 하나의 하위 폴더 안에 있어야 합니다. 폴더 이름은 바꿔도 됩니다.
3. 그 하위 패치 폴더의 `{INSTALL_CMD_NAME}`를 더블클릭합니다.
   설치기는 패치 폴더 바로 위의 게임에 적용합니다. Steam 자동 탐색이나 경로 입력은 없습니다.

```text
drepo/
  drepo.exe
  drepo.pck
  death-game-report-ko-v1.0.5/
    install.cmd
    uninstall.cmd
    patch_release.ps1
    (나머지 패치 파일)
```

패치 파일을 `drepo.exe` 옆에 낱개로 풀거나 패치 폴더를 두 겹으로 중첩하지 마세요.
제거할 때도 같은 위치에 패치 폴더를 두고 `{UNINSTALL_CMD_NAME}`를 실행합니다.

게임 폴더를 직접 지정하려면 PowerShell에서 다음과 같이 실행합니다.

```text
powershell -NoProfile -ExecutionPolicy Bypass -File {INSTALLER_PS1_NAME} -Command install -GameDir "게임 폴더 경로"
```

원본 `{GAME_PCK_NAME}`는 게임 폴더의 `{BACKUP_DIR_NAME}` 아래에 해시 검증된 상태로 보관됩니다.
호환 빌드가 아니거나 기존 backup state가 있으면 설치기는 아무 파일도 교체하지 않습니다.

## 제거

이 폴더의 `{UNINSTALL_CMD_NAME}`를 더블클릭하거나 다음을 실행합니다.

```text
powershell -NoProfile -ExecutionPolicy Bypass -File {INSTALLER_PS1_NAME} -Command uninstall -GameDir "게임 폴더 경로"
```

제거기는 설치 후 `{GAME_PCK_NAME}`가 변경되지 않았는지 확인하고 원본을 복원합니다.
사용자가 설치 후 PCK를 수정했다면 안전을 위해 자동 복원을 거부합니다.

## 호환 빌드 해시

- `{GAME_EXE_NAME}`: `{TARGET_EXE_SHA256}`
- `{GAME_PCK_NAME}`: `{TARGET_PCK_SHA256}`

## 실행이 막히는 경우

`{INSTALL_CMD_NAME}`는 이 창에서만 실행 정책을 우회하며 시스템 설정을 바꾸지 않습니다.
조직에서 관리하는 PC 등 PowerShell 실행 자체가 제한된 환경이라면 설치기가 시작되지 않을 수 있습니다.
이때는 배포 저장소의 `tools/patch_release.py`(Python 3.8 이상)로 동일한 설치를 수행할 수 있습니다.

## 현재 확인 상태

자동 데이터·통합·패치 감사와 대표 화면 기술 검증은 완료됐습니다.
전체 플레이와 최종 가독성 승인은 사람 검수 전이므로 `UNCHECKED_PROVISIONAL`입니다.
이미지 안에 그려진 원문 텍스트는 1차 한글화 범위에 포함되지 않습니다.
포함된 Galmuri 폰트의 SIL Open Font License 1.1 전문은 `{FONT_LICENSE_NAME}`에서 확인할 수 있습니다.
""".encode("utf-8")

_HEADER = struct.Struct("<16sIIQQ")
_COPY = struct.Struct("<BQQ")
_ADD = struct.Struct("<BQQ")
_COPY_OPCODE = 1
_ADD_OPCODE = 2


class PatchReleaseError(ValueError):
    """The package, target build, delta, or rollback state is unsafe."""


@dataclass(frozen=True)
class CopyOp:
    offset: int
    length: int


@dataclass(frozen=True)
class AddOp:
    data: bytes


DeltaOp = Union[CopyOp, AddOp]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path | str, label: str) -> Path:
    item = Path(path).absolute()
    if item.is_symlink() or not item.is_file():
        raise PatchReleaseError(f"{label} must be a regular non-symlink file: {item}")
    return item.resolve()


def _real_directory(path: Path | str, label: str) -> Path:
    item = Path(path).absolute()
    if item.is_symlink() or not item.is_dir():
        raise PatchReleaseError(f"{label} must be a real directory: {item}")
    return item.resolve()


def _require_hash(path: Path, expected: str, label: str) -> str:
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise PatchReleaseError(f"{label} has a malformed SHA-256 pin")
    actual = sha256_file(path)
    if actual != expected:
        raise PatchReleaseError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_json(path: Path, label: str, *, max_size: int = MAX_MANIFEST_SIZE) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PatchReleaseError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        size = path.stat().st_size
        if size > max_size:
            raise PatchReleaseError(f"{label} is too large: {size} bytes")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchReleaseError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise PatchReleaseError(f"{label} must be a JSON object")
    return value


def _weak_checksum(data: bytes, start: int, length: int) -> tuple[int, int]:
    modulus = 65521
    window = data[start:start + length]
    a = sum(window) % modulus
    b = sum((length - index) * byte for index, byte in enumerate(window)) % modulus
    return a, b


def _roll_checksum(
    checksum: tuple[int, int], old: int, new: int, length: int,
) -> tuple[int, int]:
    modulus = 65521
    a = (checksum[0] - old + new) % modulus
    b = (checksum[1] - length * old + a) % modulus
    return a, b


def _common_forward(base: bytes, target: bytes, base_at: int, target_at: int) -> int:
    maximum = min(len(base) - base_at, len(target) - target_at)
    matched = 0
    step = 64 * 1024
    while matched + step <= maximum:
        if base[base_at + matched:base_at + matched + step] != target[
            target_at + matched:target_at + matched + step
        ]:
            break
        matched += step
    while matched < maximum and base[base_at + matched] == target[target_at + matched]:
        matched += 1
    return matched


def build_delta_ops(
    base: bytes, target: bytes, *, block_size: int = DEFAULT_BLOCK_SIZE,
) -> list[DeltaOp]:
    """Return deterministic COPY/ADD operations using base-aligned anchors."""

    if block_size < MIN_BLOCK_SIZE:
        raise PatchReleaseError(f"delta block size must be at least {MIN_BLOCK_SIZE}")
    if not base or not target:
        raise PatchReleaseError("base and localized PCK must both be non-empty")
    if base == target:
        raise PatchReleaseError("localized PCK is byte-identical to the original")

    anchors: dict[tuple[int, int], list[int]] = {}
    for offset in range(0, len(base) - block_size + 1, block_size):
        anchors.setdefault(_weak_checksum(base, offset, block_size), []).append(offset)

    operations: list[DeltaOp] = []
    position = 0
    add_start = 0
    checksum = (
        _weak_checksum(target, 0, block_size)
        if len(target) >= block_size else None
    )
    while position + block_size <= len(target) and checksum is not None:
        best_offset = -1
        best_length = 0
        for offset in anchors.get(checksum, ()):
            if base[offset:offset + block_size] != target[position:position + block_size]:
                continue
            length = _common_forward(base, target, offset, position)
            if length > best_length or (length == best_length and offset < best_offset):
                best_offset = offset
                best_length = length
        if best_length:
            if add_start < position:
                operations.append(AddOp(target[add_start:position]))
            operations.append(CopyOp(best_offset, best_length))
            position += best_length
            add_start = position
            checksum = (
                _weak_checksum(target, position, block_size)
                if position + block_size <= len(target) else None
            )
            continue
        old = target[position]
        position += 1
        if position + block_size <= len(target):
            checksum = _roll_checksum(
                checksum, old, target[position + block_size - 1], block_size,
            )
        else:
            checksum = None
    if add_start < len(target):
        operations.append(AddOp(target[add_start:]))
    return _coalesce_ops(operations)


def _coalesce_ops(operations: Iterable[DeltaOp]) -> list[DeltaOp]:
    result: list[DeltaOp] = []
    for operation in operations:
        if isinstance(operation, AddOp) and not operation.data:
            continue
        if isinstance(operation, CopyOp) and operation.length <= 0:
            raise PatchReleaseError("delta contains an empty COPY operation")
        if result and isinstance(result[-1], AddOp) and isinstance(operation, AddOp):
            result[-1] = AddOp(result[-1].data + operation.data)
        elif (
            result and isinstance(result[-1], CopyOp) and isinstance(operation, CopyOp)
            and result[-1].offset + result[-1].length == operation.offset
        ):
            result[-1] = CopyOp(result[-1].offset, result[-1].length + operation.length)
        else:
            result.append(operation)
    if not result:
        raise PatchReleaseError("delta contains no operations")
    return result


def encode_delta(
    operations: Sequence[DeltaOp], *, target_size: int, block_size: int,
) -> bytes:
    output = bytearray(_HEADER.pack(
        DELTA_MAGIC, DELTA_VERSION, block_size, target_size, len(operations),
    ))
    for operation in operations:
        if isinstance(operation, CopyOp):
            output.extend(_COPY.pack(_COPY_OPCODE, operation.offset, operation.length))
        else:
            compressed = zlib.compress(operation.data, level=9)
            output.extend(_ADD.pack(_ADD_OPCODE, len(operation.data), len(compressed)))
            output.extend(compressed)
    return bytes(output)


def decode_delta(data: bytes, *, base_size: int) -> tuple[list[DeltaOp], dict[str, int]]:
    if len(data) < _HEADER.size:
        raise PatchReleaseError("delta is truncated before its header")
    magic, version, block_size, target_size, operation_count = _HEADER.unpack_from(data)
    if magic != DELTA_MAGIC or version != DELTA_VERSION:
        raise PatchReleaseError("delta magic/version mismatch")
    if block_size < MIN_BLOCK_SIZE:
        raise PatchReleaseError("delta block size is unsafe")
    if target_size <= 0 or target_size > MAX_DELTA_OUTPUT_SIZE or operation_count <= 0:
        raise PatchReleaseError("delta declares an empty output or operation list")
    if operation_count > MAX_DELTA_OPERATIONS or operation_count > target_size + 1:
        raise PatchReleaseError("delta operation count is implausible")
    cursor = _HEADER.size
    operations: list[DeltaOp] = []
    emitted = 0
    copy_bytes = 0
    add_bytes = 0
    for _ in range(operation_count):
        if cursor >= len(data):
            raise PatchReleaseError("delta is truncated in its operation table")
        opcode = data[cursor]
        if opcode == _COPY_OPCODE:
            if cursor + _COPY.size > len(data):
                raise PatchReleaseError("delta COPY operation is truncated")
            _, offset, length = _COPY.unpack_from(data, cursor)
            cursor += _COPY.size
            if length <= 0 or offset > base_size or length > base_size - offset:
                raise PatchReleaseError("delta COPY operation is outside the original PCK")
            operations.append(CopyOp(offset, length))
            copy_bytes += length
            emitted += length
        elif opcode == _ADD_OPCODE:
            if cursor + _ADD.size > len(data):
                raise PatchReleaseError("delta ADD operation is truncated")
            _, raw_length, stored_length = _ADD.unpack_from(data, cursor)
            cursor += _ADD.size
            if raw_length <= 0 or stored_length <= 0 or emitted + raw_length > target_size:
                raise PatchReleaseError("delta ADD operation has unsafe lengths")
            if cursor + stored_length > len(data):
                raise PatchReleaseError("delta ADD payload is truncated")
            compressed = data[cursor:cursor + stored_length]
            cursor += stored_length
            decompressor = zlib.decompressobj()
            try:
                raw = decompressor.decompress(compressed, raw_length + 1)
                if len(raw) > raw_length or decompressor.unconsumed_tail:
                    raise PatchReleaseError("delta ADD payload expands beyond its declared size")
                raw += decompressor.flush(raw_length + 1 - len(raw))
            except zlib.error as exc:
                raise PatchReleaseError(f"delta ADD payload is corrupt: {exc}") from exc
            if (
                len(raw) != raw_length or len(raw) > raw_length
                or not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail
            ):
                raise PatchReleaseError("delta ADD payload size/stream boundary mismatch")
            operations.append(AddOp(raw))
            add_bytes += raw_length
            emitted += raw_length
        else:
            raise PatchReleaseError(f"delta contains unknown opcode {opcode}")
        if emitted > target_size:
            raise PatchReleaseError("delta emits more bytes than declared")
    if cursor != len(data):
        raise PatchReleaseError("delta contains trailing bytes")
    if emitted != target_size:
        raise PatchReleaseError(
            f"delta output size mismatch: declared {target_size}, operations emit {emitted}"
        )
    return operations, {
        "block_size": block_size,
        "target_size": target_size,
        "operation_count": operation_count,
        "copy_bytes": copy_bytes,
        "add_bytes": add_bytes,
    }


def apply_delta_bytes(base: bytes, delta: bytes) -> bytes:
    operations, _ = decode_delta(delta, base_size=len(base))
    output = bytearray()
    for operation in operations:
        if isinstance(operation, CopyOp):
            output.extend(base[operation.offset:operation.offset + operation.length])
        else:
            output.extend(operation.data)
    return bytes(output)


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    if path.exists() or path.is_symlink():
        raise PatchReleaseError(f"refusing to overwrite existing path: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise PatchReleaseError(f"output parent must be a real existing directory: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.exists() or path.is_symlink():
            raise PatchReleaseError(f"refusing to overwrite existing path: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_delta_file(
    base_path: Path | str,
    delta_path: Path | str,
    output_path: Path | str,
    *,
    expected_base_sha256: str,
    expected_delta_sha256: str,
    expected_output_sha256: str,
) -> str:
    base_file = _regular_file(base_path, "delta base")
    delta_file = _regular_file(delta_path, "delta payload")
    output = Path(output_path).absolute()
    _require_hash(base_file, expected_base_sha256, "delta base")
    _require_hash(delta_file, expected_delta_sha256, "delta payload")
    if output.exists() or output.is_symlink():
        raise PatchReleaseError(f"refusing to overwrite existing output: {output}")
    base = base_file.read_bytes()
    localized = apply_delta_bytes(base, delta_file.read_bytes())
    if sha256_bytes(localized) != expected_output_sha256:
        raise PatchReleaseError("delta output SHA-256 mismatch")
    mode = stat.S_IMODE(base_file.stat().st_mode)
    _atomic_write(output, localized, mode=mode)
    return expected_output_sha256


def _validate_integration_manifest(
    manifest_path: Path,
    *,
    expected_exe_sha256: str,
    expected_pck_sha256: str,
    localized_sha256: str,
) -> dict[str, Any]:
    manifest = _load_json(manifest_path, "integration manifest")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("tool") != "tools/integrate.py"
        or manifest.get("build_id") != TARGET_BUILD_ID
        or manifest.get("mode") != "production"
    ):
        raise PatchReleaseError("integration manifest is not the exact production target build")
    inputs = manifest.get("inputs")
    hashes = inputs.get("sha256") if isinstance(inputs, dict) else None
    if not isinstance(hashes, dict):
        raise PatchReleaseError("integration manifest omits input hash pins")
    if hashes.get("executable") != expected_exe_sha256 or hashes.get("pck") != expected_pck_sha256:
        raise PatchReleaseError("integration manifest source build hashes do not match")
    for pin in ("segments", "source_manifest", "assignments"):
        value = hashes.get(pin)
        if (
            not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise PatchReleaseError(f"integration manifest omits canonical {pin} revision pin")
    output = manifest.get("output")
    if not isinstance(output, dict) or output.get("pck_sha256") != localized_sha256:
        raise PatchReleaseError("integration manifest localized PCK hash does not match")
    counts = manifest.get("merge_counts")
    statuses = counts.get("integrated_by_status") if isinstance(counts, dict) else None
    if not isinstance(statuses, dict) or not statuses:
        raise PatchReleaseError("integration manifest omits integrated approval counts")
    if not set(statuses).issubset({"APPROVED", "INTEGRATED", "RUNTIME_VALIDATED"}):
        raise PatchReleaseError("integration manifest contains a non-approved segment status")
    if any(not isinstance(count, int) or count <= 0 for count in statuses.values()):
        raise PatchReleaseError("integration manifest has invalid approval counts")
    if counts.get("integrated_rows") != sum(statuses.values()):
        raise PatchReleaseError("integration manifest merge counts are inconsistent")
    segment_ids = manifest.get("integrated_segment_ids")
    if (
        not isinstance(segment_ids, list)
        or len(segment_ids) != counts["integrated_rows"]
        or len(set(segment_ids)) != len(segment_ids)
        or any(not isinstance(item, str) or not item.startswith("SEG-") for item in segment_ids)
    ):
        raise PatchReleaseError("integration manifest integrated segment IDs are inconsistent")
    return manifest


def create_package(
    *,
    source_exe: Path | str,
    source_pck: Path | str,
    localized_pck: Path | str,
    integration_manifest: Path | str,
    output_dir: Path | str,
    block_size: int = DEFAULT_BLOCK_SIZE,
    _expected_exe_sha256: str = TARGET_EXE_SHA256,
    _expected_pck_sha256: str = TARGET_PCK_SHA256,
    _font_license_source: Path | str = FONT_LICENSE_SOURCE,
    _installer_source_dir: Path | str = INSTALLER_SOURCE_DIR,
) -> dict[str, Any]:
    exe = _regular_file(source_exe, "source executable")
    source = _regular_file(source_pck, "source PCK")
    localized = _regular_file(localized_pck, "localized PCK")
    integration = _regular_file(integration_manifest, "integration manifest")
    font_license = _regular_file(_font_license_source, "Galmuri font license")
    installer_sources = {
        name: _regular_file(Path(_installer_source_dir) / name, f"installer source {name}")
        for name in INSTALLER_NAMES
    }
    _require_hash(exe, _expected_exe_sha256, "source executable")
    _require_hash(source, _expected_pck_sha256, "source PCK")
    _require_hash(font_license, FONT_LICENSE_SHA256, "Galmuri font license")
    for name, path in installer_sources.items():
        _require_hash(path, INSTALLER_SHA256[name], f"installer source {name}")
    localized_hash = sha256_file(localized)
    integration_data = _validate_integration_manifest(
        integration,
        expected_exe_sha256=_expected_exe_sha256,
        expected_pck_sha256=_expected_pck_sha256,
        localized_sha256=localized_hash,
    )
    declared_output = integration_data["output"].get("pck_path")
    if (
        not isinstance(declared_output, str)
        or Path(declared_output).name != declared_output
        or (integration.parent / declared_output).resolve() != localized
    ):
        raise PatchReleaseError("localized PCK is not the integration manifest's paired output")
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise PatchReleaseError(f"package output must be a fresh path: {output}")
    resolved_output = output.resolve(strict=False)
    for item in (exe, source, localized, integration, font_license):
        try:
            resolved_output.relative_to(item.parent)
        except ValueError:
            pass
        else:
            raise PatchReleaseError(f"package output is inside an input directory: {item.parent}")
        try:
            item.relative_to(resolved_output)
        except ValueError:
            pass
        else:
            raise PatchReleaseError(f"package output would contain an input: {item}")

    base_bytes = source.read_bytes()
    target_bytes = localized.read_bytes()
    if sha256_bytes(base_bytes) != _expected_pck_sha256:
        raise PatchReleaseError("source PCK changed while packaging")
    if sha256_bytes(target_bytes) != localized_hash:
        raise PatchReleaseError("localized PCK changed while packaging")
    operations = build_delta_ops(base_bytes, target_bytes, block_size=block_size)
    delta = encode_delta(operations, target_size=len(target_bytes), block_size=block_size)
    parsed, metrics = decode_delta(delta, base_size=len(base_bytes))
    if apply_delta_bytes(base_bytes, delta) != target_bytes:
        raise PatchReleaseError("generated delta failed its internal round trip")
    if metrics["copy_bytes"] / metrics["target_size"] < MIN_COPY_RATIO:
        raise PatchReleaseError("delta does not depend sufficiently on the original PCK")

    installer_blobs = {name: path.read_bytes() for name, path in installer_sources.items()}
    for name, blob in installer_blobs.items():
        if sha256_bytes(blob) != INSTALLER_SHA256[name]:
            raise PatchReleaseError(f"installer source {name} changed while packaging")
    font_license_bytes = font_license.read_bytes()
    if sha256_bytes(font_license_bytes) != FONT_LICENSE_SHA256:
        raise PatchReleaseError("Galmuri font license changed while packaging")
    manifest: dict[str, Any] = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "tool": "tools/patch_release.py",
        "build_id": TARGET_BUILD_ID,
        "language": "ko-KR",
        "game_files": {"executable": GAME_EXE_NAME, "pck": GAME_PCK_NAME},
        "source": {
            "executable_sha256": _expected_exe_sha256,
            "pck_sha256": _expected_pck_sha256,
            "pck_size": len(base_bytes),
        },
        "localized": {"pck_sha256": localized_hash, "pck_size": len(target_bytes)},
        "delta": {
            "file": DELTA_NAME,
            "sha256": sha256_bytes(delta),
            "size": len(delta),
            "format": "drepo-copy-add-zlib-v1",
            **metrics,
        },
        "base_dependency": {
            "copy_ratio": metrics["copy_bytes"] / metrics["target_size"],
            "standalone_output_possible": False,
        },
        "integration": {
            "manifest_sha256": sha256_file(integration),
            "canonical_sha256": {
                pin: integration_data["inputs"]["sha256"][pin]
                for pin in ("segments", "source_manifest", "assignments")
            },
            "integrated_rows": integration_data["merge_counts"]["integrated_rows"],
            "integrated_by_status": integration_data["merge_counts"]["integrated_by_status"],
        },
        "installer": {"files": dict(INSTALLER_SHA256)},
        "documentation": {
            "file": INSTALL_DOC_NAME,
            "sha256": sha256_bytes(INSTALL_DOC),
        },
        "font_license": {
            "file": FONT_LICENSE_NAME,
            "sha256": FONT_LICENSE_SHA256,
        },
        "install_paths": {
            "backup_directory": BACKUP_DIR_NAME,
            "backup_pck": f"{BACKUP_DIR_NAME}/{BACKUP_PCK_NAME}",
            "state": f"{BACKUP_DIR_NAME}/{STATE_NAME}",
            "install_temporary": f".{GAME_PCK_NAME}.ko-install.tmp",
            "restore_temporary": f".{GAME_PCK_NAME}.ko-restore.tmp",
            "swap_temporary": f".{GAME_PCK_NAME}.ko-swap.tmp",
        },
    }

    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
    try:
        (stage / DELTA_NAME).write_bytes(delta)
        for name, blob in installer_blobs.items():
            (stage / name).write_bytes(blob)
        (stage / INSTALL_DOC_NAME).write_bytes(INSTALL_DOC)
        (stage / FONT_LICENSE_NAME).write_bytes(font_license_bytes)
        (stage / MANIFEST_NAME).write_bytes(_json_bytes(manifest))
        _require_hash(exe, _expected_exe_sha256, "source executable")
        _require_hash(source, _expected_pck_sha256, "source PCK")
        _require_hash(localized, localized_hash, "localized PCK")
        _require_hash(integration, manifest["integration"]["manifest_sha256"], "integration manifest")
        _require_hash(font_license, FONT_LICENSE_SHA256, "Galmuri font license")
        for name, path in installer_sources.items():
            _require_hash(path, INSTALLER_SHA256[name], f"installer source {name}")
        audit = audit_package(
            package_dir=stage,
            source_exe=exe,
            source_pck=source,
            _expected_exe_sha256=_expected_exe_sha256,
            _expected_pck_sha256=_expected_pck_sha256,
        )
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    audit["package_dir"] = str(output)
    return audit


def _manifest_contract(
    manifest: Mapping[str, Any], *, expected_exe_sha256: str, expected_pck_sha256: str,
) -> None:
    if manifest.get("schema_version") != RELEASE_SCHEMA_VERSION:
        raise PatchReleaseError("release manifest schema version mismatch")
    if manifest.get("build_id") != TARGET_BUILD_ID or manifest.get("language") != "ko-KR":
        raise PatchReleaseError("release manifest target identity mismatch")
    if manifest.get("game_files") != {"executable": GAME_EXE_NAME, "pck": GAME_PCK_NAME}:
        raise PatchReleaseError("release manifest game paths are not the fixed safe names")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("executable_sha256") != expected_exe_sha256:
        raise PatchReleaseError("release manifest executable pin mismatch")
    if source.get("pck_sha256") != expected_pck_sha256:
        raise PatchReleaseError("release manifest PCK pin mismatch")
    delta = manifest.get("delta")
    installer = manifest.get("installer")
    documentation = manifest.get("documentation")
    font_license = manifest.get("font_license")
    if not isinstance(delta, dict) or delta.get("file") != DELTA_NAME:
        raise PatchReleaseError("release manifest delta path mismatch")
    if delta.get("format") != "drepo-copy-add-zlib-v1":
        raise PatchReleaseError("release manifest delta format mismatch")
    if not isinstance(installer, dict) or set(installer) != {"files"}:
        raise PatchReleaseError("release manifest installer section mismatch")
    if installer["files"] != dict(INSTALLER_SHA256):
        raise PatchReleaseError("release manifest installer file pins mismatch")
    if (
        not isinstance(documentation, dict)
        or documentation.get("file") != INSTALL_DOC_NAME
    ):
        raise PatchReleaseError("release manifest documentation path mismatch")
    if font_license != {
        "file": FONT_LICENSE_NAME,
        "sha256": FONT_LICENSE_SHA256,
    }:
        raise PatchReleaseError("release manifest font license pin mismatch")
    if manifest.get("install_paths") != {
        "backup_directory": BACKUP_DIR_NAME,
        "backup_pck": f"{BACKUP_DIR_NAME}/{BACKUP_PCK_NAME}",
        "state": f"{BACKUP_DIR_NAME}/{STATE_NAME}",
        "install_temporary": f".{GAME_PCK_NAME}.ko-install.tmp",
        "restore_temporary": f".{GAME_PCK_NAME}.ko-restore.tmp",
        "swap_temporary": f".{GAME_PCK_NAME}.ko-swap.tmp",
    }:
        raise PatchReleaseError("release manifest install paths are not the fixed safe paths")


def _assert_no_base_block_in_adds(base: bytes, operations: Sequence[DeltaOp], block_size: int) -> None:
    anchors: dict[tuple[int, int], list[int]] = {}
    for offset in range(0, len(base) - block_size + 1, block_size):
        anchors.setdefault(_weak_checksum(base, offset, block_size), []).append(offset)
    for operation in operations:
        if not isinstance(operation, AddOp) or len(operation.data) < block_size:
            continue
        checksum = _weak_checksum(operation.data, 0, block_size)
        position = 0
        while position + block_size <= len(operation.data):
            for offset in anchors.get(checksum, ()):
                if operation.data[position:position + block_size] == base[offset:offset + block_size]:
                    raise PatchReleaseError("delta ADD data embeds an unchanged original PCK block")
            old = operation.data[position]
            position += 1
            if position + block_size <= len(operation.data):
                checksum = _roll_checksum(
                    checksum, old, operation.data[position + block_size - 1], block_size,
                )


def audit_package(
    *,
    package_dir: Path | str,
    source_exe: Path | str,
    source_pck: Path | str,
    _expected_exe_sha256: str = TARGET_EXE_SHA256,
    _expected_pck_sha256: str = TARGET_PCK_SHA256,
) -> dict[str, Any]:
    package = _real_directory(package_dir, "package directory")
    exe = _regular_file(source_exe, "source executable")
    pck = _regular_file(source_pck, "source PCK")
    _require_hash(exe, _expected_exe_sha256, "source executable")
    _require_hash(pck, _expected_pck_sha256, "source PCK")
    actual_names: set[str] = set()
    for item in package.iterdir():
        if item.is_symlink() or not item.is_file():
            raise PatchReleaseError(f"package contains a non-regular entry: {item.name}")
        actual_names.add(item.name)
    expected_names = {
        MANIFEST_NAME,
        DELTA_NAME,
        INSTALL_DOC_NAME,
        FONT_LICENSE_NAME,
        *INSTALLER_NAMES,
    }
    if actual_names != expected_names:
        raise PatchReleaseError(
            f"package file set mismatch: expected {sorted(expected_names)}, got {sorted(actual_names)}"
        )
    manifest_path = package / MANIFEST_NAME
    delta_path = package / DELTA_NAME
    installer_paths = {name: package / name for name in INSTALLER_NAMES}
    documentation_path = package / INSTALL_DOC_NAME
    font_license_path = package / FONT_LICENSE_NAME
    manifest = _load_json(manifest_path, "release manifest")
    _manifest_contract(
        manifest,
        expected_exe_sha256=_expected_exe_sha256,
        expected_pck_sha256=_expected_pck_sha256,
    )
    delta_info = manifest["delta"]
    installer_info = manifest["installer"]
    documentation_info = manifest["documentation"]
    font_license_info = manifest["font_license"]
    delta_size = delta_path.stat().st_size
    if delta_size > MAX_DELTA_FILE_SIZE:
        raise PatchReleaseError(f"delta payload is too large: {delta_size} bytes")
    _require_hash(delta_path, delta_info.get("sha256", ""), "delta payload")
    for name, path in installer_paths.items():
        _require_hash(path, installer_info["files"].get(name, ""), f"installer {name}")
    _require_hash(
        documentation_path,
        documentation_info.get("sha256", ""),
        "installation documentation",
    )
    _require_hash(
        font_license_path,
        font_license_info.get("sha256", ""),
        "Galmuri font license",
    )
    if delta_size != delta_info.get("size"):
        raise PatchReleaseError("delta size does not match the release manifest")
    base = pck.read_bytes()
    if manifest["source"].get("pck_size") != len(base):
        raise PatchReleaseError("source PCK size does not match the release manifest")
    delta_bytes = delta_path.read_bytes()
    operations, metrics = decode_delta(delta_bytes, base_size=len(base))
    for field, value in metrics.items():
        if delta_info.get(field) != value:
            raise PatchReleaseError(f"delta metric {field} does not match its manifest")
    localized = apply_delta_bytes(base, delta_bytes)
    localized_info = manifest.get("localized")
    if not isinstance(localized_info, dict):
        raise PatchReleaseError("release manifest omits localized PCK identity")
    if len(localized) != localized_info.get("pck_size"):
        raise PatchReleaseError("localized PCK size does not match its manifest")
    if sha256_bytes(localized) != localized_info.get("pck_sha256"):
        raise PatchReleaseError("localized PCK hash does not match its manifest")
    ratio = metrics["copy_bytes"] / metrics["target_size"]
    dependency = manifest.get("base_dependency")
    if (
        ratio < MIN_COPY_RATIO or not isinstance(dependency, dict)
        or dependency.get("copy_ratio") != ratio
        or dependency.get("standalone_output_possible") is not False
    ):
        raise PatchReleaseError("release payload does not prove sufficient base dependence")
    _assert_no_base_block_in_adds(base, operations, metrics["block_size"])

    source_blobs = {"executable": exe.read_bytes(), "pck": base}
    package_files = [
        manifest_path,
        delta_path,
        documentation_path,
        font_license_path,
        *installer_paths.values(),
    ]
    package_hashes = {path.name: sha256_file(path) for path in package_files}
    if set(package_hashes.values()) & {_expected_exe_sha256, _expected_pck_sha256}:
        raise PatchReleaseError("a payload file is byte-identical to an original game file")
    for label, blob in source_blobs.items():
        if len(blob) >= MIN_BLOCK_SIZE:
            for item in package_files:
                if blob in item.read_bytes():
                    raise PatchReleaseError(f"package embeds the complete original {label}")
    return {
        "build_id": TARGET_BUILD_ID,
        "manifest_sha256": sha256_file(manifest_path),
        "payload_sha256": package_hashes,
        "localized_pck_sha256": localized_info["pck_sha256"],
        "delta_metrics": metrics,
        "audit": {
            "exact_source_hashes_verified": True,
            "original_files_absent": True,
            "standalone_reconstruction_rejected": True,
            "unexpected_paths": 0,
        },
    }


def _game_paths(game_dir: Path | str) -> tuple[Path, Path, Path, Path, Path]:
    game = _real_directory(game_dir, "game directory")
    exe = game / GAME_EXE_NAME
    pck = game / GAME_PCK_NAME
    backup_dir = game / BACKUP_DIR_NAME
    return exe, pck, backup_dir, backup_dir / BACKUP_PCK_NAME, backup_dir / STATE_NAME


def install_package(
    *,
    package_dir: Path | str,
    game_dir: Path | str,
    _expected_exe_sha256: str = TARGET_EXE_SHA256,
    _expected_pck_sha256: str = TARGET_PCK_SHA256,
) -> dict[str, Any]:
    exe, pck, backup_dir, backup_pck, state_path = _game_paths(game_dir)
    _regular_file(exe, "game executable")
    _regular_file(pck, "game PCK")
    if backup_dir.exists() or backup_dir.is_symlink():
        raise PatchReleaseError(f"install/backup state already exists: {backup_dir}")
    reserved = [
        pck.parent / f".{pck.name}.ko-install.tmp",
        pck.parent / f".{pck.name}.ko-restore.tmp",
        pck.parent / f".{pck.name}.ko-swap.tmp",
    ]
    for item in reserved:
        if item.exists() or item.is_symlink():
            raise PatchReleaseError(f"reserved temporary path already exists: {item}")
    audit = audit_package(
        package_dir=package_dir, source_exe=exe, source_pck=pck,
        _expected_exe_sha256=_expected_exe_sha256,
        _expected_pck_sha256=_expected_pck_sha256,
    )
    package = Path(package_dir).resolve()
    manifest = _load_json(package / MANIFEST_NAME, "release manifest")
    original_mode = stat.S_IMODE(pck.stat().st_mode)
    generated = reserved[0]
    generated_owned = False
    backup_dir.mkdir(mode=0o700)
    try:
        _require_hash(exe, _expected_exe_sha256, "game executable")
        _require_hash(pck, _expected_pck_sha256, "game PCK")
        _atomic_write(backup_pck, pck.read_bytes(), mode=original_mode)
        _require_hash(backup_pck, _expected_pck_sha256, "installed backup PCK")
        state = {
            "schema_version": 1,
            "build_id": TARGET_BUILD_ID,
            "manifest_sha256": audit["manifest_sha256"],
            "source_pck_sha256": _expected_pck_sha256,
            "localized_pck_sha256": manifest["localized"]["pck_sha256"],
            "game_pck": GAME_PCK_NAME,
            "backup_pck": BACKUP_PCK_NAME,
        }
        _atomic_write(state_path, _json_bytes(state), mode=0o600)
        apply_delta_file(
            backup_pck,
            package / DELTA_NAME,
            generated,
            expected_base_sha256=_expected_pck_sha256,
            expected_delta_sha256=manifest["delta"]["sha256"],
            expected_output_sha256=manifest["localized"]["pck_sha256"],
        )
        generated_owned = True
        os.replace(generated, pck)
        generated_owned = False
        _require_hash(pck, manifest["localized"]["pck_sha256"], "installed localized PCK")
    except BaseException:
        if generated_owned:
            generated.unlink(missing_ok=True)
        # Keep a verified backup/state if publication may have begun.  If the
        # original is still in place, clean only the paths this attempt made.
        if pck.is_file() and sha256_file(pck) == _expected_pck_sha256:
            state_path.unlink(missing_ok=True)
            backup_pck.unlink(missing_ok=True)
            try:
                backup_dir.rmdir()
            except OSError:
                pass
        raise
    return {
        "result": "INSTALLED",
        "modified_paths": [str(pck), str(backup_pck), str(state_path)],
        "source_pck_sha256": _expected_pck_sha256,
        "installed_pck_sha256": manifest["localized"]["pck_sha256"],
        "manifest_sha256": audit["manifest_sha256"],
    }


def uninstall_package(
    *,
    package_dir: Path | str,
    game_dir: Path | str,
    _expected_exe_sha256: str = TARGET_EXE_SHA256,
    _expected_pck_sha256: str = TARGET_PCK_SHA256,
) -> dict[str, Any]:
    exe, pck, backup_dir, backup_pck, state_path = _game_paths(game_dir)
    _regular_file(exe, "game executable")
    reserved = [
        pck.parent / f".{pck.name}.ko-install.tmp",
        pck.parent / f".{pck.name}.ko-restore.tmp",
        pck.parent / f".{pck.name}.ko-swap.tmp",
    ]
    swap = reserved[2]
    # A PowerShell fallback can be interrupted after moving the destination
    # aside. Recover only when the complete verified install state proves the
    # swap belongs to this package; arbitrary pre-existing paths stay untouched.
    if not pck.exists() and swap.exists():
        _real_directory(backup_dir, "backup directory")
        if {item.name for item in backup_dir.iterdir()} != {BACKUP_PCK_NAME, STATE_NAME}:
            raise PatchReleaseError("cannot authenticate interrupted fallback state")
        _regular_file(backup_pck, "backup PCK")
        _regular_file(state_path, "install state")
        _regular_file(swap, "fallback swap PCK")
        audit = audit_package(
            package_dir=package_dir, source_exe=exe, source_pck=backup_pck,
            _expected_exe_sha256=_expected_exe_sha256,
            _expected_pck_sha256=_expected_pck_sha256,
        )
        expected_state = {
            "schema_version": 1, "build_id": TARGET_BUILD_ID,
            "manifest_sha256": audit["manifest_sha256"],
            "source_pck_sha256": _expected_pck_sha256,
            "localized_pck_sha256": audit["localized_pck_sha256"],
            "game_pck": GAME_PCK_NAME, "backup_pck": BACKUP_PCK_NAME,
        }
        if _load_json(state_path, "install state") != expected_state:
            raise PatchReleaseError("install state is invalid or belongs to another package")
        if sha256_file(swap) not in {_expected_pck_sha256, audit["localized_pck_sha256"]}:
            raise PatchReleaseError("fallback swap PCK does not belong to the verified install")
        restored = reserved[1]
        if restored.exists() or restored.is_symlink():
            raise PatchReleaseError(f"reserved temporary path already exists: {restored}")
        _atomic_write(restored, backup_pck.read_bytes(), mode=stat.S_IMODE(backup_pck.stat().st_mode))
        os.replace(restored, pck)
        _require_hash(pck, _expected_pck_sha256, "recovered original PCK")
        swap.unlink()
    _regular_file(pck, "installed game PCK")
    for item in reserved:
        if item.exists() or item.is_symlink():
            raise PatchReleaseError(f"reserved temporary path already exists: {item}")
    _real_directory(backup_dir, "backup directory")
    backup_entries = {item.name for item in backup_dir.iterdir()}
    expected_entries = {BACKUP_PCK_NAME, STATE_NAME}
    if not backup_entries or not backup_entries <= expected_entries:
        raise PatchReleaseError(
            f"backup directory contains unexpected paths: {sorted(backup_entries)}"
        )
    current_hash = sha256_file(pck)
    # If restoration completed but cleanup was interrupted, validate every
    # surviving state item and finish cleanup idempotently.
    if current_hash == _expected_pck_sha256 and backup_entries != expected_entries:
        audit = audit_package(
            package_dir=package_dir, source_exe=exe, source_pck=pck,
            _expected_exe_sha256=_expected_exe_sha256,
            _expected_pck_sha256=_expected_pck_sha256,
        )
        if BACKUP_PCK_NAME in backup_entries:
            _regular_file(backup_pck, "backup PCK")
            _require_hash(backup_pck, _expected_pck_sha256, "backup PCK")
        if STATE_NAME in backup_entries:
            _regular_file(state_path, "install state")
            state = _load_json(state_path, "install state")
            expected_state = {
                "schema_version": 1, "build_id": TARGET_BUILD_ID,
                "manifest_sha256": audit["manifest_sha256"],
                "source_pck_sha256": _expected_pck_sha256,
                "localized_pck_sha256": audit["localized_pck_sha256"],
                "game_pck": GAME_PCK_NAME, "backup_pck": BACKUP_PCK_NAME,
            }
            if state != expected_state:
                raise PatchReleaseError("install state is invalid or belongs to another package")
        state_path.unlink(missing_ok=True)
        backup_pck.unlink(missing_ok=True)
        backup_dir.rmdir()
        return {
            "result": "UNINSTALLED", "modified_paths": [str(pck), str(backup_pck), str(state_path)],
            "restored_pck_sha256": _expected_pck_sha256,
            "manifest_sha256": audit["manifest_sha256"],
        }
    _regular_file(backup_pck, "backup PCK")
    _regular_file(state_path, "install state")
    state = _load_json(state_path, "install state")
    audit = audit_package(
        package_dir=package_dir, source_exe=exe, source_pck=backup_pck,
        _expected_exe_sha256=_expected_exe_sha256,
        _expected_pck_sha256=_expected_pck_sha256,
    )
    expected_state = {
        "schema_version": 1,
        "build_id": TARGET_BUILD_ID,
        "manifest_sha256": audit["manifest_sha256"],
        "source_pck_sha256": _expected_pck_sha256,
        "localized_pck_sha256": audit["localized_pck_sha256"],
        "game_pck": GAME_PCK_NAME,
        "backup_pck": BACKUP_PCK_NAME,
    }
    if state != expected_state:
        raise PatchReleaseError("install state is invalid or belongs to another package")
    _require_hash(backup_pck, _expected_pck_sha256, "backup PCK")
    if current_hash not in {audit["localized_pck_sha256"], _expected_pck_sha256}:
        raise PatchReleaseError(
            "installed PCK was modified after installation; refusing destructive rollback"
        )
    if current_hash != _expected_pck_sha256:
        restored = reserved[1]
        _atomic_write(
            restored, backup_pck.read_bytes(), mode=stat.S_IMODE(backup_pck.stat().st_mode)
        )
        os.replace(restored, pck)
        _require_hash(pck, _expected_pck_sha256, "restored original PCK")
    state_path.unlink()
    backup_pck.unlink()
    try:
        backup_dir.rmdir()
    except OSError as exc:
        raise PatchReleaseError(
            f"backup directory contains an unexpected residual file: {backup_dir}"
        ) from exc
    return {
        "result": "UNINSTALLED",
        "modified_paths": [str(pck), str(backup_pck), str(state_path)],
        "restored_pck_sha256": _expected_pck_sha256,
        "manifest_sha256": audit["manifest_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a fresh original-free package")
    create.add_argument("--source-exe", required=True, type=Path)
    create.add_argument("--source-pck", required=True, type=Path)
    create.add_argument("--localized-pck", required=True, type=Path)
    create.add_argument("--integration-manifest", required=True, type=Path)
    create.add_argument("--output-dir", required=True, type=Path)
    audit = commands.add_parser("audit", help="verify package integrity and original absence")
    audit.add_argument("--package-dir", required=True, type=Path)
    audit.add_argument("--source-exe", required=True, type=Path)
    audit.add_argument("--source-pck", required=True, type=Path)
    install = commands.add_parser("install", help="install on the exact supported game build")
    install.add_argument("--package-dir", required=True, type=Path)
    install.add_argument("--game-dir", required=True, type=Path)
    uninstall = commands.add_parser("uninstall", help="safely restore the verified original PCK")
    uninstall.add_argument("--package-dir", required=True, type=Path)
    uninstall.add_argument("--game-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_package(
                source_exe=args.source_exe,
                source_pck=args.source_pck,
                localized_pck=args.localized_pck,
                integration_manifest=args.integration_manifest,
                output_dir=args.output_dir,
            )
        elif args.command == "audit":
            result = audit_package(
                package_dir=args.package_dir,
                source_exe=args.source_exe,
                source_pck=args.source_pck,
            )
        elif args.command == "install":
            result = install_package(package_dir=args.package_dir, game_dir=args.game_dir)
        else:
            result = uninstall_package(package_dir=args.package_dir, game_dir=args.game_dir)
    except (OSError, PatchReleaseError) as exc:
        parser.error(str(exc))
    result["python_runtime"] = {
        "executable": sys.executable,
        "implementation": sys.implementation.name,
        "version": ".".join(str(part) for part in sys.version_info[:3]),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
