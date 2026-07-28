/**
 * AgentEditorPage — full-featured Agent editor on a dedicated page.
 *
 * Replaces the cramped edit modal. Groups all fields into collapsible sections:
 * 基本信息 / Prompt 配置 / 执行参数 / 工具绑定. Loaded from a single agent
 * detail query; saves via PUT. Publish/archive actions in the header.
 *
 * Used for both editing existing agents and completing a freshly-created one
 * (backend POST only takes name+description, so the editor fills the rest).
 */
import { useState, useEffect, useMemo, type FC, type ReactNode } from 'react';
import {
  ArrowLeft, Bot, Save, Loader2, Rocket, Archive, RefreshCw, Cpu, Wrench,
  ChevronDown, ChevronRight, AlertTriangle, Sparkles, Plus, Trash2,
} from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { agentApi, agentKeys, type AgentUpdateInput } from '../services/agent-api';
import { modelApi, modelKeys } from '../services/model-api';
import { toolsApi, toolKeys, type BuiltinTool } from '../services/tools-api';
import { mcpApi, mcpKeys } from '../services/mcp-api';
import { workflowsApi, workflowKeys } from '../services/workflows-api';
import { knowledgeApi, knowledgeKeys } from '../services/knowledge-api';
import { toStudioAgent, fromStudioAgent } from '../services/adapters';
import { Select, type SelectOptionGroup } from './ui';
import { toast } from './ui/toast';
import type { Agent } from '../types';

const inputCls =
  'w-full px-3 py-2 bg-[#121214] border border-[#27272a] rounded-lg text-white placeholder:text-[#52525b] focus:outline-none focus:border-indigo-600 transition font-sans';

export function AgentEditorPage({
  agentId,
  onBack,
  onSaved,
}: {
  agentId: string;
  onBack: () => void;
  /** Called after a successful save; parent typically navigates to detail/chat. */
  onSaved?: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<Agent | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading, refetch } = useQuery({
    queryKey: agentKeys.detail(agentId),
    queryFn: () => agentApi.get(agentId),
  });

  // Sync remote data into the local form once loaded.
  useEffect(() => {
    if (data) setForm(toStudioAgent(data));
  }, [data]);

  // Tool-binding + model data sources.
  const { data: modelsData } = useQuery({
    queryKey: modelKeys.list({ status: 'active', page_size: 100 }),
    queryFn: () => modelApi.list({ status: 'active', page_size: 100 }),
  });
  const activeModels = modelsData?.items ?? [];
  // Group active models by compatibility_type (provider) for the model selector.
  const modelGroups: SelectOptionGroup[] = useMemo(() => {
    const buckets = new Map<string, SelectOptionGroup>();
    for (const m of activeModels) {
      const provider = (m.compatibility_type ?? 'other') as string;
      if (!buckets.has(provider)) buckets.set(provider, { label: provider, options: [] });
      // value = Model record _id (e.g. "model_01..."); backend get_llm_client
      // only resolves references with the "model_" prefix. Using model_id
      // (e.g. "glm-5.2") here would fall through to the env-var fallback
      // path and fail with "Missing credentials".
      buckets.get(provider)!.options.push({
        value: m.id,
        label: `${m.name} (${m.model_id})`,
      });
    }
    return Array.from(buckets.values());
  }, [activeModels]);
  const { data: builtinsData } = useQuery({
    queryKey: ['builtin-tools'],
    queryFn: () => toolsApi.listBuiltins(),
  });
  const builtinTools: BuiltinTool[] = builtinsData ?? [];
  const { data: skillsData } = useQuery({
    queryKey: toolKeys.list({ source: 'markdown', page_size: 100 }),
    queryFn: () => toolsApi.list({ source: 'markdown', page_size: 100 }),
  });
  const skillTools = skillsData?.items ?? [];
  const { data: mcpData } = useQuery({
    queryKey: mcpKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => mcpApi.list({ page: 1, page_size: 100 }),
  });
  const mcpConnections = mcpData?.items ?? [];
  const { data: workflowsData } = useQuery({
    queryKey: workflowKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => workflowsApi.list({ page: 1, page_size: 100 }),
  });
  const workflows = workflowsData?.items ?? [];
  const { data: kbData } = useQuery({
    queryKey: knowledgeKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => knowledgeApi.list({ page: 1, page_size: 100 }),
  });
  const knowledgeBases = kbData?.items ?? [];

  const updateM = useMutation({
    mutationFn: (input: AgentUpdateInput) => agentApi.update(agentId, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: agentKeys.all });
      setError(null);
      refetch();
      // 统一 Toast 反馈（替代旧的 savedMsg 临时横幅），然后交回父级跳转。
      toast.success('配置已保存');
      if (onSaved) onSaved(agentId);
    },
    onError: (e: unknown) => setError(e instanceof Error ? e.message : '保存失败'),
  });

  const publishM = useMutation({
    mutationFn: () => agentApi.publish(agentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: agentKeys.all }),
    onError: (e: unknown) => setError(e instanceof Error ? e.message : '发布失败'),
  });

  const archiveM = useMutation({
    mutationFn: () => agentApi.archive(agentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: agentKeys.all }),
    onError: (e: unknown) => setError(e instanceof Error ? e.message : '归档失败'),
  });

  const handleSave = () => {
    if (!form) return;
    if (!form.rolePrompt?.trim() || !form.taskPrompt?.trim()) {
      setError('「角色定义」和「任务描述」为必填项（对话执行校验）');
      return;
    }
    setError(null);
    updateM.mutate(fromStudioAgent(form));
  };

  if (isLoading || !form) {
    return (
      <div className="flex items-center justify-center py-16 text-[#71717a]">
        <Loader2 className="w-5 h-5 animate-spin mr-2" /> 载入 Agent 配置…
      </div>
    );
  }

  const isPublished = form.status === 'online';
  const set = (patch: Partial<Agent>) => setForm({ ...form, ...patch });

  return (
    <div className="space-y-4 max-w-3xl mx-auto pb-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button onClick={onBack} className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-white hover:bg-[#27272a] transition cursor-pointer">
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <div className="flex items-center gap-2 text-xs text-[#71717a]">
              <Bot className="w-3.5 h-3.5" />
              <span>智能体</span><span>/</span>
              <span className="text-white font-semibold">{form.name || '未命名'}</span>
            </div>
            <h2 className="text-sm font-bold text-white mt-0.5">编辑 Agent 配置</h2>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {isPublished ? (
            <button
              onClick={() => archiveM.mutate()}
              disabled={archiveM.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 border border-[#27272a] hover:bg-[#27272a] text-[#a1a1aa] hover:text-white rounded-lg text-xs font-semibold transition cursor-pointer disabled:opacity-60"
            >
              {archiveM.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Archive className="w-3.5 h-3.5" />}
              归档
            </button>
          ) : (
            <button
              onClick={() => publishM.mutate()}
              disabled={publishM.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-xs font-semibold transition cursor-pointer disabled:opacity-60"
            >
              {publishM.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
              发布
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-3 rounded-lg border border-rose-500/30 bg-rose-500/5 text-rose-300 text-xs">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* ── Section: 基本信息 ── */}
      <Section title="基本信息" icon={<Bot className="w-3.5 h-3.5" />} defaultOpen>
        <div className="grid grid-cols-[1fr_80px] gap-4">
          <Field label="智能体名称 *">
            <input className={inputCls} value={form.name} onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <Field label="头像">
            <input className={`${inputCls} text-center text-lg`} value={form.avatar} onChange={(e) => set({ avatar: e.target.value })} />
          </Field>
        </div>
        <Field label="职责描述">
          <input className={inputCls} value={form.description} onChange={(e) => set({ description: e.target.value })} placeholder="这个 Agent 专门解决什么问题…" />
        </Field>
      </Section>

      {/* ── Section: Prompt 配置 ── */}
      <Section title="Prompt 配置" icon={<Cpu className="w-3.5 h-3.5" />} defaultOpen>
        <Field label="角色定义 *（Role — 必填）">
          <textarea rows={2} className={`${inputCls} font-mono text-xs placeholder-[#52525b]`} placeholder="如：你是一位资深产品经理…" value={form.rolePrompt ?? ''} onChange={(e) => set({ rolePrompt: e.target.value })} />
        </Field>
        <Field label="任务描述 *（Task — 必填）">
          <textarea rows={3} className={`${inputCls} font-mono text-xs placeholder-[#52525b]`} placeholder="如：根据用户需求，输出功能拆解与优先级。" value={form.taskPrompt ?? ''} onChange={(e) => set({ taskPrompt: e.target.value })} />
        </Field>
        <div className="grid grid-cols-1 gap-4">
          <Field label="约束规则（Constraints · 可选）">
            <textarea rows={2} className={`${inputCls} font-mono text-xs placeholder-[#52525b]`} placeholder="如：回答必须用中文；不臆测。" value={form.constraintsPrompt ?? ''} onChange={(e) => set({ constraintsPrompt: e.target.value })} />
          </Field>
          <Field label="上下文信息（Context · 可选）">
            <textarea rows={2} className={`${inputCls} font-mono text-xs placeholder-[#52525b]`} placeholder="如：当前项目 meper-agent，技术栈 React+FastAPI。" value={form.contextPrompt ?? ''} onChange={(e) => set({ contextPrompt: e.target.value })} />
          </Field>
          <Field label="输出格式（Output Format · 可选）">
            <textarea rows={2} className={`${inputCls} font-mono text-xs placeholder-[#52525b]`} placeholder="如：用 Markdown 表格输出。" value={form.outputFormatPrompt ?? ''} onChange={(e) => set({ outputFormatPrompt: e.target.value })} />
          </Field>
          <Field label="补充说明（System Prompt · 可选）">
            <textarea rows={3} className={`${inputCls} font-mono text-xs`} value={form.systemPrompt} onChange={(e) => set({ systemPrompt: e.target.value })} />
          </Field>
        </div>
      </Section>

      {/* ── Section: 欢迎与引导（终端用户首屏） ── */}
      <Section title="欢迎与引导" icon={<Sparkles className="w-3.5 h-3.5" />} defaultOpen={false}>
        <Field label="欢迎词（Markdown，展示在终端用户首屏）">
          <textarea rows={3} className={`${inputCls} text-xs`} placeholder="如：你好！我是销售助理，可以问我业绩、客户、订单等相关问题。" value={form.welcomeMessage ?? ''} onChange={(e) => set({ welcomeMessage: e.target.value })} />
          <div className="text-[11px] text-slate-500 mt-1">留空则使用默认欢迎语。支持 Markdown 语法（加粗、列表等）。</div>
        </Field>
        <Field label="推荐问题 / 操作（终端用户可一键点击发送）">
          <div className="space-y-2">
            {(form.recommendedItems ?? []).map((item, idx) => (
              <div key={idx} className="rounded-lg border border-[#27272a] bg-[#121214] p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-[#71717a] font-mono">推荐项 #{idx + 1}</span>
                  <button type="button" onClick={() => set({ recommendedItems: (form.recommendedItems ?? []).filter((_, i) => i !== idx) })} className="flex items-center gap-1 text-[#ef4444] text-[10px] hover:underline cursor-pointer">
                    <Trash2 className="w-3 h-3" /> 删除
                  </button>
                </div>
                <input className={inputCls} placeholder="显示文案（必填），如：导出本月报表" value={item.label} onChange={(e) => set({ recommendedItems: (form.recommendedItems ?? []).map((it, i) => i === idx ? { ...it, label: e.target.value } : it) })} />
                <input className={inputCls} placeholder="实际发送内容（留空则同显示文案）" value={item.prompt} onChange={(e) => set({ recommendedItems: (form.recommendedItems ?? []).map((it, i) => i === idx ? { ...it, prompt: e.target.value } : it) })} />
              </div>
            ))}
            {(form.recommendedItems ?? []).length === 0 && (
              <div className="text-xs text-[#71717a] text-center py-3">暂无推荐项，点击下方按钮添加</div>
            )}
            <button
              type="button"
              onClick={() => set({ recommendedItems: [...(form.recommendedItems ?? []), { label: '', prompt: '' }] })}
              disabled={(form.recommendedItems ?? []).length >= 10}
              className="flex items-center gap-1.5 px-3 py-1.5 border border-[#27272a] hover:bg-[#27272a] text-[#a1a1aa] hover:text-white rounded-lg text-xs font-semibold transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Plus className="w-3.5 h-3.5" /> 添加推荐项
            </button>
            <div className="text-[11px] text-slate-500">最多 10 条。「实际发送内容」留空时，点击按钮直接发送「显示文案」。</div>
          </div>
        </Field>
      </Section>

      {/* ── Section: 执行参数 ── */}
      <Section title="执行参数" icon={<RefreshCw className="w-3.5 h-3.5" />} defaultOpen>
        <Field label="推理模型">
          <Select
            value={form.model || null}
            onChange={(v) => set({ model: v ?? '' })}
            placeholder={activeModels.length === 0 ? '暂无可用模型，请先在模型配置页添加' : '— 未选择 —'}
            groups={modelGroups}
          />
        </Field>
        <Field label={`思维活性 Temperature: ${form.temperature}`}>
          <input type="range" min="0" max="1.0" step="0.1" value={form.temperature} onChange={(e) => set({ temperature: parseFloat(e.target.value) })} className="w-full accent-indigo-500 cursor-pointer" />
        </Field>
        <Field label="最大重试次数（0-10）">
          <input type="number" min="0" max="10" className={`${inputCls} font-mono`} value={form.maxRetry ?? 3} onChange={(e) => set({ maxRetry: Math.max(0, Math.min(10, Number(e.target.value) || 0)) })} />
        </Field>
        <Field label="会话 Token 上限（0 = 全局默认）">
          <input type="number" min="0" max="10000000" step="10000" className={`${inputCls} font-mono`} value={form.maxTokens ?? 0} onChange={(e) => set({ maxTokens: Math.max(0, Number(e.target.value) || 0) })} placeholder="0 = 使用全局默认" />
          <div className="text-[11px] text-slate-500 mt-1">单次会话累计 Token 上限，超出后 Agent 自动停止。0 表示使用全局默认值（200000）。</div>
        </Field>
      </Section>

      {/* ── Section: 工具绑定 ── */}
      <Section title="工具绑定" icon={<Wrench className="w-3.5 h-3.5" />} defaultOpen>
        <ToolGroup title="内置工具 (Built-in)" hint={builtinTools.length === 0 ? '后端无内置工具' : undefined}>
          {builtinTools.map((t) => (
            <ToolChip key={`builtin:${t.name}`} label={t.name} checked={form.skills.includes(`builtin:${t.name}`)} onToggle={() => set({ skills: toggleSkill(form.skills, `builtin:${t.name}`) })} />
          ))}
        </ToolGroup>
        <ToolGroup title="技能 (Skills)" hint={skillTools.length === 0 ? '无已上传技能' : undefined}>
          {skillTools.map((t) => (
            <ToolChip key={t.id} label={t.name} checked={form.skills.includes(t.id)} onToggle={() => set({ skills: toggleSkill(form.skills, t.id) })} />
          ))}
        </ToolGroup>
        <ToolGroup title="MCP 连接" hint={mcpConnections.length === 0 ? '无 MCP 连接' : undefined}>
          {mcpConnections.map((c) => (
            <ToolChip key={`mcp:${c.id}`} label={c.name} checked={form.skills.includes(`mcp:${c.id}`)} onToggle={() => set({ skills: toggleSkill(form.skills, `mcp:${c.id}`) })} />
          ))}
        </ToolGroup>
        <ToolGroup title="工作流 (Workflows)" hint={workflows.length === 0 ? '无工作流' : undefined}>
          {workflows.map((w) => (
            <ToolChip key={w.id} label={w.name} checked={form.skills.includes(`workflow:${w.id}`)} onToggle={() => set({ skills: toggleSkill(form.skills, `workflow:${w.id}`) })} />
          ))}
        </ToolGroup>
        <ToolGroup title="知识库 (Knowledge Base)" hint={knowledgeBases.length === 0 ? '无知识库' : undefined}>
          {knowledgeBases.map((kb) => (
            <ToolChip key={`kb:${kb.id}`} label={kb.name} checked={form.skills.includes(`kb:${kb.id}`)} onToggle={() => set({ skills: toggleSkill(form.skills, `kb:${kb.id}`) })} />
          ))}
        </ToolGroup>
      </Section>

      {/* Sticky save bar — sticks to the bottom of the content scroll area
          (not the viewport), so it stays within the main column and never
          overlaps the left nav rail. Width follows the max-w-3xl content
          column. bg-[#09090b]/95 is theme-aware via the index.css override. */}
      <div className="sticky bottom-0 pt-4 pb-1 bg-[#09090b]/95 backdrop-blur-sm flex justify-end gap-3 z-30">
        <button onClick={onBack} className="px-4 py-2 border border-[#27272a] hover:bg-[#18181b] text-[#a1a1aa] hover:text-white rounded-lg cursor-pointer font-semibold text-xs">
          取消
        </button>
        <button
          onClick={handleSave}
          disabled={updateM.isPending}
          className="flex items-center gap-2 px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg cursor-pointer font-semibold text-xs disabled:opacity-60"
        >
          {updateM.isPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          保存配置
        </button>
      </div>
    </div>
  );
}

// ── Sub-components ──

/** Collapsible section with a title bar. */
const Section: FC<{ title: string; icon?: ReactNode; defaultOpen?: boolean; children: ReactNode }> = ({
  title, icon, defaultOpen = false, children,
}) => {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-xl border border-[#27272a] bg-[#18181b] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-4 py-3 text-left cursor-pointer hover:bg-[#1c1c1f] transition"
      >
        {open ? <ChevronDown className="w-4 h-4 text-[#71717a]" /> : <ChevronRight className="w-4 h-4 text-[#71717a]" />}
        {icon && <span className="text-indigo-400">{icon}</span>}
        <span className="text-sm font-bold text-white">{title}</span>
      </button>
      {open && <div className="px-4 pb-4 space-y-4">{children}</div>}
    </div>
  );
};

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-slate-400 font-semibold text-xs font-sans">{label}</label>
      {children}
    </div>
  );
}

/** Toggle a prefixed skill token in/out of the skills array. */
function toggleSkill(skills: string[], token: string): string[] {
  return skills.includes(token) ? skills.filter((s) => s !== token) : [...skills, token];
}

const ToolGroup: FC<{ title: string; hint?: string; children: ReactNode }> = ({ title, hint, children }) => (
  <div className="p-3 rounded-lg bg-[#121214] border border-[#27272a]">
    <p className="text-[10px] text-[#71717a] uppercase tracking-wider font-bold mb-2">{title}</p>
    {hint ? <p className="text-[11px] text-[#52525b] italic">{hint}</p> : <div className="flex flex-wrap gap-1.5">{children}</div>}
  </div>
);

const ToolChip: FC<{ label: string; checked: boolean; onToggle: () => void }> = ({ label, checked, onToggle }) => (
  <button
    type="button"
    onClick={onToggle}
    className={`px-2.5 py-1 rounded-lg text-[11px] font-semibold border transition cursor-pointer select-none ${
      checked ? 'bg-indigo-600 border-indigo-500 text-white' : 'bg-[#18181b] border-[#27272a] text-[#a1a1aa] hover:text-white hover:border-[#52525b]'
    }`}
  >
    {checked ? '✓ ' : ''}{label}
  </button>
);
