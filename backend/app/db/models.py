import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Design(Base):
    __tablename__ = "designs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    design_type: Mapped[str] = mapped_column(String, nullable=False, default="network")
    icon: Mapped[str | None] = mapped_column(String, nullable=True, default="dashboard")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Device(Base):
    """Canonical identity for a physical or virtual entity discovered by any
    source. Lives independently of any canvas — a Device exists once and any
    number of Nodes can reference it (across multiple designs).

    Identity facts (mac, hostname, ip) are tracked as both a `primary_*`
    indexed column (the most recently observed value, used for lookups) and a
    `*s` JSON list (the full set of values ever seen, used to match later
    discoveries that surface an older value).
    """

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    primary_mac: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    macs: Mapped[list[str]] = mapped_column(JSON, default=list)
    primary_hostname: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    hostnames: Mapped[list[str]] = mapped_column(JSON, default=list)
    primary_ip: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    ips: Mapped[list[str]] = mapped_column(JSON, default=list)
    ieee_address: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    external_source: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoveryFact(Base):
    """Append-only log of what each discovery source observed about a Device.

    Lets you answer "where did I learn this device exists?", "when did Proxmox
    last confirm this VM?", "did LLDP see this switch behind another switch?".
    Source code never deletes these; they're a permanent audit trail.
    """

    __tablename__ = "discovery_facts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(
        String, ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False,
    )
    source: Mapped[str] = mapped_column(String, index=True, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    design_id: Mapped[str | None] = mapped_column(String, ForeignKey("designs.id", ondelete="SET NULL"), nullable=True)
    # FK to the canonical Device. Nullable because decorative nodes (group
    # rects, text labels) have no underlying device, and because legacy rows
    # haven't been backfilled yet. ondelete=SET NULL preserves the canvas
    # placement if a device gets purged.
    device_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("devices.id", ondelete="SET NULL"), index=True, nullable=True,
    )
    hostname: Mapped[str | None] = mapped_column(String)
    ip: Mapped[str | None] = mapped_column(String)
    mac: Mapped[str | None] = mapped_column(String)
    os: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="unknown")
    check_method: Mapped[str | None] = mapped_column(String)
    check_target: Mapped[str | None] = mapped_column(String)
    services: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)
    pos_x: Mapped[float] = mapped_column(Float, default=0)
    pos_y: Mapped[float] = mapped_column(Float, default=0)
    parent_id: Mapped[str | None] = mapped_column(String, ForeignKey("nodes.id", ondelete="CASCADE"))
    container_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    custom_colors: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    custom_icon: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_model: Mapped[str | None] = mapped_column(String, nullable=True)
    ram_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    show_hardware: Mapped[bool] = mapped_column(Boolean, default=False)
    show_port_numbers: Mapped[bool] = mapped_column(Boolean, default=False)
    properties: Mapped[list[Any]] = mapped_column(JSON, default=list)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    bottom_handles: Mapped[int] = mapped_column(Integer, default=1)
    ieee_address: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    external_source: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    children: Mapped[list["Node"]] = relationship("Node", back_populates="parent")
    parent: Mapped["Node | None"] = relationship("Node", back_populates="children", remote_side=[id])


class Edge(Base):
    __tablename__ = "edges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String, ForeignKey("nodes.id", ondelete="CASCADE"))
    target: Mapped[str] = mapped_column(String, ForeignKey("nodes.id", ondelete="CASCADE"))
    design_id: Mapped[str | None] = mapped_column(String, ForeignKey("designs.id", ondelete="SET NULL"), nullable=True)
    type: Mapped[str] = mapped_column(String, default="ethernet")
    label: Mapped[str | None] = mapped_column(String)
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    speed: Mapped[str | None] = mapped_column(String)
    custom_color: Mapped[str | None] = mapped_column(String)
    path_style: Mapped[str | None] = mapped_column(String)
    animated: Mapped[str] = mapped_column(String, nullable=False, default='none')
    source_handle: Mapped[str | None] = mapped_column(String)
    target_handle: Mapped[str | None] = mapped_column(String)
    waypoints: Mapped[list[dict[str, float]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CanvasState(Base):
    __tablename__ = "canvas_state"

    design_id: Mapped[str] = mapped_column(String, ForeignKey("designs.id", ondelete="CASCADE"), primary_key=True)
    viewport: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    custom_style: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PendingDevice(Base):
    __tablename__ = "pending_devices"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    device_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("devices.id", ondelete="SET NULL"), index=True, nullable=True,
    )
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    mac: Mapped[str | None] = mapped_column(String)
    hostname: Mapped[str | None] = mapped_column(String)
    os: Mapped[str | None] = mapped_column(String)
    services: Mapped[list[Any]] = mapped_column(JSON, default=list)
    suggested_type: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    discovery_source: Mapped[str | None] = mapped_column(String)
    ieee_address: Mapped[str | None] = mapped_column(String, index=True, nullable=True, unique=True)
    friendly_name: Mapped[str | None] = mapped_column(String, nullable=True)
    device_subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    lqi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PendingDeviceLink(Base):
    """Link between two Zigbee endpoints discovered during import.

    Endpoints are addressed by IEEE (stable across re-imports). Either side may
    already exist as a canvas Node (resolved via Node.ieee_address) or still be
    a PendingDevice. On approval, the matching Edge is auto-created when both
    endpoints exist as canvas Nodes.
    """

    __tablename__ = "pending_device_links"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source_ieee: Mapped[str] = mapped_column(String, nullable=False, index=True)
    target_ieee: Mapped[str] = mapped_column(String, nullable=False, index=True)
    lqi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovery_source: Mapped[str] = mapped_column(String, nullable=False, default="zigbee")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String, default="running")
    kind: Mapped[str] = mapped_column(String, default="ip", server_default="ip")
    ranges: Mapped[list[str]] = mapped_column(JSON, default=list)
    devices_found: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ProxmoxIntegration(Base):
    """Stored credentials + sync config for a Proxmox VE host or cluster.

    Created when the user ticks "save credentials" during import. Used by the
    background sync job (discovers new VMs into pending_devices, refreshes
    status on already-imported nodes) and by the per-node "proxmox" status
    check method.

    Secret values (token_secret, password) are Fernet-encrypted at rest; see
    app.core.crypto.
    """

    __tablename__ = "proxmox_integrations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    host: Mapped[str] = mapped_column(String, nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=8006)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_type: Mapped[str] = mapped_column(String, nullable=False)  # "token" | "password"
    # Token auth (auth_type == "token"): user@realm!tokenid + secret
    token_user: Mapped[str | None] = mapped_column(String, nullable=True)
    token_id: Mapped[str | None] = mapped_column(String, nullable=True)
    token_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Password auth (auth_type == "password"): user@realm + password
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String, nullable=True)  # "ok" | "error"
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
