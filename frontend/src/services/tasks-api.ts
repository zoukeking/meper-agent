/**
 * Tasks API service — wraps backend /tasks endpoints.
 *
 * Uses the shared apiClient instance (auto auth header + 401 refresh).
 * Response fields are snake_case per backend contract.
 */
import { apiClient } from './api-client'

/* ─── Types (snake_case, matches backend schemas) ─── */

export type TaskStatusValue =
  | 'pending'
  | 'running'
  | 'waiting_human'
  | 'completed'
  | 'failed'
  | 'cancelled'

/**
 * 看板使用的有序状态列表（6 列顺序）。
 */
export const BOARD_STATUSES: TaskStatusValue[] = [
  'pending',
  'running',
  'waiting_human',
  'completed',
  'failed',
  'cancelled',
]

export interface TimelineEvent {
  timestamp: string
  event_type: string
  data: Record<string, unknown>
  actor: string
}

/**
 * 节点执行进度（看板卡片用）。
 * - completedCount：已结束（complete + failed）的去重节点数
 * - currentNodeId：当前正在执行的节点（最后一个 node_start 且未 complete 的）
 * - currentNodeType：当前节点的类型（node_start 事件 data 里携带）
 */
export interface NodeProgress {
  completedCount: number
  currentNodeId?: string
  currentNodeType?: string
}

/**
 * 扫描 timeline 解析节点执行进度。
 * - 收集 node_complete / node_failed 的 node_id 去重计数
 * - 找出最后一个 node_start 且未在结束集合中的节点作为 current
 */
export function parseNodeProgress(timeline?: TimelineEvent[]): NodeProgress | null {
  if (!timeline || timeline.length === 0) return null

  const startedNodes = new Map<string, string>() // node_id -> node_type
  const finishedNodes = new Set<string>()

  // 按时间顺序遍历（timeline 默认按时间升序）
  for (const evt of timeline) {
    const data = evt.data ?? {}
    const nodeId = typeof data.node_id === 'string' ? data.node_id : undefined
    if (!nodeId) continue

    if (evt.event_type === 'node_start') {
      const nodeType = typeof data.node_type === 'string' ? data.node_type : undefined
      startedNodes.set(nodeId, nodeType ?? '')
    } else if (evt.event_type === 'node_complete' || evt.event_type === 'node_failed') {
      finishedNodes.add(nodeId)
    }
  }

  if (startedNodes.size === 0 && finishedNodes.size === 0) return null

  // 找当前节点：已 start 但未 finish
  let currentNodeId: string | undefined
  let currentNodeType: string | undefined
  for (const [nid, ntype] of startedNodes) {
    if (!finishedNodes.has(nid)) {
      currentNodeId = nid
      currentNodeType = ntype || undefined
      break
    }
  }

  return {
    completedCount: finishedNodes.size,
    currentNodeId,
    currentNodeType,
  }
}

export interface TaskError {
  node_id?: string
  node_type?: string
  error_message: string
  error_code: string
  timestamp?: string
}

/**
 * Checkpoint — persisted when workflow pauses at a Human node.
 */
export interface Checkpoint {
  paused_at_node: string
  completed_nodes: string[]
  variable_snapshot: Record<string, unknown>
  paused_at: string
  human_context: {
    node_id?: string
    title?: string
    description?: string
    options?: string[]
    timeout_ms?: number
    timeout_action?: string
  }
  timeout_deadline?: string | null
  timeout_action: string
}

export interface TaskSummary {
  id: string
  workflow_id: string
  workflow_version: string
  status: TaskStatusValue
  input: Record<string, unknown>
  output?: Record<string, unknown> | null
  parent_task_id?: string | null
  created_by: string
  created_by_type: string
  version: number
  error?: TaskError | null
  checkpoint?: Checkpoint | null
  source?: 'manual' | 'trigger'
  trigger_id?: string | null
  scheduled_at?: string | null
  created_at: string
  updated_at: string
}

export interface TaskDetail extends TaskSummary {
  variables: Record<string, unknown>
  call_chain: string[]
  timeline: TimelineEvent[]
}

export interface TaskListParams {
  page?: number
  page_size?: number
  status?: string
  created_by?: string
  workflow_id?: string
}

export interface TaskListResponse {
  items: TaskSummary[]
  total: number
  page: number
  page_size: number
}

export interface TaskStats {
  global_running: number
  global_pending: number
  global_max: number
  user_stats: Array<{ user_id: string; running: number }>
}

export interface TaskCreatePayload {
  workflow_id: string
  input: Record<string, unknown>
  scheduled_at?: string | null
}

/**
 * comment 支持三种形态（与后端 _normalize_comment 对齐）：
 * - string：纯文本（老用法，向后兼容）
 * - { type: 'text', value: string }：文本
 * - { type: 'json', value: unknown }：结构化数据，value 原样存入 variables，
 *   下游可用 {{node.comment.field}} 钻取
 */
export type CommentValue =
  | string
  | { type: 'text'; value: string }
  | { type: 'json'; value: unknown }

export interface TaskIntervenePayload {
  action: string
  /** @deprecated use comment */
  reason?: string
  comment?: CommentValue
  version: number
  /** rewind 专用：回退到的目标节点（必须是已执行过的节点） */
  target_node_id?: string
  /** rewind 可选：覆盖变量池的输入值（merge 语义） */
  variables?: Record<string, unknown>
}

export interface TaskInterveneResponse {
  task_id: string
  status: TaskStatusValue
  version: number
  message: string
}

export interface WorkflowRegistryEntry {
  _id: string
  name: string
  description: string
  input_schema: Record<string, unknown>
  workflow_id: string
  has_human_node: boolean
  version: string
  tags: string[]
  published: boolean
}

/**
 * Task 输出文件（snake_case，与后端 FileRefResponse 对齐）。
 * Story 4-15-UI：前端查看/下载 Agent 节点产出的文件。
 */
export interface TaskOutputFile {
  _id: string
  owner_user_id: string
  storage_key: string
  name: string
  size: number
  mime_type: string
  sha256: string
  origin_kind: string
  origin_id: string
  status: string
  created_at: string
  updated_at: string
}

/**
 * Agent 节点执行详情的单条 timeline 条目。
 * 与 chat 路径的 timeline_entries 格式完全一致。
 */
export interface NodeTimelineEntry {
  type: 'thinking' | 'text' | 'tool_call' | 'tool_result' | 'user'
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  id?: string
}

/**
 * Agent 节点执行详情响应（按需从 checkpointer thread 读取）。
 */
export interface NodeTimelineResponse {
  task_id: string
  node_id: string
  thread_id: string
  timeline: NodeTimelineEntry[]
  message_count: number
}

/* ─── API methods ─── */

export const tasksApi = {
  /**
   * List tasks with optional filtering and pagination.
   * GET /api/v1/tasks
   */
  async list(params: TaskListParams = {}): Promise<TaskListResponse> {
    const res = await apiClient.get<TaskListResponse>('/api/v1/tasks', { params })
    return res.data
  },

  /**
   * Get a single task by ID.
   * GET /api/v1/tasks/{id}
   */
  async get(taskId: string): Promise<TaskDetail> {
    const res = await apiClient.get<TaskDetail>(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
    return res.data
  },

  /**
   * Create a new task.
   * POST /api/v1/tasks
   */
  async create(data: TaskCreatePayload): Promise<TaskDetail> {
    const res = await apiClient.post<TaskDetail>('/api/v1/tasks', data)
    return res.data
  },

  /**
   * Intervene a running task (cancel, retry, approve, reject, etc.).
   * POST /api/v1/tasks/{id}/intervene
   */
  async intervene(taskId: string, data: TaskIntervenePayload): Promise<TaskInterveneResponse> {
    const res = await apiClient.post<TaskInterveneResponse>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/intervene`,
      data,
    )
    return res.data
  },

  /**
   * Delete a terminal-state task.
   * DELETE /api/v1/tasks/{id}
   */
  async remove(taskId: string): Promise<void> {
    await apiClient.delete(`/api/v1/tasks/${encodeURIComponent(taskId)}`)
  },

  /**
   * List files registered to a task.
   * GET /api/v1/tasks/{id}/outputs
   * Story 4-15-UI: 前端查看/下载 Agent 节点产出的文件
   */
  async listOutputs(taskId: string): Promise<TaskOutputFile[]> {
    const { data } = await apiClient.get<TaskOutputFile[]>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/outputs`
    )
    return data
  },

  /**
   * Get the execution trace of an Agent node (thinking/tool_call/tool_result/text).
   * Read on demand from the LangGraph checkpointer thread.
   * GET /api/v1/tasks/{taskId}/nodes/{nodeId}/timeline
   */
  async getNodeTimeline(taskId: string, nodeId: string): Promise<NodeTimelineResponse> {
    const res = await apiClient.get<NodeTimelineResponse>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/nodes/${encodeURIComponent(nodeId)}/timeline`
    )
    return res.data
  },

  /**
   * Get task statistics (running/pending/limits).
   * GET /api/v1/tasks/stats
   */
  async getStats(): Promise<TaskStats> {
    const res = await apiClient.get<TaskStats>('/api/v1/tasks/stats')
    return res.data
  },

  /**
   * List audit logs for a task.
   * GET /api/v1/tasks/{id}/audit-logs
   */
  async getAuditLogs(taskId: string, limit = 50): Promise<{ items: unknown[]; total: number }> {
    const res = await apiClient.get(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/audit-logs`,
      { params: { limit } },
    )
    return res.data
  },

  /**
   * List published workflows (for task creation selector).
   * GET /api/v1/workflow-registry
   */
  async listWorkflows(search?: string): Promise<{ items: WorkflowRegistryEntry[]; total: number }> {
    const res = await apiClient.get('/api/v1/workflow-registry', {
      params: { page: 1, page_size: 100, ...(search ? { search } : {}) },
    })
    return res.data
  },

}

/* ─── Query key factory ─── */

export const taskKeys = {
  all: ['tasks'] as const,
  lists: () => [...taskKeys.all, 'list'] as const,
  list: (params: TaskListParams) => [...taskKeys.lists(), params] as const,
  details: () => [...taskKeys.all, 'detail'] as const,
  detail: (id: string) => [...taskKeys.details(), id] as const,
  logs: (id: string) => [...taskKeys.detail(id), 'logs'] as const,
  stats: () => [...taskKeys.all, 'stats'] as const,
  workflows: () => [...taskKeys.all, 'workflows'] as const,
}
