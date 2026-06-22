"""Deleting a node sourced from an external system (Proxmox today) must
leave a hidden PendingDevice tombstone behind so the originating sync
engine doesn't immediately recreate it.

Covers the user-visible bug 'deleting a Proxmox VM doesn't make it stay
deleted — it reappears every sync interval'. Also covers host-node
deletion (which cascades to VM children) creating tombstones for the
cascaded children.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node, PendingDevice, ProxmoxIntegration


@pytest.fixture
async def headers(client: AsyncClient):
    res = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.mark.asyncio
async def test_delete_plain_node_leaves_no_tombstone(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    create = await client.post(
        "/api/v1/nodes",
        json={"type": "server", "label": "Manual", "status": "unknown"},
        headers=headers,
    )
    node_id = create.json()["id"]
    res = await client.delete(f"/api/v1/nodes/{node_id}", headers=headers)
    assert res.status_code == 204
    pending = (await db_session.execute(select(PendingDevice))).scalars().all()
    assert pending == []


@pytest.mark.asyncio
async def test_delete_proxmox_vm_creates_hidden_tombstone(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    vm = Node(
        type="vm", label="web", status="online",
        external_source="proxmox", external_id="i1:100",
        check_method="proxmox", check_target="i1:100",
        ip="10.0.0.5", hostname="web.local",
    )
    db_session.add(vm)
    await db_session.commit()

    res = await client.delete(f"/api/v1/nodes/{vm.id}", headers=headers)
    assert res.status_code == 204

    tombstones = (await db_session.execute(
        select(PendingDevice).where(PendingDevice.external_id == "i1:100")
    )).scalars().all()
    assert len(tombstones) == 1
    t = tombstones[0]
    assert t.status == "hidden"
    assert t.discovery_source == "proxmox"
    assert t.ip == "10.0.0.5"
    assert t.hostname == "web.local"
    assert t.friendly_name == "web"


@pytest.mark.asyncio
async def test_delete_proxmox_host_tombstones_all_external_children(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    host = Node(
        type="proxmox", label="Cluster", status="unknown", container_mode=True,
        external_source="proxmox-host", external_id="i1:host",
    )
    db_session.add(host)
    await db_session.flush()
    vm1 = Node(
        type="vm", label="web", status="online", parent_id=host.id,
        external_source="proxmox", external_id="i1:100",
        check_method="proxmox", check_target="i1:100",
    )
    vm2 = Node(
        type="lxc", label="db", status="offline", parent_id=host.id,
        external_source="proxmox", external_id="i1:200",
        check_method="proxmox", check_target="i1:200",
    )
    db_session.add_all([vm1, vm2])
    await db_session.commit()

    res = await client.delete(f"/api/v1/nodes/{host.id}", headers=headers)
    assert res.status_code == 204

    tombstones = {
        t.external_id: t
        for t in (await db_session.execute(select(PendingDevice))).scalars().all()
    }
    assert set(tombstones) == {"i1:host", "i1:100", "i1:200"}
    assert all(t.status == "hidden" for t in tombstones.values())
    # Host tombstone is recorded under "proxmox" so the same sync engine can see it.
    assert tombstones["i1:host"].discovery_source == "proxmox"
    assert tombstones["i1:100"].discovery_source == "proxmox"


@pytest.mark.asyncio
async def test_delete_idempotent_when_tombstone_already_exists(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    # Pre-existing tombstone (e.g. from a previous delete + re-import cycle).
    db_session.add(PendingDevice(
        external_id="i1:300", status="hidden", discovery_source="proxmox",
        friendly_name="stale", suggested_type="vm",
    ))
    vm = Node(
        type="vm", label="stale", status="online",
        external_source="proxmox", external_id="i1:300",
        check_method="proxmox", check_target="i1:300",
    )
    db_session.add(vm)
    await db_session.commit()

    res = await client.delete(f"/api/v1/nodes/{vm.id}", headers=headers)
    assert res.status_code == 204
    tombstones = (await db_session.execute(
        select(PendingDevice).where(PendingDevice.external_id == "i1:300")
    )).scalars().all()
    assert len(tombstones) == 1  # not duplicated


_SAMPLE_VMS = [
    {"vmid": 100, "node": "pve", "type": "vm", "name": "web", "status": "running",
     "cpu_count": 2, "ram_gb": 4.0, "disk_gb": 32.0, "tags": "", "uptime_seconds": 100},
]


@pytest.mark.asyncio
async def test_sync_skips_tombstoned_vm(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    """End-to-end: user deletes a Proxmox VM, scheduler runs, the VM does NOT
    reappear as a fresh pending device."""
    from app.core.crypto import encrypt
    integration = ProxmoxIntegration(
        id="i1", name="cluster", host="pve.local", port=8006, verify_tls=False,
        auth_type="token", token_user="root@pam", token_id="t",
        token_secret_enc=encrypt("s"), sync_interval_minutes=15,
    )
    vm = Node(
        type="vm", label="web", status="online",
        external_source="proxmox", external_id="i1:100",
        check_method="proxmox", check_target="i1:100",
    )
    db_session.add_all([integration, vm])
    await db_session.commit()

    # Delete the VM — should leave a hidden tombstone.
    res = await client.delete(f"/api/v1/nodes/{vm.id}", headers=headers)
    assert res.status_code == 204

    # Run a sync; cluster still reports the VM as running.
    with patch("app.services.proxmox_sync.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post(
            "/api/v1/proxmox/integrations/i1/sync", headers=headers,
        )
    assert res.status_code == 200
    body = res.json()
    # The tombstone should make the sync skip the VM instead of creating a new
    # pending entry — so pending_created == 0 and skipped count == 1.
    assert body["pending_created"] == 0
    assert body["pending_skipped_existing"] == 1

    # And the pending row should still be exactly the hidden tombstone (not
    # silently flipped back to "pending").
    pending = (await db_session.execute(select(PendingDevice))).scalars().all()
    assert len(pending) == 1
    assert pending[0].status == "hidden"
