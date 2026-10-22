import { Button, ConfirmDialog, Dialog, Input, SearchField, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Switch, Badge, ScrollArea, Skeleton, EmptyState, ErrorState, Codicon, cn, haptic, host, useValue, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useState, useEffect, useCallback, useMemo } from 'react'

const ID = 'session-minder'
let _rest = null
const API = (path, opts = {}) => {
  const method = opts.method || 'GET'
  let body = null
  if (opts.body) {
    try { body = JSON.parse(opts.body) } catch { body = opts.body }
  }
  if (_rest) return _rest(path, { method, body })
  return fetch('/api/plugins/session-minder/' + path, opts).then(r => r.json())
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(1) + ' MB'
}
function fmtDate(ts) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}

function SessionRow({ s, selected, onSelect, pending }) {
  return jsx('button', {
    className: cn('w-full text-left px-2 py-1.5 rounded border border-transparent hover:bg-midground/5 flex flex-col gap-0.5',
      selected && 'bg-primary/10 border-primary/40'),
    onClick: () => onSelect(s.id),
    children: [
      jsxs('div', { className: 'flex items-center gap-1.5', children: [
        jsx('span', { className: 'truncate text-sm font-medium flex-1', children: s.title }),
        s.protected && jsx(Badge, { variant: 'secondary', children: 'protected' }),
        pending && jsx(Badge, { variant: 'outline', children: 'pending' }),
      ]}),
      jsxs('div', { className: 'flex items-center gap-2 text-[0.6875rem] text-text-tertiary', children: [
        jsx('span', { children: fmtBytes(s.size) }),
        jsx('span', { children: '·' }),
        jsx('span', { children: fmtDate(s.updated) }),
        s.message_count > 0 && jsxs(Fragment, { children: [jsx('span', { children: '·' }), jsx('span', { children: s.message_count + ' msgs' })] }),
      ]}),
    ]
  })
}

function Page() {
  const [sessions, setSessions] = useState([])
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState('updated')
  const [dir, setDir] = useState('desc')
  const [includeProtected, setIncludeProtected] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [showProtectedConfirm, setShowProtectedConfirm] = useState(false)
  const [pending, setPending] = useState([])
  const [disk, setDisk] = useState(null)
  const [backupInfo, setBackupInfo] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [confirmPurge, setConfirmPurge] = useState(false)
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [settings, setSettings] = useState({ undo_mode: '30s', undo_seconds: 30 })
  const [restoreOpen, setRestoreOpen] = useState(false)
  const [backups, setBackups] = useState([])
  const [riskOpen, setRiskOpen] = useState(false)
  const [riskAck, setRiskAck] = useState('')
  const [pendingBackupAction, setPendingBackupAction] = useState(null)

  const loadSettings = useCallback(async () => {
    try { setSettings(await API('settings')) } catch {}
  }, [])
  const loadDisk = useCallback(async () => {
    try { setDisk(await API('disk')) } catch {}
  }, [])
  const loadPending = useCallback(async () => {
    try { const r = await API('pending'); setPending(r.pending || []) } catch {}
  }, [])
  const loadBackups = useCallback(async () => {
    try { const r = await API('backups'); setBackups(r.backups || []) } catch {}
  }, [])

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const q = new URLSearchParams({ limit: '300', sort, direction: dir, search, include_protected: String(includeProtected), include_archived: String(includeArchived) })
      const r = await API('list?' + q.toString())
      setSessions(r.sessions || []); setTotal(r.total || 0)
    } catch (e) { setError(e.message || 'failed') }
    finally { setLoading(false) }
  }, [sort, dir, search, includeProtected, includeArchived])

  useEffect(() => { load(); loadSettings(); loadDisk(); loadPending(); loadBackups() }, [load])

  const pendingSet = useMemo(() => new Set(pending.map(p => p.session_id)), [pending])

  const toggleSelect = (id) => {
    setSelectedIds(prev => {
      const n = new Set(prev)
      if (n.has(id)) n.delete(id); else n.add(id)
      return n
    })
  }

  const ensureBackup = async () => {
    const r = await API('disk')
    setDisk(r)
    const needed = r.db_size * 2
    if (r.free < needed) {
      setRiskOpen(true)
      return new Promise((resolve) => { setPendingBackupAction(() => resolve) })
    }
    const b = await API('backup', { method: 'POST' })
    if (!b.ok) {
      if (b.error === 'insufficient_space') {
        setRiskOpen(true)
        return new Promise((resolve) => { setPendingBackupAction(() => resolve) })
      }
      throw new Error(b.error)
    }
    setBackupInfo(b)
    return b
  }

  const doDelete = async (ids) => {
    try {
      await ensureBackup()
    } catch (e) { alert('Backup failed: ' + e.message); return }
    // soft delete -> pending
    const r = await API('soft_delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, backup_first: false }) })
    await load(); await loadPending()
    haptic('tap')
  }

  const doHardDelete = async (ids) => {
    await API('purge', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) })
    await load(); await loadPending()
  }

  const doArchive = async (ids) => {
    try { await ensureBackup() } catch (e) { alert('Backup failed: ' + e.message); return }
    await API('archive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids, backup_first: false }) })
    await load()
  }

  const doExportJson = async (id) => {
    try {
      const data = await API('export_json/' + encodeURIComponent(id))
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = (data.id || id) + '.json'; a.click()
      URL.revokeObjectURL(url)
    } catch (e) { alert('Export JSON failed: ' + (e.message || e)) }
  }

  const doExportDb = async (id) => {
    const path = prompt('Destination path for .db file:')
    if (!path) return
    try { await ensureBackup() } catch (e) { alert('Backup failed: ' + e.message); return }
    const r = await API('export_db', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: id, dest_path: path }) })
    if (r.ok) alert('Exported to ' + r.path)
    else alert('Export failed')
  }

  const onSelect = async (id) => {
    setSelected(id)
    try {
      const r = await API('session/' + encodeURIComponent(id))
      setDetail(r)
    } catch (e) { setDetail(null) }
  }

  const protectedDialog = (s) => {
    // show via state
    setShowProtectedConfirm(s.id)
  }

  return jsxs('div', { className: 'flex h-full flex-col gap-2 p-3 text-sm', children: [
// header
jsxs('div', { className: 'flex flex-col gap-2', children: [
  jsx('h2', { className: 'text-base font-semibold', children: 'Session Minder' }),
  jsxs('div', { className: 'flex items-center gap-2 flex-wrap', children: [
    jsxs('div', { className: 'flex items-center gap-1', children: [
      jsx('span', { className: 'text-xs text-text-tertiary', children: 'Sort' }),
        jsx(Select, { value: sort, onValueChange: setSort, children: jsxs(Fragment, { children: [
          jsx(SelectTrigger, { className: 'w-36', children: jsx(SelectValue, {}) }),
          jsxs(SelectContent, { children: [
            jsx(SelectItem, { value: 'updated', children: 'Last updated' }),
            jsx(SelectItem, { value: 'created', children: 'Date created' }),
            jsx(SelectItem, { value: 'name', children: 'Name' }),
            jsx(SelectItem, { value: 'size', children: 'Size' }),
          ]}),
        ]}) }),
        jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setDir(d => d === 'desc' ? 'asc' : 'desc'), children: dir === 'desc' ? '↓' : '↑' }),
      ]}),
      jsxs('label', { className: 'flex items-center gap-1 text-xs', children: [
        jsx(Switch, { checked: includeProtected, onCheckedChange: (v) => {
          if (v && !includeProtected) {
            if (!confirm('Show protected (undeletable) sessions? These cannot be deleted; only archived.')) return
          }
          setIncludeProtected(v)
        }}),
        jsx('span', { children: 'Show protected' }),
      ]}),
      jsxs('label', { className: 'flex items-center gap-1 text-xs', children: [
        jsx(Switch, { checked: includeArchived, onCheckedChange: setIncludeArchived }),
        jsx('span', { children: 'Show archived' }),
      ]}),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: () => { loadBackups(); setRestoreOpen(true) }, children: 'Restore from backup' }),
      jsx(Input, { value: search, onChange: e => setSearch(e.target.value), placeholder: 'Search sessions…', className: 'ml-auto w-64' }),
    ]}),
  ]}),

    // bulk action bar
    selectedIds.size > 0 && jsxs('div', { className: 'flex items-center gap-2 p-2 rounded bg-midground/10', children: [
      jsx('span', { className: 'text-xs', children: selectedIds.size + ' selected' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setSelectedIds(new Set()), children: 'Clear' }),
      jsx(Button, { size: 'sm', onClick: () => doDelete([...selectedIds]), children: 'Delete' }),
      jsx(Button, { size: 'sm', onClick: () => doArchive([...selectedIds]), children: 'Archive' }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: async () => {
        for (const id of selectedIds) await doExportDb(id)
      }, children: 'Export .db' }),
      jsx(Button, { size: 'sm', variant: 'outline', onClick: async () => {
        for (const id of selectedIds) await doExportJson(id)
      }, children: 'Export JSON' }),
    ]}),

    // pending purge tray
    pending.length > 0 && jsxs('div', { className: 'p-2 rounded border border-warning/40 bg-warning/5', children: [
      jsxs('div', { className: 'flex items-center justify-between', children: [
        jsx('span', { className: 'text-xs font-medium', children: 'Pending purge: ' + pending.length }),
        jsx(Button, { size: 'sm', onClick: () => setConfirmPurge(true), children: 'Purge all now' }),
      ]}),
      jsx('div', { className: 'text-[0.6875rem] text-text-tertiary', children: 'Mode: ' + settings.undo_mode }),
    ]}),

    // main split
    jsxs('div', { className: 'flex flex-1 min-h-0 gap-2', children: [
      // left list
      jsx(ScrollArea, { className: 'w-1/2 min-w-0', children: loading ? jsx(Skeleton, { className: 'h-20' }) :
        sessions.length === 0 ? jsx(EmptyState, { title: 'No sessions' }) :
        jsx('div', { className: 'flex flex-col gap-0.5', children: sessions.map(s =>
          jsxs('div', { className: 'flex items-center gap-1', children: [
            jsx('input', { type: 'checkbox', checked: selectedIds.has(s.id), onChange: () => toggleSelect(s.id) }),
            jsx(SessionRow, { s, selected: selected === s.id, onSelect: onSelect, pending: pendingSet.has(s.id) }),
          ]}, s.id)
        )}) }),
      // right preview
      jsx(ScrollArea, { className: 'w-1/2 min-w-0 border border-border rounded p-2', children: !detail ? jsx('div', { className: 'text-text-tertiary text-xs', children: 'Select a session to preview' }) :
        jsxs('div', { className: 'flex flex-col gap-2', children: [
          jsxs('div', { className: 'flex flex-wrap gap-1 p-2 rounded border border-border bg-midground/5', children: [
            jsx(Button, { size: 'sm', onClick: () => doDelete([detail.session.id]), children: 'Delete' }),
            jsx(Button, { size: 'sm', onClick: () => doArchive([detail.session.id]), children: 'Archive' }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => doExportDb(detail.session.id), children: 'Export .db' }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => doExportJson(detail.session.id), children: 'Export JSON' }),
          ]}),
          jsxs('div', { className: 'flex flex-col gap-1', children: [
            jsx('span', { className: 'font-medium', children: detail.session?.title || detail.session?.id }),
            jsx('div', { className: 'text-xs', children: 'Messages: ' + (detail.total_messages || 0) }),
            jsx('div', { className: 'flex flex-col gap-1 mt-2', children: (detail.messages || []).map((m, i) =>
              jsxs('div', { className: 'text-xs border-b border-border/40 pb-1', children: [
                jsx('span', { className: 'font-medium text-text-secondary', children: m.role + ': ' }),
                jsx('span', { children: m.content }),
              ]}, i)
            )}),
          ]}),
        ]}) }),
    ]}),

    // risk dialog
    riskOpen && jsxs(Dialog, { open: true, onOpenChange: setRiskOpen, children: [
      jsx('div', { className: 'p-4 flex flex-col gap-2', children: [
        jsx('h3', { className: 'font-semibold text-destructive', children: 'Insufficient disk space for safety backup' }),
        jsx('p', { className: 'text-sm', children: 'Proceeding risks total loss of state.db. Type I ACCEPT THE RISK to continue.' }),
        jsx(Input, { value: riskAck, onChange: e => setRiskAck(e.target.value) }),
        jsxs('div', { className: 'flex gap-2 justify-end', children: [
          jsx(Button, { variant: 'ghost', onClick: () => { setRiskOpen(false); setRiskAck(''); if (pendingBackupAction) pendingBackupAction(null) }, children: 'Cancel' }),
          jsx(Button, { variant: 'destructive', disabled: riskAck !== 'I ACCEPT THE RISK', onClick: () => {
            setRiskOpen(false)
            const cb = pendingBackupAction
            setPendingBackupAction(null)
            setRiskAck('')
            if (cb) cb({ ok: true, accepted_risk: true })
          }, children: 'Proceed at risk' }),
        ]}),
      ]}),
    ]}),

    // restore dialog
    restoreOpen && jsxs(Dialog, { open: true, onOpenChange: setRestoreOpen, children: [
      jsx('div', { className: 'p-4 flex flex-col gap-2', children: [
        jsx('h3', { className: 'font-semibold', children: 'Restore from backup' }),
        jsx('p', { className: 'text-xs text-text-tertiary', children: 'Gateway must be stopped. Restore replaces state.db.' }),
        jsx('div', { className: 'max-h-40 overflow-auto', children: backups.map(b =>
          jsx('button', { key: b.path, className: 'block w-full text-left text-xs px-2 py-1 hover:bg-midground/5', onClick: async () => {
            if (!confirm('Restore from ' + b.name + '?')) return
            const r = await API('restore', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backup_path: b.path, force: true }) })
            if (r.ok) { alert('Restored. Previous saved as ' + r.previous); setRestoreOpen(false) }
            else alert('Restore failed: ' + (r.error || 'unknown') + (r.holders ? '\nHolders: ' + r.holders.map(h=>h.pid).join(',') : ''))
          }, children: b.name + ' (' + fmtBytes(b.size) + ')' }, b.path)
        )}),
        jsx(Button, { variant: 'ghost', onClick: () => setRestoreOpen(false), children: 'Close' }),
      ]}),
    ]}),

    // confirm purge
    confirmPurge && jsx(ConfirmDialog, { open: true, onOpenChange: setConfirmPurge, title: 'Purge all pending?', description: 'Permanently delete ' + pending.length + ' sessions.', confirmLabel: 'Purge all', onConfirm: async () => {
      await doHardDelete(pending.map(p => p.session_id))
      setConfirmPurge(false)
    }}),
  ]})
}

export default {
  id: ID,
  name: 'Session Minder',
  register(ctx) {
    _rest = (p, o) => ctx.rest(p, o)
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/session-minder' },
      render: () => jsx(Page, {})
    })
    try {
      ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/session-minder', label: 'Session Minder', codicon: 'library' } })
    } catch (e) {}
    try {
      ctx.register({ id: 'cmd', area: PALETTE_AREA, data: { label: 'Open Session Minder' }, render: () => null })
    } catch (e) {}
  }
}
