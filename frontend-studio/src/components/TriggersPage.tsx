/**
 * TriggersPage - 定时任务独立管理页面。
 *
 * 展示当前用户的 trigger 列表 + 对应 workflow，支持创建/编辑/启停/删除。
 * 每个 trigger 可查看其触发的任务记录（TriggerTaskRecordsModal），并跳转到
 * 任务看板明细（onViewTask -> App 切 board tab + 打开 TaskDetailDrawer）。
 *
 * 布局仿 ModelsPage：标题 + 新建 / stats 卡 / 搜索 / 表格。
 * 无 theme prop - 明暗由 .theme-light/.theme-dark CSS 作用域处理。
 */
import { useMemo, useState } from 'react'
import { Plus, Search, Pencil, Trash2, Clock, History, Loader2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  triggersApi,
  triggerKeys,
  getTriggerId,
  type TriggerConfig,
} from '../services/triggers-api'
import { tasksApi } from '../services/tasks-api'
import { Table, Tag, Switch, type TableColumn } from './ui'
import { confirmDialog } from './ui/confirm'
import { toast } from './ui/toast'
import TriggerConfigModal from './trigger/TriggerConfigModal'
import TriggerTaskRecordsModal from './trigger/TriggerTaskRecordsModal'

interface Props {
  /** 点击任务记录"查看明细"时回调，由 App 切到任务看板并打开 TaskDetailDrawer */
  onViewTask: (taskId: string) => void
}

/** 格式化 ISO 为本地时间字符串。 */
function formatDateTime(iso?: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '-'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function TriggersPage({ onViewTask }: Props) {
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [configModal, setConfigModal] = useState<{
    open: boolean
    mode: 'create' | 'edit'
    trigger: TriggerConfig | null
  }>({ open: false, mode: 'create', trigger: null })
  const [recordsTrigger, setRecordsTrigger] = useState<TriggerConfig | null>(null)

  // trigger 列表
  const triggersQuery = useQuery({
    queryKey: triggerKeys.list(),
    queryFn: () => triggersApi.list(),
  })
  // workflow 列表（建名 map + ConfigModal 选择用）
  const workflowsQuery = useQuery({
    queryKey: ['workflow-registry', 'list'],
    queryFn: () => tasksApi.listWorkflows(),
  })

  const workflows = workflowsQuery.data?.items ?? []
  const workflowNameMap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const w of workflows) {
      map[w._id] = w.name
      map[w.workflow_id] = w.name
    }
    return map
  }, [workflows])

  const triggers = triggersQuery.data?.items ?? []

  // 搜索过滤
  const filtered = useMemo(() => {
    if (!search.trim()) return triggers
    const q = search.trim().toLowerCase()
    return triggers.filter((t) => {
      const wfName = workflowNameMap[t.workflow_id] || ''
      const cron = t.cron_expression || ''
      return (
        wfName.toLowerCase().includes(q) ||
        cron.toLowerCase().includes(q) ||
        t.workflow_id.toLowerCase().includes(q)
      )
    })
  }, [triggers, search, workflowNameMap])

  // stats
  const stats = useMemo(() => {
    const enabled = triggers.filter((t) => t.enabled).length
    return { total: triggers.length, enabled, disabled: triggers.length - enabled }
  }, [triggers])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: triggerKeys.lists() })

  const toggleMutation = useMutation({
    mutationFn: (vars: { trigger: TriggerConfig; enabled: boolean }) =>
      triggersApi.toggle(getTriggerId(vars.trigger), vars.enabled),
    onSuccess: () => {
      invalidate()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (triggerId: string) => triggersApi.remove(triggerId),
    onSuccess: () => {
      invalidate()
      toast.success('已删除')
    },
  })

  const handleDelete = async (trigger: TriggerConfig) => {
    const ok = await confirmDialog({
      title: '删除定时任务',
      description: '删除后将永久停止该定时任务，已触发的任务记录不受影响。',
      okText: '删除',
      danger: true,
    })
    if (!ok) return
    deleteMutation.mutate(getTriggerId(trigger))
  }

  // 表格列（Table 泛型约束 Record<string, unknown>，render 内 cast 回 TriggerConfig）
  const columns: TableColumn<Record<string, unknown>>[] = [
    {
      title: '工作流',
      key: 'workflow',
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return (
          <span className="text-[#fafafa]">{workflowNameMap[t.workflow_id] || t.workflow_id}</span>
        )
      },
    },
    {
      title: '类型',
      key: 'type',
      width: 90,
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return (
          <Tag color={t.type === 'cron' ? 'blue' : 'purple'}>
            {t.type === 'cron' ? '重复' : '一次性'}
          </Tag>
        )
      },
    },
    {
      title: '调度',
      key: 'schedule',
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return t.type === 'cron' ? (
          <span className="font-mono text-[11px] text-slate-300">{t.cron_expression || '-'}</span>
        ) : (
          <span className="text-[11px] text-slate-300">{formatDateTime(t.execute_at)}</span>
        )
      },
    },
    {
      title: '状态',
      key: 'status',
      width: 120,
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return (
          <div className="flex items-center gap-2">
            <Switch
              checked={t.enabled}
              onChange={(c) => toggleMutation.mutate({ trigger: t, enabled: c })}
              size="small"
            />
            <Tag color={t.enabled ? 'success' : 'default'}>{t.enabled ? '启用' : '停用'}</Tag>
          </div>
        )
      },
    },
    {
      title: '下次执行',
      key: 'next',
      width: 150,
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return (
          <span className="text-[11px] text-slate-400">{formatDateTime(t.next_trigger_at)}</span>
        )
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, record) => {
        const t = record as unknown as TriggerConfig
        return (
          <div className="flex items-center gap-1">
            <button
              title="查看记录"
              onClick={() => setRecordsTrigger(t)}
              className="p-1.5 rounded-lg hover:bg-[#27272a] text-slate-400 hover:text-[#1E5EFF] cursor-pointer transition-colors"
            >
              <History size={14} />
            </button>
            <button
              title="编辑"
              onClick={() => setConfigModal({ open: true, mode: 'edit', trigger: t })}
              className="p-1.5 rounded-lg hover:bg-[#27272a] text-slate-400 hover:text-[#1E5EFF] cursor-pointer transition-colors"
            >
              <Pencil size={14} />
            </button>
            <button
              title="删除"
              onClick={() => handleDelete(t)}
              className="p-1.5 rounded-lg hover:bg-[#27272a] text-slate-400 hover:text-red-400 cursor-pointer transition-colors"
            >
              <Trash2 size={14} />
            </button>
          </div>
        )
      },
    },
  ]

  const loading = triggersQuery.isLoading

  return (
    <div className="space-y-5">
      {/* 标题 + 新建 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-[#fafafa] flex items-center gap-2">
            <Clock size={18} /> 定时任务
          </h1>
          <p className="text-[11px] text-[#71717a] mt-0.5">
            管理工作流的定时触发配置，查看触发记录
          </p>
        </div>
        <button
          onClick={() => setConfigModal({ open: true, mode: 'create', trigger: null })}
          className="flex items-center gap-1.5 h-8 px-3 rounded-md bg-[#1E5EFF] hover:bg-[#1a4fd6] text-white text-xs font-medium cursor-pointer transition-colors"
        >
          <Plus size={14} /> 新建定时任务
        </button>
      </div>

      {/* stats */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: '定时任务总数', value: stats.total, color: '#1E5EFF' },
          { label: '启用中', value: stats.enabled, color: '#10B981' },
          { label: '已停用', value: stats.disabled, color: '#71717a' },
        ].map((s) => (
          <div key={s.label} className="p-4 rounded-xl border border-[#27272a] bg-[#18181b]">
            <div className="text-[11px] text-[#71717a]">{s.label}</div>
            <div className="text-2xl font-semibold mt-1" style={{ color: s.color }}>
              {s.value}
            </div>
          </div>
        ))}
      </div>

      {/* 搜索 */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-sm">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#71717a]" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索工作流名 / cron 表达式"
            className="w-full h-8 pl-8 pr-3 rounded-md border border-[#27272a] bg-[#121214] text-[#fafafa] text-xs placeholder:text-[#52525b] focus:outline-none focus:border-[#1E5EFF] focus:ring-1 focus:ring-[#1E5EFF]/30"
          />
        </div>
      </div>

      {/* 表格 */}
      {loading ? (
        <div className="flex items-center justify-center py-16 text-[#71717a]">
          <Loader2 size={18} className="animate-spin" />
        </div>
      ) : (
        <Table
          dataSource={filtered as unknown as Record<string, unknown>[]}
          columns={columns}
          rowKey={(record) => getTriggerId(record as unknown as TriggerConfig)}
        />
      )}

      {/* 配置 Modal */}
      <TriggerConfigModal
        open={configModal.open}
        mode={configModal.mode}
        trigger={configModal.trigger}
        workflows={workflows}
        onClose={() => setConfigModal((s) => ({ ...s, open: false }))}
        onSaved={invalidate}
      />

      {/* 任务记录 Modal */}
      <TriggerTaskRecordsModal
        trigger={recordsTrigger}
        workflows={workflows}
        onClose={() => setRecordsTrigger(null)}
        onViewTask={(taskId) => {
          setRecordsTrigger(null)
          onViewTask(taskId)
        }}
      />
    </div>
  )
}

export default TriggersPage
