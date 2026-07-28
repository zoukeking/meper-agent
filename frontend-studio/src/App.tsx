import { useState, useEffect, useMemo, useRef } from 'react';
import {
  Bot, BookOpen, LayoutDashboard, Layers, Key, Server,
  Sun, Moon, MessageSquare, ListTodo, Sparkles, Shield,
  Wrench, Plug, UserCog, LogOut, ChevronDown,
  PanelLeftClose, PanelLeftOpen, Clock,
} from 'lucide-react';
import { useAuthStore, REFRESH_TOKEN_KEY } from './stores/auth-store';
import { useQuery, useQueries } from '@tanstack/react-query';
import { agentApi, agentKeys, type Agent as BackendAgent } from './services/agent-api';
import { toStudioAgent } from './services/adapters';
import { authApi } from './services/auth-api';
import { tasksApi, taskKeys, type WorkflowRegistryEntry, type TaskStatusValue } from './services/tasks-api';
import { workflowsApi, workflowKeys } from './services/workflows-api';
import Login from './components/Login';
import { AuthInitializer } from './components/AuthInitializer';
import { Toaster } from './components/ui/toast';
import { Tooltip } from './components/ui';
import { ConfirmHost } from './components/ui/confirm';
import { ChatHomepage } from './components/ChatHomepage';
import { TaskBoard } from './components/TaskBoard';
import { TaskDetailDrawer } from './components/task/TaskDetailDrawer';
import { Dashboard } from './components/Dashboard';
import { AgentSpace } from './components/AgentSpace';
import { AgentDetailPage } from './components/AgentDetailPage';
import { AgentEditorPage } from './components/AgentEditorPage';
import { WorkflowDesigner } from './components/WorkflowDesigner';
import { WorkflowSpace } from './components/WorkflowSpace';
import { SkillsStore } from './components/SkillsStore';
import { BuiltinToolsPage } from './components/BuiltinToolsPage';
import { McpManagePage } from './components/McpManagePage';
import { SkillDetailPage } from './components/SkillDetailPage';
import { KnowledgeBasePage } from './components/KnowledgeBasePage';
import { KbDetailPage } from './components/KbDetailPage';
import { UserManagement } from './components/UserManagement';
import { SystemSettings } from './components/SystemSettings';
import { ModelsPage } from './components/ModelsPage';
import { TriggersPage } from './components/TriggersPage';
import { ProfilePage } from './components/ProfilePage';
import { NotificationCenter } from './components/notification-center';
import { useTaskRealtime } from './hooks/use-task-realtime';
import { wsClient } from './lib/ws-client';
import { useNotificationStore } from './stores/notification-store';
import type { Agent } from './types';

/**
 * Navigation items, each mapping to a backend permission key. Items whose
 * permission the current user lacks are filtered out (mirrors the umi/Max
 * AppLayout NAV_GROUPS permission gating in frontend/src/layouts/AppLayout.tsx).
 *
 * `permission: undefined` means "always visible" (e.g. the home board).
 */
interface NavItem {
  id: string;
  label: string;
  icon: typeof Bot;
  badge?: string;
  permission?: string;
}

const NAV_ITEMS: NavItem[] = [
  { id: 'chat', label: '会话记录', icon: MessageSquare, badge: 'HP' },
  // 任务协作看板 badge 由下方 activeTaskCount 实时注入（执行中 + 等待人工）。
  { id: 'board', label: '任务协作看板', icon: ListTodo },
  { id: 'dashboard', label: '仪表盘', icon: LayoutDashboard, permission: 'execution:read:own' },
  { id: 'agents', label: '智能体', icon: Bot, permission: 'agent:read' },
  { id: 'models', label: '模型配置', icon: Server, permission: 'model:read' },
  { id: 'workflows', label: 'AI 工作路线', icon: Layers, permission: 'workflow:read' },
  { id: 'triggers', label: '定时任务', icon: Clock, permission: 'workflow:read' },
  { id: 'tools', label: '内置工具', icon: Wrench, permission: 'tool:read' },
  { id: 'mcp', label: '外部工具接入', icon: Plug, permission: 'tool:read' },
  { id: 'skills', label: '技能商店', icon: Sparkles, permission: 'tool:read' },
  { id: 'knowledge', label: '知识库', icon: BookOpen, permission: 'knowledge:read' },
  { id: 'users', label: '用户权限', icon: Shield, permission: 'user:read' },
  { id: 'settings', label: '系统设置', icon: Key, permission: 'settings:manage' },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<string>('chat');
  // Theme persists across reloads via localStorage (key mirrors the existing
  // `agentflow_` prefix used by auth-store.ts). Falls back to 'dark'.
  const [theme, setTheme] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('agentflow_theme') as 'dark' | 'light') || 'dark',
  );
  // 侧边栏折叠：默认展开，收起状态持久化到 localStorage（与 theme 同前缀）。
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem('agentflow_sidebar_collapsed') === 'true',
  );
  const toggleSidebar = () =>
    setCollapsed((v) => {
      const next = !v;
      localStorage.setItem('agentflow_sidebar_collapsed', String(next));
      return next;
    });
  // Sub-state for the Skill detail view (tools tab → open a Skill's files).
  const [openSkill, setOpenSkill] = useState<{ id: string; name: string } | null>(null);
  // Sub-state for the Knowledge Base detail view (list → click card → files).
  const [openKb, setOpenKb] = useState<{ id: string; name: string } | null>(null);
  // Sub-state for the workflow editor (list → click card → editor).
  const [openWorkflow, setOpenWorkflow] = useState<string | null>(null);
  // Sub-state for the Agent detail / live-test split view + editor.
  const [openAgent, setOpenAgent] = useState<{ id: string; mode: 'edit' | 'view' } | null>(null);
  // 底部用户头像下拉菜单（个人设置 / 登出）
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // 点击下拉菜单外部时关闭
  useEffect(() => {
    if (!userMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [userMenuOpen]);

  // ── Auth gate ──────────────────────────────────────────────
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const authUser = useAuthStore((s) => s.user);

  // 任务追踪抽屉 view state（通知中心 / Dashboard 触发）：存 task id，
  // 由 TaskDetailDrawer 内部 useQuery 拉取 GET /tasks/{id} + 5s 轮询。
  const [activeTraceTaskId, setActiveTraceTaskId] = useState<string | null>(null);

  // Live agent list for the ChatHomepage agent picker + count badge.
  const { data: agentsData } = useQuery({
    queryKey: agentKeys.list({ page: 1, page_size: 50, status: 'all' }),
    queryFn: () => agentApi.list({ page: 1, page_size: 50, status: 'all' }),
    enabled: isAuthenticated,
    staleTime: 60_000,
  });
  const agentCount = agentsData?.total ?? 0;
  const studioAgents: Agent[] = useMemo(
    () => (agentsData?.items ?? []).map(toStudioAgent),
    [agentsData],
  );

  // 图算工作流 badge：动态获取工作流总数（与 WorkflowSpace 列表共用缓存键，
  // 新建/删除后数字自动同步）。
  const { data: workflowsData } = useQuery({
    queryKey: workflowKeys.list({ page: 1, page_size: 100 }),
    queryFn: () => workflowsApi.list({ page: 1, page_size: 100 }),
    enabled: isAuthenticated,
    staleTime: 60_000,
  });
  const workflowCount = workflowsData?.total ?? 0;

  // 任务追踪抽屉的工作流名映射：与 TaskBoard 共用 taskKeys.workflows() 缓存，
  // 把 task.workflow_id（wf_ 模板 / wfr_ 历史 registry）解析成可读工作流名。
  const { data: wfRegistryData } = useQuery({
    queryKey: taskKeys.workflows(),
    queryFn: () => tasksApi.listWorkflows(),
    enabled: isAuthenticated,
    staleTime: 60_000,
  });
  const workflowNameMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const wf of (wfRegistryData?.items ?? []) as WorkflowRegistryEntry[]) {
      if (wf.name) {
        map[wf._id] = wf.name;
        map[wf.workflow_id] = wf.name;
      }
    }
    return map;
  }, [wfRegistryData]);

  // 任务协作看板 badge：聚合“活跃”状态（待执行 + 执行中 + 等待人工）的任务数。
  // 复用与 TaskBoard 相同的 taskKeys.list({status}) 缓存键，列表打开后两侧共享缓存、
  // 数字始终一致；刷新由 WebSocket 的 task_status 事件 invalidate 驱动（见
  // use-task-realtime），不做定时轮询。
  const BOARD_BADGE_STATUSES: TaskStatusValue[] = ['running', 'waiting_human'];
  const boardBadgeQueries = useQueries({
    queries: BOARD_BADGE_STATUSES.map((status) => ({
      queryKey: taskKeys.list({ status, page: 1, page_size: 50 }),
      queryFn: () => tasksApi.list({ status, page: 1, page_size: 50 }),
      enabled: isAuthenticated,
      // 用列表 total 作为该状态计数（任务量不大时 page_size=50 足够准确）。
      select: (res: { total: number }) => res.total,
      staleTime: 30_000,
    })),
  });
  const activeTaskCount = useMemo(
    () => boardBadgeQueries.reduce((sum, q) => sum + (q.data ?? 0), 0),
    [boardBadgeQueries],
  );

  // ── Realtime: WS task_status → react-query invalidate; WS notification → store ──
  // Mounted once at the App root; safe to call every render (it no-ops internally
  // until wsClient.connect() is driven by the isAuthenticated effect below).
  useTaskRealtime();

  // WS lifecycle follows auth state — connect on login, disconnect on logout.
  // Initial unread count is loaded once the user enters the main UI.
  const loadUnreadCount = useNotificationStore((s) => s.loadUnreadCount);
  useEffect(() => {
    if (!isAuthenticated) {
      wsClient.disconnect();
      return;
    }
    wsClient.resume();
    wsClient.connect();
    loadUnreadCount();
  }, [isAuthenticated, loadUnreadCount]);

  // Keep the WS connection in sync with access-token refreshes.
  //
  // Background: axios refreshes the access token lazily (only when an HTTP
  // request hits 401). The WS client reconnecting blindly after a 4401 close
  // would re-use the still-stale token in the store and loop connect→reject
  // until some unrelated HTTP request happened to refresh it. By subscribing
  // to the store's accessToken here, the moment a refresh lands we hand the
  // fresh token straight to the WS client and it reconnects immediately.
  useEffect(() => {
    let lastToken = useAuthStore.getState().accessToken;
    return useAuthStore.subscribe((state) => {
      const nextToken = state.accessToken;
      if (nextToken && nextToken !== lastToken) {
        lastToken = nextToken;
        // Only act when authenticated — disconnect/logout clears the token and
        // the isAuthenticated effect above handles teardown.
        wsClient.reconnectWithFreshToken(nextToken);
      }
    });
  }, []);

  const permissions = authUser?.permissions ?? [];
  const has = (perm?: string) => !perm || permissions.includes(perm);

  const visibleNav = useMemo(() => {
    const items = NAV_ITEMS.filter((n) => has(n.permission));
    // Inject live counts into the badges (agents total / board active tasks / workflows total).
    return items.map((n) => {
      if (n.id === 'agents') return { ...n, badge: String(agentCount) };
      if (n.id === 'board') return { ...n, badge: String(activeTaskCount) };
      if (n.id === 'workflows') return { ...n, badge: String(workflowCount) };
      return n;
    });
  }, [permissions, agentCount, activeTaskCount, workflowCount]);

  // If the active tab got filtered out, fall back to the first visible tab.
  // profile 是非导航项的合法页面（从用户下拉菜单进入），不参与回退判断。
  useEffect(() => {
    if (activeTab === 'profile') return;
    if (!visibleNav.some((n) => n.id === activeTab)) {
      setActiveTab(visibleNav[0]?.id ?? 'chat');
    }
  }, [visibleNav, activeTab]);

  // 进入工作流编辑器时自动收缩主菜单，给三栏编辑区让出空间（离开不自动恢复）
  useEffect(() => {
    if (openWorkflow) setCollapsed(true);
  }, [openWorkflow]);

  // Auth gate: AuthInitializer (mounted in main.tsx) drives the refresh-check
  // window and the initializing spinner; here we only branch on the resolved
  // auth state. All hooks above must run unconditionally on every render
  // (Rules of Hooks). Return only after the last hook has been called.
  if (!isAuthenticated) return <Login />;
  // ───────────────────────────────────────────────────────────

  const displayName = authUser?.username ?? '用户';
  const displayEmail = authUser?.username ? `${authUser.username}@agentflow` : '';

  // 登出：调 /auth/logout 吊销 refresh token，清本地登录态，跳登录页
  const handleLogout = async () => {
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
    try {
      if (refreshToken) await authApi.logout(refreshToken);
    } catch {
      // 登出接口幂等，失败也继续清除本地态
    } finally {
      clearAuth();
      window.location.href = '/login';
    }
  };

  return (
    <div className={`h-screen flex overflow-hidden theme-${theme} ${theme === 'dark' ? 'bg-[#09090b] text-[#fafafa]' : 'bg-slate-50 text-slate-800'} transition-colors duration-200`}>

      {/* 1. LEFT NAVIGATION RAIL BAR */}
      <aside className={`${collapsed ? 'w-16' : 'w-64'} border-r border-solid flex flex-col justify-between shrink-0 transition-[width] duration-200 ${
        theme === 'dark' ? 'bg-[#121214] border-[#27272a] text-[#a1a1aa]' : 'bg-white border-slate-200 text-slate-600'
      }`}>
        <div className="flex flex-col">
          <div className={`h-16 ${collapsed ? 'flex flex-col items-center justify-center gap-1' : 'px-4 flex items-center justify-between'} border-b shrink-0 ${theme === 'dark' ? 'border-[#27272a]' : 'border-slate-100'}`}>
            {/* Logo：展开态 FullLogo（含 AgentForge 字样）；折叠态 AFLogo（纯图标）。 */}
            <img
              src={collapsed ? '/AFLogo.png' : '/FullLogo.png'}
              alt="AgentForge"
              className={collapsed ? 'h-6 w-auto object-contain select-none' : 'h-full max-h-full w-auto py-2 object-contain select-none'}
              draggable={false}
            />
            {/* 折叠 / 展开切换：始终在 logo 区可见 */}
            <button
              onClick={toggleSidebar}
              aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
              className={`shrink-0 rounded-md p-1.5 transition-colors cursor-pointer ${
                theme === 'dark'
                  ? 'text-[#a1a1aa] hover:bg-[#1c1c1f] hover:text-white'
                  : 'text-slate-500 hover:bg-slate-100 hover:text-slate-800'
              }`}
            >
              {collapsed ? <PanelLeftOpen className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
            </button>
          </div>

          <nav className={`${collapsed ? 'flex flex-col items-center p-2' : 'p-4'} space-y-1`}>
            {!collapsed && (
              <p className={`px-2 text-[10px] font-bold uppercase tracking-widest mb-2 ${theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'}`}>Main Navigator</p>
            )}
            {visibleNav.map((item) => {
              const isActive = activeTab === item.id;
              const btn = (
                <button
                  key={item.id}
                  onClick={() => {
                    setActiveTab(item.id);
                    setActiveTraceTaskId(null);
                  }}
                  className={`${collapsed ? 'w-10 px-0 justify-center' : 'w-full px-3 justify-between'} py-2 rounded-md flex items-center font-medium transition-all text-sm cursor-pointer select-none ${
                    isActive
                      ? theme === 'dark'
                        ? 'text-indigo-400 border-l-2 border-indigo-500 bg-indigo-500/5'
                        : 'text-indigo-600 border-l-2 border-indigo-500 bg-indigo-500/10 font-semibold'
                      : theme === 'dark'
                        ? 'text-[#a1a1aa] hover:bg-[#1c1c1f] hover:text-[#fafafa]'
                        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                  }`}
                >
                  <div className={`flex items-center ${collapsed ? 'relative' : 'gap-3'}`}>
                    <item.icon className="w-4 h-4 shrink-0 transition-colors" />
                    {!collapsed && <span>{item.label}</span>}
                    {/* 折叠态：有计数的项图标右上角小圆点（具体数字走 tooltip） */}
                    {collapsed && item.badge && (
                      <span className="absolute -top-1 -right-1 w-1.5 h-1.5 rounded-full bg-indigo-500" />
                    )}
                  </div>
                  {!collapsed && item.badge && (
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono leading-none ${
                      isActive ? 'bg-indigo-500/20 text-indigo-300' : theme === 'dark' ? 'bg-[#18181b] text-[#71717a]' : 'bg-slate-100 text-slate-500'
                    }`}>
                      {item.badge}
                    </span>
                  )}
                </button>
              );
              // 折叠态用 Tooltip 包裹显示「名称（计数）」，hover 可见
              return collapsed ? (
                <Tooltip key={item.id} title={`${item.label}${item.badge ? `（${item.badge}）` : ''}`}>
                  {btn}
                </Tooltip>
              ) : (
                btn
              );
            })}
          </nav>
        </div>

        {/* Footer: 主题切换 + 用户头像下拉菜单 */}
        <div className={`p-3 border-t ${theme === 'dark' ? 'bg-[#121214] border-[#27272a]' : 'bg-slate-50 border-slate-200'}`}>
          <div className={`${collapsed ? 'flex flex-col items-stretch gap-2' : 'flex items-center justify-between gap-2'}`}>
            {/* 用户头像区：点击弹出下拉菜单（个人设置 / 登出） */}
            <div ref={userMenuRef} className={`relative ${collapsed ? '' : 'flex-1 min-w-0'}`}>
              <button
                onClick={() => setUserMenuOpen((v) => !v)}
                className={`w-full ${collapsed ? 'flex justify-center' : 'flex items-center gap-2'} p-1.5 rounded-lg transition-colors cursor-pointer ${
                  userMenuOpen
                    ? (theme === 'dark' ? 'bg-[#1c1c1f]' : 'bg-slate-200')
                    : (theme === 'dark' ? 'hover:bg-[#1c1c1f]' : 'hover:bg-slate-200')
                }`}
              >
                <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-orange-400 to-rose-400 text-sm flex items-center justify-center text-white font-bold shrink-0">
                  {displayName.slice(0, 1).toUpperCase()}
                </div>
                {!collapsed && (
                  <div className="min-w-0 flex-1 text-left">
                    <span className={`text-xs font-semibold block truncate ${theme === 'dark' ? 'text-white' : 'text-slate-800'}`}>{displayName}</span>
                    <span className={`text-[10px] truncate block ${theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'}`}>{authUser?.role ?? '—'}</span>
                  </div>
                )}
                {!collapsed && (
                  <ChevronDown className={`w-3.5 h-3.5 shrink-0 transition-transform ${userMenuOpen ? 'rotate-180' : ''} ${theme === 'dark' ? 'text-[#71717a]' : 'text-slate-400'}`} />
                )}
              </button>

              {/* 下拉菜单：展开态向上占满宽度；折叠态从头像右侧弹出（固定宽，避免被 64px 压窄） */}
              {userMenuOpen && (
                <div className={`absolute rounded-lg border shadow-2xl overflow-hidden z-50 animate-fade-in ${
                  collapsed
                    ? `bottom-0 left-full ml-2 w-48 ${theme === 'dark' ? 'bg-[#18181b] border-[#27272a]' : 'bg-white border-slate-200'}`
                    : `bottom-full left-0 right-0 mb-2 ${theme === 'dark' ? 'bg-[#18181b] border-[#27272a]' : 'bg-white border-slate-200'}`
                }`}>
                  <button
                    onClick={() => { setActiveTab('profile'); setUserMenuOpen(false); }}
                    className={`w-full flex items-center gap-2 px-3 py-2.5 text-xs transition-colors cursor-pointer ${
                      theme === 'dark' ? 'text-[#d4d4d8] hover:bg-[#1c1c1f]' : 'text-slate-700 hover:bg-slate-100'
                    }`}
                  >
                    <UserCog className="w-3.5 h-3.5 text-indigo-400" />
                    个人设置
                  </button>
                  <div className={theme === 'dark' ? 'border-t border-[#27272a]' : 'border-t border-slate-200'} />
                  <button
                    onClick={handleLogout}
                    className={`w-full flex items-center gap-2 px-3 py-2.5 text-xs transition-colors cursor-pointer ${
                      theme === 'dark' ? 'text-rose-400 hover:bg-[#1c1c1f]' : 'text-rose-600 hover:bg-rose-50'
                    }`}
                  >
                    <LogOut className="w-3.5 h-3.5" />
                    登出
                  </button>
                </div>
              )}
            </div>

            {/* 主题切换按钮 */}
            <button
              onClick={() => {
                const next = theme === 'dark' ? 'light' : 'dark';
                localStorage.setItem('agentflow_theme', next);
                setTheme(next);
                // 通知 Toaster（内联渲染，靠 localStorage 读主题）同步主题色
                window.dispatchEvent(new Event('agentflow-theme-change'));
              }}
              className={`${collapsed ? 'w-full flex justify-center' : ''} p-1.5 border rounded-lg transition-colors cursor-pointer shrink-0 ${
                theme === 'dark' ? 'bg-[#1c1c1f] border-[#27272a] text-[#a1a1aa] hover:text-white' : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50'
              }`}
            >
              {theme === 'dark' ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>
      </aside>

      {/* 2. MAIN WORKSPACE CONTAINER */}
      <main className="flex-1 flex flex-col min-w-0 overflow-hidden">

        <header className={`h-16 border-b flex items-center justify-between px-8 shrink-0 ${
          theme === 'dark' ? 'bg-[#121214] border-[#27272a]' : 'bg-white border-slate-200'
        }`}>
          <div className="flex items-center gap-3">
            <h1 className={`text-sm font-semibold ${theme === 'dark' ? 'text-[#fafafa]' : 'text-slate-800'}`}>
              {activeTab === 'profile' ? '个人设置' : visibleNav.find((n) => n.id === activeTab)?.label}
            </h1>
            <span className="bg-green-500/10 text-green-500 text-[10px] px-2 py-0.5 rounded border border-green-500/20 font-bold uppercase tracking-wide">
              VM Live
            </span>
          </div>

          {/* 通知中心：铃铛 + 未读角标 + 下拉面板（点击通知跳转任务协作看板并 highlight） */}
          <NotificationCenter
            onNavigateTask={(taskId) => {
              setActiveTab('board');
              setActiveTraceTaskId(taskId);
            }}
          />
        </header>

        <div id="content_stage" className={`flex-1 ${
          activeTab === 'chat' ? 'h-full flex flex-col p-0 overflow-hidden' :
          activeTab === 'board' ? 'h-full flex flex-col p-6 overflow-hidden' :
          'overflow-y-auto p-6'
        }`}>
          {activeTab === 'chat' && (
            <ChatHomepage agents={studioAgents} theme={theme} />
          )}

          {activeTab === 'board' && (
            <TaskBoard theme={theme} />
          )}

          {activeTab === 'dashboard' && (
            <Dashboard
              onSelectTab={setActiveTab}
              onViewTask={(task) => setActiveTraceTaskId(task.id)}
            />
          )}

          {activeTab === 'agents' && (
            openAgent ? (
              openAgent.mode === 'edit' ? (
                <AgentEditorPage
                  agentId={openAgent.id}
                  onBack={() => setOpenAgent(null)}
                  onSaved={(id) => setOpenAgent({ id, mode: 'view' })}
                />
              ) : (
                <AgentDetailPage
                  agentId={openAgent.id}
                  theme={theme}
                  onBack={() => setOpenAgent(null)}
                  onOpenEdit={(id) => setOpenAgent({ id, mode: 'edit' })}
                />
              )
            ) : (
              <AgentSpace
                onOpenDetail={(id) => setOpenAgent({ id, mode: 'view' })}
                onOpenEdit={(id) => setOpenAgent({ id, mode: 'edit' })}
              />
            )
          )}

          {activeTab === 'models' && <ModelsPage />}

          {activeTab === 'workflows' && (
            openWorkflow ? (
              <WorkflowDesigner
                theme={theme}
                workflowId={openWorkflow}
                onBack={() => setOpenWorkflow(null)}
                onCreated={(id) => setOpenWorkflow(id)}
              />
            ) : (
              <WorkflowSpace theme={theme} onOpen={(id) => setOpenWorkflow(id)} />
            )
          )}

          {activeTab === 'triggers' && (
            <TriggersPage
              onViewTask={(taskId) => {
                setActiveTab('board');
                setActiveTraceTaskId(taskId);
              }}
            />
          )}

          {activeTab === 'tools' && <BuiltinToolsPage />}

          {activeTab === 'mcp' && <McpManagePage />}

          {activeTab === 'skills' && (
            openSkill ? (
              <SkillDetailPage
                toolId={openSkill.id}
                toolName={openSkill.name}
                onBack={() => setOpenSkill(null)}
              />
            ) : (
              <SkillsStore onOpenSkill={(s) => setOpenSkill({ id: s.id, name: s.name })} />
            )
          )}

          {activeTab === 'knowledge' && (
            openKb ? (
              <KbDetailPage
                kbId={openKb.id}
                kbName={openKb.name}
                onBack={() => setOpenKb(null)}
              />
            ) : (
              <KnowledgeBasePage onOpenKb={(kb) => setOpenKb({ id: kb.id, name: kb.name })} />
            )
          )}

          {activeTab === 'users' && <UserManagement />}

          {activeTab === 'settings' && <SystemSettings />}

          {activeTab === 'profile' && (
            <ProfilePage theme={theme} />
          )}
        </div>
      </main>

      {/* 任务追踪抽屉：复用 TaskBoard 的 TaskDetailDrawer（无遮罩 + 富渲染），
          由通知中心 / Dashboard 触发。仅查看模式：不传干预回调，操作栏自适应隐藏。 */}
      <TaskDetailDrawer
        taskId={activeTraceTaskId}
        open={!!activeTraceTaskId}
        onClose={() => setActiveTraceTaskId(null)}
        workflowNameMap={workflowNameMap}
        theme={theme}
      />

      {/* 全局消息提醒：Toast + 统一确认弹窗（内联渲染，继承 theme-${theme} 主题） */}
      <Toaster />
      <ConfirmHost />

    </div>
  );
}