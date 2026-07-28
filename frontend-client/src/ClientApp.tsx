import { App as AntApp, Drawer } from 'antd'
import { useEffect, useMemo, useState } from 'react'

import {
  createSession,
  deleteSession,
  listAvailableAgents,
  listSessions,
} from './api/chat'
import { logout } from './api/auth'
import { ChatView } from './components/ChatView'
import { ConversationSidebar } from './components/ConversationSidebar'
import type { AgentSummary, ChatSession } from './types'

const AGENT_KEY = 'meper_client_agent_id'

export function ClientApp() {
  const { message, modal } = AntApp.useApp()
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [agentsLoading, setAgentsLoading] = useState(true)
  const [sessionsLoading, setSessionsLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [navigationOpen, setNavigationOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    listAvailableAgents()
      .then((items) => {
        if (cancelled) return
        setAgents(items)
        const stored = localStorage.getItem(AGENT_KEY)
        const selected = items.some((item) => item.id === stored)
          ? stored
          : items[0]?.id ?? null
        setSelectedAgentId(selected)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          void message.error(error instanceof Error ? error.message : 'Agent 加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setAgentsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [message])

  useEffect(() => {
    let cancelled = false
    setActiveSessionId(null)
    setSessions([])
    if (!selectedAgentId) return
    localStorage.setItem(AGENT_KEY, selectedAgentId)
    setSessionsLoading(true)
    listSessions(selectedAgentId)
      .then((items) => {
        if (cancelled) return
        setSessions(items)
        setActiveSessionId(items[0]?.id ?? null)
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          void message.error(error instanceof Error ? error.message : '会话加载失败')
        }
      })
      .finally(() => {
        if (!cancelled) setSessionsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [message, selectedAgentId])

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.id === selectedAgentId) ?? null,
    [agents, selectedAgentId],
  )

  const createNewSession = async () => {
    if (!selectedAgentId || creating) return
    setCreating(true)
    try {
      const created = await createSession(selectedAgentId)
      setSessions((current) => [created, ...current])
      setActiveSessionId(created.id)
      setNavigationOpen(false)
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : '新建对话失败')
    } finally {
      setCreating(false)
    }
  }

  const confirmDeleteSession = (session: ChatSession) => {
    modal.confirm({
      title: '删除这个对话？',
      content: '对话记录和会话记忆将被永久移除，此操作不可撤销。',
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      async onOk() {
        await deleteSession(session.id)
        const next = sessions.filter((item) => item.id !== session.id)
        setSessions(next)
        setActiveSessionId((active) =>
          active === session.id ? next[0]?.id ?? null : active,
        )
      },
    })
  }

  const sidebarProps = {
    agents,
    selectedAgentId,
    onSelectAgent: (id: string) => {
      setSelectedAgentId(id)
      setNavigationOpen(false)
    },
    sessions,
    activeSessionId,
    onSelectSession: (id: string) => {
      setActiveSessionId(id)
      setNavigationOpen(false)
    },
    onCreateSession: () => void createNewSession(),
    onDeleteSession: confirmDeleteSession,
    creating,
    loading: agentsLoading || sessionsLoading,
    onLogout: () => void logout(),
  }

  return (
    <div className="client-shell">
      <div className="desktop-sidebar-shell">
        <ConversationSidebar {...sidebarProps} />
      </div>
      <Drawer
        className="mobile-navigation-drawer"
        width="min(320px, 88vw)"
        placement="left"
        title={null}
        open={navigationOpen}
        onClose={() => setNavigationOpen(false)}
        closable={false}
        styles={{ body: { padding: 0 } }}
      >
        <ConversationSidebar {...sidebarProps} />
      </Drawer>
      <ChatView
        agent={selectedAgent}
        sessionId={activeSessionId}
        onOpenNavigation={() => setNavigationOpen(true)}
        onCreateSession={() => void createNewSession()}
      />
    </div>
  )
}
