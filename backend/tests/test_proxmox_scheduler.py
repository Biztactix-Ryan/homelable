"""Regression test for the scheduler's Proxmox sync tick.

The original code subtracted a tz-naive `last_sync_at` (SQLite returns naive
values for DateTime(timezone=True) columns) from a tz-aware `now`, which
raised TypeError every minute once any integration had a recorded sync.
"""
from __future__ import annotations

import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.scheduler as scheduler_mod
import app.db.database as database
from app.db.database import Base
from app.db.models import ProxmoxIntegration


@pytest.fixture
async def mem_session(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'sched.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_mod, "AsyncSessionLocal", session_factory)
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_proxmox_syncs_handles_naive_last_sync(mem_session):
    """A naive last_sync_at must not crash the tick — it should be treated as UTC."""
    factory = mem_session
    naive_recent = datetime(2026, 6, 22, 5, 0, 0)  # well within 15-minute interval
    async with factory() as db:
        db.add(ProxmoxIntegration(
            id="i1", name="x", host="h", port=8006, verify_tls=False,
            auth_type="token", token_user="root@pam", token_id="t", token_secret_enc="e",
            sync_interval_minutes=15, last_sync_at=naive_recent,
        ))
        await db.commit()

    with patch("app.core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 22, 5, 1, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        # If the bug is reintroduced this raises TypeError; the assertion below
        # just confirms the tick ran to completion.
        with patch("app.services.proxmox_sync.sync_integration", new=AsyncMock()) as mock_sync:
            await scheduler_mod._run_proxmox_syncs()
            # Last sync was 1 minute ago, interval is 15 — should NOT have re-synced.
            assert mock_sync.call_count == 0


@pytest.mark.asyncio
async def test_run_proxmox_syncs_runs_when_interval_elapsed(mem_session):
    factory = mem_session
    long_ago = datetime(2026, 6, 22, 4, 0, 0)  # naive, 1h+ in the past
    async with factory() as db:
        db.add(ProxmoxIntegration(
            id="i1", name="x", host="h", port=8006, verify_tls=False,
            auth_type="token", token_user="root@pam", token_id="t", token_secret_enc="e",
            sync_interval_minutes=15, last_sync_at=long_ago,
        ))
        await db.commit()

    with patch("app.core.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 22, 6, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        with patch("app.services.proxmox_sync.sync_integration", new=AsyncMock()) as mock_sync:
            await scheduler_mod._run_proxmox_syncs()
            assert mock_sync.call_count == 1


@pytest.mark.asyncio
async def test_run_proxmox_syncs_runs_when_never_synced(mem_session):
    factory = mem_session
    async with factory() as db:
        db.add(ProxmoxIntegration(
            id="i1", name="x", host="h", port=8006, verify_tls=False,
            auth_type="token", token_user="root@pam", token_id="t", token_secret_enc="e",
            sync_interval_minutes=15, last_sync_at=None,
        ))
        await db.commit()

    with patch("app.services.proxmox_sync.sync_integration", new=AsyncMock()) as mock_sync:
        await scheduler_mod._run_proxmox_syncs()
        assert mock_sync.call_count == 1
