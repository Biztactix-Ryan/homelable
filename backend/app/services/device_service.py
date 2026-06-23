"""Cross-source device identity resolution.

`resolve_device` is the single entry point every discovery flow goes through:
given a bag of facts (MAC / IP / hostname / IEEE / external_source+id), it
returns the canonical Device row, creating it if no match exists. On match it
*merges* the new facts onto the existing Device — appending to the historic
lists (`macs`, `ips`, `hostnames`), promoting the new value to the indexed
`primary_*` column, and bumping `last_seen`.

Match precedence (highest to lowest reliability):
1. external_source + external_id   — source-controlled, exact
2. ieee_address                    — Zigbee identity, globally unique
3. MAC                             — globally unique per NIC
4. hostname                        — soft, can collide across networks
5. IP                              — least reliable (DHCP, NAT)

`record_fact` writes an entry to discovery_facts so we keep an append-only
audit log of what each source saw and when.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device, DiscoveryFact

logger = logging.getLogger(__name__)


def _norm_mac(mac: str | None) -> str | None:
    return mac.lower() if mac else None


async def resolve_device(
    db: AsyncSession,
    *,
    mac: str | None = None,
    ip: str | None = None,
    hostname: str | None = None,
    ieee_address: str | None = None,
    external_source: str | None = None,
    external_id: str | None = None,
    vendor: str | None = None,
    kind: str | None = None,
) -> Device:
    """Find or create the canonical Device for these facts; merge new info.

    The caller is responsible for committing the session — this function
    flushes but never commits, so multi-step flows (resolve + attach Node +
    record fact) sit inside one transaction.
    """
    mac_lc = _norm_mac(mac)

    device = await _match_by_precedence(
        db, mac=mac_lc, ip=ip, hostname=hostname, ieee_address=ieee_address,
        external_source=external_source, external_id=external_id,
    )
    if device is None:
        device = Device(
            primary_mac=mac_lc,
            macs=[mac_lc] if mac_lc else [],
            primary_hostname=hostname,
            hostnames=[hostname] if hostname else [],
            primary_ip=ip,
            ips=[ip] if ip else [],
            ieee_address=ieee_address,
            external_source=external_source,
            external_id=external_id,
            vendor=vendor,
            kind=kind,
        )
        db.add(device)
        await db.flush()
        return device

    # Match — merge in new facts without overwriting non-empty primaries.
    _merge_into_device(
        device, mac=mac_lc, ip=ip, hostname=hostname,
        ieee_address=ieee_address, external_source=external_source,
        external_id=external_id, vendor=vendor, kind=kind,
    )
    await db.flush()
    return device


async def record_fact(
    db: AsyncSession,
    *,
    device: Device,
    source: str,
    source_ref: str | None = None,
    facts: dict[str, Any] | None = None,
) -> DiscoveryFact:
    """Append a DiscoveryFact log entry for a (device, source) observation."""
    fact = DiscoveryFact(
        device_id=device.id,
        source=source,
        source_ref=source_ref,
        facts=facts or {},
    )
    db.add(fact)
    await db.flush()
    return fact


def _identity_conflicts(
    candidate: Device,
    *,
    external_source: str | None,
    external_id: str | None,
    ieee_address: str | None,
) -> bool:
    """True if matching `candidate` would conflict with a stronger identity
    the caller has explicitly asserted.

    Used when a weaker matcher (MAC, hostname, IP) finds a candidate that
    already has a different *strong* identity (external_source+external_id or
    ieee_address) than the caller claims. Two Proxmox VMs sharing an IP are a
    realistic case — they have different external_ids, so don't merge them
    just because nmap saw the same IP.
    """
    if external_source and external_id and candidate.external_source and candidate.external_id:
        if (candidate.external_source, candidate.external_id) != (external_source, external_id):
            return True
    if ieee_address and candidate.ieee_address and candidate.ieee_address != ieee_address:
        return True
    return False


async def _match_by_precedence(
    db: AsyncSession,
    *,
    mac: str | None,
    ip: str | None,
    hostname: str | None,
    ieee_address: str | None,
    external_source: str | None,
    external_id: str | None,
) -> Device | None:
    def _conflicts(candidate: Device) -> bool:
        return _identity_conflicts(
            candidate,
            external_source=external_source,
            external_id=external_id,
            ieee_address=ieee_address,
        )

    # 1. external_source + external_id — exact, authoritative.
    if external_source and external_id:
        result = await db.execute(
            select(Device).where(
                Device.external_source == external_source,
                Device.external_id == external_id,
            )
        )
        device = result.scalar_one_or_none()
        if device:
            return device

    # 2. ieee_address — globally unique per Zigbee device.
    if ieee_address:
        result = await db.execute(
            select(Device).where(Device.ieee_address == ieee_address)
        )
        device = result.scalar_one_or_none()
        if device and not _conflicts(device):
            return device

    # 3. MAC — check both the indexed primary_mac and the historical macs list.
    if mac:
        result = await db.execute(select(Device).where(Device.primary_mac == mac))
        device = result.scalar_one_or_none()
        if device and not _conflicts(device):
            return device
        # Fall back to scanning JSON lists for historical MACs. Homelab scale
        # (<<1000 devices) — if it ever bites, move to a dedicated
        # mac_aliases table indexed individually.
        result = await db.execute(select(Device).where(Device.primary_mac.is_(None)))
        for candidate in result.scalars().all():
            if mac in (candidate.macs or []) and not _conflicts(candidate):
                return candidate

    # 4. hostname — soft, can collide across networks.
    if hostname:
        result = await db.execute(
            select(Device).where(Device.primary_hostname == hostname)
        )
        device = result.scalar_one_or_none()
        if device and not _conflicts(device):
            return device

    # 5. IP — least reliable, last resort.
    if ip:
        result = await db.execute(select(Device).where(Device.primary_ip == ip))
        device = result.scalar_one_or_none()
        if device and not _conflicts(device):
            return device

    return None


def _merge_into_device(
    device: Device,
    *,
    mac: str | None,
    ip: str | None,
    hostname: str | None,
    ieee_address: str | None,
    external_source: str | None,
    external_id: str | None,
    vendor: str | None,
    kind: str | None,
) -> None:
    """Merge new facts into an existing Device row.

    Rules:
    - `primary_*` is set only if currently None (latest wins for first-write,
      but we don't churn it on every discovery — that would defeat indexing).
      Newer values still land in the corresponding list so lookups against
      them work.
    - List columns are deduped append-only.
    - vendor/kind: fill only when currently empty.
    """
    if mac:
        if device.primary_mac is None:
            device.primary_mac = mac
        macs = list(device.macs or [])
        if mac not in macs:
            macs.append(mac)
            device.macs = macs
    if ip:
        if device.primary_ip is None:
            device.primary_ip = ip
        ips = list(device.ips or [])
        if ip not in ips:
            ips.append(ip)
            device.ips = ips
    if hostname:
        if device.primary_hostname is None:
            device.primary_hostname = hostname
        hostnames = list(device.hostnames or [])
        if hostname not in hostnames:
            hostnames.append(hostname)
            device.hostnames = hostnames
    if ieee_address and device.ieee_address is None:
        device.ieee_address = ieee_address
    if external_source and device.external_source is None:
        device.external_source = external_source
    if external_id and device.external_id is None:
        device.external_id = external_id
    if vendor and not device.vendor:
        device.vendor = vendor
    if kind and not device.kind:
        device.kind = kind
