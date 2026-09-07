from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.patch_release import (
    BACKUP_DIR_NAME,
    DEFAULT_BLOCK_SIZE,
    DELTA_NAME,
    FONT_LICENSE_NAME,
    FONT_LICENSE_SHA256,
    FONT_LICENSE_SOURCE,
    INSTALLER_NAMES,
    INSTALLER_SHA256,
    INSTALLER_SOURCE_DIR,
    INSTALL_DOC_NAME,
    MANIFEST_NAME,
    PatchReleaseError,
    TARGET_BUILD_ID,
    apply_delta_file,
    audit_package,
    create_package,
    install_package,
    sha256_bytes,
    sha256_file,
    uninstall_package,
)


def deterministic_bytes(label: str, size: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < size:
        output.extend(hashlib.sha256(f"{label}:{counter}".encode()).digest())
        counter += 1
    return bytes(output[:size])


def build_integration_manifest(
    *,
    exe_hash: str,
    pck_hash: str,
    localized_hash: str,
    localized_name: str = "drepo.ko.pck",
    statuses: dict | None = None,
) -> dict:
    """Shared production-shaped integration manifest fixture."""

    statuses = statuses or {"APPROVED": 10, "RUNTIME_VALIDATED": 2}
    return {
        "schema_version": 1,
        "tool": "tools/integrate.py",
        "build_id": TARGET_BUILD_ID,
        "mode": "production",
        "inputs": {
            "sha256": {
                "executable": exe_hash,
                "pck": pck_hash,
                "segments": "1" * 64,
                "source_manifest": "2" * 64,
                "assignments": "3" * 64,
            }
        },
        "merge_counts": {
            "integrated_rows": sum(statuses.values()),
            "integrated_by_status": statuses,
        },
        "output": {
            "pck_path": localized_name,
            "pck_sha256": localized_hash,
        },
        "integrated_segment_ids": [
            f"SEG-TEST-{index:04d}" for index in range(sum(statuses.values()))
        ],
    }


class PatchReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.exe = self.inputs / "drepo.exe"
        self.exe.write_bytes(deterministic_bytes("exe", 32 * 1024))
        self.source = self.inputs / "drepo.pck"
        base = deterministic_bytes("pck", DEFAULT_BLOCK_SIZE * 6 + 321)
        self.source.write_bytes(base)
        self.localized = self.inputs / "drepo.ko.pck"
        localized = (
            b"KO-HEADER\0" + base[10:DEFAULT_BLOCK_SIZE * 3]
            + deterministic_bytes("hangul-font-and-translation", 90 * 1024)
            + base[DEFAULT_BLOCK_SIZE * 3:]
        )
        self.localized.write_bytes(localized)
        self.exe_hash = sha256_file(self.exe)
        self.source_hash = sha256_file(self.source)
        self.localized_hash = sha256_file(self.localized)
        self.integration = self.inputs / "integration-manifest.json"
        self.write_integration_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_integration_manifest(self, *, statuses: dict[str, int] | None = None) -> None:
        manifest = build_integration_manifest(
            exe_hash=self.exe_hash,
            pck_hash=self.source_hash,
            localized_hash=self.localized_hash,
            statuses=statuses,
        )
        self.integration.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def create(self, name: str = "package") -> tuple[Path, dict]:
        output = self.root / name
        result = create_package(
            source_exe=self.exe,
            source_pck=self.source,
            localized_pck=self.localized,
            integration_manifest=self.integration,
            output_dir=output,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )
        return output, result

    def audit(self, package: Path, *, source: Path | None = None) -> dict:
        return audit_package(
            package_dir=package,
            source_exe=self.exe,
            source_pck=source or self.source,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )

    def test_installer_type_alias_does_not_require_runtime_pep604_support(self) -> None:
        source = (
            Path(__file__).parents[1] / "tools" / "patch_release.py"
        ).read_text(encoding="utf-8")

        for feature_version in ((3, 8), (3, 9)):
            ast.parse(source, feature_version=feature_version)

        module = ast.parse(source)
        aliases = [
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DeltaOp"
                for target in node.targets
            )
        ]
        self.assertEqual(len(aliases), 1)
        alias_value = aliases[0].value
        self.assertIsInstance(alias_value, ast.Subscript)
        self.assertIsInstance(alias_value.value, ast.Name)
        self.assertEqual(alias_value.value.id, "Union")

    def test_installer_pins_match_the_reviewed_installer_sources(self) -> None:
        actual = {
            name: sha256_file(INSTALLER_SOURCE_DIR / name) for name in INSTALLER_NAMES
        }
        self.assertEqual(
            actual,
            dict(INSTALLER_SHA256),
            "tools/installer/ changed; update INSTALLER_SHA256 in tools/patch_release.py",
        )

    def test_packaged_installer_is_powershell_only_and_byte_identical(self) -> None:
        package, _ = self.create()

        self.assertFalse((package / "patch_release.py").exists())
        for name in INSTALLER_NAMES:
            self.assertEqual(
                (package / name).read_bytes(),
                (INSTALLER_SOURCE_DIR / name).read_bytes(),
            )

        manifest = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["installer"], {"files": dict(INSTALLER_SHA256)})

        # Windows PowerShell 5.1 reads a BOM-less script as ANSI and mangles the
        # Korean messages, so the BOM is part of the contract.
        self.assertTrue(
            (package / "patch_release.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
        )
        for name in ("install.cmd", "uninstall.cmd"):
            payload = (package / name).read_bytes()
            self.assertEqual(payload, payload.decode("ascii").encode("ascii"))
            self.assertIn(b"\r\n", payload)

    def test_tampered_packaged_installer_is_rejected(self) -> None:
        package, _ = self.create("tampered-installer")
        (package / "install.cmd").write_bytes(b"@echo off\r\nrem tampered\r\n")
        with self.assertRaisesRegex(PatchReleaseError, "installer install.cmd SHA-256"):
            self.audit(package)

    def test_create_rejects_unpinned_installer_source(self) -> None:
        source_dir = self.inputs / "installer"
        source_dir.mkdir()
        for name in INSTALLER_NAMES:
            source_dir.joinpath(name).write_bytes(
                (INSTALLER_SOURCE_DIR / name).read_bytes() + b"tampered"
            )
        with self.assertRaisesRegex(PatchReleaseError, "installer source .* SHA-256 mismatch"):
            create_package(
                source_exe=self.exe,
                source_pck=self.source,
                localized_pck=self.localized,
                integration_manifest=self.integration,
                output_dir=self.root / "bad-installer-package",
                _expected_exe_sha256=self.exe_hash,
                _expected_pck_sha256=self.source_hash,
                _installer_source_dir=source_dir,
            )
        self.assertFalse((self.root / "bad-installer-package").exists())

    def test_create_audit_apply_roundtrip_and_original_absence(self) -> None:
        package, result = self.create()

        self.assertEqual(
            {path.name for path in package.iterdir()},
            {
                MANIFEST_NAME,
                DELTA_NAME,
                INSTALL_DOC_NAME,
                FONT_LICENSE_NAME,
                *INSTALLER_NAMES,
            },
        )
        self.assertTrue(result["audit"]["exact_source_hashes_verified"])
        self.assertTrue(result["audit"]["original_files_absent"])
        self.assertTrue(result["audit"]["standalone_reconstruction_rejected"])
        self.assertGreater(result["delta_metrics"]["copy_bytes"], 0)
        self.assertGreater(result["delta_metrics"]["add_bytes"], 0)
        self.assertEqual(
            (package / FONT_LICENSE_NAME).read_bytes(), FONT_LICENSE_SOURCE.read_bytes()
        )

        output = self.root / "roundtrip.pck"
        manifest = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["font_license"],
            {"file": FONT_LICENSE_NAME, "sha256": FONT_LICENSE_SHA256},
        )
        applied_hash = apply_delta_file(
            self.source,
            package / DELTA_NAME,
            output,
            expected_base_sha256=self.source_hash,
            expected_delta_sha256=manifest["delta"]["sha256"],
            expected_output_sha256=self.localized_hash,
        )
        self.assertEqual(applied_hash, self.localized_hash)
        self.assertEqual(output.read_bytes(), self.localized.read_bytes())

        original_hashes = {self.exe_hash, self.source_hash}
        self.assertFalse(original_hashes & {sha256_file(path) for path in package.iterdir()})
        for path in package.iterdir():
            payload = path.read_bytes()
            self.assertNotIn(self.exe.read_bytes(), payload)
            self.assertNotIn(self.source.read_bytes(), payload)

    def test_package_is_reproducible_and_never_overwrites_outputs(self) -> None:
        first, _ = self.create("first")
        second, _ = self.create("second")
        self.assertIn("UNCHECKED_PROVISIONAL", (first / INSTALL_DOC_NAME).read_text())
        for name in (
            MANIFEST_NAME,
            DELTA_NAME,
            INSTALL_DOC_NAME,
            FONT_LICENSE_NAME,
            *INSTALLER_NAMES,
        ):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())

        with self.assertRaisesRegex(PatchReleaseError, "fresh path"):
            self.create("first")
        sentinel = self.root / "sentinel.pck"
        sentinel.write_bytes(b"do-not-overwrite")
        manifest = json.loads((first / MANIFEST_NAME).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(PatchReleaseError, "overwrite"):
            apply_delta_file(
                self.source,
                first / DELTA_NAME,
                sentinel,
                expected_base_sha256=self.source_hash,
                expected_delta_sha256=manifest["delta"]["sha256"],
                expected_output_sha256=self.localized_hash,
            )
        self.assertEqual(sentinel.read_bytes(), b"do-not-overwrite")

    def test_wrong_base_and_delta_tampering_are_rejected(self) -> None:
        package, _ = self.create()
        wrong = self.inputs / "wrong.pck"
        wrong.write_bytes(self.source.read_bytes()[:-1] + b"X")
        with self.assertRaisesRegex(PatchReleaseError, "SHA-256 mismatch"):
            self.audit(package, source=wrong)

        delta = package / DELTA_NAME
        damaged = bytearray(delta.read_bytes())
        damaged[-1] ^= 0x01
        delta.write_bytes(damaged)
        with self.assertRaisesRegex(PatchReleaseError, "delta payload SHA-256 mismatch"):
            self.audit(package)

    def test_missing_or_tampered_font_license_is_rejected(self) -> None:
        missing, _ = self.create("missing-license")
        (missing / FONT_LICENSE_NAME).unlink()
        with self.assertRaisesRegex(PatchReleaseError, "package file set mismatch"):
            self.audit(missing)

        tampered, _ = self.create("tampered-license")
        (tampered / FONT_LICENSE_NAME).write_bytes(b"not the OFL license\n")
        manifest_path = tampered / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["font_license"]["sha256"] = sha256_file(tampered / FONT_LICENSE_NAME)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(PatchReleaseError, "font license pin mismatch"):
            self.audit(tampered)

    def test_oversized_manifest_is_rejected_before_json_parsing(self) -> None:
        package, _ = self.create("oversized-manifest")
        (package / MANIFEST_NAME).write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(PatchReleaseError, "release manifest is too large"):
            self.audit(package)

    def test_create_rejects_unpinned_font_license_source(self) -> None:
        font_license = self.inputs / "LICENSE.txt"
        font_license.write_bytes(FONT_LICENSE_SOURCE.read_bytes() + b"tampered")
        with self.assertRaisesRegex(PatchReleaseError, "font license SHA-256 mismatch"):
            create_package(
                source_exe=self.exe,
                source_pck=self.source,
                localized_pck=self.localized,
                integration_manifest=self.integration,
                output_dir=self.root / "bad-license-package",
                _expected_exe_sha256=self.exe_hash,
                _expected_pck_sha256=self.source_hash,
                _font_license_source=font_license,
            )
        self.assertFalse((self.root / "bad-license-package").exists())

    def test_nonapproved_integration_manifest_is_rejected(self) -> None:
        self.write_integration_manifest(statuses={"APPROVED": 10, "REVIEWED": 2})
        with self.assertRaisesRegex(PatchReleaseError, "non-approved"):
            self.create()
        self.assertFalse((self.root / "package").exists())

    def test_install_and_uninstall_are_hash_validating_and_scoped(self) -> None:
        package, package_result = self.create()
        game = self.root / "game"
        game.mkdir()
        game_exe = game / "drepo.exe"
        game_pck = game / "drepo.pck"
        game_exe.write_bytes(self.exe.read_bytes())
        game_pck.write_bytes(self.source.read_bytes())
        unrelated = game / "save-data.bin"
        unrelated.write_bytes(b"keep-me")

        installed = install_package(
            package_dir=package,
            game_dir=game,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )
        self.assertEqual(installed["result"], "INSTALLED")
        self.assertEqual(sha256_file(game_pck), self.localized_hash)
        self.assertEqual(
            sha256_file(game / BACKUP_DIR_NAME / "drepo.pck.original"), self.source_hash
        )
        self.assertEqual(unrelated.read_bytes(), b"keep-me")
        with self.assertRaisesRegex(PatchReleaseError, "state already exists"):
            install_package(
                package_dir=package,
                game_dir=game,
                _expected_exe_sha256=self.exe_hash,
                _expected_pck_sha256=self.source_hash,
            )

        removed = uninstall_package(
            package_dir=package,
            game_dir=game,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )
        self.assertEqual(removed["result"], "UNINSTALLED")
        self.assertEqual(sha256_file(game_pck), self.source_hash)
        self.assertFalse((game / BACKUP_DIR_NAME).exists())
        self.assertEqual(unrelated.read_bytes(), b"keep-me")
        self.assertEqual(removed["manifest_sha256"], package_result["manifest_sha256"])

    def test_install_preserves_preexisting_reserved_temporaries(self) -> None:
        package, _ = self.create()
        for name in (
            ".drepo.pck.ko-install.tmp",
            ".drepo.pck.ko-restore.tmp",
            ".drepo.pck.ko-swap.tmp",
        ):
            with self.subTest(name=name):
                game = self.root / ("game-" + name.replace(".", "x"))
                game.mkdir()
                (game / "drepo.exe").write_bytes(self.exe.read_bytes())
                (game / "drepo.pck").write_bytes(self.source.read_bytes())
                reserved = game / name
                reserved.write_bytes(b"belongs-to-user")
                with self.assertRaisesRegex(PatchReleaseError, "reserved temporary"):
                    install_package(
                        package_dir=package, game_dir=game,
                        _expected_exe_sha256=self.exe_hash,
                        _expected_pck_sha256=self.source_hash,
                    )
                self.assertEqual(reserved.read_bytes(), b"belongs-to-user")
                self.assertEqual((game / "drepo.pck").read_bytes(), self.source.read_bytes())
                self.assertFalse((game / BACKUP_DIR_NAME).exists())

    def test_uninstall_finishes_verified_partial_cleanup(self) -> None:
        package, _ = self.create()
        for missing in ("drepo.pck.original", "install-state.json"):
            with self.subTest(missing=missing):
                game = self.root / ("partial-" + missing.replace(".", "-"))
                game.mkdir()
                (game / "drepo.exe").write_bytes(self.exe.read_bytes())
                (game / "drepo.pck").write_bytes(self.source.read_bytes())
                install_package(
                    package_dir=package, game_dir=game,
                    _expected_exe_sha256=self.exe_hash,
                    _expected_pck_sha256=self.source_hash,
                )
                # Model an interruption after the original was restored and
                # after only one cleanup unlink completed.
                (game / "drepo.pck").write_bytes(self.source.read_bytes())
                (game / BACKUP_DIR_NAME / missing).unlink()
                result = uninstall_package(
                    package_dir=package, game_dir=game,
                    _expected_exe_sha256=self.exe_hash,
                    _expected_pck_sha256=self.source_hash,
                )
                self.assertEqual(result["result"], "UNINSTALLED")
                self.assertFalse((game / BACKUP_DIR_NAME).exists())

    def test_uninstall_recovers_authenticated_interrupted_fallback_swap(self) -> None:
        package, _ = self.create()
        game = self.root / "interrupted-fallback"
        game.mkdir()
        (game / "drepo.exe").write_bytes(self.exe.read_bytes())
        (game / "drepo.pck").write_bytes(self.source.read_bytes())
        install_package(
            package_dir=package, game_dir=game,
            _expected_exe_sha256=self.exe_hash, _expected_pck_sha256=self.source_hash,
        )
        (game / "drepo.pck").replace(game / ".drepo.pck.ko-swap.tmp")
        result = uninstall_package(
            package_dir=package, game_dir=game,
            _expected_exe_sha256=self.exe_hash, _expected_pck_sha256=self.source_hash,
        )
        self.assertEqual(result["result"], "UNINSTALLED")
        self.assertEqual((game / "drepo.pck").read_bytes(), self.source.read_bytes())
        self.assertFalse((game / ".drepo.pck.ko-swap.tmp").exists())
        self.assertFalse((game / BACKUP_DIR_NAME).exists())

    def test_uninstall_refuses_to_destroy_postinstall_modification(self) -> None:
        package, _ = self.create()
        game = self.root / "game"
        game.mkdir()
        (game / "drepo.exe").write_bytes(self.exe.read_bytes())
        game_pck = game / "drepo.pck"
        game_pck.write_bytes(self.source.read_bytes())
        install_package(
            package_dir=package,
            game_dir=game,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )
        game_pck.write_bytes(b"user-modified-after-install")
        with self.assertRaisesRegex(PatchReleaseError, "modified after installation"):
            uninstall_package(
                package_dir=package,
                game_dir=game,
                _expected_exe_sha256=self.exe_hash,
                _expected_pck_sha256=self.source_hash,
            )
        self.assertEqual(game_pck.read_bytes(), b"user-modified-after-install")
        self.assertTrue((game / BACKUP_DIR_NAME / "drepo.pck.original").is_file())


if __name__ == "__main__":
    unittest.main()
