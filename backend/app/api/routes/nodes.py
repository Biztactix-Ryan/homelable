from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.database import get_db
from app.db.models import Node, PendingDevice
from app.schemas.nodes import NodeCreate, NodeResponse, NodeUpdate

router = APIRouter()


@router.get("", response_model=list[NodeResponse])
async def list_nodes(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)) -> list[Node]:
    result = await db.execute(select(Node))
    return list(result.scalars().all())


@router.post("", response_model=NodeResponse, status_code=status.HTTP_201_CREATED)
async def create_node(body: NodeCreate, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)) -> Node:
    node = Node(**body.model_dump())
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)) -> Node:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    return node


@router.patch("/{node_id}", response_model=NodeResponse)
async def update_node(
    node_id: str, body: NodeUpdate, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)
) -> Node:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(node, field, value)
    await db.commit()
    await db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: str, db: AsyncSession = Depends(get_db), _: str = Depends(get_current_user)) -> None:
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
    # Tombstone nodes from external systems (Proxmox, future integrations) so a
    # scheduled re-sync doesn't immediately reappear them in pending devices.
    # The self row is tombstoned, plus any descendants that are themselves
    # external-sourced (e.g. deleting a Proxmox host node also hides every VM
    # under it). DB-level CASCADE then takes care of the actual delete.
    await _tombstone_external_descendants(db, node)
    await db.delete(node)
    await db.commit()


async def _tombstone_external_descendants(db: AsyncSession, node: Node) -> None:
    """Create hidden PendingDevice rows for any external-sourced node being
    deleted, so its source system's next sync skips re-creating it."""
    targets: list[Node] = []
    if node.external_source:
        targets.append(node)
    # Walk children — only one level here, but cheap and covers the
    # Proxmox-host-with-VM-children case which is the realistic scenario.
    stack = [node]
    while stack:
        current = stack.pop()
        children = (await db.execute(
            select(Node).where(Node.parent_id == current.id)
        )).scalars().all()
        for child in children:
            if child.external_source:
                targets.append(child)
            stack.append(child)

    for n in targets:
        # Skip if a tombstone already exists for this external_id — keep the
        # delete idempotent.
        existing = await db.execute(
            select(PendingDevice).where(PendingDevice.external_id == n.external_id)
        )
        if existing.scalar_one_or_none() is not None:
            continue
        # discovery_source = the source key the originating sync engine uses to
        # locate pending rows; for our Proxmox sync that's "proxmox", and a
        # "proxmox-host" delete cascades to VM children which are already
        # discovery_source="proxmox".
        source = n.external_source if n.external_source != "proxmox-host" else "proxmox"
        db.add(PendingDevice(
            device_id=n.device_id,
            ip=n.ip,
            mac=n.mac,
            hostname=n.hostname,
            os=n.os,
            services=list(n.services or []),
            suggested_type=n.type,
            status="hidden",
            discovery_source=source,
            external_id=n.external_id,
            friendly_name=n.label,
        ))
