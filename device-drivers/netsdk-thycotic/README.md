# netsdk-thycotic — IAG5 Python script service

netmiko-based CLI device driver that resolves device passwords from
**Delinea Thycotic Secret Server** at runtime rather than storing them in
Inventory Manager attributes. Drop-in replacement for the platform's built-in
netsdk driver for environments where credentials are centrally vaulted.

## Operations

| Action | Broker contract | Notes |
|---|---|---|
| `is-alive` | `is-alive` | Returns `true` or `false` — bare string, no JSON wrapper |
| `run-command` | `run-command` | Runs one or more operational CLI commands; returns plain text output |
| `get-config` | `get-config` | Runs the configured show command and returns the config as plain text |
| `send-command` | — | Applies an array of config commands and saves (workflow task) |
| `send-config` | — | Applies a multi-line config block string and saves (workflow task) |
| `set-config` | `set-config` | Config Manager remediation broker entry point; accepts CM changes array |

## Registered services

| Service name | Operation | Decorator |
|---|---|---|
| `netsdk-thycotic-is-alive` | is-alive | none |
| `netsdk-thycotic-run-command` | run-command | `netsdk-thycotic-run-command-input` |
| `netsdk-thycotic-get-config` | get-config | `netsdk-thycotic-get-config-input` |
| `netsdk-thycotic-send-command` | send-command | `netsdk-thycotic-send-command-input` |
| `netsdk-thycotic-send-config` | send-config | `netsdk-thycotic-send-config-input` |
| `netsdk-thycotic-set-config` | set-config | `netsdk-thycotic-set-config-input` |

## Thycotic configuration

Set these environment variables **once** on the IAG5 host (e.g. in the IAG5
systemd unit override or `/etc/gateway/env`):

```
THYCOTIC_BASE_URL=https://vault.example.com/SecretServer
THYCOTIC_USERNAME=iag5-service-account
THYCOTIC_PASSWORD=changeme
THYCOTIC_DOMAIN=corp              # optional — omit for local accounts
```

The driver authenticates to Thycotic using `grant_type=password` and fetches
the secret at connection time. No token caching — a fresh token is obtained
per IAG invocation.

## Inventory Manager attributes

```json
{
  "name": "router-01",
  "attributes": {
    "itential_host": "192.0.2.1",
    "itential_port": 22,
    "itential_driver": "netmiko",
    "itential_platform": "cisco_ios",
    "itential_user": "admin",
    "itential_password": "$SECRET:NetworkDevices/router-01",
    "itential_driver_options": {
      "netmiko": {
        "timeout": 60,
        "conn_timeout": 30,
        "banner_timeout": 30,
        "session_timeout": 60,
        "get_config_command": "show running-config",
        "save_config": true
      }
    }
  }
}
```

| Attribute | Description |
|---|---|
| `itential_host` | Device IP or hostname |
| `itential_port` | SSH port (default: `22`) |
| `itential_driver` | Transport backend: `netmiko` (default) or `scrapli` |
| `itential_platform` | Device platform — netmiko `device_type` or scrapli `platform` (e.g. `cisco_ios`, `arista_eos`, `junos`). Common netmiko names are automatically mapped to scrapli equivalents when switching backends. |
| `itential_user` | Device username. Falls back to the `Username` field from the resolved vault secret if omitted. |
| `itential_password` | `$SECRET:` vault reference or literal password fallback — see below. |
| `itential_driver_options.{driver}` | Passed through wholesale as `**kwargs` to netmiko's `ConnectHandler` or scrapli's `Scrapli`. Any option the library accepts is valid — see [netmiko docs](https://ktbyers.github.io/netmiko/) or [scrapli docs](https://carlmontanari.github.io/scrapli/) for the full set. Two keys are handled by this driver before the rest reaches the library: `get_config_command` and `save_config`. |

### Driver options reference

Two keys inside `itential_driver_options.{driver}` are handled by this driver:

| Key | Default | Description |
|---|---|---|
| `get_config_command` | `show running-config` | Show command used for `get-config` |
| `save_config` | `true` | Save config after push operations |

Everything else is passed directly to the library. Common options:

**netmiko** — `timeout` (100), `conn_timeout` (10), `banner_timeout` (15), `session_timeout` (60), `auth_timeout`, `fast_cli` (false), `global_delay_factor` (1.0), `read_timeout_override`

**scrapli** — `timeout_socket` (15.0), `timeout_transport` (30.0), `timeout_ops` (30.0), `auth_strict_key` (false*), `transport` (system)

*Driver defaults `auth_strict_key` to `false` for operational convenience — set `true` in production with known-hosts configured.

### Secret reference format

| Format | Example |
|---|---|
| `$SECRET:SecretName` | `$SECRET:router-01` — unique name, searched vault-wide |
| `$SECRET:FolderPath/SecretName` | `$SECRET:NetworkDevices/router-01` — scoped to a folder |
| `$SECRET:42` | fetch by Thycotic integer ID (URL fallback) |

Folder matching is case-insensitive; forward and back slashes both work. Zero matches or multiple matches both fail with a descriptive error listing what was found.

**Password resolution order:**
1. `--password` CLI flag (literal, local testing — skips Thycotic entirely)
2. `itential_password` starts with `$SECRET:` → resolved from Thycotic at runtime
3. `itential_password` literal value (plain-text fallback)
4. `--secret-ref` CLI flag (local testing with Thycotic: `--secret-ref "NetworkDevices/router-01"`)

## Inventory Manager action mapping

Wire to the broker contracts when creating or updating an inventory:

```json
{
  "actions": [
    {
      "name": "is-alive",
      "action_type": "iag5-service",
      "action_config": {
        "service_name": "netsdk-thycotic-is-alive",
        "cluster_id": "your-cluster-id"
      }
    },
    {
      "name": "run-command",
      "action_type": "iag5-service",
      "action_config": {
        "service_name": "netsdk-thycotic-run-command",
        "cluster_id": "your-cluster-id"
      }
    },
    {
      "name": "get-config",
      "action_type": "iag5-service",
      "action_config": {
        "service_name": "netsdk-thycotic-get-config",
        "cluster_id": "your-cluster-id"
      }
    },
    {
      "name": "set-config",
      "action_type": "iag5-service",
      "action_config": {
        "service_name": "netsdk-thycotic-set-config",
        "cluster_id": "your-cluster-id"
      }
    }
  ]
}
```

## Local testing

The driver falls through to `itential_password` or `--password` if Thycotic
env vars are not set, so you can test without a live vault:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# is-alive
NETSDK_OP=is-alive python main.py \
  --host 192.0.2.1 --user admin --password "$PASS" \
  --platform cisco_ios

# run-command
NETSDK_OP=run-command python main.py \
  --host 192.0.2.1 --user admin --password "$PASS" \
  --platform cisco_ios \
  --command "show version"

# send-command (config push)
NETSDK_OP=send-command python main.py \
  --host 192.0.2.1 --user admin --password "$PASS" \
  --platform cisco_ios \
  --commands '["interface Loopback100", "description test"]'

# with Thycotic (env vars set)
NETSDK_OP=is-alive python main.py \
  --host 192.0.2.1 --user admin \
  --secret-ref "NetworkDevices/router-01" \
  --platform cisco_ios
```

## Supported device types

Any netmiko or scrapli platform name is valid. Common values:

| Device | `itential_platform` | netmiko name | scrapli name |
|---|---|---|---|
| Cisco IOS / IOS-XE | `cisco_ios` | `cisco_ios` | `cisco_iosxe` (auto-mapped) |
| Cisco IOS-XR | `cisco_xr` | `cisco_xr` | `cisco_iosxr` (auto-mapped) |
| Cisco NX-OS | `cisco_nxos` | `cisco_nxos` | `cisco_nxos` |
| Arista EOS | `arista_eos` | `arista_eos` | `arista_eos` |
| Juniper JunOS | `junos` | `junos` | `juniper_junos` (auto-mapped) |
| Palo Alto PAN-OS | `paloalto_panos` | `paloalto_panos` | `paloalto_panos` |
| F5 BIG-IP | `f5_ltm` | `f5_ltm` | — |
| Nokia SR-OS | `nokia_sros` | `nokia_sros` | `nokia_sros` |

Auto-mapped means you can set `itential_platform: cisco_ios` and switch `itential_driver` between `netmiko` and `scrapli` without changing the platform value.

For JunOS in particular, prefer the `junos-netconf-*` services for operations
that restart daemons (software add, reboot) — NETCONF handles mid-session
restarts cleanly; netmiko does not.

## Dependencies

- `netmiko>=4.1.0`
- `requests>=2.28.0`
