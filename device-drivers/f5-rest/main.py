#!/usr/bin/env python3
"""f5-rest — F5 BIG-IP iControl REST driver for IAG5.

Implements the IAG5 device broker contracts via the F5 iControl REST API:
is-alive, run-command, get-config, send-command, and set-config.

Authentication uses F5 token-based auth — a fresh token is obtained per
invocation via POST /mgmt/shared/authn/login.

Per-device configuration in Inventory Manager attributes:
  itential_host      — F5 management IP or hostname
  itential_port      — HTTPS port (default: 443)
  itential_user      — F5 username
  itential_password  — F5 password (resolved by IAG5 — no vault logic in this driver)

  itential_driver_options.f5-rest:
    login_provider     — F5 auth provider name (default: tmos)
    verify_ssl         — verify TLS certificate (default: true)
    timeout            — request timeout seconds (default: 30)
    get_config_command — bash command used for get-config
                         (default: tmsh list all-properties)
    save_config        — save sys config after set-config (default: true)

The F5_REST_OP environment variable selects the operation (set by IAG5 per-service).
CLI flags override stdin inventory values for local testing.
"""

import argparse
import json
import os
import sys

import requests
from requests.exceptions import HTTPError


# ---------------------------------------------------------------------------
# iControl REST helpers
# ---------------------------------------------------------------------------

def _token(conn: dict) -> str:
    r = requests.post(
        f"https://{conn['host']}:{conn['port']}/mgmt/shared/authn/login",
        json={
            "username": conn["user"],
            "password": conn["password"],
            "loginProviderName": conn["login_provider"],
        },
        verify=conn["verify_ssl"],
        timeout=conn["timeout"],
    )
    r.raise_for_status()
    return r.json()["token"]["token"]


def _headers(token: str) -> dict:
    return {"X-F5-Auth-Token": token, "Content-Type": "application/json"}


def _url(conn: dict, route: str) -> str:
    return f"https://{conn['host']}:{conn['port']}/{route.lstrip('/')}"


def _bash(conn: dict, token: str, cmd: str) -> str:
    r = requests.post(
        _url(conn, "/mgmt/tm/util/bash"),
        headers=_headers(token),
        json={"command": "run", "utilCmdArgs": f"-c '{cmd}'"},
        verify=conn["verify_ssl"],
        timeout=conn["timeout"],
    )
    r.raise_for_status()
    return r.json().get("commandResult", "")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def is_alive(conn: dict, args) -> dict:
    try:
        token = _token(conn)
        r = requests.get(
            _url(conn, "/mgmt/tm/sys/version"),
            headers=_headers(token),
            verify=conn["verify_ssl"],
            timeout=conn["timeout"],
        )
        return {"success": True, "alive": r.status_code == 200, "host": conn["host"]}
    except Exception as e:
        return {"success": False, "alive": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def run_command(conn: dict, args) -> dict:
    if not args.command:
        return {"success": False, "host": conn["host"],
                "error": "command is required for action=run-command"}
    results = []
    try:
        token = _token(conn)
        for cmd in args.command:
            try:
                output = _bash(conn, token, cmd)
                results.append({"command": cmd, "output": output, "success": True})
            except Exception as e:
                results.append({"command": cmd, "output": "", "success": False, "error": str(e)})
        return {"success": all(r["success"] for r in results),
                "host": conn["host"], "results": results}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e),
                "error_type": type(e).__name__, "results": results}


def get_config(conn: dict, args) -> dict:
    cmd = conn.get("get_config_command") or "tmsh list all-properties"
    try:
        token = _token(conn)
        output = _bash(conn, token, cmd)
        return {"success": True, "host": conn["host"],
                "config_format": "text", "config": output}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def send_command(conn: dict, args) -> dict:
    """Generic iControl REST passthrough — caller supplies verb, route, body."""
    if not args.verb or not args.route:
        return {"success": False, "host": conn["host"],
                "error": "verb and route are required for action=send-command"}
    try:
        token = _token(conn)
        body = None
        if args.body:
            try:
                body = json.loads(args.body) if isinstance(args.body, str) else args.body
            except (json.JSONDecodeError, TypeError):
                body = args.body

        r = requests.request(
            args.verb.upper(),
            _url(conn, args.route),
            headers=_headers(token),
            json=body,
            verify=conn["verify_ssl"],
            timeout=conn["timeout"],
        )
        r.raise_for_status()

        try:
            response_data = r.json()
        except Exception:
            response_data = r.text

        return {"success": True, "host": conn["host"],
                "status_code": r.status_code, "response": response_data}

    except HTTPError as e:
        try:
            error_body = e.response.json()
        except Exception:
            error_body = e.response.text if e.response else str(e)
        return {"success": False, "host": conn["host"],
                "status_code": e.response.status_code if e.response else None,
                "error": str(e), "error_body": error_body}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def set_config(conn: dict, args) -> dict:
    """Config Manager remediation — execute changes array as bash/TMSH commands."""
    if not args.command:
        return {"success": False, "host": conn["host"], "error": "no config changes to apply"}
    try:
        token = _token(conn)
        for cmd in args.command:
            _bash(conn, token, cmd)
        if conn.get("save_config"):
            try:
                _bash(conn, token, "tmsh save sys config")
            except Exception:
                pass
        changes_list = getattr(args, "_changes_list", None)
        if changes_list:
            output = [
                {"result": True, "parents": c.get("parents", []),
                 "old": c.get("old", ""), "new": c.get("new", "")}
                for c in changes_list
            ]
        else:
            output = [{"result": True, "parents": [], "old": "", "new": cmd}
                      for cmd in args.command]
        return {"success": True, "host": conn["host"],
                "_changes_list": changes_list, "results": output}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


_DISPATCH = {
    "is-alive":     is_alive,
    "run-command":  run_command,
    "get-config":   get_config,
    "send-command": send_command,
    "set-config":   set_config,
}


# ---------------------------------------------------------------------------
# Inventory / stdin parsing
# ---------------------------------------------------------------------------

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
    driver_opts = dict((attrs.get("itential_driver_options") or {}).get("f5-rest") or {})

    def pick(cli_val, attr_key, default=None):
        if cli_val is not None:
            return cli_val
        v = attrs.get(attr_key)
        return v if v is not None else default

    host     = pick(args.host,     "itential_host")
    port     = int(pick(args.port, "itential_port", 443))
    user     = pick(args.user,     "itential_user")
    password = pick(args.password, "itential_password")

    get_config_cmd = driver_opts.pop("get_config_command", None) or "tmsh list all-properties"
    save           = str(driver_opts.pop("save_config", "true")).lower() not in ("false", "0", "no")
    login_provider = driver_opts.pop("login_provider", None) or "tmos"
    verify_ssl     = driver_opts.pop("verify_ssl", True)
    if isinstance(verify_ssl, str):
        verify_ssl = verify_ssl.lower() not in ("false", "0", "no")
    timeout = int(driver_opts.pop("timeout", 30) or 30)

    missing = [n for n, v in [("host", host), ("user", user), ("password", password)] if not v]
    if missing:
        raise SystemExit(
            f"missing required connection field(s): {', '.join(missing)} "
            f"(provide via --{missing[0]} or inventory attribute itential_{missing[0]})"
        )

    return {
        "host": host, "port": port, "user": user, "password": password,
        "login_provider": login_provider, "verify_ssl": verify_ssl,
        "timeout": timeout, "get_config_command": get_config_cmd,
        "save_config": save, "device_name": (node or {}).get("name") or host,
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="f5-rest: F5 BIG-IP iControl REST driver for IAG5"
    )
    parser.add_argument("--op", default=os.environ.get("F5_REST_OP"),
                        choices=list(_DISPATCH),
                        help="Operation to perform. Defaults to F5_REST_OP env var.")
    parser.add_argument("--host",     default=None)
    parser.add_argument("--port",     type=int, default=None)
    parser.add_argument("--user",     default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--timeout",  type=int, default=None)

    # run-command
    parser.add_argument("--command", action="append", default=None,
                        help="Bash/TMSH command to run (repeatable)")
    parser.add_argument("--commands", default=None,
                        help="JSON array of commands")

    # send-command (generic REST passthrough)
    parser.add_argument("--verb",  default=None,
                        help="HTTP verb for send-command (GET, POST, PUT, PATCH, DELETE)")
    parser.add_argument("--route", default=None,
                        help="iControl REST route (e.g. /mgmt/tm/ltm/virtual)")
    parser.add_argument("--body",  default=None,
                        help="JSON request body for send-command")

    # set-config / CM remediation
    parser.add_argument("--config",        default=None)
    parser.add_argument("--config_content","--config-content", dest="config_content", default=None)
    parser.add_argument("--changes",       default=None)
    parser.add_argument("--options",       default=None)

    return parser


def _extract_changes(changes_list: list) -> list:
    lines = []
    for c in changes_list:
        new_val = str(c.get("new", "") or "").strip()
        old_val = str(c.get("old", "") or "").strip()
        if new_val:
            lines.append(new_val)
        elif old_val:
            lines.append(f"no {old_val}" if not old_val.startswith("no ") else old_val)
    return lines


def _normalize_args(args):
    for attr in ("host", "user", "password", "config", "config_content",
                 "commands", "options", "verb", "route", "body"):
        if getattr(args, attr, None) == "":
            setattr(args, attr, None)

    if not hasattr(args, "_changes_list"):
        args._changes_list = None

    if args.commands and not args.command:
        try:
            cmds = json.loads(args.commands) if isinstance(args.commands, str) else args.commands
            args.command = [str(c) for c in (cmds if isinstance(cmds, list) else [cmds]) if str(c).strip()]
        except (json.JSONDecodeError, TypeError):
            args.command = [args.commands] if args.commands and args.commands.strip() else None
    args.commands = None

    for src_attr in ("config", "config_content"):
        src = getattr(args, src_attr, None)
        if src and not args.command:
            src = src.strip()
            if src.startswith("["):
                try:
                    changes_list = json.loads(src)
                    lines = _extract_changes(changes_list)
                    if lines:
                        args.command = lines
                        args._changes_list = changes_list
                except (json.JSONDecodeError, TypeError):
                    args.command = [src]
            else:
                args.command = [line.strip() for line in src.splitlines() if line.strip()]
        setattr(args, src_attr, None)

    if args.changes and not args.command:
        try:
            changes_list = json.loads(args.changes) if isinstance(args.changes, str) else args.changes
            lines = _extract_changes(changes_list)
            if lines:
                args.command = lines
                args._changes_list = changes_list
        except (json.JSONDecodeError, TypeError):
            pass
    args.changes = None

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


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_for_humans(result: dict, op: str) -> str:
    if op == "is-alive":
        return "true" if result.get("alive") else "false"

    if op == "run-command":
        results = result.get("results") or []
        if not results:
            return f"ERROR: {result.get('error', 'connection failed')}"
        if len(results) == 1:
            r = results[0]
            text = r.get("output", "")
            if not r.get("success"):
                text = f"ERROR: {r.get('error', 'unknown error')}\n{text}".rstrip()
            return text
        parts = []
        for r in results:
            parts.append(f"=== {r['command']} ===")
            if not r.get("success"):
                parts.append(f"ERROR: {r.get('error', 'unknown error')}")
            if r.get("output"):
                parts.append(r["output"])
        return "\n".join(parts)

    if op == "get-config":
        if not result.get("success"):
            return f"ERROR: {result.get('error', 'config retrieval failed')}"
        return result.get("config", "")

    if op == "set-config":
        if result.get("success"):
            changes_list = result.get("_changes_list")
            if changes_list:
                output = [
                    {"result": True, "parents": c.get("parents", []),
                     "old": c.get("old", ""), "new": c.get("new", "")}
                    for c in changes_list
                ]
            else:
                output = result.get("results", [])
            return json.dumps(output)
        print(result.get("error", "Configuration failed"), file=sys.stderr)
        return "[]"

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()
    if not args.op:
        raise SystemExit("--op flag or F5_REST_OP env var must be set")
    _normalize_args(args)
    node = _read_stdin_inventory()
    conn = _resolve_connection(args, node)
    result = _DISPATCH[args.op](conn, args)
    formatted = _format_for_humans(result, args.op)
    print(formatted, end="" if args.op == "is-alive" else "\n")
    if not result.get("success"):
        print(formatted, file=sys.stderr)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
