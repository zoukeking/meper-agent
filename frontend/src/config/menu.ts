/**
 * Navigation configuration — Dify-inspired grouped layout.
 *
 * `icon` is a string key resolved to an @ant-design/icons component
 * by the sidebar renderer. Groups define the top-level navigation;
 * children appear as sub-page tabs in the secondary nav bar.
 */
export interface MenuItem {
  key: string
  label: string
  path: string
  icon: string
  group?: string
}

export const MENU_ITEMS: MenuItem[] = [
  // Single-page groups
  { key: 'dashboard', label: '仪表盘', path: '/dashboard', icon: 'DashboardOutlined' },
  // Agent group
  { key: 'agents', label: 'Agent 管理', path: '/agents', icon: 'RobotOutlined', group: 'agent' },
  { key: 'models', label: '模型', path: '/models', icon: 'CloudOutlined', group: 'agent' },
  // Workflow group
  { key: 'workflows', label: '工作流', path: '/workflows', icon: 'BranchesOutlined', group: 'workflow' },
  { key: 'tasks', label: '任务管理', path: '/tasks', icon: 'FileTextOutlined', group: 'workflow' },
  // Tools group
  { key: 'tools', label: '工具', path: '/tools', icon: 'ToolOutlined', group: 'tools' },
  { key: 'mcp', label: 'MCP', path: '/mcp', icon: 'GatewayOutlined', group: 'tools' },
  { key: 'skills', label: 'Skill', path: '/skills', icon: 'HighlightOutlined', group: 'tools' },
  { key: 'credentials', label: '凭据', path: '/credentials', icon: 'SafetyOutlined', group: 'tools' },
  { key: 'channels', label: '渠道', path: '/channels', icon: 'ApiOutlined', group: 'tools' },
  // Single-page: users
  { key: 'users', label: '用户管理', path: '/users', icon: 'TeamOutlined' },
  // System group
  { key: 'api-keys', label: 'API 密钥', path: '/api-keys', icon: 'KeyOutlined', group: 'system' },
  { key: 'execution-stats', label: '执行统计', path: '/execution-stats', icon: 'BarChartOutlined', group: 'system' },
  { key: 'settings', label: '设置', path: '/settings', icon: 'SettingOutlined', group: 'system' },
]
