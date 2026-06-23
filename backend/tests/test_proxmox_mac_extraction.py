"""Unit tests for parsing MACs out of Proxmox VM / LXC config responses."""
from app.services.proxmox_service import extract_macs_from_config


def test_qemu_virtio_format():
    cfg = {"net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1"}
    assert extract_macs_from_config(cfg) == ["bc:24:11:aa:bb:cc"]


def test_qemu_e1000_format():
    cfg = {"net0": "e1000=52:54:00:11:22:33,bridge=vmbr0"}
    assert extract_macs_from_config(cfg) == ["52:54:00:11:22:33"]


def test_lxc_hwaddr_format():
    cfg = {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:11:22:33,ip=dhcp"}
    assert extract_macs_from_config(cfg) == ["bc:24:11:11:22:33"]


def test_multiple_interfaces():
    cfg = {
        "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0",
        "net1": "virtio=BC:24:11:AA:BB:CD,bridge=vmbr1",
    }
    assert sorted(extract_macs_from_config(cfg)) == [
        "bc:24:11:aa:bb:cc",
        "bc:24:11:aa:bb:cd",
    ]


def test_ignores_non_net_keys():
    cfg = {
        "cpu": "host",
        "memory": "4096",
        "net0": "virtio=AA:BB:CC:11:22:33,bridge=vmbr0",
        "scsi0": "local-lvm:vm-100-disk-0,size=32G",
    }
    assert extract_macs_from_config(cfg) == ["aa:bb:cc:11:22:33"]


def test_no_macs_returns_empty():
    assert extract_macs_from_config({"cpu": "host"}) == []
    assert extract_macs_from_config({}) == []


def test_handles_missing_mac_in_net_field():
    """If a net field exists but has no MAC, just skip it (no crash)."""
    cfg = {"net0": "bridge=vmbr0,firewall=1"}
    assert extract_macs_from_config(cfg) == []
