import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ProxmoxImportModal } from '../ProxmoxImportModal'

vi.mock('@/api/client', () => ({
  proxmoxApi: {
    testConnection: vi.fn(),
    list: vi.fn(),
    import: vi.fn(),
    listIntegrations: vi.fn(),
    deleteIntegration: vi.fn(),
    syncIntegration: vi.fn(),
  },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }))

import { proxmoxApi } from '@/api/client'
import { toast } from 'sonner'

const defaultProps = {
  open: true,
  onClose: vi.fn(),
  onImported: vi.fn(),
}

const sampleVMs = [
  { vmid: 100, node: 'pve', type: 'vm' as const, name: 'web', status: 'running',
    cpu_count: 2, ram_gb: 4, disk_gb: 32, tags: '', uptime_seconds: 1000 },
  { vmid: 200, node: 'pve', type: 'lxc' as const, name: 'db', status: 'stopped',
    cpu_count: 1, ram_gb: 1, disk_gb: 8, tags: '', uptime_seconds: null },
]

const fillTokenForm = () => {
  fireEvent.change(screen.getByPlaceholderText('Home Cluster'), { target: { value: 'Test PVE' } })
  fireEvent.change(screen.getByPlaceholderText('192.168.1.x or pve.local'), { target: { value: 'pve.local' } })
  fireEvent.change(screen.getByPlaceholderText('root@pam'), { target: { value: 'root@pam' } })
  fireEvent.change(screen.getByPlaceholderText('homelable'), { target: { value: 't1' } })
  // The token secret input uses the same '••••••••' placeholder as the password input,
  // so target it via getAllByPlaceholderText and pick the only visible one for token auth.
  const secretInput = screen.getAllByPlaceholderText('••••••••')[0]
  fireEvent.change(secretInput, { target: { value: 'uuid-1234' } })
}

describe('ProxmoxImportModal', () => {
  beforeEach(() => {
    vi.mocked(proxmoxApi.testConnection).mockReset()
    vi.mocked(proxmoxApi.list).mockReset()
    vi.mocked(proxmoxApi.import).mockReset()
    vi.mocked(proxmoxApi.listIntegrations).mockReset().mockResolvedValue({ data: [] } as never)
    vi.mocked(proxmoxApi.deleteIntegration).mockReset()
    vi.mocked(proxmoxApi.syncIntegration).mockReset()
    vi.mocked(toast.success).mockReset()
    vi.mocked(toast.error).mockReset()
    vi.mocked(toast.info).mockReset()
    defaultProps.onClose.mockReset()
    defaultProps.onImported.mockReset()
  })

  it('renders header and auth toggle', () => {
    render(<ProxmoxImportModal {...defaultProps} />)
    expect(screen.getByText('Proxmox Import')).toBeInTheDocument()
    expect(screen.getByText(/API Token/)).toBeInTheDocument()
    expect(screen.getByText(/Username \+ Password/)).toBeInTheDocument()
  })

  it('successful test-connection shows green status', async () => {
    vi.mocked(proxmoxApi.testConnection).mockResolvedValue({ data: { connected: true, message: 'Connected.' } } as never)
    render(<ProxmoxImportModal {...defaultProps} />)
    fillTokenForm()
    fireEvent.click(screen.getByText('Test Connection'))
    await waitFor(() => expect(screen.getByText('Connected.')).toBeInTheDocument())
  })

  it('failed test-connection shows the server message', async () => {
    vi.mocked(proxmoxApi.testConnection).mockResolvedValue(
      { data: { connected: false, message: 'Proxmox rejected credentials (HTTP 401).' } } as never,
    )
    render(<ProxmoxImportModal {...defaultProps} />)
    fillTokenForm()
    fireEvent.click(screen.getByText('Test Connection'))
    await waitFor(() => expect(screen.getByText(/rejected credentials/)).toBeInTheDocument())
  })

  it('validation rejects an empty host', async () => {
    render(<ProxmoxImportModal {...defaultProps} />)
    fireEvent.click(screen.getByText('Test Connection'))
    await waitFor(() => expect(vi.mocked(toast.error)).toHaveBeenCalledWith('Enter a Proxmox host'))
    expect(vi.mocked(proxmoxApi.testConnection)).not.toHaveBeenCalled()
  })

  it('Fetch VMs populates the list', async () => {
    vi.mocked(proxmoxApi.list).mockResolvedValue({ data: { vms: sampleVMs } } as never)
    render(<ProxmoxImportModal {...defaultProps} />)
    fillTokenForm()
    fireEvent.click(screen.getByText('Fetch VMs'))
    await waitFor(() => expect(screen.getByText('web')).toBeInTheDocument())
    expect(screen.getByText('db')).toBeInTheDocument()
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith('Found 2 VMs')
  })

  it('Import sends selected vmids and calls onImported on success', async () => {
    vi.mocked(proxmoxApi.list).mockResolvedValue({ data: { vms: sampleVMs } } as never)
    vi.mocked(proxmoxApi.import).mockResolvedValue(
      { data: { host_node_id: 'h1', created_node_ids: ['n1', 'n2'], skipped_existing: [], integration_id: 'i1' } } as never,
    )
    render(<ProxmoxImportModal {...defaultProps} />)
    fillTokenForm()
    fireEvent.click(screen.getByText('Fetch VMs'))
    await waitFor(() => expect(screen.getByText('web')).toBeInTheDocument())

    fireEvent.click(screen.getByText(/^Add 2 to Canvas$/))
    await waitFor(() => expect(vi.mocked(proxmoxApi.import)).toHaveBeenCalled())
    const payload = vi.mocked(proxmoxApi.import).mock.calls[0][0]
    expect(payload.selected_vmids.sort()).toEqual([100, 200])
    expect(payload.integration_name).toBe('Test PVE')
    expect(payload.auth_type).toBe('token')
    expect(defaultProps.onImported).toHaveBeenCalled()
  })

  it('shows saved integrations and supports sync', async () => {
    vi.mocked(proxmoxApi.listIntegrations).mockResolvedValue({
      data: [
        {
          id: 'i1', name: 'Saved Cluster', host: 'pve.local', port: 8006, verify_tls: false,
          auth_type: 'token', token_user: 'root@pam', token_id: 't1', username: null,
          sync_interval_minutes: 15, last_sync_at: null, last_sync_status: null,
          last_sync_error: null, created_at: '2026-06-22T00:00:00Z',
        },
      ],
    } as never)
    vi.mocked(proxmoxApi.syncIntegration).mockResolvedValue({
      data: { integration_id: 'i1', vms_seen: 3, nodes_updated: 1, pending_created: 2,
        pending_skipped_existing: 0, status: 'ok', error: null },
    } as never)

    render(<ProxmoxImportModal {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Saved Cluster')).toBeInTheDocument())
    expect(screen.getByText(/every 15m/)).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Sync now'))
    await waitFor(() => expect(vi.mocked(proxmoxApi.syncIntegration)).toHaveBeenCalledWith('i1'))
  })
})
