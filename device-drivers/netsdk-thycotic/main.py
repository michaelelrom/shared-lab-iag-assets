#!/usr/bin/env python3
"""netsdk-thycotic — netmiko/scrapli device driver with Delinea Thycotic Secret Server.

Wraps netmiko or scrapli for CLI-based device operations (matching netsdk's dual-backend
support). Resolves device passwords from Delinea Thycotic Secret Server instead of
storing them in inventory attributes. Implements the IAG5 device broker contracts:
is-alive, run-command, get-config, set-config, send-command, send-config.

Thycotic server credentials are set once on the IAG5 host as environment variables:
  THYCOTIC_BASE_URL   — e.g. https://vault.example.com/SecretServer  (no trailing slash)
  THYCOTIC_USERNAME   — Thycotic service account username
  THYCOTIC_PASSWORD   — Thycotic service account password
  THYCOTIC_DOMAIN     — (optional) Active Directory domain for the service account

Per-device configuration in Inventory Manager attributes:
  itential_host      — device IP or hostname
  itential_port      — SSH port (default: 22)
  itential_driver    — transport backend: netmiko (default) or scrapli
  itential_platform  — device platform: netmiko device_type or scrapli platform
                       (e.g. cisco_ios, arista_eos, junos). Common netmiko names are
                       automatically mapped to scrapli equivalents when using scrapli.
  itential_user      — device username (overrides the Username field from the vault secret)
  itential_password  — "$SECRET:SecretName" or "$SECRET:FolderPath/SecretName" to resolve
                       from Thycotic at runtime, or a literal password as fallback

  itential_driver_options.{driver}  — passed through wholesale as **kwargs to netmiko's
                                       ConnectHandler or scrapli's Scrapli. Any option the
                                       library accepts is valid here. Two keys are popped
                                       before the dict reaches the library:
    get_config_command — show command used for get-config (default: show running-config)
    save_config        — true/false, save config after push operations (default: true)

CLI flags override stdin inventory values — useful for local testing without piping JSON.
The NETSDK_OP environment variable selects the operation (set by IAG5 per-service).
"""

import argparse
import json
import os
import sys
from contextlib import contextmanager

import requests


# ---------------------------------------------------------------------------
# Thycotic Secret Server
# ---------------------------------------------------------------------------

def _thycotic_token(cfg: dict) -> str:
    base = cfg["base_url"]
    data = {
        "grant_type": "password",
        "username": cfg["username"],
        "password": cfg["password"],
    }
    if cfg.get("domain"):
        data["domain"] = cfg["domain"]
    r = requests.post(
        f"{base}/oauth2/token",
        data=data,
        timeout=15,
        verify=cfg.get("verify_ssl", True),
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _thycotic_secret(reference: str, cfg: dict) -> dict:
    """Fetch a Thycotic secret by name reference and return {username, password}.

    reference formats:
      "SecretName"            — search by name, must match exactly one secret
      "FolderPath/SecretName" — scoped to a folder, matched against folderPath
      "42"                    — integer string, fetched by ID directly (URL fallback)
    """
    token = _thycotic_token(cfg)
    base = cfg["base_url"]
    verify = cfg.get("verify_ssl", True)
    headers = {"Authorization": f"Bearer {token}"}

    # Integer string → fetch by ID directly (no search needed)
    if reference.strip().isdigit():
        secret_id = int(reference.strip())
    else:
        # Parse optional folder prefix: "FolderPath/SecretName"
        if "/" in reference:
            folder_ref, secret_name = reference.rsplit("/", 1)
        else:
            folder_ref, secret_name = None, reference

        r = requests.get(
            f"{base}/api/v1/secrets",
            params={"filter.searchText": secret_name, "filter.includeInactive": "false"},
            headers=headers,
            timeout=15,
            verify=verify,
        )
        r.raise_for_status()
        records = r.json().get("records") or []

        # Exact name match (search is fuzzy)
        matches = [s for s in records if s.get("name", "").lower() == secret_name.lower()]

        # Narrow by folder if specified
        if folder_ref:
            matches = [
                s for s in matches
                if folder_ref.lower() in (s.get("folderPath") or "").lower().replace("\\", "/")
            ]

        if not matches:
            raise ValueError(f"no secret found matching {reference!r}")
        if len(matches) > 1:
            paths = [f"{s.get('folderPath','')}/{s.get('name','')}" for s in matches]
            raise ValueError(
                f"ambiguous reference {reference!r} — {len(matches)} matches: {paths}. "
                f"Add a folder prefix to disambiguate, e.g. 'Folder/{secret_name}'"
            )
        secret_id = matches[0]["id"]

    r = requests.get(
        f"{base}/api/v1/secrets/{secret_id}",
        headers=headers,
        timeout=15,
        verify=verify,
    )
    r.raise_for_status()

    result = {}
    for item in r.json().get("items") or []:
        if item.get("isPassword") or item.get("slug", "").lower() == "password" or item.get("fieldName", "").lower() == "password":
            result["password"] = item.get("itemValue", "")
        elif item.get("slug", "").lower() == "username" or item.get("fieldName", "").lower() == "username":
            result["username"] = item.get("itemValue", "")
    return result


def _thycotic_cfg_from_env() -> dict:
    base = os.environ.get("THYCOTIC_BASE_URL", "").rstrip("/")
    username = os.environ.get("THYCOTIC_USERNAME", "")
    password = os.environ.get("THYCOTIC_PASSWORD", "")
    domain = os.environ.get("THYCOTIC_DOMAIN", "")
    if not base or not username or not password:
        raise SystemExit(
            "Thycotic env vars not set. Required: THYCOTIC_BASE_URL, "
            "THYCOTIC_USERNAME, THYCOTIC_PASSWORD"
        )
    return {"base_url": base, "username": username, "password": password, "domain": domain or None}


# ---------------------------------------------------------------------------
# Dual-backend connection (netmiko or scrapli)
# ---------------------------------------------------------------------------

# Common platform name aliases: netmiko device_type → scrapli platform
# Lets users set netsdk.platform once and switch backends without renaming.
_PLATFORM_ALIASES = {
    "cisco_ios":  "cisco_iosxe",
    "cisco_xr":   "cisco_iosxr",
    "junos":      "juniper_junos",
}

# Platform → save command for scrapli (no save_config() method)
_SCRAPLI_SAVE_CMDS = {
    "cisco_iosxe":     "write memory",
    "cisco_iosxr":     "commit",
    "cisco_nxos":      "copy running-config startup-config",
    "arista_eos":      "write memory",
    "juniper_junos":   "commit",
    "paloalto_panos":  "commit",
}


class _Conn:
    """Normalized interface over a live netmiko or scrapli connection."""

    def __init__(self, backend: str, raw, platform: str):
        self._backend = backend
        self._raw = raw
        self._platform = platform

    def is_alive(self) -> bool:
        return bool(self._raw.is_alive())

    def send_command(self, cmd: str) -> str:
        result = self._raw.send_command(cmd)
        return result if isinstance(result, str) else result.result

    def send_config_set(self, lines: list) -> str:
        if self._backend == "netmiko":
            result = self._raw.send_config_set(lines)
            return result if isinstance(result, str) else str(result)
        else:
            multi = self._raw.send_configs(lines)
            return "\n".join(r.result for r in multi)

    def save_config(self):
        if self._backend == "netmiko":
            self._raw.save_config()
        else:
            platform = _PLATFORM_ALIASES.get(self._platform, self._platform)
            cmd = _SCRAPLI_SAVE_CMDS.get(platform)
            if cmd:
                self._raw.send_command(cmd)


@contextmanager
def _connect(conn: dict):
    backend = conn["driver"]
    platform = conn["platform"]
    driver_opts = conn["driver_opts"]  # passthrough dict — everything except our custom keys

    if backend == "netmiko":
        import netmiko as _netmiko
        raw = _netmiko.ConnectHandler(
            device_type=platform,
            host=conn["host"],
            port=conn["port"],
            username=conn["user"],
            password=conn["password"],
            **driver_opts,
        )
        try:
            yield _Conn("netmiko", raw, platform)
        finally:
            try:
                raw.disconnect()
            except Exception:
                pass

    elif backend == "scrapli":
        from scrapli import Scrapli
        scrapli_platform = _PLATFORM_ALIASES.get(platform, platform)
        raw = Scrapli(
            platform=scrapli_platform,
            host=conn["host"],
            port=conn["port"],
            auth_username=conn["user"],
            auth_password=conn["password"],
            **driver_opts,
        )
        raw.open()
        try:
            yield _Conn("scrapli", raw, scrapli_platform)
        finally:
            try:
                raw.close()
            except Exception:
                pass

    else:
        raise SystemExit(f"unsupported driver {backend!r}; use netmiko or scrapli")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def is_alive(conn: dict, args) -> dict:
    device_name = conn.get("device_name") or conn["host"]
    try:
        with _connect(conn) as net:
            alive = net.is_alive()
        return {"success": True, "alive": alive, "host": conn["host"], "device_name": device_name}
    except Exception as e:
        error_type = type(e).__name__
        if "auth" in error_type.lower() or "authentication" in str(e).lower():
            error_type = "AuthenticationError"
        return {"success": False, "alive": False, "host": conn["host"], "device_name": device_name,
                "error": str(e), "error_type": error_type}


def run_command(conn: dict, args) -> dict:
    if not args.command:
        return {"success": False, "host": conn["host"], "error": "command is required for action=run-command"}
    results = []
    try:
        with _connect(conn) as net:
            for cmd in args.command:
                try:
                    output = net.send_command(cmd)
                    results.append({"command": cmd, "output": output, "success": True})
                except Exception as e:
                    results.append({"command": cmd, "output": "", "success": False, "error": str(e)})
        return {"success": all(r["success"] for r in results), "host": conn["host"], "results": results}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e),
                "error_type": type(e).__name__, "results": results}


def get_config(conn: dict, args) -> dict:
    cmd = conn.get("get_config_command") or "show running-config"
    try:
        with _connect(conn) as net:
            config = net.send_command(cmd)
        return {"success": True, "host": conn["host"], "source": args.source or "running",
                "config_format": "text", "config": config}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e), "error_type": type(e).__name__}


def _push_config(conn: dict, config_lines: list, save: bool) -> dict:
    """Core config-push used by send-command, send-config, and set-config."""
    device_name = conn.get("device_name") or conn["host"]
    if not config_lines:
        return {"success": False, "host": conn["host"], "device_name": device_name,
                "error": "no config lines to apply"}
    try:
        with _connect(conn) as net:
            output = net.send_config_set(config_lines)
            if save:
                try:
                    net.save_config()
                except Exception:
                    pass  # save_config may not be supported on all platforms
        return {
            "success": True,
            "host": conn["host"],
            "device_name": device_name,
            "commands": config_lines,
            "output": output,
        }
    except Exception as e:
        return {"success": False, "host": conn["host"], "device_name": device_name,
                "commands": config_lines, "error": str(e), "error_type": type(e).__name__}


def send_command(conn: dict, args) -> dict:
    """Apply an array of config commands (workflow task — junos-netconf-send-command equivalent)."""
    device_name = conn.get("device_name") or conn["host"]
    if not args.command:
        return {"success": False, "host": conn["host"], "device_name": device_name,
                "error": "command is required for action=send-command"}
    save = conn.get("save_config", True)
    result = _push_config(conn, args.command, save)
    if hasattr(args, "_changes_list") and args._changes_list:
        result["_changes_list"] = args._changes_list
    return result


def send_config(conn: dict, args) -> dict:
    """Apply a multi-line config block string (workflow task — junos-netconf-send-config equivalent)."""
    device_name = conn.get("device_name") or conn["host"]
    if not args.command:
        return {"success": False, "host": conn["host"], "device_name": device_name,
                "error": "config block is required for action=send-config"}
    save = conn.get("save_config", True)
    result = _push_config(conn, args.command, save)
    if hasattr(args, "_changes_list") and args._changes_list:
        result["_changes_list"] = args._changes_list
    return result


def set_config(conn: dict, args) -> dict:
    """Config Manager remediation broker entry point."""
    device_name = conn.get("device_name") or conn["host"]
    if not args.command:
        return {"success": False, "host": conn["host"], "device_name": device_name,
                "error": "no config changes to apply"}
    save = conn.get("save_config", True)
    result = _push_config(conn, args.command, save)
    if hasattr(args, "_changes_list") and args._changes_list:
        result["_changes_list"] = args._changes_list
    return result


_DISPATCH = {
    "is-alive":     is_alive,
    "run-command":  run_command,
    "get-config":   get_config,
    "send-command": send_command,
    "send-config":  send_config,
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

    def pick(cli_val, *attr_keys, default=None):
        if cli_val is not None:
            return cli_val
        for key in attr_keys:
            v = attrs.get(key)
            if v is not None:
                return v
        return default

    host = pick(args.host, "itential_host")
    user = pick(args.user, "itential_user")
    port = int(pick(args.port, "itential_port") or 22)
    driver = (pick(args.driver, "itential_driver") or "netmiko").lower()
    platform = pick(args.platform, "itential_platform") or "autodetect"

    # Driver options: the full itential_driver_options[driver] dict passed through to
    # the library as **kwargs. We pop our two custom keys before passing.
    raw_driver_opts = dict((attrs.get("itential_driver_options") or {}).get(driver) or {})
    get_config_cmd = raw_driver_opts.pop("get_config_command", None) or "show running-config"
    save = str(raw_driver_opts.pop("save_config", "true")).lower() not in ("false", "0", "no")

    # Resolve password:
    #   1. --password CLI flag (local testing)
    #   2. itential_password starts with "$SECRET:" → name/path reference
    #   3. itential_password is a literal password
    #   4. --secret-ref CLI flag (local testing with Thycotic)
    password = args.password
    secret_username = None

    if not password:
        raw_pw = pick(None, "itential_password") or ""
        if raw_pw.startswith("$SECRET:"):
            thycotic_ref = raw_pw[len("$SECRET:"):]
        elif args.secret_ref:
            thycotic_ref = args.secret_ref
        else:
            thycotic_ref = None

        if thycotic_ref:
            try:
                secret = _thycotic_secret(thycotic_ref, _thycotic_cfg_from_env())
                password = secret.get("password")
                secret_username = secret.get("username")
            except Exception as e:
                raise SystemExit(f"Thycotic secret fetch failed ({thycotic_ref!r}): {e}")
        else:
            password = raw_pw or None

    if not user and secret_username:
        user = secret_username

    missing = [name for name, val in [("host", host), ("user", user), ("password", password)] if not val]
    if missing:
        raise SystemExit(
            f"missing required connection field(s): {', '.join(missing)} "
            f"(provide via --{missing[0]}, inventory attribute itential_{missing[0]}, "
            f"or Thycotic secret)"
        )

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "driver": driver,
        "platform": platform,
        "driver_opts": raw_driver_opts,
        "get_config_command": get_config_cmd,
        "save_config": save,
        "device_name": (node or {}).get("name") or host,
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="netsdk-thycotic: netmiko driver with Thycotic secret resolution for IAG5"
    )
    parser.add_argument(
        "--op", default=os.environ.get("NETSDK_OP"),
        choices=list(_DISPATCH),
        help="Operation to perform. Defaults to NETSDK_OP env var.",
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None,
                        help="Device password (overrides Thycotic and inventory)")
    parser.add_argument("--secret-ref", dest="secret_ref", default=None,
                        help="Thycotic secret reference for local testing: 'SecretName', 'Folder/SecretName', or bare integer ID")
    parser.add_argument("--driver", default=None, choices=["netmiko", "scrapli"],
                        help="Transport backend: netmiko (default) or scrapli")
    parser.add_argument("--platform", default=None,
                        help="Device platform (e.g. cisco_ios/cisco_iosxe, arista_eos, junos/juniper_junos)")
    parser.add_argument("--timeout", type=int, default=None)

    # run-command / send-command
    parser.add_argument("--command", action="append", default=None,
                        help="Command to run or config line to apply (repeatable)")
    parser.add_argument("--commands", default=None,
                        help="JSON array of config commands for send-command workflow task")

    # send-config: multi-line block
    parser.add_argument("--config", default=None,
                        help="Multi-line config block string for send-config / set-config")
    parser.add_argument("--config_content", "--config-content", dest="config_content", default=None,
                        help="Multi-line config block (Config Manager remediation path)")

    # set-config: Config Manager changes array
    parser.add_argument("--changes", default=None,
                        help="Config Manager changes JSON array [{parents, old, new}]")
    parser.add_argument("--options", default=None,
                        help="Config Manager remediation options JSON (unused)")

    # get-config
    parser.add_argument("--source", default=None,
                        help="Config source for get-config (running or startup)")
    parser.add_argument("--filter", default=None,
                        help="Optional filter string for get-config (passed to show command)")

    return parser


def _normalize_args(args):
    for attr in ("host", "user", "password", "config", "config_content", "commands",
                 "options", "source", "filter"):
        if getattr(args, attr, None) == "":
            setattr(args, attr, None)

    # --commands JSON array → --command list (send-command workflow task)
    if args.commands and not args.command:
        raw = args.commands
        try:
            cmds = json.loads(raw) if isinstance(raw, str) else raw
            args.command = [str(c) for c in (cmds if isinstance(cmds, list) else [cmds]) if str(c).strip()]
        except (json.JSONDecodeError, TypeError):
            args.command = [raw] if raw and raw.strip() else None
    args.commands = None

    # --config / --config_content: fold into --command
    for src_attr in ("config", "config_content"):
        src = getattr(args, src_attr, None)
        if src and not args.command:
            src = src.strip()
            if src.startswith("["):
                # Config Manager passes the changes array as --config
                try:
                    changes_list = json.loads(src)
                    lines = _extract_changes(changes_list)
                    if lines:
                        args.command = lines
                        args._changes_list = changes_list
                except (json.JSONDecodeError, TypeError, AttributeError):
                    args.command = [src]
            else:
                args.command = [line.strip() for line in src.splitlines() if line.strip()]
        setattr(args, src_attr, None)

    # --changes: Config Manager changes array
    if args.changes and not args.command:
        try:
            changes_list = json.loads(args.changes) if isinstance(args.changes, str) else args.changes
            lines = _extract_changes(changes_list)
            if lines:
                args.command = lines
                args._changes_list = changes_list
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    args.changes = None

    # Split multi-line --command values (MOP framework concatenates template lines)
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

    if not hasattr(args, "_changes_list"):
        args._changes_list = None


def _extract_changes(changes_list: list) -> list:
    """Extract non-null 'new' values (or deletions from 'old') from a CM changes array."""
    lines = []
    for c in changes_list:
        new_val = str(c.get("new", "") or "").strip()
        old_val = str(c.get("old", "") or "").strip()
        if new_val:
            lines.append(new_val)
        elif old_val:
            lines.append(f"no {old_val}" if not old_val.startswith("no ") else old_val)
    return lines


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
                output = [{"result": True, "parents": [], "old": "", "new": cmd}
                          for cmd in (result.get("commands") or [])]
            return json.dumps(output)
        else:
            print(result.get("error", "Configuration failed"), file=sys.stderr)
            return "[]"

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_parser().parse_args()
    if not args.op:
        raise SystemExit("--op flag or NETSDK_OP env var must be set")
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
