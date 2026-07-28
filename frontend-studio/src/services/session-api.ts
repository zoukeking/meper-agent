/**
 * Session API service — wraps backend /sessions endpoints.
 *
 * Used by ChatPanel to load conversation history and manage sessions.
 */
import { apiClient } from '../lib/api-client'
import type { TokenUsage } from '../types'

/* ─── Types (snake_case, matches backend schemas) ─── */

export interface Session {
  _id: string
  user_id: string
  agent_id: string
  title: string
  status: string
  message_count: number
  total_tokens?: number
  created_at: string
  updated_at: string
}

export interface MessageRecord {
  _id: string
  session_id: string
  role: 'user' | 'agent'
  content: string
  /** Structured timeline events stored in agent messages */
  timeline_entries: TimelineEntryData[]
  /** File IDs attached to this message */
  file_ids?: string[]
  /** File references attached to this message */
  files?: FileRef[]
  /** Token usage for this agent message (backend token_usage dict). */
  token_usage?: TokenUsage
  created_at: string
}

export interface FileRef {
  id: string
  _id?: string  // Backend returns _id due to MongoDB field alias
  name: string
  size: number
  mime_type: string
  storage_key: string
  status: string
  created_at: string
}

/** Get file ID, handling both `id` and `_id` (MongoDB alias) */
export function getFileId(file: FileRef): string {
  return file.id || file._id || ''
}

export interface ChatFileUploadResponse {
  file: FileRef
  message: MessageRecord | null
  workspace_path: string
}

export interface TimelineEntryData {
  type:
    | 'thinking'
    | 'tool_call'
    | 'tool_result'
    | 'tool'
    | 'text'
    | 'final_answer'
    | 'interrupt'
    | 'tool_call_start'
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  /** For tool_result: structured success/error status from backend. */
  status?: 'success' | 'error'
  /** For interrupt entries: discriminates clarification vs workflow_confirmation. */
  kind?: 'clarification' | 'workflow_confirmation'
  /** For workflow_confirmation interrupt entries (confirm_workflow tool). */
  question?: string
  workflow_name?: string
  workflow_description?: string
  input_preview?: Record<string, unknown>
}

export interface SessionListResponse {
  items: Session[]
  total: number
  page: number
  page_size: number
}

export interface SessionDetailResponse {
  session: Session
  messages: MessageRecord[]
}

export interface SessionFileEntry {
  path: string
  size: number
  modified: number
}

/* ─── API methods ─── */

export const sessionApi = {
  /**
   * Create a new session.
   * POST /api/v1/sessions
   */
  async create(agentId: string, title?: string): Promise<Session> {
    const res = await apiClient.post<Session>('/api/v1/sessions', {
      agent_id: agentId,
      title: title ?? '',
    })
    return res.data
  },

  /**
   * List sessions for the current user, optionally filtered by agent.
   * GET /api/v1/sessions
   */
  async list(params: {
    agent_id?: string
    page?: number
    page_size?: number
  } = {}): Promise<SessionListResponse> {
    const res = await apiClient.get<SessionListResponse>('/api/v1/sessions', {
      params: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
        ...(params.agent_id ? { agent_id: params.agent_id } : {}),
      },
    })
    return res.data
  },

  /**
   * Get session detail with all messages.
   * GET /api/v1/sessions/{id}
   */
  async getDetail(sessionId: string): Promise<SessionDetailResponse> {
    const res = await apiClient.get<SessionDetailResponse>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    )
    return res.data
  },

  /**
   * Delete a session and all its messages.
   * DELETE /api/v1/sessions/{id}
   */
  async remove(sessionId: string): Promise<void> {
    await apiClient.delete(`/api/v1/sessions/${encodeURIComponent(sessionId)}`)
  },

  /* ─── File download endpoints ─── */

  /**
   * List output files for a session.
   * GET /api/v1/sessions/{id}/files
   */
  async listFiles(sessionId: string): Promise<SessionFileEntry[]> {
    const res = await apiClient.get<SessionFileEntry[]>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/files`,
    )
    return res.data
  },

  /**
   * Get the download URL for a single file.
   * Returns a URL string (not fetched) — use in <a href> or window.open.
   */
  getDownloadUrl(sessionId: string, filePath: string): string {
    const base = apiClient.defaults.baseURL ?? ''
    return `${base}/api/v1/sessions/${encodeURIComponent(sessionId)}/files/${filePath}`
  },

  /**
   * Get the download URL for all files as ZIP.
   */
  getZipDownloadUrl(sessionId: string): string {
    const base = apiClient.defaults.baseURL ?? ''
    return `${base}/api/v1/sessions/${encodeURIComponent(sessionId)}/files.zip`
  },

  /**
   * Download a single file with auth token.
   * Uses fetch + blob to properly include Authorization header.
   */
  async downloadFile(sessionId: string, filePath: string): Promise<void> {
    const url = `/api/v1/sessions/${encodeURIComponent(sessionId)}/files/${filePath}`
    const res = await apiClient.get(url, { responseType: 'blob' })
    const blob = new Blob([res.data])
    const downloadUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = downloadUrl
    // Extract filename from path
    const filename = filePath.split('/').pop() || 'download'
    link.download = filename
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(downloadUrl)
  },

  /**
   * Preview a file — returns blob + content-type for rendering in modal.
   * GET /api/v1/sessions/{id}/files/{path} with blob response.
   */
  async previewFile(sessionId: string, filePath: string): Promise<{ blob: Blob; contentType: string }> {
    const url = `/api/v1/sessions/${encodeURIComponent(sessionId)}/files/${filePath}`
    const res = await apiClient.get(url, { responseType: 'blob' })
    const contentType = (res.headers['content-type'] as string) || 'application/octet-stream'
    return { blob: res.data as Blob, contentType }
  },

  /**
   * Delete a single file and return the updated file list.
   * DELETE /api/v1/sessions/{id}/files/{path}
   */
  async deleteFile(sessionId: string, filePath: string): Promise<SessionFileEntry[]> {
    const url = `/api/v1/sessions/${encodeURIComponent(sessionId)}/files/${filePath}`
    const res = await apiClient.delete<SessionFileEntry[]>(url)
    return res.data
  },

  /**
   * Download all files as ZIP with auth token.
   */
  async downloadZip(sessionId: string): Promise<void> {
    const url = `/api/v1/sessions/${encodeURIComponent(sessionId)}/files.zip`
    const res = await apiClient.get(url, { responseType: 'blob' })
    const blob = new Blob([res.data])
    const downloadUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = downloadUrl
    link.download = `${sessionId}.zip`
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(downloadUrl)
  },

  /**
   * Upload a file to a chat session.
   * POST /api/v1/sessions/{id}/files/upload
   * Uses FormData with multipart/form-data.
   */
  async uploadFile(
    sessionId: string,
    file: File,
    content?: string,
    onProgress?: (percent: number) => void,
  ): Promise<ChatFileUploadResponse> {
    const formData = new FormData()
    formData.append('file', file)
    if (content) {
      formData.append('content', content)
    }
    const res = await apiClient.post<ChatFileUploadResponse>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/files/upload`,
      formData,
      {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (progressEvent) => {
          if (onProgress && progressEvent.total) {
            const percent = Math.round((progressEvent.loaded * 100) / progressEvent.total)
            onProgress(percent)
          }
        },
      },
    )
    return res.data
  },
}

/* ─── Query key factory ─── */

export const sessionKeys = {
  all: ['sessions'] as const,
  lists: () => [...sessionKeys.all, 'list'] as const,
  list: (params: { agent_id?: string }) => [...sessionKeys.lists(), params] as const,
  details: () => [...sessionKeys.all, 'detail'] as const,
  detail: (id: string) => [...sessionKeys.details(), id] as const,
}
