#!/usr/bin/env python3
"""Apply the reviewed Korean-only runtime grammar patch to generated sources.

This module never edits the recovered project.  It patches generated GDScript
copies after canonical text reinsertion and before GDScript
compilation.  Every replacement is deliberately fail-closed: its reviewed
source unit must match exactly once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable


class KoreanCodeError(ValueError):
    """The generated source does not match the reviewed patch contract."""


OVERRIDE_SOURCE = '"35-B4-09": "%sにハーモニカの紐で絞殺された", '
OVERRIDE_TARGET = '"35-B4-09": "가해자: %s / 사인: 교살 / 흉기: 하모니카 끈", '

SENTENCE_SOURCE = '''static func sentence_for(sol: Solution) -> String:
\tif sol == null:
\t\treturn ""
\tvar ov: String = REPORT_SENTENCE_OVERRIDES.get(sol.record_name, "")
\tif ov != "":
\t\treturn (ov % _nobreak(_surname_of(sol.culprit))) if (ov.contains("%s") and sol.is_murder()) else ov
\tvar manner: = sol.manner()
\tvar weapon: = sol.weapon_manner()
\tvar wp: = "" if not _has_weapon(sol) else weapon + "で"
\tif sol.is_murder():


\t\tvar who: = _nobreak(_surname_of(sol.culprit))
\t\tif sol.manner_id() == "fall":
\t\t\treturn "%sに突き落とされ、落下死" % who
\t\tif sol.manner_id() == "poison":
\t\t\treturn "%sに毒を盛られ、死亡" % who
\t\tif sol.manner_id() in SLAY_MANNERS:
\t\t\treturn "%sに%s%sされた" % [who, wp, manner]
\t\treturn "%sに%s殺害された" % [who, wp]

\tvar s: = flavor_sentence(sol.flavor)
\tif s != "":
\t\treturn s
\tif sol.manner_id() == "fall":
\t\treturn "自ら身を投げて死亡"
\treturn "%sにより死亡" % manner
'''

SENTENCE_TARGET = '''static func sentence_for(sol: Solution) -> String:
\tif sol == null:
\t\treturn ""
\tvar ov: String = REPORT_SENTENCE_OVERRIDES.get(sol.record_name, "")
\tif ov != "":
\t\treturn (ov % _nobreak(_surname_of(sol.culprit))) if (ov.contains("%s") and sol.is_murder()) else ov
\tvar manner: = sol.manner()
\tvar weapon: = sol.weapon_manner()
\tif sol.is_murder():
\t\tvar who: = _nobreak(_surname_of(sol.culprit))
\t\tif sol.manner_id() == "fall":
\t\t\treturn "가해자: %s / 경위: 밀어 떨어뜨림 / 사인: 추락사" % who
\t\tif sol.manner_id() == "poison":
\t\t\treturn "가해자: %s / 사인: 중독사" % who
\t\tif sol.manner_id() in SLAY_MANNERS:
\t\t\tif _has_weapon(sol):
\t\t\t\treturn "가해자: %s / 사인: %s / 흉기: %s" % [who, manner, weapon]
\t\t\treturn "가해자: %s / 사인: %s" % [who, manner]
\t\tif _has_weapon(sol):
\t\t\treturn "가해자: %s / 사인: 타살 / 흉기: %s" % [who, weapon]
\t\treturn "가해자: %s / 사인: 타살" % who

\tvar s: = flavor_sentence(sol.flavor)
\tif s != "":
\t\treturn s
\tif sol.manner_id() == "fall":
\t\treturn "스스로 몸을 던져 사망"
\treturn "사인: %s" % manner
'''

# Production reinsertion localizes the one independently extracted fallback
# literal before this function-level grammar rewrite runs. Pilot/no-change
# inputs may still contain the original literal. Both complete reviewed source
# units are accepted, but their combined match count must remain exactly one.
SENTENCE_SOURCE_WITH_LOCALIZED_FALLBACK = SENTENCE_SOURCE.replace(
    '\t\treturn "自ら身を投げて死亡"',
    '\t\treturn "스스로 몸을 던져 사망"',
)

# ``segments.py`` correctly extracts the translatable answer prefix in this
# expression, but deliberately excludes the punctuation-only suffix.  Once the
# canonical prefix has been reinserted, patch the whole reviewed expression so
# the generated Korean source cannot retain the Japanese full stop.
HINT_ANSWER_PERIOD_SOURCE = '\t\t\treturn "정답 ── " + " ／ ".join(fields) + "。"'
HINT_ANSWER_PERIOD_TARGET = '\t\t\treturn "정답 ── " + " ／ ".join(fields) + "."'


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_text(source: str) -> str:
    for label, old, new in (("report override", OVERRIDE_SOURCE, OVERRIDE_TARGET),):
        count = source.count(old)
        if count != 1:
            raise KoreanCodeError(f"{label} source unit must occur exactly once; found {count}")
        if new in source:
            raise KoreanCodeError(f"{label} target unit is already present")
        source = source.replace(old, new, 1)
    sentence_variants = (SENTENCE_SOURCE, SENTENCE_SOURCE_WITH_LOCALIZED_FALLBACK)
    matches = [(variant, source.count(variant)) for variant in sentence_variants]
    total = sum(count for _variant, count in matches)
    if total != 1:
        raise KoreanCodeError(
            f"sentence_for source unit must occur exactly once; found {total}"
        )
    if SENTENCE_TARGET in source:
        raise KoreanCodeError("sentence_for target unit is already present")
    matched_source = next(variant for variant, count in matches if count == 1)
    source = source.replace(matched_source, SENTENCE_TARGET, 1)
    return source


def patch_hint_text(source: str) -> str:
    count = source.count(HINT_ANSWER_PERIOD_SOURCE)
    if count != 1:
        raise KoreanCodeError(
            "hint answer-period source unit must occur exactly once; "
            f"found {count}"
        )
    if HINT_ANSWER_PERIOD_TARGET in source:
        raise KoreanCodeError("hint answer-period target unit is already present")
    return source.replace(HINT_ANSWER_PERIOD_SOURCE, HINT_ANSWER_PERIOD_TARGET, 1)


def _patch_generated_file(
    source_path: Path | str,
    output_path: Path | str,
    patcher: Callable[[str], str],
) -> dict[str, str]:
    source_path = Path(source_path).resolve()
    output_path = Path(output_path).resolve()
    if source_path == output_path:
        raise KoreanCodeError("source and output must be different paths")
    if output_path.exists():
        raise KoreanCodeError(f"refusing to overwrite output: {output_path}")
    raw = source_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise KoreanCodeError("UTF-8 BOM is not supported")
    text = raw.decode("utf-8")
    patched = patcher(text).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(patched)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output_path)
        except FileExistsError as exc:
            raise KoreanCodeError(f"refusing to overwrite output: {output_path}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source": str(source_path),
        "output": str(output_path),
        "source_sha256": sha256_bytes(raw),
        "output_sha256": sha256_bytes(patched),
    }


def patch_generated_file(source_path: Path | str, output_path: Path | str) -> dict[str, str]:
    """Patch the generated ``data/game_data.gd`` copy."""

    return _patch_generated_file(source_path, output_path, patch_text)


def patch_hint_generated_file(
    source_path: Path | str, output_path: Path | str,
) -> dict[str, str]:
    """Patch the generated ``data/hint.gd`` answer-period expression."""

    return _patch_generated_file(source_path, output_path, patch_hint_text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = patch_generated_file(args.source, args.output)
    except (OSError, UnicodeError, KoreanCodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
