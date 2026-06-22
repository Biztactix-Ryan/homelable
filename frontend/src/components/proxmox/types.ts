export type ProxmoxAuthType = 'token' | 'password'

export interface ProxmoxVM {
  vmid: number
  node: string
  type: 'vm' | 'lxc'
  name: string
  status: string | null
  cpu_count: number | null
  ram_gb: number | null
  disk_gb: number | null
  tags: string
  uptime_seconds: number | null
}

export interface ProxmoxIntegration {
  id: string
  name: string
  host: string
  port: number
  verify_tls: boolean
  auth_type: ProxmoxAuthType
  token_user: string | null
  token_id: string | null
  username: string | null
  sync_interval_minutes: number
  last_sync_at: string | null
  last_sync_status: 'ok' | 'error' | null
  last_sync_error: string | null
  created_at: string
}

export interface ProxmoxSyncResult {
  integration_id: string
  vms_seen: number
  nodes_updated: number
  pending_created: number
  pending_skipped_existing: number
  status: 'ok' | 'error'
  error: string | null
}
