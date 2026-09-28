#!/usr/bin/env python3
"""Reads text content from a path under a fixed base directory.

Read-side counterpart to write-backup-copy - used wherever a workflow
needs the actual bytes of a local backup copy (restore, diff) instead of
just its metadata.

Decorator-defined params (read-backup-copy-input) arrive as CLI flags:

    main.py --path=shared-lab-ios/2026-09-24T22-08-58.cfg

`path` is always joined under BASE_DIR - no absolute paths, no traversal
above the base directory.
"""
import argparse
import json
import os
import sys

BASE_DIR = "/opt/backup-copies"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read a local backup copy for IAG5")
    parser.add_argument("--path", required=True, help="Relative path under the base directory")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    rel_path = args.path.lstrip("/")
    full_path = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    if not (full_path == os.path.normpath(BASE_DIR) or full_path.startswith(os.path.normpath(BASE_DIR) + os.sep)):
        print(json.dumps({"error": "path escapes base directory"}))
        return 1

    try:
        with open(full_path, "r") as f:
            content = f.read()
    except OSError as e:
        print(json.dumps({"error": str(e)}))
        return 1

    print(json.dumps({"response": content}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
