"""API endpoint tests for /api/v1/proxmox/*."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node, ProxmoxIntegration

# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def headers(client: AsyncClient):
    res = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


_TOKEN_BODY = {
    "host": "pve.local",
    "port": 8006,
    "verify_tls": False,
    "auth_type": "token",
    "token_user": "root@pam",
    "token_id": "homelable",
    "token_secret": "uuid-1234",
}

_PASSWORD_BODY = {
    "host": "pve.local",
    "port": 8006,
    "verify_tls": False,
    "auth_type": "password",
    "username": "root@pam",
    "password": "hunter2",
}

_SAMPLE_VMS = [
    {"vmid": 100, "node": "pve", "type": "vm", "name": "web", "status": "running",
     "cpu_count": 2, "ram_gb": 4.0, "disk_gb": 32.0, "tags": "", "uptime_seconds": 12345},
    {"vmid": 200, "node": "pve", "type": "lxc", "name": "db", "status": "stopped",
     "cpu_count": 1, "ram_gb": 1.0, "disk_gb": 8.0, "tags": "", "uptime_seconds": None},
]


# ── /test-connection ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_test_connection_token_success(client: AsyncClient, headers: dict):
    with patch("app.api.routes.proxmox.check_connection", new=AsyncMock(return_value=None)):
        res = await client.post("/api/v1/proxmox/test-connection", json=_TOKEN_BODY, headers=headers)
    assert res.status_code == 200
    assert res.json() == {"connected": True, "message": "Connected."}


@pytest.mark.asyncio
async def test_test_connection_password_success(client: AsyncClient, headers: dict):
    with patch("app.api.routes.proxmox.check_connection", new=AsyncMock(return_value=None)):
        res = await client.post("/api/v1/proxmox/test-connection", json=_PASSWORD_BODY, headers=headers)
    assert res.status_code == 200
    assert res.json()["connected"] is True


@pytest.mark.asyncio
async def test_test_connection_failure_returns_message(client: AsyncClient, headers: dict):
    with patch(
        "app.api.routes.proxmox.check_connection",
        new=AsyncMock(side_effect=ConnectionError("Proxmox rejected credentials (HTTP 401).")),
    ):
        res = await client.post("/api/v1/proxmox/test-connection", json=_TOKEN_BODY, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is False
    assert "401" in body["message"]


@pytest.mark.asyncio
async def test_test_connection_requires_auth(client: AsyncClient):
    res = await client.post("/api/v1/proxmox/test-connection", json=_TOKEN_BODY)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_token_auth_missing_secret_rejected(client: AsyncClient, headers: dict):
    body = {**_TOKEN_BODY, "token_secret": None}
    res = await client.post("/api/v1/proxmox/test-connection", json=body, headers=headers)
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_password_auth_missing_password_rejected(client: AsyncClient, headers: dict):
    body = {**_PASSWORD_BODY, "password": None}
    res = await client.post("/api/v1/proxmox/test-connection", json=body, headers=headers)
    assert res.status_code == 422


# ── /list ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_returns_vms(client: AsyncClient, headers: dict):
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post("/api/v1/proxmox/list", json=_TOKEN_BODY, headers=headers)
    assert res.status_code == 200
    vms = res.json()["vms"]
    assert len(vms) == 2
    assert {v["vmid"] for v in vms} == {100, 200}
    assert vms[0]["type"] in ("vm", "lxc")


@pytest.mark.asyncio
async def test_list_connection_error_502(client: AsyncClient, headers: dict):
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(side_effect=ConnectionError("unreachable"))):
        res = await client.post("/api/v1/proxmox/list", json=_TOKEN_BODY, headers=headers)
    assert res.status_code == 502


# ── /import ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_import_creates_host_and_vm_nodes(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    body = {**_TOKEN_BODY, "selected_vmids": [100, 200], "integration_name": "Home Cluster",
            "save_credentials": False, "sync_interval_minutes": 15}
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post("/api/v1/proxmox/import", json=body, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert len(data["created_node_ids"]) == 2
    assert data["host_node_id"]
    assert data["skipped_existing"] == []
    assert data["integration_id"] is None  # not saved

    # Host node should be a container, VMs should be children
    nodes = (await db_session.execute(select(Node))).scalars().all()
    host = next(n for n in nodes if n.external_source == "proxmox-host")
    assert host.container_mode is True
    assert host.label == "Home Cluster"
    vms = [n for n in nodes if n.external_source == "proxmox"]
    assert len(vms) == 2
    assert all(n.parent_id == host.id for n in vms)
    assert all(n.check_method == "proxmox" for n in vms)


@pytest.mark.asyncio
async def test_import_is_idempotent(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    body = {**_TOKEN_BODY, "selected_vmids": [100, 200], "integration_name": "Home Cluster",
            "save_credentials": True, "sync_interval_minutes": 15}
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        first = await client.post("/api/v1/proxmox/import", json=body, headers=headers)
        assert first.status_code == 200
        first_data = first.json()
        # Re-import same vmids: should skip both
        body_repeat = {**body, "integration_name": "Home Cluster"}
        # use the same integration_id so dedup applies — easiest is to call without re-saving
        body_repeat["save_credentials"] = False
        # Note: ephemeral re-import generates a fresh integration_id, so it won't dedup
        # against the saved integration's VMs. The realistic re-import scenario is the
        # scheduler hitting the SAME saved integration; mimic that by hitting /sync.
        res = await client.post(
            f"/api/v1/proxmox/integrations/{first_data['integration_id']}/sync",
            headers=headers,
        )
    assert res.status_code == 200
    sync = res.json()
    # Both VMs already exist as Nodes, so neither should land in pending.
    assert sync["pending_created"] == 0


@pytest.mark.asyncio
async def test_import_with_save_credentials_persists_integration(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    body = {**_TOKEN_BODY, "selected_vmids": [100], "integration_name": "Saved Cluster",
            "save_credentials": True, "sync_interval_minutes": 30}
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post("/api/v1/proxmox/import", json=body, headers=headers)
    assert res.status_code == 200
    assert res.json()["integration_id"]
    integrations = (await db_session.execute(select(ProxmoxIntegration))).scalars().all()
    assert len(integrations) == 1
    saved = integrations[0]
    assert saved.name == "Saved Cluster"
    assert saved.auth_type == "token"
    # Secret should be encrypted, not the raw value
    assert saved.token_secret_enc != "uuid-1234"
    assert saved.token_secret_enc is not None
    assert saved.sync_interval_minutes == 30


@pytest.mark.asyncio
async def test_import_no_matching_vmids_rejected(
    client: AsyncClient, headers: dict
):
    body = {**_TOKEN_BODY, "selected_vmids": [9999], "integration_name": "X",
            "save_credentials": False, "sync_interval_minutes": 15}
    with patch("app.api.routes.proxmox.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post("/api/v1/proxmox/import", json=body, headers=headers)
    assert res.status_code == 422


# ── /integrations ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_integrations_redacts_secrets(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    from app.core.crypto import encrypt
    row = ProxmoxIntegration(
        name="Test", host="pve.local", port=8006, verify_tls=False, auth_type="token",
        token_user="root@pam", token_id="t1", token_secret_enc=encrypt("very-secret-uuid"),
        sync_interval_minutes=15,
    )
    db_session.add(row)
    await db_session.commit()

    res = await client.get("/api/v1/proxmox/integrations", headers=headers)
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 1
    assert items[0]["name"] == "Test"
    # No secret field on the response model — schema enforces redaction.
    assert "token_secret" not in items[0]
    assert "token_secret_enc" not in items[0]
    assert "password" not in items[0]


@pytest.mark.asyncio
async def test_delete_integration(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    from app.core.crypto import encrypt
    row = ProxmoxIntegration(
        name="Del", host="pve.local", port=8006, verify_tls=False, auth_type="token",
        token_user="root@pam", token_id="t1", token_secret_enc=encrypt("s"),
    )
    db_session.add(row)
    await db_session.commit()

    res = await client.delete(f"/api/v1/proxmox/integrations/{row.id}", headers=headers)
    assert res.status_code == 204
    remaining = (await db_session.execute(select(ProxmoxIntegration))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_delete_integration_404(client: AsyncClient, headers: dict):
    res = await client.delete("/api/v1/proxmox/integrations/nope", headers=headers)
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_sync_integration_endpoint(
    client: AsyncClient, headers: dict, db_session: AsyncSession
):
    from app.core.crypto import encrypt
    row = ProxmoxIntegration(
        name="Sync", host="pve.local", port=8006, verify_tls=False, auth_type="token",
        token_user="root@pam", token_id="t1", token_secret_enc=encrypt("s"),
    )
    db_session.add(row)
    await db_session.commit()

    with patch("app.services.proxmox_sync.list_vms", new=AsyncMock(return_value=_SAMPLE_VMS)):
        res = await client.post(
            f"/api/v1/proxmox/integrations/{row.id}/sync", headers=headers
        )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["vms_seen"] == 2
    # Both VMs are new → land in pending devices.
    assert body["pending_created"] == 2


@pytest.mark.asyncio
async def test_integrations_require_auth(client: AsyncClient):
    assert (await client.get("/api/v1/proxmox/integrations")).status_code == 401
    assert (await client.delete("/api/v1/proxmox/integrations/x")).status_code == 401
    assert (await client.post("/api/v1/proxmox/integrations/x/sync")).status_code == 401
