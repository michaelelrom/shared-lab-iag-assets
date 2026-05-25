# junos-netconf — IAG5 Python script service

NETCONF-based driver for Juniper Junos devices. Use this for any flow that
needs to survive an in-flight session restart (software add, reboot, halt)
— CLI-based drivers like netmiko and scrapli drop those responses because
they wait for a prompt that never returns.

Transport: NETCONF over SSH (port 830). Requires `set system services
netconf ssh` committed on the target device.

## Actions

| Action | Purpose | Notes |
|---|---|---|
| `is-alive` | Confirm device responds to NETCONF | Returns `{alive: true, session_id: N}` on success |
| `run-command` | Execute one or more operational CLI commands | Wraps the Junos `<command format="text">` RPC. `--command` is repeatable. |
| `get-config` | Retrieve running or candidate configuration | Defaults to `running`. Optional subtree filter. |
| `send-command` | Apply Junos `set …` config and commit | Locks candidate, loads `action="set" format="text"`, commits, unlocks. Discards on failure. |
| `reboot` | Schedule a reboot via `<request-reboot/>` RPC | No `[yes,no]` prompt. `--at +5` for delayed reboot. |

## Invocation model

Connection parameters (`host`, `port`, `user`, `password`, `timeout`,
`lock-timeout`, `lock-poll-interval`) come from the device's Inventory
Manager record via stdin — gateway5 pipes the `InventoryInfo` JSON
(`{"inventory_nodes": [{"name": "...", "attributes": {...}}]}`) to the
script's stdin automatically when invoked through an inventory action.

Only the action selector and per-action runtime inputs (`command`,
`source`, `filter`, `at`, `message`) are passed via `--set` /
`action_parameters`. The decorator schema marks just `action` as required.

### From iagctl (manual run with the inventory's attributes)

```bash
iagctl run service python-script junos-netconf \
  --set action=is-alive

iagctl run service python-script junos-netconf \
  --set action=run-command \
  --set command="show version"

iagctl run service python-script junos-netconf \
  --set action=reboot \
  --set at="+5"
```

### From an Inventory Manager action mapping

```json
{
  "name": "run-command",
  "action_type": "iag5-service",
  "action_config": {
    "service_name": "junos-netconf",
    "cluster_id":   "cluster-itential"
  },
  "action_parameters": {
    "action": "run-command"
  }
}
```

The script reads `itential_host` / `itential_user` / `itential_password`
plus `itential_driver_options.netconf.{port,timeout,lock_timeout,lock_poll_interval}`
from the inventory record. Workflow tasks supply the runtime
`command` (or `at`, `source`, etc.) when invoking the action.

### Direct local testing

The connection params can also be passed as CLI flags. Useful when
testing the script outside gateway5:

```bash
python main.py \
  --action run-command \
  --host 10.0.16.8 \
  --user itential \
  --password "$JUNOS_PASS" \
  --command "show version"
```

CLI flags win over stdin values when both are present.

### Required vs optional inputs

- **Required (always):** `action`
- **Required for `run-command` and `send-command`:** `command` (script enforces)
- **Connection fields (`host`, `user`, `password`, etc.):** required at
  runtime but resolved from inventory by default; CLI flags only when
  overriding
- **Unknown keys are rejected** by `additionalProperties: false`

## Candidate datastore locking (send-command)

`send-command` takes an exclusive lock on the candidate datastore before
loading config — NETCONF requires this and it prevents two operators (or
two automations) from committing conflicting changes. If another session
already holds the lock, the default behavior is a 30-second wait with
2-second polling before failing. Tunables:

| Flag | Default | Purpose |
|---|---|---|
| `--lock-timeout` | `30` | Max seconds to wait. `0` = fail immediately. |
| `--lock-poll-interval` | `2.0` | Seconds between retries. |

The successful response includes `lock_wait_seconds` so you can see how
long the operation actually blocked. Only `lock-denied` / `in-use` errors
are retried — any other RPC failure short-circuits the wait immediately.

## Recommended inventory attributes per device

For devices that should use this driver, set these in Inventory Manager
alongside (or replacing) your netmiko/scrapli attributes. The workflow
task that calls `junos-netconf` reads these and maps them to the script's
flags.

```json
{
  "name": "aws-lab-junos",
  "attributes": {
    "itential_host": "10.0.16.8",
    "itential_user": "itential",
    "itential_password": "$SECRET_vault $KEY_junos_pass",
    "itential_netconf_port": 830,
    "itential_netconf_timeout": 30,
    "itential_netconf_lock_timeout": 60,
    "itential_netconf_lock_poll_interval": 2
  }
}
```

The `itential_netconf_*` attributes are read by the workflow author and
passed to the script as `--port`, `--timeout`, `--lock-timeout`,
`--lock-poll-interval`. For devices that are mostly read-only or unlikely
to have contended locks, `itential_netconf_lock_timeout: 0` (fail-fast)
is reasonable. For shared lab devices where multiple operators may be
poking at the candidate, `60` or higher is sane.

## Why NETCONF for destructive ops

`request system software add` and `request system reboot` invoke device
operations that restart daemons (mgd, sshd) or reboot the device. With
CLI/SSH automation, the client session terminates before the operational
output reaches the caller — the driver raises a connection-closed error
and any captured response is discarded.

NETCONF is request/response over a framed channel. The device sends the
RPC reply as a single message; the channel can close immediately afterward
without losing the response. For `request-reboot` in particular there is
no interactive confirmation prompt to negotiate.

## Prerequisites on the device

```
set system services netconf ssh
commit
```

Port 830 must be reachable from IAG5. The vSRX security group should
allow TCP/22 (CLI) and TCP/830 (NETCONF) from the IAG5 host.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py is-alive --host 10.0.16.8 --user itential --password "$PASS"
```
