/**
 * Agents page — manage AI agents with lifecycle status.
 *
 * Powered by TanStack Query + agent-api.ts service.
 * Backend contract: snake_case fields, paginated list.
 *
 * Design: DESIGN.md aligned — table layout, surface ladder,
 * semantic tokens, 4px/8px radius, strict type scale.
 *
 * Features:
 * - Agent table with search, status filter, and inline stats
 * - Create agent → auto-redirect to detail page
 * - Publish / Archive lifecycle management
 * - Duplicate with automatic naming
 */
import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Select, Tooltip, message, Spin, Modal, Input } from 'antd'
import {
  PlusOutlined,
  SearchOutlined,
  CopyOutlined,
  DeleteOutlined,
  InboxOutlined,
  CloudUploadOutlined,
  StopOutlined,
  FilterOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import {
  agentApi,
  agentKeys,
  type Agent,
  type AgentListParams,
  type AgentStatus,
} from '../services/agent-api'
import { AGENT_STATUS_STYLES } from '../constants/agent-status'

/* ─── Status mappings ─── */
const STATUS_STYLES = AGENT_STATUS_STYLES

/* ─── Debounce hook ─── */
function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/* ─── Format relative time ─── */
function formatTime(iso: string) {
  if (!iso) return '-'
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

export default function AgentsPage() {
  const { t } = useTheme()
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  /* ─── Filter state ─── */
  const [searchInput, setSearchInput] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const debouncedSearch = useDebouncedValue(searchInput, 300)

  /* ─── Create modal state ─── */
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createDesc, setCreateDesc] = useState('')
  const [creating, setCreating] = useState(false)

  /* ─── Query: agent list ─── */
  const queryParams: AgentListParams = {
    page: 1,
    page_size: 50,
    ...(debouncedSearch ? { name: debouncedSearch } : {}),
    status: statusFilter === 'all' ? 'all' : (statusFilter as AgentStatus),
  }

  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: agentKeys.list(queryParams),
    queryFn: () => agentApi.list(queryParams),
  })

  const agents = data?.items ?? []
  const total = data?.total ?? 0

  /* ─── Mutation: delete agent ─── */
  const deleteMutation = useMutation({
    mutationFn: agentApi.remove,
    onSuccess: () => {
      message.success('Agent 已删除')
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
    },
  })

  /* ─── Mutation: duplicate agent ─── */
  const duplicateMutation = useMutation({
    mutationFn: agentApi.duplicate,
    onSuccess: () => {
      message.success('Agent 已复制')
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
    },
  })

  /* ─── Mutation: publish agent ─── */
  const publishMutation = useMutation({
    mutationFn: agentApi.publish,
    onSuccess: () => {
      message.success('Agent 已发布')
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
    },
  })

  /* ─── Mutation: archive agent ─── */
  const archiveMutation = useMutation({
    mutationFn: agentApi.archive,
    onSuccess: () => {
      message.success('Agent 已下架')
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
    },
  })

  /* ─── Actions ─── */
  const handleCreate = () => {
    setCreateName('')
    setCreateDesc('')
    setCreating(false)
    setCreateOpen(true)
  }

  const handleCreateSubmit = async () => {
    if (!createName.trim()) {
      message.warning('请输入 Agent 名称')
      return
    }
    setCreating(true)
    try {
      const newAgent = await agentApi.create({
        name: createName.trim(),
        description: createDesc.trim(),
      })
      message.success('Agent 创建成功')
      queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
      setCreateOpen(false)
      navigate(`/agents/${newAgent.id}`)
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '创建失败'
      message.error(msg)
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = (agent: Agent) => {
    Modal.confirm({
      title: '确认删除',
      content: `确定要删除 Agent「${agent.name}」吗？此操作不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteMutation.mutate(agent.id),
    })
  }

  const handleDuplicate = (agent: Agent) => {
    Modal.confirm({
      title: '确认复制',
      content: `确定要复制 Agent「${agent.name}」吗？将创建一个新的草稿 Agent。`,
      okText: '复制',
      cancelText: '取消',
      onOk: () => duplicateMutation.mutate(agent.id),
    })
  }

  const handlePublish = (agent: Agent) => {
    Modal.confirm({
      title: '确认发布',
      content: `确定要发布 Agent「${agent.name}」吗？发布后将出现在对话测试面板中。`,
      okText: '发布',
      cancelText: '取消',
      onOk: () => publishMutation.mutate(agent.id),
    })
  }

  const handleArchive = (agent: Agent) => {
    Modal.confirm({
      title: '确认下架',
      content: `确定要下架 Agent「${agent.name}」吗？下架后新对话将无法使用此 Agent。`,
      okText: '下架',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => archiveMutation.mutate(agent.id),
    })
  }

  /* ─── Computed stats ─── */
  const publishedCount = agents.filter(a => a.status === 'published').length
  const draftCount = agents.filter(a => a.status === 'draft').length
  const archivedCount = agents.filter(a => a.status === 'archived').length

  /* ─── Pending state helper ─── */
  const isPending = publishMutation.isPending || archiveMutation.isPending || duplicateMutation.isPending || deleteMutation.isPending

  return (
    <div>
      {/* ════════ Page header ════════ */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-txt" style={{ letterSpacing: '-0.02em' }}>
            Agent 管理
          </h1>
          <p className="text-[13px] text-txt-3 mt-1">
            创建、配置和部署 AI Agent
          </p>
        </div>
        <button
          onClick={handleCreate}
          className="flex items-center gap-1.5 px-4 h-9 text-[13px] font-medium text-white border-0 cursor-pointer"
          style={{ background: t.primary, borderRadius: 6 }}
        >
          <PlusOutlined style={{ fontSize: 12 }} />
          新建 Agent
        </button>
      </div>

      {/* ════════ Stats bar (inline, not cards) ════════ */}
      <div className="flex items-center gap-6 mb-5">
        {[
          { label: '全部', value: total.toString() },
          { label: '已发布', value: publishedCount.toString() },
          { label: '草稿', value: draftCount.toString() },
          { label: '已归档', value: archivedCount.toString() },
        ].map((s, i) => (
          <div key={s.label} className="flex items-center gap-5">
            <div className="flex items-baseline gap-1.5">
              <span className="text-lg font-semibold text-txt" style={{ letterSpacing: '-0.01em' }}>
                {isLoading ? <Spin size="small" /> : s.value}
              </span>
              <span className="text-[13px] text-txt-3">{s.label}</span>
            </div>
            {i < 3 && <div className="w-px h-4 bg-line" />}
          </div>
        ))}
      </div>

      {/* ════════ Action bar ════════ */}
      <div className="flex items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-2">
          {/* Search */}
          <div className="relative">
            <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-muted text-[13px]" />
            <input
              type="text"
              placeholder="搜索 Agent..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="pl-8 pr-3 py-[6px] text-[13px] border border-line bg-canvas text-txt placeholder:text-muted focus:outline-none focus:border-primary w-56"
              style={{ borderRadius: 6, '--tw-ring-color': t.primary } as React.CSSProperties}
            />
          </div>
          {/* Filter */}
          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            className="w-28"
            suffixIcon={<FilterOutlined style={{ fontSize: 11 }} />}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'published', label: '已发布' },
              { value: 'draft', label: '草稿' },
              { value: 'archived', label: '已归档' },
            ]}
          />
        </div>
        <span className="text-[12px] text-txt-muted">
          {!isLoading && `共 ${total} 个 Agent`}
        </span>
      </div>

      {/* ════════ Loading state ════════ */}
      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <Spin size="large" />
        </div>
      )}

      {/* ════════ Error state ════════ */}
      {isError && !isLoading && (
        <div className="flex flex-col items-center justify-center py-20">
          <InboxOutlined className="text-3xl text-muted mb-2" />
          <p className="text-[13px] text-txt-3 mb-1">加载失败</p>
          <p className="text-[12px] text-muted">
            {error && typeof error === 'object' && 'message' in error
              ? (error as { message: string }).message
              : '请稍后重试'}
          </p>
        </div>
      )}

      {/* ════════ Empty state ════════ */}
      {!isLoading && !isError && agents.length === 0 && (
        <div
          className="flex flex-col items-center justify-center py-20 bg-canvas border border-line"
          style={{ borderRadius: 8 }}
        >
          <InboxOutlined className="text-3xl text-muted mb-2" />
          <p className="text-[13px] text-txt-2 mb-3">暂无 Agent</p>
          <button
            onClick={handleCreate}
            className="flex items-center gap-1.5 px-4 h-8 text-[13px] font-medium text-white border-0 cursor-pointer"
            style={{ background: t.primary, borderRadius: 6 }}
          >
            <PlusOutlined style={{ fontSize: 12 }} />
            创建第一个 Agent
          </button>
        </div>
      )}

      {/* ════════ Agent table ════════ */}
      {!isLoading && !isError && agents.length > 0 && (
        <div
          className="bg-canvas border border-line overflow-hidden overflow-x-auto"
          style={{ borderRadius: 8 }}
        >
          {/* min-width ensures horizontal scroll on narrow screens instead of column crush */}
          <div style={{ minWidth: 780 }}>
            {/* Table header */}
            <div
              className="grid items-center px-5 h-10 bg-surface-muted border-b border-line"
              style={{ gridTemplateColumns: 'minmax(180px,2fr) minmax(110px,1.2fr) 72px minmax(90px,1fr) 80px 80px' }}
            >
              <span className="text-[12px] font-medium text-txt-3">名称</span>
              <span className="text-[12px] font-medium text-txt-3">模型</span>
              <span className="text-[12px] font-medium text-txt-3">状态</span>
              <span className="text-[12px] font-medium text-txt-3">工具</span>
              <span className="text-[12px] font-medium text-txt-3">更新</span>
              <span className="text-[12px] font-medium text-txt-3 text-right">操作</span>
            </div>

            {/* Rows */}
            {agents.map((agent, i) => {
              const ss = STATUS_STYLES[agent.status] ?? STATUS_STYLES.draft
              const modelLabel = agent.default_model || '未配置'
              const canPublish = agent.status === 'draft' || agent.status === 'archived'
              const canArchive = agent.status === 'published'
              const skillCount = agent.skill_ids?.length ?? 0
              const mcpCount = agent.mcp_connection_ids?.length ?? 0
              const builtinCount = agent.builtin_config?.length ?? 0

              return (
                <div
                  key={agent.id}
                  onClick={() => navigate(`/agents/${agent.id}`)}
                  className="grid items-center px-5 h-[52px] cursor-pointer transition-colors duration-150 hover:bg-surface"
                  style={{
                    gridTemplateColumns: 'minmax(180px,2fr) minmax(110px,1.2fr) 72px minmax(90px,1fr) 80px 80px',
                    borderBottom: i < agents.length - 1 ? '1px solid rgb(var(--c-line-2))' : 'none',
                  }}
                >
                  {/* Name + desc */}
                  <div className="min-w-0 pr-3">
                    <div className="text-[14px] font-medium text-txt truncate">
                      {agent.name}
                    </div>
                    <div className="text-[12px] text-txt-3 truncate">
                      {agent.description || '暂无描述'}
                    </div>
                  </div>

                  {/* Model (mono font) */}
                  <span className="text-[13px] text-txt-2 font-mono truncate pr-2" title={modelLabel}>
                    {modelLabel}
                  </span>

                  {/* Status badge */}
                  <span
                    className="inline-flex items-center text-[12px] font-medium w-fit px-2 py-[1px]"
                    style={{ color: ss.color, background: ss.bg, borderRadius: 4 }}
                  >
                    {ss.label}
                  </span>

                  {/* Tool tags */}
                  <div className="flex items-center gap-1 min-w-0 flex-wrap">
                    {skillCount > 0 && (
                      <span
                        className="text-[11px] px-1.5 py-[1px] shrink-0"
                        style={{ color: t.primary, background: t.bg, borderRadius: 4 }}
                      >
                        {skillCount} Skill
                      </span>
                    )}
                    {builtinCount > 0 && (
                      <span
                        className="text-[11px] px-1.5 py-[1px] shrink-0"
                        style={{ color: '#F59E0B', background: '#FFFBEB', borderRadius: 4 }}
                      >
                        {builtinCount} Built-in
                      </span>
                    )}
                    {mcpCount > 0 && (
                      <span
                        className="text-[11px] px-1.5 py-[1px] shrink-0"
                        style={{ color: '#10B981', background: '#ECFDF5', borderRadius: 4 }}
                      >
                        {mcpCount} MCP
                      </span>
                    )}
                    {skillCount === 0 && builtinCount === 0 && mcpCount === 0 && (
                      <span className="text-[12px] text-muted">-</span>
                    )}
                  </div>

                  {/* Time */}
                  <span className="text-[12px] text-txt-muted whitespace-nowrap">
                    {formatTime(agent.updated_at)}
                  </span>

                  {/* Actions */}
                  <div className="flex items-center gap-0.5 justify-end" onClick={(e) => e.stopPropagation()}>
                    {canPublish && (
                      <Tooltip title="发布">
                        <button
                          onClick={() => handlePublish(agent)}
                          disabled={isPending}
                          className="border-0 bg-transparent w-7 h-7 flex items-center justify-center text-[#10B981] hover:bg-[#D1FAE5] transition-colors duration-150 text-[13px] disabled:opacity-40"
                          style={{ borderRadius: 4 }}
                        >
                          <CloudUploadOutlined />
                        </button>
                      </Tooltip>
                    )}
                    {canArchive && (
                      <Tooltip title="下架">
                        <button
                          onClick={() => handleArchive(agent)}
                          disabled={isPending}
                          className="border-0 bg-transparent w-7 h-7 flex items-center justify-center text-[#F59E0B] hover:bg-[#FEF3C7] transition-colors duration-150 text-[13px] disabled:opacity-40"
                          style={{ borderRadius: 4 }}
                        >
                          <StopOutlined />
                        </button>
                      </Tooltip>
                    )}
                    <Tooltip title="复制">
                      <button
                        onClick={() => handleDuplicate(agent)}
                        disabled={isPending}
                        className="border-0 bg-transparent w-7 h-7 flex items-center justify-center text-txt-muted hover:text-txt hover:bg-surface-muted transition-colors duration-150 text-[13px] disabled:opacity-40"
                        style={{ borderRadius: 4 }}
                      >
                        <CopyOutlined />
                      </button>
                    </Tooltip>
                    <Tooltip title="删除">
                      <button
                        onClick={() => handleDelete(agent)}
                        disabled={isPending}
                        className="border-0 bg-transparent w-7 h-7 flex items-center justify-center text-txt-muted hover:text-error hover:bg-error/10 transition-colors duration-150 text-[13px] disabled:opacity-40"
                        style={{ borderRadius: 4 }}
                      >
                        <DeleteOutlined />
                      </button>
                    </Tooltip>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ════════ Create Agent Modal ════════ */}
      <Modal
        title={<span className="text-base font-semibold">新建 Agent</span>}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreateSubmit}
        okText="创建"
        cancelText="取消"
        confirmLoading={creating}
        okButtonProps={{ disabled: !createName.trim() }}
        destroyOnHidden
        width={480}
      >
        <div className="flex flex-col gap-4 py-2">
          <div>
            <label className="block text-[13px] text-txt mb-1.5">
              名称 <span className="text-error">*</span>
            </label>
            <Input
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              placeholder="如：客服助手"
              maxLength={100}
              showCount
              autoFocus
            />
          </div>
          <div>
            <label className="block text-[13px] text-txt mb-1.5">
              描述
            </label>
            <Input.TextArea
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="简要描述 Agent 的用途"
              maxLength={500}
              showCount
              rows={3}
            />
          </div>
        </div>
      </Modal>
    </div>
  )
}
