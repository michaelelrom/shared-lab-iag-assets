#!/usr/bin/env python3
"""Writes arbitrary text content to a path under a fixed base directory.

Used as a local "second copy" landing spot for config backups that
otherwise go to S3 — on the shared lab this points at a directory on the
IAG5 host; in production the same base directory would be a mount to a
customer-owned server/share instead.

Decorator-defined params (write-backup-copy-input) arrive as CLI flags:

    main.py --path=shared-lab-ios/2026-09-24T22-08-58.cfg --content="..."

`path` is always joined under BASE_DIR — no absolute paths, no traversal
above the base directory.
"""
import argparse
import json
import os
import sys

BASE_DIR = "/opt/backup-copies"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write a local backup copy for IAG5")
    parser.add_argument("--path", required=True, help="Relative path under the base directory")
    parser.add_argument("--content", required=True, help="Text content to write")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    rel_path = args.path.lstrip("/")
    full_path = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    if not (full_path == os.path.normpath(BASE_DIR) or full_path.startswith(os.path.normpath(BASE_DIR) + os.sep)):
        print(json.dumps({"error": "path escapes base directory"}))
        return 1

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(args.content)

    print(json.dumps({"written_to": full_path}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
