/**
 * Tasks API — 任务详情/干预/产物（对接后端 /api/v1/tasks）。
 *
 * 复用 ./client 的 apiRequest（自动带 token + 401 refresh + ApiError）。
 * 字段 snake_case，与后端 schema 对齐；类型精简自 frontend-studio 的 tasks-api.ts，
 * 只取 dispatch_workflow 卡片需要的部分。
 */
import { apiRequest } from './client'

export type TaskStatusValue =
  | 'pending'
  | 'running'
  | 'waiting_human'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface TaskError {
  node_id?: string
  node_type?: string
  error_message: string
  error_code: string
  timestamp?: string
}

export interface Checkpoint {
  paused_at_node: string
  completed_nodes: string[]
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

export interface TimelineEvent {
  timestamp: string
  event_type: string
  data: Record<string, unknown>
  actor: string
}

export interface TaskOutputFile {
  id?: string
  _id: string
  name: string
  size: number
  mime_type: string
  origin_kind: string
  origin_id: string
  created_at: string
}

export interface TaskDetail {
  id: string
  workflow_id: string
  status: TaskStatusValue
  input: Record<string, unknown>
  output?: Record<string, unknown> | null
  created_by: string
  created_by_type: string
  version: number
  error?: TaskError | null
  checkpoint?: Checkpoint | null
  timeline: TimelineEvent[]
  total_tokens?: number
  created_at: string
  updated_at: string
}

export type CommentValue =
  | string
  | { type: 'text'; value: string }
  | { type: 'json'; value: unknown }

export interface TaskIntervenePayload {
  action: string
  comment?: CommentValue
  version: number
}

export interface TaskInterveneResponse {
  task_id: string
  status: TaskStatusValue
  version: number
  message: string
}

/** 工作流节点定义（取自 GET /v1/workflows/{id} 的 nodes[]，用于 node_id → label 映射）。 */
export interface WorkflowNode {
  node_id: string
  type: string
  label: string
}

export interface WorkflowDetail {
  id: string
  name: string
  nodes: WorkflowNode[]
}

/** Agent 节点执行 trace（按需从 checkpointer thread 读取，GET /tasks/{id}/nodes/{nid}/timeline）。 */
export interface NodeTimelineEntry {
  type: 'thinking' | 'text' | 'tool_call' | 'tool_result' | 'tool' | 'user'
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  id?: string
}

export interface NodeTimelineResponse {
  task_id: string
  node_id: string
  timeline: NodeTimelineEntry[]
  message_count: number
}

const PATH = (id: string) => `/v1/tasks/${encodeURIComponent(id)}`

export const tasksApi = {
  /** GET /v1/tasks/{id} — 任务详情（status/input/output/error/timeline/checkpoint/version）。 */
  get(taskId: string): Promise<TaskDetail> {
    return apiRequest<TaskDetail>(PATH(taskId))
  },

  /** POST /v1/tasks/{id}/intervene — approve/reject/skip/retry/resume/cancel，带 version 乐观锁。 */
  intervene(taskId: string, body: TaskIntervenePayload): Promise<TaskInterveneResponse> {
    return apiRequest<TaskInterveneResponse>(`${PATH(taskId)}/intervene`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },

  /** GET /v1/tasks/{id}/outputs — 产物文件列表（无产物返回 404，语义化为空列表）。 */
  async listOutputs(taskId: string): Promise<TaskOutputFile[]> {
    try {
      return await apiRequest<TaskOutputFile[]>(`${PATH(taskId)}/outputs`)
    } catch (err) {
      if ((err as { status?: number })?.status === 404) return []
      throw err
    }
  },

  /** GET /v1/workflows/{id} — 工作流定义（取 nodes 建 node_id→label 映射）。无权限时抛错，调用方降级。 */
  getWorkflow(workflowId: string): Promise<WorkflowDetail> {
    return apiRequest<WorkflowDetail>(`/v1/workflows/${encodeURIComponent(workflowId)}`)
  },

  /** GET /v1/tasks/{id}/nodes/{nodeId}/timeline — Agent 节点 REACT trace（thinking/tool_call/text）。 */
  getNodeTimeline(taskId: string, nodeId: string): Promise<NodeTimelineResponse> {
    return apiRequest<NodeTimelineResponse>(
      `${PATH(taskId)}/nodes/${encodeURIComponent(nodeId)}/timeline`,
    )
  },
}
