#!/usr/bin/env python3
"""Lists all local backup copies under a fixed base directory, grouped by device.

Walks the base directory and groups files by device prefix (vendor/device
for multi-segment CLI paths like cisco-ios/shared-lab-ios, or the top-level
segment for single-segment device types like panorama), reporting size and
modified time for each file.

No arguments - always lists the whole base directory.
"""
import datetime
import json
import os
import sys

BASE_DIR = "/opt/backup-copies"
VENDOR_PREFIXES = ("cisco-ios", "cisco-nxos")


def classify(parts: list) -> str:
    if parts[0] in VENDOR_PREFIXES and len(parts) >= 3:
        return parts[0] + "/" + parts[1]
    return parts[0]


def main() -> int:
    devices = {}
    for root, _dirs, files in os.walk(BASE_DIR):
        for fname in files:
            if fname.startswith("."):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, BASE_DIR)
            prefix = classify(rel.split(os.sep))
            mtime = os.path.getmtime(full)
            mtime_iso = datetime.datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            size = os.path.getsize(full)
            devices.setdefault(prefix, {"count": 0, "files": []})
            devices[prefix]["count"] += 1
            devices[prefix]["files"].append({"path": rel, "size": size, "mtime": mtime_iso})

    print(json.dumps({"devices": devices}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
