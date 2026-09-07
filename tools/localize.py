#!/usr/bin/env python3
"""Command-line entry point for deterministic localization resources."""

from __future__ import annotations

try:
    from tools.segments import main
except ModuleNotFoundError:  # Direct execution: python tools/localize.py
    from segments import main


if __name__ == "__main__":
    raise SystemExit(main())
