/**
 * Task board page — 6 列并排的全状态看板。
 *
 * 设计要点：
 * - 6 列状态分桶：pending / running / waiting_human / completed / failed / cancelled
 * - 列表查询：useQueries 并发发起 6 次 list（按 status，page=1, page_size=50）
 * - 节点进度：仅对 running / waiting_human 两列拉详情（5 秒轮询，上限 20 个）
 * - 详情查询与详情 Drawer 共用 taskKeys.detail(id) 缓存
 * - 搜索改为纯前端过滤（task.id / workflow_id includes）
 */
import { useState, useMemo, useCallback, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient, useQueries } from '@tanstack/react-query'
import { Button, Tag, message, Spin, Modal, Drawer, Input, Empty, Alert, Segmented, DatePicker, Switch, Radio, Divider, Select, Tooltip } from 'antd'
import {
  StopOutlined,
  RedoOutlined,
  DeleteOutlined,
  SearchOutlined,
  PlusOutlined,
  WarningOutlined,
  ThunderboltOutlined,
  CloseCircleOutlined,
  CheckOutlined,
  EditOutlined,
  ClockCircleOutlined,
  CaretRightOutlined,
  RollbackOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import {
  tasksApi,
  taskKeys,
  type TaskSummary,
  type TaskStatusValue,
  type TaskDetail,
  type NodeProgress,
  type CommentValue,
  BOARD_STATUSES,
  parseNodeProgress,
} from '../services/tasks-api'
import { TASK_STATUS_STYLES } from '../constants/task-status'
import { TaskBoardColumn } from '../components/task-board-column'
import { TaskOutputFiles } from '../components/task-result-card'
import AgentTimeline from '../components/agent-timeline'
import { parseBackendDate } from '../lib/format'
import { WorkflowTriggerAPI } from '../services/workflow-trigger-api'
import { workflowsApi } from '../services/workflows-api'
import type { TriggerConfig, TriggerType } from '../types/workflow-trigger'
import type { VariableDefinition } from '../features/workflow-editor/utils/variable-types'
import TriggerSchedulePicker from '../components/workflows/TriggerSchedulePicker'
import VariableFormField from '../features/workflow-editor/VariableFormField'
import dayjs from 'dayjs'

/* ─── helpers ─── */
function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  useMemo(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)
  }, [value, delay])
  return debounced
}

function formatDateTime(iso: string) {
  if (!iso) return '-'
  return parseBackendDate(iso).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/* ─── active 状态集合：需要拉详情以展示节点进度 ─── */
const ACTIVE_STATUSES: TaskStatusValue[] = ['running', 'waiting_human']
/* 进度展示的任务上限，避免请求风暴 */
const MAX_PROGRESS_TASKS = 20

export default function TasksPage() {
  const { t } = useTheme()
  const queryClient = useQueryClient()

  /* ─── Search state（纯前端过滤） ─── */
  const [searchInput, setSearchInput] = useState('')
  const debouncedSearch = useDebouncedValue(searchInput.toLowerCase(), 200)

  /* ─── Create modal state ─── */
  const [createOpen, setCreateOpen] = useState(false)
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string>('')
  const [createInput, setCreateInput] = useState('')
  const [workflowSearch, setWorkflowSearch] = useState('')
  const debouncedWorkflowSearch = useDebouncedValue(workflowSearch, 300)

  /* ─── Detail drawer state ─── */
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null)

  /* ─── Agent node detail modal state ─── */
  const [nodeDetail, setNodeDetail] = useState<{ taskId: string; nodeId: string } | null>(null)
  /* ─── Approval modal state ─── */
  const [approvalAction, setApprovalAction] = useState<'approve' | 'reject' | null>(null)
  const [approvalComment, setApprovalComment] = useState('')
  // comment 输入模式：text 纯文本 / json 结构化（与看板卡片弹窗保持一致）
  const [approvalCommentMode, setApprovalCommentMode] = useState<'text' | 'json'>('text')
  const [approvalTask, setApprovalTask] = useState<TaskSummary | TaskDetail | null>(null)

  /* ─── Rewind state（退回重跑 Modal）─── */
  const [rewindModalOpen, setRewindModalOpen] = useState(false)
  const [rewindTargetNode, setRewindTargetNode] = useState<string>('')
  // 变量编辑模式：'none' = 不改变量(纯退回), 'json' = JSON 编辑
  const [rewindVarsMode, setRewindVarsMode] = useState<'none' | 'json'>('none')
  const [rewindVarsText, setRewindVarsText] = useState<string>('')

  /* ─── Edit scheduled task modal state ─── */
  const [editTask, setEditTask] = useState<TaskSummary | null>(null)
  const [editEnabled, setEditEnabled] = useState(false)
  const [editTriggerType, setEditTriggerType] = useState<TriggerType>('cron')
  const [editCronExpression, setEditCronExpression] = useState('0 9 * * *')
  const [editScheduledAt, setEditScheduledAt] = useState<string>('')
  const [editDefaultInput, setEditDefaultInput] = useState<Record<string, unknown>>({})
  const [editDirty, setEditDirty] = useState(false)

  /* ─── 5 列并发列表查询（非 pending 状态分桶）+ trigger 列表 ─── */
  // pending 列改为展示 trigger 列表（定时任务管理），不再查询 pending task
  const TASK_STATUSES = BOARD_STATUSES.filter((s) => s !== 'pending') as TaskStatusValue[]
  const boardQueries = useQueries({
    queries: TASK_STATUSES.map((status) => ({
      queryKey: taskKeys.list({ status, page: 1, page_size: 50 }),
      queryFn: () => tasksApi.list({ status, page: 1, page_size: 50 }),
    })),
  })

  // 按 status 索引结果
  const tasksByStatus = useMemo(() => {
    const map: Record<TaskStatusValue, TaskSummary[]> = {
      pending: [],
      running: [],
      waiting_human: [],
      completed: [],
      failed: [],
      cancelled: [],
    }
    TASK_STATUSES.forEach((status, idx) => {
      const result = boardQueries[idx]
      map[status] = (result?.data?.items ?? []) as TaskSummary[]
    })
    return map
  }, [boardQueries])

  const boardLoading = boardQueries.some((q) => q.isLoading)

  /* ─── Trigger 列表查询（定时任务列） ─── */
  const { data: triggersData, isLoading: triggersLoading } = useQuery({
    queryKey: ['triggers-list'],
    queryFn: () => WorkflowTriggerAPI.listTriggers(),
    staleTime: 10_000,
  })
  const triggers = useMemo(() => triggersData?.items ?? [], [triggersData])

  /* ─── 收集 active 任务，批量拉详情（5s 轮询，上限 20 个） ─── */
  const activeTasksForDetail = useMemo(() => {
    const collected: TaskSummary[] = []
    for (const status of ACTIVE_STATUSES) {
      for (const task of tasksByStatus[status]) {
        if (collected.length >= MAX_PROGRESS_TASKS) break
        collected.push(task)
      }
      if (collected.length >= MAX_PROGRESS_TASKS) break
    }
    return collected
  }, [tasksByStatus])

  const activeDetailQueries = useQueries({
    queries: activeTasksForDetail.map((task) => ({
      queryKey: taskKeys.detail(task.id),
      queryFn: () => tasksApi.get(task.id),
      staleTime: 3_000,
    })),
  })

  /* ─── 组装进度 map ─── */
  const progressMap = useMemo(() => {
    const map: Record<string, NodeProgress | null | undefined> = {}
    activeTasksForDetail.forEach((task, idx) => {
      const detail = activeDetailQueries[idx]?.data
      if (detail) {
        map[task.id] = parseNodeProgress(detail.timeline)
      }
    })
    return map
  }, [activeTasksForDetail, activeDetailQueries])

  /* ─── Workflow list query (for create modal) ─── */
  const { data: workflowsData, isLoading: workflowsLoading } = useQuery({
    queryKey: [...taskKeys.workflows(), debouncedWorkflowSearch],
    queryFn: () => tasksApi.listWorkflows(debouncedWorkflowSearch || undefined),
    enabled: createOpen,
  })
  const workflows = workflowsData?.items ?? []

  /* ─── Workflow 名称映射（看板卡片用，始终启用） ─── */
  const { data: allWorkflowsData } = useQuery({
    queryKey: [...taskKeys.workflows(), 'board-map'],
    queryFn: () => tasksApi.listWorkflows(),
    staleTime: 60_000,
  })
  const workflowNameMap = useMemo(() => {
    const map: Record<string, string> = {}
    for (const wf of allWorkflowsData?.items ?? []) {
      map[wf.workflow_id] = wf.name
    }
    return map
  }, [allWorkflowsData])

  /* ─── Detail query（Drawer 使用，与卡片共用 detail 缓存） ─── */
  const { data: taskDetail, isLoading: detailLoading } = useQuery({
    queryKey: taskKeys.detail(detailTaskId ?? ''),
    queryFn: () => tasksApi.get(detailTaskId!),
    enabled: !!detailTaskId,
  })

  /* ─── Agent node timeline query（按需加载，点击查看详情时触发） ─── */
  const { data: nodeTimelineData, isLoading: nodeTimelineLoading, error: nodeTimelineError } = useQuery({
    queryKey: ['tasks', 'node-timeline', nodeDetail?.taskId, nodeDetail?.nodeId],
    queryFn: () => tasksApi.getNodeTimeline(nodeDetail!.taskId, nodeDetail!.nodeId),
    enabled: !!nodeDetail,
  })

  /* ─── Selected workflow schema ─── */
  const selectedWorkflow = workflows.find((w) => w._id === selectedWorkflowId)
  const inputSchema = selectedWorkflow?.input_schema ?? {}
  const schemaKeys = Object.keys(inputSchema as Record<string, unknown>)

  /* ─── Mutation: create task ─── */
  const createMutation = useMutation({
    mutationFn: (data: { workflow_id: string; input: Record<string, unknown> }) =>
      tasksApi.create(data),
    onSuccess: () => {
      message.success('任务创建成功')
      queryClient.invalidateQueries({ queryKey: taskKeys.lists() })
      setCreateOpen(false)
      setSelectedWorkflowId('')
      setCreateInput('')
    },
  })

  /* ─── Mutation: intervene (cancel / retry / approve / reject) ─── */
  const interveneMutation = useMutation({
    mutationFn: ({ taskId, action, version, comment, target_node_id, variables }: { taskId: string; action: string; version: number; comment?: CommentValue; target_node_id?: string; variables?: Record<string, unknown> }) =>
      tasksApi.intervene(taskId, { action, version, comment, target_node_id, variables }),
    onSuccess: (data) => {
      // 优先用后端返回的具体消息（如 rewind 的「已退回重跑」），无则兜底通用提示
      message.success(data?.message || '操作成功')
      queryClient.invalidateQueries({ queryKey: taskKeys.lists() })
      if (detailTaskId) {
        queryClient.invalidateQueries({ queryKey: taskKeys.detail(detailTaskId) })
      }
    },
  })

  /* ─── Mutation: delete task ─── */
  const deleteMutation = useMutation({
    mutationFn: tasksApi.remove,
    onSuccess: () => {
      message.success('任务已删除')
      queryClient.invalidateQueries({ queryKey: taskKeys.lists() })
      if (detailTaskId) {
        queryClient.invalidateQueries({ queryKey: taskKeys.detail(detailTaskId) })
      }
    },
  })

  /* ─── Query: load trigger config when edit modal is open ─── */
  const { data: editTriggerConfig, isLoading: editTriggerLoading } = useQuery({
    queryKey: ['trigger-detail', editTask?.trigger_id ?? ''],
    queryFn: () => WorkflowTriggerAPI.getTriggerById(editTask!.trigger_id!),
    enabled: !!editTask?.trigger_id,
  })

  /* ─── Query: load workflow nodes for input parameter form ─── */
  const { data: editWorkflowDetail } = useQuery({
    queryKey: ['workflow-detail-for-edit', editTask?.workflow_id ?? ''],
    queryFn: () => workflowsApi.get(editTask!.workflow_id),
    enabled: !!editTask?.workflow_id,
  })

  // rewind Modal 打开时拉工作流模板，用于把 completed_nodes 的 node_id 映射成节点名
  const rewindWorkflowQuery = useQuery({
    queryKey: ['workflow-detail-for-rewind', rewindModalOpen && taskDetail ? taskDetail.workflow_id : ''],
    queryFn: () => workflowsApi.get(taskDetail!.workflow_id),
    enabled: rewindModalOpen && !!taskDetail?.workflow_id,
    staleTime: 60_000,
  })
  // node_id → { label, type } 映射，供 Select 显示节点名
  const rewindNodeMap = useMemo(() => {
    const nodes = rewindWorkflowQuery.data?.nodes ?? []
    const m: Record<string, { label: string; type: string }> = {}
    for (const n of nodes) {
      m[n.node_id] = { label: n.label || n.node_id, type: n.type }
    }
    return m
  }, [rewindWorkflowQuery.data])

  /* ─── Derive start node variables from workflow ─── */
  const editStartNodeVars = useMemo<VariableDefinition[]>(() => {
    const startNode = editWorkflowDetail?.nodes?.find((n) => n.type === 'start')
    return (startNode?.config?.output_variables as VariableDefinition[]) ?? []
  }, [editWorkflowDetail])

  /* ─── Sync form state when trigger config loads ─── */
  /* eslint-disable react-hooks/set-state-in-effect -- form initialization from loaded data */
  useEffect(() => {
    if (editTriggerConfig) {
      setEditEnabled(editTriggerConfig.enabled)
      setEditTriggerType(editTriggerConfig.type)
      setEditCronExpression(editTriggerConfig.cron_expression ?? '0 9 * * *')
      setEditScheduledAt(editTriggerConfig.execute_at ?? '')
      setEditDefaultInput(editTriggerConfig.default_input ?? {})
      setEditDirty(false)
    }
  }, [editTriggerConfig])
  /* eslint-enable react-hooks/set-state-in-effect */

  /* ─── Mutation: update scheduled task config ─── */
  const editScheduleMutation = useMutation({
    mutationFn: ({ triggerId, config }: { triggerId: string; config: Partial<TriggerConfig> }) =>
      WorkflowTriggerAPI.updateTriggerById(triggerId, config),
    onSuccess: () => {
      message.success('定时任务配置已更新')
      queryClient.invalidateQueries({ queryKey: ['triggers-list'] })
      queryClient.invalidateQueries({ queryKey: ['trigger-detail'] })
      setEditTask(null)
      setEditDirty(false)
    },
  })

  /* ─── Mutation: delete trigger (取消定时任务) ─── */
  const deleteTriggerMutation = useMutation({
    mutationFn: (triggerId: string) => WorkflowTriggerAPI.deleteTriggerById(triggerId),
    onSuccess: () => {
      message.success('定时任务已删除')
      queryClient.invalidateQueries({ queryKey: ['triggers-list'] })
    },
  })

  /* ─── Actions ─── */
  const handleCreate = () => {
    setSelectedWorkflowId('')
    setCreateInput('{}')
    setWorkflowSearch('')
    setCreateOpen(true)
  }

  const handleCreateSubmit = () => {
    if (!selectedWorkflowId) {
      message.warning('请选择工作流')
      return
    }
    let parsedInput: Record<string, unknown>
    try {
      parsedInput = JSON.parse(createInput || '{}')
    } catch {
      message.warning('请输入有效的 JSON 格式输入参数')
      return
    }
    createMutation.mutate({ workflow_id: selectedWorkflowId, input: parsedInput })
  }

  const handleCancel = useCallback((task: TaskSummary) => {
    Modal.confirm({
      title: '确认取消',
      content: `确定要取消任务「${task.id}」吗？`,
      okText: '取消任务',
      okButtonProps: { danger: true },
      cancelText: '关闭',
      onOk: () => interveneMutation.mutate({ taskId: task.id, action: 'cancel', version: task.version }),
    })
  }, [interveneMutation])

  /* ─── 取消定时任务（删 trigger） ─── */
  const handleDeleteTrigger = useCallback((trigger: TriggerConfig) => {
    const triggerId = trigger._id || trigger.id || ''
    Modal.confirm({
      title: '确认删除定时任务',
      content: trigger.type === 'cron'
        ? `确定要删除定时任务（${trigger.cron_expression}）吗？删除后将停止定时执行。`
        : `确定要删除定时任务吗？删除后将停止定时执行。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteTriggerMutation.mutate(triggerId),
    })
  }, [deleteTriggerMutation])

  /* ─── 编辑定时任务（从 trigger 列打开编辑 modal） ─── */
  const handleEditTrigger = useCallback((trigger: TriggerConfig) => {
    const triggerId = trigger._id || trigger.id || ''
    setEditTask({
      id: triggerId,
      workflow_id: trigger.workflow_id,
      status: 'pending' as TaskStatusValue,
      source: 'trigger',
      trigger_id: triggerId,
      version: 1,
    } as TaskSummary)
  }, [])

  const handleRetry = useCallback((task: TaskSummary) => {
    Modal.confirm({
      title: '确认重试',
      content: `确定要重试任务「${task.id}」吗？`,
      okText: '重试',
      cancelText: '关闭',
      onOk: () => interveneMutation.mutate({ taskId: task.id, action: 'retry', version: task.version }),
    })
  }, [interveneMutation])

  const handleResume = useCallback((task: TaskSummary | TaskDetail) => {
    Modal.confirm({
      title: '确认恢复',
      content: `确定要恢复任务「${task.id}」吗？任务将从上次中断的位置继续执行。`,
      okText: '恢复',
      cancelText: '关闭',
      onOk: () => interveneMutation.mutate({ taskId: task.id, action: 'resume', version: task.version }),
    })
  }, [interveneMutation])

  const handleApprove = useCallback((task: TaskSummary | TaskDetail) => {
    setApprovalAction('approve')
    setApprovalTask(task)
    setApprovalComment('')
    setApprovalCommentMode('text')
  }, [])

  const handleReject = useCallback((task: TaskSummary | TaskDetail) => {
    setApprovalAction('reject')
    setApprovalTask(task)
    setApprovalComment('')
    setApprovalCommentMode('text')
  }, [])

  // 把当前 comment 输入按模式组装为 CommentValue，返回 null 表示解析失败需阻断
  const buildComment = useCallback((): CommentValue | null => {
    if (approvalCommentMode === 'json') {
      const trimmed = approvalComment.trim()
      if (!trimmed) return { type: 'json', value: '' }
      try {
        return { type: 'json', value: JSON.parse(trimmed) }
      } catch {
        message.error('JSON 格式错误，请检查输入')
        return null
      }
    }
    return { type: 'text', value: approvalComment }
  }, [approvalCommentMode, approvalComment])

  const handleApprovalConfirm = useCallback(() => {
    if (!approvalAction || !approvalTask) return
    const comment = buildComment()
    if (comment === null) return // JSON 解析失败，已提示，阻断提交
    interveneMutation.mutate({
      taskId: approvalTask.id,
      action: approvalAction,
      version: approvalTask.version,
      comment,
    })
    setApprovalAction(null)
    setApprovalTask(null)
    setApprovalComment('')
    setApprovalCommentMode('text')
  }, [approvalAction, approvalTask, buildComment, interveneMutation])

  const handleApprovalCancel = useCallback(() => {
    setApprovalAction(null)
    setApprovalTask(null)
    setApprovalComment('')
    setApprovalCommentMode('text')
  }, [])

  /* ─── Rewind handlers ─── */
  const openRewindModal = useCallback(() => {
    setRewindTargetNode('')
    setRewindVarsMode('none')
    // 预填当前 variable_snapshot 作为 JSON 编辑起点
    const snapshot = taskDetail?.checkpoint?.variable_snapshot
    setRewindVarsText(snapshot ? JSON.stringify(snapshot, null, 2) : '{}')
    setRewindModalOpen(true)
  }, [taskDetail])

  const handleRewind = useCallback(() => {
    if (!taskDetail) return
    if (!rewindTargetNode) {
      message.warning('请选择退回节点')
      return
    }
    let variables: Record<string, unknown> | undefined
    if (rewindVarsMode === 'json') {
      try {
        variables = JSON.parse(rewindVarsText)
      } catch {
        message.error('JSON 格式错误，请检查变量输入')
        return
      }
    }
    interveneMutation.mutate({
      taskId: taskDetail.id,
      action: 'rewind',
      target_node_id: rewindTargetNode,
      variables,
      version: taskDetail.version,
    })
    setRewindModalOpen(false)
  }, [taskDetail, rewindTargetNode, rewindVarsMode, rewindVarsText, interveneMutation])

  const handleDelete = useCallback((task: TaskSummary) => {
    Modal.confirm({
      title: '确认删除',
      content: `确定要删除任务「${task.id}」吗？此操作不可恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteMutation.mutate(task.id),
    })
  }, [deleteMutation])

  const handleEdit = useCallback((task: TaskSummary) => {
    setEditTask(task)
    // Form state will be initialized by the useQuery + useMemo sync below
  }, [])

  // 看板卡片上的快捷审批（通过/驳回 + 评论）
  const handleCardApproval = useCallback((task: TaskSummary, action: 'approve' | 'reject', comment: CommentValue) => {
    interveneMutation.mutate({
      taskId: task.id,
      action,
      version: task.version,
      comment,
    })
  }, [interveneMutation])

  const handleCardClick = useCallback((task: TaskSummary) => {
    setDetailTaskId(task.id)
  }, [])

  /* ─── 顶部紧凑统计卡（4 张） ─── */
  const scheduledCount = triggers.length
  const runningCount = tasksByStatus.running.length
  const waitingHumanCount = tasksByStatus.waiting_human.length
  const failedCount = tasksByStatus.failed.length

  const statCards = [
    { label: '定时任务', value: scheduledCount, color: '#F59E0B', bg: '#FEF3C7', icon: <ClockCircleOutlined /> },
    { label: '运行中', value: runningCount, color: '#2563EB', bg: '#DBEAFE', icon: <ThunderboltOutlined /> },
    { label: '等待人工', value: waitingHumanCount, color: '#8B5CF6', bg: '#EDE9FE', icon: <WarningOutlined /> },
    { label: '已失败', value: failedCount, color: '#EF4444', bg: '#FEE2E2', icon: <CloseCircleOutlined /> },
  ]

  /* ─── 过滤函数（前端 includes 匹配） ─── */
  const filterTasks = useCallback((tasks: TaskSummary[]) => {
    if (!debouncedSearch) return tasks
    return tasks.filter(
      (task) =>
        task.id.toLowerCase().includes(debouncedSearch) ||
        task.workflow_id.toLowerCase().includes(debouncedSearch),
    )
  }, [debouncedSearch])

  return (
    <div className="flex flex-col h-full">
      {/* ─── 顶部统计条 ─── */}
      <div className="grid grid-cols-4 gap-3 mb-4">
        {statCards.map((s) => (
          <div
            key={s.label}
            className="bg-white rounded-xl border border-gray-200 p-3 flex items-center gap-3"
          >
            <div
              className="w-9 h-9 rounded-lg flex items-center justify-center text-base"
              style={{ background: s.bg, color: s.color }}
            >
              {s.icon}
            </div>
            <div className="min-w-0">
              <div className="text-xl font-semibold text-[#0F172A] leading-tight">{s.value}</div>
              <div className="text-[11px] text-[#64748B]">{s.label}</div>
            </div>
          </div>
        ))}
      </div>

      {/* ─── 工具栏：搜索 + 新建 ─── */}
      <div className="flex items-center justify-between gap-3 mb-4">
        <div className="relative">
          <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-[#94A3B8] text-sm" />
          <input
            type="text"
            placeholder="搜索任务 ID 或工作流 ID..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            className="pl-9 pr-4 py-2 rounded-lg border border-gray-200 text-sm focus:outline-none focus:ring-2 w-80"
            style={{ ['--tw-ring-color' as string]: t.bg }}
          />
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={handleCreate}
        >
          新建任务
        </Button>
      </div>

      {/* ─── 看板（定时任务列 + 5 列任务状态） ─── */}
      {boardLoading ? (
        <div className="flex items-center justify-center py-20">
          <Spin size="large" />
        </div>
      ) : (
        <div className="flex gap-4 overflow-x-auto pb-2 flex-1 min-h-0" style={{ height: 'calc(100vh - 220px)' }}>
          {/* ─── 定时任务列（展示 trigger） ─── */}
          <div className="flex flex-col min-w-[280px] w-[280px] flex-shrink-0">
            <div
              className="rounded-t-xl px-3 py-2 flex items-center justify-between"
              style={{ background: '#FEF3C7' }}
            >
              <span className="text-sm font-medium" style={{ color: '#F59E0B' }}>
                {'🕒 定时任务'}
              </span>
              <span className="text-xs text-[#94A3B8]">{triggers.length}</span>
            </div>
            <div
              className="flex-1 overflow-y-auto rounded-b-xl border-x border-b bg-gray-50/50 p-2 space-y-2"
              style={{ minHeight: 0 }}
            >
              {triggersLoading ? (
                <div className="flex justify-center py-8"><Spin /></div>
              ) : triggers.length === 0 ? (
                <div className="text-center py-8 text-xs text-[#94A3B8]">暂无定时任务</div>
              ) : (
                triggers.map((trigger) => {
                  const triggerId = trigger._id || trigger.id || ''
                  const wfName = workflowNameMap[trigger.workflow_id] || trigger.workflow_id
                  return (
                    <div
                      key={triggerId}
                      className="bg-white rounded-lg border border-gray-200 p-3 hover:shadow-md transition-shadow cursor-pointer group"
                      style={{ borderLeft: `3px solid ${trigger.enabled ? '#F59E0B' : '#CBD5E1'}` }}
                    >
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-medium text-[#0F172A] truncate">{wfName}</span>
                        <Tag
                          color={trigger.enabled ? 'success' : 'default'}
                          className="!m-0 !text-[10px] !px-1.5 !leading-4"
                        >
                          {trigger.enabled ? '启用' : '停用'}
                        </Tag>
                      </div>
                      <div className="text-[11px] text-[#64748B] mb-2">
                        {trigger.type === 'cron'
                          ? `Cron: ${trigger.cron_expression}`
                          : `定时: ${trigger.execute_at ? formatDateTime(trigger.execute_at) : '-'}`}
                      </div>
                      {trigger.next_trigger_at && (
                        <div className="text-[11px] text-[#F59E0B] mb-2">
                          下次执行: {formatDateTime(trigger.next_trigger_at)}
                        </div>
                      )}
                      <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <Button
                          size="small"
                          type="text"
                          icon={<EditOutlined />}
                          onClick={() => handleEditTrigger(trigger)}
                          className="!text-[#64748B]"
                        />
                        <Button
                          size="small"
                          type="text"
                          icon={<DeleteOutlined />}
                          onClick={() => handleDeleteTrigger(trigger)}
                          className="!text-[#EF4444]"
                          loading={deleteTriggerMutation.isPending}
                        />
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </div>

          {/* ─── 5 列任务状态（非 pending） ─── */}
          {TASK_STATUSES.map((status) => {
            const style = TASK_STATUS_STYLES[status]
            const filtered = filterTasks(tasksByStatus[status])
            return (
              <TaskBoardColumn
                key={status}
                style={style}
                tasks={filtered}
                progressMap={progressMap}
                workflowNameMap={workflowNameMap}
                loading={false}
                onCardClick={handleCardClick}
                onCancel={handleCancel}
                onRetry={handleRetry}
                onDelete={handleDelete}
                onEdit={undefined}
                onApprovalSubmit={handleCardApproval}
                interveneLoading={interveneMutation.isPending}
                deleteLoading={deleteMutation.isPending}
              />
            )
          })}
        </div>
      )}

      {/* ─── Create Task Modal ─── */}
      <Modal
        title="新建任务"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={handleCreateSubmit}
        okText="创建"
        cancelText="取消"
        confirmLoading={createMutation.isPending}
        okButtonProps={{ disabled: !selectedWorkflowId }}
        destroyOnClose
        width={560}
      >
        <div className="flex flex-col gap-4 py-2">
          {/* Workflow selector */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              选择工作流 <span className="text-[#EF4444]">*</span>
            </label>
            <Input
              placeholder="搜索工作流..."
              value={workflowSearch}
              onChange={(e) => setWorkflowSearch(e.target.value)}
              className="mb-2"
              prefix={<SearchOutlined className="text-[#94A3B8]" />}
              allowClear
            />
            <div className="max-h-48 overflow-y-auto border border-gray-200 rounded-lg">
              {workflowsLoading ? (
                <div className="flex items-center justify-center py-8"><Spin size="small" /></div>
              ) : workflows.length === 0 ? (
                <div className="text-center py-8 text-sm text-[#94A3B8]">暂无已发布的工作流</div>
              ) : (
                workflows.map((wf) => (
                  <div
                    key={wf._id}
                    onClick={() => setSelectedWorkflowId(wf._id)}
                    className={`px-3 py-2.5 cursor-pointer border-b border-gray-50 last:border-b-0 transition-colors ${
                      selectedWorkflowId === wf._id ? 'bg-[#EFF6FF]' : 'hover:bg-[#F8FAFC]'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className={`text-sm ${selectedWorkflowId === wf._id ? 'font-medium text-[#1E5EFF]' : 'text-[#0F172A]'}`}>
                        {wf.name}
                      </span>
                      {wf.has_human_node && (
                        <Tag className="!m-0 !text-[10px] !px-1.5 !py-0" color="orange">需审批</Tag>
                      )}
                    </div>
                    <div className="text-[11px] text-[#94A3B8] mt-0.5 truncate">{wf.description}</div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Input schema fields */}
          {selectedWorkflow && schemaKeys.length > 0 && (
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">
                输入参数 <span className="text-[#94A3B8] font-normal">(JSON)</span>
              </label>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {schemaKeys.map((key) => {
                  const schema = (inputSchema as Record<string, unknown>)[key] as Record<string, unknown> | undefined
                  return (
                    <Tag key={key} className="!m-0 !text-[11px]" color="default">
                      {key}{schema?.required ? <span className="text-[#EF4444] ml-0.5">*</span> : null}
                      <span className="text-[#94A3B8] ml-1">{(schema?.type as string) ?? 'string'}</span>
                    </Tag>
                  )
                })}
              </div>
              <Input.TextArea
                value={createInput}
                onChange={(e) => setCreateInput(e.target.value)}
                placeholder='{"key": "value"}'
                rows={4}
                className="font-mono text-sm"
              />
            </div>
          )}

          {selectedWorkflow && schemaKeys.length === 0 && (
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">输入参数</label>
              <Input.TextArea
                value={createInput}
                onChange={(e) => setCreateInput(e.target.value)}
                placeholder='{"key": "value"}（可选）'
                rows={3}
                className="font-mono text-sm"
              />
            </div>
          )}
        </div>
      </Modal>

      {/* ─── Task Detail Drawer ─── */}
      <Drawer
        title={detailLoading ? '加载中...' : `任务详情`}
        open={!!detailTaskId}
        onClose={() => setDetailTaskId(null)}
        width={520}
        extra={
          taskDetail && (
            <div className="flex items-center gap-2">
              {taskDetail.source === 'trigger' && (
                <Tag className="!m-0" icon={<ClockCircleOutlined />} color="processing">定时任务</Tag>
              )}
              <Tag className="!m-0" style={{ color: TASK_STATUS_STYLES[taskDetail.status]?.color, background: TASK_STATUS_STYLES[taskDetail.status]?.bg, borderColor: 'transparent' }}>
                {TASK_STATUS_STYLES[taskDetail.status]?.icon} {TASK_STATUS_STYLES[taskDetail.status]?.label}
              </Tag>
            </div>
          )
        }
      >
        {detailLoading && (
          <div className="flex items-center justify-center py-20"><Spin size="large" /></div>
        )}

        {!detailLoading && taskDetail && (
          <div className="flex flex-col gap-5">
            {/* Basic info */}
            <section>
              <h4 className="text-xs font-medium text-[#94A3B8] uppercase tracking-wider mb-3">基本信息</h4>
              <div className="space-y-2.5">
                <InfoRow label="ID" value={taskDetail.id} mono />
                <InfoRow label="工作流" value={workflowNameMap[taskDetail.workflow_id] ?? taskDetail.workflow_id} />
                <InfoRow label="来源" value={taskDetail.source === 'trigger' ? '定时触发' : '手动创建'} />
                <InfoRow label="创建者" value={taskDetail.created_by || '系统'} />
                <InfoRow label="版本" value={`v${taskDetail.version}`} />
                <InfoRow label="创建时间" value={parseBackendDate(taskDetail.created_at).toLocaleString('zh-CN')} />
                <InfoRow label="更新时间" value={parseBackendDate(taskDetail.updated_at).toLocaleString('zh-CN')} />
                {taskDetail.source === 'trigger' && taskDetail.scheduled_at && (
                  <InfoRow label="计划执行" value={parseBackendDate(taskDetail.scheduled_at).toLocaleString('zh-CN')} />
                )}
              </div>
            </section>

            {/* 输入参数 + 错误信息 */}
            <section>
              <h4 className="text-xs font-medium text-[#94A3B8] uppercase tracking-wider mb-3">输入参数</h4>
              <div className="space-y-1.5">
                {(() => {
                  const inp = taskDetail.input ?? {}
                  const entries = Object.entries(inp)
                  if (entries.length === 0) return <div className="text-xs text-[#94A3B8] italic">无</div>
                  return entries.map(([k, v]) => (
                    <div key={k} className="flex gap-2 text-xs">
                      <span className="text-[#64748B] font-medium shrink-0 min-w-[80px]">{k}</span>
                      <span className="text-[#0F172A] break-all">
                        {typeof v === 'string' ? v : JSON.stringify(v)}
                      </span>
                    </div>
                  ))
                })()}
              </div>
              {taskDetail.error && (
                <Alert
                  type="error"
                  showIcon
                  message="执行错误"
                  description={`[${taskDetail.error.error_code}] ${taskDetail.error.error_message}`}
                  className="mt-3"
                />
              )}
            </section>

            {/* 执行流程 */}
            <section>
              <h4 className="text-xs font-medium text-[#94A3B8] uppercase tracking-wider mb-3">
                执行流程 <span className="font-normal">({taskDetail.timeline?.length ?? 0})</span>
              </h4>
              {taskDetail.timeline && taskDetail.timeline.length > 0 ? (
                (() => {
                  const timeline = taskDetail.timeline!
                  // 过滤：如果时间线中存在任何审批完成事件，说明所有 waiting_human 都已解决，全部隐藏
                  const approveTypes = new Set([
                    'approve', 'skip', 'reject', 'cancel', 'resume',  // UI API (tasks.py)
                    'intervene_approve', 'intervene_reject', 'intervene_cancel', 'intervene_resume',  // Agent 工具
                    'human_approved', 'human_rejected',  // 引擎内部
                  ])
                  const hasAnyApproval = timeline.some(e => approveTypes.has(e.event_type))
                  const filtered = timeline.filter(e => {
                    // 隐藏 node_start：data 仅 node_id/node_type（已拼进标签），折叠块看起来空
                    if (e.event_type === 'node_start') return false
                    // 审批已完成 → 隐藏所有 waiting_human
                    if (hasAnyApproval && e.event_type === 'waiting_human') return false
                    // 人工审批节点的 node_complete 只是引擎恢复信号，审批事件已覆盖
                    if (e.event_type === 'node_complete' && e.data?.node_type === 'human') return false
                    return true
                  })

                  // 节点类型 → 中文标签
                  const nodeTypeLabel: Record<string, string> = {
                    start: '输入节点', end: '输出节点', agent: 'Agent 节点',
                    tool: '工具节点', gateway: '网关节点', parallel: '并行节点', human: '人工审批节点',
                  }
                  // 事件类型 → 中文标签 + 颜色
                  const eventMeta: Record<string, { label: string; color: string }> = {
                    // ── 生命周期 ──
                    created:           { label: '任务创建',   color: '#1E5EFF' },
                    started:           { label: '开始执行',   color: '#1E5EFF' },
                    auto_scheduled:    { label: '自动调度',   color: '#1E5EFF' },
                    completed:         { label: '任务完成',   color: '#10B981' },
                    failed:            { label: '任务失败',   color: '#EF4444' },
                    task_failed:       { label: '任务失败',   color: '#EF4444' },
                    cancelled:         { label: '任务取消',   color: '#94A3B8' },
                    // ── 节点执行 ──
                    node_start:        { label: '节点开始',   color: '#1E5EFF' },
                    node_complete:     { label: '节点完成',   color: '#10B981' },
                    node_failed:       { label: '节点失败',   color: '#EF4444' },
                    // ── 人工审批（UI API） ──
                    waiting_human:     { label: '等待审批',   color: '#8B5CF6' },
                    human_node_start:  { label: '审批开始',   color: '#8B5CF6' },
                    approve:           { label: '审批通过',   color: '#10B981' },
                    skip:              { label: '审批跳过',   color: '#F59E0B' },
                    reject:            { label: '审批驳回',   color: '#EF4444' },
                    cancel:            { label: '人工取消',   color: '#94A3B8' },
                    resume:            { label: '人工恢复',   color: '#1E5EFF' },
                    // ── 人工干预（Agent 工具） ──
                    human_approved:    { label: '审批通过',   color: '#10B981' },
                    human_rejected:    { label: '审批拒绝',   color: '#EF4444' },
                    intervene_approve: { label: '人工通过',   color: '#10B981' },
                    intervene_reject:  { label: '人工拒绝',   color: '#EF4444' },
                    intervene_cancel:  { label: '人工取消',   color: '#EF4444' },
                    intervene_resume:  { label: '人工恢复',   color: '#1E5EFF' },
                    intervene_retry:    { label: '人工重试',   color: '#F59E0B' },
                    rewoun:            { label: '已退回重跑', color: '#F59E0B' },
                  }

                  // 废弃判定：一条 node_complete/node_failed，若其 node_id 出现在其后
                  // 某个 rewoun 的 rewound_nodes 里，说明它是被退回重跑掉的旧轮 → 标记废弃。
                  // 旧轮记录的行内 output_summary 是真快照（保留），但其「查看执行详情」按钮
                  // 取的 checkpointer thread 已被 rewind 清掉覆盖（新轮内容，误导），故禁用。
                  const supersededIdx = new Set<number>()
                  for (let i = 0; i < filtered.length; i++) {
                    const evt = filtered[i]
                    if (evt.event_type !== 'node_complete' && evt.event_type !== 'node_failed') continue
                    const nodeId = evt.data?.node_id as string | undefined
                    if (!nodeId) continue
                    for (let j = i + 1; j < filtered.length; j++) {
                      const later = filtered[j]
                      if (later.event_type === 'rewoun') {
                        const rewound = (later.data?.rewound_nodes as string[] | undefined) ?? []
                        if (rewound.includes(nodeId)) {
                          supersededIdx.add(i)
                          break
                        }
                      }
                    }
                  }

                  return (
                    <div className="relative pl-7">
                      {/* 连续连接线 */}
                      <div className="absolute left-[9px] top-2 bottom-2 w-[2px] bg-[#E2E8F0]" />
                      {filtered.map((evt, idx) => {
                        const superseded = supersededIdx.has(idx)
                        const meta = eventMeta[evt.event_type] ?? { label: evt.event_type, color: '#94A3B8' }

                        // 节点事件：拼接类型前缀 → "Agent 节点开始" / "输入节点完成"
                        const nodeType = evt.data?.node_type as string | undefined
                        const isNodeEvent = !!nodeType && (
                          evt.event_type === 'node_start' ||
                          evt.event_type === 'node_complete' ||
                          evt.event_type === 'node_failed'
                        )
                        const displayLabel = isNodeEvent
                          ? `${nodeTypeLabel[nodeType] ?? nodeType}${meta.label.replace('节点', '')}`
                          : meta.label

                        const hasData = Object.keys(evt.data ?? {}).length > 0

                        return (
                          <div key={idx} className={`relative py-2${superseded ? ' opacity-50' : ''}`}>
                            {/* 时间轴圆点 */}
                            <div
                              className="absolute -left-7 top-2.5 w-[14px] h-[14px] rounded-full border-2 border-white shadow-sm z-10"
                              style={{ backgroundColor: meta.color }}
                            />
                            {/* 事件主体 */}
                            <div className="flex items-baseline gap-2 flex-wrap">
                              <span className="text-xs font-medium" style={{ color: meta.color }}>{displayLabel}</span>
                              {superseded && <span className="text-[10px] text-[#94A3B8] bg-[#F1F5F9] rounded px-1 py-0.5">已废弃</span>}
                              <span className="text-[11px] text-[#94A3B8]">{formatDateTime(evt.timestamp)}</span>
                              {evt.actor && <span className="text-[11px] text-[#94A3B8]">· {evt.actor}</span>}
                              {/* Agent 节点完成后可查看执行详情 */}
                              {isNodeEvent && nodeType === 'agent' &&
                                (evt.event_type === 'node_complete' || evt.event_type === 'node_failed') && (
                                superseded ? (
                                  <Tooltip title="该记录已被退回重跑覆盖，详情不可用">
                                    <span className="text-[10px] text-[#94A3B8] cursor-not-allowed ml-1">查看执行详情</span>
                                  </Tooltip>
                                ) : (
                                  <button
                                    onClick={() => setNodeDetail({
                                      taskId: taskDetail.id,
                                      nodeId: evt.data?.node_id as string,
                                    })}
                                    className="text-[10px] text-[#1E5EFF] hover:underline border-0 bg-transparent cursor-pointer p-0 ml-1"
                                  >
                                    查看执行详情
                                  </button>
                                )
                              )}
                            </div>
                            {/* 所有数据统一折叠 */}
                            {hasData && (
                              <details className="mt-1">
                                <summary className="text-[10px] text-[#94A3B8] cursor-pointer hover:text-[#1E5EFF] list-none flex items-center gap-1">
                                  <span className="text-[8px]">▶</span>
                                  详细数据
                                </summary>
                                <pre className="text-[10px] text-[#64748B] bg-[#F8FAFC] rounded p-2 mt-1 overflow-x-auto max-h-32">
                                  {JSON.stringify(evt.data, null, 2)}
                                </pre>
                              </details>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )
                })()
              ) : (
                <Empty description="暂无时间线事件" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              )}
            </section>

            {/* Checkpoint / Human approval info（waiting_human 时显示） */}
            {taskDetail.status === 'waiting_human' && taskDetail.checkpoint && (
              <section>
                <h4 className="text-xs font-medium text-[#94A3B8] uppercase tracking-wider mb-3">
                  <WarningOutlined className="mr-1" style={{ color: '#8B5CF6' }} />
                  审批信息
                </h4>
                <div className="space-y-2.5">
                  {taskDetail.checkpoint.human_context?.title && (
                    <InfoRow label="审批标题" value={taskDetail.checkpoint.human_context.title} />
                  )}
                  {taskDetail.checkpoint.human_context?.description && (
                    <div>
                      <div className="text-xs text-[#64748B] mb-1">审批描述</div>
                      <div className="text-xs text-[#0F172A] bg-[#F8FAFC] rounded-lg p-3">
                        {taskDetail.checkpoint.human_context.description}
                      </div>
                    </div>
                  )}
                  {taskDetail.checkpoint.timeout_deadline && (
                    <InfoRow
                      label="超时截止"
                      value={`${parseBackendDate(taskDetail.checkpoint.timeout_deadline).toLocaleString('zh-CN')} (${taskDetail.checkpoint.timeout_action})`}
                    />
                  )}
                  {/* Upstream node outputs (exclude 'input' and the paused node itself) */}
                  {(() => {
                    const pausedNode = taskDetail.checkpoint.paused_at_node
                    const upstreamVars = Object.entries(taskDetail.variables ?? {})
                      .filter(([key]) => key !== 'input' && key !== pausedNode)
                    if (upstreamVars.length === 0) return null
                    return (
                      <div>
                        <div className="text-xs text-[#64748B] mb-1">上游节点输出</div>
                        <div className="max-h-48 overflow-y-auto space-y-2">
                          {upstreamVars.map(([key, value]) => (
                            <div key={key}>
                              <div className="text-[11px] text-[#94A3B8] font-medium">{key}</div>
                              <pre className="text-[10px] bg-[#F8FAFC] rounded p-2 overflow-x-auto text-[#0F172A]">
                                {JSON.stringify(value, null, 2)}
                              </pre>
                            </div>
                          ))}
                        </div>
                      </div>
                    )
                  })()}
                </div>
              </section>
            )}

            {/* 输出文件（completed 时展示） */}
            {taskDetail.status === 'completed' && (
              <TaskOutputFiles taskId={taskDetail.id} />
            )}

            {/* Intervention actions（waiting_human 显示批准/驳回） */}
            <section>
              <h4 className="text-xs font-medium text-[#94A3B8] uppercase tracking-wider mb-3">操作</h4>
              <div className="flex items-center gap-2 flex-wrap">
                {/* 待执行定时任务：编辑配置 + 取消 */}
                {taskDetail.status === 'pending' && taskDetail.source === 'trigger' && (
                  <Button
                    icon={<EditOutlined />}
                    onClick={() => handleEdit(taskDetail)}
                  >
                    编辑配置
                  </Button>
                )}
                {/* 所有待执行任务都可以取消 */}
                {taskDetail.status === 'pending' && (
                  <Button
                    danger
                    icon={<StopOutlined />}
                    onClick={() => handleCancel(taskDetail)}
                    loading={interveneMutation.isPending}
                  >
                    取消任务
                  </Button>
                )}
                {taskDetail.status === 'running' && (
                  <Button
                    danger
                    icon={<StopOutlined />}
                    onClick={() => handleCancel(taskDetail)}
                    loading={interveneMutation.isPending}
                  >
                    取消任务
                  </Button>
                )}
                {taskDetail.status === 'waiting_human' && (() => {
                  const humanOptions: string[] = Array.isArray(taskDetail.checkpoint?.human_context?.options)
                    ? taskDetail.checkpoint.human_context.options.filter(Boolean)
                    : []

                  return (
                    <>
                      {humanOptions.length === 0 ? (
                        // 无选项 → 通用"继续"按钮
                        <Button
                          type="primary"
                          icon={<CheckOutlined />}
                          onClick={() => interveneMutation.mutate({
                            taskId: taskDetail.id,
                            action: 'resume',
                            version: taskDetail.version,
                            comment: '',
                          })}
                          loading={interveneMutation.isPending}
                        >
                          继续
                        </Button>
                      ) : (
                        // 有选项 → 批准/驳回
                        <>
                          <Button
                            type="primary"
                            icon={<CheckOutlined />}
                            onClick={() => handleApprove(taskDetail)}
                            loading={interveneMutation.isPending}
                            style={{ backgroundColor: '#8B5CF6', borderColor: '#8B5CF6' }}
                          >
                            批准
                          </Button>
                          <Button
                            danger
                            icon={<CloseCircleOutlined />}
                            onClick={() => handleReject(taskDetail)}
                            loading={interveneMutation.isPending}
                          >
                            驳回
                          </Button>
                        </>
                      )}
                      {/* 退回重跑：无论有无 human options 都可用 */}
                      <Button
                        icon={<RollbackOutlined />}
                        onClick={openRewindModal}
                        loading={interveneMutation.isPending}
                      >
                        退回重跑
                      </Button>
                    </>
                  )
                })()}
                {taskDetail.status === 'failed' && (
                  <Button
                    type="primary"
                    icon={<RedoOutlined />}
                    onClick={() => handleRetry(taskDetail)}
                    loading={interveneMutation.isPending}
                  >
                    重试
                  </Button>
                )}
                {taskDetail.status === 'cancelled' && (
                  <Button
                    type="primary"
                    icon={<CaretRightOutlined />}
                    onClick={() => handleResume(taskDetail)}
                    loading={interveneMutation.isPending}
                  >
                    恢复
                  </Button>
                )}
                {(taskDetail.status === 'completed' || taskDetail.status === 'failed' || taskDetail.status === 'cancelled') && (
                  <Button
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => handleDelete(taskDetail)}
                    loading={deleteMutation.isPending}
                  >
                    删除
                  </Button>
                )}
              </div>
            </section>
          </div>
        )}
      </Drawer>

      {/* ─── Approval Modal ─── */}
      <Modal
        title={
          approvalAction === 'approve'
            ? '批准任务'
            : approvalAction === 'reject'
              ? '驳回任务'
              : '审批操作'
        }
        open={!!approvalAction}
        onOk={handleApprovalConfirm}
        onCancel={handleApprovalCancel}
        okText={approvalAction === 'approve' ? '批准' : '驳回'}
        cancelText="取消"
        confirmLoading={interveneMutation.isPending}
        okButtonProps={approvalAction === 'reject' ? { danger: true } : {}}
        destroyOnClose
      >
        <div className="flex flex-col gap-3 py-2">
          <p className="text-sm text-[#0F172A]">
            {approvalAction === 'approve'
              ? `确定批准任务「${approvalTask?.id}」并继续执行吗？`
              : `确定驳回任务「${approvalTask?.id}」吗？任务将被标记为失败。`}
          </p>
          {/* 审核信息：与通过/驳回同界面，审批人据此决策 */}
          {(() => {
            const ctx = approvalTask?.checkpoint?.human_context
            if (!ctx) return null
            return (
              <div className="border border-line rounded-lg p-3 bg-[#F8FAFC]">
                {ctx.title && (
                  <div className="text-sm font-medium text-[#0F172A] mb-1.5">{ctx.title}</div>
                )}
                {ctx.description && (
                  <div className="text-xs text-[#475569] whitespace-pre-wrap break-words leading-relaxed">
                    {ctx.description}
                  </div>
                )}
                {!ctx.title && !ctx.description && (
                  <div className="text-xs text-[#94A3B8] italic">该审批节点未配置说明</div>
                )}
              </div>
            )
          })()}
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="block text-sm text-[#0F172A]">审批意见（可选）</label>
              <Segmented
                size="small"
                value={approvalCommentMode}
                onChange={(val) => setApprovalCommentMode(val as 'text' | 'json')}
                options={[
                  { label: '文本', value: 'text' },
                  { label: 'JSON', value: 'json' },
                ]}
              />
            </div>
            <Input.TextArea
              value={approvalComment}
              onChange={(e) => setApprovalComment(e.target.value)}
              placeholder={
                approvalCommentMode === 'json'
                  ? '{"score": 8, "note": "ok"}'
                  : '请输入审批意见...'
              }
              rows={3}
              className={approvalCommentMode === 'json' ? 'font-mono' : ''}
            />
          </div>
        </div>
      </Modal>

      {/* ─── Rewind Modal（退回重跑）─── */}
      <Modal
        title="退回重跑"
        open={rewindModalOpen}
        onOk={handleRewind}
        onCancel={() => setRewindModalOpen(false)}
        okText="确定退回"
        cancelText="取消"
        confirmLoading={interveneMutation.isPending}
        destroyOnClose
        width={560}
      >
        {(() => {
          if (!taskDetail?.checkpoint) {
            return <div className="text-sm text-[#94A3B8]">无可回退的执行上下文</div>
          }
          const pausedAt = taskDetail.checkpoint.paused_at_node ?? ''
          const completed = (taskDetail.checkpoint.completed_nodes ?? []).filter(n => n !== pausedAt)
          const selectOptions = completed.map(n => {
            const meta = rewindNodeMap[n]
            return { value: n, label: meta ? `${meta.label} · ${meta.type}` : n }
          })
          const wfLoading = rewindWorkflowQuery.isLoading
          const targetOutput = rewindTargetNode ? taskDetail.variables?.[rewindTargetNode] : undefined

          return (
            <div className="flex flex-col gap-3 py-2">
              <p className="text-sm text-[#475569]">
                选择一个已执行的节点，任务将从该节点重新执行其全部下游。当前审批节点（{pausedAt}）不可选。
              </p>

              {/* 目标节点选择 */}
              <div>
                <label className="block text-sm text-[#0F172A] mb-1.5">退回到节点 <span className="text-red-500">*</span></label>
                <Select
                  value={rewindTargetNode || undefined}
                  onChange={(v: string) => setRewindTargetNode(v)}
                  placeholder="选择一个已执行的节点"
                  options={selectOptions}
                  loading={wfLoading}
                  disabled={wfLoading || completed.length === 0}
                  showSearch
                  optionFilterProp="label"
                  style={{ width: '100%' }}
                  notFoundContent={wfLoading ? <Spin size="small" /> : (completed.length === 0 ? <Empty description="无可回退的节点" /> : null)}
                />
              </div>

              {/* 选中节点的当前输出预览（确认退回点）*/}
              {rewindTargetNode && (
                <div>
                  <label className="block text-sm text-[#0F172A] mb-1.5">该节点当前输出</label>
                  <pre className="text-xs font-mono bg-[#F8FAFC] border border-line rounded-lg p-3 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[#475569]">
                    {targetOutput === undefined ? '(无输出)' : JSON.stringify(targetOutput, null, 2)}
                  </pre>
                </div>
              )}

              {/* 可选：修改变量 */}
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="block text-sm text-[#0F172A]">修改变量（可选）</label>
                  <Segmented
                    size="small"
                    value={rewindVarsMode}
                    onChange={(val) => setRewindVarsMode(val as 'none' | 'json')}
                    options={[
                      { label: '不改', value: 'none' },
                      { label: 'JSON 编辑', value: 'json' },
                    ]}
                  />
                </div>
                {rewindVarsMode === 'json' && (
                  <Input.TextArea
                    value={rewindVarsText}
                    onChange={(e) => setRewindVarsText(e.target.value)}
                    placeholder='{"input": {"q": "修改后的值"}}'
                    rows={6}
                    className="font-mono"
                  />
                )}
                {rewindVarsMode === 'none' && (
                  <div className="text-xs text-[#94A3B8]">不修改变量，仅退回并重跑下游节点。</div>
                )}
              </div>
            </div>
          )
        })()}
      </Modal>

      {/* ─── Edit Scheduled Task Modal ─── */}
      <Modal
        title="编辑定时任务配置"
        open={!!editTask}
        onCancel={() => { setEditTask(null); setEditDirty(false) }}
        width={560}
        destroyOnClose
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button onClick={() => { setEditTask(null); setEditDirty(false) }}>取消</Button>
            <Button
              type="primary"
              loading={editScheduleMutation.isPending || editTriggerLoading}
              disabled={!editDirty || !editTask?.trigger_id}
              onClick={() => {
                if (!editTask?.trigger_id) return

                // Validate
                if (editEnabled && editTriggerType === 'once' && !editScheduledAt) {
                  message.warning('请选择执行时间')
                  return
                }
                if (editTriggerType === 'cron' && !editCronExpression.trim()) {
                  message.warning('请填写 Cron 表达式')
                  return
                }

                const config: Partial<TriggerConfig> = {
                  type: editTriggerType,
                  enabled: editEnabled,
                  default_input: editDefaultInput as Record<string, unknown>,
                }
                if (editTriggerType === 'cron') {
                  config.cron_expression = editCronExpression
                } else {
                  config.execute_at = editScheduledAt
                }

                editScheduleMutation.mutate({
                  triggerId: editTask.trigger_id,
                  config,
                })
              }}
            >
              保存
            </Button>
          </div>
        }
      >
        <Spin spinning={editTriggerLoading}>
          <div className="space-y-4 py-2">
            {/* 启用开关 */}
            <div className="flex items-center gap-3">
              <span className="text-sm text-[#0F172A]">启用</span>
              <Switch
                checked={editEnabled}
                onChange={(checked) => { setEditEnabled(checked); setEditDirty(true) }}
                size="small"
              />
              {editEnabled && (
                <span className="text-xs text-green-500">● 已启用</span>
              )}
            </div>

            {/* 触发类型 */}
            <div className="flex items-center gap-3">
              <span className="text-sm text-[#0F172A]">触发类型:</span>
              <Radio.Group
                value={editTriggerType}
                onChange={(e) => { setEditTriggerType(e.target.value); setEditDirty(true) }}
              >
                <Radio value="cron">重复执行</Radio>
                <Radio value="once">一次性</Radio>
              </Radio.Group>
            </div>

            <Divider className="!my-2" />

            {/* 频率配置 */}
            {editTriggerType === 'cron' && (
              <div>
                <div className="text-sm text-[#0F172A] font-medium mb-2">执行频率</div>
                <TriggerSchedulePicker
                  value={editCronExpression}
                  onChange={(cron) => { setEditCronExpression(cron); setEditDirty(true) }}
                  disabled={editTriggerLoading}
                />
              </div>
            )}

            {editTriggerType === 'once' && (
              <div>
                <div className="text-sm text-[#0F172A] font-medium mb-2">执行时间</div>
                <DatePicker
                  showTime={{ format: 'HH:mm' }}
                  format="YYYY-MM-DD HH:mm"
                  value={editScheduledAt ? dayjs(editScheduledAt) : null}
                  onChange={(date) => {
                    // Send ISO string with timezone offset to avoid ambiguity
                    setEditScheduledAt(date ? date.toISOString() : '')
                    setEditDirty(true)
                  }}
                  className="!w-full"
                  disabled={editTriggerLoading}
                />
              </div>
            )}

            <Divider className="!my-2" />

            {/* 输入参数 */}
            <div>
              <div className="text-sm text-[#0F172A] font-medium mb-2">输入参数</div>
              {editStartNodeVars.length > 0 ? (
                <div className="space-y-3">
                  {editStartNodeVars.map((v) => (
                    <VariableFormField
                      key={v.name}
                      variable={v}
                      value={editDefaultInput[v.name]}
                      onChange={(val) => {
                        setEditDefaultInput((prev) => ({ ...prev, [v.name]: val }))
                        setEditDirty(true)
                      }}
                      disabled={editTriggerLoading}
                    />
                  ))}
                  <p className="text-[10px] text-[#94A3B8]">
                    提示: 支持模板语法 <code>{'{{ now() }}'}</code> <code>{'{{ today() }}'}</code>
                  </p>
                </div>
              ) : (
                <div className="text-xs text-[#94A3B8]">
                  开始节点未定义变量，触发时将使用空输入。
                </div>
              )}
            </div>
          </div>
        </Spin>
      </Modal>

      {/* ─── Agent 节点执行详情 Modal ─── */}
      <Modal
        title="Agent 执行详情"
        open={!!nodeDetail}
        onCancel={() => setNodeDetail(null)}
        footer={null}
        width={680}
        destroyOnClose
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
      >
        <Spin spinning={nodeTimelineLoading}>
          {nodeTimelineError ? (
            <Alert
              type="warning"
              message="无执行记录"
              description="该节点可能尚未执行，或执行过程中未产生 checkpoint。"
              showIcon
            />
          ) : nodeTimelineData ? (
            <>
              <div className="flex items-center gap-3 mb-3 text-[11px] text-[#94A3B8]">
                <span>消息数: {nodeTimelineData.message_count}</span>
                <span>·</span>
                <span>节点: <code className="text-[10px]">{nodeTimelineData.node_id}</code></span>
              </div>
              <AgentTimeline entries={nodeTimelineData.timeline} />
            </>
          ) : null}
        </Spin>
      </Modal>
    </div>
  )
}

/* ─── Info row component ─── */
function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-[#64748B]">{label}</span>
      <span className={`text-xs text-[#0F172A] ${mono ? 'font-mono' : ''} ml-4 text-right max-w-[280px] truncate`}>
        {value}
      </span>
    </div>
  )
}
