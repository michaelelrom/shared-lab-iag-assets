#!/usr/bin/env python3
"""panorama-rest — Palo Alto Panorama XML API driver for IG5.

Implements the IG5 device-broker contracts against the Panorama/PAN-OS XML API:
is-alive, run-command, get-config, rest-call, and set-config.

Panorama is a REST controller, so this driver is the REST counterpart to the
netmiko CLI path — the same broker actions, resolved over HTTPS instead of SSH.
Auth is PAN-OS key-based: the driver exchanges user/password for an API key via
type=keygen, then passes ?key= on every subsequent call.

Per-device configuration in Inventory Manager node attributes:
  itential_host      — Panorama management IP or hostname
  itential_port      — HTTPS port (default: 443)
  itential_user      — Panorama username (used for keygen)
  itential_password  — Panorama password (resolved by IG5 — no secret logic here)

  itential_driver_options.panorama-rest:
    verify_ssl  — verify TLS certificate (default: false; lab Panoramas use self-signed)
    timeout     — request timeout seconds (default: 60)
    config_src  — running | candidate  for get-config (default: running)

The PANORAMA_REST_OP environment variable selects the operation (set by IG5 per-service).
CLI flags override stdin inventory values for local testing.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

# lib/ is private to this driver — not shared with other device-drivers/*.
sys.path.insert(0, os.path.dirname(__file__))

from lib.iag import (
    read_stdin_inventory,
    normalize_args,
    print_result,
    build_set_config_output,
)

import requests
try:
    requests.packages.urllib3.disable_warnings()  # quiet self-signed cert noise
except Exception:
    pass


# ---------------------------------------------------------------------------
# Panorama XML API client
# ---------------------------------------------------------------------------

class PanoramaError(Exception):
    pass


class PanoramaClient:
    """Minimal PAN-OS/Panorama XML API client (key auth)."""

    def __init__(self, host, port, user, password, verify_ssl=False, timeout=60):
        self._base = f"https://{host}:{port}/api/"
        self._user = user
        self._password = password
        self._verify = verify_ssl
        self._timeout = timeout
        self._key = None

    def _request(self, params):
        r = requests.get(self._base, params=params, verify=self._verify,
                         timeout=self._timeout)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        if root.attrib.get("status") != "success":
            # surface PAN-OS error text when present
            msg = "".join(root.itertext()).strip() or r.text[:300]
            raise PanoramaError(f"Panorama API error: {msg}")
        return root

    def key(self):
        if not self._key:
            root = self._request({
                "type": "keygen",
                "user": self._user,
                "password": self._password,
            })
            node = root.find(".//key")
            if node is None or not (node.text or "").strip():
                raise PanoramaError("keygen succeeded but no <key> returned")
            self._key = node.text.strip()
        return self._key

    def op(self, cmd_xml):
        """Run an operational command (XML-wrapped) and return the <result> element."""
        root = self._request({"type": "op", "cmd": cmd_xml, "key": self.key()})
        return root.find("result")

    def config_show(self, xpath):
        """Return the <result> element for a config-tree read."""
        root = self._request({"type": "config", "action": "show",
                              "xpath": xpath, "key": self.key()})
        return root.find("result")

    def config_set(self, xpath, element):
        root = self._request({"type": "config", "action": "set",
                              "xpath": xpath, "element": element, "key": self.key()})
        return root


def _elem_to_obj(el):
    """Convert an ElementTree element to an xmltodict-style dict/str (stdlib only).

    - attributes become "@name" keys
    - repeated child tags collapse into a list
    - a leaf with no attributes becomes its text string
    - mixed text + children keeps the text under "#text"
    """
    d = {}
    for k, v in el.attrib.items():
        d[f"@{k}"] = v
    children = list(el)
    if children:
        for c in children:
            cv = _elem_to_obj(c)
            if c.tag in d:
                if not isinstance(d[c.tag], list):
                    d[c.tag] = [d[c.tag]]
                d[c.tag].append(cv)
            else:
                d[c.tag] = cv
        text = (el.text or "").strip()
        if text:
            d["#text"] = text
        return d
    text = (el.text or "").strip()
    if d:
        if text:
            d["#text"] = text
        return d
    return text


def _result_to_text(result_el):
    """Serialize a <result> element's children to XML text (the config/data payload)."""
    if result_el is None:
        return ""
    # If result wraps a single <config> (get-config), return that element's XML.
    children = list(result_el)
    if len(children) == 1:
        return ET.tostring(children[0], encoding="unicode")
    if children:
        return "".join(ET.tostring(c, encoding="unicode") for c in children)
    return (result_el.text or "").strip()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def is_alive(conn, args):
    try:
        client = _client(conn)
        client.op("<show><system><info></info></system></show>")
        return {"success": True, "alive": True, "host": conn["host"]}
    except Exception as e:
        return {"success": False, "alive": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def get_config(conn, args):
    try:
        client = _client(conn)
        src = conn.get("config_src") or "running"
        cmd = f"<show><config><{src}></{src}></config></show>"
        result_el = client.op(cmd)
        children = list(result_el) if result_el is not None else []
        if not children:
            return {"success": False, "host": conn["host"],
                    "error": f"empty {src} config returned"}
        fmt = (conn.get("config_format") or "json").lower()
        if fmt == "xml":
            config_out = _result_to_text(result_el)
        else:
            cfg_el = children[0]  # the <config> element
            config_out = json.dumps({cfg_el.tag: _elem_to_obj(cfg_el)}, indent=2)
        return {"success": True, "host": conn["host"],
                "config_format": fmt, "config": config_out}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def run_command(conn, args):
    """Run one or more Panorama operational commands (each an XML-wrapped op cmd)."""
    if not args.command:
        return {"success": False, "host": conn["host"],
                "error": "command is required for action=run-command "
                         "(XML op command, e.g. <show><system><info/></system></show>)"}
    results = []
    try:
        client = _client(conn)
        for cmd in args.command:
            try:
                result_el = client.op(cmd)
                results.append({"command": cmd, "output": _result_to_text(result_el),
                                "success": True})
            except Exception as e:
                results.append({"command": cmd, "output": "", "success": False,
                                "error": str(e)})
        return {"success": all(r["success"] for r in results),
                "host": conn["host"], "results": results}
    except Exception as e:
        return {"success": False, "host": conn["host"], "error": str(e),
                "error_type": type(e).__name__, "results": results}


def rest_call(conn, args):
    """Generic XML-API passthrough — caller supplies type/action/xpath/cmd/element."""
    if not args.route and not getattr(args, "cmd", None):
        return {"success": False, "host": conn["host"],
                "error": "provide --route (xpath) or --cmd for action=rest-call"}
    try:
        client = _client(conn)
        verb = (args.verb or "op").lower()
        if verb == "op" or getattr(args, "cmd", None):
            result_el = client.op(args.cmd or args.body or "")
            return {"success": True, "host": conn["host"],
                    "response": _result_to_text(result_el)}
        # treat --route as an xpath config read
        result_el = client.config_show(args.route)
        return {"success": True, "host": conn["host"],
                "response": _result_to_text(result_el)}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


def set_config(conn, args):
    """Config Manager remediation — apply changes via type=config&action=set.

    Expects --changes / --config as a CM changes array; each change's `new`
    value is an 'xpath=...;element=...' pair (semicolon-separated) so the broker
    can target the PAN-OS config tree precisely.
    """
    if not args.command:
        return {"success": False, "host": conn["host"],
                "error": "no config changes to apply"}
    applied = []
    try:
        client = _client(conn)
        for change in args.command:
            if "element=" in change and "xpath=" in change:
                xpath = change.split("xpath=", 1)[1].split(";element=", 1)[0]
                element = change.split(";element=", 1)[1]
                client.config_set(xpath, element)
                applied.append(change)
            else:
                raise PanoramaError(
                    "set-config change must be 'xpath=<xpath>;element=<xml>' — "
                    f"got: {change[:80]}")
        changes_list = getattr(args, "_changes_list", None)
        output = build_set_config_output(args.command, changes_list)
        return {"success": True, "host": conn["host"],
                "_output": output, "_changes_list": changes_list}
    except Exception as e:
        return {"success": False, "host": conn["host"],
                "error": str(e), "error_type": type(e).__name__}


_DISPATCH = {
    "is-alive":    is_alive,
    "run-command": run_command,
    "get-config":  get_config,
    "rest-call":   rest_call,
    "set-config":  set_config,
}


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------

def _client(conn):
    return PanoramaClient(
        host=conn["host"], port=conn["port"],
        user=conn["user"], password=conn["password"],
        verify_ssl=conn["verify_ssl"], timeout=conn["timeout"],
    )


def _resolve_connection(args, node):
    attrs = ((node or {}).get("attributes") or {})
    driver_opts = dict((attrs.get("itential_driver_options") or {}).get("panorama-rest") or {})

    def pick(cli_val, attr_key, default=None):
        if cli_val is not None:
            return cli_val
        v = attrs.get(attr_key)
        return v if v is not None else default

    host     = pick(args.host,     "itential_host")
    port     = int(pick(args.port, "itential_port", 443))
    user     = pick(args.user,     "itential_user")
    password = pick(args.password, "itential_password")

    verify_ssl = driver_opts.pop("verify_ssl", False)
    if isinstance(verify_ssl, str):
        verify_ssl = verify_ssl.lower() not in ("false", "0", "no", "")
    timeout    = int(driver_opts.pop("timeout", 60) or 60)
    config_src = driver_opts.pop("config_src", None) or "running"
    config_format = driver_opts.pop("config_format", None) or "json"

    missing = [n for n, v in [("host", host), ("user", user), ("password", password)] if not v]
    if missing:
        raise SystemExit(
            f"missing required field(s): {', '.join(missing)} "
            f"(set itential_{missing[0]} on the inventory node)")

    return {
        "host": host, "port": port, "user": user, "password": password,
        "verify_ssl": verify_ssl, "timeout": timeout, "config_src": config_src,
        "config_format": config_format,
        "device_name": (node or {}).get("name") or host,
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="panorama-rest: Palo Alto Panorama XML API driver for IG5")
    parser.add_argument("--op", default=os.environ.get("PANORAMA_REST_OP"),
                        choices=list(_DISPATCH),
                        help="Operation. Defaults to PANORAMA_REST_OP env var.")
    parser.add_argument("--host",     default=None)
    parser.add_argument("--port",     type=int, default=None)
    parser.add_argument("--user",     default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--timeout",  type=int, default=None)

    # run-command / set-config
    parser.add_argument("--command", action="append", default=None,
                        help="XML op command (repeatable)")
    parser.add_argument("--commands", default=None, help="JSON array of commands")
    parser.add_argument("--config",         default=None)
    parser.add_argument("--config_content", "--config-content",
                        dest="config_content", default=None)
    parser.add_argument("--changes", default=None)
    parser.add_argument("--options", default=None)

    # rest-call
    parser.add_argument("--verb",  default=None, help="op (default) or config")
    parser.add_argument("--route", default=None, help="xpath for a config read")
    parser.add_argument("--cmd",   default=None, help="XML op command for rest-call")
    parser.add_argument("--body",  default=None)

    return parser


def main():
    args = build_parser().parse_args()
    if not args.op:
        raise SystemExit("--op flag or PANORAMA_REST_OP env var must be set")
    normalize_args(args)
    node = read_stdin_inventory()
    conn = _resolve_connection(args, node)
    result = _DISPATCH[args.op](conn, args)
    return print_result(result, args.op)


if __name__ == "__main__":
    sys.exit(main())
