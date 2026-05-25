#!/usr/bin/env python3
"""Trivial smoke-test script invoked by IAG5."""
import os
import platform
import sys


def main() -> int:
    print("python test ok")
    print(f"python: {sys.version.split()[0]} ({platform.machine()})")
    print(f"host:   {platform.node()}")
    print(f"cwd:    {os.getcwd()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
