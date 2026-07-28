import {
  DeleteOutlined,
  LogoutOutlined,
  MessageOutlined,
  MoonOutlined,
  PlusOutlined,
  SunOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Conversations } from '@ant-design/x'
import { Avatar, Button, Dropdown, Empty, Skeleton, Typography } from 'antd'

import { useAuthStore } from '../store/auth'
import type { AgentSummary, ChatSession } from '../types'

interface ConversationSidebarProps {
  agents: AgentSummary[]
  selectedAgentId: string | null
  onSelectAgent: (agentId: string) => void
  sessions: ChatSession[]
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onCreateSession: () => void
  onDeleteSession: (session: ChatSession) => void
  creating: boolean
  loading: boolean
  onLogout: () => void
}

function sessionLabel(session: ChatSession): string {
  if (session.title?.trim()) return session.title
  if (session.created_at) {
    return new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(session.created_at))
  }
  return `会话 ${session.id.slice(0, 6)}`
}

export function ConversationSidebar(props: ConversationSidebarProps) {
  const user = useAuthStore((state) => state.user)
  const theme = useAuthStore((state) => state.theme)
  const toggleTheme = useAuthStore((state) => state.toggleTheme)
  const selected = props.agents.find((agent) => agent.id === props.selectedAgentId)

  return (
    <aside className="conversation-sidebar">
      <div className="sidebar-brand">
        <img src="/FullLogo.png" alt="Agent Flow" />
      </div>

      <div className="agent-switcher">
        <Typography.Text className="sidebar-label" type="secondary">
          选择 Agent
        </Typography.Text>
        <Dropdown
          trigger={['click']}
          menu={{
            selectedKeys: props.selectedAgentId ? [props.selectedAgentId] : [],
            items: props.agents.map((agent) => ({
              key: agent.id,
              label: (
                <div className="agent-menu-item">
                  <Avatar size={30}>{agent.name.slice(0, 1)}</Avatar>
                  <div>
                    <span>{agent.name}</span>
                    <small>
                      {agent.status === 'published' ? '已发布' : agent.status}
                    </small>
                  </div>
                </div>
              ),
            })),
            onClick: ({ key }) => props.onSelectAgent(key),
          }}
        >
          <Button className="agent-trigger" block>
            <Avatar size={28}>{selected?.name.slice(0, 1) || 'A'}</Avatar>
            <span>{selected?.name || '暂无可用 Agent'}</span>
          </Button>
        </Dropdown>
      </div>

      <Button
        className="new-session-button"
        type="primary"
        icon={<PlusOutlined />}
        loading={props.creating}
        disabled={!props.selectedAgentId}
        onClick={props.onCreateSession}
      >
        新建对话
      </Button>

      <div className="conversation-list">
        <Typography.Text className="sidebar-label" type="secondary">
          对话列表
        </Typography.Text>
        {props.loading ? (
          <Skeleton active paragraph={{ rows: 5 }} title={false} />
        ) : props.sessions.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有对话" />
        ) : (
          <Conversations
            activeKey={props.activeSessionId ?? undefined}
            items={props.sessions.map((session) => ({
              key: session.id,
              label: sessionLabel(session),
              icon: <MessageOutlined />,
            }))}
            onActiveChange={(key) => props.onSelectSession(key)}
            menu={(item) => ({
              items: [
                {
                  key: 'delete',
                  danger: true,
                  icon: <DeleteOutlined />,
                  label: '删除对话',
                },
              ],
              onClick: ({ key, domEvent }) => {
                domEvent.stopPropagation()
                if (key !== 'delete') return
                const session = props.sessions.find((value) => value.id === item.key)
                if (session) props.onDeleteSession(session)
              },
            })}
          />
        )}
      </div>

      <div className="sidebar-account">
        <Avatar icon={<UserOutlined />} />
        <div className="account-copy">
          <strong>{user?.username || '用户'}</strong>
          <small>{user?.role || ''}</small>
        </div>
        <Button
          type="text"
          icon={theme === 'dark' ? <SunOutlined /> : <MoonOutlined />}
          onClick={toggleTheme}
          aria-label="切换主题"
        />
        <Button
          type="text"
          icon={<LogoutOutlined />}
          onClick={props.onLogout}
          aria-label="退出登录"
        />
      </div>
    </aside>
  )
}
