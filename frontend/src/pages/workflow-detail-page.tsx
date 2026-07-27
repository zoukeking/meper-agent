/**
 * Workflow Detail / Editor page — covers Stories 4-3, 4-4, 4-5, 4-6.
 *
 * Features:
 * - Dify 风格三栏式可视化 DAG 编辑器（Palette + Canvas + Config Panel）
 * - 拖拽添加/连接节点
 * - 变量选择器（VariableSelector）辅助输入模板变量
 * - Workflow metadata editing (name, description, tags)
 * - Test run (create Task and execute)
 * - Version history display
 * - Publish / Archive lifecycle
 */
import { useState, useCallback, useEffect, useRef, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  Tag,
  Tooltip,
  message,
  Spin,
  Modal,
  Input,
  Drawer,
  Alert,
  Divider,
} from 'antd'
import {
  ArrowLeftOutlined,
  SaveOutlined,
  CloudUploadOutlined,
  StopOutlined,
  PlayCircleOutlined,
  HistoryOutlined,
  ExclamationCircleOutlined,
  EyeOutlined,
  FileOutlined,
  ClockCircleOutlined,
} from '@ant-design/icons'
import {
  workflowsApi,
  workflowKeys,
  type WorkflowNode,
  type WorkflowEdge,
} from '../services/workflows-api'
import { useAuthStore } from '../stores/auth-store'
import { parseBackendDate } from '../lib/format'
import { tasksApi, type TaskOutputFile } from '../services/tasks-api'
import FileDownloadButton from '../components/file-download-button'
import FilePreview from '../components/file-preview'
import { getPreviewKind } from '../lib/file-preview'
import { wsClient } from '../lib/ws-client'

/* ─── Workflow Editor 组件 ─── */
import WorkflowCanvas from '../features/workflow-editor/WorkflowCanvas'
import WorkflowNodePalette from '../features/workflow-editor/WorkflowNodePalette'
import WorkflowNodeConfigPanel from '../features/workflow-editor/WorkflowNodeConfigPanel'
import { validateWorkflow } from '../features/workflow-editor/utils/workflow-validator'
import { deriveXyflowEdgesFromNodes } from '../features/workflow-editor/utils/canvas-converters'
import type { VariableDefinition } from '../features/workflow-editor/utils/variable-types'

/* ─── VariableFormField (提取到独立文件) ─── */
import VariableFormField from '../features/workflow-editor/VariableFormField'

/* ─── Trigger 配置 Modal ─── */
import TriggerConfigModal from '../components/workflows/TriggerConfigModal'
import { WorkflowTriggerAPI } from '../services/workflow-trigger-api'

/* ─── Cron 表达式 → 人类可读摘要 ─── */
const WEEKDAY_NAMES = ['日', '一', '二', '三', '四', '五', '六']
function cronToSummary(cron: string): string {
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return cron

  const [minStr, hourStr, domStr, , dowStr] = parts

  // 每小时: "{m} * * * *"
  if (domStr === '*' && dowStr === '*' && hourStr === '*') {
    return `每小时第 ${minStr} 分钟`
  }
  // 每天: "{m} {h} * * *"
  if (domStr === '*' && dowStr === '*') {
    return `每天 ${hourStr.padStart(2, '0')}:${minStr.padStart(2, '0')}`
  }
  // 每月: "{m} {h} {d} * *"
  if (domStr !== '*' && dowStr === '*') {
    return `每月 ${domStr} 号 ${hourStr.padStart(2, '0')}:${minStr.padStart(2, '0')}`
  }
  // 每周: "{m} {h} * * {d1,d2,...}"
  if (domStr === '*' && dowStr !== '*') {
    const days = dowStr.split(',').map(Number)
      .map((d) => WEEKDAY_NAMES[d % 7])
      .join('、')
    return `每周${days} ${hourStr.padStart(2, '0')}:${minStr.padStart(2, '0')}`
  }

  // 自定义/无法解析 → 返回原始 cron
  return cron
}

/* ─── Test Run Modal ─── */
function TestRunModal({ workflowId, workflowName, nodes, open, onClose }: {
  workflowId: string
  workflowName: string
  nodes: WorkflowNode[]
  open: boolean
  onClose: () => void
}) {
  // 从 start 节点提取变量定义
  const startNode = nodes.find((n) => n.type === 'start')
  const variables = useMemo<VariableDefinition[]>(
    () => (startNode?.config?.output_variables as VariableDefinition[]) ?? [],
    [startNode],
  )
  const hasDefinedVars = variables.length > 0

  const [formValues, setFormValues] = useState<Record<string, unknown>>({})
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<{ taskId: string; status: string; output?: Record<string, unknown> | null; error?: string | null } | null>(null)

  // 当 modal 打开时重置表单状态（延迟到微任务避免 effect 内同步 setState 导致的级联渲染）
  useEffect(() => {
    if (open) {
      queueMicrotask(() => {
        const defaults: Record<string, unknown> = {}
        for (const v of variables) {
          if (v.constraints?.default_value !== undefined && v.constraints?.default_value !== null) {
            defaults[v.name] = v.constraints.default_value
          }
        }
        setFormValues(defaults)
        setResult(null)
        setRunning(false)
      })
    }
  }, [open, variables])

  const setValue = useCallback((name: string, val: unknown) => {
    setFormValues((prev) => ({ ...prev, [name]: val }))
  }, [])

  const handleRun = async () => {
    // 构造输入 JSON
    const parsedInput: Record<string, unknown> = {}
    for (const v of variables) {
      const val = formValues[v.name]
      const isEmpty = val === undefined || val === null || val === '' || (Array.isArray(val) && val.length === 0)
      if (!isEmpty) {
        parsedInput[v.name] = val
      } else if (v.constraints?.required) {
        message.warning(`请填写必填项: ${v.label || v.name}`)
        return
      }
    }

    // 1. 先验证工作流结构（不改变任何 UI 状态）
    let validation
    try {
      validation = await workflowsApi.validate(workflowId)
    } catch {
      message.error('验证失败，请稍后重试')
      return
    }
    if (!validation.is_valid) {
      const errorMessages = validation.issues
        .filter((i) => i.severity === 'error')
        .map((i) => i.message)
        .join('\n')
      message.error(`工作流存在 ${validation.error_count} 个错误，请先修复：\n${errorMessages}`)
      return
    }
    if (validation.warning_count > 0) {
      const warnMessages = validation.issues
        .filter((i) => i.severity === 'warning')
        .map((i) => i.message)
        .join('\n')
      message.warning(`工作流有 ${validation.warning_count} 个警告：\n${warnMessages}`)
    }

    // 2. 验证通过，开始执行
    setRunning(true)
    setResult(null)
    try {
      const { tasksApi } = await import('../services/tasks-api')
      const task = await tasksApi.create({
        workflow_id: workflowId,
        input: parsedInput,
      })
      setResult({ taskId: task.id, status: task.status })
      message.success('任务创建成功，正在执行...')

      // Wait for task completion via WS + HTTP fallback
      await waitForTask(task.id)

      // Fetch final result data
      try {
        const finalTask = await tasksApi.get(task.id)
        setResult(prev => prev ? {
          ...prev,
          status: finalTask.status,
          output: finalTask.output ?? null,
          error: finalTask.error?.error_message ?? null,
        } : prev)
      } catch { /* ignore */ }
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '运行失败'
      message.error(msg)
    } finally {
      setRunning(false)
    }
  }

  // Wait for task terminal state via WebSocket + HTTP fallback
  const waitForTask = useCallback(async (taskId: string) => {
    const { tasksApi: api } = await import('../services/tasks-api')
    const terminalStates = ['completed', 'failed', 'cancelled']

    return new Promise<void>((resolve) => {
      let done = false
      const finish = () => {
        if (done) return
        done = true
        unsub()
        clearInterval(intervalId)
        resolve()
      }

      // Primary: WebSocket listener
      const unsub = wsClient.on('task_status', (data: unknown) => {
        const d = data as { task_id: string; status: string }
        if (d.task_id === taskId && terminalStates.includes(d.status)) {
          finish()
        }
      })

      // Fallback: HTTP poll every 5s (less aggressive since WS is primary)
      const intervalId = setInterval(async () => {
        try {
          const task = await api.get(taskId)
          setResult(prev => prev ? { ...prev, status: task.status, output: task.output ?? null, error: task.error?.error_message ?? null } : prev)
          if (terminalStates.includes(task.status)) {
            finish()
          }
        } catch { /* ignore */ }
      }, 5_000)
    })
  }, [])

  const isTerminal = result && ['completed', 'failed', 'cancelled'].includes(result.status)

  return (
    <Modal
      title={`测试运行: ${workflowName}`}
      open={open}
      onCancel={() => { onClose(); setResult(null) }}
      onOk={handleRun}
      okText="运行"
      cancelText="关闭"
      confirmLoading={running}
      width={520}
    >
      <div className="py-2 space-y-3">
        {/* 动态表单区域 */}
        {hasDefinedVars ? (
          <div className="space-y-3">
            {variables.map((v: VariableDefinition) => (
              <VariableFormField
                key={v.name}
                variable={v}
                value={formValues[v.name]}
                onChange={(val) => setValue(v.name, val)}
                disabled={running}
              />
            ))}
          </div>
        ) : (
          <div>
            <label className="block text-sm text-[#0F172A] mb-1">输入参数 (JSON)</label>
            <Input.TextArea
              value={
                Object.keys(formValues).length > 0
                  ? JSON.stringify(formValues, null, 2)
                  : '{}'
              }
              onChange={(e) => {
                try { setFormValues(JSON.parse(e.target.value || '{}')) }
                catch { /* 输入非法时不更新 */ }
              }}
              rows={5}
              className="font-mono text-sm"
              placeholder='{"key": "value"}'
            />
            <p className="text-[10px] text-[#94A3B8] mt-1">
              开始节点未定义变量，请手动输入 JSON
            </p>
          </div>
        )}

        {/* 已构造的 JSON 预览 */}
        {hasDefinedVars && (
          <details className="text-[10px]">
            <summary className="text-[#94A3B8] cursor-pointer hover:text-[#64748B]">
              查看 JSON
            </summary>
            <pre className="mt-1 p-2 bg-gray-50 rounded text-[10px] font-mono max-h-20 overflow-auto">
              {JSON.stringify(
                Object.fromEntries(
                  Object.entries(formValues).filter(
                    ([, v]) => v !== undefined && v !== null && v !== '',
                  ),
                ),
                null,
                2,
              )}
            </pre>
          </details>
        )}

        {/* 结果展示 */}
        {result && (
          <Alert
            type={result.status === 'completed' ? 'success' : result.status === 'failed' ? 'error' : 'info'}
            showIcon
            message={isTerminal ? (result.status === 'completed' ? '执行完成' : '执行失败') : '正在执行...'}
            description={
              <div className="space-y-1">
                <div>
                  <span className="text-[#64748B]">任务 ID: </span>
                  <span className="font-mono text-xs">{result.taskId}</span>
                  <span className="ml-3 text-[#64748B]">状态: </span>
                  <Tag color={result.status === 'completed' ? 'green' : result.status === 'failed' ? 'red' : 'blue'}>
                    {result.status}
                  </Tag>
                </div>
                {result.error && (
                  <div className="text-xs text-red-500">{result.error}</div>
                )}
                {result.output && isTerminal && (
                  <div className="mt-2">
                    <span className="text-[#64748B] text-xs">输出:</span>
                    <pre className="text-[10px] bg-gray-50 rounded p-2 mt-1 max-h-40 overflow-auto font-mono">
                      {JSON.stringify(result.output, null, 2)}
                    </pre>
                  </div>
                )}

                {/* Story 4-15-UI: 输出文件列表（Agent 节点产物，仅 completed 状态拉取） */}
                {result.status === 'completed' && (
                  <TaskOutputFilesInline taskId={result.taskId} />
                )}
              </div>
            }
          />
        )}
      </div>
    </Modal>
  )
}

/* ─── Version History Drawer ─── */
function VersionHistoryDrawer({ workflowId, open, onClose }: { workflowId: string; open: boolean; onClose: () => void }) {
  const { data: workflow, isLoading } = useQuery({
    queryKey: workflowKeys.detail(workflowId),
    queryFn: () => workflowsApi.get(workflowId),
    enabled: open && !!workflowId,
  })

  return (
    <Drawer
      title="版本历史"
      open={open}
      onClose={onClose}
      width={400}
    >
      {isLoading ? (
        <div className="flex items-center justify-center py-20"><Spin /></div>
      ) : workflow ? (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <span className="text-sm text-[#64748B]">当前版本</span>
            <Tag color="blue" className="!m-0">v{workflow.version}</Tag>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm text-[#64748B]">状态</span>
            <Tag className="!m-0">{workflow.status}</Tag>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm text-[#64748B]">节点数</span>
            <span className="text-sm">{(workflow.nodes ?? []).length}</span>
          </div>
          <Divider className="!my-3" />
          <div className="text-xs text-[#94A3B8]">
            <div>创建时间: {parseBackendDate(workflow.created_at).toLocaleString('zh-CN')}</div>
            <div className="mt-1">更新时间: {parseBackendDate(workflow.updated_at).toLocaleString('zh-CN')}</div>
          </div>
          <Divider className="!my-3" />
          <div className="text-xs text-[#94A3B8]">
            版本快照和回滚功能将在后续版本中实现。当前仅展示最新版本信息。
          </div>
        </div>
      ) : null}
    </Drawer>
  )
}


/* ═══════════════════════════════════════════════════════════════
   Main Page Component
   ═══════════════════════════════════════════════════════════════ */
export default function WorkflowDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  /* ─── Test run state ─── */
  const [testRunOpen, setTestRunOpen] = useState(false)

  /* ─── Version history state ─── */
  const [versionOpen, setVersionOpen] = useState(false)

  /* ─── Trigger config state ─── */
  const [triggerOpen, setTriggerOpen] = useState(false)
  const [triggerEnabled, setTriggerEnabled] = useState(false)
  const [, setTriggerSummary] = useState('')
  const [, setTriggerCron] = useState('')

  /* ─── Editing state ─── */
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editNodes, setEditNodes] = useState<WorkflowNode[]>([])
  const [editTags, setEditTags] = useState('')
  const [hasChanges, setHasChanges] = useState(false)
  const [saving, setSaving] = useState(false)
  const [canvasKey, setCanvasKey] = useState(0)

  /* ─── Selected node ID for editing ─── */
  // 只存储 ID，节点数据从 editNodes 中派生（避免 ReactFlow 替换节点对象时丢失选中状态）
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const selectedNode = useMemo(
    () => editNodes.find((n) => n.node_id === selectedNodeId) ?? null,
    [editNodes, selectedNodeId],
  )

  /* ─── Query ─── */
  const { data: workflow, isLoading, isError } = useQuery({
    queryKey: workflowKeys.detail(id ?? ''),
    queryFn: () => workflowsApi.get(id!),
    enabled: !!id,
  })

  /* ─── Initialize local state from server data ─── */
  // 使用 ref 标记是否已初始化，避免 refetch 覆盖编辑状态
  const initializedRef = useRef(false)
  useEffect(() => {
    if (workflow && !initializedRef.current) {
      initializedRef.current = true
      setEditName(workflow.name)
      setEditDesc(workflow.description)
      setEditNodes(workflow.nodes ?? [])
      setEditTags((workflow.tags ?? []).join(', '))
      setCanvasKey((k) => k + 1)
    }
  }, [workflow])

  /* ─── Load trigger enabled status ─── */
  useEffect(() => {
    if (!id) return
    WorkflowTriggerAPI.getTrigger(id).then((config) => {
      setTriggerEnabled(config.enabled)
      if (config.type === 'cron' && config.cron_expression) {
        setTriggerSummary(cronToSummary(config.cron_expression))
        setTriggerCron(config.cron_expression)
      } else if (config.type === 'once' && config.execute_at) {
        setTriggerSummary(`一次性: ${new Date(config.execute_at).toLocaleString('zh-CN')}`)
        setTriggerCron('')
      } else {
        setTriggerSummary('')
        setTriggerCron('')
      }
    }).catch(() => {
      setTriggerEnabled(false)
      setTriggerSummary('')
      setTriggerCron('')
    })
  }, [id])

  /* ─── Mutations ─── */
  const updateMutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => workflowsApi.update(id!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: workflowKeys.detail(id!) })
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
      setHasChanges(false)
    },
  })

  /* ─── Warn on close/refresh when unsaved ─── */
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (hasChanges) {
        e.preventDefault()
      }
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [hasChanges])

  /* ─── Auto-save with debounce ─── */
  // 参考 Dify 实现：每次变更后自动保存，无需手动点击
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const isAutoSavingRef = useRef(false)
  // 使用 ref 保持最新数据，避免闭包过期
  const autoSaveDataRef = useRef({ name: editName, desc: editDesc, nodes: editNodes, tags: editTags })
  useEffect(() => {
    autoSaveDataRef.current = { name: editName, desc: editDesc, nodes: editNodes, tags: editTags }
  })

  useEffect(() => {
    // 初始加载未完成 或 无变更 时不触发自动保存
    if (!initializedRef.current || !hasChanges || !id) return

    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current)
    }

    autoSaveTimerRef.current = setTimeout(async () => {
      if (isAutoSavingRef.current) return
      isAutoSavingRef.current = true
      try {
        const { name, desc, nodes, tags } = autoSaveDataRef.current
        await workflowsApi.update(id!, {
          name,
          description: desc,
          nodes,
          tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        } as Record<string, unknown>)
        queryClient.invalidateQueries({ queryKey: workflowKeys.detail(id!) })
        queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
        setHasChanges(false)
      } catch {
        // 自动保存静默失败，下次变更会重试
      } finally {
        isAutoSavingRef.current = false
      }
    }, 2000)

    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current)
      }
    }
  }, [hasChanges, id, queryClient])

  /* ─── Save on unmount ─── */
  const unmountSavedRef = useRef(false)
  useEffect(() => {
    return () => {
      if (hasChanges && initializedRef.current && !unmountSavedRef.current) {
        unmountSavedRef.current = true
        const { name, desc, nodes, tags } = autoSaveDataRef.current
        const token = useAuthStore.getState().accessToken
        // 使用 keepalive 确保页面关闭时请求能发出
        fetch(`/api/v1/workflows/${encodeURIComponent(id!)}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          keepalive: true,
          body: JSON.stringify({
            name,
            description: desc,
            nodes,
            tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
          }),
        }).catch(() => {
          // 静默失败
        })
      }
    }
  }, [hasChanges, id])

  const publishMutation = useMutation({
    mutationFn: () => workflowsApi.publish(id!),
    onSuccess: () => {
      message.success('工作流已发布')
      queryClient.invalidateQueries({ queryKey: workflowKeys.detail(id!) })
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
    },
  })

  const archiveMutation = useMutation({
    mutationFn: () => workflowsApi.archive(id!),
    onSuccess: () => {
      message.success('工作流已归档')
      queryClient.invalidateQueries({ queryKey: workflowKeys.detail(id!) })
      queryClient.invalidateQueries({ queryKey: workflowKeys.lists() })
    },
  })

  /* ─── Publish handler with validation ─── */
  const handlePublish = () => {
    // 1. 从节点的 next_nodes / gateway / parallel config 推导出边
    const derivedEdges = deriveXyflowEdgesFromNodes(editNodes)

    // 2. 运行前端验证（使用推导边而非空数组）
    const validation = validateWorkflow(editNodes, derivedEdges as unknown as WorkflowEdge[], hasChanges)

    if (!validation.valid || validation.warnings.length > 0) {
      const hasErrors = !validation.valid

      Modal.confirm({
        title: hasErrors ? '工作流验证失败' : '工作流验证警告',
        content: (
          <div className="max-h-96 overflow-y-auto">
            {validation.errors.length > 0 && (
              <div className="mb-4">
                <div className="text-sm font-semibold text-red-600 mb-2">
                  错误 ({validation.errors.length})
                </div>
                {validation.errors.map((err) => (
                  <div key={err.id} className="text-sm text-red-500 mb-1">
                    • {err.message}
                  </div>
                ))}
              </div>
            )}
            {validation.warnings.length > 0 && (
              <div>
                <div className="text-sm font-semibold text-orange-600 mb-2">
                  警告 ({validation.warnings.length})
                </div>
                {validation.warnings.map((warn) => (
                  <div key={warn.id} className="text-sm text-orange-500 mb-1">
                    • {warn.message}
                  </div>
                ))}
              </div>
            )}
          </div>
        ),
        okText: hasErrors ? '修复后重试' : '忽略警告发布',
        cancelText: '取消',
        okButtonProps: hasErrors ? { disabled: true } : { danger: true },
        onOk: () => {
          if (hasErrors) return
          // 忽略警告，继续发布
          publishMutation.mutate()
        },
      })
      return
    }

    // 2. 验证通过，直接发布
    publishMutation.mutate()
  }

  /* ─── Save handler ─── */
  const handleSave = () => {
    // 清除待处理的自动保存
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current)
    }
    setSaving(true)
    const data: Record<string, unknown> = {
      name: editName,
      description: editDesc,
      nodes: editNodes,
      tags: editTags.split(',').map((t) => t.trim()).filter(Boolean),
    }
    updateMutation.mutate(data, {
      onSettled: () => setSaving(false),
      onSuccess: () => { message.success('已保存') },
    })
  }

  /* ─── Node operations ─── */
  const updateNode = useCallback((updated: WorkflowNode) => {
    setEditNodes((prev) => prev.map((n) => n.node_id === updated.node_id ? updated : n))
    // selectedNodeId 不变，selectedNode 会从 editNodes 自动派生最新数据
    setHasChanges(true)
  }, [])

  /* ─── Canvas 同步回调 ─── */
  const handleCanvasNodesChange = useCallback((nodes: WorkflowNode[]) => {
    setEditNodes(nodes)
    setHasChanges(true)
  }, [])

  const handleSelectNode = useCallback((node: WorkflowNode | null) => {
    setSelectedNodeId(node?.node_id ?? null)
  }, [])

  const handleDeleteNode = useCallback((nodeId: string) => {
    // 删除节点：移除该节点，并清理其他节点中引用该节点的 next_nodes
    setEditNodes((prev) => {
      const filtered = prev.filter((n) => n.node_id !== nodeId)
      // 清理其他节点中引用被删除节点的 next_nodes
      return filtered.map((n) => {
        const config = { ...(n.config ?? {}) } as Record<string, unknown>
        const nextNodes = config.next_nodes as Array<{ target: string }> | undefined
        if (nextNodes && Array.isArray(nextNodes)) {
          const cleaned = nextNodes.filter((nn) => nn.target !== nodeId)
          if (cleaned.length !== nextNodes.length) {
            config.next_nodes = cleaned
            return { ...n, config }
          }
        }
        return n
      })
    })
    setSelectedNodeId((prev) => prev === nodeId ? null : prev)
    setHasChanges(true)
  }, [])

  /* ─── Loading / Error states ─── */
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-32">
        <Spin size="large" tip="加载工作流..." />
      </div>
    )
  }

  if (isError || !workflow) {
    return (
      <div className="flex flex-col items-center justify-center py-32 text-[#94A3B8]">
        <ExclamationCircleOutlined className="text-4xl mb-3" />
        <p className="text-sm">工作流不存在或加载失败</p>
        <Button className="mt-4" onClick={() => navigate('/workflows')}>返回列表</Button>
      </div>
    )
  }

  const statusStyles: Record<string, { label: string; color: string; bg: string }> = {
    draft: { label: '草稿', color: '#F59E0B', bg: '#FEF3C7' },
    published: { label: '已发布', color: '#10B981', bg: '#D1FAE5' },
    archived: { label: '已归档', color: '#94A3B8', bg: '#F1F5F9' },
  }
  const ss = statusStyles[workflow.status] ?? statusStyles.draft

  return (
    <div className="animate-[fadeIn_0.3s_ease-out] flex flex-col -m-6 h-[calc(100%+48px)] overflow-hidden">
      {/* ── Header ── */}
      <div className="flex items-center justify-between px-6 pt-4 pb-2 shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/workflows')}
            className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-lg text-[#94A3B8] hover:text-[#0F172A] hover:bg-gray-100 transition-colors text-sm cursor-pointer"
          ><ArrowLeftOutlined /></button>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={editName}
                onChange={(e) => { setEditName(e.target.value); setHasChanges(true) }}
                className="text-lg font-semibold text-[#0F172A] bg-transparent border-0 border-b border-transparent hover:border-gray-300 focus:border-[#3B82F6] focus:outline-none px-0 py-0.5 transition-colors min-w-[120px]"
                placeholder="工作流名称"
              />
              <Tag className="!m-0 !px-2 !py-0.5 !text-xs !rounded shrink-0" style={{ color: ss.color, background: ss.bg, borderColor: 'transparent' }}>
                {ss.label}
              </Tag>
              <Tag className="!m-0 !text-xs shrink-0">v{workflow.version}</Tag>
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <input
                type="text"
                value={editDesc}
                onChange={(e) => { setEditDesc(e.target.value); setHasChanges(true) }}
                className="text-xs text-[#64748B] bg-transparent border-0 border-b border-transparent hover:border-gray-300 focus:border-[#3B82F6] focus:outline-none px-0 py-0.5 transition-colors min-w-[200px] flex-1"
                placeholder="添加工作流描述..."
              />
              <span className="text-[10px] text-[#94A3B8] shrink-0">{workflow.id}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {(workflow.status === 'draft' || workflow.status === 'archived') && (
            <Button icon={<CloudUploadOutlined />} onClick={handlePublish} loading={publishMutation.isPending}>
              发布
            </Button>
          )}
          {workflow.status === 'published' && (
            <Button icon={<StopOutlined />} onClick={() => archiveMutation.mutate()} loading={archiveMutation.isPending}>
              归档
            </Button>
          )}
          <Button icon={<PlayCircleOutlined />} type="primary" onClick={() => setTestRunOpen(true)}>
            测试运行
          </Button>
          <Button
            icon={<ClockCircleOutlined />}
            onClick={() => setTriggerOpen(true)}
            className={triggerEnabled ? '!border-green-400 !text-green-600' : ''}
          >
            定时触发{triggerEnabled && ' ✓'}
          </Button>
          <Button icon={<HistoryOutlined />} onClick={() => setVersionOpen(true)}>
            版本
          </Button>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            onClick={handleSave}
            loading={saving}
            disabled={!hasChanges}
          >
            保存
          </Button>
        </div>
      </div>

      {/* ── 编辑器区域 ── */}
      <div className="flex-1 min-h-0 px-6 pb-4">
        <div className="h-full border border-gray-200 rounded-xl overflow-hidden flex flex-col">
          <div className="flex items-center px-4 py-2 bg-white border-b border-gray-200 shrink-0">
            <span className="text-xs text-[#0F172A]">
              节点 ({editNodes.length})
            </span>
            <Tooltip title="从左侧 Palette 拖拽节点到画布，连接 Handle 创建路由">
              <span className="text-[10px] text-[#94A3B8] cursor-help ml-2">
                拖拽添加 · 点击编辑
              </span>
            </Tooltip>
          </div>
          <div className="flex-1 flex min-h-0">
            {/* 左侧 Palette */}
            <div className="w-[200px] shrink-0">
              <WorkflowNodePalette />
            </div>

            {/* 中间 Canvas */}
            <div className="flex-1 bg-[#F8FAFC]">
              <WorkflowCanvas
                key={canvasKey}
                workflowNodes={editNodes}
                selectedNodeId={selectedNodeId}
                onNodesChange={handleCanvasNodesChange}
                onSelectNode={handleSelectNode}
              />
            </div>

            {/* 右侧 Config Panel */}
            <div className="w-[360px] shrink-0 border-l border-gray-200 overflow-y-auto bg-white">
              <div className="p-4">
                <WorkflowNodeConfigPanel
                  key={selectedNodeId ?? 'none'}
                  selectedNode={selectedNode}
                  allNodes={editNodes}
                  onNodeChange={updateNode}
                  onNodeDelete={handleDeleteNode}
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ── Test Run Modal ── */}
      <TestRunModal
        workflowId={workflow.id}
        workflowName={workflow.name}
        nodes={editNodes}
        open={testRunOpen}
        onClose={() => setTestRunOpen(false)}
      />

      {/* ── Version History Drawer ── */}
      <VersionHistoryDrawer
        workflowId={workflow.id}
        open={versionOpen}
        onClose={() => setVersionOpen(false)}
      />

      {/* ── Trigger Config Modal ── */}
      <TriggerConfigModal
        workflowId={workflow.id}
        workflowName={workflow.name}
        nodes={editNodes}
        open={triggerOpen}
        onClose={() => {
          setTriggerOpen(false)
          // 重新加载触发状态
          WorkflowTriggerAPI.getTrigger(workflow.id).then((config) => {
            setTriggerEnabled(config.enabled)
            if (config.type === 'cron' && config.cron_expression) {
              setTriggerSummary(cronToSummary(config.cron_expression))
              setTriggerCron(config.cron_expression)
            } else if (config.type === 'once' && config.execute_at) {
              setTriggerSummary(`一次性: ${new Date(config.execute_at).toLocaleString('zh-CN')}`)
              setTriggerCron('')
            } else {
              setTriggerSummary('')
              setTriggerCron('')
            }
          }).catch(() => {
            setTriggerEnabled(false)
            setTriggerSummary('')
            setTriggerCron('')
          })
        }}
      />

    </div>
  )
}

/**
 * TaskOutputFilesInline — 列出 task 的输出文件，每项支持就地折叠预览。
 *
 * 与 TaskResultCard 中的 TaskOutputFiles 同构（plan v2 决策：不复用公共组件）。
 * Story 4-15-UI
 */
function TaskOutputFilesInline({ taskId }: { taskId: string }) {
  const [files, setFiles] = useState<TaskOutputFile[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        const list = await tasksApi.listOutputs(taskId)
        if (!cancelled) setFiles(list)
      } catch (err) {
        console.error('[list_task_outputs]', err)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [taskId])

  if (loading) {
    return (
      <div className="mt-2 text-xs text-[#64748B]">
        <Spin size="small" className="mr-1" />
        加载输出文件…
      </div>
    )
  }

  if (files.length === 0) return null

  return (
    <div className="mt-2">
      <span className="text-[#64748B] text-xs">输出文件 ({files.length}):</span>
      <ul className="mt-1 space-y-1">
        {files.map(f => {
          const previewable = getPreviewKind(f.mime_type) !== 'none'
          const expanded = expandedId === f._id
          return (
            <li
              key={f._id}
              className="bg-gray-50 rounded px-2 py-1.5 text-xs border border-gray-200"
            >
              <div className="flex items-center gap-2">
                <FileOutlined className="text-gray-500" />
                <span
                  className="flex-1 truncate text-gray-700"
                  title={f.name}
                >
                  {f.name}
                </span>
                <span className="text-gray-400">{formatSize(f.size)}</span>
                {previewable && (
                  <Button
                    size="small"
                    type="text"
                    icon={<EyeOutlined />}
                    onClick={() =>
                      setExpandedId(expanded ? null : f._id)
                    }
                  >
                    {expanded ? '收起' : '预览'}
                  </Button>
                )}
                <FileDownloadButton fileId={f._id} filename={f.name} />
              </div>
              {expanded && (
                <div className="mt-1">
                  <FilePreview
                    fileId={f._id}
                    filename={f.name}
                    mime={f.mime_type}
                  />
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** 本地小工具 — 字节数转人类可读字符串 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
