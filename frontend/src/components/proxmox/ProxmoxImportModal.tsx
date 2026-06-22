import { useEffect, useState } from 'react'
import { Server, Box, CheckCircle2, XCircle, Loader2, Trash2, RefreshCw } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { proxmoxApi } from '@/api/client'
import { toast } from 'sonner'
import type { ProxmoxAuthType, ProxmoxVM, ProxmoxIntegration } from './types'

interface ProxmoxImportModalProps {
  open: boolean
  onClose: () => void
  onImported?: () => void
  /** Design the imported nodes belong to — without it the canvas can't see them. */
  designId?: string | null
}

interface ConnectionForm {
  integration_name: string
  host: string
  port: string
  verify_tls: boolean
  auth_type: ProxmoxAuthType
  token_user: string
  token_id: string
  token_secret: string
  username: string
  password: string
  save_credentials: boolean
  sync_interval_minutes: string
}

const DEFAULT_FORM: ConnectionForm = {
  integration_name: '',
  host: '',
  port: '8006',
  verify_tls: false,
  auth_type: 'token',
  token_user: '',
  token_id: '',
  token_secret: '',
  username: '',
  password: '',
  save_credentials: true,
  sync_interval_minutes: '15',
}

const VM_TYPE_ICON = { vm: Server, lxc: Box } as const

export function ProxmoxImportModal({ open, onClose, onImported, designId }: ProxmoxImportModalProps) {
  const [form, setForm] = useState<ConnectionForm>(DEFAULT_FORM)
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [connectionMsg, setConnectionMsg] = useState('')
  const [loading, setLoading] = useState(false)
  const [vms, setVms] = useState<ProxmoxVM[]>([])
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [integrations, setIntegrations] = useState<ProxmoxIntegration[]>([])

  const updateField = (field: keyof ConnectionForm, value: string | boolean) =>
    setForm((f) => ({ ...f, [field]: value }))

  // Refresh saved integrations when the modal opens so the manage section is current.
  useEffect(() => {
    if (!open) return
    proxmoxApi.listIntegrations().then((res) => setIntegrations(res.data)).catch(() => {})
  }, [open])

  const buildCredentials = () => ({
    host: form.host.trim(),
    port: Number(form.port) || 8006,
    verify_tls: form.verify_tls,
    auth_type: form.auth_type,
    ...(form.auth_type === 'token'
      ? {
          token_user: form.token_user.trim(),
          token_id: form.token_id.trim(),
          token_secret: form.token_secret,
        }
      : { username: form.username.trim(), password: form.password }),
  })

  const validateCreds = (): string | null => {
    if (!form.host.trim()) return 'Enter a Proxmox host'
    if (form.auth_type === 'token') {
      if (!form.token_user.trim()) return 'Token user (user@realm) required'
      if (!form.token_id.trim()) return 'Token ID required'
      if (!form.token_secret) return 'Token secret required'
    } else {
      if (!form.username.trim()) return 'Username (user@realm) required'
      if (!form.password) return 'Password required'
    }
    return null
  }

  const extractError = (err: unknown): string | undefined => {
    if (err && typeof err === 'object' && 'response' in err) {
      return (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
    }
    return undefined
  }

  const handleTest = async () => {
    const err = validateCreds()
    if (err) { toast.error(err); return }
    setConnectionStatus('testing')
    try {
      const res = await proxmoxApi.testConnection(buildCredentials())
      if (res.data.connected) {
        setConnectionStatus('ok')
        setConnectionMsg(res.data.message)
      } else {
        setConnectionStatus('fail')
        setConnectionMsg(res.data.message)
      }
    } catch {
      setConnectionStatus('fail')
      setConnectionMsg('Request failed — check host/port')
    }
  }

  const handleFetch = async () => {
    const err = validateCreds()
    if (err) { toast.error(err); return }
    setLoading(true)
    try {
      const res = await proxmoxApi.list(buildCredentials())
      setVms(res.data.vms)
      setChecked(new Set(res.data.vms.map((v) => v.vmid)))
      if (res.data.vms.length === 0) {
        toast.info('No VMs or containers found on this Proxmox host')
      } else {
        toast.success(`Found ${res.data.vms.length} VM${res.data.vms.length !== 1 ? 's' : ''}`)
      }
    } catch (e: unknown) {
      toast.error(extractError(e) ?? 'Failed to fetch VMs')
    } finally {
      setLoading(false)
    }
  }

  const toggleCheck = (vmid: number) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(vmid)) next.delete(vmid); else next.add(vmid)
      return next
    })

  const toggleAll = () =>
    setChecked(checked.size === vms.length ? new Set() : new Set(vms.map((v) => v.vmid)))

  const handleImport = async () => {
    const err = validateCreds()
    if (err) { toast.error(err); return }
    if (!form.integration_name.trim()) { toast.error('Give this Proxmox host a name'); return }
    if (checked.size === 0) { toast.error('Select at least one VM to import'); return }
    setLoading(true)
    try {
      const res = await proxmoxApi.import({
        ...buildCredentials(),
        selected_vmids: Array.from(checked),
        integration_name: form.integration_name.trim(),
        design_id: designId ?? null,
        save_credentials: form.save_credentials,
        sync_interval_minutes: Math.max(1, Number(form.sync_interval_minutes) || 15),
      })
      toast.success(
        `Added ${res.data.created_node_ids.length} VM${res.data.created_node_ids.length !== 1 ? 's' : ''} to canvas` +
          (res.data.skipped_existing.length ? ` (${res.data.skipped_existing.length} already imported)` : ''),
      )
      onImported?.()
      handleClose()
    } catch (e: unknown) {
      toast.error(extractError(e) ?? 'Import failed')
    } finally {
      setLoading(false)
    }
  }

  const handleClose = () => {
    setForm(DEFAULT_FORM)
    setVms([])
    setChecked(new Set())
    setConnectionStatus('idle')
    setConnectionMsg('')
    onClose()
  }

  const handleSyncIntegration = async (id: string) => {
    try {
      const res = await proxmoxApi.syncIntegration(id)
      if (res.data.status === 'ok') {
        toast.success(
          `Synced — ${res.data.nodes_updated} updated, ${res.data.pending_created} new pending`,
        )
      } else {
        toast.error(res.data.error ?? 'Sync failed')
      }
      const refreshed = await proxmoxApi.listIntegrations()
      setIntegrations(refreshed.data)
    } catch (e: unknown) {
      toast.error(extractError(e) ?? 'Sync failed')
    }
  }

  const handleDeleteIntegration = async (id: string) => {
    if (!confirm('Delete this saved Proxmox integration? Canvas nodes will be kept.')) return
    try {
      await proxmoxApi.deleteIntegration(id)
      setIntegrations((rows) => rows.filter((r) => r.id !== id))
      toast.success('Integration removed')
    } catch (e: unknown) {
      toast.error(extractError(e) ?? 'Delete failed')
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
      <DialogContent className="bg-[#161b22] border-border max-w-xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-foreground flex items-center gap-2">
            <Server size={16} className="text-[#00d4ff]" />
            Proxmox Import
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 py-2 min-h-0">
          {/* Saved integrations */}
          {integrations.length > 0 && (
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Saved integrations</Label>
              <div className="space-y-1">
                {integrations.map((it) => (
                  <div
                    key={it.id}
                    className="flex items-center justify-between gap-2 px-2 py-1.5 rounded-md border border-border bg-[#0d1117] text-xs"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-foreground font-medium truncate">{it.name}</div>
                      <div className="text-muted-foreground font-mono text-[10px] truncate">
                        {it.host}:{it.port} • every {it.sync_interval_minutes}m
                        {it.last_sync_status === 'error' ? ' • last sync failed' : ''}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-xs"
                      onClick={() => handleSyncIntegration(it.id)}
                      title="Sync now"
                    >
                      <RefreshCw size={12} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-6 px-2 text-xs text-[#f85149] hover:text-[#f85149]"
                      onClick={() => handleDeleteIntegration(it.id)}
                      title="Delete integration"
                    >
                      <Trash2 size={12} />
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Connection form */}
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2 space-y-1">
                <Label className="text-xs text-muted-foreground">Friendly Name</Label>
                <Input
                  value={form.integration_name}
                  onChange={(e) => updateField('integration_name', e.target.value)}
                  placeholder="Home Cluster"
                  className="text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Host</Label>
                <Input
                  value={form.host}
                  onChange={(e) => updateField('host', e.target.value)}
                  placeholder="192.168.1.x or pve.local"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Port</Label>
                <Input
                  value={form.port}
                  onChange={(e) => updateField('port', e.target.value)}
                  type="number"
                  className="font-mono text-sm bg-[#0d1117] border-border"
                />
              </div>
              <div className="col-span-2 flex items-center gap-4 pt-1">
                <span className="text-xs text-muted-foreground">Auth:</span>
                <label className="flex items-center gap-1.5 text-xs cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="pve-auth"
                    checked={form.auth_type === 'token'}
                    onChange={() => updateField('auth_type', 'token')}
                    className="accent-[#00d4ff] cursor-pointer"
                  />
                  API Token (recommended)
                </label>
                <label className="flex items-center gap-1.5 text-xs cursor-pointer text-foreground">
                  <input
                    type="radio"
                    name="pve-auth"
                    checked={form.auth_type === 'password'}
                    onChange={() => updateField('auth_type', 'password')}
                    className="accent-[#00d4ff] cursor-pointer"
                  />
                  Username + Password
                </label>
              </div>

              {form.auth_type === 'token' ? (
                <>
                  <div className="col-span-2 space-y-1">
                    <Label className="text-xs text-muted-foreground">Token User</Label>
                    <Input
                      value={form.token_user}
                      onChange={(e) => updateField('token_user', e.target.value)}
                      placeholder="root@pam"
                      className="font-mono text-sm bg-[#0d1117] border-border"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Token ID</Label>
                    <Input
                      value={form.token_id}
                      onChange={(e) => updateField('token_id', e.target.value)}
                      placeholder="homelable"
                      className="font-mono text-sm bg-[#0d1117] border-border"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Token Secret</Label>
                    <Input
                      value={form.token_secret}
                      onChange={(e) => updateField('token_secret', e.target.value)}
                      type="password"
                      autoComplete="new-password"
                      placeholder="••••••••"
                      className="font-mono text-sm bg-[#0d1117] border-border"
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Username</Label>
                    <Input
                      value={form.username}
                      onChange={(e) => updateField('username', e.target.value)}
                      placeholder="root@pam"
                      className="font-mono text-sm bg-[#0d1117] border-border"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-muted-foreground">Password</Label>
                    <Input
                      value={form.password}
                      onChange={(e) => updateField('password', e.target.value)}
                      type="password"
                      autoComplete="new-password"
                      placeholder="••••••••"
                      className="text-sm bg-[#0d1117] border-border"
                    />
                  </div>
                </>
              )}

              <div className="col-span-2 flex items-center gap-4 pt-1">
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.verify_tls}
                    onChange={(e) => updateField('verify_tls', e.target.checked)}
                    className="w-3 h-3 accent-[#00d4ff] cursor-pointer"
                  />
                  Verify TLS cert
                </label>
                <label className="flex items-center gap-1.5 text-xs text-foreground cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.save_credentials}
                    onChange={(e) => updateField('save_credentials', e.target.checked)}
                    className="w-3 h-3 accent-[#00d4ff] cursor-pointer"
                  />
                  Save credentials for periodic scanning
                </label>
              </div>

              {form.save_credentials && (
                <div className="col-span-2 space-y-1">
                  <Label className="text-xs text-muted-foreground">Sync every (minutes)</Label>
                  <Input
                    value={form.sync_interval_minutes}
                    onChange={(e) => updateField('sync_interval_minutes', e.target.value)}
                    type="number"
                    min={1}
                    max={1440}
                    className="font-mono text-sm bg-[#0d1117] border-border w-32"
                  />
                </div>
              )}
            </div>

            {/* Connection status */}
            {connectionStatus !== 'idle' && (
              <div
                className={`flex items-center gap-1.5 text-xs px-2 py-1.5 rounded-md border ${
                  connectionStatus === 'ok'
                    ? 'bg-[#39d353]/10 border-[#39d353]/30 text-[#39d353]'
                    : connectionStatus === 'fail'
                      ? 'bg-[#f85149]/10 border-[#f85149]/30 text-[#f85149]'
                      : 'bg-[#e3b341]/10 border-[#e3b341]/30 text-[#e3b341]'
                }`}
              >
                {connectionStatus === 'testing' && <Loader2 size={12} className="animate-spin" />}
                {connectionStatus === 'ok' && <CheckCircle2 size={12} />}
                {connectionStatus === 'fail' && <XCircle size={12} />}
                <span>{connectionStatus === 'testing' ? 'Testing…' : connectionMsg}</span>
              </div>
            )}

            <div className="flex gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={handleTest}
                disabled={connectionStatus === 'testing'}
              >
                Test Connection
              </Button>
              <Button size="sm" onClick={handleFetch} disabled={loading}>
                {loading ? <Loader2 size={12} className="animate-spin mr-1" /> : null}
                Fetch VMs
              </Button>
            </div>
          </div>

          {/* VM list */}
          {vms.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-xs text-muted-foreground">
                  {vms.length} VM{vms.length !== 1 ? 's' : ''} found
                </Label>
                <button
                  onClick={toggleAll}
                  className="text-xs text-[#00d4ff] hover:underline cursor-pointer"
                >
                  {checked.size === vms.length ? 'Deselect all' : 'Select all'}
                </button>
              </div>
              <div className="space-y-1">
                {vms.map((vm) => {
                  const Icon = VM_TYPE_ICON[vm.type]
                  const isOn = vm.status === 'running'
                  return (
                    <label
                      key={vm.vmid}
                      className="flex items-center gap-2 px-2 py-1.5 rounded-md border border-border bg-[#0d1117] cursor-pointer hover:bg-[#161b22]"
                    >
                      <input
                        type="checkbox"
                        checked={checked.has(vm.vmid)}
                        onChange={() => toggleCheck(vm.vmid)}
                        className="w-3 h-3 accent-[#00d4ff] cursor-pointer"
                      />
                      <Icon size={14} className={isOn ? 'text-[#39d353]' : 'text-muted-foreground'} />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-foreground truncate">
                          {vm.name}{' '}
                          <span className="text-[10px] text-muted-foreground font-mono">
                            #{vm.vmid} • {vm.node} • {vm.type.toUpperCase()}
                          </span>
                        </div>
                      </div>
                      <span
                        className={`text-[10px] uppercase ${
                          isOn ? 'text-[#39d353]' : 'text-muted-foreground'
                        }`}
                      >
                        {vm.status ?? 'unknown'}
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="border-t border-border pt-3">
          <Button variant="ghost" size="sm" onClick={handleClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={handleImport}
            disabled={loading || vms.length === 0 || checked.size === 0}
          >
            Add {checked.size > 0 ? checked.size : ''} to Canvas
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
