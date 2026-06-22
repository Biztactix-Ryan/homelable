"""Periodic sync engine for saved Proxmox integrations.

Called from:
- `POST /api/v1/proxmox/integrations/{id}/sync` (manual trigger)
- the APScheduler background loop (every integration.sync_interval_minutes)

For each saved integration this:
1. Pulls every VM + LXC from `/cluster/resources?type=vm`.
2. Updates the status/CPU/RAM fields on Node rows already on the canvas
   (matched by `external_id = "{integration_id}:{vmid}"`).
3. Inserts any newly-discovered VMs into `pending_devices` for user approval
   — matches the Zigbee scheduled-discovery UX.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.db.models import Node, PendingDevice, ProxmoxIntegration
from app.schemas.proxmox import ProxmoxSyncResponse
from app.services.proxmox_service import (
    ProxmoxAuth,
    list_vms,
    status_to_node_status,
)

logger = logging.getLogger(__name__)


def integration_to_auth(integration: ProxmoxIntegration) -> ProxmoxAuth:
    """Decrypt stored secrets and return a ProxmoxAuth ready for the API client."""
    if integration.auth_type == "token":
        assert integration.token_secret_enc
        return ProxmoxAuth(
            token_user=integration.token_user,
            token_id=integration.token_id,
            token_secret=decrypt(integration.token_secret_enc),
        )
    assert integration.password_enc
    return ProxmoxAuth(
        username=integration.username,
        password=decrypt(integration.password_enc),
    )


async def sync_integration(db: AsyncSession, integration: ProxmoxIntegration) -> ProxmoxSyncResponse:
    """Run one sync pass for a single integration.

    Always commits its own changes and refreshes integration.last_sync_at/status
    before returning. Errors are reported via the response object, not raised.
    """
    integration_id = integration.id
    try:
        auth = integration_to_auth(integration)
        vms = await list_vms(integration.host, integration.port, auth, integration.verify_tls)
    except Exception as exc:
        logger.warning("Proxmox sync failed for integration %s: %s", integration_id, exc)
        integration.last_sync_at = datetime.now(timezone.utc)
        integration.last_sync_status = "error"
        integration.last_sync_error = str(exc)
        await db.commit()
        return ProxmoxSyncResponse(
            integration_id=integration_id,
            vms_seen=0,
            nodes_updated=0,
            pending_created=0,
            pending_skipped_existing=0,
            status="error",
            error=str(exc),
        )

    existing_nodes = await _existing_vm_node_map(db, integration_id)
    existing_pending = await _existing_pending_map(db, integration_id)

    nodes_updated = 0
    pending_created = 0
    pending_skipped = 0
    for vm in vms:
        ext = f"{integration_id}:{vm['vmid']}"
        if vm["vmid"] in existing_nodes:
            node = existing_nodes[vm["vmid"]]
            new_status = status_to_node_status(vm.get("status"))
            changed = False
            if node.status != new_status:
                node.status = new_status
                changed = True
            if vm.get("cpu_count") is not None and node.cpu_count != vm["cpu_count"]:
                node.cpu_count = vm["cpu_count"]
                changed = True
            if vm.get("ram_gb") is not None and node.ram_gb != vm["ram_gb"]:
                node.ram_gb = vm["ram_gb"]
                changed = True
            if vm.get("disk_gb") is not None and node.disk_gb != vm["disk_gb"]:
                node.disk_gb = vm["disk_gb"]
                changed = True
            if new_status == "online":
                node.last_seen = datetime.now(timezone.utc)
            if changed:
                nodes_updated += 1
        elif ext in existing_pending:
            pending_skipped += 1
        else:
            db.add(_vm_to_pending(vm, integration_id))
            pending_created += 1

    integration.last_sync_at = datetime.now(timezone.utc)
    integration.last_sync_status = "ok"
    integration.last_sync_error = None
    await db.commit()
    return ProxmoxSyncResponse(
        integration_id=integration_id,
        vms_seen=len(vms),
        nodes_updated=nodes_updated,
        pending_created=pending_created,
        pending_skipped_existing=pending_skipped,
        status="ok",
    )


async def _existing_vm_node_map(db: AsyncSession, integration_id: str) -> dict[int, Node]:
    result = await db.execute(
        select(Node).where(
            Node.external_source == "proxmox",
            Node.external_id.like(f"{integration_id}:%"),
        )
    )
    out: dict[int, Node] = {}
    for node in result.scalars().all():
        if node.external_id and ":" in node.external_id:
            try:
                out[int(node.external_id.rsplit(":", 1)[1])] = node
            except ValueError:
                continue
    return out


async def _existing_pending_map(db: AsyncSession, integration_id: str) -> dict[str, PendingDevice]:
    result = await db.execute(
        select(PendingDevice).where(
            PendingDevice.discovery_source == "proxmox",
            PendingDevice.external_id.like(f"{integration_id}:%"),
        )
    )
    return {p.external_id: p for p in result.scalars().all() if p.external_id}


def _vm_to_pending(vm: dict[str, Any], integration_id: str) -> PendingDevice:
    return PendingDevice(
        hostname=vm.get("name"),
        suggested_type="lxc" if vm["type"] == "lxc" else "vm",
        status="pending",
        discovery_source="proxmox",
        friendly_name=vm.get("name"),
        external_id=f"{integration_id}:{vm['vmid']}",
    )
