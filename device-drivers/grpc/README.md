# grpc — Gateway 5 Python script service

gRPC driver for network devices: **gNMI** (Capabilities, Get, Set), **gNOI**
(System, Layer2, BGP, Interface) and **any other gRPC service** through server
reflection. It is the Gateway 5 successor to the gRPC support in Gateway 4
(`grpc.py`, `grpc_gnoi.py`, `api_v2/gnmi.py`, `api_v2/gnoi.py`) and follows the
same shape as the Junos NETCONF driver: one `main.py`, one service per
operation, behaviour selected by an env var (`GRPC_OP`), connection details
taken from the Inventory Manager record on stdin.

## Table of Contents

- [Operations](#operations)
- [Connection pooling](#connection-pooling)
- [Invocation model](#invocation-model)
- [Inventory attributes](#inventory-attributes)
- [Extending the driver](#extending-the-driver)
- [Output and errors](#output-and-errors)
- [Prerequisites](#prerequisites)
- [Local development](#local-development)

## Operations

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-is-alive` | `is-alive` | Channel + gNMI Capabilities round trip. Prints bare `true` / `false`. |
| `grpc-gnmi-capabilities` | `capabilities` | gNMI Capabilities. |
| `grpc-gnmi-get` | `get` | gNMI Get: `path` (repeatable), `datatype`, `encoding`, `prefix`, `target`, `origin`, `raw`. |
| `grpc-gnmi-get-config` | `get-config` | Get restricted to config data. |
| `grpc-gnmi-set` | `set` | gNMI Set: `update` / `replace` / `union_replace` as `[{path, value}]`, `delete` as `[path]`, plus `origin`, `raw`, `force`. Applied atomically by the device. `union_replace` (gNMI 0.10.0) is refused unless the device advertises 0.10.0+ (older targets silently ignore the field); `force=true` sends it anyway. |
| `grpc-gnmi-set-config` | `set-config` | Config Manager broker entry point. `config` is `[{parents, old, new}]`: `parents` joins into the path, `new` (JSON) is written, `old` with empty `new` deletes. `mode` = `update` (default), `replace` or `union_replace`; also `origin`, `force`. Returns `[{"result": true, ...}]` per change, like the Junos driver. |
| `grpc-run-command` | `run-command` | Config Manager / Command Templates entry point. `command` is newline-separated. Each line is `gnmi capabilities`, `gnmi get <path>`, `gnoi <service.Method> [json]`, or anything else, which is sent as a CLI-origin gNMI Get (e.g. `show version` on IOS XR). Returns `{success, host, results:[{command, output, success}]}`; one failing command does not hide the others. |
| `grpc-gnoi-call` | `gnoi` | Any gNOI RPC by name: `rpc=system.Time`, `rpc=layer2.ClearSpanningTree request='{...}'`. Streaming RPCs (Ping, Traceroute) return a list. |
| `grpc-gnoi-time` | `gnoi-time` | `system.Time`. |
| `grpc-gnoi-reboot` | `gnoi-reboot` | `system.Reboot` (`method`, `delay` ns, `message`). Never retried. |
| `grpc-reflection-call` | `call` | Any unary or server-streaming method via reflection: `method=gnoi.system.System/Time payload='{}'`. |
| `grpc-pool-status` / `grpc-pool-stop` | `pool-status` / `pool-stop` | Inspect or stop the pool daemon. |

The gNOI RPCs that Gateway 4 exposed one-by-one (ping, traceroute, reboot,
cancel_reboot, reboot_status, set_package, switch_control_processor, time,
clear_lldp_interface, clear_bgp_neighbor, clear_interface_counters,
clear_neighbor_discovery, clear_spanning_tree, wake_on_lan) are all reachable
through `gnoi` — it reads the protobuf service descriptors instead of needing a
wrapper per RPC. `grpc-gnoi-call` with a bad `rpc` lists every valid name.

## Connection pooling

**Gateway 4 had no pooling.** Every REST request ran
`with get_client(host) as gc:`, which built a new channel (TCP + TLS +
Capabilities round trip) and closed it again.

**Gateway 5 makes the obvious fix impossible.** A `python-script` service is a
fresh process per call, so a pool held inside `main.py` is empty every time.
Real pooling needs something that outlives the call, so this driver ships one:

```
 Gateway 5 call 1 ─ python main.py ─┐
 Gateway 5 call 2 ─ python main.py ─┼─ unix socket ─▶ pool daemon ══ 1 warm HTTP/2 channel ══▶ device
 Gateway 5 call 3 ─ python main.py ─┘   (JSON lines)  (background)     per device identity
```

- **`GRPC_POOL=auto` (default)** — use the daemon, starting it on first use. If
  it cannot be reached or started, fall back to a private channel for that one
  call. The result reports which happened (`--envelope true` →
  `pool.mode` = `pooled` | `fallback`, `pool.reused`).
- **`off`** — always a private channel (the Gateway 4 behaviour).
- **`required`** — fail rather than fall back; use it when you want a broken
  pool to be loud.

Set it per device with `itential_driver_options.grpc.pool`, or globally with
the `GRPC_POOL` env var on the service. A `--pool` flag overrides both.

### What the pool guarantees

| Concern | Behaviour |
|---|---|
| Pool key | `sha256` of host, port, credentials, TLS and gRPC options. A rotated password or changed option opens a new channel; it can never reuse a stale one. |
| Concurrency | One channel per key, shared. gRPC multiplexes concurrent calls over a single HTTP/2 connection, so a size-N pool per device would add nothing. |
| Liveness | Channel state is tracked; unhealthy channels are replaced. Keepalive pings (60 s) are on by default. |
| Dead channel | A call on a *reused* channel that fails with `UNAVAILABLE` / `UNAUTHENTICATED` evicts it and is retried once on a fresh one — **only for operations marked idempotent** (get, capabilities, is-alive, time, ...). `set`, `set-config`, `gnoi-reboot` and anything else are never replayed. |
| Hung device | pygnmi sets no per-RPC deadline, so every operation runs under `rpc_timeout` (default 30 s). On expiry the channel is closed, which cancels the in-flight RPC, and the channel is evicted. |
| Lifecycle | Idle channels close after `GRPC_POOL_IDLE_TTL` (300 s); channels are recycled after `GRPC_POOL_MAX_AGE` (3600 s); the daemon exits after `GRPC_POOL_DAEMON_IDLE` (600 s) with no channels; `GRPC_POOL_MAX_SIZE` (32) evicts least-recently-used idle channels. |
| Ambiguous failure | If the daemon dies *after* receiving a request, the call fails instead of falling back — a config push is never run twice. Fallback only happens when nothing was sent. |
| Code updates | The socket name includes a hash of the driver code, so a re-pulled repo gets a fresh daemon. Old ones idle out. |
| Security | Socket directory is `0700` and the socket `0600` (same uid only). Credentials sit in daemon memory for the life of a channel and are never written to disk. Logs go to `<socket>.log`. |

### Limits to know about

- **Scope is one Gateway host/container.** Each Gateway 5 node runs its own
  daemon; nothing is shared across nodes.
- **The pool only helps if the daemon survives the call.** It is started
  detached (`start_new_session`). If your Gateway 5 runtime reaps child
  processes when a script exits, `auto` quietly degrades to one channel per call
  (still correct, just no faster than Gateway 4). Check on your deployment:

  ```bash
  iagctl run service python-script grpc-gnmi-get --set path='["/"]'   # twice, against a real device
  iagctl run service python-script grpc-pool-status                    # hits should be 1, uses 2
  ```

  `running: false` after a call means the runtime killed it; set `GRPC_POOL=off`
  to stop paying for the spawn attempt, or run the daemon as a real service
  (`python -m grpc_driver.poold` under systemd or as a sidecar container, with
  `GRPC_POOL_SOCKET` pointing at a shared volume).
- Credentials are passed to the daemon per request, so the daemon holds a
  password for as long as the channel is alive.
- Unix sockets are capped at ~104 bytes; the default path is under `/tmp` for
  that reason.

## Invocation model

**One service per operation**, all pointing at `main.py` with a different
`GRPC_OP`. `import.yaml` registers them and their input schemas (booleans are
`"true"`/`"false"` strings; unset optional fields arrive as empty strings and
are ignored).

```bash
iagctl run service python-script grpc-is-alive
iagctl run service python-script grpc-gnmi-get --set path='["/interfaces/interface[name=eth0]/state"]' --set datatype=state
iagctl run service python-script grpc-gnoi-call --set rpc=system.Time
iagctl run service python-script grpc-reflection-call --set method=gnoi.system.System/Time
```

Inventory Manager action mapping:

```json
{
  "name": "get-config",
  "action_type": "iag5-service",
  "action_config": { "service_name": "grpc-gnmi-get-config", "cluster_id": "cluster-itential" },
  "action_parameters": {}
}
```

Connection flags (`--host --port --user --password --insecure --skip-verify
--path-cert --path-key --path-root --override --token --timeout --rpc-timeout
--pool`) override the inventory, for local testing:

```bash
python main.py --op get --host 10.0.0.5 --port 57400 --user admin --password "$PW" --insecure true --path /
```

## Inventory attributes

```json
{
  "name": "leaf1",
  "attributes": {
    "itential_host": "10.0.0.5",
    "itential_user": "admin",
    "itential_password": "$SECRET_vault $KEY_leaf1",
    "itential_driver_options": {
      "grpc": {
        "port": 57400,
        "insecure": false,
        "skip_verify": false,
        "path_root": "/etc/ssl/ca.pem",
        "path_cert": "/etc/ssl/client.pem",
        "path_key": "/etc/ssl/client.key",
        "override": "leaf1.example.com",
        "token": null,
        "timeout": 10,
        "rpc_timeout": 30,
        "grpc_options": { "grpc.max_send_message_length": 104857600 },
        "pool": "auto"
      }
    }
  }
}
```

These are the Gateway 4 per-device gRPC variables (`host`, `port`, `username`,
`password`, `insecure`, `path_*`, `override`, `skip_verify`, `grpc_timeout`,
`grpc_options`, `token`) under their Gateway 5 homes. `grpc_timeout` is now
`timeout` (connect) plus `rpc_timeout` (per operation). Gateway 4's global
`grpc_max_send_size` is just a `grpc.max_send_message_length` option in bytes.

## Extending the driver

Everything is registered in `grpc_driver/registry.py`. Three extension points,
none of which touch the core:

**1. New operation.** Drop `plugins/<name>.py` (see `plugins/_example_plugin.py`):

```python
from grpc_driver.registry import operation

@operation("interface-counters", idempotent=True)
def interface_counters(session, params):
    return session.gnmi.get(path=[f"/interfaces/interface[name={params['name']}]/state/counters"], datatype="state")
```

`session` is a connected channel (`session.gnmi`, `session.channel`,
`session.metadata`, `session.stub(name)`); `params` are the `--key value` flags.
Mark `idempotent=True` only if replaying is safe. Add a service to `import.yaml`
with `GRPC_OP: interface-counters` and a decorator for its inputs. Pooling
works for plugin operations automatically.

**2. New gRPC service with compiled stubs.**
`register_service("healthz", healthz_pb2, healthz_pb2_grpc.HealthzStub, "Healthz")`
makes every RPC callable as `grpc-gnoi-call rpc=healthz.<Method>`.

**3. No stubs at all.** `grpc-reflection-call` works against any server with
reflection enabled. Descriptors are cached on the pooled channel, so repeat
calls skip the reflection round trips.

Plugin search path: `plugins/`, each directory in `GRPC_PLUGIN_DIR`
(`os.pathsep`-separated), and each module or `.py` file in `GRPC_PLUGINS`
(comma-separated).

The vendored `gnoi/` package is the Gateway 4 stub set, with imports rewritten
from `automation_gateway.gnoi` to `gnoi`.

## Output and errors

- Success: the operation's result as JSON on stdout, exit `0`.
  `is-alive` prints bare `true`/`false` (no newline) for gw-manager.
- Failure: exit `1`, the envelope on stderr
  (`success`, `op`, `host`, `device_name`, `error`, `error_type`, `pool`).
  `error_type` is the lower-cased gRPC status (`unavailable`, `unauthenticated`,
  `unimplemented`, ...) or `timeout` / `invalid_input` / `unknown_rpc` — the
  same distinctions Gateway 4 mapped to HTTP 408/501/security errors.
- `--envelope true` prints the full envelope on success too.

## Known issues covered by the mock device

The test-suite runs against `tests/mock_device.py`, a stateful gNMI/gNOI device that can be configured to behave like the problem devices from support cases. Each of these has a regression test in `tests/test_regressions.py`:

- **`union_replace`**: replaces the whole config with the union of its updates, keeps state, rejects overlapping paths and combination with `replace`; the driver refuses it against gNMI < 0.10.0 (unless `force`). The mock's overlap rules are assumptions, not verified against the spec text.
- **Native / multi-origin paths** (`origin:/path`, `cli:` text, prefix/path origin mismatch).
- **CLI paths** with `/` in interface names and mixed case: never split or case-folded.
- **Boolean and numeric Set values**.
- **Large payloads**: 64 MB send/receive message limits by default (stock pygnmi fails at 4 MB).
- **Session limits** (10 sessions per user): pooled mode keeps one peer; `max_concurrent` (default 8, per host:port:user) rejects with `busy` rather than exhausting the device.
- **Reboot subcomponents**.

Still needs a real device: Cisco IOS XR 25.4.2 behaviour, TLS/certificate renewal, and scale.

## Prerequisites

gNMI (and gNOI where you use it) enabled on the device and reachable from
Gateway 5. The code avoids 3.10+ syntax so it should run on the host's default `python3` (tested on 3.14 only, so pin dependencies if you are on 3.9); dependencies install from `requirements.txt` via the
service's `runtime.req-file`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The tests run against an in-process fake gNMI/gNOI device and cover direct and
pooled paths, including two separate processes sharing one device connection,
dead-channel recovery, no replay of non-idempotent operations, and deadline
cancellation.
