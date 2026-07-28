/**
 * Models page — manage LLM model configurations.
 *
 * Displays configured LLM models with status, context window info,
 * and CRUD operations. Powered by TanStack Query + model-api.ts.
 */
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Button, Tag, Select, Tooltip, message, Spin, Modal, Input,
  InputNumber, Slider, Collapse,
} from 'antd'
import {
  PlusOutlined,
  SearchOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  EditOutlined,
  DeleteOutlined,
  InboxOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useTheme } from '../contexts/ThemeContext'
import { parseBackendDate } from '../lib/format'
import {
  modelApi,
  modelKeys,
  type Model,
  type ModelStatus,
  type ModelCreateInput,
  type ModelTestResult,
  type AuthType,
} from '../services/model-api'

/* ─── Compat / Auth labels ─── */
const COMPAT_LABELS: Record<string, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
}

const AUTH_TYPE_OPTIONS: { value: AuthType; label: string; desc: string }[] = [
  { value: 'bearer', label: 'Bearer Token', desc: 'Authorization: Bearer {key}' },
  { value: 'x_api_key', label: 'x-api-key', desc: 'x-api-key: {key}（Anthropic 风格）' },
  { value: 'api_key_header', label: 'api-key', desc: 'api-key: {key}（Azure 风格）' },
  { value: 'custom', label: '自定义', desc: '通过 auth_header_format 模板自定义' },
]

const AUTH_TYPE_LABELS: Record<AuthType, string> = {
  bearer: 'Bearer',
  x_api_key: 'x-api-key',
  api_key_header: 'api-key',
  custom: '自定义',
}

/* ─── Debounce hook ─── */
function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/* ─── Form state type ─── */
interface FormState {
  model_id: string
  name: string
  base_url: string
  api_key: string
  compatibility_type: 'openai' | 'anthropic'
  auth_type: AuthType
  auth_header_format: string
  temperature: number
  max_tokens: number
  context_window: number
  provider_tag: string
}

const EMPTY_FORM: FormState = {
  model_id: '',
  name: '',
  base_url: '',
  api_key: '',
  compatibility_type: 'openai',
  auth_type: 'bearer',
  auth_header_format: 'Bearer {key}',
  temperature: 0.7,
  max_tokens: 4096,
  context_window: 128000,
  provider_tag: '',
}

export default function ModelsPage() {
  const { t } = useTheme()
  const queryClient = useQueryClient()

  /* ─── Filter state ─── */
  const [searchInput, setSearchInput] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const debouncedSearch = useDebouncedValue(searchInput, 300)

  /* ─── Modal state ─── */
  const [modalOpen, setModalOpen] = useState(false)
  const [editingModel, setEditingModel] = useState<Model | null>(null)
  const [form, setForm] = useState<FormState>({ ...EMPTY_FORM })

  /* ─── Test result state ─── */
  const [testModalOpen, setTestModalOpen] = useState(false)
  const [testingModel, setTestingModel] = useState<Model | null>(null)
  const [testResult, setTestResult] = useState<ModelTestResult | null>(null)
  // Cache latest test result per model id for inline status display
  const [testStatusMap, setTestStatusMap] = useState<Record<string, ModelTestResult>>({})

  const isEditMode = editingModel !== null

  /* ─── Query: model list ─── */
  const queryParams = {
    page: 1,
    page_size: 100,
    ...(statusFilter !== 'all' ? { status: statusFilter as ModelStatus } : {}),
  }

  const {
    data,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: modelKeys.list(queryParams),
    queryFn: () => modelApi.list(queryParams),
  })

  const models = data?.items ?? []
  const total = data?.total ?? 0

  // Client-side search filtering (search by name / model_id / provider_tag)
  const filtered = debouncedSearch
    ? models.filter((m) => {
        const q = debouncedSearch.toLowerCase()
        return (
          m.name.toLowerCase().includes(q) ||
          m.model_id.toLowerCase().includes(q) ||
          (m.provider_tag ?? '').toLowerCase().includes(q)
        )
      })
    : models

  /* ─── Mutation: delete model ─── */
  const deleteMutation = useMutation({
    mutationFn: modelApi.remove,
    onSuccess: () => {
      message.success('模型已删除')
      queryClient.invalidateQueries({ queryKey: modelKeys.lists() })
    },
  })

  /* ─── Mutation: save model (create or update) ─── */
  const saveMutation = useMutation({
    mutationFn: (input: { id?: string; data: ModelCreateInput }) => {
      return input.id
        ? modelApi.update(input.id, input.data)
        : modelApi.create(input.data)
    },
    onSuccess: () => {
      message.success(isEditMode ? '模型更新成功' : '模型创建成功')
      closeModal()
      queryClient.invalidateQueries({ queryKey: modelKeys.lists() })
    },
  })

  /* ─── Mutation: test model connectivity ─── */
  const testMutation = useMutation({
    mutationFn: modelApi.test,
    onSuccess: (result) => {
      setTestResult(result)
      if (testingModel) {
        setTestStatusMap((prev) => ({ ...prev, [testingModel.id]: result }))
      }
      if (result.success) {
        message.success(`测试成功 · ${result.latency_ms}ms`)
      } else {
        message.warning(`测试失败 · ${result.error}`)
      }
    },
    onError: (err: unknown) => {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message
        : '测试请求失败'
      const errResult: ModelTestResult = {
        success: false,
        latency_ms: 0,
        reply: '',
        error: msg,
        error_code: 'REQUEST_ERROR',
        tested_at: new Date().toISOString(),
      }
      setTestResult(errResult)
      if (testingModel) {
        setTestStatusMap((prev) => ({ ...prev, [testingModel.id]: errResult }))
      }
    },
  })

  /* ─── Open modal for create ─── */
  const openCreateModal = () => {
    setEditingModel(null)
    setForm({ ...EMPTY_FORM })
    setModalOpen(true)
  }

  /* ─── Open modal for edit ─── */
  const openEditModal = (model: Model) => {
    setEditingModel(model)
    setForm({
      model_id: model.model_id,
      name: model.name,
      base_url: model.base_url,
      api_key: '', // Don't prefill encrypted key
      compatibility_type: model.compatibility_type,
      auth_type: model.auth_type || 'bearer',
      auth_header_format: model.auth_header_format || 'Bearer {key}',
      temperature: model.default_params?.temperature ?? 0.7,
      max_tokens: model.default_params?.max_tokens ?? 4096,
      context_window: model.default_params?.context_window ?? 128000,
      provider_tag: model.provider_tag || '',
    })
    setModalOpen(true)
  }

  /* ─── Close modal ─── */
  const closeModal = () => {
    setModalOpen(false)
    setEditingModel(null)
    setForm({ ...EMPTY_FORM })
  }

  /* ─── Open test modal ─── */
  const openTestModal = (model: Model) => {
    setTestingModel(model)
    setTestResult(null)
    setTestModalOpen(true)
    testMutation.mutate(model.id)
  }

  /* ─── Close test modal ─── */
  const closeTestModal = () => {
    setTestModalOpen(false)
    setTestingModel(null)
    setTestResult(null)
  }

  /* ─── Submit form ─── */
  const handleSubmit = () => {
    if (!form.model_id.trim()) {
      message.warning('请输入模型标识')
      return
    }
    if (!form.name.trim()) {
      message.warning('请输入显示名称')
      return
    }
    if (!form.base_url.trim()) {
      message.warning('请输入 Base URL')
      return
    }
    if (!isEditMode && !form.api_key.trim()) {
      message.warning('请输入 API Key')
      return
    }

    // In edit mode, omit api_key from payload when left blank to preserve existing key
    const payload: ModelCreateInput = {
      model_id: form.model_id.trim(),
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim(),
      compatibility_type: form.compatibility_type,
      auth_type: form.auth_type,
      auth_header_format: form.auth_header_format.trim() || 'Bearer {key}',
      default_params: {
        temperature: form.temperature,
        max_tokens: form.max_tokens,
        context_window: form.context_window,
      },
      provider_tag: form.provider_tag.trim(),
    }

    saveMutation.mutate({
      ...(isEditMode ? { id: editingModel.id } : {}),
      data: payload,
    })
  }

  /* ─── Delete with confirmation ─── */
  const handleDelete = (model: Model) => {
    Modal.confirm({
      title: '确认删除',
      content: `确定要删除模型「${model.name}」吗？如果该模型正被 Agent 引用，将无法删除。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => deleteMutation.mutate(model.id),
    })
  }

  /* ─── Stats ─── */
  const activeCount = models.filter(m => m.status === 'active').length
  const stats = [
    { label: '模型总数', value: total.toString() },
    { label: '可用模型', value: activeCount.toString() },
    { label: 'OpenAI 兼容', value: models.filter(m => m.compatibility_type === 'openai').length.toString() },
    { label: 'Anthropic 兼容', value: models.filter(m => m.compatibility_type === 'anthropic').length.toString() },
  ]

  /* ─── Format relative time ─── */
  function formatTime(iso: string) {
    if (!iso) return '-'
    // eslint-disable-next-line react-hooks/purity -- Date.now is needed for relative time display
    const diff = Date.now() - parseBackendDate(iso).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return '刚刚'
    if (mins < 60) return `${mins} 分钟前`
    const hours = Math.floor(mins / 60)
    if (hours < 24) return `${hours} 小时前`
    return `${Math.floor(hours / 24)} 天前`
  }

  return (
    <div className="animate-[fadeIn_0.3s_ease-out]">
      {/* Stats row */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        {stats.map((s) => (
          <div key={s.label} className="rounded-xl border border-gray-200 bg-white p-4">
            <div className="text-2xl font-semibold text-[#0F172A] mb-0.5">
              {isLoading ? <Spin size="small" /> : s.value}
            </div>
            <div className="text-xs text-[#64748B]">{s.label}</div>
          </div>
        ))}
      </div>

      {/* Search / action bar */}
      <div className="flex items-center justify-between gap-4 mb-6">
        <div className="flex items-center gap-3">
          <div className="relative">
            <SearchOutlined className="absolute left-3 top-1/2 -translate-y-1/2 text-[#94A3B8] text-sm" />
            <input
              type="text"
              placeholder="搜索模型或提供商..."
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="pl-9 pr-4 py-2 rounded-lg border border-gray-200 text-sm focus:outline-none focus:ring-2 w-64"
              style={{ '--tw-ring-color': t.bg } as React.CSSProperties}
            />
          </div>
          <Select
            value={statusFilter}
            onChange={setStatusFilter}
            className="w-28"
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'active', label: '已启用' },
              { value: 'inactive', label: '已停用' },
            ]}
          />
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
          添加模型
        </Button>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="flex items-center justify-center py-20">
          <Spin size="large" tip="加载中..." />
        </div>
      )}

      {/* Error state */}
      {isError && !isLoading && (
        <div className="flex flex-col items-center justify-center py-20 text-[#94A3B8]">
          <InboxOutlined className="text-4xl mb-3" />
          <p className="text-sm">
            {error && typeof error === 'object' && 'message' in error
              ? (error as { message: string }).message
              : '加载失败，请稍后重试'}
          </p>
        </div>
      )}

      {/* Empty state */}
      {!isLoading && !isError && filtered.length === 0 && (
        <div className="flex flex-col items-center justify-center py-20 text-[#94A3B8]">
          <InboxOutlined className="text-4xl mb-3" />
          <p className="text-sm">暂无模型，点击右上角「添加模型」开始配置</p>
        </div>
      )}

      {/* Table */}
      {!isLoading && !isError && filtered.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white">
          {/* Table header */}
          <div className="grid grid-cols-[1fr_120px_120px_100px_100px_100px_130px] gap-4 px-5 py-3 bg-[#F8FAFC] border-b border-gray-100 text-xs font-medium text-[#64748B]">
            <span>模型名称</span>
            <span>提供商</span>
            <span>兼容协议</span>
            <span>认证方式</span>
            <span>上下文窗口</span>
            <span>更新时间</span>
            <span>操作</span>
          </div>

          {/* Rows */}
          {filtered.map((model, i) => {
            const ctxWindow = model.default_params?.context_window
              ? `${Math.round(model.default_params.context_window / 1000)}K`
              : '-'
            return (
              <div
                key={model.id}
                className={`grid grid-cols-[1fr_120px_120px_100px_100px_100px_130px] gap-4 px-5 py-3.5 items-center hover:bg-[#F8FAFC] transition-colors duration-150 ${i > 0 ? 'border-t border-gray-50' : ''}`}
              >
                <div className="min-w-0 flex items-start gap-2">
                  {testStatusMap[model.id] && (
                    <Tooltip
                      title={
                        testStatusMap[model.id].success
                          ? `连接正常 · ${testStatusMap[model.id].latency_ms}ms`
                          : testStatusMap[model.id].error
                      }
                    >
                      <span
                        className={`mt-1.5 flex-shrink-0 inline-block w-2 h-2 rounded-full ${
                          testStatusMap[model.id].success ? 'bg-emerald-400' : 'bg-red-400'
                        }`}
                      />
                    </Tooltip>
                  )}
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-[#0F172A] truncate">{model.name}</div>
                    <div className="text-[11px] text-[#94A3B8] font-mono truncate">{model.model_id}</div>
                  </div>
                </div>
                <span className="text-sm text-[#64748B]">{model.provider_tag || '-'}</span>
                <Tag className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded !w-fit" style={{ color: t.primary, background: t.bg, borderColor: 'transparent' }}>
                  {COMPAT_LABELS[model.compatibility_type] || model.compatibility_type}
                </Tag>
                <Tag className="!m-0 !px-2 !py-0.5 !text-[11px] !rounded !w-fit" style={{ color: '#64748B', background: '#F1F5F9', borderColor: 'transparent' }}>
                  {AUTH_TYPE_LABELS[model.auth_type] || model.auth_type}
                </Tag>
                <span className="text-sm text-[#64748B]">{ctxWindow}</span>
                <span className="text-sm text-[#64748B]">{formatTime(model.updated_at)}</span>
                <div className="flex items-center gap-0.5">
                  <Tooltip title="测试连通性">
                    <button
                      onClick={() => openTestModal(model)}
                      disabled={testMutation.isPending}
                      className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#94A3B8] hover:text-[#8B5CF6] hover:bg-purple-50 transition-colors duration-150 text-xs"
                    ><ThunderboltOutlined /></button>
                  </Tooltip>
                  <Tooltip title="编辑">
                    <button
                      onClick={() => openEditModal(model)}
                      className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#94A3B8] hover:text-[#0F172A] hover:bg-gray-50 transition-colors duration-150 text-xs"
                    ><EditOutlined /></button>
                  </Tooltip>
                  <Tooltip title="删除">
                    <button
                      onClick={() => handleDelete(model)}
                      disabled={deleteMutation.isPending}
                      className="border-0 bg-transparent w-7 h-7 flex items-center justify-center rounded text-[#94A3B8] hover:text-[#EF4444] hover:bg-gray-50 transition-colors duration-150 text-xs"
                    ><DeleteOutlined /></button>
                  </Tooltip>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Test Model Modal */}
      <Modal
        title={
          <div className="flex items-center gap-2">
            <ThunderboltOutlined style={{ color: '#8B5CF6' }} />
            <span>测试模型连通性</span>
            {testingModel && (
              <span className="text-sm text-[#94A3B8] font-normal">
                · {testingModel.name}
              </span>
            )}
          </div>
        }
        open={testModalOpen}
        onCancel={closeTestModal}
        footer={
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-[#94A3B8]">
              发送 "hello" 消息验证模型配置是否正确
            </span>
            <div className="flex items-center gap-2">
              <Button onClick={closeTestModal}>关闭</Button>
              <Button
                type="primary"
                loading={testMutation.isPending}
                onClick={() => testingModel && testMutation.mutate(testingModel.id)}
                icon={<ThunderboltOutlined />}
                style={{ background: '#8B5CF6', borderColor: '#8B5CF6' }}
              >
                重新测试
              </Button>
            </div>
          </div>
        }
        destroyOnClose
        width={480}
      >
        <div className="py-4">
          {/* Loading state */}
          {testMutation.isPending && !testResult && (
            <div className="flex flex-col items-center gap-3 py-8">
              <Spin size="large" />
              <span className="text-sm text-[#64748B]">正在向模型发送测试请求...</span>
            </div>
          )}

          {/* Success result */}
          {testResult?.success && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-3 p-4 rounded-lg bg-emerald-50 border border-emerald-200">
                <CheckCircleOutlined className="text-xl text-emerald-500" />
                <div>
                  <div className="text-sm font-medium text-emerald-700">连接成功</div>
                  <div className="text-xs text-emerald-600">
                    延迟 {testResult.latency_ms}ms · {testResult.tested_at ? parseBackendDate(testResult.tested_at).toLocaleTimeString() : ''}
                  </div>
                </div>
              </div>
              {testResult.reply && (
                <div className="p-3 rounded-lg bg-gray-50 border border-gray-100">
                  <div className="text-[11px] text-[#94A3B8] mb-1.5">模型回复</div>
                  <div className="text-sm text-[#0F172A] whitespace-pre-wrap break-all">
                    {testResult.reply}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Error result */}
          {testResult && !testResult.success && (
            <div className="flex flex-col gap-3">
              <div className="flex items-start gap-3 p-4 rounded-lg bg-red-50 border border-red-200">
                <CloseCircleOutlined className="text-xl text-red-500 mt-0.5" />
                <div>
                  <div className="text-sm font-medium text-red-700">连接失败</div>
                  <div className="text-xs text-red-600 mt-0.5">
                    {testResult.error_code && (
                      <span className="font-mono bg-red-100 px-1.5 py-0.5 rounded mr-1.5">
                        {testResult.error_code}
                      </span>
                    )}
                    耗时 {testResult.latency_ms}ms
                  </div>
                </div>
              </div>
              <div className="p-3 rounded-lg bg-gray-50 border border-gray-100">
                <div className="text-[11px] text-[#94A3B8] mb-1.5">错误详情</div>
                <div className="text-sm text-[#0F172A]">{testResult.error}</div>
              </div>
              {/* Troubleshooting hints based on error code */}
              <div className="p-3 rounded-lg bg-amber-50 border border-amber-100">
                <div className="text-[11px] text-amber-600 mb-1.5">排查建议</div>
                <div className="text-xs text-amber-700">
                  {testResult.error_code === 'AUTH_FAILED' && '请检查 API Key 是否正确、是否已过期。'}
                  {testResult.error_code === 'MODEL_NOT_FOUND_UPSTREAM' && '请确认 model_id 在目标平台存在。'}
                  {testResult.error_code === 'CONNECTION_ERROR' && '请检查 Base URL 是否正确、网络是否可达。'}
                  {testResult.error_code === 'TIMEOUT' && '网络延迟较高，请检查网络连接或稍后重试。'}
                  {testResult.error_code === 'RATE_LIMITED' && '上游 API 请求频率超限，请稍后重试。'}
                  {testResult.error_code === 'QUOTA_EXCEEDED' && 'API 配额不足，请检查账户余额。'}
                  {testResult.error_code === 'SSL_ERROR' && 'SSL 证书验证失败，请检查 Base URL 是否使用了 HTTPS。'}
                  {!['AUTH_FAILED', 'MODEL_NOT_FOUND_UPSTREAM', 'CONNECTION_ERROR', 'TIMEOUT', 'RATE_LIMITED', 'QUOTA_EXCEEDED', 'SSL_ERROR'].includes(testResult.error_code) && '请检查所有配置项是否正确，或查看服务端日志获取更多信息。'}
                </div>
              </div>
            </div>
          )}
        </div>
      </Modal>

      {/* Create / Edit Model Modal */}
      <Modal
        title={isEditMode ? '编辑模型' : '添加模型'}
        open={modalOpen}
        onCancel={closeModal}
        onOk={handleSubmit}
        okText={isEditMode ? '保存' : '创建'}
        cancelText="取消"
        confirmLoading={saveMutation.isPending}
        okButtonProps={{ disabled: !form.model_id.trim() || !form.name.trim() || !form.base_url.trim() }}
        destroyOnClose
        width={560}
      >
        <div className="flex flex-col gap-4 py-2">
          {/* Model ID */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              模型标识 <span className="text-[#EF4444]">*</span>
            </label>
            <Input
              placeholder="例如: deepseek-chat, gpt-4o-mini"
              value={form.model_id}
              onChange={(e) => setForm({ ...form, model_id: e.target.value })}
              maxLength={200}
            />
            <div className="text-[11px] text-[#94A3B8] mt-1">
              发送给上游 API 的模型 ID（如 deepseek-chat）
            </div>
          </div>

          {/* Display Name */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              显示名称 <span className="text-[#EF4444]">*</span>
            </label>
            <Input
              placeholder="例如: DeepSeek V3 Chat"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              maxLength={100}
            />
          </div>

          {/* Base URL */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              Base URL <span className="text-[#EF4444]">*</span>
            </label>
            <Input
              placeholder="例如: https://api.deepseek.com/v1"
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              maxLength={500}
            />
          </div>

          {/* API Key */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">
              API Key <span className="text-[#EF4444]">*</span>
            </label>
            <Input.Password
              placeholder={isEditMode ? '留空则保留原有密钥' : '输入 API Key'}
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            />
          </div>

          {/* Compatibility Type */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">兼容协议</label>
            <Select
              value={form.compatibility_type}
              onChange={(val) => setForm({ ...form, compatibility_type: val })}
              className="w-full"
              options={[
                { value: 'openai', label: 'OpenAI 兼容' },
                { value: 'anthropic', label: 'Anthropic 兼容' },
              ]}
            />
          </div>

          {/* Auth Type */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">认证方式</label>
            <Select
              value={form.auth_type}
              onChange={(val) => setForm({ ...form, auth_type: val })}
              className="w-full"
              options={AUTH_TYPE_OPTIONS.map(opt => ({
                value: opt.value,
                label: (
                  <div>
                    <span className="font-medium">{opt.label}</span>
                    <span className="text-[11px] text-[#94A3B8] ml-2">{opt.desc}</span>
                  </div>
                ),
              }))}
            />
            <div className="text-[11px] text-[#94A3B8] mt-1">
              {AUTH_TYPE_OPTIONS.find(o => o.value === form.auth_type)?.desc}
            </div>
          </div>

          {/* Custom Auth Header Format (only shown when auth_type == 'custom') */}
          {form.auth_type === 'custom' && (
            <div>
              <label className="block text-sm text-[#0F172A] mb-1.5">
                自定义认证模板 <span className="text-[#EF4444]">*</span>
              </label>
              <Input
                placeholder='例如: Bearer {key} 或 {"X-Api-Key": "{key}"}'
                value={form.auth_header_format}
                onChange={(e) => setForm({ ...form, auth_header_format: e.target.value })}
                maxLength={500}
              />
              <div className="text-[11px] text-[#94A3B8] mt-1">
                支持 {'{key}'} 占位符。纯文本格式自动作为 Authorization header 值；JSON 格式支持多个自定义 header
              </div>
            </div>
          )}

          {/* Provider Tag */}
          <div>
            <label className="block text-sm text-[#0F172A] mb-1.5">提供商标签</label>
            <Input
              placeholder="例如: DeepSeek, OpenAI, 通义千问"
              value={form.provider_tag}
              onChange={(e) => setForm({ ...form, provider_tag: e.target.value })}
              maxLength={100}
            />
          </div>

          {/* Default Params (collapsible) */}
          <Collapse
            ghost
            items={[{
              key: 'params',
              label: <span className="text-sm text-[#64748B]">默认参数配置</span>,
              children: (
                <div className="flex flex-col gap-4">
                  {/* Temperature */}
                  <div>
                    <label className="block text-sm text-[#0F172A] mb-1.5">
                      Temperature: {form.temperature}
                    </label>
                    <Slider
                      min={0}
                      max={2}
                      step={0.1}
                      value={form.temperature}
                      onChange={(val) => setForm({ ...form, temperature: val })}
                    />
                  </div>

                  {/* Max Tokens */}
                  <div>
                    <label className="block text-sm text-[#0F172A] mb-1.5">Max Tokens</label>
                    <InputNumber
                      min={1}
                      max={1000000}
                      value={form.max_tokens}
                      onChange={(val) => setForm({ ...form, max_tokens: val ?? 4096 })}
                      className="w-full"
                    />
                  </div>

                  {/* Context Window */}
                  <div>
                    <label className="block text-sm text-[#0F172A] mb-1.5">上下文窗口 (tokens)</label>
                    <InputNumber
                      min={1}
                      max={10000000}
                      value={form.context_window}
                      onChange={(val) => setForm({ ...form, context_window: val ?? 128000 })}
                      className="w-full"
                    />
                  </div>
                </div>
              ),
            }]}
          />
        </div>
      </Modal>
    </div>
  )
}
