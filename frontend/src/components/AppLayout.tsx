/**
 * AppLayout — Dify-inspired two-tier navigation layout.
 *
 * Top bar: group-level tabs (仪表盘 / Agent / 工作流 / 工具 / 用户管理 / 系统信息).
 * Secondary bar: sub-page tabs (left-aligned, small font), only visible for
 * groups with multiple children. Single-page groups navigate directly.
 */
import { useLocation, useNavigate, Outlet } from 'react-router-dom'
import { REFRESH_TOKEN_KEY } from '../stores/auth-store'
import {
  NodeIndexOutlined,
  DashboardOutlined,
  RobotOutlined,
  BranchesOutlined,
  ToolOutlined,
  TeamOutlined,
  SettingOutlined,
  SearchOutlined,
  QuestionCircleOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Avatar, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import { useTheme } from '../contexts/ThemeContext'
import { useAuthStore } from '../stores/auth-store'
import { authApi } from '../services/auth-api'
import { LogoutOutlined } from '@ant-design/icons'
import NotificationCenter from './notification-center'
import type { ReactNode } from 'react'

/* ─── System role display name mapping ─── */
const SYSTEM_ROLE_DISPLAY: Record<string, string> = {
  admin: '管理员',
  developer: '开发者',
  operator: '运营者',
  viewer: '查看者',
}

/* ─── Group nav items ─── */
interface SubPage {
  label: string
  path: string
  key: string
  permission?: string // Required permission to show this sub-page
}

interface NavGroup {
  key: string
  label: string
  icon: ReactNode
  single?: boolean       // single-page group, no sub-tabs
  path?: string           // path for single groups
  children?: SubPage[]    // sub-pages for grouped groups
  permission?: string     // Required permission to show this group
}

const GROUPS: NavGroup[] = [
  { key: 'dashboard', label: '仪表盘', icon: <DashboardOutlined />, single: true, path: '/dashboard' },
  {
    key: 'agent', label: 'Agent', icon: <RobotOutlined />,
    children: [
      { label: 'Agent 管理', path: '/agents', key: 'agents', permission: 'agent:read' },
      { label: '模型', path: '/models', key: 'models', permission: 'model:read' },
    ],
  },
  {
    key: 'workflow', label: '工作流', icon: <BranchesOutlined />,
    children: [
      { label: '工作流', path: '/workflows', key: 'workflows', permission: 'workflow:read' },
      { label: '任务管理', path: '/tasks', key: 'tasks', permission: 'task:read' },
    ],
  },
  {
    key: 'tools', label: '工具', icon: <ToolOutlined />,
    children: [
      { label: '工具', path: '/tools', key: 'tools', permission: 'tool:read' },
      { label: 'MCP', path: '/mcp', key: 'mcp', permission: 'mcp:read' },
      { label: 'Skill', path: '/skills', key: 'skills', permission: 'skill:read' },
      { label: '凭据', path: '/credentials', key: 'credentials', permission: 'tool:read' },
      { label: '渠道', path: '/channels', key: 'channels', permission: 'tool:read' },
    ],
  },
  {
    key: 'users',
    label: '用户管理',
    icon: <TeamOutlined />,
    children: [
      { label: '用户', path: '/users', key: 'users', permission: 'user:read' },
      { label: '角色管理', path: '/roles', key: 'roles', permission: 'user:read' },
    ],
  },
  {
    key: 'system', label: '系统信息', icon: <SettingOutlined />,
    children: [
      { label: 'API 密钥', path: '/api-keys', key: 'api-keys', permission: 'apikey:manage' },
      { label: '执行统计', path: '/execution-stats', key: 'execution-stats', permission: 'apikey:manage' },
      { label: '设置', path: '/settings', key: 'settings', permission: 'settings:manage' },
    ],
  },
]

/* ─── Path → group lookup ─── */
const PATH_TO_GROUP: Record<string, string> = {
  '/dashboard': 'dashboard',
  '/agents': 'agent',
  '/models': 'agent',
  '/workflows': 'workflow',
  '/tasks': 'workflow',
  '/tools': 'tools',
  '/mcp': 'tools',
  '/skills': 'tools',
  '/channels': 'tools',
  '/users': 'users',
  '/roles': 'users',
  '/api-keys': 'system',
  '/execution-stats': 'system',
  '/settings': 'system',
}

export default function AppLayout() {
  const { t } = useTheme()
  const location = useLocation()
  const navigate = useNavigate()
  const currentPath = location.pathname
  const { clearAuth, user } = useAuthStore()

  // Resolve user display info
  const username = user?.username ?? 'User'
  const roleDisplayName = user?.role
    ? (SYSTEM_ROLE_DISPLAY[user.role] ?? user.role)
    : '用户'

  /* ─── Filter groups by permissions ─── */
  const userPermissions = user?.permissions ?? []
  const hasPermission = (perm: string) => userPermissions.includes(perm)

  const visibleGroups = GROUPS.filter((group) => {
    if (group.permission && !hasPermission(group.permission)) return false
    return true
  }).map((group) => {
    if (!group.children) return group
    const visibleChildren = group.children.filter(
      (child) => !child.permission || hasPermission(child.permission),
    )
    if (visibleChildren.length === 0) return null
    if (visibleChildren.length === 1) {
      // Collapse to single-page group
      return { ...group, single: true, path: visibleChildren[0].path, children: undefined }
    }
    return { ...group, children: visibleChildren }
  }).filter(Boolean) as NavGroup[]

  /* ─── Resolve active group & child ─── */
  // Support dynamic routes like /agents/:id → group "agent", /workflows/:id → group "workflow"
  const basePath = currentPath.startsWith('/agents/')
    ? '/agents'
    : currentPath.startsWith('/workflows/')
      ? '/workflows'
      : currentPath
  const activeGroupKey = PATH_TO_GROUP[basePath] || 'dashboard'
  const activeGroup = visibleGroups.find((g) => g.key === activeGroupKey)

  const handleGroupClick = (group: NavGroup) => {
    if (group.single && group.path) {
      navigate(group.path)
    } else if (group.children && group.children.length > 0) {
      // If already in this group, stay; otherwise navigate to first child
      if (activeGroupKey !== group.key) {
        navigate(group.children[0].path)
      }
    }
  }

  const handleLogout = async () => {
    try {
      await authApi.logout(localStorage.getItem(REFRESH_TOKEN_KEY) ?? '')
    } catch {
      // 即使 API 失败也要本地清除登录态
    }
    clearAuth()
    navigate('/login', { replace: true })
  }

  const userMenuItems: MenuProps['items'] = [
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      danger: true,
      onClick: handleLogout,
    },
  ]

  return (
    <div className="flex flex-col h-screen overflow-hidden bg-canvas text-txt">
      {/* ════════════ Top navigation bar ════════════ */}
      <header className="h-14 shrink-0 flex items-center px-6 border-b border-line bg-canvas">
        {/* Logo */}
        <div className="flex items-center gap-2 mr-8 shrink-0">
          <div className="w-7 h-7 rounded-lg flex items-center justify-center text-white shrink-0" style={{ background: t.primary }}>
            <NodeIndexOutlined className="text-[11px]" />
          </div>
          <span className="font-semibold text-sm text-txt tracking-tight">Agent Flow</span>
        </div>

        {/* Group tabs */}
        <nav className="flex items-center gap-0.5">
          {visibleGroups.map((group) => {
            const isActive = activeGroupKey === group.key
            return (
              <button
                key={group.key}
                onClick={() => handleGroupClick(group)}
                className={`flex items-center gap-1.5 px-3.5 py-[6px] text-sm rounded-lg transition-all duration-150 cursor-pointer border-0 bg-transparent ${
                  isActive
                    ? 'font-medium shadow-sm'
                    : 'text-txt-3 hover:text-txt-hover hover:bg-surface-muted'
                }`}
                style={isActive ? { color: t.primary, background: t.bg } : undefined}
              >
                <span className="text-sm leading-none">{group.icon}</span>
                <span>{group.label}</span>
              </button>
            )
          })}
        </nav>

        {/* Spacer */}
        <div className="flex-1" />

        {/* User controls */}
        <div className="flex items-center gap-1">
          <button className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-txt-3 hover:text-txt hover:bg-surface-muted transition-colors duration-150">
            <SearchOutlined />
          </button>
          <button className="border-0 bg-transparent w-8 h-8 flex items-center justify-center rounded-md text-txt-3 hover:text-txt hover:bg-surface-muted transition-colors duration-150">
            <QuestionCircleOutlined />
          </button>
          <NotificationCenter />
          <div className="w-px h-6 mx-2 bg-line" />
          <Dropdown menu={{ items: userMenuItems }} placement="bottomRight" trigger={['click']}>
            <div className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-surface-muted transition-colors duration-150 cursor-pointer">
              <Avatar size={28} icon={<UserOutlined />} style={{ background: t.primary }} />
              <div className="hidden sm:block text-left leading-tight">
                <div className="text-sm font-medium text-txt">{username}</div>
                <div className="text-[11px] text-txt-muted">{roleDisplayName}</div>
              </div>
            </div>
          </Dropdown>
        </div>
      </header>

      {/* ════════ Sub-page tabs (grouped groups only) ════════ */}
      {activeGroup?.children && activeGroup.children.length > 0 && (
        <div className="flex items-center h-10 bg-surface border-b border-line-2 px-6 gap-0.5">
          {activeGroup.children.map((child) => {
            const isChildActive = currentPath === child.path
              || (child.path === '/agents' && currentPath.startsWith('/agents/'))
              || (child.path === '/workflows' && currentPath.startsWith('/workflows/'))
            return (
              <button
                key={child.key}
                onClick={() => navigate(child.path)}
                className={`px-3 py-1 text-xs rounded-md transition-all duration-150 cursor-pointer border-0 ${
                  isChildActive
                    ? 'text-white font-medium shadow-sm'
                    : 'text-txt-3 hover:text-txt-hover hover:bg-surface-muted'
                }`}
                style={isChildActive ? { background: t.primary } : undefined}
              >
                {child.label}
              </button>
            )
          })}
        </div>
      )}

      {/* ════════ Content area ════════ */}
      <main className="flex-1 p-6 bg-surface overflow-auto flex flex-col min-h-0">
        <Outlet />
      </main>
    </div>
  )
}
