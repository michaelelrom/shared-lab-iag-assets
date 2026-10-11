import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_device  # noqa: E402


def start(device=None, port=0):
    device = device or mock_device.make(interfaces=["eth0", "eth1"])
    server, bound = mock_device.serve(device, port=port, host="127.0.0.1")
    return device, server, bound


@pytest.fixture()
def make_device():
    servers = []

    def factory(**kwargs):
        kwargs.setdefault("interfaces", ["eth0", "eth1"])
        dev, server, port = start(mock_device.make(**kwargs))
        servers.append(server)
        return dev, port

    yield factory
    for server in servers:
        server.stop(0)
