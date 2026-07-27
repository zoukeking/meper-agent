/**
 * AgentConfigForm — pure form for Agent configuration (no Drawer shell).
 *
 * Extracted from agent-config-drawer.tsx so it can be embedded in
 * both the Drawer (for quick create) and the detail page tab.
 *
 * Exposes a `submit()` method via `ref` and reports `dirty` state
 * so the parent can render its own save button.
 */
import { useState, useEffect, useImperativeHandle, forwardRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Input, InputNumber, Select, Button, Collapse, Tag, message, Modal,
  Spin, Empty,
} from 'antd'
import {
  CloudUploadOutlined,
  StopOutlined,
  EyeOutlined,
} from '@ant-design/icons'
import {
  agentApi,
  agentKeys,
  type Agent,
  type PreviewResponse,
  type ToolPreview,
} from '../services/agent-api'
import {
  modelApi,
  modelKeys,
  type Model,
} from '../services/model-api'
import { AGENT_STATUS_STYLES } from '../constants/agent-status'
import { FIXED_SLOTS } from '../constants/prompt-slots'
import ToolSelector, {
  DEFAULT_TOOL_VALUE,
  type ToolSelectorValue,
} from './tool-selector'
import SlotValueEditor from './slot-value-editor'
import { parseBackendDate } from '../lib/format'

/* ─── Status styles ─── */
const STATUS_STYLES = AGENT_STATUS_STYLES

/* ─── Public handle ─── */
export interface AgentConfigFormHandle {
  /** Trigger the underlying save mutation. */
  submit: () => void
  /** Whether there is an ongoing save. */
  isSaving: () => boolean
}

/* ─── Props ─── */
export interface AgentConfigFormProps {
  agent: Agent | null
  mode: 'create' | 'edit'
  /** Called after a successful save/create. */
  onSaved?: () => void
  /** Called whenever the save mutation's pending state changes. */
  onSavingChange?: (saving: boolean) => void
}

const AgentConfigForm = forwardRef<AgentConfigFormHandle, AgentConfigFormProps>(
  function AgentConfigForm({ agent, mode, onSaved, onSavingChange }, ref) {
    const queryClient = useQueryClient()
    const isEdit = mode === 'edit' && agent !== null

    /* ─── Form state ─── */
    const [formName, setFormName] = useState('')
    const [formDesc, setFormDesc] = useState('')
    const [formPromptSlots, setFormPromptSlots] = useState<Record<string, string>>({})
    const [formModelId, setFormModelId] = useState('')
    const [formMaxRetry, setFormMaxRetry] = useState(3)
    const [formMaxTokens, setFormMaxTokens] = useState(0)
    const [toolConfig, setToolConfig] = useState<ToolSelectorValue>(DEFAULT_TOOL_VALUE)
    const [previewOpen, setPreviewOpen] = useState(false)
    const [previewData, setPreviewData] = useState<PreviewResponse | null>(null)
    const [previewLoading, setPreviewLoading] = useState(false)

    /* ─── Populate form when agent changes ─── */
    useEffect(() => {
      if (agent && isEdit) {
        setFormName(agent.name)
        setFormDesc(agent.description)
        setFormPromptSlots(agent.prompt_slots || {})
        setFormModelId(agent.default_model || '')
        setFormMaxRetry(agent.max_retry ?? 3)
        setFormMaxTokens(agent.max_tokens ?? 0)
        setToolConfig({
          builtin_config: agent.builtin_config ?? [],
          skill_ids: agent.skill_ids ?? [],
          mcp_connection_ids: agent.mcp_connection_ids ?? [],
          workflow_ids: agent.workflow_ids ?? [],
          custom_tool_ids: agent.custom_tool_ids ?? [],
        })
      } else {
        setFormName('')
        setFormDesc('')
        setFormPromptSlots({})
        setFormModelId('')
        setFormMaxRetry(3)
        setFormMaxTokens(0)
        setToolConfig(DEFAULT_TOOL_VALUE)
      }
    }, [agent, isEdit])

    /* ─── Query: available models ─── */
    const { data: modelsData } = useQuery({
      queryKey: modelKeys.list({ page: 1, page_size: 100, status: 'active' }),
      queryFn: () => modelApi.list({ page: 1, page_size: 100, status: 'active' }),
    })
    const availableModels: Model[] = modelsData?.items ?? []

    /* ─── Mutation: save agent ─── */
    const saveMutation = useMutation({
      mutationFn: async () => {
        if (isEdit && agent) {
          return agentApi.update(agent.id, {
            name: formName.trim(),
            description: formDesc.trim(),
            prompt_slots: formPromptSlots,
            skill_ids: toolConfig.skill_ids,
            mcp_connection_ids: toolConfig.mcp_connection_ids,
            builtin_config: toolConfig.builtin_config,
            workflow_ids: toolConfig.workflow_ids,
            custom_tool_ids: toolConfig.custom_tool_ids,
            knowledge_base_ids: agent.knowledge_base_ids,
            default_model: formModelId,
            max_retry: formMaxRetry,
            max_tokens: formMaxTokens,
          })
        } else {
          return agentApi.create({
            name: formName.trim(),
            description: formDesc.trim(),
          })
        }
      },
      onSuccess: () => {
        message.success(isEdit ? 'Agent 更新成功' : 'Agent 创建成功')
        queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
        if (agent) {
          queryClient.invalidateQueries({ queryKey: agentKeys.detail(agent.id) })
        }
        onSaved?.()
      },
    })

    /* ─── Mutation: publish / archive ─── */
    const publishMutation = useMutation({
      mutationFn: agentApi.publish,
      onSuccess: () => {
        message.success('Agent 已发布')
        queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
        if (agent) {
          queryClient.invalidateQueries({ queryKey: agentKeys.detail(agent.id) })
        }
      },
    })

    const archiveMutation = useMutation({
      mutationFn: agentApi.archive,
      onSuccess: () => {
        message.success('Agent 已下架')
        queryClient.invalidateQueries({ queryKey: agentKeys.lists() })
        if (agent) {
          queryClient.invalidateQueries({ queryKey: agentKeys.detail(agent.id) })
        }
      },
    })

    /* ─── Imperative handle ─── */
    useImperativeHandle(ref, () => ({
      submit: () => {
        if (!formName.trim()) {
          message.warning('请输入 Agent 名称')
          return
        }
        // Validate required prompt slots
        const missingRequired = FIXED_SLOTS
          .filter((slot) => slot.required && !formPromptSlots[slot.name]?.trim())
          .map((slot) => slot.label)
        if (missingRequired.length > 0) {
          message.warning(`请填写必填项：${missingRequired.join('、')}`)
          return
        }
        saveMutation.mutate()
      },
      isSaving: () => saveMutation.isPending,
    }), [formName, formPromptSlots, saveMutation])

    /* ─── Notify parent when saving state changes ─── */
    useEffect(() => {
      onSavingChange?.(saveMutation.isPending)
    }, [saveMutation.isPending, onSavingChange])

    /* ─── Current status info ─── */
    const currentStatus = agent?.status ?? 'draft'
    const statusStyle = STATUS_STYLES[currentStatus] ?? STATUS_STYLES.draft
    const canPublish = currentStatus === 'draft' || currentStatus === 'archived'
    const canArchive = currentStatus === 'published'

    /* ─── Collapse items ─── */
    const collapseItems = [
      {
        key: 'basic',
        label: '基本信息',
        children: (
          <div className="flex flex-col gap-4">
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">
                名称 <span className="text-[#EF4444]">*</span>
              </label>
              <Input
                placeholder="请输入 Agent 名称（1-100 字符）"
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                maxLength={100}
                showCount
              />
            </div>
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">描述</label>
              <Input.TextArea
                placeholder="简要描述 Agent 的用途"
                value={formDesc}
                onChange={(e) => setFormDesc(e.target.value)}
                maxLength={500}
                showCount
                rows={2}
              />
            </div>
          </div>
        ),
      },
      {
        key: 'prompt',
        label: '系统提示词',
        children: (
          <div>
            <SlotValueEditor
              slotValues={formPromptSlots}
              onChange={setFormPromptSlots}
            />
            <div className="text-[11px] text-[#94A3B8] mt-1">
              填写各卡槽内容，定义 Agent 的行为方式和能力边界
            </div>
          </div>
        ),
      },
      {
        key: 'model',
        label: '模型配置',
        children: (
          <div className="flex flex-col gap-4">
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">默认模型</label>
              <Select
                placeholder="选择默认模型"
                value={formModelId || undefined}
                onChange={(val) => setFormModelId(val || '')}
                allowClear
                showSearch
                optionFilterProp="label"
                className="w-full"
                options={availableModels.map(m => ({
                  value: m.id,
                  label: `${m.name} (${m.model_id})`,
                }))}
              />
            </div>

            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">
                最大重试次数 <span className="text-[11px] text-[#94A3B8] font-normal">({formMaxRetry})</span>
              </label>
              <InputNumber
                min={0}
                max={10}
                value={formMaxRetry}
                onChange={(v) => setFormMaxRetry(v ?? 3)}
                className="w-full"
              />
              <div className="text-[11px] text-[#94A3B8] mt-1">
                当 LLM 调用失败时自动重试的次数，建议 1-5
              </div>
            </div>

            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">
                会话 Token 上限
              </label>
              <InputNumber
                min={0}
                max={10000000}
                step={10000}
                value={formMaxTokens}
                onChange={(v) => setFormMaxTokens(v ?? 0)}
                className="w-full"
                placeholder="0 = 使用全局默认"
              />
              <div className="text-[11px] text-[#94A3B8] mt-1">
                单次会话累计 Token 上限，超出后 Agent 自动停止。0 表示使用全局默认值
              </div>
            </div>
          </div>
        ),
      },
      {
        key: 'tools',
        label: '工具配置',
        children: (
          <ToolSelector
            value={toolConfig}
            onChange={setToolConfig}
          />
        ),
      },
      ...(isEdit
        ? [{
            key: 'lifecycle',
            label: '生命周期',
            children: (
              <div className="flex flex-col gap-4">
                <div className="flex items-center justify-between">
                  <span className="text-sm text-[#0F172A]">当前状态</span>
                  <Tag style={{ color: statusStyle.color, background: statusStyle.bg, borderColor: 'transparent' }}>
                    {statusStyle.label}
                  </Tag>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-[#0F172A]">创建时间</span>
                  <span className="text-xs text-[#94A3B8]">{agent?.created_at ? parseBackendDate(agent.created_at).toLocaleString('zh-CN') : '-'}</span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-sm text-[#0F172A]">更新时间</span>
                  <span className="text-xs text-[#94A3B8]">{agent?.updated_at ? parseBackendDate(agent.updated_at).toLocaleString('zh-CN') : '-'}</span>
                </div>
                <div className="pt-2 border-t border-gray-100 flex gap-2">
                  {canPublish && (
                    <Button
                      type="primary"
                      icon={<CloudUploadOutlined />}
                      onClick={() => {
                        if (!agent) return
                        Modal.confirm({
                          title: '确认发布',
                          content: `确定要发布 Agent「${agent.name}」吗？发布后将出现在对话测试面板中。`,
                          okText: '发布',
                          cancelText: '取消',
                          onOk: () => publishMutation.mutate(agent.id),
                        })
                      }}
                      loading={publishMutation.isPending}
                      style={{ background: '#10B981', borderColor: '#10B981' }}
                    >
                      发布
                    </Button>
                  )}
                  {canArchive && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={() => {
                        if (!agent) return
                        Modal.confirm({
                          title: '确认下架',
                          content: `确定要下架 Agent「${agent.name}」吗？下架后新对话将无法使用此 Agent。`,
                          okText: '下架',
                          cancelText: '取消',
                          onOk: () => archiveMutation.mutate(agent.id),
                        })
                      }}
                      loading={archiveMutation.isPending}
                    >
                      下架
                    </Button>
                  )}
                  {!canPublish && !canArchive && (
                    <div className="text-[11px] text-[#94A3B8]">
                      当前状态不支持发布/下架操作
                    </div>
                  )}
                </div>
              </div>
            ),
          }]
        : []),
    ]

    /* ─── Preview handler ─── */
    const handlePreview = async () => {
      if (!agent) {
        message.warning('请先保存 Agent 后再预览')
        return
      }
      setPreviewOpen(true)
      setPreviewLoading(true)
      setPreviewData(null)
      try {
        const data = await agentApi.preview(agent.id, {
          input: '测试消息',
          enable_thinking: false,
        })
        setPreviewData(data)
      } catch (err: unknown) {
        const msg = err && typeof err === 'object' && 'message' in err
          ? (err as { message: string }).message
          : '预览失败'
        message.error(msg)
        setPreviewOpen(false)
      } finally {
        setPreviewLoading(false)
      }
    }

    /* ─── Tool type color & label ─── */
    const TOOL_TYPE_STYLE: Record<string, { color: string; label: string }> = {
      skill: { color: 'blue', label: 'Skill' },
      mcp: { color: 'purple', label: 'MCP' },
      builtin: { color: 'green', label: '内置' },
      workflow: { color: 'orange', label: '工作流' },
    }

    return (
      <>
        <Collapse
          defaultActiveKey={['basic', 'prompt', 'model', 'tools', ...(isEdit ? ['lifecycle'] : [])]}
          items={collapseItems}
          className="!bg-transparent"
          expandIconPosition="end"
        />
        {isEdit && agent && (
          <div className="mt-3 pt-3 border-t border-gray-100">
            <Button
              icon={<EyeOutlined />}
              onClick={handlePreview}
              className="w-full"
            >
              预览组装结果
            </Button>
          </div>
        )}

        {/* Preview Modal */}
        <Modal
          title="Agent 组装预览"
          open={previewOpen}
          onCancel={() => setPreviewOpen(false)}
          footer={null}
          width={720}
          styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
        >
          {previewLoading ? (
            <div className="flex justify-center py-12">
              <Spin tip="正在组装..." />
            </div>
          ) : previewData ? (
            <div className="flex flex-col gap-5">
              {/* Summary */}
              <div className="flex items-center gap-3 flex-wrap">
                <Tag color="blue">模型: {previewData.model || '未配置'}</Tag>
                <Tag>工具总数: {previewData.tool_summary.total}</Tag>
                {previewData.tool_summary.skill > 0 && <Tag color="blue">Skill: {previewData.tool_summary.skill}</Tag>}
                {previewData.tool_summary.mcp > 0 && <Tag color="purple">MCP: {previewData.tool_summary.mcp}</Tag>}
                {previewData.tool_summary.builtin > 0 && <Tag color="green">内置: {previewData.tool_summary.builtin}</Tag>}
                {previewData.tool_summary.workflow > 0 && <Tag color="orange">工作流: {previewData.tool_summary.workflow}</Tag>}
              </div>

              {/* System Prompt */}
              <div>
                <div className="text-sm font-medium text-[#0F172A] mb-1.5">系统提示词 (System Prompt)</div>
                <pre className="text-xs bg-[#F8FAFC] border border-gray-100 rounded-lg p-3 whitespace-pre-wrap break-all max-h-[200px] overflow-y-auto">
                  {previewData.system_prompt || '(空)'}
                </pre>
              </div>

              {/* Messages */}
              <div>
                <div className="text-sm font-medium text-[#0F172A] mb-1.5">消息列表 (Messages)</div>
                <div className="flex flex-col gap-1.5">
                  {previewData.messages.map((msg, i) => (
                    <div key={i} className="flex items-start gap-2 text-xs">
                      <Tag color={msg.role === 'system' ? 'gold' : msg.role === 'user' ? 'blue' : 'default'}>
                        {msg.role}
                      </Tag>
                      <span className="flex-1 text-[#475569] break-all line-clamp-3">
                        {msg.content}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Tools */}
              <div>
                <div className="text-sm font-medium text-[#0F172A] mb-1.5">
                  工具列表 (Tools) — {previewData.tools.length} 个
                </div>
                {previewData.tools.length === 0 ? (
                  <Empty description="无工具" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                ) : (
                  <div className="flex flex-col gap-2">
                    {previewData.tools.map((tool: ToolPreview) => {
                      const style = TOOL_TYPE_STYLE[tool.type] ?? { color: 'default', label: tool.type }
                      return (
                        <div key={tool.name} className="border border-gray-100 rounded-lg p-3">
                          <div className="flex items-center gap-2 mb-1">
                            <Tag color={style.color} className="text-[10px]">{style.label}</Tag>
                            <span className="text-sm font-medium text-[#0F172A]">{tool.name}</span>
                          </div>
                          {tool.description && (
                            <div className="text-xs text-[#64748B] mb-1.5">{tool.description}</div>
                          )}
                          {Object.keys(tool.input_schema).length > 0 && (
                            <details className="text-[10px]">
                              <summary className="cursor-pointer text-[#94A3B8] hover:text-[#64748B]">
                                Input Schema
                              </summary>
                              <pre className="mt-1 bg-[#F8FAFC] rounded p-2 overflow-x-auto max-h-[120px]">
                                {JSON.stringify(tool.input_schema, null, 2)}
                              </pre>
                            </details>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </Modal>
      </>
    )
  },
)

export default AgentConfigForm
