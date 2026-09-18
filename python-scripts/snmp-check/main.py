#!/usr/bin/env python3
"""Independent interface verification via SNMP (IF-MIB / IP-MIB).

Deliberately not another SSH/CLI check -- this confirms interface state
through a completely different protocol path than the MOP command
templates use, so it catches cases where the SSH-based checks alone
would be wrong (device credentials swapped, CLI parsing bug, etc.).

device_ip can be passed explicitly via params, or left unset and targeted
via runService's inventory/nodeNames instead -- gateway5 resolves the node
and pipes its attributes (including itential_host) to this script's stdin.
An explicit --device_ip always takes precedence over the piped inventory.
"""
import argparse
import json
import sys

from puresnmp import Client, V2C, PyWrapper


def read_stdin_inventory():
    """Read the InventoryInfo JSON gateway5 pipes to stdin when a runService
    call is targeted via inventory/nodeNames. Returns the first node dict,
    or None if stdin is a TTY or the payload is missing/malformed."""
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

IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IP_AD_ENT_ADDR = "1.3.6.1.2.1.4.20.1.1"
IP_AD_ENT_IF_INDEX = "1.3.6.1.2.1.4.20.1.2"

STATUS_MAP = {1: "up", 2: "down", 3: "testing"}


async def find_if_index(client, interface_name):
    async for row in client.walk(IF_DESCR):
        descr = row.value.decode() if isinstance(row.value, bytes) else str(row.value)
        if descr == interface_name:
            return int(str(row.oid).rsplit(".", 1)[-1])
    return None


async def get_ip_addresses(client, if_index):
    addr_to_index = {}
    async for row in client.walk(IP_AD_ENT_IF_INDEX):
        ip = str(row.oid).split(f"{IP_AD_ENT_IF_INDEX}.")[-1]
        addr_to_index[ip] = int(row.value)
    return [ip for ip, idx in addr_to_index.items() if idx == if_index]


async def run_check(device_ip, community, interface_name):
    client = PyWrapper(Client(device_ip, V2C(community)))

    if_index = await find_if_index(client, interface_name)
    if if_index is None:
        return {
            "success": False,
            "error": f"Interface '{interface_name}' not found via SNMP (IF-MIB::ifDescr)",
            "interface_name": interface_name,
        }

    admin_raw = await client.get(f"{IF_ADMIN_STATUS}.{if_index}")
    oper_raw = await client.get(f"{IF_OPER_STATUS}.{if_index}")
    ip_addresses = await get_ip_addresses(client, if_index)

    admin_status = STATUS_MAP.get(int(admin_raw), str(admin_raw))
    oper_status = STATUS_MAP.get(int(oper_raw), str(oper_raw))

    return {
        "success": admin_status == "up" and oper_status == "up",
        "interface_name": interface_name,
        "if_index": if_index,
        "admin_status": admin_status,
        "oper_status": oper_status,
        "ip_addresses": ip_addresses,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device_ip", required=False, default=None)
    parser.add_argument("--community", default="")
    parser.add_argument("--interface", required=True)
    args = parser.parse_args()

    community = args.community or "itential-lab-ro"

    device_ip = args.device_ip or None
    if not device_ip:
        node = read_stdin_inventory()
        if node:
            device_ip = (node.get("attributes") or {}).get("itential_host")

    if not device_ip:
        print(json.dumps({
            "success": False,
            "error": "device_ip not provided and no inventory node was targeted "
                     "(pass --device_ip, or target this service via inventory/nodeNames)",
            "interface_name": args.interface,
        }))
        return 0

    try:
        import asyncio

        result = asyncio.run(run_check(device_ip, community, args.interface))
        print(json.dumps(result))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e), "interface_name": args.interface}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
