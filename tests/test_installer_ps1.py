"""End-to-end checks for the dependency-free Windows PowerShell installer.

The shipped installer is `tools/installer/patch_release.ps1`; `tools/patch_release.py`
stays in the repository as the packaging tool and as the reference implementation.
These tests run the real Windows PowerShell through WSL interop and require the
fixtures to live on the Windows filesystem, because `ReplaceFile` and the
Win32 file attributes the installer relies on are not available on the 9p mount.
Everything is skipped when that environment is not present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_patch_release import build_integration_manifest, deterministic_bytes
from tools.patch_release import (
    BACKUP_DIR_NAME,
    BACKUP_PCK_NAME,
    DEFAULT_BLOCK_SIZE,
    DELTA_NAME,
    INSTALLER_PS1_NAME,
    STATE_NAME,
    create_package,
    install_package,
    sha256_file,
    uninstall_package,
)

POWERSHELL_CANDIDATES = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
)


def find_powershell() -> str | None:
    override = os.environ.get("PATCH_RELEASE_POWERSHELL")
    if override:
        return override if Path(override).exists() else None
    for candidate in POWERSHELL_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("powershell.exe")


def to_windows_path(path: Path) -> str:
    return subprocess.run(
        ["wslpath", "-w", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def windows_temp_root(powershell: str) -> Path | None:
    """Return a Linux-visible path to the Windows user temp directory."""

    try:
        windows_temp = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command",
             "[System.IO.Path]::GetTempPath()"],
            check=True, capture_output=True, text=True, timeout=120,
        ).stdout.strip()
        linux_temp = subprocess.run(
            ["wslpath", "-u", windows_temp], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    root = Path(linux_temp)
    return root if root.is_dir() and os.access(root, os.W_OK) else None


class PowerShellInstallerTests(unittest.TestCase):
    """The PowerShell installer must be interchangeable with the Python one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.powershell = find_powershell()
        if not cls.powershell:
            raise unittest.SkipTest("Windows PowerShell is not reachable from this host")
        cls.windows_temp = windows_temp_root(cls.powershell)
        if cls.windows_temp is None:
            raise unittest.SkipTest("no writable Windows-filesystem temp directory")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=str(self.windows_temp))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.exe = self.inputs / "drepo.exe"
        self.exe.write_bytes(deterministic_bytes("exe", 32 * 1024))
        self.source = self.inputs / "drepo.pck"
        base = deterministic_bytes("pck", DEFAULT_BLOCK_SIZE * 6 + 321)
        self.source.write_bytes(base)
        self.localized = self.inputs / "drepo.ko.pck"
        self.localized.write_bytes(
            b"KO-HEADER\0" + base[10:DEFAULT_BLOCK_SIZE * 3]
            + deterministic_bytes("hangul-font-and-translation", 90 * 1024)
            + base[DEFAULT_BLOCK_SIZE * 3:]
        )
        self.exe_hash = sha256_file(self.exe)
        self.source_hash = sha256_file(self.source)
        self.localized_hash = sha256_file(self.localized)

        self.integration = self.inputs / "integration-manifest.json"
        self.integration.write_text(
            json.dumps(
                build_integration_manifest(
                    exe_hash=self.exe_hash,
                    pck_hash=self.source_hash,
                    localized_hash=self.localized_hash,
                ),
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )

        self.package = self.root / "package"
        create_package(
            source_exe=self.exe,
            source_pck=self.source,
            localized_pck=self.localized,
            integration_manifest=self.integration,
            output_dir=self.package,
            _expected_exe_sha256=self.exe_hash,
            _expected_pck_sha256=self.source_hash,
        )

    def make_game_dir(self, name: str) -> Path:
        game = self.root / name
        game.mkdir()
        shutil.copyfile(self.exe, game / "drepo.exe")
        shutil.copyfile(self.source, game / "drepo.pck")
        (game / "unrelated-sentinel.dat").write_bytes(b"do-not-touch")
        return game

    def run_installer(self, command: str, game: Path, *, expect_success: bool = True):
        result = subprocess.run(
            [
                self.powershell, "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", to_windows_path(self.package / INSTALLER_PS1_NAME),
                "-Command", command,
                "-PackageDir", to_windows_path(self.package),
                "-GameDir", to_windows_path(game),
                "-ExpectedExeSha256", self.exe_hash,
                "-ExpectedPckSha256", self.source_hash,
            ],
            capture_output=True, text=True, errors="replace", timeout=600,
        )
        output = f"{result.stdout}\n{result.returncode}\n{result.stderr}"
        if expect_success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertEqual(result.returncode, 1, output)
        return result

    def python_install(self, game: Path) -> None:
        install_package(
            package_dir=self.package, game_dir=game,
            _expected_exe_sha256=self.exe_hash, _expected_pck_sha256=self.source_hash,
        )

    def python_uninstall(self, game: Path) -> None:
        uninstall_package(
            package_dir=self.package, game_dir=game,
            _expected_exe_sha256=self.exe_hash, _expected_pck_sha256=self.source_hash,
        )

    def assert_untouched(self, game: Path) -> None:
        self.assertEqual((game / "drepo.pck").read_bytes(), self.source.read_bytes())
        self.assertEqual((game / "unrelated-sentinel.dat").read_bytes(), b"do-not-touch")
        self.assertFalse((game / BACKUP_DIR_NAME).exists())
        self.assertEqual(
            sorted(item.name for item in game.iterdir()),
            ["drepo.exe", "drepo.pck", "unrelated-sentinel.dat"],
        )

    def run_function_probe(self, body: str) -> str:
        """Run PowerShell against the shipped script's function definitions only.

        The functions are re-declared from the AST so the probe never executes
        the script's parameter block or main entry point.
        """

        script = self.package / INSTALLER_PS1_NAME
        probe = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)\n"
            "$errors=$null; $tokens=$null\n"
            f"$ast=[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{to_windows_path(script)}',[ref]$tokens,[ref]$errors)\n"
            "if ($errors) { throw 'parse errors' }\n"
            "foreach ($fn in $ast.FindAll({$args[0] -is "
            "[System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) "
            "{ Invoke-Expression $fn.Extent.Text }\n"
            + body
        )
        result = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True, text=True, errors="replace", timeout=300,
        )
        self.assertEqual(
            result.returncode, 0, f"{result.stdout}\n{result.stderr}"
        )
        return result.stdout

    def run_launcher(self, command: str, *, cwd: Path, expect_success: bool = True):
        launcher = to_windows_path(self.package / (command + ".cmd"))
        result = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command",
             f"& '{launcher}' -ExpectedExeSha256 {self.exe_hash} "
             f"-ExpectedPckSha256 {self.source_hash}; exit $LASTEXITCODE"],
            cwd=cwd, input="\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        self.assertEqual(result.returncode, 0 if expect_success else 1,
                         result.stdout + result.stderr)
        self.assertNotIn("Steam", result.stdout)
        return result

    def test_nested_package_launchers_roundtrip_from_unrelated_cwd(self) -> None:
        game = self.make_game_dir("게임 설치 [테스트]")
        nested = game / "한글 패치 rc10"
        shutil.move(self.package, nested)
        self.package = nested
        result = self.run_launcher("install", cwd=self.root)
        self.assertIn("패치할 게임 폴더:", result.stdout)
        self.assertEqual(sha256_file(game / "drepo.pck"), self.localized_hash)
        self.assertEqual(sha256_file(game / BACKUP_DIR_NAME / BACKUP_PCK_NAME),
                         self.source_hash)
        self.run_launcher("uninstall", cwd=self.inputs)
        self.assertEqual(sha256_file(game / "drepo.pck"), self.source_hash)
        self.assertFalse((game / BACKUP_DIR_NAME).exists())
        self.assertEqual((game / "unrelated-sentinel.dat").read_bytes(), b"do-not-touch")

    def test_missing_parent_game_does_not_use_current_directory(self) -> None:
        game = self.make_game_dir("valid-game-in-cwd")
        result = self.run_launcher("install", cwd=game, expect_success=False)
        self.assertIn("패치 폴더 바로 위에서", result.stdout)
        self.assert_untouched(game)

    def test_nested_package_rejects_incompatible_parent(self) -> None:
        game = self.make_game_dir("wrong-version")
        (game / "drepo.exe").write_bytes(b"wrong build")
        nested = game / "patch"
        shutil.move(self.package, nested)
        self.package = nested
        self.run_launcher("install", cwd=self.root, expect_success=False)
        self.assertEqual(sha256_file(game / "drepo.pck"), self.source_hash)
        self.assertFalse((game / BACKUP_DIR_NAME).exists())

    def test_powershell_install_is_byte_identical_to_python_install(self) -> None:
        shell_game = self.make_game_dir("game-powershell")
        python_game = self.make_game_dir("game-python")

        installed = self.run_installer("install", shell_game)
        self.python_install(python_game)

        # NTFS must take the atomic ReplaceFile path; the swap fallback exists
        # only for filesystems that cannot do it.
        self.assertIn("replace_mode=atomic", installed.stdout)

        self.assertEqual(
            (shell_game / "drepo.pck").read_bytes(), self.localized.read_bytes()
        )
        self.assertEqual(
            (shell_game / "drepo.pck").read_bytes(),
            (python_game / "drepo.pck").read_bytes(),
        )
        self.assertEqual(
            (shell_game / BACKUP_DIR_NAME / BACKUP_PCK_NAME).read_bytes(),
            self.source.read_bytes(),
        )
        # Byte-identical state keeps the two implementations interchangeable.
        self.assertEqual(
            (shell_game / BACKUP_DIR_NAME / STATE_NAME).read_bytes(),
            (python_game / BACKUP_DIR_NAME / STATE_NAME).read_bytes(),
        )
        self.assertEqual(
            (shell_game / "unrelated-sentinel.dat").read_bytes(), b"do-not-touch"
        )
        self.assertEqual(
            sorted(item.name for item in shell_game.iterdir()),
            [BACKUP_DIR_NAME, "drepo.exe", "drepo.pck", "unrelated-sentinel.dat"],
        )

    def test_powershell_roundtrip_restores_the_original(self) -> None:
        game = self.make_game_dir("game-roundtrip")
        self.run_installer("install", game)
        self.run_installer("uninstall", game)
        self.assert_untouched(game)

    def test_installs_and_uninstalls_are_cross_compatible(self) -> None:
        shell_first = self.make_game_dir("game-shell-first")
        self.run_installer("install", shell_first)
        self.python_uninstall(shell_first)
        self.assert_untouched(shell_first)

        python_first = self.make_game_dir("game-python-first")
        self.python_install(python_first)
        self.run_installer("uninstall", python_first)
        self.assert_untouched(python_first)

    def test_audit_command_verifies_without_touching_the_game(self) -> None:
        game = self.make_game_dir("game-audit")
        result = self.run_installer("audit", game)
        self.assertIn(f"localized_pck_sha256={self.localized_hash}", result.stdout)
        self.assert_untouched(game)

    def test_incompatible_build_is_refused(self) -> None:
        game = self.make_game_dir("game-incompatible")
        (game / "drepo.pck").write_bytes(self.source.read_bytes()[:-1] + b"X")
        self.run_installer("install", game, expect_success=False)
        self.assertFalse((game / BACKUP_DIR_NAME).exists())
        self.assertEqual((game / "unrelated-sentinel.dat").read_bytes(), b"do-not-touch")

    def test_tampered_delta_is_refused(self) -> None:
        game = self.make_game_dir("game-tampered")
        delta = self.package / DELTA_NAME
        damaged = bytearray(delta.read_bytes())
        damaged[-1] ^= 0x01
        delta.write_bytes(bytes(damaged))
        self.run_installer("install", game, expect_success=False)
        self.assert_untouched(game)

    def test_existing_backup_state_blocks_reinstall(self) -> None:
        game = self.make_game_dir("game-existing-state")
        self.run_installer("install", game)
        installed = (game / "drepo.pck").read_bytes()
        self.run_installer("install", game, expect_success=False)
        self.assertEqual((game / "drepo.pck").read_bytes(), installed)
        self.assertEqual(
            (game / BACKUP_DIR_NAME / BACKUP_PCK_NAME).read_bytes(),
            self.source.read_bytes(),
        )

    def test_preexisting_reserved_temporaries_are_preserved(self) -> None:
        for name in (
            ".drepo.pck.ko-install.tmp",
            ".drepo.pck.ko-restore.tmp",
            ".drepo.pck.ko-swap.tmp",
        ):
            with self.subTest(name=name):
                game = self.make_game_dir("reserved-" + name.replace(".", "x"))
                reserved = game / name
                reserved.write_bytes(b"belongs-to-user")
                self.run_installer("install", game, expect_success=False)
                self.assertEqual(reserved.read_bytes(), b"belongs-to-user")
                self.assertEqual((game / "drepo.pck").read_bytes(), self.source.read_bytes())
                self.assertFalse((game / BACKUP_DIR_NAME).exists())

    def test_uninstall_finishes_verified_partial_cleanup(self) -> None:
        for missing in (BACKUP_PCK_NAME, STATE_NAME):
            with self.subTest(missing=missing):
                game = self.make_game_dir("partial-" + missing.replace(".", "-"))
                self.run_installer("install", game)
                (game / "drepo.pck").write_bytes(self.source.read_bytes())
                (game / BACKUP_DIR_NAME / missing).unlink()
                self.run_installer("uninstall", game)
                self.assert_untouched(game)

    def test_uninstall_recovers_authenticated_interrupted_fallback_swap(self) -> None:
        game = self.make_game_dir("interrupted-fallback")
        self.run_installer("install", game)
        (game / "drepo.pck").replace(game / ".drepo.pck.ko-swap.tmp")
        self.run_installer("uninstall", game)
        self.assert_untouched(game)

    def test_locked_game_pck_fails_before_touching_anything(self) -> None:
        # Forgetting to close the game is the most likely user error.
        game = self.make_game_dir("game-locked")
        script = to_windows_path(self.package / INSTALLER_PS1_NAME)
        wrapper = (
            # The child writes UTF-8; the parent host must decode it the same way.
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)\n"
            f"$g = '{to_windows_path(game)}'\n"
            f"$p = '{to_windows_path(self.package)}'\n"
            "$lock = [System.IO.File]::Open("
            "(Join-Path $g 'drepo.pck'), 'Open', 'Read', 'None')\n"
            "try {\n"
            f"  & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '{script}' "
            "-Command install -PackageDir $p -GameDir $g "
            f"-ExpectedExeSha256 '{self.exe_hash}' -ExpectedPckSha256 '{self.source_hash}' "
            "*>&1 | Out-String -Stream | Write-Output\n"
            "  'installer_exit=' + $LASTEXITCODE\n"
            "} finally { $lock.Dispose() }\n"
        )
        result = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", wrapper],
            capture_output=True, text=True, errors="replace", timeout=600,
        )
        self.assertIn("installer_exit=1", result.stdout, result.stdout + result.stderr)
        self.assertIn("다른 프로그램이 사용 중입니다", result.stdout)
        self.assert_untouched(game)

    def test_uninstall_refuses_to_destroy_postinstall_modification(self) -> None:
        game = self.make_game_dir("game-modified")
        self.run_installer("install", game)
        (game / "drepo.pck").write_bytes(b"user modified this after install")
        self.run_installer("uninstall", game, expect_success=False)
        self.assertEqual(
            (game / "drepo.pck").read_bytes(), b"user modified this after install"
        )
        self.assertEqual(
            (game / BACKUP_DIR_NAME / BACKUP_PCK_NAME).read_bytes(),
            self.source.read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
