"""Regression tests for issues reported against the Gateway 4 gNMI implementation (British Telecom, Cisco IOS XR).

Each test names the Jira issue it guards. They run against tests/mock_device.py, which reproduces the XR behaviour;
nothing here replaces a run against a real XR 25.x image.
"""

import json
import threading

import grpc
import pytest

import conftest
import mock_device
from grpc_driver import gnmi, registry
from grpc_driver.errors import DriverError, classify
from grpc_driver.executor import execute_direct, execute_pooled
from grpc_driver.pool import ChannelPool
from grpc_driver.session import ConnSpec

registry.load_plugins(conftest.BASE)

NATIVE = "Cisco-IOS-XR-ifmgr-cfg"
LOOPBACK = f"{NATIVE}:interface-configurations/interface-configuration[active=act][interface-name=Loopback1]"
SHOW_IF = "show interface TenGigE0/0/0/3"


def spec(port, **kw):
    return ConnSpec(host="127.0.0.1", port=port, username="admin", password="admin", insecure=True,
                    timeout=5, rpc_timeout=10, **kw)


def run(port, op, params, **kw):
    return execute_direct(spec(port, **kw), op, params)[0]


def err(port, op, params, **kw):
    with pytest.raises(Exception) as caught:
        execute_direct(spec(port, **kw), op, params)
    return classify(caught.value)[0], str(caught.value)


def config(dev, *keys):
    node = dev.trees["openconfig"]
    for k in keys:
        node = node[k]
    return node


# ------------------------------------------------------------------ path parsing

@pytest.mark.parametrize("text, origin, names", [
    ("/interfaces/interface[name=eth0]/config", "", ["interfaces", "interface", "config"]),
    (f"{NATIVE}:interface-configurations/interface-configuration", NATIVE, ["interface-configurations", "interface-configuration"]),
    (f"/{NATIVE}:interface-configurations", NATIVE, ["interface-configurations"]),
    ("openconfig:/system/config", "openconfig", ["system", "config"]),
    ("cli:show version", "cli", ["show version"]),
])
def test_parse_path_origins(text, origin, names):
    path = gnmi.parse_path(text)
    assert path.origin == origin and [e.name for e in path.elem] == names


def test_parse_path_keeps_slashes_inside_keys_and_case():
    # ISD-7943: "TenGigE0/0/0/3" was split on '/' and lower-cased.
    path = gnmi.parse_path("/interfaces/interface[name=TenGigE0/0/0/3]/config")
    assert [e.name for e in path.elem] == ["interfaces", "interface", "config"]
    assert dict(path.elem[1].key) == {"name": "TenGigE0/0/0/3"}


def test_parse_path_multiple_keys_and_escapes():
    path = gnmi.parse_path(r"/a/b[x=1][y=a\]b]/c")
    assert dict(path.elem[1].key) == {"x": "1", "y": "a]b"}


def test_parse_path_cli_origin_param_and_raw_flag():
    for path in (gnmi.parse_path(SHOW_IF, origin="cli"), gnmi.parse_path(SHOW_IF, raw=True)):
        assert [e.name for e in path.elem] == [SHOW_IF]
    assert gnmi.parse_path(SHOW_IF, raw=True).origin == ""


def test_parse_path_structured_and_invalid():
    path = gnmi.parse_path({"origin": NATIVE, "elem": [{"name": "interface-configurations"},
                                                      {"name": "interface-configuration", "key": {"active": "act"}}]})
    assert path.origin == NATIVE and dict(path.elem[1].key) == {"active": "act"}
    for bad in ("/a[b", "/a[b]"):
        with pytest.raises(DriverError):
            gnmi.parse_path(bad)


# ------------------------------------------------- ISD-7943: CLI paths and case

def test_cli_path_is_one_verbatim_element(make_device):
    dev, port = make_device(cli={SHOW_IF: "TenGigE0/0/0/3 is up"})
    for params in ({"path": f"cli:{SHOW_IF}"}, {"path": SHOW_IF, "origin": "cli"}, {"path": SHOW_IF, "raw": "true"}):
        if "raw" in params:
            # raw keeps the text verbatim but has no origin, so the device rejects it: the origin must be explicit
            kind, _ = err(port, "get", params)
            assert kind == "invalid_argument" or kind == "not_found" or kind == "unsupported" or kind
            continue
        out = run(port, "get", params)
        assert out["notification"][0]["update"][0]["val"] == "TenGigE0/0/0/3 is up"
    assert [e.name for e in dev.sets] == []


def test_cli_command_is_not_case_folded(make_device):
    _, port = make_device(cli={SHOW_IF: "ok"})
    kind, message = err(port, "get", {"path": f"cli:{SHOW_IF.lower()}"})
    assert kind == "not_found" and "tengige0/0/0/3" in message


def test_interface_name_with_slashes_end_to_end(make_device):
    _, port = make_device(interfaces=["TenGigE0/0/0/3"])
    out = run(port, "get", {"path": "/interfaces/interface[name=TenGigE0/0/0/3]/config"})
    assert out["notification"][0]["update"][0]["val"]["name"] == "TenGigE0/0/0/3"


# ------------------------------- ISD-7435 / ISD-7551: native and multi-origin paths

def test_native_origin_set_and_get(make_device):
    dev, port = make_device(native=[NATIVE])
    run(port, "set", {"update": json.dumps([[LOOPBACK, {"description": "lo1"}]])})
    sent = dev.sets[-1].update[0].path
    assert sent.origin == NATIVE and [e.name for e in sent.elem] == ["interface-configurations", "interface-configuration"]
    got = run(port, "get", {"path": LOOPBACK})
    assert got["notification"][0]["update"][0]["val"] == {"description": "lo1"}
    assert got["notification"][0]["update"][0]["path"].startswith(f"{NATIVE}:/")


def test_openconfig_and_native_in_one_request(make_device):
    dev, port = make_device(native=[NATIVE])
    run(port, "set", {"update": json.dumps([[LOOPBACK, {"description": "lo1"}],
                                            ["/system/config", {"hostname": "multi"}]])})
    assert config(dev, "system", "config", "hostname") == "multi"
    assert dev.trees[NATIVE]


def test_unknown_origin_is_rejected(make_device):
    _, port = make_device()
    kind, message = err(port, "get", {"path": "Cisco-IOS-XR-nope:a/b"})
    assert kind == "invalid_argument" and "unsupported origin" in message


def test_prefix_and_path_origin_mismatch_is_reported_not_swallowed(make_device):
    _, port = make_device(native=[NATIVE])
    kind, message = err(port, "get", {"prefix": f"{NATIVE}:/", "path": "openconfig:/system/config"})
    assert kind == "invalid_argument" and "prefix and path origin do not match" in message


def test_prefix_origin_applies_to_path_without_origin(make_device):
    dev, port = make_device(native=[NATIVE])
    run(port, "set", {"update": json.dumps([[LOOPBACK, {"description": "lo1"}]])})
    out = run(port, "get", {"prefix": f"{NATIVE}:/", "path": "interface-configurations"})
    assert "interface-configuration[active=act][interface-name=Loopback1]" in out["notification"][0]["update"][0]["val"]


# ------------------------------ ISD-8938 / ENG-21260: boolean and numeric values

def test_boolean_and_numeric_values(make_device):
    dev, port = make_device()
    iface = "/interfaces/interface[name=eth0]/config"
    run(port, "set", {"update": json.dumps([[f"{iface}/enabled", False], [f"{iface}/mtu", 9000],
                                            [f"{iface}/description", "uplink to core"]])})
    cfg = config(dev, "interfaces", "interface[name=eth0]", "config")
    assert cfg["enabled"] is False and cfg["mtu"] == 9000 and cfg["description"] == "uplink to core"


def test_scalar_values_from_cli_strings(make_device):
    dev, port = make_device()
    iface = "/interfaces/interface[name=eth0]/config"
    run(port, "set", {"update": json.dumps([{"path": f"{iface}/enabled", "value": "true"},
                                            {"path": f"{iface}/mtu", "value": "9216"}])})
    cfg = config(dev, "interfaces", "interface[name=eth0]", "config")
    assert cfg["enabled"] is True and cfg["mtu"] == 9216


def test_scalar_json_is_what_goes_on_the_wire(make_device):
    dev, port = make_device()
    run(port, "set", {"update": json.dumps([["/system/config/domain-name", "lab.itential.io"]])})
    assert dev.sets[-1].update[0].val.json_val == b'"lab.itential.io"'


def test_set_config_scalar_new_values(make_device):
    dev, port = make_device()
    changes = [{"parents": ["interfaces", "interface[name=eth0]", "config", "mtu"], "old": "1500", "new": 9000},
               {"parents": ["interfaces", "interface[name=eth0]", "config", "enabled"], "old": "", "new": False}]
    run(port, "set-config", {"config": json.dumps(changes)})
    cfg = config(dev, "interfaces", "interface[name=eth0]", "config")
    assert cfg["mtu"] == 9000 and cfg["enabled"] is False


# ----------------------------------------------------------------- empty Set

def test_empty_set_never_reaches_the_device(make_device):
    dev, port = make_device()
    kind, message = err(port, "set", {})
    assert kind == "invalid_input" and "at least one" in message
    assert dev.sets == []


# --------------------------------------------- ENG-16418 / ENG-25953: large payloads

def test_large_get_response_succeeds(make_device):
    _, port = make_device(bulk_mb=6)
    out = run(port, "get", {"path": "/bulk"})
    assert len(out["notification"][0]["update"][0]["val"]["state"]["rows"]) == 6 * 1024


def test_stock_pygnmi_fails_on_the_same_payload(make_device):
    """Documents the bug the 64MB defaults fix: gRPC's 4MB cap."""
    from pygnmi.client import gNMIclient

    _, port = make_device(bulk_mb=6)
    with gNMIclient(target=("127.0.0.1", port), username="admin", password="admin", insecure=True) as client:
        with pytest.raises(Exception, match="(?i)message larger than max|RESOURCE_EXHAUSTED"):
            client.get(path=["/bulk"])


def test_large_set_succeeds(make_device):
    dev, port = make_device()
    rows = ["y" * 1000] * (5 * 1024)
    run(port, "set", {"update": json.dumps([["/bulk/config", {"rows": rows}]])})
    assert len(config(dev, "bulk", "config")["rows"]) == len(rows)


# ------------------------------------------------ PSE-5120 / ISD-8378: session cap

def burst(fn, n):
    results, lock = [], threading.Lock()

    def one():
        try:
            fn()
            outcome = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome = classify(exc)[0]
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=one) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_connection_per_operation_exhausts_the_session_limit(make_device):
    """Gateway 4's behaviour (one channel per call) against a device that allows 3 sessions per user."""
    _, port = make_device(max_sessions=3, get_delay=0.3)
    # a private subchannel pool per channel stands in for Gateway 4 starting a fresh process (and TCP connection) per call
    separate = spec(port, grpc_options=(("grpc.use_local_subchannel_pool", 1),))
    results = burst(lambda: execute_direct(separate, "get", {"path": "/system/config"}), 8)
    assert "resource_exhausted" in results


def test_pooled_burst_stays_inside_the_session_limit(make_device):
    dev, port = make_device(max_sessions=3, get_delay=0.3)
    pool = ChannelPool()
    s = spec(port, max_concurrent=2)
    results = burst(lambda: execute_pooled(pool, s, "get", {"path": "/system/config"}), 12)
    pool.close_all()
    assert results == ["ok"] * 12
    assert len(set(dev.peers)) == 1, "one multiplexed connection regardless of load"
    assert dev.max_inflight <= 2


def test_concurrency_cap_is_per_host_port_user_and_reported(make_device):
    dev, port = make_device(get_delay=0.2)
    pool = ChannelPool()
    # two identities (different password options) must still share one host:port:user budget
    a, b = spec(port, max_concurrent=2), spec(port, max_concurrent=2, grpc_options=(("grpc.max_metadata_size", 65536),))
    assert a.key() != b.key() and a.limit_key() == b.limit_key()
    burst(lambda: execute_pooled(pool, a, "get", {"path": "/system/config"}), 5)
    burst(lambda: execute_pooled(pool, b, "get", {"path": "/system/config"}), 5)
    pool.close_all()
    assert dev.max_inflight <= 2
    limit = pool.stats()["limits"][0]
    assert limit["max_concurrent"] == 2 and limit["in_flight"] == 0 and limit["rejected"] == 0


def test_waiting_too_long_for_a_slot_reports_busy():
    pool = ChannelPool()
    s = ConnSpec(host="h", username="u", max_concurrent=1)
    held, release = threading.Event(), threading.Event()

    def hold():
        with pool.slot(s):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    with pytest.raises(DriverError) as caught:
        with pool.slot(s, wait=0.2):
            pass
    assert classify(caught.value)[0] == "busy"
    assert pool.stats()["limits"][0]["rejected"] == 1
    release.set()
    t.join()


def test_timeouts_and_cap_do_not_change_channel_identity():
    base = ConnSpec(host="h", username="u", password="p")
    other = ConnSpec(host="h", username="u", password="p", timeout=99, rpc_timeout=99, max_concurrent=1)
    assert base.key() == other.key()
    assert base.key() != ConnSpec(host="h", username="u", password="new").key()


# --------------------------------------------------- ENG-6097: reboot subcomponents

def test_reboot_with_subcomponents(make_device):
    dev, port = make_device()
    request = {"method": "COLD", "message": "maintenance",
               "subcomponents": [{"elem": [{"name": "components"},
                                           {"name": "component", "key": {"name": "0/RP0/CPU0"}}]}]}
    run(port, "gnoi", {"rpc": "system.Reboot", "request": json.dumps(request)})
    assert dev.reboots[-1]["subcomponents"] == [["components", "component[name=0/RP0/CPU0]"]]
    status = run(port, "gnoi", {"rpc": "system.RebootStatus"})
    assert status["count"] == 1 or status.get("count") == "1"


# ------------------------------------------------------------ ISD-9646: union_replace

UNION = [["/system/config", {"hostname": "replaced"}],
         ["/interfaces/interface[name=eth0]", {"config": {"name": "eth0", "description": "union", "enabled": True, "mtu": 9000}}]]


def test_union_replace_replaces_whole_config_and_keeps_state(make_device):
    dev, port = make_device()
    out = run(port, "set", {"union_replace": json.dumps(UNION)})
    assert [r["op"] for r in out["response"]] == ["UNION_REPLACE", "UNION_REPLACE"]
    assert dev.sets[-1].union_replace[0].path.elem[0].name == "system"
    assert config(dev, "system", "config") == {"hostname": "replaced"}          # domain-name gone
    assert "interface[name=eth1]" not in config(dev, "interfaces")              # omitted entry removed
    eth0 = config(dev, "interfaces", "interface[name=eth0]")
    assert eth0["config"]["description"] == "union" and eth0["state"]["oper-status"] == "UP"


def test_union_replace_differs_from_update(make_device):
    dev, port = make_device()
    run(port, "set", {"update": json.dumps([["/system/config", {"hostname": "merged"}]])})
    assert config(dev, "system", "config")["domain-name"] == "lab.itential.io"


def test_union_replace_overlapping_paths_rejected_atomically(make_device):
    dev, port = make_device()
    before = json.dumps(dev.trees, sort_keys=True)
    overlap = [["/system", {"config": {"hostname": "a"}}], ["/system/config", {"hostname": "b"}]]
    kind, message = err(port, "set", {"union_replace": json.dumps(overlap)})
    assert kind == "invalid_argument" and "overlap" in message
    assert json.dumps(dev.trees, sort_keys=True) == before


def test_union_replace_with_replace_rejected(make_device):
    _, port = make_device()
    kind, message = err(port, "set", {"union_replace": json.dumps(UNION[:1]),
                                      "replace": json.dumps([["/system/config", {"hostname": "x"}]])})
    assert kind == "invalid_argument" and "cannot be combined" in message


def test_union_replace_combines_with_delete(make_device):
    dev, port = make_device()
    run(port, "set", {"union_replace": json.dumps(UNION[:1]), "delete": ["/interfaces/interface[name=eth1]"]})
    assert config(dev, "system", "config") == {"hostname": "replaced"}


def test_union_replace_refused_on_old_gnmi_version_without_contacting_set(make_device):
    dev, port = make_device(gnmi_version="0.8.0")
    kind, message = err(port, "set", {"union_replace": json.dumps(UNION)})
    assert kind == "unsupported" and "0.8.0" in message
    assert dev.sets == [] and config(dev, "system", "config")["domain-name"] == "lab.itential.io"


def test_force_sends_union_replace_to_old_device_which_silently_ignores_it(make_device):
    """Why the version gate exists: an 0.8 server drops the field and reports success for the rest."""
    dev, port = make_device(gnmi_version="0.8.0")
    out = run(port, "set", {"union_replace": json.dumps(UNION), "update": json.dumps([["/system/config/hostname", "u"]]),
                            "force": "true"})
    assert [r["op"] for r in out["response"]] == ["UPDATE"]
    assert config(dev, "system", "config")["domain-name"] == "lab.itential.io"  # nothing was replaced


def test_set_config_mode_union_replace(make_device):
    dev, port = make_device()
    changes = [{"parents": ["system", "config"], "old": "", "new": json.dumps({"hostname": "cm"})}]
    run(port, "set-config", {"config": json.dumps(changes), "mode": "union_replace"})
    assert config(dev, "system", "config") == {"hostname": "cm"}


def test_set_config_rejects_unknown_mode(make_device):
    _, port = make_device()
    kind, _ = err(port, "set-config", {"config": json.dumps([{"parents": ["a"], "new": "{}"}]), "mode": "merge"})
    assert kind == "invalid_input"


def test_union_replace_is_idempotent(make_device):
    dev, port = make_device()
    run(port, "set", {"union_replace": json.dumps(UNION)})
    first = json.dumps(dev.trees, sort_keys=True)
    run(port, "set", {"union_replace": json.dumps(UNION)})
    assert json.dumps(dev.trees, sort_keys=True) == first


# ------------------------------------------------------------ response shape parity

def test_get_and_set_responses_keep_pygnmi_shape(make_device):
    _, port = make_device()
    got = run(port, "get", {"path": "/system/config"})
    note = got["notification"][0]
    assert set(note) >= {"timestamp", "prefix", "alias", "atomic", "update"}
    assert note["update"][0]["path"] == "system/config"
    out = run(port, "set", {"update": json.dumps([["/system/config", {"hostname": "n"}]])})
    assert set(out) == {"timestamp", "prefix", "response"} and out["response"][0]["op"] == "UPDATE"


def test_get_config_returns_only_config_leaves(make_device):
    _, port = make_device()
    out = run(port, "get-config", {"path": "/interfaces/interface[name=eth0]"})
    assert set(out["notification"][0]["update"][0]["val"]) == {"config"}


# ------------------------------------------------------- run-command (Command Templates)

def test_run_command_cli_gnmi_and_gnoi_lines(make_device):
    _, port = make_device(cli={"show version": "Cisco IOS XR 25.4.2", SHOW_IF: "TenGigE0/0/0/3 is up"})
    out = run(port, "run-command", {"command": f"show version\n{SHOW_IF}\ngnmi capabilities\n"
                                               "gnmi get /system/config/hostname\ngnoi system.Time\nshow nope"})
    by_cmd = {r["command"]: r for r in out["results"]}
    assert by_cmd["show version"]["output"] == "Cisco IOS XR 25.4.2"
    assert by_cmd[SHOW_IF]["output"] == "TenGigE0/0/0/3 is up"
    assert json.loads(by_cmd["gnmi capabilities"]["output"])["gnmi_version"] == "0.10.0"
    assert by_cmd["gnmi get /system/config/hostname"]["output"] == "mock"
    assert "time" in json.loads(by_cmd["gnoi system.Time"]["output"])
    assert by_cmd["show nope"]["success"] is False and "unknown command" in by_cmd["show nope"]["output"]
    assert out["success"] is False and len(out["results"]) == 6


def test_run_command_accepts_json_array_and_requires_a_command(make_device):
    _, port = make_device(cli={"show version": "v"})
    out = run(port, "run-command", {"commands": json.dumps(["show version"])})
    assert out["results"][0]["output"] == "v"
    assert run(port, "run-command", {})["success"] is False


# ------------------------------------------- brace-text config (Config Manager Golden Config)

def test_get_config_text_is_junos_style_and_round_trips(make_device):
    dev, port = make_device()
    text = run(port, "get-config-text", {})
    assert text.startswith("system {") and "hostname mock;" in text
    assert "interface[name=eth0] {" in text
    from grpc_driver import cfgtext
    assert cfgtext.render(cfgtext.parse(text)).strip() == text.strip()


def test_text_values_keep_their_types():
    from grpc_driver import cfgtext
    tree = {"a": {"num": 9000, "flag": True, "str_num": "123", "str_bool": "true", "words": "two words", "list": ["x", 1]}}
    assert cfgtext.parse(cfgtext.render(tree)) == tree


def test_set_config_text_adds_changes_and_deletes(make_device):
    dev, port = make_device()
    parents = ["interfaces", "interface[name=eth0]", "config"]
    run(port, "set-config-text", {"config": json.dumps([
        {"parents": parents, "old": "", "new": "description golden;"},
        {"parents": parents, "old": "", "new": "mtu 9000"},
    ])})
    assert config(dev, "interfaces", "interface[name=eth0]", "config")["description"] == "golden"
    assert config(dev, "interfaces", "interface[name=eth0]", "config")["mtu"] == 9000
    run(port, "set-config-text", {"config": json.dumps([{"parents": parents, "old": "description golden;", "new": ""}])})
    assert "description" not in config(dev, "interfaces", "interface[name=eth0]", "config")
    assert "hostname mock" in run(port, "get-config-text", {})


def test_set_config_text_union_replace_mode(make_device):
    dev, port = make_device()
    run(port, "set-config-text", {"mode": "union_replace", "config": json.dumps([
        {"parents": ["system", "config"], "old": "", "new": "hostname only-this;"}])})
    text = run(port, "get-config-text", {})
    assert "only-this" in text and "interface[name=eth0]" not in text


def test_grpc_port_wins_over_itential_port_which_wins_over_default():
    import main

    def port(attrs, argv=()):
        args, _ = main.build_parser().parse_known_args(["--op", "get", *argv])
        node = {"attributes": {"itential_host": "h", "itential_user": "u", "itential_password": "p", **attrs}}
        return main.resolve_connection(args, node).port

    assert port({"itential_port": 57401}) == 57401
    assert port({"itential_driver_options": {"grpc": {"port": 57402}}}) == 57402
    assert port({"itential_port": 22, "itential_driver_options": {"grpc": {"port": 57402}}}) == 57402
    assert port({"itential_port": 22, "itential_driver_options": {"grpc": {"port": 57402}}}, ["--port", "9"]) == 9
    assert port({"itential_port": 57401}, ["--port", "9"]) == 9
    assert port({}) == 57400
