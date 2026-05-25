#!/usr/bin/env python3
"""Junos NETCONF service for IAG5.

Replaces CLI-based netmiko/scrapli flows for destructive Junos operations
that drop the SSH session mid-command (software add, reboot, halt).
NETCONF is RPC-over-SSH (port 830) — operations return cleanly even when
the underlying device restarts daemons.

Actions: is-alive, run-command, get-config, send-command, reboot

Connection parameters (host, port, user, password, timeout, lock options) are
read from stdin as JSON in the gateway5 InventoryInfo format:

    {"inventory_nodes": [{"name": "...", "attributes": {
        "itential_host": "...", "itential_user": "...",
        "itential_password": "...",
        "itential_driver_options": {"netconf": {"port": 830, ...}}
    }}]}

CLI flags for those connection params still exist and override stdin values
when both are present — useful for local testing without piping JSON.
"""

import argparse
import json
import os
import sys
import time

from ncclient import manager
from ncclient.operations.rpc import RPCError
from ncclient.transport.errors import AuthenticationError, SSHError


def _connect(conn):
    return manager.connect(
        host=conn["host"],
        port=conn["port"],
        username=conn["user"],
        password=conn["password"],
        hostkey_verify=False,
        device_params={"name": "junos"},
        timeout=conn["timeout"],
        allow_agent=False,
        look_for_keys=False,
    )


def is_alive(conn, args) -> dict:
    try:
        with _connect(conn) as m:
            return {
                "success": True,
                "alive": bool(m.connected),
                "session_id": m.session_id,
                "host": conn["host"],
            }
    except (AuthenticationError, SSHError) as e:
        return {"success": False, "alive": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}
    except Exception as e:
        return {"success": False, "alive": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}


def run_command(conn, args) -> dict:
    if not args.command:
        return {"success": False, "host": conn["host"], "error": "command is required for action=run-command"}
    results = []
    try:
        with _connect(conn) as m:
            for cmd in args.command:
                try:
                    rpc_reply = m.command(command=cmd, format="text")
                    output_nodes = rpc_reply.xpath(".//output")
                    output = output_nodes[0].text if output_nodes else rpc_reply.xml
                    results.append({"command": cmd, "output": output or "", "success": True})
                except RPCError as e:
                    results.append({"command": cmd, "output": "", "success": False, "error": str(e)})
        return {"success": all(r["success"] for r in results), "host": conn["host"], "results": results}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__, "results": results}


def get_config(conn, args) -> dict:
    try:
        with _connect(conn) as m:
            reply = m.get_config(source=args.source, filter=("subtree", args.filter) if args.filter else None)
            return {"success": True, "host": conn["host"], "source": args.source, "config": reply.data_xml}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}


def _acquire_candidate_lock(m, timeout: int, poll_interval: float) -> float:
    deadline = time.monotonic() + max(timeout, 0)
    start = time.monotonic()
    while True:
        try:
            m.lock(target="candidate")
            return time.monotonic() - start
        except RPCError as e:
            msg = str(e).lower()
            transient = "lock-denied" in msg or "lock denied" in msg or "in-use" in msg or "in use" in msg
            if not transient or timeout == 0 or time.monotonic() >= deadline:
                raise
            time.sleep(poll_interval)


def send_command(conn, args) -> dict:
    if not args.command:
        return {"success": False, "host": conn["host"], "error": "command is required for action=send-command"}
    try:
        with _connect(conn) as m:
            lock_wait = _acquire_candidate_lock(m, conn["lock_timeout"], conn["lock_poll_interval"])
            try:
                config_text = "\n".join(args.command)
                m.load_configuration(action="set", config=config_text)
                commit_reply = m.commit()
                return {
                    "success": True,
                    "host": conn["host"],
                    "commands": args.command,
                    "lock_wait_seconds": round(lock_wait, 2),
                    "commit": commit_reply.xml,
                }
            except Exception as inner:
                try:
                    m.discard_changes()
                except Exception:
                    pass
                return {
                    "success": False,
                    "host": conn["host"],
                    "commands": args.command,
                    "error": str(inner),
                    "error_type": type(inner).__name__,
                }
            finally:
                try:
                    m.unlock(target="candidate")
                except Exception:
                    pass
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}


def reboot(conn, args) -> dict:
    try:
        with _connect(conn) as m:
            rpc = "<request-reboot>"
            if args.at:
                rpc += f"<at>{args.at}</at>"
            if args.message:
                rpc += f"<message>{args.message}</message>"
            rpc += "</request-reboot>"
            reply = m.rpc(rpc)
            return {"success": True, "host": conn["host"], "at": args.at, "response": reply.xml}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}


_DISPATCH = {
    "is-alive": is_alive,
    "run-command": run_command,
    "get-config": get_config,
    "send-command": send_command,
    "reboot": reboot,
}


def _read_stdin_inventory():
    """Read the InventoryInfo JSON gateway5 pipes to stdin. Returns None if no data."""
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
    if not nodes:
        return None
    return nodes[0]


def _resolve_connection(args, node):
    """Merge connection params: CLI args win over inventory attributes."""
    attrs = (node or {}).get("attributes", {}) or {}
    netconf_opts = (attrs.get("itential_driver_options") or {}).get("netconf") or {}

    def pick(cli_val, *attr_paths, default=None):
        if cli_val is not None:
            return cli_val
        for path in attr_paths:
            cursor = attrs
            for k in path:
                if not isinstance(cursor, dict):
                    cursor = None
                    break
                cursor = cursor.get(k)
            if cursor is not None:
                return cursor
        return default

    host = pick(args.host, ("itential_host",))
    user = pick(args.user, ("itential_user",))
    password = pick(args.password, ("itential_password",))
    port = pick(args.port, ("itential_driver_options", "netconf", "port"), default=830)
    timeout = pick(args.timeout, ("itential_driver_options", "netconf", "timeout"), default=30)
    lock_timeout = pick(args.lock_timeout, ("itential_driver_options", "netconf", "lock_timeout"), default=30)
    lock_poll_interval = pick(args.lock_poll_interval, ("itential_driver_options", "netconf", "lock_poll_interval"), default=2.0)

    missing = [name for name, val in [("host", host), ("user", user), ("password", password)] if not val]
    if missing:
        raise SystemExit(
            f"missing required connection field(s): {', '.join(missing)} "
            f"(provide via --{missing[0]} or inventory attribute itential_{missing[0]})"
        )

    return {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "timeout": int(timeout),
        "lock_timeout": int(lock_timeout),
        "lock_poll_interval": float(lock_poll_interval),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Junos NETCONF operations for IAG5")
    parser.add_argument("--op", default=os.environ.get("JUNOS_OP"),
                        choices=sorted(_DISPATCH.keys()),
                        help="Operation to perform. Defaults to JUNOS_OP env var.")

    parser.add_argument("--host", default=None, help="Override the inventory's itential_host")
    parser.add_argument("--port", type=int, default=None, help="Override the inventory's netconf port")
    parser.add_argument("--user", default=None, help="Override the inventory's itential_user")
    parser.add_argument("--password", default=None, help="Override the inventory's itential_password")
    parser.add_argument("--timeout", type=int, default=None, help="Override the netconf session timeout")
    parser.add_argument("--lock-timeout", type=int, default=None,
                        help="Override candidate-lock wait for send-command (0 = no wait)")
    parser.add_argument("--lock-poll-interval", type=float, default=None,
                        help="Override candidate-lock retry interval")

    parser.add_argument("--command", action="append", default=None,
                        help="Operational or set-style command (repeatable; multi-line values are split into separate commands)")
    parser.add_argument("--source", default=None,
                        help="Datastore for get-config (running|candidate); defaults to running. Empty string treated as unset.")
    parser.add_argument("--filter", default=None, help="Optional subtree filter for get-config")
    parser.add_argument("--at", default=None, help="Junos time spec for reboot (e.g. '+5')")
    parser.add_argument("--message", default=None, help="Optional broadcast message for reboot")

    return parser


def _normalize_args(args):
    """The IM/MOP framework injects empty-string CLI args for every decorator-defined
    optional string field (e.g. --filter= --source= --at=). Normalize those to None
    so downstream code can treat them as 'unset'. Also splits multi-line --command
    values into separate commands — the MOP command-template framework joins
    multiple template lines with newlines into one --command value."""
    for attr in ("source", "filter", "at", "message", "host", "user", "password"):
        if getattr(args, attr, None) == "":
            setattr(args, attr, None)

    if args.command:
        split = []
        for raw in args.command:
            if raw is None:
                continue
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    split.append(line)
        args.command = split or None

    if args.source is None:
        args.source = "running"
    if args.source not in ("running", "candidate"):
        raise SystemExit(f"--source must be 'running' or 'candidate', got {args.source!r}")


def main() -> int:
    args = build_parser().parse_args()
    if not args.op:
        raise SystemExit("--op flag or JUNOS_OP env var must be set")
    _normalize_args(args)
    node = _read_stdin_inventory()
    conn = _resolve_connection(args, node)
    result = _DISPATCH[args.op](conn, args)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
