"""Subscribe, the dedicated gNMI/gNOI services, and the Config Manager contract that must not drift."""

import json

import pytest

import conftest
from grpc_driver import registry
from grpc_driver.errors import classify
from grpc_driver.executor import execute_direct
from grpc_driver.session import ConnSpec

registry.load_plugins(conftest.BASE)

ONCE, POLL = 1, 2
IFACE = "/interfaces/interface[name=eth0]"


def spec(port, **kw):
    return ConnSpec(host="127.0.0.1", port=port, username="admin", password="admin", insecure=True,
                    timeout=5, rpc_timeout=kw.pop("rpc_timeout", 5), **kw)


def run(port, op, params, **kw):
    return execute_direct(spec(port, **kw), op, params)[0]


def fails(port, op, params):
    with pytest.raises(Exception) as caught:
        execute_direct(spec(port), op, params)
    return classify(caught.value)[0], str(caught.value)


def iface(dev, name="eth0"):
    return dev.trees["openconfig"]["interfaces"][f"interface[name={name}]"]


# ----------------------------------------------------------------- Config Manager contract

def test_config_manager_text_ops_keep_their_broker_contract(make_device):
    dev, port = make_device()
    assert isinstance(run(port, "get-config-text", {}), str)
    out = run(port, "set-config-text", {"config": json.dumps([
        {"parents": ["interfaces", "interface[name=eth0]", "config"], "old": "", "new": "description cm;"}])})
    assert isinstance(out, list) and out[0]["result"] is True
    assert iface(dev)["config"]["description"] == "cm"


def test_set_config_keeps_its_broker_contract(make_device):
    dev, port = make_device()
    out = run(port, "set-config", {"config": json.dumps([
        {"parents": ["interfaces", "interface[name=eth0]", "config"], "old": "", "new": {"description": "x"}}])})
    assert out[0]["result"] is True and iface(dev)["config"]["description"] == "x"


# ----------------------------------------------------------------- subscribe

def test_subscribe_once_returns_snapshot_and_sync(make_device):
    dev, port = make_device()
    out = run(port, "subscribe-once", {"path": IFACE})
    assert out["stop_reason"] == "complete" and out["sync_responses"] == 1
    assert len(out["notification"]) == 1 and dev.subscribes[0].mode == ONCE


def test_subscribe_once_missing_path_is_a_not_found_error(make_device):
    _, port = make_device()
    code, _ = fails(port, "subscribe-once", {"path": "/nope"})
    assert code


def test_subscribe_poll_sends_one_poll_per_count(make_device):
    dev, port = make_device()
    out = run(port, "subscribe-poll", {"path": IFACE, "count": 3, "interval": 0.05})
    assert out["stop_reason"] == "complete" and out["sync_responses"] == 3
    assert len(out["notification"]) == 3 and dev.polls == 3 and dev.subscribes[0].mode == POLL


def test_subscribe_stream_stops_at_duration(make_device):
    dev, port = make_device()
    out = run(port, "subscribe-stream", {"path": IFACE, "duration": 0.6, "sample_interval": 0.1, "max_updates": 1000})
    assert out["stop_reason"] == "duration"
    assert 2 <= len(out["notification"]) <= 12


def test_subscribe_stream_stops_at_max_updates(make_device):
    _, port = make_device()
    out = run(port, "subscribe-stream", {"path": IFACE, "duration": 10, "sample_interval": 0.05, "max_updates": 3})
    assert out["stop_reason"] == "max_updates" and len(out["notification"]) == 3


def test_subscribe_stream_on_change_sees_a_set(make_device):
    import threading
    import time
    dev, port = make_device()

    def change():
        time.sleep(0.4)
        run(port, "update-config", {"update": json.dumps([{"path": f"{IFACE}/config", "value": {"description": "hit"}}])})

    t = threading.Thread(target=change)
    t.start()
    out = run(port, "subscribe-stream", {"path": IFACE, "stream_mode": "on_change", "duration": 1.5, "updates_only": False})
    t.join()
    bodies = json.dumps(out["notification"])
    assert out["stop_reason"] == "duration" and "hit" in bodies


def test_subscribe_stream_is_bounded(make_device, monkeypatch):
    _, port = make_device()
    monkeypatch.setenv("GRPC_SUBSCRIBE_MAX_DURATION", "5")
    code, msg = fails(port, "subscribe-stream", {"path": IFACE, "duration": 60})
    assert "duration" in msg
    code, msg = fails(port, "subscribe-stream", {"path": IFACE, "stream_mode": "bogus", "duration": 1})
    assert "stream_mode" in msg


def test_subscribe_stream_deadline_covers_duration(make_device):
    _, port = make_device()
    s = spec(port, rpc_timeout=2)
    assert registry.deadline_for("subscribe-stream", {"duration": 20}, s) == 22
    assert registry.deadline_for("subscribe-poll", {"count": 4, "interval": 2}, s) == 10
    assert registry.deadline_for("get", {}, s) == 2


def test_subscribe_stream_through_the_pool_outlives_rpc_timeout(make_device):
    from grpc_driver.executor import execute_pooled
    from grpc_driver.pool import ChannelPool
    _, port = make_device()
    s = spec(port, rpc_timeout=1)
    out, _ = execute_pooled(ChannelPool(), s, "subscribe-stream",
                            {"path": IFACE, "duration": 1.8, "sample_interval": 0.2})
    assert out["stop_reason"] == "duration" and len(out["notification"]) >= 3


# ----------------------------------------------------------------- dedicated gNMI services

def test_get_state_and_get_all_and_get_operational(make_device):
    _, port = make_device()
    state = json.dumps(run(port, "get-state", {"path": IFACE}))
    assert "oper-status" in state and "description" not in state
    assert "oper-status" in json.dumps(run(port, "get-operational", {"path": IFACE}))
    both = json.dumps(run(port, "get-all", {"path": IFACE}))
    assert "oper-status" in both and "mtu" in both


def test_update_replace_union_delete_config(make_device):
    dev, port = make_device()
    cfg = f"{IFACE}/config"
    run(port, "update-config", {"update": json.dumps([{"path": cfg, "value": {"description": "u"}}])})
    assert iface(dev)["config"]["description"] == "u" and iface(dev)["config"]["mtu"] == 1500
    run(port, "replace-config", {"replace": json.dumps([{"path": cfg, "value": {"name": "eth0", "mtu": 9000}}])})
    assert iface(dev)["config"] == {"name": "eth0", "mtu": 9000}
    run(port, "union-replace-config", {"union_replace": json.dumps([
        {"path": "/system/config", "value": {"hostname": "only"}}])})
    assert list(dev.trees["openconfig"]) == ["system"]
    run(port, "delete-config", {"delete": "/system/config/hostname"})
    assert "hostname" not in dev.trees["openconfig"]["system"]["config"]
    for op, key in (("update-config", "update"), ("replace-config", "replace"),
                    ("union-replace-config", "union_replace"), ("delete-config", "delete")):
        assert key in fails(port, op, {})[1]


def test_dedicated_set_services_only_send_their_own_verb(make_device):
    dev, port = make_device()
    run(port, "update-config", {"update": json.dumps([{"path": f"{IFACE}/config", "value": {"description": "v"}}]),
                                "replace": json.dumps([{"path": "/system/config", "value": {"hostname": "no"}}])})
    sent = dev.sets[-1]
    assert len(sent.update) == 1 and len(sent.replace) == 0


@pytest.mark.parametrize("op, key", [("update-all", "update"), ("replace-all", "replace"),
                                      ("union-replace-all", "union_replace")])
def test_all_services_require_confirm(make_device, op, key):
    dev, port = make_device()
    before = list(dev.sets)
    assert "confirm" in fails(port, op, {"value": json.dumps({"system": {"config": {"hostname": "z"}}})})[1]
    assert "confirm" in fails(port, op, {"confirm": "false", "value": "{}"})[1]
    assert dev.sets == before


def test_update_all_with_confirm_writes_at_root(make_device):
    dev, port = make_device()
    run(port, "update-all", {"confirm": "true", "value": json.dumps({"system": {"config": {"hostname": "root"}}})})
    assert dev.trees["openconfig"]["system"]["config"]["hostname"] == "root"
    assert "interface[name=eth0]" in dev.trees["openconfig"]["interfaces"]


def test_replace_all_with_confirm_replaces_the_tree(make_device):
    dev, port = make_device()
    run(port, "replace-all", {"confirm": True, "value": {"system": {"config": {"hostname": "r"}}}})
    assert list(dev.trees["openconfig"]) == ["system"]


# ----------------------------------------------------------------- dedicated gNOI services

def test_gnoi_ping_and_traceroute(make_device):
    _, port = make_device()
    out = run(port, "gnoi-ping", {"destination": "10.1.1.1", "count": 2})
    assert out[-1]["sent"] == 2 and out[0]["source"] == "10.1.1.1"
    hops = run(port, "gnoi-traceroute", {"destination": "10.1.1.1", "max_ttl": 5, "do_not_resolve": "true"})
    assert hops[1]["address"] == "10.0.0.1"
    assert "destination" in fails(port, "gnoi-ping", {})[1]
    assert "integer" in fails(port, "gnoi-ping", {"destination": "x", "count": "many"})[1]


def test_gnoi_reboot_status_and_cancel(make_device):
    dev, port = make_device()
    assert not run(port, "gnoi-reboot-status", {}).get("active")
    run(port, "gnoi-reboot", {"delay": 60, "message": "test"})
    assert run(port, "gnoi-reboot-status", {})["count"] == 1
    run(port, "gnoi-cancel-reboot", {"message": "oops"})
    assert dev.reboots == []


def test_gnoi_wrappers_accept_a_request_json(make_device):
    _, port = make_device()
    out = run(port, "gnoi-ping", {"request": json.dumps({"destination": "9.9.9.9", "count": 1})})
    assert out[-1]["sent"] == 1


def test_idempotency_flags():
    for name in ("subscribe-once", "subscribe-poll", "subscribe-stream", "get-state", "get-all", "get-operational",
                 "gnoi-ping", "gnoi-traceroute", "gnoi-reboot-status"):
        assert registry.OPERATIONS[name].idempotent, name
    for name in ("update-config", "replace-config", "union-replace-config", "delete-config", "update-all",
                 "replace-all", "union-replace-all", "gnoi-cancel-reboot"):
        assert not registry.OPERATIONS[name].idempotent, name
