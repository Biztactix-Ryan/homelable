"""Unit tests for app.services.proxmox_status — caching + status mapping."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.services.proxmox_status as status_mod
from app.db.models import ProxmoxIntegration
from app.services.proxmox_service import ProxmoxAuth
from app.services.proxmox_status import _cached_list_vms, invalidate_cache


def _integration(id_: str = "i1") -> ProxmoxIntegration:
    return ProxmoxIntegration(
        id=id_, name="t", host="pve.local", port=8006, verify_tls=False,
        auth_type="token", token_user="root@pam", token_id="x", token_secret_enc="enc",
    )


def _auth() -> ProxmoxAuth:
    return ProxmoxAuth(token_user="root@pam", token_id="x", token_secret="s")


@pytest.fixture(autouse=True)
def _clear_cache():
    status_mod._VMS_CACHE.clear()
    status_mod._VMS_LOCKS.clear()
    yield
    status_mod._VMS_CACHE.clear()
    status_mod._VMS_LOCKS.clear()


@pytest.mark.asyncio
async def test_cached_list_vms_collapses_concurrent_calls_into_one_fetch():
    """N concurrent status checks for one integration must issue one list_vms call."""
    sample = [{"vmid": 1, "type": "vm"}]
    mock = AsyncMock(return_value=sample)
    integration = _integration()
    with patch("app.services.proxmox_status.list_vms", mock):
        import asyncio

        results = await asyncio.gather(
            *(_cached_list_vms(integration, _auth()) for _ in range(10))
        )
    assert all(r == sample for r in results)
    assert mock.call_count == 1


@pytest.mark.asyncio
async def test_cached_list_vms_separate_integrations_dont_share():
    sample_a = [{"vmid": 1}]
    sample_b = [{"vmid": 99}]
    seq = {"i1": sample_a, "i2": sample_b}
    async def fake(host, port, auth, verify_tls):  # noqa: ARG001
        return seq["i1"] if "i1-host" in host else seq["i2"]
    int_a = _integration("i1")
    int_a.host = "i1-host"
    int_b = _integration("i2")
    int_b.host = "i2-host"
    with patch("app.services.proxmox_status.list_vms", new=AsyncMock(side_effect=fake)):
        a = await _cached_list_vms(int_a, _auth())
        b = await _cached_list_vms(int_b, _auth())
    assert a == sample_a
    assert b == sample_b


@pytest.mark.asyncio
async def test_invalidate_cache_forces_refetch():
    mock = AsyncMock(return_value=[{"vmid": 1}])
    integration = _integration()
    with patch("app.services.proxmox_status.list_vms", mock):
        await _cached_list_vms(integration, _auth())
        await _cached_list_vms(integration, _auth())  # cached
        invalidate_cache(integration.id)
        await _cached_list_vms(integration, _auth())  # fresh again
    assert mock.call_count == 2


@pytest.mark.asyncio
async def test_check_proxmox_node_short_circuits_on_ephemeral_id():
    from app.services.proxmox_status import check_proxmox_node
    result = await check_proxmox_node("ephemeral-abc123:100")
    assert result == {"status": "unknown", "response_time_ms": None}


@pytest.mark.asyncio
async def test_check_proxmox_node_bad_target_returns_unknown():
    from app.services.proxmox_status import check_proxmox_node
    assert (await check_proxmox_node(None))["status"] == "unknown"
    assert (await check_proxmox_node("no-colon"))["status"] == "unknown"
    assert (await check_proxmox_node("int:not-an-int"))["status"] == "unknown"
