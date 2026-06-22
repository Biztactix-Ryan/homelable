"""Pydantic v2 schemas for Proxmox VE integration."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProxmoxCredentials(BaseModel):
    """Connection details + credentials for a Proxmox API call.

    Reused by every endpoint that talks to Proxmox so the auth shape is
    consistent. Exactly one of (token_*) or (username, password) must be set.
    """

    host: str = Field(..., description="Proxmox hostname or IP")
    port: int = Field(8006, ge=1, le=65535)
    verify_tls: bool = Field(False, description="Verify TLS cert (off by default — most homelab PVE uses self-signed)")
    auth_type: str = Field(..., pattern="^(token|password)$")
    # Token mode
    token_user: str | None = Field(None, description="user@realm — e.g. root@pam")
    token_id: str | None = Field(None, description="API token id")
    token_secret: str | None = Field(None, description="API token secret (UUID)")
    # Password mode
    username: str | None = Field(None, description="user@realm — e.g. root@pam")
    password: str | None = Field(None)

    @model_validator(mode="after")
    def _check_creds(self) -> "ProxmoxCredentials":
        if self.auth_type == "token":
            if not (self.token_user and self.token_id and self.token_secret):
                raise ValueError("auth_type=token requires token_user, token_id, token_secret")
        else:
            if not (self.username and self.password):
                raise ValueError("auth_type=password requires username and password")
        return self


class ProxmoxTestRequest(ProxmoxCredentials):
    pass


class ProxmoxTestResponse(BaseModel):
    connected: bool
    message: str


class ProxmoxListRequest(ProxmoxCredentials):
    pass


class ProxmoxVMOut(BaseModel):
    """A Proxmox VM or LXC, normalized for the frontend."""

    vmid: int
    node: str
    type: str  # "vm" | "lxc"
    name: str
    status: str | None  # "running" | "stopped" | "paused" | None
    cpu_count: int | None = None
    ram_gb: float | None = None
    disk_gb: float | None = None
    tags: str = ""
    uptime_seconds: int | None = None


class ProxmoxListResponse(BaseModel):
    vms: list[ProxmoxVMOut]


class ProxmoxImportRequest(ProxmoxCredentials):
    """One-off import: add selected VMs as child nodes of a Proxmox host node.

    If `save_credentials` is true the credentials are persisted as a
    ProxmoxIntegration row so the scheduler can keep this host in sync.
    """

    selected_vmids: list[int] = Field(..., description="vmid values of VMs to add to the canvas")
    integration_name: str = Field(..., description="Friendly name for this Proxmox host")
    save_credentials: bool = False
    sync_interval_minutes: int = Field(15, ge=1, le=1440)


class ProxmoxImportResponse(BaseModel):
    host_node_id: str
    created_node_ids: list[str]
    skipped_existing: list[int] = Field(
        default_factory=list,
        description="vmids that were already on the canvas under this integration",
    )
    integration_id: str | None = None


class ProxmoxIntegrationOut(BaseModel):
    """Saved-integration row, with secrets redacted."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    host: str
    port: int
    verify_tls: bool
    auth_type: str
    token_user: str | None
    token_id: str | None
    username: str | None
    sync_interval_minutes: int
    last_sync_at: datetime | None
    last_sync_status: str | None
    last_sync_error: str | None
    created_at: datetime


class ProxmoxSyncResponse(BaseModel):
    """Result of a manual or scheduled sync run for one saved integration."""

    integration_id: str
    vms_seen: int
    nodes_updated: int
    pending_created: int
    pending_skipped_existing: int
    status: str  # "ok" | "error"
    error: str | None = None
