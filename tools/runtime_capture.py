#!/usr/bin/env python3
"""Install deterministic runtime-capture hooks in a generated desktop source.

The recovered project is an immutable input.  This module patches only a
generated ``scripts/desktop.gd`` copy, and every reviewed source unit must
match exactly once before an output is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


class RuntimeCaptureError(ValueError):
    """The generated desktop source does not satisfy the capture contract."""


REPORT_CASES_DEFAULT = "01,03,06,09,10,25"

REPORT_SOURCE = '''\telif OS.has_environment("GODOT_SHOT_REPORT"):
\t\tif OS.has_environment("GODOT_SEED_FILING"):
\t\t\tfor num in ["01", "02", "03", "04", "05", "06"]:
\t\t\t\tvar s: = GameData.solution_for_num(num)
\t\t\t\tif s:
\t\t\t\t\tGameState.set_report(num, s.manner_id(), s.weapon_id(), s.culprit)

\t\topen_window("report")
'''

REPORT_TARGET = '''\telif OS.has_environment("GODOT_SHOT_REPORT"):
\t\tif not _shot_report_cases():
\t\t\treturn
'''

ENDING_SOURCE = '''\telif OS.has_environment("GODOT_SHOT_ENDING"):
\t\tGameState.debug_force_ending()
\telif OS.has_environment("GODOT_SHOT_CREDITS"):
'''

ENDING_TARGET = '''\telif OS.has_environment("GODOT_SHOT_FINAL_REPORT"):
\t\tif not _shot_final_report():
\t\t\treturn
\telif OS.has_environment("GODOT_SHOT_ENDING"):
\t\tif not _shot_ending_report():
\t\t\treturn
\telif OS.has_environment("GODOT_SHOT_DIGEST_KIND"):
\t\tif not _shot_ending_digest():
\t\t\treturn
\telif OS.has_environment("GODOT_SHOT_CREDITS"):
'''

DEBUG_CAPTURE_ANCHOR = "func _debug_capture() -> void :\n"
DEBUG_CAPTURE_TARGET = '''func _debug_capture() -> void :
\tif not _shot_validate_selector_set():
\t\treturn
'''
PATCH_MARKER = "const _SHOT_REPORT_DEFAULT_CASES: = \"01,03,06,09,10,25\""

CAPTURE_HELPERS = '''const _SHOT_REPORT_DEFAULT_CASES: = "01,03,06,09,10,25"


func _shot_fail(message: String) -> void :
\tpush_error("GODOT_SHOT selector error: " + message)
\tget_tree().quit(2)


func _shot_validate_selector_set() -> bool:
\tvar selectors: = [
\t\t"GODOT_SHOT_DESK", "GODOT_SHOT_CAUSE", "GODOT_SHOT_MAP",
\t\t"GODOT_SHOT_ROSTER", "GODOT_SHOT_REPORT", "GODOT_SHOT_MESSAGE",
\t\t"GODOT_SHOT_START", "GODOT_SHOT_INTRO", "GODOT_SHOT_TITLE",
\t\t"GODOT_SHOT_BOOT", "GODOT_SHOT_CONFIG", "GODOT_SHOT_REFERENCE",
\t\t"GODOT_SHOT_FINAL_REPORT", "GODOT_SHOT_ENDING",
\t\t"GODOT_SHOT_DIGEST_KIND", "GODOT_SHOT_CREDITS", "GODOT_SHOT_SAVE",
\t\t"GODOT_SHOT_LOAD", "GODOT_SHOT_ORDER", "GODOT_SHOT_BOOTPOPUP",
\t\t"GODOT_SHOT_AUTOSAVE",
\t]
\tvar selected: Array = []
\tfor selector: String in selectors:
\t\tif OS.has_environment(selector):
\t\t\tselected.append(selector)
\tif selected.size() > 1:
\t\t_shot_fail("capture selectors are mutually exclusive: " + ",".join(selected))
\t\treturn false
\tif OS.has_environment("GODOT_SHOT_REPORT_CASES") and not OS.has_environment("GODOT_SHOT_REPORT"):
\t\t_shot_fail("GODOT_SHOT_REPORT_CASES requires GODOT_SHOT_REPORT")
\t\treturn false
\tif OS.has_environment("GODOT_SHOT_ENDING_KIND") and not OS.has_environment("GODOT_SHOT_ENDING"):
\t\t_shot_fail("GODOT_SHOT_ENDING_KIND requires GODOT_SHOT_ENDING=1")
\t\treturn false
\tif OS.has_environment("GODOT_SHOT_DIGEST_FRAME") and not OS.has_environment("GODOT_SHOT_DIGEST_KIND"):
\t\t_shot_fail("GODOT_SHOT_DIGEST_FRAME requires GODOT_SHOT_DIGEST_KIND")
\t\treturn false
\treturn true


func _shot_report_case_nums() -> Array:
\tvar raw: = _SHOT_REPORT_DEFAULT_CASES
\tif OS.has_environment("GODOT_SHOT_REPORT_CASES"):
\t\traw = OS.get_environment("GODOT_SHOT_REPORT_CASES")
\tvar nums: Array = raw.split(",", true)
\tif nums.size() != 6:
\t\t_shot_fail("GODOT_SHOT_REPORT_CASES must contain exactly six CSV numbers")
\t\treturn []
\tvar seen: = {}
\tfor value: Variant in nums:
\t\tvar num: = str(value)
\t\tif num.length() != 2 or not num.is_valid_int() or ("%02d" % int(num)) != num:
\t\t\t_shot_fail("invalid report case number: " + num)
\t\t\treturn []
\t\tif seen.has(num):
\t\t\t_shot_fail("duplicate report case number: " + num)
\t\t\treturn []
\t\tvar solution: = GameData.solution_for_num(num)
\t\tif solution == null or solution.manner_id() == "":
\t\t\t_shot_fail("unknown or non-reportable case number: " + num)
\t\t\treturn []
\t\tseen[num] = true
\treturn nums


func _shot_report_cases() -> bool:
\tvar nums: = _shot_report_case_nums()
\tif nums.is_empty():
\t\treturn false
\tfor value: Variant in nums:
\t\tvar num: = str(value)
\t\tvar solution: = GameData.solution_for_num(num)
\t\tGameState.set_report(num, solution.manner_id(), solution.weapon_id(), solution.culprit)
\topen_window("report")
\tif not is_instance_valid(_death_report):
\t\t_shot_fail("death report window was not created")
\t\treturn false
\tfor num: String in _death_report._cards:
\t\t(_death_report._cards[num]["panel"] as CanvasItem).visible = nums.has(num)
\tfor value: Variant in nums:
\t\tif not _death_report._cards.has(str(value)):
\t\t\t_shot_fail("death report card is missing: " + str(value))
\t\t\treturn false
\tif _windows.has("report"):
\t\tvar report_window: MacWindow = _windows["report"]
\t\treport_window.position = Vector2(200, 50)
\t\treport_window.size = Vector2(880, 620)
\t\tfor window_id: String in _windows:
\t\t\tif window_id != "report":
\t\t\t\t(_windows[window_id] as CanvasItem).hide()
\t\t_bring_to_front(report_window)
\tif _death_report._grid:
\t\t_death_report._grid.columns = 3
\treturn true


func _shot_prepare_ending(kind: String) -> bool:
\tif kind != "correct" and kind != "conceal":
\t\t_shot_fail("ending kind must be exactly correct or conceal")
\t\treturn false
\tGameState._enter_finale()
\tvar cause: = GameData.SURVIVE_CAUSE if kind == "correct" else "blast"
\tGameState.set_report(GameData.FINAL_CHOICE_NUM, cause, "none", "")
\tif not GameState.submit_ending():
\t\t_shot_fail("could not submit the requested ending")
\t\treturn false
\tvar expected: = GameState.Ending.CORRECT if kind == "correct" else GameState.Ending.CONCEAL
\tif GameState.ending_kind() != expected:
\t\t_shot_fail("submitted ending kind did not match the selector")
\t\treturn false
\treturn true


func _shot_final_report() -> bool:
\tif OS.get_environment("GODOT_SHOT_FINAL_REPORT") != "alive":
\t\t_shot_fail("GODOT_SHOT_FINAL_REPORT must be exactly alive")
\t\treturn false
\tGameState._enter_finale()
\topen_window("report")
\tif not is_instance_valid(_death_report):
\t\t_shot_fail("death report window was not created")
\t\treturn false
\t_death_report.reveal_final_choice()
\tGameState.set_report(GameData.FINAL_CHOICE_NUM, GameData.SURVIVE_CAUSE, "none", "")
\tif not GameState.submit_ending():
\t\t_shot_fail("could not submit the alive final report")
\t\treturn false
\tfor num: String in _death_report._cards:
\t\t(_death_report._cards[num]["panel"] as CanvasItem).visible = num == GameData.FINAL_CHOICE_NUM
\tif _windows.has("report"):
\t\tvar report_window: MacWindow = _windows["report"]
\t\treport_window.position = Vector2(200, 50)
\t\treport_window.size = Vector2(880, 620)
\t\tfor window_id: String in _windows:
\t\t\tif window_id != "report":
\t\t\t\t(_windows[window_id] as CanvasItem).hide()
\t\t_bring_to_front(report_window)
\treturn true


func _shot_ending_report() -> bool:
\tif OS.get_environment("GODOT_SHOT_ENDING") != "1":
\t\t_shot_fail("GODOT_SHOT_ENDING must be exactly 1")
\t\treturn false
\tif not OS.has_environment("GODOT_SHOT_ENDING_KIND"):
\t\t_shot_fail("GODOT_SHOT_ENDING_KIND is required")
\t\treturn false
\tif not _shot_prepare_ending(OS.get_environment("GODOT_SHOT_ENDING_KIND")):
\t\treturn false
\t_ed_report()
\treturn true


func _shot_ending_digest() -> bool:
\tvar kind: = OS.get_environment("GODOT_SHOT_DIGEST_KIND")
\tif kind != "correct" and kind != "conceal":
\t\t_shot_fail("GODOT_SHOT_DIGEST_KIND must be exactly correct or conceal")
\t\treturn false
\tif not OS.has_environment("GODOT_SHOT_DIGEST_FRAME"):
\t\t_shot_fail("GODOT_SHOT_DIGEST_FRAME is required")
\t\treturn false
\tvar frame_raw: = OS.get_environment("GODOT_SHOT_DIGEST_FRAME")
\tif frame_raw != "0" and frame_raw != "2":
\t\t_shot_fail("GODOT_SHOT_DIGEST_FRAME must be exactly 0 or 2")
\t\treturn false
\tif not _shot_prepare_ending(kind):
\t\treturn false
\tvar digest: = preload("res://scenes/ending_digest.tscn").instantiate() as EndingDigest
\tdigest.setup(GameState.ending_kind())
\tdigest._idx = int(frame_raw)
\t_add_overlay(digest, Color.BLACK, true)
\treturn true


'''


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_text(source: str) -> str:
    """Return a patched desktop source or fail without a partial result."""

    if PATCH_MARKER in source:
        raise RuntimeCaptureError("runtime capture hooks are already patched")
    replacements = (
        ("report capture branch", REPORT_SOURCE, REPORT_TARGET),
        ("ending capture branch", ENDING_SOURCE, ENDING_TARGET),
        (
            "debug capture helper anchor",
            DEBUG_CAPTURE_ANCHOR,
            CAPTURE_HELPERS + DEBUG_CAPTURE_TARGET,
        ),
    )
    for label, old, _new in replacements:
        count = source.count(old)
        if count != 1:
            raise RuntimeCaptureError(
                f"{label} source unit must occur exactly once; found {count}"
            )
    patched = source
    for _label, old, new in replacements:
        patched = patched.replace(old, new, 1)
    return patched


def patch_generated_file(source_path: Path | str, output_path: Path | str) -> dict[str, str]:
    """Patch a generated desktop source without overwriting either input or output."""

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise RuntimeCaptureError("source and output must be different paths")
    if source.is_symlink() or not source.is_file():
        raise RuntimeCaptureError(f"source must be a regular non-symlink file: {source}")
    if output.exists() or output.is_symlink():
        raise RuntimeCaptureError(f"refusing to overwrite output: {output}")
    raw = source.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RuntimeCaptureError("UTF-8 BOM is not supported")
    patched = patch_text(raw.decode("utf-8")).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(patched)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise RuntimeCaptureError(f"refusing to overwrite output: {output}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source": str(source),
        "output": str(output),
        "source_sha256": sha256_bytes(raw),
        "output_sha256": sha256_bytes(patched),
        "report_cases_default": REPORT_CASES_DEFAULT,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = patch_generated_file(args.source, args.output)
    except (OSError, UnicodeError, RuntimeCaptureError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
