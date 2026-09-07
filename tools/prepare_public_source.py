#!/usr/bin/env python3
"""Join public translations to locally recovered, hash-matched source text."""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from tools import segments
except ModuleNotFoundError:
    import segments


def prepare(translations: Path, recovered: Path, output: Path) -> int:
    _, rows = segments.read_tsv(translations)
    _, extracted = segments.rows_from_resources(segments.load_resources(recovered))
    original = {(r['source_path'], r['source_key']): r for r in extracted}
    if len(original) != len(extracted) or len(rows) != len(extracted):
        raise ValueError('Source segment count or uniqueness mismatch')
    seen = set()
    for row in rows:
        key = (row['source_path'], row['source_key'])
        if key in seen or key not in original:
            raise ValueError('Duplicate or missing source key')
        seen.add(key)
        source = original[key]
        if row['source_hash'] != source['source_hash']:
            raise ValueError(f"Source hash mismatch: {row['segment_id']}")
        for field in ('source_text', 'context_before', 'context_after', 'developer_comment'):
            row[field] = source[field]
    # Never replace a source or existing output, including through a symlink.
    output = output.absolute()
    if output.is_symlink() or output.exists() or output.resolve().is_relative_to(recovered.resolve()):
        raise ValueError('Output must be a fresh path outside recovered source')
    segments.write_tsv_atomic(output, rows, segments.SEGMENT_COLUMNS)
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--translations', type=Path, required=True)
    parser.add_argument('--recovered-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(f"Prepared {prepare(args.translations, args.recovered_dir, args.output)} segments")
