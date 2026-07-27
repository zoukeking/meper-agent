/**
 * AgentTimeline — 共享的 Agent 执行过程渲染组件。
 *
 * 将后端 timeline_entries（thinking/tool_call/tool_result/text）渲染为
 * 可折叠的卡片列表。自管理展开状态，可在 Modal / Drawer / 页面中独立使用。
 *
 * 复用自 chat-panel.tsx 的渲染样式（TimelineEntryCard），供 tasks-page
 * 的 Agent 节点详情 Modal 使用。
 */
import { useState, useMemo } from 'react'
import { Spin } from 'antd'
import {
  BulbOutlined,
  ToolOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons'

/* ─── Types (与 chat-panel 对齐) ─── */

export type TimelineEntryType = 'thinking' | 'tool' | 'text' | 'error' | 'user'

export type ToolStatus = 'pending' | 'running' | 'success' | 'error'

export interface TimelineEntry {
  id: string
  type: TimelineEntryType
  content: string
  toolName?: string
  args?: Record<string, unknown>
  result?: string
  toolStatus?: ToolStatus
  expanded?: boolean
}

/** 后端返回的原始 timeline 条目（snake_case）。 */
export interface TimelineEntryData {
  type: 'thinking' | 'tool_call' | 'tool_result' | 'tool' | 'text' | 'user'
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  /** For tool_result: structured success/error status from backend.
   *  Frontend uses this instead of sniffing "Error" prefix in content. */
  status?: 'success' | 'error'
}

/* ─── Converter: 后端 entries → UI entries ─── */

/** Convert persisted timeline_entries to TimelineEntry[] for UI rendering.
 *  Merges adjacent tool_call + tool_result into a single tool entry. */
function historyEntryToTimeline(entries: TimelineEntryData[]): TimelineEntry[] {
  const result: TimelineEntry[] = []
  const pendingToolCalls = new Map<string, { idx: number; entry: TimelineEntry }>()

  for (let i = 0; i < entries.length; i++) {
    const e = entries[i]

    if (e.type === 'tool_call') {
      const entry: TimelineEntry = {
        id: `h-tc-${i}`,
        type: 'tool',
        content: '',
        toolName: e.tool_name,
        args: e.args,
        toolStatus: 'running',
      }
      const idx = result.length
      result.push(entry)
      pendingToolCalls.set(e.tool_name ?? '', { idx, entry })
    } else if (e.type === 'tool_result') {
      const pending = pendingToolCalls.get(e.tool_name ?? '')
      if (pending) {
        const isError = e.status === 'error'
        result[pending.idx] = {
          ...pending.entry,
          result: e.content,
          toolStatus: isError ? 'error' : 'success',
        }
        pendingToolCalls.delete(e.tool_name ?? '')
      } else {
        result.push({
          id: `h-tr-${i}`,
          type: 'tool',
          content: '',
          toolName: e.tool_name,
          result: e.content,
          toolStatus: 'success',
        })
      }
    } else if (e.type === 'tool') {
      result.push({
        id: `h-tool-${i}`,
        type: 'tool',
        content: '',
        toolName: e.tool_name,
        args: e.args,
        result: e.content,
        toolStatus: 'success',
      })
    } else if (e.type === 'thinking') {
      result.push({ id: `h-think-${i}`, type: 'thinking', content: e.content ?? '' })
    } else if (e.type === 'text') {
      result.push({ id: `h-fa-${i}`, type: 'text', content: e.content ?? '' })
    } else if (e.type === 'user') {
      result.push({ id: `h-user-${i}`, type: 'user', content: e.content ?? '' })
    } else {
      // tool_call_start 等瞬态事件跳过
    }
  }

  return result
}

/* ─── Styles ─── */

const TOOL_STATUS_STYLE: Record<ToolStatus, { color: string; bg: string; border: string; icon: typeof ToolOutlined }> = {
  pending: { color: '#6366F1', bg: '#EEF2FF', border: '#C7D2FE', icon: LoadingOutlined },
  running: { color: '#D97706', bg: '#FFFBEB', border: '#FDE68A', icon: ToolOutlined },
  success: { color: '#059669', bg: '#ECFDF5', border: '#A7F3D0', icon: CheckCircleOutlined },
  error:   { color: '#DC2626', bg: '#FEF2F2', border: '#FECACA', icon: ExclamationCircleOutlined },
}

/* ─── Single entry card (self-contained expand/collapse) ─── */

function TimelineEntryCard({ entry }: { entry: TimelineEntry }) {
  const [expanded, setExpanded] = useState(false)

  switch (entry.type) {
    case 'thinking':
      return (
        <div className="rounded-lg border border-blue-100 bg-blue-50/50 overflow-hidden">
          <button
            onClick={() => setExpanded(!expanded)}
            className="w-full flex items-center gap-2 px-3 py-2 border-0 bg-transparent cursor-pointer text-left hover:bg-blue-50 transition-colors"
          >
            <BulbOutlined className="text-blue-400 text-xs" />
            <span className="text-xs font-medium text-blue-600">思考过程</span>
            <span className="text-[10px] text-blue-300 ml-auto">
              {expanded ? '收起' : '展开'}
            </span>
          </button>
          {expanded && (
            <div className="px-3 pb-2 text-xs text-blue-700 whitespace-pre-wrap leading-relaxed border-t border-blue-100 pt-2">
              {entry.content}
            </div>
          )}
        </div>
      )

    case 'tool': {
      const status = entry.toolStatus ?? 'running'
      const style = TOOL_STATUS_STYLE[status]
      const StatusIcon = style.icon
      const hasDetail = (entry.args && Object.keys(entry.args).length > 0) || entry.result

      return (
        <div className="rounded-lg overflow-hidden" style={{ background: style.bg, borderColor: style.border, borderWidth: 1 }}>
          <button
            onClick={hasDetail ? () => setExpanded(!expanded) : undefined}
            className={`w-full flex items-center gap-2 px-3 py-2 border-0 bg-transparent text-left transition-colors ${hasDetail ? 'cursor-pointer hover:opacity-80' : 'cursor-default'}`}
          >
            {status === 'running' ? (
              <Spin size="small" indicator={<StatusIcon style={{ color: style.color, fontSize: 12 }} spin />} />
            ) : status === 'pending' ? (
              <Spin size="small" indicator={<LoadingOutlined style={{ color: style.color, fontSize: 12 }} spin />} />
            ) : (
              <StatusIcon style={{ color: style.color, fontSize: 12 }} />
            )}
            <span className="text-xs font-medium" style={{ color: style.color }}>
              {status === 'pending' ? '正在生成参数' : (entry.toolName || 'unknown_tool')}
            </span>
            {status === 'running' && (
              <span className="text-[10px] opacity-60" style={{ color: style.color }}>执行中...</span>
            )}
            {hasDetail && (
              <span className="text-[10px] opacity-50 ml-auto" style={{ color: style.color }}>
                {expanded ? '收起' : '详情'}
              </span>
            )}
          </button>
          {expanded && hasDetail && (
            <div className="border-t px-3 pb-2 pt-2" style={{ borderColor: style.border }}>
              {entry.args && Object.keys(entry.args).length > 0 && (
                <div className="mb-2">
                  <div className="text-[10px] font-medium mb-1 opacity-60" style={{ color: style.color }}>请求参数</div>
                  <pre className="text-[11px] whitespace-pre-wrap break-all leading-relaxed rounded p-2 bg-white/60" style={{ color: '#334155' }}>
                    {JSON.stringify(entry.args, null, 2)}
                  </pre>
                </div>
              )}
              {entry.result && (
                <div>
                  <div className="text-[10px] font-medium mb-1 opacity-60" style={{ color: style.color }}>返回结果</div>
                  <pre className="text-[11px] whitespace-pre-wrap break-all leading-relaxed rounded p-2 bg-white/60 max-h-48 overflow-y-auto" style={{ color: '#334155' }}>
                    {entry.result}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      )
    }

    case 'error':
      return (
        <div className="rounded-xl rounded-tl-sm px-4 py-2.5 bg-[#FEF2F2] border border-red-100">
          <div className="text-sm text-[#DC2626]">{entry.content}</div>
        </div>
      )

    case 'user':
      return (
        <div className="rounded-xl rounded-tr-sm px-4 py-2.5 bg-[#F1F5F9] border border-slate-200">
          <div className="text-[10px] text-[#94A3B8] mb-1">用户输入</div>
          <div className="text-sm text-[#0F172A] whitespace-pre-wrap leading-relaxed">{entry.content}</div>
        </div>
      )

    case 'text':
      return (
        <div className="rounded-xl rounded-tl-sm px-4 py-2.5 bg-[#F8FAFC] border border-gray-100">
          <div className="text-sm leading-relaxed text-[#0F172A] whitespace-pre-wrap">{entry.content}</div>
        </div>
      )

    default:
      return null
  }
}

/* ─── Public component: renders a full timeline list ─── */

export interface AgentTimelineProps {
  entries: TimelineEntryData[]
}

/**
 * Render a list of backend timeline entries as collapsible cards.
 * Self-contained — manages its own expand/collapse state.
 */
export default function AgentTimeline({ entries }: AgentTimelineProps) {
  const items = useMemo(() => historyEntryToTimeline(entries), [entries])
  if (items.length === 0) {
    return <div className="text-xs text-[#94A3B8] text-center py-4">暂无执行过程数据</div>
  }

  return (
    <div className="flex flex-col gap-2">
      {items.map((entry) => (
        <TimelineEntryCard key={entry.id} entry={entry} />
      ))}
    </div>
  )
}
