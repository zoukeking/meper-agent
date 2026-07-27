/**
 * McpManagePage — MCP connection management.
 *
 * Full CRUD + connectivity test (test → auto-discover) + view-tools modal.
 * Renders connection status with color-coded dots. Ported from
 * frontend/src/pages/mcp-page.tsx, native Tailwind (lucide icons).
 */
import { useState, type FormEvent, type ReactNode } from 'react';
import {
  Plug, Plus, Search, Zap, Pencil, Trash2, Eye, X, Loader2,
  CircleCheck, CircleSlash, AlertCircle, PlugZap,
} from 'lucide-react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  mcpApi,
  mcpKeys,
  type McpConnection,
  type McpConnectionCreateInput,
  type ConnectionStatus,
  type McpAuthType,
} from '../services/mcp-api';
import { toolsApi, toolKeys } from '../services/tools-api';
import { Select } from './ui';
import { confirmDialog } from './ui/confirm';
import { toast } from './ui/toast';

const STATUS_STYLES: Record<ConnectionStatus, { label: string; color: string; Icon: typeof CircleCheck }> = {
  connected: { label: '已连接', color: 'text-emerald-400', Icon: CircleCheck },
  connecting: { label: '连接中', color: 'text-sky-400', Icon: Loader2 },
  disconnected: { label: '未连接', color: 'text-zinc-400', Icon: CircleSlash },
  error: { label: '错误', color: 'text-rose-400', Icon: AlertCircle },
};

const AUTH_LABELS: Record<McpAuthType, string> = {
  none: '无认证',
  api_key: 'API Key',
  bearer_token: 'Bearer Token',
  basic: 'Basic Auth',
};

/** Inline guidance shown above the structured auth fields. Tells the user what
 * the selected auth_type does and which header it produces. */
const AUTH_HINTS: Record<Exclude<McpAuthType, 'none'>, string> = {
  api_key: '通过自定义请求头携带密钥（默认 X-API-Key）。用于上游服务以 API Key 鉴权的场景。',
  bearer_token: '生成标准请求头 Authorization: Bearer <token>。用于上游服务以 OAuth/JWT 鉴权的场景。',
  basic: '用户名密码做 Base64 编码，生成请求头 Authorization: Basic <base64>。用于 HTTP 基础认证。',
};

const PROTOCOL_OPTIONS = ['streamable-http', 'sse'];
const AUTH_OPTIONS: McpAuthType[] = ['none', 'api_key', 'bearer_token', 'basic'];

type ConnForm = Omit<McpConnectionCreateInput, 'auth_config' | 'default_params'> & {
  // Structured auth fields — assembled into auth_config on submit. Keep them
  // flat (instead of editing raw JSON) so users pick an auth_type and just fill
  // labeled inputs. auth_config keys follow the backend contract:
  //   api_key:       { api_key, header_name? }
  //   bearer_token:  { token }
  //   basic:         { username, password }
  apiKey: string;
  headerName: string;
  token: string;
  username: string;
  password: string;
  default_params: string; // JSON string
};

function emptyForm(): ConnForm {
  return {
    name: '',
    description: '',
    url: '',
    protocol: 'streamable-http',
    auth_type: 'none',
    apiKey: '',
    headerName: 'X-API-Key',
    token: '',
    username: '',
    password: '',
    timeout: 30,
    default_params: '',
  };
}

function connToForm(c: McpConnection): ConnForm {
  const cfg = c.auth_config ?? {};
  return {
    name: c.name,
    description: c.description ?? '',
    url: c.url,
    protocol: c.protocol ?? 'streamable-http',
    auth_type: c.auth_type ?? 'none',
    // Read each auth_config value defensively: older records may use either
    // shape, and masked responses ("***") are preserved for unchanged fields.
    apiKey: String(cfg.api_key ?? ''),
    headerName: String(cfg.header_name ?? 'X-API-Key'),
    token: String(cfg.token ?? ''),
    username: String(cfg.username ?? ''),
    password: String(cfg.password ?? ''),
    timeout: c.timeout ?? 30,
    default_params: c.default_params && Object.keys(c.default_params).length ? JSON.stringify(c.default_params, null, 2) : '',
  };
}

/**
 * Build the auth_config object for the current auth_type from the structured
 * form fields. Mirrors the backend's `_build_headers` contract exactly:
 *   api_key → { api_key, header_name? }  (header_name omitted if default)
 *   bearer_token → { token }
 *   basic → { username, password }
 * Returns undefined for 'none' so the field is omitted from the payload.
 */
function buildAuthConfig(form: ConnForm): Record<string, string> | undefined {
  switch (form.auth_type) {
    case 'api_key': {
      const cfg: Record<string, string> = { api_key: form.apiKey.trim() };
      if (form.headerName.trim() && form.headerName.trim() !== 'X-API-Key') {
        cfg.header_name = form.headerName.trim();
      }
      return Object.keys(cfg).length ? cfg : undefined;
    }
    case 'bearer_token':
      return form.token.trim() ? { token: form.token.trim() } : undefined;
    case 'basic': {
      const u = form.username.trim();
      const p = form.password;
      return u ? { username: u, password: p } : undefined;
    }
    default:
      return undefined;
  }
}

/** Parse a JSON text field, returning {} on empty. Throws on invalid JSON. */
function parseJsonOrEmpty(text: string, field: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  try {
    return JSON.parse(trimmed);
  } catch {
    throw new Error(`${field} 不是有效的 JSON`);
  }
}

export function McpManagePage() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState('');
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<McpConnection | null>(null);
  const [form, setForm] = useState<ConnForm>(emptyForm());
  const [error, setError] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [viewingConn, setViewingConn] = useState<McpConnection | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: mcpKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => mcpApi.list({ page: 1, page_size: 100 }),
  });
  const connections = data?.items ?? [];

  const filtered = search.trim()
    ? connections.filter((c) => c.name.toLowerCase().includes(search.trim().toLowerCase()))
    : connections;

  const stats = {
    total: connections.length,
    connected: connections.filter((c) => c.status === 'connected').length,
    disconnected: connections.filter((c) => c.status === 'disconnected').length,
    error: connections.filter((c) => c.status === 'error').length,
  };

  const createM = useMutation({
    mutationFn: (input: McpConnectionCreateInput) => mcpApi.create(input),
    onSuccess: async () => {
      queryClient.invalidateQueries({ queryKey: mcpKeys.all });
      setError(null);
      setCreating(false);
    },
    onError: (e) => setError(e instanceof Error ? e.message : '创建连接失败'),
  });

  const updateM = useMutation({
    mutationFn: ({ id, input }: { id: string; input: McpConnectionCreateInput }) => mcpApi.update(id, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: mcpKeys.all });
      setError(null);
      setEditing(null);
    },
    onError: (e) => setError(e instanceof Error ? e.message : '保存连接失败'),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) => mcpApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: mcpKeys.all }),
    onError: (e) => setError(e instanceof Error ? e.message : '删除失败'),
  });

  const testM = useMutation({
    mutationFn: async (conn: McpConnection) => {
      const res = await mcpApi.test(conn.id);
      if (res.success) {
        // test ok → auto discover tools
        await mcpApi.discover(conn.id);
        queryClient.invalidateQueries({ queryKey: mcpKeys.all });
        queryClient.invalidateQueries({ queryKey: toolKeys.all });
      }
      return res;
    },
    onSuccess: (res, conn) => {
      setTestingId(null);
      if (res.success) {
        toast.success(`「${conn.name}」连接成功，已发现 ${res.tool_count ?? 0} 个工具`);
      } else {
        toast.error(`「${conn.name}」连接失败：${res.error || '未知错误'}`);
      }
    },
    onError: (e, conn) => {
      setTestingId(null);
    },
  });

  const buildPayload = (): McpConnectionCreateInput => {
    const auth_config = buildAuthConfig(form);
    const default_params = parseJsonOrEmpty(form.default_params, 'default_params');
    return {
      name: form.name.trim(),
      description: form.description.trim() || undefined,
      url: form.url.trim(),
      protocol: form.protocol,
      auth_type: form.auth_type,
      ...(auth_config ? { auth_config } : {}),
      ...(form.timeout ? { timeout: form.timeout } : {}),
      ...(Object.keys(default_params).length ? { default_params } : {}),
    };
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const payload = buildPayload();
      if (editing) updateM.mutate({ id: editing.id, input: payload });
      else createM.mutate(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : '表单校验失败');
    }
  };

  const handleDelete = async (c: McpConnection) => {
    const ok = await confirmDialog({
      title: `删除 MCP 连接「${c.name}」？`,
      description: '关联的 MCP 工具将一并移除。',
      okText: '删除',
      danger: true,
    });
    if (!ok) return;
    setError(null);
    deleteM.mutate(c.id);
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-white flex items-center gap-2">
            <Plug className="w-5 h-5 text-indigo-400" />
            外部工具接入
          </h2>
          <p className="text-xs text-[#71717a] mt-1">
            管理 Model Context Protocol 连接：测试连通、发现工具、配置认证。
          </p>
        </div>
        <button
          onClick={() => { setError(null); setForm(emptyForm()); setCreating(true); }}
          className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-semibold transition cursor-pointer shadow-md shadow-indigo-600/20"
        >
          <Plus className="w-4 h-4" /> 新建连接
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: '连接总数', value: stats.total, color: 'text-white' },
          { label: '已连接', value: stats.connected, color: 'text-emerald-400' },
          { label: '未连接', value: stats.disconnected, color: 'text-zinc-400' },
          { label: '错误', value: stats.error, color: 'text-rose-400' },
        ].map((s) => (
          <div key={s.label} className="p-4 rounded-xl border border-[#27272a] bg-[#18181b]">
            <p className="text-[10px] text-[#71717a] uppercase tracking-widest font-bold">{s.label}</p>
            <p className={`text-2xl font-bold mt-1 ${s.color}`}>{s.value}</p>
          </div>
        ))}
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#71717a]" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索连接名称"
          className="w-full pl-9 pr-3 py-2 bg-[#121214] border border-[#27272a] rounded-lg text-sm text-white placeholder:text-[#52525b] focus:outline-none focus:border-indigo-500 transition"
        />
      </div>

      {error && (
        <div className="p-3 rounded-lg border border-rose-500/30 bg-rose-500/5 text-rose-300 text-xs">{error}</div>
      )}

      {/* Table */}
      <div className="rounded-xl border border-[#27272a] bg-[#18181b] overflow-hidden">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-[#71717a]">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中…
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-[#52525b]">
            <Plug className="w-8 h-8 mb-2 opacity-40" />
            <p className="text-sm">暂无 MCP 连接，点击右上角新建</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[#27272a] text-[#71717a] text-[11px] uppercase tracking-wider">
                <th className="text-left font-semibold px-4 py-3">名称</th>
                <th className="text-left font-semibold px-4 py-3">URL</th>
                <th className="text-left font-semibold px-4 py-3">协议</th>
                <th className="text-left font-semibold px-4 py-3">状态</th>
                <th className="text-left font-semibold px-4 py-3">工具数</th>
                <th className="text-right font-semibold px-4 py-3">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((c) => {
                const st = STATUS_STYLES[c.status] ?? STATUS_STYLES.disconnected;
                return (
                  <tr key={c.id} className="border-b border-[#27272a] last:border-0 hover:bg-[#1c1c1f] transition">
                    <td className="px-4 py-3">
                      <p className="font-semibold text-white">{c.name}</p>
                      {c.description && <p className="text-[11px] text-[#71717a] line-clamp-1">{c.description}</p>}
                    </td>
                    <td className="px-4 py-3 text-[11px] text-[#a1a1aa] font-mono max-w-[200px] truncate">{c.url}</td>
                    <td className="px-4 py-3 text-[11px] text-[#a1a1aa]">{c.protocol ?? '—'}</td>
                    <td className="px-4 py-3">
                      <span className={`flex items-center gap-1.5 text-[11px] font-bold ${st.color}`}>
                        <st.Icon className={`w-3.5 h-3.5 ${c.status === 'connecting' ? 'animate-spin' : ''}`} />
                        {st.label}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-[11px] text-[#a1a1aa] font-mono">{c.tool_count ?? 0}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <button onClick={() => { setError(null); testM.mutate(c); setTestingId(c.id); }} title="测试连接 + 发现工具" disabled={testingId === c.id}
                          className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-amber-400 hover:bg-[#27272a] transition cursor-pointer disabled:opacity-50">
                          {testingId === c.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
                        </button>
                        <button onClick={() => setViewingConn(c)} title="查看工具"
                          className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-sky-400 hover:bg-[#27272a] transition cursor-pointer">
                          <Eye className="w-4 h-4" />
                        </button>
                        <button onClick={() => { setError(null); setForm(connToForm(c)); setEditing(c); }} title="编辑"
                          className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-indigo-400 hover:bg-[#27272a] transition cursor-pointer">
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button onClick={() => handleDelete(c)} title="删除"
                          className="p-1.5 rounded-lg text-[#a1a1aa] hover:text-rose-400 hover:bg-[#27272a] transition cursor-pointer">
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Create / Edit Modal */}
      {(creating || editing) && (
        <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4 z-50 animate-fade-in">
          <div className="w-full max-w-xl max-h-[90vh] overflow-y-auto bg-[#121214] border border-[#27272a] rounded-2xl shadow-2xl">
            <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272a] sticky top-0 bg-[#121214] z-10">
              <h3 className="text-sm font-bold text-white flex items-center gap-2">
                <PlugZap className="w-4 h-4 text-indigo-400" />
                {editing ? `编辑连接 · ${editing.name}` : '新建 MCP 连接'}
              </h3>
              <button onClick={() => { setCreating(false); setEditing(null); setError(null); }}
                className="p-1 rounded-lg text-[#71717a] hover:text-white hover:bg-[#27272a] transition cursor-pointer">
                <X className="w-4 h-4" />
              </button>
            </div>
            <form onSubmit={handleSubmit} className="p-5 space-y-4 text-xs">
              <div className="grid grid-cols-2 gap-4">
                <Field label="连接名称 *"><input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className={inputCls} /></Field>
                <Field label="协议">
                  <Select
                    value={form.protocol}
                    onChange={(v) => setForm({ ...form, protocol: (v ?? 'streamable-http') as string })}
                    options={PROTOCOL_OPTIONS.map((p) => ({ value: p, label: p }))}
                  />
                </Field>
              </div>
              <Field label="端点 URL *"><input required value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://api.example.com/v1/tools" className={inputCls} /></Field>
              <Field label="描述"><input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className={inputCls} /></Field>
              <div className="grid grid-cols-2 gap-4">
                <Field label="认证方式">
                  <Select
                    value={form.auth_type}
                    onChange={(v) => setForm({ ...form, auth_type: (v ?? 'none') as McpAuthType })}
                    options={AUTH_OPTIONS.map((a) => ({ value: a, label: AUTH_LABELS[a] }))}
                  />
                </Field>
                <Field label="超时 (秒)"><input type="number" min="1" max="300" value={form.timeout} onChange={(e) => setForm({ ...form, timeout: Number(e.target.value) })} className={inputCls} /></Field>
              </div>
              {form.auth_type !== 'none' && (
                <div className="space-y-3 rounded-lg border border-[#27272a] bg-[#18181b]/40 p-3">
                  <p className="text-[10px] text-[#71717a] leading-relaxed">
                    {AUTH_HINTS[form.auth_type]}
                  </p>
                  {form.auth_type === 'api_key' && (
                    <div className="grid grid-cols-2 gap-3">
                      <Field label="API Key *" hint={editing && form.apiKey === '***' ? '已配置，保持 *** 不修改；重新输入以更新' : undefined}>
                        <input
                          required
                          value={form.apiKey}
                          onChange={(e) => setForm({ ...form, apiKey: e.target.value })}
                          placeholder="sk-..."
                          className={inputCls}
                          autoComplete="off"
                        />
                      </Field>
                      <Field label="请求头名称">
                        <input
                          value={form.headerName}
                          onChange={(e) => setForm({ ...form, headerName: e.target.value })}
                          placeholder="X-API-Key"
                          className={inputCls}
                        />
                      </Field>
                    </div>
                  )}
                  {form.auth_type === 'bearer_token' && (
                    <Field label="Token *" hint={editing && form.token === '***' ? '已配置，保持 *** 不修改；重新输入以更新' : undefined}>
                      <input
                        required
                        value={form.token}
                        onChange={(e) => setForm({ ...form, token: e.target.value })}
                        placeholder="粘贴 Bearer Token（无需加 Bearer 前缀）"
                        className={inputCls}
                        autoComplete="off"
                      />
                    </Field>
                  )}
                  {form.auth_type === 'basic' && (
                    <div className="grid grid-cols-2 gap-3">
                      <Field label="用户名 *">
                        <input
                          required
                          value={form.username}
                          onChange={(e) => setForm({ ...form, username: e.target.value })}
                          className={inputCls}
                          autoComplete="off"
                        />
                      </Field>
                      <Field label="密码" hint={editing && form.password === '***' ? '已配置，保持 *** 不修改；重新输入以更新' : undefined}>
                        <input
                          type="password"
                          value={form.password}
                          onChange={(e) => setForm({ ...form, password: e.target.value })}
                          className={inputCls}
                          autoComplete="new-password"
                        />
                      </Field>
                    </div>
                  )}
                </div>
              )}
              <Field label="默认参数 (JSON，可选)">
                <textarea value={form.default_params} onChange={(e) => setForm({ ...form, default_params: e.target.value })} placeholder="{}" rows={2} className={`${inputCls} font-mono resize-y`} />
              </Field>
              <div className="flex justify-end gap-3 pt-4 border-t border-[#27272a]">
                <button type="button" onClick={() => { setCreating(false); setEditing(null); setError(null); }}
                  className="px-4 py-2 border border-[#27272a] hover:bg-[#18181b] text-[#a1a1aa] hover:text-white rounded-lg cursor-pointer font-semibold">取消</button>
                <button type="submit" disabled={createM.isPending || updateM.isPending}
                  className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg shadow-md cursor-pointer font-semibold disabled:opacity-60 flex items-center gap-2">
                  {(createM.isPending || updateM.isPending) && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  {editing ? '保存' : '创建'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* View-tools Modal */}
      {viewingConn && <ViewToolsModal conn={viewingConn} onClose={() => setViewingConn(null)} />}
    </div>
  );
}

function ViewToolsModal({ conn, onClose }: { conn: McpConnection; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: [...toolKeys.lists(), { mcp: conn.id }],
    queryFn: () => toolsApi.list({ mcp_connection_id: conn.id, page_size: 100 }),
  });
  const tools = data?.items ?? [];
  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4 z-50 animate-fade-in">
      <div className="w-full max-w-2xl max-h-[85vh] flex flex-col bg-[#121214] border border-[#27272a] rounded-2xl shadow-2xl">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#27272a]">
          <h3 className="text-sm font-bold text-white">MCP 工具列表 — {conn.name}</h3>
          <button onClick={onClose} className="p-1 rounded-lg text-[#71717a] hover:text-white hover:bg-[#27272a] transition cursor-pointer">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-5 overflow-y-auto flex-1 space-y-2">
          {isLoading ? (
            <div className="flex items-center justify-center py-8 text-[#71717a]"><Loader2 className="w-5 h-5 animate-spin mr-2" />加载中…</div>
          ) : tools.length === 0 ? (
            <div className="text-center py-8 text-[#52525b] text-sm">该连接暂无工具。请先点「测试连接」触发发现。</div>
          ) : (
            tools.map((t) => {
              const props = (t.input_schema?.properties ?? {}) as Record<string, unknown>;
              const required = (t.input_schema?.required ?? []) as string[];
              const paramNames = Object.keys(props);
              return (
                <div key={t.id} className="p-3 rounded-lg border border-[#27272a] bg-[#18181b]">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-bold text-white text-xs">{t.name}</span>
                    {paramNames.length > 0 && (
                      <div className="flex flex-wrap gap-1 ml-auto">
                        {paramNames.map((p) => (
                          <span key={p} className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${required.includes(p) ? 'bg-sky-500/10 text-sky-400' : 'bg-[#121214] text-[#71717a]'}`}>
                            {p}{required.includes(p) ? '*' : ''}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  {t.description && <p className="text-[11px] text-[#a1a1aa] mt-1">{t.description}</p>}
                </div>
              );
            })
          )}
        </div>
        <div className="px-5 py-3 border-t border-[#27272a] text-[11px] text-[#71717a]">共 {tools.length} 个工具</div>
      </div>
    </div>
  );
}

const inputCls = 'w-full px-3 py-2 bg-[#121214] border border-[#27272a] rounded-lg text-white placeholder:text-[#52525b] focus:outline-none focus:border-indigo-500 transition text-xs font-sans';

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-slate-400 font-medium font-sans">{label}</label>
      {children}
      {hint && <p className="text-[10px] text-[#71717a] leading-relaxed">{hint}</p>}
    </div>
  );
}
