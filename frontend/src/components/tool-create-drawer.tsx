/**
 * ToolCreateDrawer — create custom tools (OpenAPI / Code).
 *
 * Two param types:
 * - User args: filled when Agent binds the tool (e.g. token, owner). Sensitive fields marked.
 * - LLM args: filled by LLM at runtime (e.g. state, query).
 *
 * Templates: {{user.xxx}} → user args, {{llm.xxx}} → LLM args.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Drawer, Button, Input, Select, Radio, Segmented, message, Divider, Checkbox, Tag } from 'antd'
import { GlobalOutlined, CodeOutlined, PlusOutlined } from '@ant-design/icons'
import { toolsApi, toolKeys } from '../services/tools-api'
import { agentKeys } from '../services/agent-api'
import ParamEditor from './param-editor'
import type { ToolParam } from './param-editor'
import { paramsToSchema } from './param-utils'
import KeyValueEditor from './key-value-editor'
import type { KVPair } from './key-value-editor'

const { TextArea } = Input

export interface ToolCreateDrawerProps {
  open: boolean
  onClose: () => void
}

type CreateMode = 'manual' | 'openapi_import'
type ToolSource = 'openapi' | 'code'

export default function ToolCreateDrawer({ open, onClose }: ToolCreateDrawerProps) {
  const queryClient = useQueryClient()

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [source, setSource] = useState<ToolSource>('openapi')
  const [createMode, setCreateMode] = useState<CreateMode>('manual')

  // User params (with sensitive flag)
  const [userParams, setUserParams] = useState<(ToolParam & { sensitive?: boolean })[]>([])

  // LLM params
  const [llmParams, setLlmParams] = useState<ToolParam[]>([])

  // HTTP config
  const [method, setMethod] = useState('GET')
  const [url, setUrl] = useState('')
  const [headers, setHeaders] = useState<KVPair[]>([])
  const [queryParams, setQueryParams] = useState<KVPair[]>([])

  // Code
  const [code, setCode] = useState('')

  // OpenAPI import
  const [openapiSpec, setOpenapiSpec] = useState('')

  const createMutation = useMutation({
    mutationFn: toolsApi.createCustom,
    onSuccess: () => {
      message.success('工具创建成功')
      queryClient.invalidateQueries({ queryKey: toolKeys.customTools() })
      queryClient.invalidateQueries({ queryKey: toolKeys.lists() })
      queryClient.invalidateQueries({ queryKey: agentKeys.all })
      resetForm()
      onClose()
    },
  })

  const resetForm = () => {
    setName(''); setDescription(''); setSource('openapi'); setCreateMode('manual')
    setUserParams([]); setLlmParams([])
    setMethod('GET'); setUrl(''); setHeaders([]); setQueryParams([])
    setCode(''); setOpenapiSpec('')
  }

  // ── Build user_args_schema with sensitive flags ──
  const buildUserArgsSchema = () => {
    const properties: Record<string, unknown> = {}
    const required: string[] = []
    for (const p of userParams) {
      properties[p.name] = {
        type: p.type,
        ...(p.description ? { description: p.description } : {}),
        ...(p.sensitive ? { sensitive: true } : {}),
      }
      if (p.required) required.push(p.name)
    }
    return { type: 'object', properties, ...(required.length ? { required } : {}) }
  }

  const parseOpenApi = () => {
    try {
      const spec = JSON.parse(openapiSpec)
      const paths = spec.paths || {}
      const firstPath = Object.keys(paths)[0]
      if (!firstPath) { message.warning('未找到 paths'); return }
      const pathItem = paths[firstPath]
      const firstMethod = Object.keys(pathItem).find(m => ['get', 'post', 'put', 'delete'].includes(m))
      if (!firstMethod) { message.warning('未找到 method'); return }
      const operation = pathItem[firstMethod]
      if (operation.operationId && !name) setName(operation.operationId)
      if (operation.summary && !description) setDescription(operation.summary)
      setMethod(firstMethod.toUpperCase())
      const serverUrl = spec.servers?.[0]?.url || ''
      setUrl(serverUrl + firstPath)
      const opParams = operation.parameters || []
      const toolParams: ToolParam[] = opParams.map((p: Record<string, unknown>) => {
        const schema = p.schema as Record<string, unknown> | undefined
        return {
          name: p.name as string || '',
          type: ((schema?.type as string) || 'string') as ToolParam['type'],
          required: p.required as boolean || false,
          description: (p.description as string) || '',
        }
      })
      setLlmParams(toolParams)
      message.success(`解析成功：${toolParams.length} 个参数`)
    } catch (err) {
      message.error('JSON 解析失败：' + (err as Error).message)
    }
  }

  const handleSubmit = () => {
    if (!name.trim()) { message.warning('请输入工具名称'); return }
    const kvToDict = (kvs: KVPair[]) => {
      const d: Record<string, string> = {}
      for (const kv of kvs) { if (kv.key.trim()) d[kv.key] = kv.value }
      return d
    }
    if (source === 'openapi') {
      if (!url.trim()) { message.warning('请输入 URL'); return }
      createMutation.mutate({
        name: name.trim(), description, source: 'openapi',
        user_args_schema: buildUserArgsSchema(),
        llm_args_schema: paramsToSchema(llmParams),
        endpoint: { method, url, headers: kvToDict(headers), params: kvToDict(queryParams) },
      })
    } else {
      if (!code.trim()) { message.warning('请输入代码'); return }
      createMutation.mutate({
        name: name.trim(), description, source: 'code',
        user_args_schema: buildUserArgsSchema(),
        llm_args_schema: paramsToSchema(llmParams),
        code,
      })
    }
  }

  const userParamNames = userParams.map(p => p.name)
  const llmParamNames = llmParams.map(p => p.name)

  return (
    <Drawer
      title="创建自定义工具" open={open} onClose={onClose} width={720} destroyOnClose
      extra={
        <div className="flex gap-2">
          <Button onClick={onClose}>取消</Button>
          <Button type="primary" onClick={handleSubmit} loading={createMutation.isPending}>创建</Button>
        </div>
      }
    >
      <div className="flex flex-col gap-5 pb-8">
        {/* 基本信息 */}
        <Section title="基本信息">
          <div className="space-y-3">
            <div>
              <Label required>工具名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如: github_list_issues" showCount maxLength={100} />
            </div>
            <div>
              <Label>描述</Label>
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="如: 列出 GitHub Issues" />
            </div>
            <div>
              <Label>工具类型</Label>
              <Segmented value={source} onChange={(v) => setSource(v as ToolSource)} block
                options={[
                  { label: 'OpenAPI（HTTP）', value: 'openapi', icon: <GlobalOutlined /> },
                  { label: 'Code（Python）', value: 'code', icon: <CodeOutlined /> },
                ]}
              />
            </div>
          </div>
        </Section>

        {/* 创建方式 (OpenAPI) */}
        {source === 'openapi' && (
          <Section title="创建方式">
            <Radio.Group value={createMode} onChange={(e) => setCreateMode(e.target.value)}>
              <Radio value="manual">手动配置</Radio>
              <Radio value="openapi_import">导入 OpenAPI</Radio>
            </Radio.Group>
            {createMode === 'openapi_import' && (
              <div className="mt-3">
                <Label>粘贴 OpenAPI JSON</Label>
                <TextArea rows={6} value={openapiSpec} onChange={(e) => setOpenapiSpec(e.target.value)} className="font-mono text-xs" />
                <Button className="mt-2" size="small" onClick={parseOpenApi}>解析并填充</Button>
              </div>
            )}
          </Section>
        )}

        {/* 用户参数 */}
        <Section title="用户参数" subtitle="Agent 绑定工具时由用户填入。不暴露给 LLM。标记敏感的字段加密存储。模板用 {{user.字段名}} 引用。">
          <UserParamEditor value={userParams} onChange={setUserParams} />
        </Section>

        {/* LLM 参数 */}
        <Section title="LLM 参数" subtitle="LLM 调用工具时根据对话上下文填入。模板用 {{llm.字段名}} 引用。">
          <ParamEditor value={llmParams} onChange={setLlmParams} />
        </Section>

        {/* HTTP 配置 */}
        {source === 'openapi' && (
          <Section title="HTTP 配置" subtitle="用 {{user.xxx}} 引用用户参数，{{llm.xxx}} 引用 LLM 参数">
            <div className="space-y-4">
              <div className="grid grid-cols-[100px_1fr] gap-3 items-center">
                <Label required>请求方法</Label>
                <Select value={method} onChange={setMethod} className="w-32"
                  options={['GET','POST','PUT','DELETE'].map(m => ({ value: m, label: m }))} />
              </div>
              <div>
                <Label required>URL</Label>
                <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/{{user.path}}/resource" />
                {userParamNames.length > 0 && (
                  <div className="flex gap-1.5 mt-1 flex-wrap">
                    {userParamNames.map(n => <Tag key={n} className="!m-0 !text-[10px]" color="orange">{`{{user.${n}}}`}</Tag>)}
                    {llmParamNames.map(n => <Tag key={n} className="!m-0 !text-[10px]" color="blue">{`{{llm.${n}}}`}</Tag>)}
                  </div>
                )}
              </div>
              <Divider className="!my-2" />
              <div>
                <Label>Headers</Label>
                <KeyValueEditor value={headers} onChange={setHeaders}
                  keyPlaceholder="Header 名" valuePlaceholder={'如: Bearer {{user.token}}'} emptyHint="暂无 Headers" />
              </div>
              <div>
                <Label>Query Params</Label>
                <KeyValueEditor value={queryParams} onChange={setQueryParams}
                  keyPlaceholder="参数名" valuePlaceholder={'如: {{llm.state}}'} emptyHint="暂无 Query Params" />
              </div>
            </div>
          </Section>
        )}

        {/* Code */}
        {source === 'code' && (
          <Section title="Python 代码" subtitle="用户参数经环境变量 USER_xxx 注入。LLM 参数作为函数参数传入。">
            {userParamNames.length > 0 && (
              <div className="mb-2 px-3 py-2 bg-[#FFFBEB] rounded-lg border border-[#FCD34D]">
                <p className="text-[11px] text-[#92400E] mb-1">用户参数通过环境变量注入：</p>
                {userParamNames.map(f => <code key={f} className="text-xs text-[#0F172A] font-mono block">os.environ['USER_{f}']</code>)}
              </div>
            )}
            {llmParamNames.length > 0 && (
              <div className="mb-2 px-3 py-2 bg-[#F0F7FF] rounded-lg border border-[#93C5FD]">
                <p className="text-[11px] text-[#2563EB] mb-1">函数签名（LLM 参数）：</p>
                <code className="text-xs text-[#0F172A] font-mono">def {name || 'my_tool'}({llmParamNames.join(', ')}) -&gt; str:</code>
              </div>
            )}
            <TextArea rows={10} value={code} onChange={(e) => setCode(e.target.value)}
              placeholder={`def ${name || 'my_tool'}(${llmParamNames.join(', ')}) -> str:\n    return "result"`} className="font-mono text-sm" />
            <p className="text-[11px] text-[#94A3B8] mt-1">代码在 Docker 沙箱中执行。</p>
          </Section>
        )}
      </div>
    </Drawer>
  )
}

// ── UserParamEditor: like ParamEditor but with sensitive checkbox ──

interface UserToolParam extends ToolParam { sensitive?: boolean }

function UserParamEditor({ value, onChange }: { value: UserToolParam[]; onChange: (v: UserToolParam[]) => void }) {
  const [adding, setAdding] = useState(false)
  const [newParam, setNewParam] = useState<UserToolParam>({ name: '', type: 'string', required: true, description: '', sensitive: false })

  const addParam = () => {
    if (!newParam.name.trim()) return
    onChange([...value, { ...newParam, name: newParam.name.trim() }])
    setNewParam({ name: '', type: 'string', required: true, description: '', sensitive: false })
    setAdding(false)
  }

  return (
    <div>
      <div className="space-y-1.5 mb-3">
        {value.length === 0 ? (
          <div className="text-xs text-[#94A3B8] py-3 text-center bg-[#F8FAFC] rounded-lg border border-dashed border-[#E2E8F0]">
            暂无用户参数（如 token、owner 等固定配置项）
          </div>
        ) : (
          <div className="border border-[#E2E8F0] rounded-lg overflow-hidden divide-y divide-[#F1F5F9]">
            {value.map((param, index) => (
              <div key={index} className="flex items-center gap-2 px-3 py-2 hover:bg-[#F8FAFC]">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-[#0F172A]">{param.name}</span>
                    {param.sensitive && <Tag color="red" className="!m-0 !text-[10px]">敏感</Tag>}
                    {param.required && <Tag color="blue" className="!m-0 !text-[10px]">必填</Tag>}
                  </div>
                  {param.description && <div className="text-xs text-[#94A3B8] mt-0.5">{param.description}</div>}
                </div>
                <Button size="small" type="text" danger icon={<PlusOutlined style={{ transform: 'rotate(45deg)' }} />}
                  onClick={() => onChange(value.filter((_, i) => i !== index))} />
              </div>
            ))}
          </div>
        )}
      </div>

      {adding ? (
        <div className="border border-[#FCD34D] rounded-lg p-3 bg-[#FFFBEB] space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-[#64748B] mb-1">参数名 *</label>
              <Input size="small" value={newParam.name} onChange={(e) => setNewParam({ ...newParam, name: e.target.value })} placeholder="如: token" />
            </div>
            <div>
              <label className="block text-xs text-[#64748B] mb-1">类型</label>
              <Select size="small" value={newParam.type} onChange={(v) => setNewParam({ ...newParam, type: v })} className="w-full"
                options={[{value:'string',label:'字符串'},{value:'integer',label:'整数'},{value:'number',label:'数字'},{value:'boolean',label:'布尔'}]} />
            </div>
          </div>
          <div>
            <label className="block text-xs text-[#64748B] mb-1">说明</label>
            <Input size="small" value={newParam.description} onChange={(e) => setNewParam({ ...newParam, description: e.target.value })} placeholder="如: GitHub Token" />
          </div>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <Checkbox checked={newParam.required} onChange={(e) => setNewParam({ ...newParam, required: e.target.checked })}>
                <span className="text-xs text-[#64748B]">必填</span>
              </Checkbox>
              <Checkbox checked={newParam.sensitive} onChange={(e) => setNewParam({ ...newParam, sensitive: e.target.checked })}>
                <span className="text-xs text-[#EF4444]">敏感（加密存储）</span>
              </Checkbox>
            </div>
            <div className="flex gap-2">
              <Button size="small" onClick={() => { setAdding(false); setNewParam({ name:'',type:'string',required:true,description:'',sensitive:false }) }}>取消</Button>
              <Button size="small" type="primary" onClick={addParam} disabled={!newParam.name.trim()}>添加</Button>
            </div>
          </div>
        </div>
      ) : (
        <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => setAdding(true)} className="w-full">添加用户参数</Button>
      )}
    </div>
  )
}

// ── UI helpers ──

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-[#0F172A]">{title}</h3>
        {subtitle && <p className="text-[11px] text-[#94A3B8] mt-0.5">{subtitle}</p>}
      </div>
      {children}
    </div>
  )
}

function Label({ children, required }: { children: React.ReactNode; required?: boolean }) {
  return <label className="block text-sm text-[#0F172A] mb-1.5">{children}{required && <span className="text-[#EF4444] ml-0.5">*</span>}</label>
}
