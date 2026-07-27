import { useCallback, useEffect, useRef, useState } from 'react'

import {
  getSessionFile,
  getUploadedFile,
  listMessages,
  streamConfirmation,
  streamMessage,
  uploadSessionFile,
} from '../api/chat'
import type {
  AttachmentView,
  ChatMessage,
  HitlState,
  MessageRecord,
  StreamEvent,
  ToolRun,
} from '../types'

/** 生成 UUID。crypto.randomUUID 在非安全上下文（如非 localhost 的 HTTP）
 * 下不可用，这里退化为随机 UUID v4，保证发送流程在任何环境都能跑。 */
function genId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

const OUTPUT_PATH_RE = /\boutput\/([^\s"'<>)\\]+\.[A-Za-z0-9]+)/g
const FILE_BLOCK_RE =
  /<file_hint\b[^>]*>[\s\S]*?<\/file_hint>|<file\b[^>]*\/>|<file\b[^>]*>[\s\S]*?<\/file>/g
const FILE_NAME_RE = /<file\s+name="([^"]+)"\s+mime="([^"]+)"/g

function outputAttachments(text: string): AttachmentView[] {
  const seen = new Set<string>()
  const result: AttachmentView[] = []
  for (const match of text.matchAll(OUTPUT_PATH_RE)) {
    const name = match[1]
    if (!name || seen.has(name)) continue
    seen.add(name)
    result.push({
      id: `output:${name}`,
      name,
      contentType: 'application/octet-stream',
      kind: /\.(png|jpe?g|gif|webp|svg)$/i.test(name) ? 'image' : 'file',
      source: 'output',
    })
  }
  return result
}

function userHistoryContent(content: string): {
  text: string
  attachments: AttachmentView[]
} {
  const attachments: AttachmentView[] = []
  let index = 0
  for (const match of content.matchAll(FILE_NAME_RE)) {
    const name = match[1]
    const contentType = match[2]
    if (!name || !contentType) continue
    attachments.push({
      id: `history-file:${index++}:${name}`,
      name,
      contentType,
      kind: contentType.startsWith('image/') ? 'image' : 'file',
      source: 'upload',
    })
  }
  return { text: content.replace(FILE_BLOCK_RE, '').trim(), attachments }
}

function fromHistory(record: MessageRecord): ChatMessage {
  if (record.role === 'user') {
    const parsed = userHistoryContent(record.content ?? '')
    const storedAttachments: AttachmentView[] = (record.files ?? []).map(
      (file, index) => ({
        id: file.id || file._id || `history-file-${index}`,
        name: file.name,
        contentType: file.mime_type || 'application/octet-stream',
        kind:
          file.mime_type?.startsWith('image/') ||
          /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.name)
            ? 'image'
            : 'file',
        source: 'upload',
      }),
    )
    return {
      id: record.id,
      role: 'user',
      text: parsed.text,
      reasoning: '',
      tools: [],
      attachments: storedAttachments.length ? storedAttachments : parsed.attachments,
      charts: [],
      status: 'success',
      createdAt: record.created_at ? new Date(record.created_at) : undefined,
    }
  }
  const entries = record.timeline_entries ?? []
  const tools: ToolRun[] = []
  const pendingTools = new Map<string, ToolRun>()
  for (const [index, entry] of entries.entries()) {
    if (entry.type === 'tool_call' || entry.type === 'tool') {
      const tool: ToolRun = {
        id: `history-tool-${index}`,
        name: entry.tool_name || 'tool',
        args: entry.args ? JSON.stringify(entry.args, null, 2) : undefined,
        status: entry.type === 'tool' ? 'complete' : 'running',
      }
      tools.push(tool)
      if (entry.type === 'tool_call') {
        pendingTools.set(entry.tool_name || 'tool', tool)
      }
    } else if (entry.type === 'tool_result') {
      const matched = pendingTools.get(entry.tool_name || 'tool')
      if (matched) {
        matched.result = entry.content
        matched.status = 'complete'
        pendingTools.delete(entry.tool_name || 'tool')
      }
    }
  }
  const reasoning = entries
    .filter((entry) => entry.type === 'thinking')
    .map((entry) => entry.content ?? '')
    .join('')
  const text =
    entries
      .filter((entry) => entry.type === 'text' || entry.type === 'final_answer')
      .map((entry) => entry.content ?? '')
      .join('') || record.content || ''
  const attachments = tools.flatMap((tool) => outputAttachments(tool.result ?? ''))
  return {
    id: record.id,
    role: 'assistant',
    text,
    reasoning,
    tools,
    attachments,
    charts: [],
    status: 'success',
    createdAt: record.created_at ? new Date(record.created_at) : undefined,
  }
}

interface AssistantAccumulator {
  id: string
  text: string
  reasoning: string
  tools: Map<string, ToolRun>
  attachments: Map<string, AttachmentView>
  charts: Map<string, string>
  /** 流内 error 事件记录的错误文本。一旦设置，后续 flush 会保持 error 状态。 */
  errorText?: string
}

function isImageName(name: string): boolean {
  return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(name)
}

function isChartOption(source: string): boolean {
  if (new Blob([source]).size > 1024 * 1024) return false
  try {
    const parsed = JSON.parse(source) as { series?: unknown }
    return Array.isArray(parsed.series) && parsed.series.length > 0
  } catch {
    return false
  }
}

export function useChat(
  agentId: string | null,
  sessionId: string | null,
  onFilesChanged: () => void,
) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false)
  const [hitl, setHitl] = useState<HitlState | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const accRef = useRef<AssistantAccumulator | null>(null)
  const urlsRef = useRef<Set<string>>(new Set())

  const revokeUrls = useCallback(() => {
    for (const url of urlsRef.current) URL.revokeObjectURL(url)
    urlsRef.current.clear()
  }, [])

  useEffect(() => {
    let cancelled = false
    abortRef.current?.abort()
    accRef.current = null
    revokeUrls()
    setMessages([])
    setHitl(null)
    setLoadError(null)
    if (!sessionId) return
    setLoading(true)
    listMessages(sessionId)
      .then((records) => {
        if (cancelled) return
        const history = records.map(fromHistory)
        setMessages(history)
        const pending = [...history]
          .reverse()
          .flatMap((message) => [...message.tools].reverse())
          .find((tool) => tool.name === 'ask_clarification' && !tool.result)
        if (pending) {
          let args: Record<string, unknown> = {}
          try {
            args = pending.args ? JSON.parse(pending.args) : {}
          } catch {
            args = {}
          }
          const rawOptions = args.options
          setHitl({
            taskId: pending.id,
            question: String(args.question || '请补充信息后继续。'),
            clarificationType: String(args.clarification_type || 'missing_info'),
            context: typeof args.context === 'string' ? args.context : undefined,
            options: Array.isArray(rawOptions)
              ? rawOptions.map(String)
              : typeof rawOptions === 'string'
                ? (() => {
                    try {
                      const parsed = JSON.parse(rawOptions)
                      return Array.isArray(parsed) ? parsed.map(String) : []
                    } catch {
                      return []
                    }
                  })()
                : [],
          })
        }
        void Promise.all(
          history.map(async (message) => {
            const attachments = await Promise.all(
              message.attachments.map(async (attachment) => {
                if (!isImageName(attachment.name)) return attachment
                try {
                  const blob =
                    attachment.source === 'upload'
                      ? await getUploadedFile(attachment.id)
                      : await getSessionFile(sessionId, attachment.name)
                  if (cancelled) return attachment
                  const url = URL.createObjectURL(blob)
                  urlsRef.current.add(url)
                  return { ...attachment, kind: 'image' as const, url }
                } catch {
                  return attachment
                }
              }),
            )
            const charts: string[] = []
            if (message.role === 'assistant') {
              const outputNames = new Set(
                message.tools.flatMap((tool) =>
                  outputAttachments(tool.result ?? '').map((item) => item.name),
                ),
              )
              for (const name of outputNames) {
                if (!name.toLowerCase().endsWith('.json')) continue
                try {
                  const raw = await (await getSessionFile(sessionId, name)).text()
                  if (isChartOption(raw)) charts.push(raw.trim())
                } catch {
                  // Non-previewable outputs stay available through the file card.
                }
              }
            }
            return { id: message.id, attachments, charts }
          }),
        ).then((artifacts) => {
          if (cancelled) return
          const byId = new Map(artifacts.map((item) => [item.id, item]))
          setMessages((current) =>
            current.map((message) => {
              const artifact = byId.get(message.id)
              return artifact
                ? {
                    ...message,
                    attachments: artifact.attachments,
                    charts: artifact.charts,
                  }
                : message
            }),
          )
        })
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : '历史消息加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
      abortRef.current?.abort()
    }
  }, [sessionId, revokeUrls])

  useEffect(() => revokeUrls, [revokeUrls])

  const flush = useCallback((acc: AssistantAccumulator, status: ChatMessage['status'] = 'loading') => {
    // 流内 error 事件记录了错误文本时，后续 flush 一律保持 error 状态并带上 error 字段，
    // 避免被循环内的普通 flush（默认 loading）覆盖丢失。
    const effectiveStatus: ChatMessage['status'] = acc.errorText ? 'error' : status
    const next: ChatMessage = {
      id: acc.id,
      role: 'assistant',
      text: acc.text,
      reasoning: acc.reasoning,
      tools: Array.from(acc.tools.values()),
      attachments: Array.from(acc.attachments.values()),
      charts: Array.from(acc.charts.values()),
      status: effectiveStatus,
      error: acc.errorText,
      createdAt: new Date(),
    }
    setMessages((current) =>
      current.map((message) => (message.id === acc.id ? next : message)),
    )
  }, [])

  const process = useCallback(
    async (events: AsyncGenerator<StreamEvent>, acc: AssistantAccumulator) => {
      try {
        for await (const event of events) {
          if ((event.type === 'text_delta' || event.type === 'text') && event.content) {
            if (event.type === 'text') acc.text = event.content
            else acc.text += event.content
          } else if (
            (event.type === 'thinking' || event.type === 'thinking_delta') &&
            event.content
          ) {
            acc.reasoning += event.content
          } else if (event.type === 'tool_call') {
            const id = `tool-${acc.tools.size + 1}`
            const current = acc.tools.get(id)
            acc.tools.set(id, {
              id,
              name: event.tool_name || current?.name || 'tool',
              args: event.args ? JSON.stringify(event.args, null, 2) : current?.args,
              result: current?.result,
              isError: current?.isError,
              auto: event.auto ?? current?.auto,
              status: 'running',
            })
          } else if (event.type === 'tool_result' && event.content) {
            const id =
              Array.from(acc.tools.values()).find((tool) => tool.status === 'running')?.id ||
              `tool-${acc.tools.size + 1}`
            const current = acc.tools.get(id)
            const isError = event.status === 'error'
            acc.tools.set(id, {
              id,
              name: current?.name || event.tool_name || 'tool',
              args: current?.args,
              result: event.content,
              isError,
              auto: event.auto ?? current?.auto,
              status: isError ? 'error' : 'complete',
            })
            for (const attachment of outputAttachments(event.content)) {
              acc.attachments.set(attachment.id, attachment)
              if (isImageName(attachment.name)) {
                try {
                  const blob = await getSessionFile(sessionId!, attachment.name)
                  const url = URL.createObjectURL(blob)
                  urlsRef.current.add(url)
                  acc.attachments.set(attachment.id, { ...attachment, url })
                } catch {
                  // The file remains downloadable even when inline preview fails.
                }
              } else if (attachment.name.toLowerCase().endsWith('.json')) {
                try {
                  const raw = await (
                    await getSessionFile(sessionId!, attachment.name)
                  ).text()
                  if (isChartOption(raw)) {
                    acc.charts.set(attachment.name, raw.trim())
                  }
                } catch {
                  // A normal JSON output is still shown as a downloadable file.
                }
              }
            }
            onFilesChanged()
          } else if (event.type === 'interrupt') {
            const clarificationTool = Array.from(acc.tools.values())
              .reverse()
              .find((tool) => tool.name === 'ask_clarification' && !tool.result)
            setHitl({
              taskId: clarificationTool?.id || event.interrupt_id || '',
              question: event.question ?? '请补充信息后继续。',
              clarificationType: event.clarification_type ?? 'missing_info',
              context: event.context ?? undefined,
              options: event.options ?? [],
            })
            flush(acc, 'success')
            setRunning(false)
            return
          } else if (event.done) {
            for (const tool of acc.tools.values()) {
              if (tool.status === 'running') tool.status = 'complete'
            }
            flush(acc, 'success')
            onFilesChanged()
            return
          } else if (event.type === 'error') {
            // 不 throw 中断流：记录错误到当前消息，后续仍可能有事件。
            // errorText 写入 acc，循环内的后续 flush 会保持 error 状态。
            const errContent = event.content || 'Agent 执行失败'
            if (!acc.errorText) acc.errorText = errContent
            flush(acc)
          }
          flush(acc)
        }
        flush(acc, 'success')
      } catch (error: unknown) {
        const aborted = error instanceof DOMException && error.name === 'AbortError'
        setMessages((current) =>
          current.map((message) =>
            message.id === acc.id
              ? {
                  ...message,
                  status: aborted ? 'abort' : 'error',
                  error: aborted
                    ? '已停止生成'
                    : error instanceof Error
                      ? error.message
                      : '生成失败',
                }
              : message,
          ),
        )
      }
    },
    [flush, onFilesChanged, sessionId],
  )

  const send = useCallback(
    async (text: string, files: File[]) => {
      if (!agentId || !sessionId || running || hitl) return
      const trimmed = text.trim()
      if (!trimmed && files.length === 0) return
      setRunning(true)
      const userId = genId()
      const assistantId = genId()
      const attachments: AttachmentView[] = files.map((file) => {
        const url = file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined
        if (url) urlsRef.current.add(url)
        return {
          id: `local:${genId()}`,
          name: file.name,
          contentType: file.type || 'application/octet-stream',
          kind: file.type.startsWith('image/') ? 'image' : 'file',
          url,
          source: 'local',
        }
      })
      const acc: AssistantAccumulator = {
        id: assistantId,
        text: '',
        reasoning: '',
        tools: new Map(),
        attachments: new Map(),
        charts: new Map(),
      }
      accRef.current = acc
      setMessages((current) => [
        ...current,
        {
          id: userId,
          role: 'user',
          text: trimmed,
          reasoning: '',
          tools: [],
          attachments,
          charts: [],
          status: 'success',
          createdAt: new Date(),
        },
        {
          id: assistantId,
          role: 'assistant',
          text: '',
          reasoning: '',
          tools: [],
          attachments: [],
          charts: [],
          status: 'loading',
          createdAt: new Date(),
        },
      ])
      const controller = new AbortController()
      abortRef.current = controller
      try {
        const uploaded = await Promise.all(
          files.map((file) => uploadSessionFile(sessionId, file)),
        )
        setMessages((current) =>
          current.map((message) =>
            message.id === userId
              ? {
                  ...message,
                  attachments: message.attachments.map((attachment, index) => {
                    const stored = uploaded[index]
                    return stored
                      ? {
                          ...attachment,
                          id: stored.id,
                          name: stored.name,
                          contentType: stored.mime,
                          source: 'upload' as const,
                        }
                      : attachment
                  }),
                }
              : message,
          ),
        )
        await process(
          streamMessage(
            agentId,
            sessionId,
            trimmed,
            uploaded.map((file) => file.id),
            uploaded.map((file) => file.path),
            controller.signal,
          ),
          acc,
        )
      } catch (error: unknown) {
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  status: 'error',
                  error: error instanceof Error ? error.message : '附件上传失败',
                }
              : message,
          ),
        )
      } finally {
        setRunning(false)
        abortRef.current = null
      }
    },
    [agentId, hitl, process, running, sessionId],
  )

  const answerClarification = useCallback(
    async (answer: string) => {
      if (!agentId || !sessionId || !hitl || running) return
      const clarificationToolId = hitl.taskId
      const acc = accRef.current ?? {
        id: genId(),
        text: '',
        reasoning: '',
        tools: new Map<string, ToolRun>(),
        attachments: new Map<string, AttachmentView>(),
        charts: new Map<string, string>(),
      }
      if (!accRef.current) {
        accRef.current = acc
        setMessages((current) => [
          ...current,
          {
            id: acc.id,
            role: 'assistant',
            text: '',
            reasoning: '',
            tools: [],
            attachments: [],
            charts: [],
            status: 'loading',
          },
        ])
      }
      const controller = new AbortController()
      abortRef.current = controller
      setMessages((current) =>
        current.map((message) => ({
          ...message,
          tools: message.tools.map((tool) =>
            tool.id === clarificationToolId
              ? { ...tool, result: answer, status: 'complete' as const }
              : tool,
          ),
        })),
      )
      setHitl(null)
      setRunning(true)
      try {
        await process(
          streamConfirmation(agentId, sessionId, answer, controller.signal),
          acc,
        )
      } finally {
        setRunning(false)
        abortRef.current = null
      }
    },
    [agentId, hitl, process, running, sessionId],
  )

  const cancel = useCallback(() => abortRef.current?.abort(), [])

  return {
    messages,
    loading,
    running,
    hitl,
    loadError,
    send,
    cancel,
    answerClarification,
  }
}
