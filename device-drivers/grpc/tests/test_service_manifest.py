"""The Gateway 5 service manifest and the Python operation registry must describe the same set of operations.

The manifest is ``import.yaml`` beside main.py, or the repo-wide ``import.yml`` two levels up when this driver
lives in a shared assets repository. Every service names its operation in ``runtime.env.GRPC_OP``.
"""

import os

import pytest

import conftest
from grpc_driver import registry

yaml = pytest.importorskip("yaml")

registry.load_plugins(conftest.BASE)

CANDIDATES = (
    os.path.join(conftest.BASE, "import.yaml"),
    os.path.join(conftest.BASE, "import.yml"),
    os.path.join(conftest.BASE, "..", "..", "import.yml"),
)

import main  # noqa: E402

# Control operations handled by main.py itself, outside the registry.
KNOWN_OPS = set(registry.OPERATIONS) | set(main.CONTROL_OPS)


def load():
    for path in CANDIDATES:
        if os.path.exists(path):
            with open(path) as handle:
                return yaml.safe_load(handle)
    pytest.skip("no import.yaml / import.yml next to the driver")


def grpc_services():
    return [s for s in load().get("services", []) if (s.get("runtime", {}).get("env") or {}).get("GRPC_OP")]


def test_every_service_points_at_a_real_operation():
    unknown = {s["name"]: s["runtime"]["env"]["GRPC_OP"] for s in grpc_services()
               if s["runtime"]["env"]["GRPC_OP"] not in KNOWN_OPS}
    assert not unknown, f"services whose GRPC_OP is not registered: {unknown}"


def test_every_operation_has_a_service():
    exposed = {s["runtime"]["env"]["GRPC_OP"] for s in grpc_services()}
    missing = sorted(KNOWN_OPS - exposed)
    assert not missing, f"operations with no service in the manifest: {missing}"


def test_service_names_and_ops_are_unique():
    services = grpc_services()
    names = [s["name"] for s in services]
    ops = [s["runtime"]["env"]["GRPC_OP"] for s in services]
    assert len(names) == len(set(names)) and len(ops) == len(set(ops))


def test_decorators_exist_and_describe_known_inputs():
    manifest = load()
    decorators = {d["name"]: d["schema"] for d in manifest.get("decorators", [])}
    for s in grpc_services():
        ref = s.get("decorator")
        if ref:
            assert ref in decorators, f"{s['name']} references missing decorator {ref}"
            schema = decorators[ref]
            for key in schema.get("required", []):
                assert key in schema["properties"], f"{ref} requires undeclared input {key}"
