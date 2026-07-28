/**
 * Execution stats API — cross-channel agent-execution statistics.
 *
 * Data comes from the `execution_logs` collection (per-call records,
 * independent of session lifecycle) + `tasks` (workflow executions).
 */
import { apiClient } from './api-client'

/** Stats block for a single access channel. */
export interface ChannelStats {
  calls: number
  tokens: number
  input_tokens: number
  output_tokens: number
  llm_calls: number
  /** Average latency in ms (0 if no data). */
  avg_latency_ms: number
  success: number
  failed: number
}

/** Task (workflow) execution stats by trigger type. */
export interface TaskStats {
  internal: { tasks: number; tokens: number }
  api_key: { tasks: number; tokens: number }
  agent_triggered: { tasks: number; tokens: number }
  scheduled: { tasks: number; tokens: number }
}

export interface ExecutionStats {
  range: { start: string | null; end: string | null }
  channels: {
    internal: ChannelStats
    api_key: ChannelStats
    im: ChannelStats
  }
  totals: ChannelStats & { success_rate: number }
  tasks: TaskStats
}

/** One execution-log record (per agent invocation). */
export interface ExecutionLogItem {
  source: string
  user_id: string
  caller_name: string
  agent_id: string
  session_id: string
  request_id: string
  status: string
  latency_ms: number
  total_tokens: number
  input_tokens: number
  output_tokens: number
  llm_calls: number
  timestamp: string
}

export interface ExecutionLogListResponse {
  items: ExecutionLogItem[]
  total: number
  page: number
  page_size: number
}

export const statsApi = {
  getExecutionStats: (params: {
    start?: string
    end?: string
    date?: string
  }) => apiClient.get<ExecutionStats>('/api/v1/execution-stats', { params }),

  listExecutionLogs: (params: {
    source?: string
    agent_id?: string
    session_id?: string
    start?: string
    end?: string
    page?: number
    page_size?: number
  }) => apiClient.get<ExecutionLogListResponse>('/api/v1/execution-logs', { params }),
}
