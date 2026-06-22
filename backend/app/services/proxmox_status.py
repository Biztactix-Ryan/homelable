"""Per-node 'proxmox' status check.

The scheduler dispatcher passes Node.check_target as the only context, so we
parse it as "{integration_id}:{vmid}", look up the integration row, and query
Proxmox for the VM's lifecycle status. Returns the same shape as the rest of
the status_checker dispatcher: {status, response_time_ms}.

Ephemeral imports (integration_id starts with "ephemeral-") have no stored
credentials, so this returns 'unknown' for them — the user has to re-import
under a saved integration to get live status.

list_vms() is cached per integration for a short TTL so that one tick of the
status checker — which fans out across every node in the canvas — produces a
single Proxmox cluster fetch per integration instead of one fetch per VM.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Node, ProxmoxIntegration
from app.services.proxmox_service import ProxmoxAuth, list_vms
from app.services.proxmox_sync import integration_to_auth

logger = logging.getLogger(__name__)


_UNKNOWN: dict[str, Any] = {"status": "unknown", "response_time_ms": None}

# Short-TTL cache so a status-check tick doesn't fan out into N cluster fetches.
# 25s is < the default status_checker_interval (60s), so the first VM to be
# checked in a tick triggers a fresh fetch and everyone else in that tick reads
# the result. Module-level state because the cache is process-wide.
_VMS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_VMS_LOCKS: dict[str, asyncio.Lock] = {}
_VMS_CACHE_TTL = 25.0


async def _cached_list_vms(integration: ProxmoxIntegration, auth: ProxmoxAuth) -> list[dict[str, Any]]:
    """Return list_vms(integration) with per-integration TTL caching.

    Cache miss path acquires a per-integration lock so concurrent status checks
    don't issue duplicate requests; the second waiter sees the cached result
    immediately on lock acquire.
    """
    now_t = time.monotonic()
    cached = _VMS_CACHE.get(integration.id)
    if cached and now_t - cached[0] < _VMS_CACHE_TTL:
        return cached[1]
    lock = _VMS_LOCKS.setdefault(integration.id, asyncio.Lock())
    async with lock:
        cached = _VMS_CACHE.get(integration.id)
        if cached and time.monotonic() - cached[0] < _VMS_CACHE_TTL:
            return cached[1]
        vms = await list_vms(integration.host, integration.port, auth, integration.verify_tls)
        _VMS_CACHE[integration.id] = (time.monotonic(), vms)
        return vms


def invalidate_cache(integration_id: str) -> None:
    """Drop a cached VM list — call after a sync_integration run so subsequent
    status checks see the freshest data."""
    _VMS_CACHE.pop(integration_id, None)


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

    start = time.monotonic()
    try:
        vms = await _cached_list_vms(integration, auth)
    except Exception as exc:
        logger.debug("Proxmox list_vms failed during status check: %s", exc)
        return {"status": "offline", "response_time_ms": None}

    match = next((vm for vm in vms if vm.get("vmid") == vmid), None)
    if not match:
        return _UNKNOWN

    # Cluster summary is authoritative — `paused` -> offline, anything else
    # unknown -> unknown (don't fall through to per-VM endpoint, which proxies
    # via the owning cluster node and returns 595 when that node is offline,
    # producing log noise without adding any information cluster/resources
    # doesn't already give us).
    summary_status = match.get("status")
    if summary_status == "running":
        resolved = "online"
    elif summary_status in ("stopped", "paused"):
        resolved = "offline"
    else:
        resolved = "unknown"
    return {
        "status": resolved,
        "response_time_ms": int((time.monotonic() - start) * 1000),
    }
