/**
 * Route definitions — AppLayout wraps all pages with sidebar + header.
 *
 * ProtectedRoute guards authenticated routes; login & design-system are public.
 */
import AppLayout from '../components/AppLayout'
import { ProtectedRoute } from './protected-routes'
import DashboardPage from '../pages/dashboard-page'
import AgentsPage from '../pages/agents-page'
import AgentDetailPage from '../pages/agent-detail-page'
import ModelsPage from '../pages/models-page'
import SkillsPage from '../pages/skills-page'
import McpPage from '../pages/mcp-page'
import TasksPage from '../pages/tasks-page'
import WorkflowsPage from '../pages/workflows-page'
import WorkflowDetailPage from '../pages/workflow-detail-page'
import ToolsPage from '../pages/tools-page'
import SkillDetailPage from '../pages/skill-detail-page'
import ExecutionStatsPage from '../pages/execution-stats-page'
import ApiKeysPage from '../pages/api-keys-page'
import CredentialsPage from '../pages/credentials-page'
import ChannelsPage from '../pages/channels-page'
import UsersPage from '../pages/users-page'
import RolesPage from '../pages/roles-page'
import SettingsPage from '../pages/settings-page'
import { DesignSystemPage } from '../pages/design-system-page'
import DesignReferencePage from '../pages/design-reference-page'
import { LoginPage } from '../pages/login-page'

export const routes = [
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { path: '/', element: <DashboardPage /> },
          { path: '/dashboard', element: <DashboardPage /> },
          { path: '/agents', element: <AgentsPage /> },
          { path: '/agents/:id', element: <AgentDetailPage /> },
          { path: '/models', element: <ModelsPage /> },
          { path: '/skills', element: <SkillsPage /> },
          { path: '/mcp', element: <McpPage /> },
          { path: '/tasks', element: <TasksPage /> },
          { path: '/workflows', element: <WorkflowsPage /> },
          { path: '/workflows/:id', element: <WorkflowDetailPage /> },
          { path: '/tools', element: <ToolsPage /> },
          { path: '/skills/:id', element: <SkillDetailPage /> },
          { path: '/execution-stats', element: <ExecutionStatsPage /> },
          { path: '/api-keys', element: <ApiKeysPage /> },
          { path: '/credentials', element: <CredentialsPage /> },
          { path: '/channels', element: <ChannelsPage /> },
          { path: '/users', element: <UsersPage /> },
          { path: '/roles', element: <RolesPage /> },
          { path: '/settings', element: <SettingsPage /> },
        ],
      },
    ],
  },
  { path: '/design-system', element: <DesignSystemPage /> },
  { path: '/design-reference', element: <DesignReferencePage /> },
  { path: '/login', element: <LoginPage /> },
  { path: '*', element: <DashboardPage /> },
]
