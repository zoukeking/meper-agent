/**
 * Channels page — manage IM platform channels (Lark / DingTalk / WeCom).
 *
 * Configure platform credentials and bind Agents. Users message the IM
 * bot to receive Agent replies. Powered by TanStack Query + channel-api.ts.
 *
 * Form credentials render dynamically from the provider schema served by
 * the backend (channelApi.getProviderSchema). New credentials use password
 * inputs; in edit mode leaving a field blank leaves it unchanged.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Button, Tag, Select, Modal, Input, Form, Spin, Empty,
  message, Tooltip,
} from 'antd'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, CheckCircleOutlined,
  StopOutlined, ReloadOutlined, CopyOutlined,
} from '@ant-design/icons'
import {
  channelApi,
  channelKeys,
  type Channel,
  type ChannelProvider,
  type ProviderSchema,
  type ReceiveMode,
} from '../services/channel-api'
import { agentApi, agentKeys } from '../services/agent-api'

const PROVIDER_LABELS: Record<ChannelProvider, string> = {
  lark: '飞书',
  dingtalk: '钉钉',
  wecom: '企业微信',
  mock: 'Mock',
}

const PROVIDER_COLORS: Record<ChannelProvider, string> = {
  lark: 'blue',
  dingtalk: 'green',
  wecom: 'purple',
  mock: 'default',
}

const PROVIDER_OPTIONS = (Object.keys(PROVIDER_LABELS) as ChannelProvider[]).map((p) => ({
  value: p,
  label: PROVIDER_LABELS[p],
}))

export default function ChannelsPage() {
  const queryClient = useQueryClient()
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Channel | null>(null)
  const [form] = Form.useForm()

  /* ─── Queries ─── */
  const { data, isLoading } = useQuery({
    queryKey: channelKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => channelApi.list({ page: 1, page_size: 100 }),
  })

  const { data: schemaData } = useQuery({
    queryKey: channelKeys.schema(),
    queryFn: () => channelApi.getProviderSchema(),
  })

  const { data: agentsData } = useQuery({
    queryKey: agentKeys.list({ page: 1, page_size: 100, status: 'published' }),
    queryFn: () => agentApi.list({ page: 1, page_size: 100, status: 'published' }),
  })

  /* ─── Mutations ─── */
  const createMutation = useMutation({
    mutationFn: (input: Parameters<typeof channelApi.create>[0]) => channelApi.create(input),
    onSuccess: () => {
      message.success('Channel 创建成功')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
      setModalOpen(false)
      form.resetFields()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, input }: { id: string; input: Parameters<typeof channelApi.update>[1] }) =>
      channelApi.update(id, input),
    onSuccess: () => {
      message.success('Channel 更新成功')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
      setModalOpen(false)
      form.resetFields()
      setEditing(null)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => channelApi.remove(id),
    onSuccess: () => {
      message.success('已删除')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  const toggleMutation = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) => {
      if (enabled) await channelApi.enable(id)
      else await channelApi.disable(id)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  const resetMutation = useMutation({
    mutationFn: (id: string) => channelApi.reset(id),
    onSuccess: () => {
      message.success('已重置')
      queryClient.invalidateQueries({ queryKey: channelKeys.lists() })
    },
  })

  /* ─── Actions ─── */
  function openCreate() {
    setEditing(null)
    form.resetFields()
    setModalOpen(true)
  }

  function openEdit(ch: Channel) {
    setEditing(ch)
    form.setFieldsValue({
      name: ch.name,
      provider: ch.provider,
      agent_id: ch.agent_id,
      receive_mode: ch.receive_mode,
    })
    setModalOpen(true)
  }

  function closeModal() {
    setModalOpen(false)
    setEditing(null)
  }

  async function handleSubmit() {
    const values = await form.validateFields()
    const credInput: Record<string, string> = {}
    const providerSchema: ProviderSchema | undefined =
      schemaData?.providers[values.provider as ChannelProvider]
    providerSchema?.credential_fields.forEach((f) => {
      const v = values[`cred_${f.key}`]
      if (v) credInput[f.key] = v
    })

    if (editing) {
      updateMutation.mutate({
        id: editing.id,
        input: {
          name: values.name,
          agent_id: values.agent_id,
          credentials: Object.keys(credInput).length ? credInput : undefined,
          receive_mode: values.receive_mode,
        },
      })
    } else {
      createMutation.mutate({
        name: values.name,
        provider: values.provider,
        agent_id: values.agent_id,
        credentials: credInput,
        receive_mode: values.receive_mode ?? 'webhook',
      })
    }
  }

  const channels = data?.items ?? []

  return (
    <div className="animate-[fadeIn_0.3s_ease-out]">
      <div className="flex justify-between items-center mb-4">
        <div>
          <h2 className="text-xl font-semibold text-[#0F172A] mb-1">渠道管理</h2>
          <p className="text-sm text-[#64748B]">
            配置 IM 平台（飞书 / 钉钉 / 企微）凭据并绑定 Agent，用户在 IM 发消息即可收到 Agent 回复。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建 Channel
        </Button>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <Spin size="large" />
        </div>
      ) : channels.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-[#94A3B8]">
          <Empty description="还没有 Channel" />
        </div>
      ) : (
        <div className="rounded-xl border border-gray-200 bg-white divide-y divide-gray-50">
          {channels.map((ch) => (
            <div
              key={ch.id}
              className="flex items-center justify-between gap-4 px-5 py-3.5 hover:bg-[#F8FAFC] transition-colors duration-150"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-[#0F172A]">{ch.name}</span>
                  <Tag color={PROVIDER_COLORS[ch.provider]} className="!m-0">
                    {PROVIDER_LABELS[ch.provider] ?? ch.provider}
                  </Tag>
                  <Tag color={ch.enabled ? 'green' : 'default'} className="!m-0">
                    {ch.enabled ? '已启用' : '已禁用'}
                  </Tag>
                  {ch.status === 'degraded' && (
                    <Tag color="red" className="!m-0">已降级</Tag>
                  )}
                </div>
                <div className="text-xs text-[#64748B] mt-1 truncate flex items-center gap-2 flex-wrap">
                  <span>Agent: {ch.agent_id}</span>
                  <span className="text-gray-300">·</span>
                  {ch.receive_mode === 'long_connection' ? (
                    <span className="flex items-center gap-1">
                      <span
                        className={`inline-block w-1.5 h-1.5 rounded-full ${
                          ch.connection_status === 'long_connection_connected'
                            ? 'bg-green-500'
                            : 'bg-gray-300'
                        }`}
                      />
                      长连接:
                      {ch.connection_status === 'long_connection_connected'
                        ? '已连接'
                        : ch.connection_status === 'long_connection_disconnected'
                        ? '已断开'
                        : '未启动'}
                    </span>
                  ) : (
                    <code className="font-mono">
                      入站: {ch.inbound_url.split('?')[0]}
                    </code>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <Tooltip title={ch.enabled ? '禁用' : '启用'}>
                  <Button
                    size="small"
                    type="text"
                    icon={ch.enabled ? <StopOutlined /> : <CheckCircleOutlined />}
                    onClick={() => toggleMutation.mutate({ id: ch.id, enabled: !ch.enabled })}
                  />
                </Tooltip>
                {ch.status === 'degraded' && (
                  <Tooltip title="重置降级状态">
                    <Button
                      size="small"
                      type="text"
                      icon={<ReloadOutlined />}
                      onClick={() => resetMutation.mutate(ch.id)}
                    />
                  </Tooltip>
                )}
                <Tooltip title="复制入站 URL">
                  <Button
                    size="small"
                    type="text"
                    icon={<CopyOutlined />}
                    onClick={() => {
                      navigator.clipboard.writeText(ch.inbound_url)
                      message.success('已复制')
                    }}
                  />
                </Tooltip>
                <Tooltip title="编辑">
                  <Button
                    size="small"
                    type="text"
                    icon={<EditOutlined />}
                    onClick={() => openEdit(ch)}
                  />
                </Tooltip>
                <Tooltip title="删除">
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      Modal.confirm({
                        title: '确认删除?',
                        content: `将删除 Channel「${ch.name}」`,
                        okText: '删除',
                        okButtonProps: { danger: true },
                        cancelText: '取消',
                        onOk: () => deleteMutation.mutate(ch.id),
                      })
                    }}
                  />
                </Tooltip>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal
        title={editing ? '编辑 Channel' : '新建 Channel'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={closeModal}
        okText={editing ? '保存' : '创建'}
        cancelText="取消"
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
        width={560}
      >
        <Form form={form} layout="vertical" className="pt-2">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：售后客服-飞书" maxLength={200} />
          </Form.Item>
          <Form.Item name="provider" label="平台" rules={[{ required: true, message: '请选择平台' }]}>
            <Select
              placeholder="选择 IM 平台"
              disabled={!!editing}
              options={PROVIDER_OPTIONS}
            />
          </Form.Item>
          <Form.Item shouldUpdate={(prev, next) => prev.provider !== next.provider}>
            {({ getFieldValue, setFieldValue }) => {
              const provider = getFieldValue('provider') as ChannelProvider | undefined
              const modes: ReceiveMode[] = provider
                ? schemaData?.providers[provider]?.receive_modes ?? ['webhook']
                : ['webhook']
              // If the current mode isn't supported by this provider, reset to webhook
              const current = getFieldValue('receive_mode') as ReceiveMode | undefined
              if (current && !modes.includes(current)) {
                setFieldValue('receive_mode', 'webhook')
              }
              return (
                <Form.Item
                  name="receive_mode"
                  label="接收模式"
                  rules={[{ required: true, message: '请选择接收模式' }]}
                  initialValue="webhook"
                  extra={
                    getFieldValue('receive_mode') === 'long_connection'
                      ? '长连接模式：服务主动连出，无需公网 IP/域名，适合内网部署'
                      : 'Webhook 模式：需要公网可达的回调 URL（可用 ngrok 做本地开发）'
                  }
                >
                  <Select
                    placeholder="选择接收模式"
                    options={modes.map((m) => ({
                      value: m,
                      label: m === 'long_connection' ? '长连接（免公网）' : 'Webhook',
                    }))}
                  />
                </Form.Item>
              )
            }}
          </Form.Item>
          <Form.Item
            name="agent_id"
            label="绑定 Agent"
            rules={[{ required: true, message: '请选择 Agent' }]}
          >
            <Select
              placeholder="选择要绑定的 Agent"
              showSearch
              optionFilterProp="label"
              options={(agentsData?.items ?? []).map((a) => ({
                value: a.id,
                label: a.name,
              }))}
            />
          </Form.Item>
          <Form.Item shouldUpdate={(prev, next) => prev.provider !== next.provider}>
            {({ getFieldValue }) => {
              const provider = getFieldValue('provider') as ChannelProvider | undefined
              const fields = provider
                ? schemaData?.providers[provider]?.credential_fields ?? []
                : []
              if (fields.length === 0) return null
              return (
                <div className="rounded-lg border border-gray-100 bg-gray-50 p-3">
                  <div className="text-xs text-[#64748B] mb-2">平台凭据</div>
                  {fields.map((f) => {
                    // 编辑模式下,显示当前已保存的 mask 值作为提示
                    const savedValue = editing?.credentials?.[f.key]
                    const maskHint = savedValue ? `当前: ${savedValue}（留空不修改）` : `输入 ${f.label}`
                    return (
                    <Form.Item
                      key={f.key}
                      name={`cred_${f.key}`}
                      label={f.label}
                      rules={f.required && !editing ? [{ required: true, message: `请输入${f.label}` }] : []}
                    >
                      <Input.Password
                        placeholder={editing ? maskHint : `输入 ${f.label}`}
                        autoComplete="new-password"
                        visibilityToggle
                      />
                    </Form.Item>
                    )
                  })}
                </div>
              )
            }}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
