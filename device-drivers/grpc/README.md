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
  - [gNMI Subscribe](#gnmi-subscribe)
  - [Config Manager (broker) services](#config-manager-broker-services)
  - [gNOI](#gnoi)
- [Connection pooling](#connection-pooling)
- [Invocation model](#invocation-model)
- [Inventory attributes](#inventory-attributes)
- [Extending the driver](#extending-the-driver)
- [Output and errors](#output-and-errors)
- [Prerequisites](#prerequisites)
- [Local development](#local-development)

## Operations

One Gateway 5 service per operation, 32 in total. `import.yaml` (or the repo-wide
`import.yml`) is checked against the Python operation registry by
`tests/test_service_manifest.py`, so a service without an operation, or an
operation without a service, fails the test suite.

### Connection and discovery

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-is-alive` | `is-alive` | Channel + gNMI Capabilities round trip. Prints bare `true` / `false`. |
| `grpc-gnmi-capabilities` | `capabilities` | gNMI Capabilities. |
| `grpc-reflection-call` | `call` | Any unary or server-streaming method via reflection: `method=gnoi.system.System/Time payload='{}'`. |
| `grpc-pool-status` / `grpc-pool-stop` | `pool-status` / `pool-stop` | Inspect or stop the pool daemon. |

### gNMI Get

Every Get takes `path` (repeatable), `encoding`, `prefix`, `target`, `origin` and `raw`.

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-gnmi-get` | `get` | gNMI Get with an explicit `datatype` (`all`, `config`, `state`, `operational`). |
| `grpc-gnmi-get-config` | `get-config` | Get type `CONFIG`. |
| `grpc-gnmi-get-state` | `get-state` | Get type `STATE`. |
| `grpc-gnmi-get-operational` | `get-operational` | Get type `OPERATIONAL`. |
| `grpc-gnmi-get-all` | `get-all` | Get type `ALL`. |

### gNMI Set

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-gnmi-set` | `set` | Generic Set: `update` / `replace` / `union_replace` as `[{path, value}]`, `delete` as `[path]`, plus `origin`, `raw`, `force`. Applied atomically by the device. `union_replace` (gNMI 0.10.0) is refused unless the device advertises 0.10.0+ (older targets silently ignore the field); `force=true` sends it anyway. |
| `grpc-gnmi-update-config` | `update-config` | Set with only `update[]` (merge). |
| `grpc-gnmi-replace-config` | `replace-config` | Set with only `replace[]`. |
| `grpc-gnmi-union-replace-config` | `union-replace-config` | Set with only `union_replace[]`. |
| `grpc-gnmi-delete-config` | `delete-config` | Set with only `delete[]`. |
| `grpc-gnmi-update-all` / `-replace-all` / `-union-replace-all` | `update-all` / `replace-all` / `union-replace-all` | The same verbs at the root path `/` with one `value`. **Require `confirm=true`**: a root replace rewrites the whole configuration, so the service refuses without it. |

The dedicated services accept only their own verb (plus `encoding`, `prefix`,
`target`, `origin`, `raw`, `force`), so a service named `update-config` can never
send a `replace`. None of the Set services are retried.

### gNMI Subscribe

Bounded by design: a `python-script` service has to finish, so nothing here
runs forever. Every Subscribe takes `path` (repeatable), `encoding`, `prefix`,
`target`, `origin`, `raw` and `updates_only`, and returns
`{"notification": [...], "sync_responses": n, "stop_reason": "..."}`.

| Service | `GRPC_OP` | Behaviour |
|---|---|---|
| `grpc-gnmi-subscribe-once` | `subscribe-once` | Mode `ONCE`: one snapshot, returned when the device sends `sync_response`. |
| `grpc-gnmi-subscribe-poll` | `subscribe-poll` | Mode `POLL`: sends `count` Poll messages `interval` seconds apart and returns after `count` `sync_response`s. |
| `grpc-gnmi-subscribe-stream` | `subscribe-stream` | Mode `STREAM`: collects for `duration` seconds (default 10) or `max_updates` notifications (default 1000), then cancels. `stream_mode` is `sample` (default), `on_change` or `target_defined`; also `sample_interval`, `heartbeat_interval`, `suppress_redundant`. |

`stop_reason` is `complete` (ONCE and POLL finished), `duration`, `max_updates`
or `device_closed`. `duration` is capped by `GRPC_SUBSCRIBE_MAX_DURATION`
(default 300 s) and `max_updates` at 100000. These operations legitimately
outlive `rpc_timeout`, so each declares its own deadline (`duration` +
`rpc_timeout` for a stream) instead of being cut off at 30 s; the pool daemon
waits for it too. A Subscribe holds one `max_concurrent` slot for its whole
run, so keep streams short on devices with a tight session limit. They are
marked idempotent: a dead channel is retried once.

POLL behaviour is device dependent. The driver expects one `sync_response` per
Poll and nothing for the initial request; the mock device behaves that way, but
this has not been checked against a real device.

### Config Manager (broker) services

Inventory Manager maps its `set-config` and `get-config` actions to services by
name, so these entry points keep their names, inputs and output shapes. The
dedicated gNMI services above sit beside them and never replace them. Do not
rename them: a service called `set-config` or `get-config` is what Config Manager
looks for. Backups and Golden Config use the `-text` pair.

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-gnmi-set-config` | `set-config` | Config Manager broker entry point. `config` is `[{parents, old, new}]`: `parents` joins into the path, `new` (JSON) is written, `old` with empty `new` deletes. `mode` = `update` (default), `replace` or `union_replace`; also `origin`, `force`. Returns `[{"result": true, ...}]` per change, like the Junos driver. |
| `grpc-gnmi-get-config-text` | `get-config-text` | Configuration as Junos-style brace text, for Config Manager backups and Golden Config. Returns bare text. |
| `grpc-gnmi-set-config-text` | `set-config-text` | Config Manager remediation with brace-text changes (`[{parents, old, new}]` with text in `old` / `new`). Same `mode`, `origin`, `force` and result shape as `set-config`. |
| `grpc-run-command` | `run-command` | Config Manager / Command Templates entry point. `command` is newline-separated. Each line is `gnmi capabilities`, `gnmi get <path>`, `gnoi <service.Method> [json]`, or anything else, which is sent as a CLI-origin gNMI Get (e.g. `show version` on IOS XR). Returns `{success, host, results:[{command, output, success}]}`; one failing command does not hide the others. |

### gNOI

| Service | `GRPC_OP` | Purpose |
|---|---|---|
| `grpc-gnoi-call` | `gnoi` | Any gNOI RPC by name: `rpc=system.Time`, `rpc=layer2.ClearSpanningTree request='{...}'`. Streaming RPCs return a list (capped at 1000 messages). |
| `grpc-gnoi-time` | `gnoi-time` | `system.Time`. |
| `grpc-gnoi-ping` | `gnoi-ping` | `system.Ping`: `destination` (required), `source`, `count`, `interval`, `wait`, `size`, `do_not_fragment`, `do_not_resolve`, `l3protocol`. Replies come back as a list. |
| `grpc-gnoi-traceroute` | `gnoi-traceroute` | `system.Traceroute`: `destination` (required), `source`, `initial_ttl`, `max_ttl`, `wait`, `do_not_fragment`, `do_not_resolve`, `l3protocol`, `l4protocol`. |
| `grpc-gnoi-reboot` | `gnoi-reboot` | `system.Reboot` (`method`, `delay` ns, `message`). Never retried. |
| `grpc-gnoi-reboot-status` | `gnoi-reboot-status` | `system.RebootStatus`. |
| `grpc-gnoi-cancel-reboot` | `gnoi-cancel-reboot` | `system.CancelReboot` (`message`). Not retried. |

The ping, traceroute and reboot services build the request from the protobuf
descriptor, so each field above is the proto field name. Anything nested goes in
`request` as JSON; explicit inputs win over `request`.

The gNOI RPCs that Gateway 4 exposed one-by-one (ping, traceroute, reboot,
cancel_reboot, reboot_status, time, clear_lldp_interface, clear_bgp_neighbor,
clear_interface_counters, clear_neighbor_discovery, clear_spanning_tree,
wake_on_lan, switch_control_processor) are all reachable through `gnoi`, which
reads the protobuf service descriptors instead of needing a wrapper per RPC.
`grpc-gnoi-call` with a bad `rpc` lists every valid name.

**Not supported: `system.SetPackage`.** It is a client-streaming upload (package
metadata, then a stream of chunks, then a hash). The driver sends client-streaming
RPCs as a single message, so a software upload through `gnoi` will not work. Use
the device's own tooling for image transfer.

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
| Dead channel | A call on a *reused* channel that fails with `UNAVAILABLE` / `UNAUTHENTICATED` evicts it and is retried once on a fresh one — **only for operations marked idempotent** (get, capabilities, is-alive, time, ...). `set`, `set-config`, the dedicated Set services, `gnoi-reboot` and anything else are never replayed. |
| Hung device | pygnmi sets no per-RPC deadline, so every operation runs under `rpc_timeout` (default 30 s); Subscribe operations declare a longer deadline of their own (see [gNMI Subscribe](#gnmi-subscribe)). On expiry the channel is closed, which cancels the in-flight RPC, and the channel is evicted. |
| Lifecycle | Idle channels close after `GRPC_POOL_IDLE_TTL` (300 s); channels are recycled after `GRPC_POOL_MAX_AGE` (3600 s); the daemon exits after `GRPC_POOL_DAEMON_IDLE` (600 s) with no channels; `GRPC_POOL_MAX_SIZE` (32) evicts least-recently-used idle channels. |
| Ambiguous failure | If the daemon dies *after* receiving a request, the call fails instead of falling back — a config push is never run twice. Fallback only happens when nothing was sent. |
| Code updates | The socket name includes a hash of the driver code, so a re-pulled repo gets a fresh daemon. Old ones idle out. |
| Security | Socket directory is `0700` and the socket `0600` (same uid only). Credentials sit in daemon memory for the life of a channel and are never written to disk. Logs go to `<socket>.log`. |

### Scope: per runner, not cross-server

**This is per-runner pooling, not a cross-server or cross-runner solution.**
The daemon is local to the host or container where the script executes, and
it is reached over a unix socket, so two runners can never share a channel.

| Gateway 5 topology | Pool | What a device sees |
|---|---|---|
| Single gateway, embedded runner | One daemon | 1 channel, up to `max_concurrent` operations in flight |
| Server plus separate runners (shared or physical hosts) | One daemon per runner host. The server executes nothing, so it holds no pool. | One channel per runner that touches the device |
| Virtual cluster, containerized runners | One daemon per runner container, lost when the container restarts or scales in (a cold reconnect, harmless) | One channel per runner replica |

Consequences:

- **Pooling still pays off.** Within one runner, repeat calls skip the TCP,
  TLS and Capabilities round trip, and a burst of calls shares one channel
  instead of opening one per call. Gateway 4 paid that cost on every request.
  The saving is smaller when devices are spread thinly across many runners,
  because each runner warms its own channel.
- **`max_concurrent` is per daemon, not global.** Devices cap sessions per
  user across all runners. With N runners that can reach a device, it can see
  up to N × `max_concurrent` operations in flight. Set `max_concurrent` to the
  device's limit divided by N.
- **Runner placement decides the hit rate.** If Gateway 5 spreads one device's
  jobs across runners, each runner opens its own channel and the device sees
  one source per runner. Pin devices to runners where you can.
- **No coordination between runners.** There is no shared state, lock or
  global session budget. If you need one, put a shared proxy in front of the
  devices instead of extending the pool.
- Tune `GRPC_POOL_*` on each runner. Keep `GRPC_POOL_IDLE_TTL` below any idle
  timeout on the device or a firewall.

### Limits to know about

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
    "itential_port": 22,
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
        "pool": "auto",
        "max_concurrent": 8
      }
    }
  }
}
```

### Ports: `itential_port` and the gRPC port

One inventory node can be reached over more than one protocol, depending on
which actions it is assigned. For example, Config Manager backups may use
`netmiko` (SSH, port 22) for some actions while `get-config` and `set-config`
use this driver (gNMI, typically 57400). Those protocols listen on different
ports, so a node can carry two:

- **`itential_port`** is the standard attribute shared by every driver on the
  node. Config Manager shows it in the Port field. For a node that is also
  managed over SSH it is usually `22`.
- **`itential_driver_options.grpc.port`** is the gRPC port for this driver only.

The driver picks the first of these that is set:

1. `--port` (local testing)
2. `itential_driver_options.grpc.port`
3. `itential_port`
4. `57400`

So set `grpc.port` whenever the node is shared with another protocol. If the
node is used only through this driver, `itential_port` alone is enough and
`grpc.port` can be left out. If `grpc.port` is missing on a shared node, the
driver will use the SSH port from `itential_port` and fail to connect.

The pool settings (`pool`, `max_concurrent`) are per device and are described
under [Connection pooling](#connection-pooling).

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

The test-suite runs against `tests/mock_device.py`, a stateful gNMI/gNOI device (including Subscribe ONCE / POLL / STREAM) that can be configured to behave like the problem devices from support cases. Each of these has a regression test in `tests/test_regressions.py`. Subscribe, the dedicated services and the Config Manager contract are covered in `tests/test_services.py`:

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
pip install -r requirements.txt pytest pyyaml
python -m pytest tests -q
```

`pyyaml` is only needed by `tests/test_service_manifest.py` (it is skipped
without it); the driver itself does not import it.

The tests run against an in-process fake gNMI/gNOI device and cover direct and
pooled paths, including two separate processes sharing one device connection,
dead-channel recovery, no replay of non-idempotent operations, and deadline
cancellation.
