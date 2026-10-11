"""Mock gNMI + gNOI device with the behaviours that bit Gateway 4 on Cisco IOS XR.

Single source: copied verbatim into the dev stack (network/grpc/mock_device.py).

Per-device spec keys (all optional except name/port/username/password):
  gnmi_version   "0.10.0" (default) understands union_replace; "0.8.0" parses requests with the 0.8.0 schema,
                 so the unknown union_replace field is silently dropped, like an old server does
  interfaces     openconfig interface names to seed
  native         list of native YANG module origins accepted besides openconfig, e.g. Cisco-IOS-XR-ifmgr-cfg
  cli            {"command text": "output"}; served for origin "cli" (exact, case-sensitive, one element)
  max_sessions   max distinct client connections per user inside session_ttl seconds (0 = unlimited)
  session_ttl    seconds a silent connection still counts as a session (default 10)
  bulk_mb        adds /bulk with roughly that many MB of JSON (large payload tests)
  recv_mb        server receive limit in MB (default 64)

Behaviours (each mirrors a Jira-reported XR issue):
  - prefix.origin and path.origin that differ  -> INVALID_ARGUMENT "prefix and path origin do not match"
  - value bytes must be valid JSON             -> INVALID_ARGUMENT otherwise (booleans and numbers are valid)
  - an empty SetRequest                        -> INVALID_ARGUMENT
  - union_replace (gNMI 0.10): replaces the whole configuration with the union of its updates; operational state
    under surviving list entries is kept; overlapping union_replace paths or combining with `replace` is rejected
"""

import copy
import json
import re
import threading
import time
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection
from pygnmi.spec.v080 import gnmi_pb2 as pb080
from pygnmi.spec.v080 import gnmi_pb2_grpc  # noqa: F401  (registers gnmi.gNMI for reflection)

from gnoi.system import system_pb2
from grpc_driver.gnmi100 import gnmi_pb2 as pb100

ELEM_KEY = re.compile(r"^([^\[]+)((?:\[[^=\]]+=[^\]]*\])*)$")
OPENCONFIG_ORIGINS = ("", "openconfig")


def path_keys(path):
    out = []
    for e in path.elem:
        k = e.name
        for kk in sorted(e.key):
            k += f"[{kk}={e.key[kk]}]"
        out.append(k)
    return out


def merge(dst, src):
    if isinstance(dst, dict) and isinstance(src, dict):
        for k, v in src.items():
            dst[k] = merge(dst[k], v) if k in dst else v
        return dst
    return src


def graft_state(old, new):
    """After union_replace keep operational state of list entries that still exist."""
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return
    if "state" in old and "state" not in new and "config" in new:
        new["state"] = copy.deepcopy(old["state"])
    for k, v in new.items():
        if k in old:
            graft_state(old[k], v)


def initial_config(spec):
    interfaces = {}
    for i, name in enumerate(spec.get("interfaces", []), 1):
        interfaces[f"interface[name={name}]"] = {
            "config": {"name": name, "description": "", "enabled": True, "mtu": 1500},
            "state": {"oper-status": "UP", "counters": {"in-octets": 1000 * i, "out-octets": 900 * i}},
        }
    tree = {
        "system": {"config": {"hostname": spec.get("hostname", spec["name"]), "domain-name": "lab.itential.io"}},
        "interfaces": interfaces,
    }
    if spec.get("bulk_mb"):
        line = "x" * 1000
        tree["bulk"] = {"state": {"rows": [line] * int(spec["bulk_mb"] * 1024)}}
    return tree


class MockDevice:
    def __init__(self, spec):
        self.spec = spec
        self.name = spec["name"]
        self.username, self.password = spec["username"], spec["password"]
        self.pb = pb100 if spec.get("gnmi_version", "0.10.0") != "0.8.0" else pb080
        self.lock = threading.Lock()
        self.trees = {"openconfig": initial_config(spec)}
        for origin in spec.get("native") or []:
            self.trees[origin] = {}
        self.cli = spec.get("cli") or {}
        self.peers = []          # every call's TCP peer, in order (tests assert on connection reuse)
        self.sets = []           # every SetRequest as received
        self.sessions = {}       # peer -> last seen
        self.reboots = []
        self.get_delay = spec.get("get_delay", 0)
        self.inflight = 0
        self.max_inflight = 0

    # ------------------------------------------------------------------ plumbing
    def _auth(self, context, rpc):
        md = dict(context.invocation_metadata())
        peer = context.peer()
        with self.lock:
            self.peers.append(peer)
        if md.get("username") != self.username or md.get("password") != self.password:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad credentials")
        limit = int(self.spec.get("max_sessions") or 0)
        if limit:
            now, ttl = time.time(), float(self.spec.get("session_ttl", 10))
            with self.lock:
                self.sessions = {p: t for p, t in self.sessions.items() if now - t < ttl}
                if peer not in self.sessions and len(self.sessions) >= limit:
                    context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED,
                                  f"Maximum number of sessions ({limit}) for user '{self.username}' reached")
                self.sessions[peer] = now

    def _check_origins(self, prefix, path, context):
        if prefix.origin and path.origin and prefix.origin != path.origin:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "prefix and path origin do not match")

    def _tree_for(self, origin, context):
        origin = "openconfig" if origin in OPENCONFIG_ORIGINS else origin
        if origin not in self.trees:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"unsupported origin '{origin}'")
        return self.trees[origin]

    @staticmethod
    def _walk(tree, keys, create=False):
        node = tree
        for k in keys:
            if not isinstance(node, dict):
                return None
            if k not in node:
                if not create:
                    return None
                node[k] = {}
            node = node[k]
        return node

    # ---------------------------------------------------------------------- gNMI
    def Capabilities(self, request, context):
        self._auth(context, "Capabilities")
        pb = self.pb
        models = [pb.ModelData(name="openconfig-interfaces", organization="OpenConfig", version="2.4.3"),
                  pb.ModelData(name="openconfig-system", organization="OpenConfig", version="0.17.1")]
        models += [pb.ModelData(name=m, organization="Cisco Systems, Inc.", version="2025-01-01")
                   for m in self.spec.get("native") or []]
        return pb.CapabilityResponse(
            supported_models=models,
            supported_encodings=[pb.JSON_IETF, pb.JSON],
            gNMI_version=self.spec.get("gnmi_version", "0.10.0"),
        )

    def Get(self, request, context):
        self._auth(context, "Get")
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(self.get_delay)
            return self._get(request, context)
        finally:
            with self.lock:
                self.inflight -= 1

    def _get(self, request, context):
        pb = self.pb
        notifs = []
        for p in list(request.path) or [pb.Path()]:
            self._check_origins(request.prefix, p, context)
            origin = p.origin or request.prefix.origin
            if origin == "cli":
                notifs.append(self._cli(p, context))
                continue
            tree = self._tree_for(origin, context)
            keys = path_keys(request.prefix) + path_keys(p)
            with self.lock:
                node = self._walk(tree, keys)
                if node is None:
                    context.abort(grpc.StatusCode.NOT_FOUND, f"path not found: {keys}")
                if request.type == pb.GetRequest.CONFIG:
                    node = self._only(node, "config")
                elif request.type in (pb.GetRequest.STATE, pb.GetRequest.OPERATIONAL):
                    node = self._only(node, "state")
                body = json.dumps(node).encode()
            notifs.append(pb.Notification(
                timestamp=time.time_ns(),
                update=[pb.Update(path=p, val=pb.TypedValue(json_ietf_val=body))]))
        return pb.GetResponse(notification=notifs)

    @staticmethod
    def _only(node, kind):
        """Config-only or state-only view: keep `config` (or `state`) subtrees, drop the other."""
        drop = "state" if kind == "config" else "config"
        if not isinstance(node, dict):
            return node
        return {k: MockDevice._only(v, kind) for k, v in node.items() if k != drop}

    def _cli(self, path, context):
        pb = self.pb
        if len(path.elem) != 1:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          f"cli path must be one element holding the command, got {len(path.elem)}")
        command = path.elem[0].name
        if command not in self.cli:
            context.abort(grpc.StatusCode.NOT_FOUND, f"unknown command '{command}'")
        return pb.Notification(timestamp=time.time_ns(), update=[
            pb.Update(path=path, val=pb.TypedValue(ascii_val=self.cli[command]))])

    def Set(self, request, context):
        self._auth(context, "Set")
        pb = self.pb
        with self.lock:
            self.sets.append(request)
        union = list(getattr(request, "union_replace", []))
        if not (request.delete or request.replace or request.update or union):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "SetRequest contains no operations")
        if union and request.replace:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "union_replace cannot be combined with replace")
        for p in list(request.delete) + [u.path for u in list(request.replace) + list(request.update) + union]:
            self._check_origins(request.prefix, p, context)

        results = []
        with self.lock:
            staged = {o: copy.deepcopy(t) for o, t in self.trees.items()}
            live, self.trees = self.trees, staged
            try:
                for p in request.delete:
                    keys = path_keys(p)
                    tree = self._tree_for(p.origin or request.prefix.origin, context)
                    parent = self._walk(tree, path_keys(request.prefix) + keys[:-1])
                    if keys and isinstance(parent, dict):
                        parent.pop(keys[-1], None)
                    results.append(pb.UpdateResult(path=p, op=pb.UpdateResult.DELETE))
                if union:
                    self._union_replace(request, union, context)
                    results += [pb.UpdateResult(path=u.path, op=pb.UpdateResult.UNION_REPLACE) for u in union]
                for u in request.replace:
                    self._apply(request.prefix, u, True, context)
                    results.append(pb.UpdateResult(path=u.path, op=pb.UpdateResult.REPLACE))
                for u in request.update:
                    self._apply(request.prefix, u, False, context)
                    results.append(pb.UpdateResult(path=u.path, op=pb.UpdateResult.UPDATE))
            except ValueError as exc:
                self.trees = live
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return pb.SetResponse(timestamp=time.time_ns(), response=results)

    @staticmethod
    def _decode(update):
        tv = update.val
        kind = tv.WhichOneof("value")
        if kind in ("json_val", "json_ietf_val"):
            raw = getattr(tv, kind)
            try:
                return json.loads(raw.decode())
            except ValueError:
                raise ValueError(f"value is not valid JSON: {raw[:40]!r}")
        if kind in ("string_val", "int_val", "uint_val", "bool_val", "float_val", "ascii_val"):
            return getattr(tv, kind)
        raise ValueError("update carries no decodable value")

    def _apply(self, prefix, update, replace, context):
        tree = self._tree_for(update.path.origin or prefix.origin, context)
        keys = path_keys(prefix) + path_keys(update.path)
        value = self._decode(update)
        if not keys:
            if not isinstance(value, dict):
                raise ValueError("root value must be an object")
            new = value if replace else merge(tree, value)
            tree.clear()
            tree.update(new)
            return
        parent = self._walk(tree, keys[:-1], create=True)
        if not isinstance(parent, dict):
            raise ValueError(f"cannot set below a leaf: {keys}")
        leaf = keys[-1]
        parent[leaf] = value if replace or leaf not in parent else merge(parent[leaf], value)

    def _union_replace(self, request, updates, context):
        paths = [tuple(path_keys(request.prefix) + path_keys(u.path)) for u in updates]
        origins = [u.path.origin or request.prefix.origin for u in updates]
        for i, a in enumerate(paths):
            for j, b in enumerate(paths):
                if i < j and origins[i] == origins[j] and a[:len(b)] == b[:len(a)]:
                    raise ValueError("union_replace paths overlap")
        by_origin = {}
        for u, origin in zip(updates, origins):
            by_origin.setdefault("openconfig" if origin in OPENCONFIG_ORIGINS else origin, []).append(u)
        for origin, ups in by_origin.items():
            old = self._tree_for(origin, context)
            new = {}
            for u in ups:
                keys = path_keys(request.prefix) + path_keys(u.path)
                value = self._decode(u)
                if not keys:
                    new = merge(new, value) if isinstance(value, dict) else new
                    continue
                parent = self._walk(new, keys[:-1], create=True)
                parent[keys[-1]] = value
            graft_state(old, new)
            if self.spec.get("bulk_mb") and origin == "openconfig" and "bulk" in old:
                new.setdefault("bulk", old["bulk"])
            old.clear()
            old.update(new)

    # ---------------------------------------------------------------------- gNOI
    def Time(self, request, context):
        self._auth(context, "System.Time")
        return system_pb2.TimeResponse(time=time.time_ns())

    def Ping(self, request, context):
        self._auth(context, "System.Ping")
        count = request.count if request.count > 0 else 3
        for i in range(count):
            yield system_pb2.PingResponse(source=request.destination, time=500_000 + i * 1000, bytes=64, sequence=i, ttl=64)
        yield system_pb2.PingResponse(source=request.destination, sent=count, received=count,
                                      min_time=500_000, avg_time=501_000, max_time=502_000)

    def Traceroute(self, request, context):
        self._auth(context, "System.Traceroute")
        yield system_pb2.TracerouteResponse(destination_name=request.destination, destination_address=request.destination)
        yield system_pb2.TracerouteResponse(hop=1, address="10.0.0.1", name="gw", rtt=400_000)

    def Reboot(self, request, context):
        self._auth(context, "System.Reboot")
        with self.lock:
            self.reboots.append({
                "when": time.time_ns() + request.delay,
                "reason": request.message,
                "method": int(request.method),
                "subcomponents": [path_keys(p) for p in request.subcomponents],
            })
        return system_pb2.RebootResponse()

    def RebootStatus(self, request, context):
        self._auth(context, "System.RebootStatus")
        with self.lock:
            if not self.reboots:
                return system_pb2.RebootStatusResponse(active=False, count=0)
            r = self.reboots[-1]
            wait = max(0, r["when"] - time.time_ns())
            return system_pb2.RebootStatusResponse(active=wait > 0, wait=wait, when=r["when"],
                                                   reason=r["reason"], count=len(self.reboots))

    def CancelReboot(self, request, context):
        self._auth(context, "System.CancelReboot")
        with self.lock:
            self.reboots.clear()
        return system_pb2.CancelRebootResponse()


def serve(device, port=None, host="0.0.0.0"):
    """Start a server for one MockDevice. Returns (server, bound_port)."""
    mb = int(device.spec.get("recv_mb", 64))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16), options=[
        ("grpc.max_receive_message_length", mb * 1024 * 1024),
        ("grpc.max_send_message_length", -1),
    ])
    pb = device.pb

    def uu(fn, req, resp):
        return grpc.unary_unary_rpc_method_handler(fn, req.FromString, resp.SerializeToString)

    server.add_generic_rpc_handlers((
        grpc.method_handlers_generic_handler("gnmi.gNMI", {
            "Capabilities": uu(device.Capabilities, pb.CapabilityRequest, pb.CapabilityResponse),
            "Get": uu(device.Get, pb.GetRequest, pb.GetResponse),
            "Set": uu(device.Set, pb.SetRequest, pb.SetResponse),
        }),
        grpc.method_handlers_generic_handler("gnoi.system.System", {
            "Time": uu(device.Time, system_pb2.TimeRequest, system_pb2.TimeResponse),
            "Reboot": uu(device.Reboot, system_pb2.RebootRequest, system_pb2.RebootResponse),
            "RebootStatus": uu(device.RebootStatus, system_pb2.RebootStatusRequest, system_pb2.RebootStatusResponse),
            "CancelReboot": uu(device.CancelReboot, system_pb2.CancelRebootRequest, system_pb2.CancelRebootResponse),
            "Ping": grpc.unary_stream_rpc_method_handler(
                device.Ping, system_pb2.PingRequest.FromString, system_pb2.PingResponse.SerializeToString),
            "Traceroute": grpc.unary_stream_rpc_method_handler(
                device.Traceroute, system_pb2.TracerouteRequest.FromString, system_pb2.TracerouteResponse.SerializeToString),
        }),
    ))
    reflection.enable_server_reflection(["gnmi.gNMI", "gnoi.system.System", reflection.SERVICE_NAME], server)
    bound = server.add_insecure_port(f"{host}:{device.spec['port'] if port is None else port}")
    server.start()
    return server, bound


def make(name="mock", port=0, username="admin", password="admin", **extra):
    return MockDevice({"name": name, "port": port, "username": username, "password": password, **extra})
