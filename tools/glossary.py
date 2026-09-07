#!/usr/bin/env python3
"""Validate exact approved source-to-target glossary mappings."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate glossary source: {key!r}")
        result[key] = value
    return result


def load_glossary(path: str | Path) -> dict[str, str]:
    source = Path(path)
    value = json.loads(
        source.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
    )
    if not isinstance(value, dict) or not value:
        raise ValueError("exact glossary must be a nonempty JSON object")
    if any(
        not isinstance(key, str) or not key
        or not isinstance(target, str) or not target
        for key, target in value.items()
    ):
        raise ValueError("exact glossary keys and targets must be nonempty strings")
    return value


def read_rows(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"segment_id", "source_text", "target_text"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("segments TSV is missing required fields")
        return list(reader)


def validate_exact(
    rows: Iterable[Mapping[str, str]], glossary: Mapping[str, str],
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    checked = 0
    for row in rows:
        source = row.get("source_text", "")
        target = row.get("target_text", "")
        expected = glossary.get(source)
        if expected is None or not target:
            continue
        checked += 1
        if target != expected:
            issues.append({
                "segment_id": row.get("segment_id", ""),
                "source_text": source,
                "expected_target": expected,
                "actual_target": target,
            })
    return {
        "ok": not issues,
        "error_count": len(issues),
        "checked_exact_rows": checked,
        "glossary_entry_count": len(glossary),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("segments", type=Path)
    parser.add_argument("--glossary", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate_exact(read_rows(args.segments), load_glossary(args.glossary))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result = {"ok": False, "error_count": 1, "issues": [{"error": str(exc)}]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
