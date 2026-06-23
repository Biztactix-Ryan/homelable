"""Backward-compatibility tests for the legacy → multi-design migration.

Simulates a database created by a pre-"designs" version of the app and asserts
that running init_db() adopts all existing nodes/edges/canvas into a single
default "Network Topology" design with no data loss. The rest of the test suite
builds the *current* schema via create_all and never exercises this upgrade
path, so this file guards real users upgrading in place.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.database as database


@pytest.fixture
def legacy_engine(tmp_path, monkeypatch):
    """Point the module-global engine + sqlite_path at a throwaway legacy DB."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(database.settings, "sqlite_path", str(db_path))
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(database, "engine", engine)
    return db_path, engine


async def _build_legacy_schema(engine) -> None:
    """Create the pre-designs schema (no design_id, integer canvas_state PK)."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE nodes (id VARCHAR PRIMARY KEY, type VARCHAR, label VARCHAR, "
            "status VARCHAR, services JSON, pos_x FLOAT, pos_y FLOAT)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE edges (id VARCHAR PRIMARY KEY, source VARCHAR, target VARCHAR, type VARCHAR)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE canvas_state (id INTEGER PRIMARY KEY, viewport JSON, "
            "custom_style JSON, saved_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, status, services, pos_x, pos_y) "
            "VALUES ('n1','server','Old Server','online','[]',10,20)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, status, services, pos_x, pos_y) "
            "VALUES ('n2','router','Old Router','offline','[]',30,40)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO edges (id, source, target, type) VALUES ('e1','n1','n2','ethernet')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO canvas_state (id, viewport, custom_style, saved_at) "
            "VALUES (1, '{\"x\":5,\"y\":6,\"zoom\":2}', NULL, '2024-01-01 00:00:00')"
        )


async def test_legacy_canvas_migrates_into_default_design(legacy_engine):
    db_path, engine = legacy_engine
    await _build_legacy_schema(engine)

    await database.init_db()

    check = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with check.begin() as conn:
            # Exactly one seeded default design.
            designs = (await conn.exec_driver_sql(
                "SELECT id, name, design_type, icon FROM designs"
            )).fetchall()
            assert len(designs) == 1
            did, name, dtype, icon = designs[0]
            assert name == "Network Topology"
            assert dtype == "network"
            assert icon == "dashboard"

            # Every legacy node adopted into the default design, data preserved.
            nodes = (await conn.exec_driver_sql(
                "SELECT id, label, status, design_id FROM nodes ORDER BY id"
            )).fetchall()
            assert [(n[0], n[1], n[2]) for n in nodes] == [
                ("n1", "Old Server", "online"),
                ("n2", "Old Router", "offline"),
            ]
            assert all(n[3] == did for n in nodes)

            # Legacy edge adopted too.
            edge = (await conn.exec_driver_sql(
                "SELECT design_id FROM edges WHERE id='e1'"
            )).fetchone()
            assert edge[0] == did

            # canvas_state rebuilt with design_id PK; the old id=1 row maps to the
            # default design and the viewport survives.
            cs = (await conn.exec_driver_sql(
                "SELECT design_id, viewport FROM canvas_state"
            )).fetchall()
            assert len(cs) == 1
            assert cs[0][0] == did
            assert "zoom" in (cs[0][1] or "")
    finally:
        await check.dispose()
        await engine.dispose()


async def test_proxmox_columns_added_to_existing_schema(legacy_engine):
    """A DB that predates the Proxmox integration must gain external_source /
    external_id columns and indices on startup, with no data loss.
    """
    db_path, engine = legacy_engine
    await _build_legacy_schema(engine)

    await database.init_db()

    check = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with check.begin() as conn:
            nodes_cols = {
                row[1] for row in
                (await conn.exec_driver_sql("PRAGMA table_info(nodes)")).fetchall()
            }
            assert "external_source" in nodes_cols
            assert "external_id" in nodes_cols

            pending_cols = {
                row[1] for row in
                (await conn.exec_driver_sql("PRAGMA table_info(pending_devices)")).fetchall()
            }
            assert "external_id" in pending_cols

            indices = {
                row[1] for row in
                (await conn.exec_driver_sql("SELECT * FROM sqlite_master WHERE type='index'"))
                .fetchall()
            }
            assert "ix_nodes_external_source" in indices
            assert "ix_nodes_external_id" in indices
            assert "ix_pending_devices_external_id" in indices

            # proxmox_integrations table is brand new and should be auto-created.
            tables = {
                row[0] for row in
                (await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )).fetchall()
            }
            assert "proxmox_integrations" in tables

            # Existing nodes still round-trip.
            nodes = (await conn.exec_driver_sql(
                "SELECT id, label FROM nodes ORDER BY id"
            )).fetchall()
            assert [(n[0], n[1]) for n in nodes] == [("n1", "Old Server"), ("n2", "Old Router")]
    finally:
        await check.dispose()
        await engine.dispose()


async def _build_v25_schema(engine) -> None:
    """A more recent legacy schema — pre-device-split but post-most-other-migrations.

    Includes the identity columns (ip, mac, hostname, ieee_address,
    external_source, external_id) so the device backfill has something
    realistic to chew on.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE designs (id VARCHAR PRIMARY KEY, name VARCHAR, "
            "design_type VARCHAR, icon VARCHAR, created_at DATETIME, updated_at DATETIME)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO designs (id, name, design_type, icon, created_at, updated_at) "
            "VALUES ('d1','Topology','network','dashboard','2026-01-01','2026-01-01')"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE nodes ("
            "id VARCHAR PRIMARY KEY, type VARCHAR, label VARCHAR, design_id VARCHAR, "
            "hostname VARCHAR, ip VARCHAR, mac VARCHAR, os VARCHAR, status VARCHAR, "
            "ieee_address VARCHAR, external_source VARCHAR, external_id VARCHAR, "
            "services JSON, pos_x FLOAT, pos_y FLOAT)"
        )
        # Mix of identity types: one with MAC+IP (nmap-style), one Zigbee
        # (ieee_address), one Proxmox-style (external_source+id).
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, design_id, hostname, ip, mac, status, services) "
            "VALUES ('n1','server','web','d1','web.lan','10.0.0.5','BC:24:11:AA:BB:CC','online','[]')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, design_id, ieee_address, status, services) "
            "VALUES ('n2','iot','light','d1','0xABCDEF01','online','[]')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, design_id, external_source, external_id, status, services) "
            "VALUES ('n3','vm','db','d1','proxmox','i1:100','offline','[]')"
        )
        # Decorative node with no identity — should NOT get a Device.
        await conn.exec_driver_sql(
            "INSERT INTO nodes (id, type, label, design_id, status, services) "
            "VALUES ('n4','text','annotation','d1','unknown','[]')"
        )
        # Edges + canvas_state need to exist so the rest of init_db doesn't choke.
        await conn.exec_driver_sql(
            "CREATE TABLE edges (id VARCHAR PRIMARY KEY, source VARCHAR, target VARCHAR, "
            "type VARCHAR, design_id VARCHAR)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE canvas_state (design_id VARCHAR PRIMARY KEY, viewport JSON, "
            "custom_style JSON, saved_at DATETIME)"
        )


async def test_device_backfill_populates_existing_nodes(legacy_engine):
    """Boot the v2.5 schema (pre-device-split) and confirm init_db creates a
    Device per identity-bearing Node, links Node.device_id, and leaves
    decorative nodes untouched."""
    db_path, engine = legacy_engine
    await _build_v25_schema(engine)

    await database.init_db()

    check = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with check.begin() as conn:
            # Every identity-bearing legacy node now has device_id set.
            rows = (await conn.exec_driver_sql(
                "SELECT id, device_id FROM nodes ORDER BY id"
            )).fetchall()
            by_id = dict(rows)
            assert by_id["n1"] is not None
            assert by_id["n2"] is not None
            assert by_id["n3"] is not None
            # Decorative node: still NULL (nothing to match on, nothing to invent).
            assert by_id["n4"] is None

            # Three distinct devices (each Node had a different identity).
            count_row = (await conn.exec_driver_sql(
                "SELECT COUNT(*) FROM devices"
            )).fetchone()
            assert count_row[0] == 3

            # The Proxmox-shaped Node's Device carries external_source/external_id.
            d = (await conn.exec_driver_sql(
                "SELECT external_source, external_id, ieee_address, primary_mac "
                "FROM devices WHERE id = ?", (by_id["n3"],),
            )).fetchone()
            assert d == ("proxmox", "i1:100", None, None)

            # The nmap-shaped Node's Device carries MAC (lowercased).
            d2 = (await conn.exec_driver_sql(
                "SELECT primary_mac, primary_ip, primary_hostname FROM devices WHERE id = ?",
                (by_id["n1"],),
            )).fetchone()
            assert d2 == ("bc:24:11:aa:bb:cc", "10.0.0.5", "web.lan")

            # The Zigbee-shaped Node's Device carries ieee_address.
            d3 = (await conn.exec_driver_sql(
                "SELECT ieee_address FROM devices WHERE id = ?", (by_id["n2"],),
            )).fetchone()
            assert d3 == ("0xABCDEF01",)
    finally:
        await check.dispose()
        await engine.dispose()


async def test_device_backfill_is_idempotent(legacy_engine):
    """Running init_db twice must not duplicate Device rows."""
    db_path, engine = legacy_engine
    await _build_v25_schema(engine)

    await database.init_db()
    await database.init_db()

    check = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with check.begin() as conn:
            devices = (await conn.exec_driver_sql("SELECT COUNT(*) FROM devices")).fetchone()
            assert devices[0] == 3
            # device_id links unchanged after second run.
            n3 = (await conn.exec_driver_sql(
                "SELECT device_id FROM nodes WHERE id='n3'"
            )).fetchone()
            assert n3[0] is not None
    finally:
        await check.dispose()
        await engine.dispose()


async def test_migration_is_idempotent(legacy_engine):
    """Running init_db twice must not duplicate the design or drop any data."""
    db_path, engine = legacy_engine
    await _build_legacy_schema(engine)

    await database.init_db()
    await database.init_db()  # second boot — should be a no-op

    check = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with check.begin() as conn:
            designs = (await conn.exec_driver_sql("SELECT id FROM designs")).fetchall()
            assert len(designs) == 1
            did = designs[0][0]

            nodes = (await conn.exec_driver_sql(
                "SELECT design_id FROM nodes"
            )).fetchall()
            assert len(nodes) == 2
            assert all(n[0] == did for n in nodes)

            cs = (await conn.exec_driver_sql("SELECT design_id FROM canvas_state")).fetchall()
            assert len(cs) == 1
            assert cs[0][0] == did
    finally:
        await check.dispose()
        await engine.dispose()
