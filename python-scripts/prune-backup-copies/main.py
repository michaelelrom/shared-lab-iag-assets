#!/usr/bin/env python3
"""Prunes local backup copies under a fixed base directory, keeping only the N most recent per device.

Groups files by device prefix (same classification as list-backup-copies),
sorts each group by the timestamp embedded in the filename, and removes
everything beyond --keep-count. Supports a dry-run mode that reports what
would be deleted without touching any files.

Decorator-defined params (prune-backup-copies-input) arrive as CLI flags:

    main.py --keep-count=10 --dry-run=true
"""
import argparse
import json
import os
import sys

BASE_DIR = "/opt/backup-copies"
VENDOR_PREFIXES = ("cisco-ios", "cisco-nxos")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prune local backup copies for IAG5")
    parser.add_argument("--keep-count", type=int, required=True, help="Number of most-recent backups to keep per device")
    parser.add_argument("--dry-run", type=str, default="true", help="If true, report candidates without deleting")
    return parser


def classify(parts: list) -> str:
    if parts[0] in VENDOR_PREFIXES and len(parts) >= 3:
        return parts[0] + "/" + parts[1]
    return parts[0] if len(parts) > 1 else "(root)"


def extract_ts(rel_path: str) -> str:
    base_name = rel_path.rsplit("/", 1)[-1]
    return base_name.rsplit(".", 1)[0]


def main() -> int:
    args = build_parser().parse_args()
    keep_count = args.keep_count
    dry_run = args.dry_run.strip().lower() in ("true", "1", "yes")

    devices = {}
    for root_dir, _dirs, files in os.walk(BASE_DIR):
        for fname in files:
            if fname.startswith("."):
                continue
            full = os.path.join(root_dir, fname)
            rel = os.path.relpath(full, BASE_DIR)
            prefix = classify(rel.split(os.sep))
            devices.setdefault(prefix, []).append((rel, full))

    candidates = []
    actually_deleted = []
    summary = {}
    for prefix, entries in devices.items():
        entries_sorted = sorted(entries, key=lambda e: extract_ts(e[0]), reverse=True)
        keep = entries_sorted[:keep_count]
        remove = entries_sorted[keep_count:]
        for rel, full in remove:
            candidates.append(rel)
            if not dry_run:
                os.remove(full)
                actually_deleted.append(rel)
        summary[prefix] = {"total": len(entries), "kept": len(keep), "candidates_for_deletion": len(remove)}

    message = (
        f"DRY RUN - no files were actually deleted. {len(candidates)} file(s) identified as candidates."
        if dry_run
        else f"{len(actually_deleted)} file(s) actually deleted."
    )
    print(json.dumps({
        "message": message,
        "dry_run": dry_run,
        "candidates_for_deletion": candidates,
        "candidate_count": len(candidates),
        "actually_deleted": actually_deleted,
        "actually_deleted_count": len(actually_deleted),
        "summary": summary
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
