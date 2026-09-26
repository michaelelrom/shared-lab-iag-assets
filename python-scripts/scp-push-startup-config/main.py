#!/usr/bin/env python3
"""Pushes a config file directly to a Cisco device's startup-config via SCP.

Cisco IOS/NX-OS's SCP server (ip scp server enable) accepts a direct write
to the virtual "startup-config" destination — no copy command, no
confirmation prompt, no temp file left on flash. Byte-exact NVRAM write.

Host/user/password are resolved from the inventory node's attributes,
piped in by IAG5 via stdin when the calling runService task uses
inventory targeting — the calling workflow never sees or passes the
device credentials. Only the (non-sensitive) config content is a decorator
input, matching how write-backup-copy takes --content.

Decorator-defined params (scp-push-startup-config-input) arrive as CLI flags:

    main.py --content="hostname foo\n..."

--host/--user/--password CLI flags exist only for local testing without
piping inventory JSON on stdin; in production the calling workflow never
sets them, and inventory attributes are used instead.
"""
import argparse
import io
import json
import sys

import paramiko
from scp import SCPClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SCP a config file to a device's startup-config")
    parser.add_argument("--content", required=True, help="Config text to write to startup-config")
    parser.add_argument("--host", default=None, help="Local testing only - device host override")
    parser.add_argument("--user", default=None, help="Local testing only - device user override")
    parser.add_argument("--password", default=None, help="Local testing only - device password override")
    parser.add_argument("--port", type=int, default=None, help="Local testing only - SSH port override")
    return parser


def _read_stdin_inventory():
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "inventory_nodes" not in data:
        return None
    nodes = data.get("inventory_nodes") or []
    return nodes[0] if nodes else None


def _resolve_connection(args, node) -> dict:
    attrs = ((node or {}).get("attributes") or {})

    def pick(cli_val, attr_key, default=None):
        if cli_val is not None:
            return cli_val
        v = attrs.get(attr_key)
        return v if v is not None else default

    host = pick(args.host, "itential_host")
    user = pick(args.user, "itential_user")
    password = pick(args.password, "itential_password")
    port = int(pick(args.port, "itential_port") or 22)

    missing = [name for name, val in [("host", host), ("user", user), ("password", password)] if not val]
    if missing:
        raise SystemExit(
            f"missing required connection field(s): {', '.join(missing)} "
            f"(provide via --{missing[0]} for local testing, or inventory attribute itential_{missing[0]})"
        )

    return {"host": host, "user": user, "password": password, "port": port}


def push_startup_config(conn: dict, content: str) -> dict:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            conn["host"], port=conn["port"], username=conn["user"], password=conn["password"],
            timeout=15, look_for_keys=False, allow_agent=False,
        )
        client = SCPClient(ssh.get_transport(), socket_timeout=30)
        try:
            encoded = content.encode("utf-8")
            client.putfo(io.BytesIO(encoded), "startup-config", size=len(encoded))
            return {"success": True, "host": conn["host"], "bytes_written": len(encoded)}
        finally:
            client.close()
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}
    finally:
        ssh.close()


def main() -> int:
    args = build_parser().parse_args()
    node = _read_stdin_inventory()
    conn = _resolve_connection(args, node)
    result = push_startup_config(conn, args.content)
    print(json.dumps(result))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
