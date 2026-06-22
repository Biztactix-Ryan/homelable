"""Per-node 'proxmox' status check.

The scheduler dispatcher passes Node.check_target as the only context, so we
parse it as "{integration_id}:{vmid}", look up the integration row, and query
Proxmox for the VM's lifecycle status. Returns the same shape as the rest of
the status_checker dispatcher: {status, response_time_ms}.

Ephemeral imports (integration_id starts with "ephemeral-") have no stored
credentials, so this returns 'unknown' for them — the user has to re-import
under a saved integration to get live status.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Node, ProxmoxIntegration
from app.services.proxmox_service import check_vm_status
from app.services.proxmox_sync import integration_to_auth

logger = logging.getLogger(__name__)


_UNKNOWN: dict[str, Any] = {"status": "unknown", "response_time_ms": None}


async def check_proxmox_node(check_target: str | None) -> dict[str, Any]:
    if not check_target or ":" not in check_target:
        return _UNKNOWN
    integration_id, _, vmid_str = check_target.rpartition(":")
    if integration_id.startswith("ephemeral-"):
        return _UNKNOWN
    try:
        vmid = int(vmid_str)
    except ValueError:
        return _UNKNOWN

    async with AsyncSessionLocal() as db:
        integration = await db.get(ProxmoxIntegration, integration_id)
        if not integration:
            return _UNKNOWN

        # The cluster-resources field is keyed by external_id, not vmid, so it tells
        # us which Proxmox node (cluster member) owns this VM and whether it's qemu/lxc.
        node_row = await db.execute(
            select(Node).where(Node.external_id == check_target, Node.external_source == "proxmox")
        )
        node = node_row.scalar_one_or_none()
        if not node:
            return _UNKNOWN

        try:
            auth = integration_to_auth(integration)
        except Exception as exc:
            logger.warning("Proxmox creds for integration %s could not be decrypted: %s", integration_id, exc)
            return _UNKNOWN

    # Resolve {proxmox_node, vm_type} from a cluster-resources fetch. Cheap call
    # (cached server-side; one HTTP round-trip per node check). Could be optimized
    # to a single per-tick fetch shared across all proxmox nodes, but the obvious
    # implementation has cache-invalidation cost — keep it simple unless it bites.
    from app.services.proxmox_service import list_vms

    start = time.monotonic()
    try:
        vms = await list_vms(integration.host, integration.port, auth, integration.verify_tls)
    except Exception as exc:
        logger.debug("Proxmox list_vms failed during status check: %s", exc)
        return {"status": "offline", "response_time_ms": None}

    match = next((vm for vm in vms if vm.get("vmid") == vmid), None)
    if not match:
        return _UNKNOWN

    status: dict[str, Any] = {
        "status": "online" if match.get("status") == "running" else "offline",
        "response_time_ms": int((time.monotonic() - start) * 1000),
    }
    # Drill into the per-VM endpoint only if the cluster summary says paused —
    # paused VMs report 'paused' (not running, not stopped) which we treat as offline
    # but might be worth distinguishing later.
    if match.get("status") not in ("running", "stopped", "paused"):
        per_vm = await check_vm_status(
            integration.host, integration.port, auth, integration.verify_tls,
            match["node"], match["type"], vmid,
        )
        status["status"] = per_vm
    return status
