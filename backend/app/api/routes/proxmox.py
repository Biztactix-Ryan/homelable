"""FastAPI router for Proxmox VE integration.

Endpoints:
- POST /test-connection          ephemeral creds, validates the API is reachable
- POST /list                     ephemeral creds, returns all VMs + LXCs
- POST /import                   adds selected VMs as child nodes of a Proxmox host node,
                                 optionally persists the credentials as a saved integration
- GET  /integrations             list saved integrations (secrets redacted)
- DELETE /integrations/{id}      remove a saved integration (Node rows are kept)
- POST /integrations/{id}/sync   trigger an immediate sync run for one saved integration
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.crypto import encrypt
from app.db.database import get_db
from app.db.models import Node, ProxmoxIntegration
from app.schemas.proxmox import (
    ProxmoxImportRequest,
    ProxmoxImportResponse,
    ProxmoxIntegrationOut,
    ProxmoxListRequest,
    ProxmoxListResponse,
    ProxmoxSyncResponse,
    ProxmoxTestRequest,
    ProxmoxTestResponse,
    ProxmoxVMOut,
)
from app.services import proxmox_sync
from app.services.device_service import record_fact, resolve_device
from app.services.proxmox_service import (
    ProxmoxAuth,
    check_connection,
    get_vm_macs,
    list_vms,
    status_to_node_status,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _auth_from_request(req: ProxmoxTestRequest | ProxmoxListRequest | ProxmoxImportRequest) -> ProxmoxAuth:
    if req.auth_type == "token":
        return ProxmoxAuth(
            token_user=req.token_user,
            token_id=req.token_id,
            token_secret=req.token_secret,
        )
    return ProxmoxAuth(username=req.username, password=req.password)


@router.post("/test-connection", response_model=ProxmoxTestResponse)
async def test_proxmox_connection(
    payload: ProxmoxTestRequest,
    _: str = Depends(get_current_user),
) -> ProxmoxTestResponse:
    try:
        await check_connection(payload.host, payload.port, _auth_from_request(payload), payload.verify_tls)
    except ConnectionError as exc:
        return ProxmoxTestResponse(connected=False, message=str(exc))
    return ProxmoxTestResponse(connected=True, message="Connected.")


@router.post("/list", response_model=ProxmoxListResponse)
async def list_proxmox_vms(
    payload: ProxmoxListRequest,
    _: str = Depends(get_current_user),
) -> ProxmoxListResponse:
    """Return every VM and LXC in the cluster. Used for the import-modal preview."""
    try:
        raw = await list_vms(payload.host, payload.port, _auth_from_request(payload), payload.verify_tls)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ProxmoxListResponse(vms=[ProxmoxVMOut(**vm) for vm in raw])


@router.post("/import", response_model=ProxmoxImportResponse)
async def import_proxmox_vms(
    payload: ProxmoxImportRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> ProxmoxImportResponse:
    """Add selected VMs as canvas nodes under a Proxmox host node.

    Creates the host node (container_mode=True) on first import for a given
    integration, then adds each selected VM as a child. Re-imports are
    idempotent: VMs already on the canvas under this integration are listed
    in `skipped_existing` rather than duplicated.
    """
    auth = _auth_from_request(payload)
    try:
        all_vms = await list_vms(payload.host, payload.port, auth, payload.verify_tls)
    except ConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    selected = {vm["vmid"] for vm in all_vms if vm["vmid"] in set(payload.selected_vmids)}
    if not selected:
        raise HTTPException(status_code=422, detail="None of the selected vmids were returned by Proxmox.")

    integration_id: str | None = None
    if payload.save_credentials:
        integration = _build_integration_row(payload)
        db.add(integration)
        await db.flush()
        integration_id = integration.id
    else:
        # Generate a stable id even for ephemeral imports so the host/VM rows have a
        # coherent external_id (`{integration_id}:{vmid}`). The Node rows survive
        # even though no ProxmoxIntegration row was persisted; they just won't
        # auto-sync.
        from uuid import uuid4

        integration_id = f"ephemeral-{uuid4().hex[:12]}"

    host_node = await _get_or_create_host_node(db, integration_id, payload)

    created_ids: list[str] = []
    skipped: list[int] = []
    existing_by_external = await _existing_vm_node_map(db, integration_id)
    for vm in all_vms:
        if vm["vmid"] not in selected:
            continue
        if vm["vmid"] in existing_by_external:
            skipped.append(vm["vmid"])
            continue
        # Best-effort MAC enrichment so cross-source dedup (vs nmap, LLDP, ...)
        # can match by L2 identity. Failure of the config endpoint must never
        # block the import — get_vm_macs already swallows errors.
        macs = await get_vm_macs(
            payload.host, payload.port, auth, payload.verify_tls,
            vm["node"], vm["type"], vm["vmid"],
        )
        primary_mac = macs[0] if macs else None
        ext = f"{integration_id}:{vm['vmid']}"
        device = await resolve_device(
            db,
            mac=primary_mac,
            hostname=vm.get("name"),
            external_source="proxmox",
            external_id=ext,
            kind="lxc" if vm["type"] == "lxc" else "vm",
        )
        # Surface every NIC on the Device (multi-homed VMs).
        for extra_mac in macs[1:]:
            await resolve_device(db, mac=extra_mac, external_source="proxmox", external_id=ext)
        await record_fact(
            db,
            device=device,
            source="proxmox",
            source_ref=ext,
            facts={
                "vmid": vm["vmid"],
                "type": vm["type"],
                "node": vm.get("node"),
                "status": vm.get("status"),
                "cpu_count": vm.get("cpu_count"),
                "ram_gb": vm.get("ram_gb"),
                "disk_gb": vm.get("disk_gb"),
                "macs": macs,
            },
        )
        node = _vm_to_node(vm, host_node.id, integration_id, payload.design_id, device.id, primary_mac)
        db.add(node)
        await db.flush()
        created_ids.append(node.id)

    await db.commit()
    return ProxmoxImportResponse(
        host_node_id=host_node.id,
        created_node_ids=created_ids,
        skipped_existing=skipped,
        integration_id=integration_id if payload.save_credentials else None,
    )


@router.get("/integrations", response_model=list[ProxmoxIntegrationOut])
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> list[ProxmoxIntegration]:
    result = await db.execute(select(ProxmoxIntegration).order_by(ProxmoxIntegration.created_at))
    return list(result.scalars().all())


@router.delete("/integrations/{integration_id}", status_code=204)
async def delete_integration(
    integration_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> None:
    row = await db.get(ProxmoxIntegration, integration_id)
    if not row:
        raise HTTPException(status_code=404, detail="Integration not found")
    await db.delete(row)
    await db.commit()


@router.post("/integrations/{integration_id}/sync", response_model=ProxmoxSyncResponse)
async def sync_integration(
    integration_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_user),
) -> ProxmoxSyncResponse:
    row = await db.get(ProxmoxIntegration, integration_id)
    if not row:
        raise HTTPException(status_code=404, detail="Integration not found")
    return await proxmox_sync.sync_integration(db, row)


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_integration_row(payload: ProxmoxImportRequest) -> ProxmoxIntegration:
    if payload.auth_type == "token":
        assert payload.token_secret  # validated by ProxmoxCredentials
        return ProxmoxIntegration(
            name=payload.integration_name,
            host=payload.host,
            port=payload.port,
            verify_tls=payload.verify_tls,
            auth_type="token",
            token_user=payload.token_user,
            token_id=payload.token_id,
            token_secret_enc=encrypt(payload.token_secret),
            sync_interval_minutes=payload.sync_interval_minutes,
        )
    assert payload.password  # validated by ProxmoxCredentials
    return ProxmoxIntegration(
        name=payload.integration_name,
        host=payload.host,
        port=payload.port,
        verify_tls=payload.verify_tls,
        auth_type="password",
        username=payload.username,
        password_enc=encrypt(payload.password),
        sync_interval_minutes=payload.sync_interval_minutes,
    )


async def _get_or_create_host_node(
    db: AsyncSession, integration_id: str, payload: ProxmoxImportRequest
) -> Node:
    """Find the Proxmox host node for this integration or create it.

    The host itself is a device (identity = hostname + external_id of the
    integration), so it gets a Device row + DiscoveryFact too — that way a
    later nmap discovery of the Proxmox host's IP can dedupe against it.
    """
    host_external_id = f"{integration_id}:host"
    result = await db.execute(
        select(Node).where(
            Node.external_source == "proxmox-host",
            Node.external_id == host_external_id,
        )
    )
    node = result.scalar_one_or_none()
    if node:
        return node
    device = await resolve_device(
        db,
        hostname=payload.host,
        external_source="proxmox-host",
        external_id=host_external_id,
        kind="proxmox",
    )
    await record_fact(
        db,
        device=device,
        source="proxmox-host",
        source_ref=host_external_id,
        facts={"host": payload.host, "port": payload.port},
    )
    node = Node(
        type="proxmox",
        label=payload.integration_name,
        hostname=payload.host,
        status="unknown",
        container_mode=True,
        design_id=payload.design_id,
        device_id=device.id,
        external_source="proxmox-host",
        external_id=host_external_id,
    )
    db.add(node)
    await db.flush()
    return node


async def _existing_vm_node_map(db: AsyncSession, integration_id: str) -> dict[int, Node]:
    """Return {vmid: Node} for VMs already on the canvas under this integration."""
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


def _vm_to_node(
    vm: dict[str, Any],
    host_id: str,
    integration_id: str,
    design_id: str | None,
    device_id: str | None,
    primary_mac: str | None,
) -> Node:
    """Build a Node row from a normalized VM dict.

    `device_id` is the canonical identity row (see resolve_device). The Node's
    legacy identity columns (mac, etc.) are denormalised cache mirroring the
    Device — kept populated so the rest of the app (which still reads them)
    keeps working until those code paths are migrated.
    """
    ext = f"{integration_id}:{vm['vmid']}"
    return Node(
        type="lxc" if vm["type"] == "lxc" else "vm",
        label=vm["name"],
        parent_id=host_id,
        design_id=design_id,
        device_id=device_id,
        status=status_to_node_status(vm.get("status")),
        check_method="proxmox",
        # check_target stores the external_id so the status checker can find
        # the integration + vmid without a separate column.
        check_target=ext,
        mac=primary_mac,
        cpu_count=vm.get("cpu_count"),
        ram_gb=vm.get("ram_gb"),
        disk_gb=vm.get("disk_gb"),
        external_source="proxmox",
        external_id=ext,
    )
