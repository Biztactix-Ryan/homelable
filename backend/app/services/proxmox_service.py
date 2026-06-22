"""Proxmox VE REST API client.

Supports both auth modes:
- API token (`PVEAPIToken=user@realm!tokenid=secret` header) — preferred,
  works fine with the read-only PVEAuditor role.
- Username + password (ticket flow) — POST /access/ticket, then use the
  ticket as a PVEAuthCookie. Read-only callers don't need the CSRF token.

All functions are async (uses httpx) and stateless; credentials are passed
in per call so the same module serves ephemeral one-off imports and the
background scheduler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass(frozen=True)
class ProxmoxAuth:
    """Credentials passed to client calls. Exactly one auth mode is set."""

    # token mode
    token_user: str | None = None
    token_id: str | None = None
    token_secret: str | None = None
    # password mode
    username: str | None = None
    password: str | None = None

    @property
    def mode(self) -> str:
        if self.token_secret:
            return "token"
        if self.password:
            return "password"
        raise ValueError("ProxmoxAuth has no credentials")


def _base_url(host: str, port: int) -> str:
    # Proxmox API is always HTTPS; users provide hostname/IP without scheme.
    return f"https://{host}:{port}/api2/json"


def _token_header(auth: ProxmoxAuth) -> dict[str, str]:
    return {"Authorization": f"PVEAPIToken={auth.token_user}!{auth.token_id}={auth.token_secret}"}


async def _ticket_cookies(client: httpx.AsyncClient, host: str, port: int, auth: ProxmoxAuth) -> dict[str, str]:
    """Exchange username+password for a PVEAuthCookie via /access/ticket."""
    resp = await client.post(
        f"{_base_url(host, port)}/access/ticket",
        data={"username": auth.username, "password": auth.password},
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    ticket = data.get("ticket")
    if not ticket:
        raise ConnectionError("Proxmox /access/ticket returned no ticket")
    return {"PVEAuthCookie": ticket}


async def _client(verify_tls: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=verify_tls, timeout=_TIMEOUT)


async def _request(
    method: str,
    host: str,
    port: int,
    path: str,
    auth: ProxmoxAuth,
    verify_tls: bool,
) -> Any:
    """Make an authenticated request and return the unwrapped `data` field."""
    async with await _client(verify_tls) as client:
        if auth.mode == "token":
            headers = _token_header(auth)
            cookies: dict[str, str] = {}
        else:
            cookies = await _ticket_cookies(client, host, port, auth)
            headers = {}
        resp = await client.request(
            method, f"{_base_url(host, port)}{path}", headers=headers, cookies=cookies
        )
        resp.raise_for_status()
        return resp.json().get("data")


def _sanitize_error(exc: BaseException) -> str:
    """Strip credentials out of httpx error messages before they reach API responses."""
    msg = str(exc)
    # Token headers and ticket cookies can leak in error text; redact aggressively.
    if "PVEAPIToken" in msg or "PVEAuthCookie" in msg:
        return "Proxmox authentication failed (credentials redacted)."
    return msg


async def check_connection(host: str, port: int, auth: ProxmoxAuth, verify_tls: bool) -> None:
    """Raise ConnectionError if the host/credentials don't work; return None on success."""
    try:
        await _request("GET", host, port, "/version", auth, verify_tls)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise ConnectionError(f"Proxmox rejected credentials (HTTP {code}).") from exc
        raise ConnectionError(f"Proxmox returned HTTP {code}.") from exc
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise ConnectionError(f"Cannot reach Proxmox at {host}:{port} — {_sanitize_error(exc)}") from exc
    except Exception as exc:
        logger.warning("Proxmox test_connection failed: %s", exc)
        raise ConnectionError(_sanitize_error(exc)) from exc


async def list_vms(host: str, port: int, auth: ProxmoxAuth, verify_tls: bool) -> list[dict[str, Any]]:
    """Return every VM and LXC across the cluster.

    Uses /cluster/resources?type=vm which returns both qemu and lxc in a single
    call with their current status, owning node, vmid, name, etc.
    """
    try:
        raw = await _request("GET", host, port, "/cluster/resources?type=vm", auth, verify_tls)
    except httpx.HTTPStatusError as exc:
        raise ConnectionError(f"Proxmox returned HTTP {exc.response.status_code}.") from exc
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise ConnectionError(f"Cannot reach Proxmox at {host}:{port} — {_sanitize_error(exc)}") from exc
    items = raw or []
    return [_normalize_vm(item) for item in items if isinstance(item, dict)]


def _normalize_vm(item: dict[str, Any]) -> dict[str, Any]:
    """Map a raw /cluster/resources entry to the shape our DB and frontend expect."""
    vmid = item.get("vmid")
    node = item.get("node")
    raw_type = item.get("type")  # "qemu" or "lxc"
    return {
        "vmid": int(vmid) if vmid is not None else None,
        "node": node,
        # Proxmox calls VMs "qemu"; expose the friendlier "vm" to the rest of the app.
        "type": "vm" if raw_type == "qemu" else "lxc" if raw_type == "lxc" else raw_type,
        "name": item.get("name") or (f"vm-{vmid}" if vmid is not None else "unknown"),
        "status": item.get("status"),  # "running" | "stopped" | "paused" | "unknown"
        "cpu_count": int(item["maxcpu"]) if isinstance(item.get("maxcpu"), int | float) else None,
        "ram_gb": round(item["maxmem"] / (1024 ** 3), 2) if isinstance(item.get("maxmem"), int | float) else None,
        "disk_gb": round(item["maxdisk"] / (1024 ** 3), 2) if isinstance(item.get("maxdisk"), int | float) else None,
        "tags": item.get("tags") or "",
        "uptime_seconds": int(item["uptime"]) if isinstance(item.get("uptime"), int | float) else None,
    }


async def check_vm_status(
    host: str,
    port: int,
    auth: ProxmoxAuth,
    verify_tls: bool,
    node: str,
    vm_type: str,
    vmid: int,
) -> str:
    """Return 'online' if running, 'offline' if stopped/paused, else 'unknown'."""
    api_type = "qemu" if vm_type == "vm" else vm_type  # accept our normalized name or raw
    try:
        data = await _request(
            "GET",
            host,
            port,
            f"/nodes/{node}/{api_type}/{vmid}/status/current",
            auth,
            verify_tls,
        )
    except Exception as exc:
        logger.debug("Proxmox status check failed for %s/%s/%s: %s", node, api_type, vmid, _sanitize_error(exc))
        return "unknown"
    status = (data or {}).get("status")
    if status == "running":
        return "online"
    if status in ("stopped", "paused"):
        return "offline"
    return "unknown"


def status_to_node_status(proxmox_status: str | None) -> str:
    """Map a Proxmox VM lifecycle status to a Homelable node status."""
    if proxmox_status == "running":
        return "online"
    if proxmox_status in ("stopped", "paused"):
        return "offline"
    return "unknown"
