"""gNMI operations (channel and Capabilities via pygnmi; Get/Set via the vendored 0.10.0 wire layer)."""

import json

from grpc_driver import gnmi
from grpc_driver.errors import DriverError
from grpc_driver.registry import operation

DATATYPES = ("all", "config", "state", "operational")


def as_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def as_list(value):
    """Accept a list, a JSON array string, or a single scalar; return a list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return [value]
    return [value]


def as_json(value, what="value"):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise DriverError(f"{what} is not valid JSON: {exc}", "invalid_input")
    return value


def _pairs(items, what):
    """Normalise [[path, value], ...] or [{"path":..., "value":...}, ...] into pygnmi tuples."""
    out = []
    for item in as_list(items) if not isinstance(items, list) else items:
        if isinstance(item, dict):
            path, value = item.get("path"), item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            path, value = item
        else:
            raise DriverError(f"{what} entries must be [path, value] or {{path, value}}", "invalid_input")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        out.append((path, value))
    return out


@operation("capabilities", idempotent=True, description="gNMI Capabilities")
def capabilities(session, params):
    return session.gnmi.capabilities()


@operation("is-alive", idempotent=True, description="Channel + gNMI Capabilities round trip")
def is_alive(session, params):
    caps = session.gnmi.capabilities()
    if caps is None:
        raise DriverError("Device did not answer gNMI Capabilities", "unavailable", evict=True)
    return {"alive": True, "gnmi_version": caps.get("gnmi_version")}


@operation("get", idempotent=True, description="gNMI Get (origin / raw CLI paths supported)")
def get(session, params):
    paths = as_list(params.get("path")) or ["/"]
    datatype = (params.get("datatype") or "all").lower()
    if datatype not in DATATYPES:
        raise DriverError(f"datatype must be one of {DATATYPES}", "invalid_input")
    return gnmi.get(
        session,
        paths,
        datatype=datatype,
        encoding=params.get("encoding") or None,
        prefix=params.get("prefix") or "",
        target=params.get("target") or None,
        origin=params.get("origin") or None,
        raw=as_bool(params.get("raw")),
    )


@operation("get-config", idempotent=True, description="gNMI Get restricted to configuration data")
def get_config(session, params):
    return get(session, {**params, "datatype": "config"})


@operation("set", description="gNMI Set (update / replace / union_replace / delete, applied atomically by the device)")
def set_(session, params):
    update = _pairs(params.get("update"), "update")
    replace = _pairs(params.get("replace"), "replace")
    union_replace = _pairs(params.get("union_replace"), "union_replace")
    delete = as_list(params.get("delete"))
    if not (update or replace or union_replace or delete):
        raise DriverError("set needs at least one of update, replace, union_replace or delete", "invalid_input")
    return gnmi.set_(
        session,
        update=update,
        replace=replace,
        union_replace=union_replace,
        delete=delete,
        encoding=params.get("encoding") or None,
        prefix=params.get("prefix") or "",
        target=params.get("target") or None,
        origin=params.get("origin") or None,
        raw=as_bool(params.get("raw")),
        force=as_bool(params.get("force")),
    )


@operation("set-config", description="Config Manager remediation: [{parents, old, new}] applied as one gNMI Set")
def set_config(session, params):
    raw = params.get("config") or params.get("changes") or params.get("config_content")
    changes = as_json(raw, "config")
    if not isinstance(changes, list) or not changes:
        raise DriverError("config must be a non-empty JSON array of {parents, old, new}", "invalid_input")
    mode = (params.get("mode") or "update").lower()
    if mode not in ("update", "replace", "union_replace"):
        raise DriverError("mode must be update, replace or union_replace", "invalid_input")
    apply, delete = [], []
    for change in changes:
        path = "/" + "/".join(str(p).strip("/") for p in change.get("parents") or [])
        new, old = change.get("new"), change.get("old")
        if new not in (None, ""):
            apply.append((path, as_json(new, "new") if isinstance(new, str) and new.lstrip()[:1] in "{[" else new))
        elif old not in (None, ""):
            delete.append(path)
    if not (apply or delete):
        raise DriverError("no change had a 'new' value to apply or an 'old' value to delete", "invalid_input")
    gnmi.set_(
        session,
        **{mode: apply},
        delete=delete,
        origin=params.get("origin") or None,
        force=as_bool(params.get("force")),
    )
    return [
        {"result": True, "parents": c.get("parents") or [], "old": c.get("old") or "", "new": c.get("new")}
        for c in changes
    ]
