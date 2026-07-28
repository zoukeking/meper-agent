/**
 * TaskFlowTimeline — 执行流程「阶段时间线」视图（流程区默认视图）。
 *
 * 把扁平的 timeline 事件按 node_id 分组为「阶段」，每个节点一行卡片：
 * - 类型图标 + 名称 + 执行状态徽标（已完成/执行中/失败/审批中/待执行）+ 耗时
 * - 点击展开该节点：事件列表（中文标签 + 时间 + actor + 可折叠事件数据 JSON）
 *   + 节点输出（variables[node_id]）
 * - 生命周期里程碑（任务创建/完成/失败/取消）作为首尾节点
 *
 * 保留了旧 TimelineView 的富细节：EVENT_META 中文事件标签、节点类型前缀
 * （如「Agent 节点开始」）、actor 展示、事件 data 的 <details> JSON、审批事件过滤。
 *
 * 数据全部来自现有 timeline + variables + checkpoint，零额外请求、无版本漂移。
 */
import { useState, useMemo, type ReactNode } from 'react'
import {
  PlayCircle, StopCircle, Workflow, Wrench, GitBranch, Split, UserCheck,
  ChevronRight, Clock, CheckCircle2, XCircle, Loader2, CircleDot, Flag,
} from 'lucide-react'
import { tasksApi, taskKeys, type TaskDetail, type TimelineEvent, type NodeTimelineEntry } from '../../services/tasks-api'
import { useQuery } from '@tanstack/react-query'
import { workflowsApi, workflowKeys } from '../../services/workflows-api'
import { getNodeExecState, type NodeExecState, type NodeStageInfo } from './task-flow-utils'
import { DataView } from './DataView'
import { Spin } from '../ui'
import AgentTimeline from './AgentTimeline'

/* ─── 节点类型 → 图标 / 中文标签（与 WorkflowDesigner 的 NODE_TYPE_CONFIGS 对齐） ─── */

const NODE_TYPE_ICON: Record<string, ReactNode> = {
  start: <PlayCircle size={13} strokeWidth={2} />,
  end: <StopCircle size={13} strokeWidth={2} />,
  agent: <Workflow size={13} strokeWidth={2} />,
  tool: <Wrench size={13} strokeWidth={2} />,
  gateway: <GitBranch size={13} strokeWidth={2} />,
  parallel: <Split size={13} strokeWidth={2} />,
  human: <UserCheck size={13} strokeWidth={2} />,
}
const NODE_TYPE_LABEL: Record<string, string> = {
  start: '输入节点', end: '输出节点', agent: 'Agent 节点',
  tool: '工具节点', gateway: '网关节点', parallel: '并行节点', human: '人工审批节点',
}

/* ─── 执行状态 → 徽标（图标 + 颜色 + 文案） ─── */

const STATE_META: Record<NodeExecState, { icon: ReactNode; color: string; label: string }> = {
  completed: { icon: <CheckCircle2 size={12} />, color: '#10B981', label: '已完成' },
  executing: { icon: <Loader2 size={12} className="animate-spin" />, color: '#3B82F6', label: '执行中' },
  failed: { icon: <XCircle size={12} />, color: '#EF4444', label: '失败' },
  waiting: { icon: <CircleDot size={12} />, color: '#8B5CF6', label: '审批中' },
  pending: { icon: <Clock size={12} />, color: '#71717a', label: '待执行' },
}

/* ─── 旧 TimelineView 保留的富事件元数据 ─── */

/** 后端事件类型 → 中文标签 + 颜色 */
const EVENT_META: Record<string, { label: string; color: string }> = {
  // 生命周期
  created: { label: '任务创建', color: '#3B82F6' },
  started: { label: '开始执行', color: '#3B82F6' },
  auto_scheduled: { label: '自动调度', color: '#3B82F6' },
  completed: { label: '任务完成', color: '#10B981' },
  failed: { label: '任务失败', color: '#EF4444' },
  task_failed: { label: '任务失败', color: '#EF4444' },
  cancelled: { label: '任务取消', color: '#94A3B8' },
  // 节点执行
  node_start: { label: '节点开始', color: '#3B82F6' },
  node_complete: { label: '节点完成', color: '#10B981' },
  node_failed: { label: '节点失败', color: '#EF4444' },
  // 人工审批（UI API）
  waiting_human: { label: '等待审批', color: '#8B5CF6' },
  human_node_start: { label: '审批开始', color: '#8B5CF6' },
  approve: { label: '审批通过', color: '#10B981' },
  skip: { label: '审批跳过', color: '#F59E0B' },
  reject: { label: '审批驳回', color: '#EF4444' },
  cancel: { label: '人工取消', color: '#94A3B8' },
  resume: { label: '人工恢复', color: '#3B82F6' },
  // 人工干预（Agent 工具）
  human_approved: { label: '审批通过', color: '#10B981' },
  human_rejected: { label: '审批拒绝', color: '#EF4444' },
  intervene_approve: { label: '人工通过', color: '#10B981' },
  intervene_reject: { label: '人工拒绝', color: '#EF4444' },
  intervene_cancel: { label: '人工取消', color: '#EF4444' },
  intervene_resume: { label: '人工恢复', color: '#3B82F6' },
  intervene_retry: { label: '人工重试', color: '#F59E0B' },
}

/** 审批已完成事件类型集合（用于过滤冗余的 waiting_human / human node_complete） */
const APPROVE_TYPES = new Set([
  'approve', 'skip', 'reject', 'cancel', 'resume',
  'intervene_approve', 'intervene_reject', 'intervene_cancel', 'intervene_resume',
  'human_approved', 'human_rejected',
])

const TERMINAL_LABEL: Record<string, string> = {
  completed: '任务完成',
  failed: '任务失败',
  task_failed: '任务失败',
  cancelled: '任务取消',
}

/** 节点事件 → 中文标签（拼类型前缀，如 "Agent 节点开始"） */
function nodeEventLabel(evt: TimelineEvent): string {
  const meta = EVENT_META[evt.event_type] ?? { label: evt.event_type, color: '#94A3B8' }
  const nodeType = evt.data?.node_type as string | undefined
  const isNodeEvent = !!nodeType && (
    evt.event_type === 'node_start' ||
    evt.event_type === 'node_complete' ||
    evt.event_type === 'node_failed'
  )
  return isNodeEvent
    ? `${NODE_TYPE_LABEL[nodeType] ?? nodeType}${meta.label.replace('节点', '')}`
    : meta.label
}

/**
 * 已被上层消费、展开后纯冗余的事件 data 字段：
 * - node_id / node_type / node_label：阶段卡片的标题、图标、状态徽标已完整展示
 * - output_summary：完整输出已在「节点输出」分区渲染（仅当该节点取不到完整输出时，
 *   output_summary 才作为唯一线索保留，不计入冗余）
 */
const REDUNDANT_EVENT_DATA_KEYS = new Set(['node_id', 'node_type', 'node_label'])

/** 事件 data 是否还有「有信息量」的字段值得展开「详细数据」。 */
function eventHasMeaningfulData(evt: TimelineEvent, nodeOutputExists: boolean): boolean {
  const keys = Object.keys(evt.data ?? {})
  if (keys.length === 0) return false
  return keys.some((k) =>
    k === 'output_summary' ? !nodeOutputExists : !REDUNDANT_EVENT_DATA_KEYS.has(k),
  )
}

export interface TaskFlowTimelineProps {
  task: TaskDetail
  theme?: 'light' | 'dark'
  /** 把 task.workflow_id（可能是 registry id wfr_...）解析为可拉 /workflows/{id} 的模板 id（wf_...），与 TaskFlowGraph 同源 */
  resolveTemplateId?: (maybeRegistryId: string) => string
}

export function TaskFlowTimeline({ task, theme = 'dark', resolveTemplateId }: TaskFlowTimelineProps) {
  const stages = useMemo<NodeStageInfo[]>(() => buildStages(task), [task])
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())

  // 拉工作流定义取节点名（node_id → label）。与 TaskFlowGraph 共用 workflowKeys.detail 缓存，
  // 同一抽屉内不会额外打请求；拉不到（模板被删/解析失败）则回退到类型名。
  const templateId = useMemo(
    () => (resolveTemplateId ? resolveTemplateId(task.workflow_id) : task.workflow_id),
    [task.workflow_id, resolveTemplateId],
  )
  const { data: wf } = useQuery({
    queryKey: workflowKeys.detail(templateId),
    queryFn: () => workflowsApi.get(templateId),
    enabled: !!templateId,
    staleTime: 60_000,
    retry: 1,
  })

  const nodeNameMap = useMemo(() => {
    const map = new Map<string, string>()
    for (const n of wf?.nodes ?? []) {
      if (n.node_id && n.label) map.set(n.node_id, n.label)
    }
    return map
  }, [wf])

  // 生命周期里程碑：创建 / 终态（完成·失败·取消）
  const createdEvt = task.timeline.find((e) => e.event_type === 'created' || e.event_type === 'started')
  const terminalEvt = task.timeline.find((e) =>
    ['completed', 'failed', 'task_failed', 'cancelled'].includes(e.event_type))

  if (stages.length === 0 && !createdEvt) {
    return <div className="text-xs text-[#71717a] italic">暂无执行事件</div>
  }

  const mutedText = theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'
  const subText = theme === 'dark' ? 'text-[#a1a1aa]' : 'text-slate-500'

  return (
    <div className="relative pl-6">
      {/* 竖向连接线 */}
      <div className={`absolute left-[11px] top-2 bottom-2 w-[2px] ${theme === 'dark' ? 'bg-[#27272a]' : 'bg-slate-200'}`} />

      {/* 创建里程碑 */}
      {createdEvt && (
        <Milestone color="#3B82F6" label="任务创建" time={createdEvt.timestamp} subText={subText} />
      )}

      {/* 节点阶段 */}
      {stages.map((stage) => {
        const state = stage.state
        const meta = STATE_META[state]
        const isExpanded = expandedIds.has(stage.nodeId)
        const nodeLabel = nodeNameMap.get(stage.nodeId) || stage.label || (NODE_TYPE_LABEL[stage.nodeType] ?? stage.nodeType)
        const output = task.variables?.[stage.nodeId]
        return (
          <div key={stage.nodeId} className="relative py-1.5">
            {/* 左侧状态圆点 */}
            <div
              className={`absolute -left-6 top-3 w-3 h-3 rounded-full border-2 z-10 flex items-center justify-center ${
                theme === 'dark' ? 'border-[#121214]' : 'border-white'
              }`}
              style={{ backgroundColor: meta.color }}
            />

            {/* 阶段卡片 */}
            <div
              onClick={() =>
                setExpandedIds((prev) => {
                  const next = new Set(prev)
                  if (next.has(stage.nodeId)) next.delete(stage.nodeId)
                  else next.add(stage.nodeId)
                  return next
                })
              }
              className={`rounded-lg border px-3 py-2 transition-colors cursor-pointer ${
                state === 'executing'
                  ? 'border-[#3B82F6]/50 bg-[#3B82F6]/5'
                  : state === 'failed'
                    ? 'border-[#EF4444]/40 bg-[#EF4444]/5'
                    : state === 'waiting'
                      ? 'border-[#8B5CF6]/40 bg-[#8B5CF6]/5'
                      : theme === 'dark'
                        ? 'border-[#27272a] bg-[#18181b] hover:border-[#3f3f46]'
                        : 'border-slate-200 bg-white hover:border-slate-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <span className={`shrink-0 ${subText}`}>{NODE_TYPE_ICON[stage.nodeType] ?? <Flag size={13} />}</span>
                <span className={`text-xs font-medium truncate flex-1 ${theme === 'dark' ? 'text-[#fafafa]' : 'text-slate-800'}`}>{nodeLabel}</span>
                <span
                  className="inline-flex items-center gap-1 text-[10px] font-medium shrink-0"
                  style={{ color: meta.color }}
                >
                  {meta.icon} {meta.label}
                </span>
                <ChevronRight
                  size={13}
                  className={`shrink-0 transition-transform ${isExpanded ? 'rotate-90' : ''} ${mutedText}`}
                />
              </div>

              {/* 耗时 / Token 消耗 */}
              {(stage.duration || stage.tokenTotal) && (
                <div className={`text-[10px] mt-1 font-mono ${mutedText}`}>
                  {stage.duration && <>耗时 {stage.duration}</>}
                  {stage.duration && stage.tokenTotal ? ' · ' : ''}
                  {stage.tokenTotal && <>{stage.tokenTotal.toLocaleString()} tokens</>}
                </div>
              )}

              {/* 展开区：该节点的事件列表 + 输出 */}
              {/* 阻止冒泡到外层卡片 div 的 onClick：否则点击「详细数据」summary
                  会触发 setExpandedId(null) 把整段折叠，导致详情点不开。 */}
              {isExpanded && (
                <div
                  onClick={(e) => e.stopPropagation()}
                  className={`mt-2 pt-2 space-y-2.5 ${theme === 'dark' ? 'border-[#27272a]' : 'border-slate-200'} border-t`}
                >
                  {/* 事件列表（保留旧的中文标签 / actor / 可折叠 data） */}
                  {stage.events.length > 0 && (
                    <div className="space-y-1">
                      {stage.events.map((evt, idx) => {
                        const emeta = EVENT_META[evt.event_type] ?? { label: evt.event_type, color: '#94A3B8' }
                        const label = nodeEventLabel(evt)
                        // 薄字段过滤：node_start/node_complete 的 node_id/node_type 已在阶段卡片
                        // 消费、output_summary 已在「节点输出」分区渲染，仅剩这些的事件不再展开
                        // 详细数据（节点无完整输出时 output_summary 仍保留作唯一线索）。
                        const hasData = eventHasMeaningfulData(evt, output !== undefined)
                        return (
                          <div key={idx} className="text-[10px]">
                            <div className="flex items-baseline gap-1.5 flex-wrap">
                              <span className="font-medium" style={{ color: emeta.color }}>{label}</span>
                              <span className={mutedText}>{fmtTime(evt.timestamp)}</span>
                              {evt.actor && evt.actor !== 'system' && (
                                <span className={mutedText}>· {evt.actor}</span>
                              )}
                            </div>
                            {hasData && (
                              <details open className="mt-1 group rounded-lg border border-[#27272a] bg-[#09090b] overflow-hidden">
                                <summary className={`cursor-pointer list-none flex items-center gap-1 px-2.5 py-1.5 text-[10px] font-medium ${mutedText} hover:text-[#1E5EFF] hover:bg-[#18181b]/40 transition-colors [&::-webkit-details-marker]:hidden`}>
                                  <ChevronRight className="w-3 h-3 shrink-0 group-open:hidden" />
                                  <ChevronRight className="w-3 h-3 shrink-0 hidden group-open:inline rotate-90" />
                                  详细数据
                                </summary>
                                <div className="p-2.5 border-t border-[#27272a]">
                                  <DataView value={evt.data} context="event_data" showRaw={false} />
                                </div>
                              </details>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}

                  {/* 节点输出 */}
                  <div>
                    <div className={`text-[10px] uppercase tracking-wider mb-1 ${mutedText}`}>节点输出</div>
                    {output === undefined ? (
                      <div className={`text-[10px] italic ${mutedText}`}>无</div>
                    ) : (
                      <div className="rounded-lg border border-[#27272a] bg-[#09090b] p-2.5">
                        <DataView value={output} context="node_output" nodeType={stage.nodeType} showRaw={false} />
                      </div>
                    )}
                  </div>

                  {/* Agent 节点执行明细：stage 展开即自动加载（每节点独立缓存，互不干扰） */}
                  {stage.nodeType === 'agent' && state !== 'pending' && (
                    <NodeAgentTrace taskId={task.id} nodeId={stage.nodeId} theme={theme} />
                  )}
                </div>
              )}
            </div>
          </div>
        )
      })}

      {/* 终态里程碑 */}
      {terminalEvt && (
        <Milestone
          color={terminalEvt.event_type === 'completed' ? '#10B981' : terminalEvt.event_type === 'cancelled' ? '#94A3B8' : '#EF4444'}
          label={TERMINAL_LABEL[terminalEvt.event_type] ?? terminalEvt.event_type}
          time={terminalEvt.timestamp}
          subText={subText}
        />
      )}
    </div>
  )
}

/* ─── 子组件：Agent 节点执行明细（挂载即自动加载，每节点独立缓存） ─── */

function NodeAgentTrace({ taskId, nodeId, theme }: { taskId: string; nodeId: string; theme: 'light' | 'dark' }) {
  // 组件仅在 stage 展开 + agent 节点时挂载 → 挂载即拉取；nodeId 维度的缓存键让多个
  // agent 节点各自独立、互不串数据。收起 stage 即卸载，重展开走缓存秒显。
  const { data: nodeTimeline, isLoading, error } = useQuery({
    queryKey: taskKeys.nodeTimeline(taskId, nodeId),
    queryFn: () => tasksApi.getNodeTimeline(taskId, nodeId),
    staleTime: 60_000,
    retry: 1,
  })
  const mutedText = theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'
  const errAny = error as { statusCode?: number; response?: { status?: number }; message?: string } | null
  const errStatus = errAny?.statusCode ?? errAny?.response?.status
  const is404 = errStatus === 404
  return (
    <div onClick={(e) => e.stopPropagation()} className="mt-2">
      <div className={`flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider ${mutedText} mb-1.5`}>
        <Workflow size={11} /> Agent 执行明细
      </div>
      {isLoading ? (
        <div className="flex items-center justify-center py-3">
          <Spin />
        </div>
      ) : error ? (
        <div className={`text-[10px] italic ${theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'}`}>
          {is404 ? '该节点暂无执行记录' : `加载失败：${errAny?.message ?? '请稍后重试'}`}
        </div>
      ) : nodeTimeline?.timeline?.length ? (
        <div className="space-y-1.5">
          {nodeTimeline.message_count ? (
            <div className={`text-[10px] ${mutedText}`}>消息数 {nodeTimeline.message_count}</div>
          ) : null}
          <AgentTimeline entries={nodeTimeline.timeline} theme={theme} />
        </div>
      ) : (
        <div className={`text-[10px] italic ${mutedText}`}>该节点暂无执行记录</div>
      )}
    </div>
  )
}

/* ─── 子组件：生命周期里程碑（首尾） ─── */

function Milestone({ color, label, time, subText }: {
  color: string
  label: string
  time: string
  subText: string
}) {
  return (
    <div className="relative py-2">
      <div
        className="absolute -left-6 top-3 w-3 h-3 rounded-full border-2 border-[#121214] z-10"
        style={{ backgroundColor: color }}
      />
      <div className="flex items-center gap-2">
        <Flag size={12} style={{ color }} />
        <span className="text-xs font-medium" style={{ color }}>{label}</span>
        <span className={`text-[10px] ${subText}`}>{fmtTime(time)}</span>
      </div>
    </div>
  )
}

/* ─── 工具：ISO → 时:分:秒 ─── */

function fmtTime(iso: string): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return iso
  }
}

/* ─── 构建阶段列表 ─── */

/**
 * 把 timeline 按 node_id 聚合成阶段，并推导每个节点的执行状态。
 * 顺序按节点首次出现（node_start）的时间排列。
 *
 * 同时保留旧 TimelineView 的审批事件过滤：若存在任意审批完成事件
 * （approve/skip/reject/...），则过滤掉冗余的 waiting_human 事件。
 */
function buildStages(task: TaskDetail): NodeStageInfo[] {
  const timeline = task.timeline ?? []
  const pausedNode = task.checkpoint?.paused_at_node

  // 审批已完成？若是，过滤掉 waiting_human（旧 APPROVE_TYPES 逻辑）
  const hasAnyApproval = timeline.some((e) => APPROVE_TYPES.has(e.event_type))

  // 收集每个节点的相关事件 + 首次出现顺序
  const order: string[] = []
  const eventsByNode = new Map<string, TimelineEvent[]>()
  const typeByNode = new Map<string, string>()

  for (const evt of timeline) {
    const nodeId = typeof evt.data?.node_id === 'string' ? evt.data.node_id : undefined
    const nodeType = typeof evt.data?.node_type === 'string' ? evt.data.node_type : undefined
    if (!nodeId) continue
    // 审批过滤：存在审批完成事件时，跳过 waiting_human 事件
    if (hasAnyApproval && evt.event_type === 'waiting_human') continue
    if (!eventsByNode.has(nodeId)) {
      eventsByNode.set(nodeId, [])
      order.push(nodeId)
    }
    // 审批过滤：跳过 human 节点的 node_complete（已被审批事件取代）
    if (hasAnyApproval && evt.event_type === 'node_complete' && nodeType === 'human') continue
    eventsByNode.get(nodeId)!.push(evt)
    if (nodeType) typeByNode.set(nodeId, nodeType)
  }

  return order.map((nodeId) => {
    const evts = eventsByNode.get(nodeId)!
    const nodeType = typeByNode.get(nodeId) ?? 'agent'
    const state = getNodeExecState(evts, nodeId === pausedNode)
    const startEvt = evts.find((e) => e.event_type === 'node_start')
    const endEvt = evts.find((e) => e.event_type === 'node_complete' || e.event_type === 'node_failed')
    const duration = (startEvt && endEvt) ? humanDuration(startEvt.timestamp, endEvt.timestamp) : undefined
    const label = typeof evts[0]?.data?.node_label === 'string' ? evts[0].data.node_label as string : ''
    // 节点 token 用量（仅 agent 节点的 node_complete 事件 data.usage 携带）
    const usage = endEvt?.data?.usage as { total_tokens?: number } | undefined
    const tokenTotal = typeof usage?.total_tokens === 'number' ? usage.total_tokens : undefined
    return { nodeId, nodeType, state, events: evts, duration, label, tokenTotal }
  })
}

/** 两个 ISO 时间差 → 人类可读（<1s / Ns / Nm Ns） */
function humanDuration(startIso: string, endIso: string): string {
  const diff = new Date(endIso).getTime() - new Date(startIso).getTime()
  if (diff < 0) return '-'
  if (diff < 1000) return '<1s'
  if (diff < 60000) return `${Math.floor(diff / 1000)}s`
  return `${Math.floor(diff / 60000)}m ${Math.floor((diff % 60000) / 1000)}s`
}

export default TaskFlowTimeline
