import json
import os
import subprocess
import sys

import pytest

import conftest
import mock_device
from grpc_driver import registry
from grpc_driver.executor import execute_direct, execute_pooled
from grpc_driver.pool import ChannelPool
from grpc_driver.session import ConnSpec

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
registry.load_plugins(BASE)


@pytest.fixture()
def device(make_device):
    return make_device()


def spec(port, **kw):
    return ConnSpec(host="127.0.0.1", port=port, username="admin", password="admin", insecure=True,
                    timeout=5, rpc_timeout=5, **kw)


def test_direct_gnmi_and_gnoi(device):
    dev, port = device
    assert execute_direct(spec(port), "is-alive", {})[0]["alive"] is True
    got, _ = execute_direct(spec(port), "get", {"path": "/interfaces/interface[name=eth0]/state/counters"})
    assert got["notification"][0]["update"][0]["val"] == {"in-octets": 1000, "out-octets": 900}
    assert execute_direct(spec(port), "gnoi", {"rpc": "system.Time"})[0]["time"].isdigit()
    ping, _ = execute_direct(spec(port), "gnoi", {"rpc": "system.Ping", "request": '{"destination": "1.1.1.1"}'})
    assert len(ping) == 4 and ping[-1]["sent"] == 3


def test_set_and_set_config(device):
    dev, port = device
    execute_direct(spec(port), "set", {"update": json.dumps([["/system/config", {"hostname": "r1"}]])})
    assert dev.sets[-1].update[0].path.elem[-1].name == "config"
    changes = [{"parents": ["interfaces", "interface[name=eth0]", "config"], "old": "", "new": '{"mtu": 9000}'}]
    out, _ = execute_direct(spec(port), "set-config", {"config": json.dumps(changes)})
    assert out[0]["result"] is True and len(dev.sets) == 2


def test_reflection_call(device):
    _, port = device
    out, _ = execute_direct(spec(port), "call", {"method": "gnoi.system.System/Time"})
    assert out["time"].isdigit()


def test_bad_credentials(device):
    _, port = device
    bad = ConnSpec(host="127.0.0.1", port=port, username="admin", password="nope", insecure=True, timeout=5)
    with pytest.raises(Exception) as err:
        execute_direct(bad, "is-alive", {})
    from grpc_driver.errors import classify
    assert classify(err.value)[0] == "unauthenticated"


def test_unknown_operation_and_rpc(device):
    _, port = device
    with pytest.raises(Exception, match="Unknown operation"):
        execute_direct(spec(port), "nope", {})
    with pytest.raises(Exception, match="Unknown rpc"):
        execute_direct(spec(port), "gnoi", {"rpc": "system.Nope"})


def test_pool_reuses_one_connection(device):
    dev, port = device
    pool = ChannelPool()
    for _ in range(5):
        _, meta = execute_pooled(pool, spec(port), "get", {"path": "/system/config"})
    assert pool.stats()["hits"] == 4 and pool.stats()["misses"] == 1
    assert len(set(dev.peers)) == 1, "all calls must share one TCP connection"
    pool.close_all()


def test_pool_keys_on_credentials(device):
    dev, port = device
    pool = ChannelPool()
    execute_pooled(pool, spec(port), "is-alive", {})
    execute_pooled(pool, spec(port, grpc_options=(("grpc.max_send_message_length", 1000000),)), "is-alive", {})
    assert pool.size() == 2
    pool.close_all()


def test_pool_recovers_from_dead_channel_for_idempotent_ops():
    dev, server, port = conftest.start()
    pool = ChannelPool()
    s = spec(port)
    execute_pooled(pool, s, "get", {"path": "/system/config"})
    server.stop(0)
    dev2, server2, _ = conftest.start(port=port)
    try:
        _, meta = execute_pooled(pool, s, "get", {"path": "/system/config"})
        assert meta["mode"] == "pooled"
    finally:
        server2.stop(0)
        pool.close_all()


def test_non_idempotent_op_is_not_replayed_on_dead_channel():
    dev, server, port = conftest.start()
    pool = ChannelPool()
    s = spec(port)
    execute_pooled(pool, s, "get", {"path": "/system/config"})
    server.stop(0)
    with pytest.raises(Exception):
        execute_pooled(pool, s, "set", {"update": json.dumps([["/system/config", {"x": 1}]])})
    assert dev.sets == []
    pool.close_all()


def test_deadline_cancels_hung_rpc():
    dev, server, port = conftest.start(mock_device.make(interfaces=["eth0"], get_delay=5))
    try:
        s = ConnSpec(host="127.0.0.1", port=port, username="admin", password="admin", insecure=True,
                     timeout=5, rpc_timeout=1)
        with pytest.raises(Exception, match="(?i)deadline"):
            execute_direct(s, "get", {"path": "/system/config"})
    finally:
        server.stop(0)


def run_cli(port, op, *extra, env=None, stdin=None):
    node = {"inventory_nodes": [{"name": "dev1", "attributes": {
        "itential_host": "127.0.0.1", "itential_user": "admin", "itential_password": "admin",
        "itential_driver_options": {"grpc": {"port": port, "insecure": True, "timeout": 5}}}}]}
    return subprocess.run([sys.executable, os.path.join(BASE, "main.py"), "--op", op, *extra],
                          input=json.dumps(stdin or node), capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))


def test_cli_one_shot_processes_share_pooled_channel(device, tmp_path):
    dev, port = device
    env = {"GRPC_POOL_SOCKET": f"/tmp/gp-test-{os.getpid()}.sock", "GRPC_POOL_DAEMON_IDLE": "2"}
    try:
        first = run_cli(port, "get", "--path", "/system/config", "--envelope", "true", env=env)
        second = run_cli(port, "get", "--path", "/system/config", "--envelope", "true", env=env)
        assert first.returncode == 0, first.stderr
        a, b = json.loads(first.stdout), json.loads(second.stdout)
        assert a["pool"] == {"mode": "pooled", "reused": False}
        assert b["pool"] == {"mode": "pooled", "reused": True}
        assert len(set(dev.peers)) == 1, "two separate processes, one device connection"
        status = json.loads(run_cli(port, "pool-status", env=env).stdout)
        assert status["running"] and status["hits"] == 1 and status["channels"][0]["uses"] == 2
    finally:
        run_cli(port, "pool-stop", env=env)


def test_cli_pool_off_uses_new_connection_each_time(device):
    dev, port = device
    run_cli(port, "get", "--path", "/system/config", "--pool", "off")
    run_cli(port, "get", "--path", "/system/config", "--pool", "off")
    assert len(set(dev.peers)) == 2


def test_cli_falls_back_when_daemon_cannot_start(device, tmp_path):
    dev, port = device
    env = {"GRPC_POOL_SOCKET": "/nonexistent-dir/x.sock"}
    out = run_cli(port, "get", "--path", "/system/config", "--envelope", "true", env=env)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["pool"]["mode"] == "fallback"
    required = run_cli(port, "get", "--path", "/system/config", "--pool", "required", env=env)
    assert required.returncode == 1


def test_cli_is_alive_prints_bare_boolean(device):
    _, port = device
    out = run_cli(port, "is-alive", "--pool", "off")
    assert out.stdout == "true" and out.returncode == 0
    bad = run_cli(port, "is-alive", "--pool", "off", "--password", "wrong")
    assert bad.stdout == "false" and bad.returncode == 1


def test_cli_empty_string_args_are_ignored(device):
    _, port = device
    out = run_cli(port, "get", "--path", "/system/config", "--datatype=", "--encoding", "", "--pool", "off")
    assert out.returncode == 0, out.stderr


def test_cli_array_and_object_params(device):
    dev, port = device
    out = run_cli(port, "get", "--path", '["/system/config","/interfaces"]', "--pool", "off")
    assert out.returncode == 0, out.stderr
    repeated = run_cli(port, "get", "--path", "/system/config", "--path", "/interfaces", "--pool", "off")
    assert repeated.returncode == 0, repeated.stderr
    upd = run_cli(port, "set", "--update", '[{"path": "/system/config", "value": {"hostname": "r1"}}]', "--pool", "off")
    assert upd.returncode == 0, upd.stderr
    assert len(dev.sets) == 1


def test_cli_set_config_matches_config_manager_contract(device):
    _, port = device
    changes = [{"parents": ["system", "config"], "old": "", "new": '{"hostname": "r1"}'}]
    out = run_cli(port, "set-config", "--config", json.dumps(changes), "--options", "{}", "--pool", "off")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [{"result": True, "parents": ["system", "config"], "old": "", "new": '{"hostname": "r1"}'}]


def test_plugin_directory_adds_operations(device, tmp_path):
    _, port = device
    (tmp_path / "mine.py").write_text(
        "from grpc_driver.registry import operation\n"
        "@operation('echo-host', idempotent=True)\n"
        "def echo(session, params):\n    return {'host': session.spec.host, 'got': params.get('x')}\n")
    out = run_cli(port, "echo-host", "--x", "7", "--pool", "off", env={"GRPC_PLUGIN_DIR": str(tmp_path)})
    assert json.loads(out.stdout) == {"host": "127.0.0.1", "got": "7"}
