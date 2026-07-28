/**
 * API Keys & Webhooks page — manage external integration configuration.
 *
 * Powered by TanStack Query + api-key-api.ts + webhook-api.ts services.
 *
 * Features:
 * - List/Create/Revoke API Keys
 * - One-time raw key display with copy button
 * - List/Create/Delete Webhooks
 * - Toggle webhook status + test delivery
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Checkbox, DatePicker, Input, InputNumber, Modal, Select, Spin, Switch, Tag, Tooltip, message } from 'antd'
import {
  PlusOutlined,
  KeyOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  LinkOutlined,
  ApiOutlined,
  SendOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import {
  apiKeyApi,
  apiKeyKeys,
  ALL_API_KEY_SCOPES,
  SCOPE_LABELS,
  type ApiKey,
} from '../services/api-key-api'
import {
  webhookApi,
  webhookKeys,
  WEBHOOK_EVENTS,
  WEBHOOK_EVENT_LABELS,
} from '../services/webhook-api'
import { agentApi } from '../services/agent-api'
import { workflowsApi } from '../services/workflows-api'

/* ─── Status styles ─── */
const STATUS_STYLES: Record<string, { label: string; color: string; bg: string }> = {
  active: { label: '活跃', color: '#10B981', bg: '#D1FAE5' },
  revoked: { label: '已吊销', color: '#94A3B8', bg: '#F1F5F9' },
}

export default function ApiKeysPage() {
  const { t } = useTheme()
  const queryClient = useQueryClient()

  /* ═══════════ API Keys Section ═══════════ */
  const { data: keysData, isLoading: keysLoading } = useQuery({
    queryKey: apiKeyKeys.list({}),
    queryFn: () => apiKeyApi.list({}),
  })
  const keys = keysData?.items ?? []

  /* ─── Fetch agents for ID -> Name mapping ─── */
  const { data: allAgentsData } = useQuery({
    queryKey: ['agents', 'all-for-bindings'],
    queryFn: () => agentApi.list({ page_size: 200 }),
  })
  const agentMap = new Map((allAgentsData?.items ?? []).map(a => [a.id, a.name]))

  /* ─── Create/Edit Key modal (shared form state) ─── */
  // formMode: 'create' | 'edit' | null（null = 关闭）
  const [formMode, setFormMode] = useState<'create' | 'edit' | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [formName, setFormName] = useState('')
  const [formScopes, setFormScopes] = useState<string[]>([])
  const [formAgentBindings, setFormAgentBindings] = useState<string[]>([])
  const [formWorkflowBindings, setFormWorkflowBindings] = useState<string[]>([])
  const [formRateLimit, setFormRateLimit] = useState(60)
  const [formExpiresAt, setFormExpiresAt] = useState<string | null>(null)
  const [formUserInfoUrl, setFormUserInfoUrl] = useState<string>('')
  const [revealedKey, setRevealedKey] = useState<{ name: string; key: string } | null>(null)

  const { data: agentsData } = useQuery({
    queryKey: ['agents', 'published-select'],
    queryFn: () => agentApi.list({ page_size: 200, status: 'published' }),
    enabled: formMode !== null,
  })
  const { data: workflowsData } = useQuery({
    queryKey: ['workflows', 'published-select'],
    queryFn: () => workflowsApi.list({ page_size: 200, status: 'published' }),
    enabled: formMode !== null,
  })
  const agents = agentsData?.items ?? []
  const workflows = workflowsData?.items ?? []

  const resetForm = () => {
    setFormName(''); setFormScopes([]); setFormAgentBindings([])
    setFormWorkflowBindings([]); setFormRateLimit(60); setFormExpiresAt(null)
    setFormUserInfoUrl('')
  }

  const handleCreateKey = () => {
    resetForm()
    setEditingId(null)
    setFormMode('create')
  }

  const handleEditKey = (key: ApiKey) => {
    setFormName(key.name)
    setFormScopes(key.scopes)
    setFormAgentBindings(key.bindings.agents)
    setFormWorkflowBindings(key.bindings.workflows)
    setFormRateLimit(key.rate_limit)
    setFormExpiresAt(key.expires_at)
    setFormUserInfoUrl(key.user_info_url ?? '')
    setEditingId(key.id)
    setFormMode('edit')
  }

  const closeForm = () => {
    setFormMode(null)
    setEditingId(null)
  }

  const handleKeySubmit = async () => {
    if (!formName.trim()) { message.warning('请输入名称'); return }
    if (formScopes.length === 0) { message.warning('请至少选择一个权限'); return }
    setSubmitting(true)
    try {
      if (formMode === 'create') {
        const result = await apiKeyApi.create({
          name: formName.trim(),
          scopes: formScopes,
          bindings: { agents: formAgentBindings, workflows: formWorkflowBindings },
          rate_limit: formRateLimit,
          expires_at: formExpiresAt,
          user_info_url: formUserInfoUrl.trim() || null,
        })
        message.success('API Key 创建成功')
        queryClient.invalidateQueries({ queryKey: apiKeyKeys.lists() })
        closeForm()
        setRevealedKey({ name: result.name, key: result.key })
      } else if (formMode === 'edit' && editingId) {
        await apiKeyApi.update(editingId, {
          name: formName.trim(),
          scopes: formScopes,
          bindings: { agents: formAgentBindings, workflows: formWorkflowBindings },
          rate_limit: formRateLimit,
          expires_at: formExpiresAt,
          // 空串显式传 null 清除配置（后端 Omit None 语义下需要明确传值）
          user_info_url: formUserInfoUrl.trim() || '',
        })
        message.success('API Key 已更新')
        queryClient.invalidateQueries({ queryKey: apiKeyKeys.lists() })
        closeForm()
      }
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message : '操作失败'
      message.error(msg)
    } finally { setSubmitting(false) }
  }

  const revokeMutation = useMutation({
    mutationFn: (id: string) => apiKeyApi.revoke(id),
    onSuccess: () => { message.success('已吊销'); queryClient.invalidateQueries({ queryKey: apiKeyKeys.lists() }) },
  })

  const handleRevoke = (key: ApiKey) => {
    Modal.confirm({
      title: '确认吊销', icon: <ExclamationCircleOutlined />,
      content: `确定要吊销 API Key「${key.name}」吗？吊销后不可恢复。`,
      okText: '吊销', okButtonProps: { danger: true }, cancelText: '取消',
      onOk: () => revokeMutation.mutate(key.id),
    })
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text).then(
      () => message.success('已复制到剪贴板'),
      () => message.error('复制失败'),
    )
  }

  /* ═══════════ Webhooks Section ═══════════ */
  const { data: whData, isLoading: whLoading } = useQuery({
    queryKey: webhookKeys.list(1),
    queryFn: () => webhookApi.list(),
  })
  const webhooks = whData?.items ?? []

  const [createWhOpen, setCreateWhOpen] = useState(false)
  const [creatingWh, setCreatingWh] = useState(false)
  const [whName, setWhName] = useState('')
  const [whUrl, setWhUrl] = useState('')
  const [whEvents, setWhEvents] = useState<string[]>([])
  const [whApiKey, setWhApiKey] = useState<string | null>(null)

  const handleCreateWh = () => { setWhName(''); setWhUrl(''); setWhEvents([]); setWhApiKey(null); setCreateWhOpen(true) }

  const handleCreateWhSubmit = async () => {
    if (!whName.trim() || !whUrl.trim()) { message.warning('请填写名称和 URL'); return }
    if (whEvents.length === 0) { message.warning('请至少选择一个事件'); return }
    setCreatingWh(true)
    try {
      await webhookApi.create({ name: whName.trim(), url: whUrl.trim(), events: whEvents, api_key_id: whApiKey })
      message.success('Webhook 创建成功')
      queryClient.invalidateQueries({ queryKey: webhookKeys.lists() })
      setCreateWhOpen(false)
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : '创建失败'
      message.error(msg)
    } finally { setCreatingWh(false) }
  }

  const toggleWhStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: 'active' | 'disabled' }) =>
      webhookApi.update(id, { status }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: webhookKeys.lists() }) },
  })

  const deleteWhMutation = useMutation({
    mutationFn: (id: string) => webhookApi.delete(id),
    onSuccess: () => { message.success('已删除'); queryClient.invalidateQueries({ queryKey: webhookKeys.lists() }) },
  })

  const testWhMutation = useMutation({
    mutationFn: (id: string) => webhookApi.test(id),
    onSuccess: (result) => {
      if (result.success) message.success('测试投递成功')
      else message.error(`测试投递失败: ${result.error ?? '未知错误'}`)
    },
  })

  return (
    <div className="animate-[fadeIn_0.3s_ease-out]">
      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        {[
          { label: '活跃密钥', value: keys.filter(k => k.status === 'active').length.toString() },
          { label: '密钥总数', value: (keysData?.total ?? 0).toString() },
          { label: 'Webhook', value: webhooks.filter(w => w.status === 'active').length.toString() },
          { label: 'Webhook 总数', value: (whData?.total ?? 0).toString() },
        ].map((s) => (
          <div key={s.label} className="rounded-xl border border-gray-200 bg-white p-4">
            <div className="text-2xl font-semibold text-[#0F172A] mb-0.5">{s.value}</div>
            <div className="text-xs text-[#64748B]">{s.label}</div>
          </div>
        ))}
      </div>

      {/* ════════ API Keys Section ════════ */}
      <div className="flex items-center justify-between gap-4 mb-4">
        <div className="flex items-center gap-2">
          <KeyOutlined style={{ color: t.primary }} />
          <span className="font-semibold text-sm text-[#0F172A]">API 密钥</span>
        </div>
        <button
          onClick={handleCreateKey}
          className="flex items-center gap-1.5 px-4 h-9 text-[13px] font-medium text-white border-0 cursor-pointer"
          style={{ background: t.primary, borderRadius: 6 }}
        >
          <PlusOutlined style={{ fontSize: 12 }} />
          创建 API Key
        </button>
      </div>

      <div className="rounded-xl border border-gray-200 bg-white mb-6">
        {keysLoading ? (
          <div className="flex items-center justify-center py-12"><Spin /></div>
        ) : keys.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-[#94A3B8]">
            <KeyOutlined style={{ fontSize: 32, marginBottom: 8 }} />
            <span className="text-sm">暂无 API Key</span>
          </div>
        ) : (
          keys.map((apiKey, i) => {
            const style = STATUS_STYLES[apiKey.status] ?? STATUS_STYLES.active
            return (
              <div key={apiKey.id} className={`flex items-center justify-between px-5 py-4 hover:bg-[#F8FAFC] transition-colors duration-150 ${i > 0 ? 'border-t border-gray-50' : ''}`}>
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <div className="w-9 h-9 rounded-lg flex items-center justify-center text-base shrink-0" style={{ background: t.bg, color: t.primary }}>
                    <KeyOutlined />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-[#0F172A]">{apiKey.name}</span>
                      {apiKey.scopes.slice(0, 2).map(s => (
                        <Tag key={s} className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>
                          {SCOPE_LABELS[s] ?? s}
                        </Tag>
                      ))}
                      {apiKey.scopes.length > 2 && (
                        <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#94A3B8', background: '#F8FAFC', borderColor: 'transparent' }}>
                          +{apiKey.scopes.length - 2}
                        </Tag>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-xs text-[#64748B] mt-0.5">
                      <span className="font-mono bg-[#F8FAFC] px-2 py-0.5 rounded border border-gray-100">
                        {apiKey.key_prefix}...
                      </span>
                      <span>限速 {apiKey.rate_limit}/min</span>
                      {apiKey.user_info_url ? (
                        <Tooltip title={apiKey.user_info_url}>
                          <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#7C3AED', background: '#F3E8FF', borderColor: 'transparent' }}>
                            回调验证
                          </Tag>
                        </Tooltip>
                      ) : (
                        <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#94A3B8', background: '#F8FAFC', borderColor: 'transparent' }}>
                          兼容模式
                        </Tag>
                      )}
                    </div>
                    {apiKey.bindings.agents.length > 0 && (
                      <div className="flex items-center gap-1 mt-1 flex-wrap">
                        <span className="text-[10px] text-[#94A3B8]">Agent:</span>
                        {apiKey.bindings.agents.map(agentId => (
                          <Tooltip key={agentId} title={`点击复制 ID: ${agentId}`}>
                            <Tag
                              className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded cursor-pointer hover:opacity-80"
                              style={{ color: '#0369A1', background: '#E0F2FE', borderColor: 'transparent' }}
                              onClick={() => copyToClipboard(agentId)}
                            >
                              {agentMap.get(agentId) ?? agentId.slice(0, 8)}
                            </Tag>
                          </Tooltip>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-4 ml-3 shrink-0">
                  <Tag className="!m-0 !px-2 !py-0.5 !text-xs !rounded" style={{
                    color: style.color, background: style.bg, borderColor: 'transparent',
                  }}>
                    {style.label}
                  </Tag>
                  {apiKey.status === 'active' && (
                    <>
                      <Tooltip title="编辑">
                        <button onClick={() => handleEditKey(apiKey)} className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-[#64748B] hover:text-[#0F172A] hover:bg-gray-50 transition-colors duration-150 cursor-pointer">
                          <EditOutlined />
                        </button>
                      </Tooltip>
                      <Tooltip title="吊销">
                        <button onClick={() => handleRevoke(apiKey)} className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-[#64748B] hover:text-[#EF4444] hover:bg-gray-50 transition-colors duration-150 cursor-pointer">
                          <DeleteOutlined />
                        </button>
                      </Tooltip>
                    </>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>

      {/* ════════ Webhooks Section ════════ */}
      <div className="flex items-center justify-between gap-4 mb-4">
        <div className="flex items-center gap-2">
          <LinkOutlined style={{ color: t.primary }} />
          <span className="font-semibold text-sm text-[#0F172A]">Webhook 配置</span>
        </div>
        <button
          onClick={handleCreateWh}
          className="flex items-center gap-1.5 px-4 h-9 text-[13px] font-medium text-white border-0 cursor-pointer"
          style={{ background: t.primary, borderRadius: 6 }}
        >
          <PlusOutlined style={{ fontSize: 12 }} />
          添加 Webhook
        </button>
      </div>

      <div className="rounded-xl border border-gray-200 bg-white">
        {whLoading ? (
          <div className="flex items-center justify-center py-12"><Spin /></div>
        ) : webhooks.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-[#94A3B8]">
            <LinkOutlined style={{ fontSize: 32, marginBottom: 8 }} />
            <span className="text-sm">暂无 Webhook 配置</span>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-[1fr_120px_100px] gap-4 px-5 py-3 bg-[#F8FAFC] border-b border-gray-100 text-xs font-medium text-[#64748B]">
              <span>名称 / 端点</span>
              <span>状态</span>
              <span></span>
            </div>
            {webhooks.map((wh) => (
              <div key={wh.id} className="grid grid-cols-[1fr_120px_100px] gap-4 px-5 py-3.5 items-center hover:bg-[#F8FAFC] transition-colors duration-150 border-t border-gray-50">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <ApiOutlined className="text-[#94A3B8] text-xs" />
                    <span className="text-sm font-medium text-[#0F172A]">{wh.name}</span>
                  </div>
                  <code className="text-[11px] font-mono text-[#64748B] bg-[#F8FAFC] px-1.5 py-0.5 rounded border border-gray-100 truncate max-w-[400px] block mt-0.5">{wh.url}</code>
                  <div className="flex items-center gap-1 mt-1">
                    {wh.api_key_id && (
                      <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#0369A1', background: '#E0F2FE', borderColor: 'transparent' }}>
                        🔑 {keys.find(k => k.id === wh.api_key_id)?.name ?? wh.api_key_id.slice(0, 12)}
                      </Tag>
                    )}
                    {!wh.api_key_id && (
                      <Tag className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded" style={{ color: '#94A3B8', background: '#F8FAFC', borderColor: 'transparent' }}>
                        系统级
                      </Tag>
                    )}
                    {wh.events.map((ev) => (
                      <Tag key={ev} className="!m-0 !px-1.5 !py-0 !text-[10px] !rounded font-mono" style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>
                        {WEBHOOK_EVENT_LABELS[ev] ?? ev}
                      </Tag>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    size="small"
                    checked={wh.status === 'active'}
                    onChange={(checked) => toggleWhStatus.mutate({ id: wh.id, status: checked ? 'active' : 'disabled' })}
                    style={{ background: wh.status === 'active' ? t.primary : undefined }}
                  />
                  <span className="text-xs text-[#64748B]">{wh.status === 'active' ? '启用' : '停用'}</span>
                </div>
                <div className="flex items-center gap-1">
                  <Tooltip title="测试投递">
                    <button onClick={() => testWhMutation.mutate(wh.id)} className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-[#64748B] hover:text-[#0F172A] hover:bg-gray-50 transition-colors cursor-pointer">
                      <SendOutlined />
                    </button>
                  </Tooltip>
                  <Tooltip title="删除">
                    <button onClick={() => {
                      Modal.confirm({
                        title: '确认删除', icon: <ExclamationCircleOutlined />,
                        content: `确定要删除 Webhook「${wh.name}」吗？`,
                        okText: '删除', okButtonProps: { danger: true }, cancelText: '取消',
                        onOk: () => deleteWhMutation.mutate(wh.id),
                      })
                    }} className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-[#64748B] hover:text-[#EF4444] hover:bg-gray-50 transition-colors cursor-pointer">
                      <DeleteOutlined />
                    </button>
                  </Tooltip>
                </div>
              </div>
            ))}
          </>
        )}
      </div>

      {/* ════════ Create / Edit Key Modal ════════ */}
      <Modal
        title={formMode === 'edit' ? '编辑 API Key' : '创建 API Key'}
        open={formMode !== null}
        onCancel={closeForm}
        onOk={handleKeySubmit}
        confirmLoading={submitting}
        okText={formMode === 'edit' ? '保存' : '创建'}
        cancelText="取消"
        width={560}
      >
        <div className="flex flex-col gap-4 py-2">
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">名称 *</label>
            <Input value={formName} onChange={e => setFormName(e.target.value)} placeholder="例如：MES 产线 A" maxLength={100} />
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">权限 *</label>
            <div className="flex flex-wrap gap-2">
              {ALL_API_KEY_SCOPES.map(scope => (
                <Checkbox key={scope} checked={formScopes.includes(scope)}
                  onChange={e => { if (e.target.checked) setFormScopes([...formScopes, scope]); else setFormScopes(formScopes.filter(s => s !== scope)) }}>
                  <span className="text-xs">{SCOPE_LABELS[scope]}</span>
                </Checkbox>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">Agent 绑定</label>
            <Select mode="multiple" allowClear placeholder="不选 = 可访问全部" value={formAgentBindings} onChange={setFormAgentBindings} className="w-full"
              options={agents.map(a => ({ label: a.name, value: a.id }))} />
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">Workflow 绑定</label>
            <Select mode="multiple" allowClear placeholder="不选 = 可访问全部" value={formWorkflowBindings} onChange={setFormWorkflowBindings} className="w-full"
              options={workflows.map(w => ({ label: w.name, value: w.id }))} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-[#374151] mb-1">限速 (次/分钟)</label>
              <InputNumber value={formRateLimit} onChange={v => setFormRateLimit(v ?? 60)} min={1} max={10000} className="w-full" />
            </div>
            <div>
              <label className="block text-xs font-medium text-[#374151] mb-1">过期时间</label>
              <DatePicker showTime className="w-full" onChange={(_, dateStr) => setFormExpiresAt(dateStr ? String(dateStr) : null)} />
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">
              用户认证 Introspection URL
            </label>
            <Input
              value={formUserInfoUrl}
              onChange={e => setFormUserInfoUrl(e.target.value)}
              placeholder="https://partner.example.com/oauth/introspect"
              maxLength={500}
            />
            <div className="text-[10px] text-[#94A3B8] mt-1">
              空 = 兼容模式（用 visitor_id 做会话隔离）；填入 URL = 回调验证模式（强制请求带 <code className="font-mono">X-User-Token</code>，按 RFC 7662 校验用户）
            </div>
          </div>
        </div>
      </Modal>

      {/* ════════ Key Reveal Modal ════════ */}
      <Modal
        title="API Key 创建成功" open={!!revealedKey} onCancel={() => setRevealedKey(null)}
        footer={<button onClick={() => setRevealedKey(null)} className="px-4 py-2 text-sm font-medium text-white border-0 cursor-pointer" style={{ background: t.primary, borderRadius: 6 }}>我已保存，关闭</button>}
        closable width={520}
      >
        {revealedKey && (
          <div className="py-2">
            <div className="flex items-start gap-2 mb-4 p-3 rounded-lg" style={{ background: '#FFFBEB', border: '1px solid #FDE68A' }}>
              <ExclamationCircleOutlined style={{ color: '#D97706', marginTop: 2 }} />
              <div className="text-xs text-[#92400E]">请妥善保存此 Key，关闭后将<strong>不会再次显示</strong>。</div>
            </div>
            <div className="text-xs font-medium text-[#374151] mb-1">名称：{revealedKey.name}</div>
            <div className="flex items-center gap-2 mt-2">
              <code className="flex-1 text-xs font-mono bg-[#F8FAFC] px-3 py-2 rounded border border-gray-200 break-all select-all">{revealedKey.key}</code>
              <Tooltip title="复制">
                <button onClick={() => copyToClipboard(revealedKey.key)} className="border-0 bg-transparent w-9 h-9 flex items-center justify-center rounded-md text-[#64748B] hover:text-[#0F172A] hover:bg-gray-100 transition-colors cursor-pointer shrink-0">
                  <CopyOutlined />
                </button>
              </Tooltip>
            </div>
          </div>
        )}
      </Modal>

      {/* ════════ Create Webhook Modal ════════ */}
      <Modal
        title="添加 Webhook" open={createWhOpen} onCancel={() => setCreateWhOpen(false)}
        onOk={handleCreateWhSubmit} confirmLoading={creatingWh} okText="创建" cancelText="取消" width={480}
      >
        <div className="flex flex-col gap-4 py-2">
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">名称 *</label>
            <Input value={whName} onChange={e => setWhName(e.target.value)} placeholder="例如：执行完成通知" maxLength={200} />
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">回调 URL *</label>
            <Input value={whUrl} onChange={e => setWhUrl(e.target.value)} placeholder="https://your-system.com/webhooks/agent-flow" />
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">绑定 API Key</label>
            <Select allowClear placeholder="不选 = 接收全部（系统级）" value={whApiKey} onChange={setWhApiKey} className="w-full"
              options={keys.filter(k => k.status === 'active').map(k => ({ label: k.name, value: k.id }))} />
            <div className="text-[10px] text-[#94A3B8] mt-1">绑定后仅接收该 Key 触发的事件；不绑定则接收所有事件</div>
          </div>
          <div>
            <label className="block text-xs font-medium text-[#374151] mb-1">订阅事件 *</label>
            <div className="flex flex-wrap gap-2">
              {WEBHOOK_EVENTS.map(ev => (
                <Checkbox key={ev} checked={whEvents.includes(ev)}
                  onChange={e => { if (e.target.checked) setWhEvents([...whEvents, ev]); else setWhEvents(whEvents.filter(s => s !== ev)) }}>
                  <span className="text-xs">{WEBHOOK_EVENT_LABELS[ev] ?? ev}</span>
                </Checkbox>
              ))}
            </div>
          </div>
        </div>
      </Modal>
    </div>
  )
}
