# panorama-rest — Palo Alto Panorama XML API device driver for IG5

REST counterpart to the netmiko CLI path for Panorama. Implements the IG5
device-broker contracts against the Panorama/PAN-OS **XML API** so a Panorama
controller can be brokered in Configuration Manager / Inventory Manager with
the same standard actions as a CLI device — resolved over HTTPS instead of SSH.

## Services (one `main.py`, op selected by `PANORAMA_REST_OP`)

| Service | Op | What it does |
|---|---|---|
| `panorama-rest-is-alive` | `is-alive` | keygen + `show system info` → `true`/`false` |
| `panorama-rest-get-config` | `get-config` | `show config running` → config XML |
| `panorama-rest-run-command` | `run-command` | run an XML-wrapped op command |
| `panorama-rest-set-config` | `set-config` | CM remediation via `type=config&action=set` |
| `panorama-rest-call` | `rest-call` | generic op/config passthrough (workflow task) |

## Auth

PAN-OS key auth: the driver exchanges `itential_user` / `itential_password` for
an API key via `type=keygen`, then passes `?key=` on every call. Credentials come
from the inventory node (resolved by IG5 — no secret logic in the driver; use a
`$GATEWAYSECRET_(alias)` reference on the node to keep them out of cleartext).

## Inventory node attributes

```json
{
  "name": "panorama-rest",
  "attributes": {
    "itential_host": "3.225.113.251",
    "itential_port": 443,
    "itential_user": "admin",
    "itential_password": "$GATEWAYSECRET_(panorama-admin)",
    "itential_driver_options": {
      "panorama-rest": { "verify_ssl": false, "timeout": 60, "config_src": "running" }
    }
  }
}
```

Note there is **no** `itential_driver` / `itential_platform` (those drive netmiko).
The broker actions on the inventory must point at the `panorama-rest-*` services.

## Local test

```bash
NODE='{"inventory_nodes":[{"name":"panorama","attributes":{"itential_host":"<ip>","itential_port":443,"itential_user":"admin","itential_password":"<pw>","itential_driver_options":{"panorama-rest":{"verify_ssl":false}}}}]}'
printf '%s' "$NODE" | PANORAMA_REST_OP=is-alive   python3 main.py   # -> true
printf '%s' "$NODE" | PANORAMA_REST_OP=get-config python3 main.py   # -> <config ...>
```

Verified against Panorama 11.1.2 (keygen, show config running, show system info).
