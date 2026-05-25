#!/usr/bin/env python3
"""Junos NETCONF service for IAG5.

Replaces CLI-based netmiko/scrapli flows for destructive Junos operations
that drop the SSH session mid-command (software add, reboot, halt).
NETCONF is RPC-over-SSH (port 830) — operations return cleanly even when
the underlying device restarts daemons.

Actions: is-alive, run-command, get-config, send-command, reboot

All inputs are top-level flags so iagctl's `--set key=value` translation
works (e.g. `--set action=run-command --set command="show version"`).
"""

import argparse
import json
import sys
import time

from ncclient import manager
from ncclient.operations.rpc import RPCError
from ncclient.transport.errors import AuthenticationError, SSHError


def _connect(host: str, port: int, user: str, password: str, timeout: int = 30):
    return manager.connect(
        host=host,
        port=port,
        username=user,
        password=password,
        hostkey_verify=False,
        device_params={"name": "junos"},
        timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )


def is_alive(args) -> dict:
    try:
        with _connect(args.host, args.port, args.user, args.password, timeout=args.timeout) as m:
            return {
                "success": True,
                "alive": bool(m.connected),
                "session_id": m.session_id,
                "host": args.host,
            }
    except (AuthenticationError, SSHError) as e:
        return {"success": False, "alive": False, "host": args.host, "error": str(e), "error_type": type(e).__name__}
    except Exception as e:
        return {"success": False, "alive": False, "host": args.host, "error": str(e), "error_type": type(e).__name__}


def run_command(args) -> dict:
    if not args.command:
        return {"success": False, "host": args.host, "error": "command is required for action=run-command"}
    results = []
    try:
        with _connect(args.host, args.port, args.user, args.password, timeout=args.timeout) as m:
            for cmd in args.command:
                try:
                    rpc_reply = m.command(command=cmd, format="text")
                    output_nodes = rpc_reply.xpath(".//output")
                    output = output_nodes[0].text if output_nodes else rpc_reply.xml
                    results.append({"command": cmd, "output": output or "", "success": True})
                except RPCError as e:
                    results.append({"command": cmd, "output": "", "success": False, "error": str(e)})
        return {"success": all(r["success"] for r in results), "host": args.host, "results": results}
    except Exception as e:
        return {"success": False, "host": args.host, "error": str(e), "error_type": type(e).__name__, "results": results}


def get_config(args) -> dict:
    try:
        with _connect(args.host, args.port, args.user, args.password, timeout=args.timeout) as m:
            reply = m.get_config(source=args.source, filter=("subtree", args.filter) if args.filter else None)
            return {"success": True, "host": args.host, "source": args.source, "config": reply.data_xml}
    except Exception as e:
        return {"success": False, "host": args.host, "error": str(e), "error_type": type(e).__name__}


def _acquire_candidate_lock(m, timeout: int, poll_interval: float) -> float:
    """Lock the candidate datastore, retrying on lock-denied up to `timeout` seconds.

    Returns elapsed seconds waited. Raises the last RPCError if timeout expires.
    Only lock-denied / in-use are retried; other RPC failures bubble immediately.
    """
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


def send_command(args) -> dict:
    if not args.command:
        return {"success": False, "host": args.host, "error": "command is required for action=send-command"}
    try:
        with _connect(args.host, args.port, args.user, args.password, timeout=args.timeout) as m:
            lock_wait = _acquire_candidate_lock(m, args.lock_timeout, args.lock_poll_interval)
            try:
                config_text = "\n".join(args.command)
                m.load_configuration(action="set", config=config_text)
                commit_reply = m.commit()
                return {
                    "success": True,
                    "host": args.host,
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
                    "host": args.host,
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
        return {"success": False, "host": args.host, "error": str(e), "error_type": type(e).__name__}


def reboot(args) -> dict:
    try:
        with _connect(args.host, args.port, args.user, args.password, timeout=args.timeout) as m:
            rpc = "<request-reboot>"
            if args.at:
                rpc += f"<at>{args.at}</at>"
            if args.message:
                rpc += f"<message>{args.message}</message>"
            rpc += "</request-reboot>"
            reply = m.rpc(rpc)
            return {"success": True, "host": args.host, "at": args.at, "response": reply.xml}
    except Exception as e:
        return {"success": False, "host": args.host, "error": str(e), "error_type": type(e).__name__}


_DISPATCH = {
    "is-alive": is_alive,
    "run-command": run_command,
    "get-config": get_config,
    "send-command": send_command,
    "reboot": reboot,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Junos NETCONF operations for IAG5")
    parser.add_argument("--action", required=True, choices=sorted(_DISPATCH.keys()))
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=830)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--timeout", type=int, default=30)

    parser.add_argument("--command", action="append", default=None,
                        help="Operational or set-style command (repeatable). Required for run-command and send-command.")
    parser.add_argument("--source", default="running", choices=["running", "candidate"],
                        help="Datastore source for get-config")
    parser.add_argument("--filter", default=None, help="Optional subtree filter for get-config")
    parser.add_argument("--lock-timeout", type=int, default=30,
                        help="Max seconds to wait for the candidate lock in send-command (0 = no wait)")
    parser.add_argument("--lock-poll-interval", type=float, default=2.0,
                        help="Seconds between candidate-lock retries in send-command")
    parser.add_argument("--at", default=None,
                        help="Junos time spec for reboot: '+5', '23:30'. Omit for immediate.")
    parser.add_argument("--message", default=None, help="Optional broadcast message for reboot")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = _DISPATCH[args.action](args)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
