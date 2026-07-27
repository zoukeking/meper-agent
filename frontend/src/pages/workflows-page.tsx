/**
 * Workflows page — manage workflow templates with lifecycle status.
 *
 * Powered by TanStack Query + workflows-api.ts service.
 * Backend contract: snake_case fields, paginated list.
 *
 * Features:
 * - Workflow list with search, status filter, and stats
 * - Create workflow modal
 * - Publish / Archive lifecycle management
 * - Navigate to workflow detail/editor
 */
import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Tag, Select, Tooltip, message, Spin, Modal, Input } from 'antd'
import {
  PlusOutlined,
  SearchOutlined,
  BranchesOutlined,
  DeleteOutlined,
  InboxOutlined,
  CloudUploadOutlined,
  StopOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import { workflowsApi, workflowKeys, type WorkflowSummary, type WorkflowStatusValue } from '../services/workflows-api'

/* ─── Status mappings ─── */
const STATUS_STYLES: Record<WorkflowStatusValue, { label: string; color: string; bg: string }> = {
  draft: { label: '草稿', color: '#F59E0B', bg: '#FEF3C7' },
  published: { label: '已发布', color: '#10B981', bg: '#D1FAE5' },
  archived: { label: '已归档', color: '#94A3B8', bg: '#F1F5F9' },
}

/* ─── Debounce hook ─── */
function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

export default function WorkflowsPage() {
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

  /* ─── Query: workflow list ─── */
  const queryParams = {
    page: 1,
    page_size: 50,
    ...(debouncedSearch ? { name: debouncedSearch } : {}),
    ...(statusFilter !== 'all' ? { status: statusFilter } : {}),
  }

  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: workflowKeys.list(queryParams),
    queryFn: () => workflowsApi.list(queryParams),
  })

  const workflows = data?.items ?? []
  const total = data?.total ?? 0

  /* ─── Mutation: create ─── */
  const createMutation = useMutation({
    mutationFn: (data: { name: string; description: string }) =>
      workflowsApi.create(data),
    onSuccess: (newWf) => {
      message.success('工作流创建成功')
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
      setCreateOpen(false)
      navigate(`/workflows/${newWf.id}`)
    },
  })

  /* ─── Mutation: delete ─── */
  const deleteMutation = useMutation({
    mutationFn: workflowsApi.remove,
    onSuccess: () => {
      message.success('工作流已删除')
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
    },
  })

  /* ─── Mutation: publish ─── */
  const publishMutation = useMutation({
    mutationFn: workflowsApi.publish,
    onSuccess: () => {
      message.success('工作流已发布')
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
    },
  })

  /* ─── Mutation: archive ─── */
  const archiveMutation = useMutation({
    mutationFn: workflowsApi.archive,
    onSuccess: () => {
      message.success('工作流已归档')
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
    },
  })

  /* ─── Actions ─── */
  const handleCreate = () => {
    setCreateName('')
    setCreateDesc('')
    setCreateOpen(true)
  }

  const handleCreateSubmit = async () => {
    if (!createName.trim()) {
      message.warning('请输入工作流名称')
      return
    }
    createMutation.mutate({
      name: createName.trim(),
      description: createDesc.trim(),
    })
  }

  const handleDelete = (wf: WorkflowSummary) => {
    Modal.confirm({
      title: '确认删除',
      content: `确定要删除工作流「${wf.name}」吗？此操作不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteMutation.mutate(wf.id),
    })
  }

  const handlePublish = (wf: WorkflowSummary) => {
    Modal.confirm({
      title: '确认发布',
      content: `确定要发布工作流「${wf.name}」吗？发布后将可在创建任务时选择。`,
      okText: '发布',
      cancelText: '取消',
      onOk: () => publishMutation.mutate(wf.id),
    })
  }

  const handleArchive = (wf: WorkflowSummary) => {
    Modal.confirm({
      title: '确认归档',
      content: `确定要归档工作流「${wf.name}」吗？`,
      okText: '归档',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => archiveMutation.mutate(wf.id),
    })
  }

  /* ─── Format time ─── */
  function formatTime(iso: string) {
    if (!iso) return '-'
    // eslint-disable-next-line react-hooks/purity -- Date.now is needed for relative time display
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return '刚刚'
    if (mins < 60) return `${mins} 分钟前`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours} 小时前`
    return `${Math.floor(hours / 24)} 天前`
  }

  /* ─── Stats ─── */
  const stats = [
    { label: '工作流总数', value: total.toString() },
    { label: '已发布', value: workflows.filter(w => w.status === 'published').length.toString() },
    { label: '草稿', value: workflows.filter(w => w.status === 'draft').length.toString() },
    { label: '已归档', value: workflows.filter(w => w.status === 'archived').length.toString() },
  ]

  return (
    <div className="animate-[fadeIn_0.3s_ease-out]">
      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        {stats.map((s) => (
          <div key={s.label} className="rounded-xl border border-gray-200 bg-white p-4">
            <div className="text-2xl font-semibold text-[#0F172A] mb-0.5">
              {isLoading ? <Spin size="small" /> : s.value}
            </div>
            <div className="text-xs text-[#64748B]">{s.label}</div>
          </div>
        ))}
      </div>

      {/* Header actions */}
      <div className="flex items-center justify-between gap-4 mb-6">
        <div className="flex items-center gap-3">
          <div className="relative">
            <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-[#94A3B8] text-sm" />
            <input
              type="text"
              placeholder="搜索工作流..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="pl-9 pr-4 py-2 rounded-lg border border-gray-200 text-sm focus:outline-none focus:ring-2 w-64"
              style={{ '--tw-ring-color': t.bg } as React.CSSProperties}
            />
          </div>
          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            className="w-28"
            options={[
              { value: 'all', label: '全部' },
              { value: 'draft', label: '草稿' },
              { value: 'published', label: '已发布' },
              { value: 'archived', label: '已归档' },
            ]}
          />
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
          新建工作流
        </Button>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <Spin size="large" tip="加载中..." />
        </div>
      )}

      {/* Error state */}
      {isError && !isLoading && (
        <div className="flex flex-col items-center justify-center py-20 text-[#94A3B8]">
          <ExclamationCircleOutlined className="text-4xl mb-3" />
          <p className="text-sm">
            {error && typeof error === 'object' && 'message' in error
              ? (error as { message: string }).message
              : '加载失败，请稍后重试'}
          </p>
        </div>
      )}

      {/* Empty state */}
      {!isLoading && !isError && workflows.length === 0 && (
        <div className="flex flex-col items-center justify-center py-20 text-[#94A3B8]">
          <InboxOutlined className="text-4xl mb-3" />
          <p className="text-sm">暂无工作流，点击右上角「新建工作流」开始创建</p>
        </div>
      )}

      {/* Workflow cards */}
      {!isLoading && !isError && workflows.length > 0 && (
        <div className="grid grid-cols-3 gap-4">
          {workflows.map((wf) => {
            const ss = STATUS_STYLES[wf.status] ?? STATUS_STYLES.draft
            return (
              <div
                key={wf.id}
                onClick={() => navigate(`/workflows/${wf.id}`)}
                className="rounded-xl border border-gray-200 bg-white p-5 hover:shadow-sm transition-all duration-200 cursor-pointer"
              >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="w-10 h-10 rounded-lg flex items-center justify-center text-base shrink-0" style={{ background: t.bg, color: t.primary }}>
                      <BranchesOutlined />
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-[#0F172A] truncate">{wf.name}</span>
                        <Tooltip title={`v${wf.version}`}>
                          <span className="text-[10px] font-mono text-[#94A3B8] border border-gray-200 rounded px-1 leading-none py-0.5">v{wf.version}</span>
                        </Tooltip>
                      </div>
                      <div className="text-xs text-[#64748B] truncate max-w-[180px]">{wf.description || '暂无描述'}</div>
                    </div>
                  </div>
                </div>

                {/* Tags */}
                <div className="flex items-center gap-2 mb-3 flex-wrap">
                  <Tag className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded" style={{ color: ss.color, background: ss.bg, borderColor: 'transparent' }}>
                    {ss.label}
                  </Tag>
                  {wf.node_count > 0 && (
                    <Tag className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded" style={{ color: '#3B82F6', background: '#EFF6FF', borderColor: 'transparent' }}>
                      {wf.node_count} 节点
                    </Tag>
                  )}
                  {wf.tags?.map((tag) => (
                    <Tag key={tag} className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded" style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>
                      {tag}
                    </Tag>
                  ))}
                </div>

                {/* Footer */}
                <div className="flex items-center justify-between pt-3 border-t border-gray-50">
                  <span className="text-[11px] text-[#94A3B8]">{formatTime(wf.updated_at)}</span>
                  <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                    {wf.status === 'draft' && (
                      <Tooltip title="发布">
                        <button
                          onClick={() => handlePublish(wf)}
                          disabled={publishMutation.isPending}
                          className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#10B981] hover:bg-[#D1FAE5] transition-colors duration-150 text-xs"
                        ><CloudUploadOutlined /></button>
                      </Tooltip>
                    )}
                    {wf.status === 'published' && (
                      <Tooltip title="归档">
                        <button
                          onClick={() => handleArchive(wf)}
                          disabled={archiveMutation.isPending}
                          className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#F59E0B] hover:bg-[#FEF3C7] transition-colors duration-150 text-xs"
                        ><StopOutlined /></button>
                      </Tooltip>
                    )}
                    <Tooltip title="编辑">
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/workflows/${wf.id}`) }}
                        className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#94A3B8] hover:text-[#0F172A] hover:bg-gray-50 transition-colors duration-150 text-xs"
                      ><EditOutlined /></button>
                    </Tooltip>
                    <Tooltip title="删除">
                      <button
                        onClick={() => handleDelete(wf)}
                        disabled={deleteMutation.isPending}
                        className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#94A3B8] hover:text-[#EF4444] hover:bg-gray-50 transition-colors duration-150 text-xs"
                      ><DeleteOutlined /></button>
                    </Tooltip>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Create Workflow Modal */}
      <Modal
        title="新建工作流"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreateSubmit}
        okText="创建"
        cancelText="取消"
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: !createName.trim() }}
        destroyOnClose
        width={480}
      >
        <div className="flex flex-col gap-4 py-2">
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              名称 <span className="text-[#EF4444]">*</span>
            </label>
            <Input
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              placeholder="如：数据分析流水线"
              maxLength={200}
              showCount
              autoFocus
            />
          </div>
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">描述</label>
            <Input.TextArea
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="简要描述工作流的用途"
              maxLength={1000}
              showCount
              rows={3}
            />
          </div>
        </div>
      </Modal>
    </div>
  )
}
