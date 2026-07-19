"""Unit tests for ``pixie.web._inventory.normalise_inventory``.

The wire payload posted by the live env is verbatim ``{"lshw": <lshw
-json>, "disks": [...]}``; these tests exercise the extractor against
representative ``lshw -json`` fragments covering: single-CPU,
multi-CPU (multi-socket), fully-populated DIMMs, partly-populated
DIMMs (empty slots present), and a payload with no memory bank
records at all (some firmwares don't populate SMBIOS type 17 fully).
"""

from __future__ import annotations

from pixie.web._inventory import humanize_bytes, humanize_hz, normalise_inventory


def _dimm(slot: str, size: int, mhz: int = 3200, ddr: str = "DDR4") -> dict:
    description = f"DIMM {ddr} Synchronous 3200 MHz (0.3 ns)" if mhz else f"DIMM {ddr} Synchronous"
    return {
        "id": f"bank:{slot}",
        "class": "memory",
        "description": description,
        "product": "M393A2K43DB3-CWE",
        "vendor": "Samsung",
        "slot": f"DIMM_{slot}",
        "size": size,
        "clock": mhz * 1_000_000 if mhz else None,
    }


def _empty_dimm(slot: str) -> dict:
    return {
        "id": f"bank:{slot}",
        "class": "memory",
        "description": "DIMM DDR4 Synchronous [empty]",
        "product": "NO DIMM",
        "vendor": "NO DIMM",
        "slot": f"DIMM_{slot}",
    }


def _cpu_node(
    model: str, cores: int, threads: int, mhz: float = 2.8e9, capacity: float = 3.35e9
) -> dict:
    return {
        "id": "cpu",
        "class": "processor",
        "product": model,
        "vendor": "Advanced Micro Devices [AMD]",
        "slot": "CPU0",
        "width": 64,
        "size": mhz,
        "capacity": capacity,
        "capabilities": {"x86-64": "64bits extensions (x86-64)"},
        "configuration": {"cores": str(cores), "threads": str(threads)},
    }


def _system(children: list[dict]) -> dict:
    return {
        "id": "fwtop",
        "class": "system",
        "product": "Test Server 2000",
        "vendor": "Test Vendor",
        "serial": "SN-12345",
        "children": [
            {
                "id": "core",
                "class": "bus",
                "children": [
                    {
                        "id": "firmware",
                        "class": "memory",
                        "description": "BIOS",
                        "vendor": "American Megatrends Inc.",
                        "version": "F2",
                        "date": "03/31/2021",
                    },
                    *children,
                ],
            }
        ],
    }


def test_normalise_inventory_handles_none_and_empty() -> None:
    assert normalise_inventory(None) == {
        "system": {},
        "cpu": {"sockets": [], "total_cores": None, "total_threads": None},
        "memory": {"total_bytes": None, "dimms": None},
        "nics": [],
        "disks": [],
    }
    assert normalise_inventory({}) == normalise_inventory(None)
    assert normalise_inventory({"disks": [{"path": "/dev/sda"}]})["disks"] == [{"path": "/dev/sda"}]


def test_normalise_inventory_single_cpu_full_dimms() -> None:
    lshw = _system(
        [
            _cpu_node("AMD EPYC 7402P 24-Core Processor", 24, 48),
            {
                "id": "memory",
                "class": "memory",
                "size": 34359738368,
                "children": [_dimm("A1", 17179869184), _dimm("A2", 17179869184)],
            },
        ]
    )
    inv = normalise_inventory({"lshw": lshw, "disks": []})

    assert inv["system"]["model"] == "Test Server 2000"
    assert inv["system"]["vendor"] == "Test Vendor"
    assert inv["system"]["serial"] == "SN-12345"
    assert "F2" in inv["system"]["firmware"]

    cpu = inv["cpu"]
    assert len(cpu["sockets"]) == 1
    socket = cpu["sockets"][0]
    assert socket["model"] == "AMD EPYC 7402P 24-Core Processor"
    assert socket["cores"] == 24
    assert socket["threads"] == 48
    assert socket["arch"] == "x86_64"
    assert cpu["total_cores"] == 24
    assert cpu["total_threads"] == 48

    memory = inv["memory"]
    assert memory["total_bytes"] == 34359738368
    assert memory["slots_total"] == 2
    assert memory["slots_populated"] == 2
    assert memory["dominant_type"] == "DDR4"
    assert memory["dominant_speed_hz"] == 3200 * 1_000_000
    assert all(not d["empty"] for d in memory["dimms"])


def test_normalise_inventory_multi_cpu_sockets() -> None:
    lshw = _system(
        [
            _cpu_node("AMD EPYC 7402P 24-Core Processor", 24, 48),
            {
                **_cpu_node("AMD EPYC 7402P 24-Core Processor", 24, 48),
                "id": "cpu:1",
                "slot": "CPU1",
            },
        ]
    )
    inv = normalise_inventory({"lshw": lshw})
    cpu = inv["cpu"]
    assert len(cpu["sockets"]) == 2
    assert cpu["total_cores"] == 48
    assert cpu["total_threads"] == 96


def test_normalise_inventory_partly_populated_dimms() -> None:
    lshw = _system(
        [
            {
                "id": "memory",
                "class": "memory",
                "children": [
                    _dimm("A1", 17179869184),
                    _empty_dimm("A2"),
                    _dimm("B1", 17179869184),
                    _empty_dimm("B2"),
                ],
            },
        ]
    )
    inv = normalise_inventory({"lshw": lshw})
    memory = inv["memory"]
    assert memory["slots_total"] == 4
    assert memory["slots_populated"] == 2
    # No explicit "size" on the whole-memory node here, so the total
    # is derived by summing the occupied DIMMs.
    assert memory["total_bytes"] == 34359738368
    empties = [d for d in memory["dimms"] if d["empty"]]
    assert len(empties) == 2
    assert all(d["size_bytes"] is None for d in empties)


def test_normalise_inventory_no_memory_banks_falls_back_to_total_only() -> None:
    lshw = _system([{"id": "memory", "class": "memory", "size": 33285996544}])
    inv = normalise_inventory({"lshw": lshw})
    memory = inv["memory"]
    assert memory["total_bytes"] == 33285996544
    assert memory["dimms"] is None


def test_normalise_inventory_missing_memory_node() -> None:
    lshw = _system([_cpu_node("AMD Ryzen 7 7840U", 8, 16)])
    inv = normalise_inventory({"lshw": lshw})
    assert inv["memory"] == {"total_bytes": None, "dimms": None}


def test_normalise_inventory_extracts_nics() -> None:
    lshw = _system(
        [
            {
                "id": "network",
                "class": "network",
                "logicalname": "eth0",
                "vendor": "Intel Corporation",
                "serial": "aa:bb:cc:dd:ee:ff",
                "capacity": 1_000_000_000,
                "configuration": {"driver": "e1000e"},
            }
        ]
    )
    inv = normalise_inventory({"lshw": lshw})
    assert inv["nics"] == [
        {
            "name": "eth0",
            "mac": "aa:bb:cc:dd:ee:ff",
            "vendor": "Intel Corporation",
            "driver": "e1000e",
            "speed": "1.00 GHz",
        }
    ]


def test_humanize_bytes() -> None:
    assert humanize_bytes(None) == "-"
    assert humanize_bytes(-1) == "-"
    assert humanize_bytes(0) == "0 B"
    assert humanize_bytes(1024) == "1.0 KiB"
    assert humanize_bytes(17179869184) == "16.0 GiB"


def test_humanize_hz() -> None:
    assert humanize_hz(None) == "-"
    assert humanize_hz(0) == "-"
    assert humanize_hz(2_800_000_000) == "2.80 GHz"
    assert humanize_hz(100_000_000) == "0.10 GHz"
