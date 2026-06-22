"""Unit tests for app.services.proxmox_service (the API client)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.proxmox_service import (
    ProxmoxAuth,
    _normalize_vm,
    _token_header,
    check_connection,
    check_vm_status,
    list_vms,
    status_to_node_status,
)

# ── ProxmoxAuth ──────────────────────────────────────────────────────────────

def test_auth_mode_token():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    assert auth.mode == "token"


def test_auth_mode_password():
    auth = ProxmoxAuth(username="root@pam", password="hunter2")
    assert auth.mode == "password"


def test_auth_mode_empty_raises():
    with pytest.raises(ValueError, match="no credentials"):
        _ = ProxmoxAuth().mode


def test_token_header_format():
    auth = ProxmoxAuth(token_user="root@pam", token_id="claude", token_secret="uuid-here")
    headers = _token_header(auth)
    assert headers["Authorization"] == "PVEAPIToken=root@pam!claude=uuid-here"


# ── _normalize_vm ────────────────────────────────────────────────────────────

def test_normalize_vm_qemu_becomes_vm():
    raw = {"vmid": 100, "node": "pve", "type": "qemu", "name": "ubuntu", "status": "running"}
    assert _normalize_vm(raw)["type"] == "vm"


def test_normalize_vm_lxc_stays_lxc():
    raw = {"vmid": 200, "node": "pve", "type": "lxc", "name": "nginx", "status": "stopped"}
    assert _normalize_vm(raw)["type"] == "lxc"


def test_normalize_vm_memory_converted_to_gb():
    raw = {"vmid": 1, "node": "pve", "type": "qemu", "name": "x", "status": "running",
           "maxmem": 4 * 1024 ** 3, "maxdisk": 32 * 1024 ** 3, "maxcpu": 2}
    out = _normalize_vm(raw)
    assert out["ram_gb"] == 4.0
    assert out["disk_gb"] == 32.0
    assert out["cpu_count"] == 2


def test_normalize_vm_missing_fields_become_none():
    raw = {"vmid": 1, "node": "pve", "type": "qemu", "name": "x", "status": "running"}
    out = _normalize_vm(raw)
    assert out["cpu_count"] is None
    assert out["ram_gb"] is None
    assert out["disk_gb"] is None
    assert out["uptime_seconds"] is None


def test_normalize_vm_missing_name_falls_back():
    raw = {"vmid": 42, "node": "pve", "type": "qemu", "status": "running"}
    assert _normalize_vm(raw)["name"] == "vm-42"


# ── status_to_node_status ────────────────────────────────────────────────────

@pytest.mark.parametrize("proxmox_status,expected", [
    ("running", "online"),
    ("stopped", "offline"),
    ("paused", "offline"),
    ("unknown", "unknown"),
    (None, "unknown"),
    ("something-weird", "unknown"),
])
def test_status_mapping(proxmox_status, expected):
    assert status_to_node_status(proxmox_status) == expected


# ── test_connection ──────────────────────────────────────────────────────────

def _mock_httpx_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
async def test_test_connection_success():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response({"data": {"version": "8.0"}}))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        await check_connection("pve.local", 8006, auth, verify_tls=False)


@pytest.mark.asyncio
async def test_test_connection_rejects_401():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="bad")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response({}, status_code=401))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client), \
         pytest.raises(ConnectionError, match="401"):
        await check_connection("pve.local", 8006, auth, verify_tls=False)


@pytest.mark.asyncio
async def test_test_connection_connect_refused():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="x")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client), \
         pytest.raises(ConnectionError, match="Cannot reach"):
        await check_connection("pve.local", 8006, auth, verify_tls=False)


# ── list_vms ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_vms_returns_normalized():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    payload = {
        "data": [
            {"vmid": 100, "node": "pve", "type": "qemu", "name": "web", "status": "running",
             "maxmem": 2 * 1024 ** 3, "maxcpu": 2},
            {"vmid": 200, "node": "pve", "type": "lxc", "name": "db", "status": "stopped"},
        ]
    }
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response(payload))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        vms = await list_vms("pve.local", 8006, auth, verify_tls=False)
    assert len(vms) == 2
    assert vms[0]["type"] == "vm"
    assert vms[0]["ram_gb"] == 2.0
    assert vms[1]["type"] == "lxc"


@pytest.mark.asyncio
async def test_list_vms_empty():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response({"data": []}))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        assert await list_vms("pve.local", 8006, auth, verify_tls=False) == []


# ── check_vm_status ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_vm_status_running_is_online():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response({"data": {"status": "running"}}))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        assert await check_vm_status("pve.local", 8006, auth, False, "pve", "vm", 100) == "online"


@pytest.mark.asyncio
async def test_check_vm_status_stopped_is_offline():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=_mock_httpx_response({"data": {"status": "stopped"}}))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        assert await check_vm_status("pve.local", 8006, auth, False, "pve", "lxc", 200) == "offline"


@pytest.mark.asyncio
async def test_check_vm_status_error_returns_unknown():
    auth = ProxmoxAuth(token_user="root@pam", token_id="t1", token_secret="abc")
    mock_client = MagicMock()
    mock_client.request = AsyncMock(side_effect=httpx.ConnectError("no route"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    with patch("app.services.proxmox_service.httpx.AsyncClient", return_value=mock_client):
        assert await check_vm_status("pve.local", 8006, auth, False, "pve", "vm", 100) == "unknown"
