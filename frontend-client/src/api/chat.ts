import {
  ApiError,
  apiRequest,
  applyApiKeyHeaders,
  ensureAccessToken,
  getVisitorId,
  streamUrl,
  AUTH_MODE,
} from './client'
import { useAuthStore } from '../store/auth'
import type {
  AgentRecord,
  AgentSummary,
  ChatSession,
  FileUploadResult,
  MessageRecord,
  SessionFile,
  StreamEvent,
} from '../types'

function toLocalAgent(item: AgentRecord): AgentSummary {
  return {
    id: item.id,
    name: item.name,
    description: item.description ?? '',
    avatar: item.avatar,
    status: item.status,
    accessSource: 'company_owned',
    welcomeMessage: item.welcome_message ?? '',
    recommendedItems: item.recommended_items,
  }
}

/** apikey 模式下拼接 visitor_id query（后端从 query 读）。 */
function visitorQuery(): string {
  return AUTH_MODE === 'apikey'
    ? `visitor_id=${encodeURIComponent(getVisitorId())}`
    : ''
}

/** 给已有 query 的路径追加 visitor_id（apikey 模式）。 */
function withVisitor(path: string): string {
  const q = visitorQuery()
  return q ? `${path}${path.includes('?') ? '&' : '?'}${q}` : path
}

export async function listAvailableAgents(): Promise<AgentSummary[]> {
  const path =
    AUTH_MODE === 'apikey' ? '/v1/ext/agents?page_size=200' : '/v1/agents?page_size=200'
  const page = await apiRequest<{ items: AgentRecord[] }>(path)
  return page.items.map(toLocalAgent)
}

export async function listSessions(agentId: string): Promise<ChatSession[]> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/agents/${encodeURIComponent(agentId)}/sessions?page_size=200`)
      : `/v1/sessions?agent_id=${encodeURIComponent(agentId)}&page_size=200`
  const page = await apiRequest<{ items: ChatSession[] }>(path)
  return page.items.map(normalizeSession)
}

export async function createSession(agentId: string): Promise<ChatSession> {
  if (AUTH_MODE === 'apikey') {
    const session = await apiRequest<ChatSession & { _id?: string }>(
      withVisitor(`/v1/ext/agents/${encodeURIComponent(agentId)}/sessions`),
      { method: 'POST' },
    )
    return normalizeSession(session)
  }
  const session = await apiRequest<ChatSession & { _id?: string }>('/v1/sessions', {
    method: 'POST',
    body: JSON.stringify({ agent_id: agentId, title: '' }),
  })
  return normalizeSession(session)
}

function normalizeSession(session: ChatSession & { _id?: string }): ChatSession {
  return { ...session, id: session.id || session._id || '' }
}

export async function deleteSession(sessionId: string): Promise<void> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/sessions/${encodeURIComponent(sessionId)}`)
      : `/v1/sessions/${encodeURIComponent(sessionId)}`
  await apiRequest(path, { method: 'DELETE' })
}

export async function listMessages(sessionId: string): Promise<MessageRecord[]> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/sessions/${encodeURIComponent(sessionId)}`)
      : `/v1/sessions/${encodeURIComponent(sessionId)}`
  const detail = await apiRequest<{
    messages: Array<MessageRecord & { _id?: string }>
  }>(path)
  return detail.messages.map((message) => ({
    ...message,
    id: message.id || message._id || '',
  }))
}

export async function uploadSessionFile(
  sessionId: string,
  file: File,
): Promise<FileUploadResult> {
  const form = new FormData()
  form.append('file', file)
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/sessions/${encodeURIComponent(sessionId)}/files/upload`)
      : `/v1/sessions/${encodeURIComponent(sessionId)}/files/upload`
  const result = await apiRequest<{
    file: { id?: string; _id?: string; name: string; size: number; mime_type: string }
    workspace_path: string
  }>(path, { method: 'POST', body: form })
  return {
    id: result.file.id || result.file._id || '',
    name: result.file.name,
    path: result.workspace_path,
    size: result.file.size,
    mime: result.file.mime_type,
    is_output: false,
  }
}

export async function listSessionFiles(sessionId: string): Promise<SessionFile[]> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/sessions/${encodeURIComponent(sessionId)}/files`)
      : `/v1/sessions/${encodeURIComponent(sessionId)}/files`
  return apiRequest<SessionFile[]>(path)
}

export async function getSessionFile(
  sessionId: string,
  fileName: string,
): Promise<Blob> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(
          `/v1/ext/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileName)}`,
        )
      : `/v1/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(fileName)}`
  return apiRequest<Blob>(path)
}

export async function getUploadedFile(fileId: string): Promise<Blob> {
  if (AUTH_MODE === 'apikey') {
    // ext API 首期未暴露按 file_id 下载；无状态模式下历史「上传」附件不回显。
    throw new ApiError('无状态模式下不支持按文件 ID 下载历史附件', 404)
  }
  return apiRequest<Blob>(`/v1/files/${encodeURIComponent(fileId)}/download`)
}

export async function downloadUploadedFile(
  fileId: string,
  fileName: string,
): Promise<void> {
  const blob = await getUploadedFile(fileId)
  downloadBlob(blob, fileName)
}

export async function downloadSessionFile(
  sessionId: string,
  fileName: string,
): Promise<void> {
  const blob = await getSessionFile(sessionId, fileName)
  downloadBlob(blob, fileName)
}

function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export async function downloadSessionZip(sessionId: string): Promise<void> {
  const path =
    AUTH_MODE === 'apikey'
      ? withVisitor(`/v1/ext/sessions/${encodeURIComponent(sessionId)}/files.zip`)
      : `/v1/sessions/${encodeURIComponent(sessionId)}/files.zip`
  const blob = await apiRequest<Blob>(path)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `session-${sessionId}.zip`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

async function* parseSse(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.ok || !response.body) {
    throw new Error(`流式请求失败（${response.status}）`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const frames = buffer.split(/\r?\n\r?\n/)
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      for (const line of frame.split(/\r?\n/)) {
        if (!line.startsWith('data:')) continue
        const raw = line.slice(5).trim()
        if (!raw) continue
        try {
          yield JSON.parse(raw) as StreamEvent
        } catch {
          // A malformed event is isolated to its frame; later events remain readable.
        }
      }
    }
    if (done) break
  }
}

async function openStream(
  path: string,
  body: Record<string, unknown>,
  signal: AbortSignal,
  retry = true,
): Promise<Response> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  if (AUTH_MODE === 'apikey') {
    applyApiKeyHeaders(headers)
  } else {
    const token = await ensureAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }
  const response = await fetch(streamUrl(path), {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal,
  })
  if (response.status === 401 && retry && AUTH_MODE === 'jwt') {
    useAuthStore.setState({ accessToken: null })
    await ensureAccessToken()
    return openStream(path, body, signal, false)
  }
  return response
}

export async function* streamMessage(
  agentId: string,
  sessionId: string,
  content: string,
  fileIds: string[],
  filePaths: string[],
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  if (AUTH_MODE === 'apikey') {
    const response = await openStream(
      `/v1/ext/agents/${encodeURIComponent(agentId)}/invoke/stream`,
      {
        message: content,
        session_id: sessionId,
        visitor_id: getVisitorId(),
        enable_thinking: true,
        ...(fileIds.length ? { file_ids: fileIds } : {}),
        ...(filePaths.length ? { file_paths: filePaths } : {}),
      },
      signal,
    )
    yield* parseSse(response)
    return
  }
  const response = await openStream(
    `/v1/agents/${encodeURIComponent(agentId)}/stream`,
    {
      input: content,
      session_id: sessionId,
      enable_thinking: true,
      ...(fileIds.length ? { file_ids: fileIds } : {}),
      ...(filePaths.length ? { file_paths: filePaths } : {}),
    },
    signal,
  )
  yield* parseSse(response)
}

export async function* streamConfirmation(
  agentId: string,
  sessionId: string,
  answer: string,
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  if (AUTH_MODE === 'apikey') {
    const response = await openStream(
      `/v1/ext/agents/${encodeURIComponent(agentId)}/invoke/resume`,
      { session_id: sessionId, answer, visitor_id: getVisitorId(), enable_thinking: true },
      signal,
    )
    yield* parseSse(response)
    return
  }
  const response = await openStream(
    `/v1/agents/${encodeURIComponent(agentId)}/resume`,
    { session_id: sessionId, answer, enable_thinking: true },
    signal,
  )
  yield* parseSse(response)
}
