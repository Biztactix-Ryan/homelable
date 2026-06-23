"""Unit tests for app.services.device_service.resolve_device."""
from __future__ import annotations

import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device, DiscoveryFact
from app.services.device_service import record_fact, resolve_device


@pytest.mark.asyncio
async def test_resolve_creates_new_device_on_miss(db_session: AsyncSession):
    device = await resolve_device(
        db_session, mac="AA:BB:CC:11:22:33", ip="10.0.0.1", hostname="server1",
    )
    assert device.id
    # MAC is normalised to lowercase
    assert device.primary_mac == "aa:bb:cc:11:22:33"
    assert device.macs == ["aa:bb:cc:11:22:33"]
    assert device.primary_ip == "10.0.0.1"
    assert device.primary_hostname == "server1"


@pytest.mark.asyncio
async def test_resolve_match_by_external_source_and_id(db_session: AsyncSession):
    d1 = await resolve_device(
        db_session, external_source="proxmox", external_id="i1:100",
        hostname="web", mac="aa:bb:cc:00:00:01",
    )
    # Second resolve with same external_source+external_id but different
    # incidental facts (different IP) MUST find the existing device, not
    # create a new one.
    d2 = await resolve_device(
        db_session, external_source="proxmox", external_id="i1:100",
        ip="10.0.0.5",
    )
    assert d2.id == d1.id
    # New IP merges in
    assert d2.primary_ip == "10.0.0.5"
    assert "10.0.0.5" in d2.ips


@pytest.mark.asyncio
async def test_resolve_match_by_mac_across_sources(db_session: AsyncSession):
    """The headline use case: nmap and Proxmox see the same MAC -> one Device."""
    nmap_device = await resolve_device(
        db_session, mac="bc:24:11:aa:bb:cc", ip="10.0.0.5", hostname="web.local",
    )
    proxmox_device = await resolve_device(
        db_session, mac="BC:24:11:AA:BB:CC",
        external_source="proxmox", external_id="i1:100",
        kind="vm",
    )
    assert proxmox_device.id == nmap_device.id
    # The Proxmox-side identity merges onto the existing device
    assert proxmox_device.external_source == "proxmox"
    assert proxmox_device.external_id == "i1:100"
    assert proxmox_device.kind == "vm"


@pytest.mark.asyncio
async def test_resolve_match_by_ieee_address(db_session: AsyncSession):
    d1 = await resolve_device(db_session, ieee_address="0xABCDEF01")
    d2 = await resolve_device(
        db_session, ieee_address="0xABCDEF01", hostname="zigbee-light",
    )
    assert d2.id == d1.id
    assert d2.primary_hostname == "zigbee-light"


@pytest.mark.asyncio
async def test_resolve_match_by_hostname_when_no_mac(db_session: AsyncSession):
    d1 = await resolve_device(db_session, hostname="switch01")
    d2 = await resolve_device(db_session, hostname="switch01", ip="10.0.0.2")
    assert d2.id == d1.id


@pytest.mark.asyncio
async def test_resolve_match_by_ip_as_last_resort(db_session: AsyncSession):
    d1 = await resolve_device(db_session, ip="10.0.0.99")
    d2 = await resolve_device(db_session, ip="10.0.0.99")
    assert d2.id == d1.id


@pytest.mark.asyncio
async def test_resolve_precedence_external_id_beats_ip_match(db_session: AsyncSession):
    """Two devices, same IP, different external_ids -> separate identities."""
    d1 = await resolve_device(
        db_session, external_source="proxmox", external_id="i1:100", ip="10.0.0.5",
    )
    d2 = await resolve_device(
        db_session, external_source="proxmox", external_id="i1:200", ip="10.0.0.5",
    )
    assert d1.id != d2.id


@pytest.mark.asyncio
async def test_resolve_doesnt_overwrite_primary_with_later_value(db_session: AsyncSession):
    d = await resolve_device(db_session, hostname="first")
    d2 = await resolve_device(db_session, hostname="first", ip="10.0.0.1")
    d3 = await resolve_device(db_session, hostname="first", ip="10.0.0.2")
    assert d3.id == d.id == d2.id
    # primary_ip is sticky to the first observation; the new IP still lands in the list.
    assert d3.primary_ip == "10.0.0.1"
    assert "10.0.0.1" in d3.ips
    assert "10.0.0.2" in d3.ips


@pytest.mark.asyncio
async def test_record_fact_appends_to_log(db_session: AsyncSession):
    device = await resolve_device(db_session, hostname="x")
    f1 = await record_fact(
        db_session, device=device, source="proxmox", source_ref="i1:100",
        facts={"status": "running"},
    )
    f2 = await record_fact(
        db_session, device=device, source="nmap", source_ref="10.0.0.5",
        facts={"open_ports": [80]},
    )
    assert f1.device_id == device.id
    assert f2.device_id == device.id
    from sqlalchemy import select
    facts = (await db_session.execute(
        select(DiscoveryFact).where(DiscoveryFact.device_id == device.id)
    )).scalars().all()
    assert {f.source for f in facts} == {"proxmox", "nmap"}


@pytest.mark.asyncio
async def test_empty_facts_returns_isolated_devices(db_session: AsyncSession):
    """Calling with no identifying facts should create a new Device every time,
    since there's nothing to match on. Avoids accidentally merging unrelated
    callers that forgot to pass keys."""
    d1 = await resolve_device(db_session)
    d2 = await resolve_device(db_session)
    assert d1.id != d2.id


@pytest.mark.asyncio
async def test_devices_count(db_session: AsyncSession):
    """Resolve N times with the same MAC -> one Device row total."""
    for _ in range(5):
        await resolve_device(db_session, mac="11:22:33:aa:bb:cc")
    from sqlalchemy import select
    devices = (await db_session.execute(select(Device))).scalars().all()
    assert len(devices) == 1
