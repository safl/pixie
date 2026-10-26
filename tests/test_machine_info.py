"""``GET /machines/{ref}/info``: one machine's binding, last contact and
summarised inventory, by name or MAC, with nothing sent as null."""

from __future__ import annotations

import tomlkit
from fastapi.testclient import TestClient

from tests.conftest import authed
from tests.test_web_inventory import _cpu_node, _dimm, _empty_dimm, _system

MAC = "aa:bb:cc:dd:ee:01"


def _inventory() -> dict:
    lshw = _system(
        [
            _cpu_node("AMD EPYC 7302P", 16, 32),
            {
                "id": "memory",
                "class": "memory",
                "size": 2 * 16 * 2**30,
                "children": [_dimm("A1", 16 * 2**30), _dimm("B1", 16 * 2**30), _empty_dimm("C1")],
            },
            {
                "id": "network",
                "class": "network",
                "logicalname": "eno1",
                "serial": MAC,
                "capacity": 1_000_000_000,
                "configuration": {"driver": "igb"},
            },
        ]
    )
    disk = {
        "path": "/dev/nvme0n1",
        "size": "1.8T",
        "model": "990 PRO",
        "serial": "S6Z",
        "tran": "nvme",
    }
    return {"lshw": lshw, "disks": [disk]}


def _walk(value: object) -> list[object]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _walk(v)]
    if isinstance(value, list):
        return [x for v in value for x in _walk(v)]
    return [value]


def _named(client: TestClient) -> None:
    authed(client).put(f"/machines/{MAC}/name", json={"name": "epyc-02"})


def test_info_without_inventory(client: TestClient) -> None:
    _named(client)
    info = client.get("/machines/epyc-02/info").json()
    assert info["name"] == "epyc-02" and info["mac"] == MAC
    assert info["binding"]["boot_mode_label"]
    assert "inventory" not in info
    assert None not in _walk(info)
    assert client.get("/machines/nope/info").status_code == 404


def test_info_with_binding_and_inventory(client: TestClient) -> None:
    _named(client)
    store = client.app.state.machines_store
    store.set_inventory(MAC, _inventory())
    store.upsert_binding(MAC, boot_mode="nbdboot-ephemeral", image_content_sha256="a" * 64)
    info = client.get(f"/machines/{MAC}/info").json()
    # The sha has no catalog entry, so there is no name to show.
    assert info["binding"]["image"] == {"sha256": "a" * 64}
    inv = info["inventory"]
    assert inv["cpu"]["sockets"][0]["model"] == "AMD EPYC 7302P"
    assert inv["memory"]["slots_populated"] == 2
    assert inv["nics"][0]["speed"] == "1 Gbit/s"
    assert inv["disks"][0]["path"] == "/dev/nvme0n1"
    # Leaving unknowns out is what keeps the answer TOML-representable.
    assert None not in _walk(info)
    tomlkit.dumps(info)
