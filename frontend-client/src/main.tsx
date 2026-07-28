import { App as AntApp, ConfigProvider, Spin, theme as antTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { XProvider } from '@ant-design/x'
import { StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'

import { AUTH_MODE, bootstrapAuth } from './api/client'
import { ClientApp } from './ClientApp'
import { LoginPage } from './components/LoginPage'
import { useParentToken } from './hooks/use-parent-token'
import { useAuthStore } from './store/auth'
import './styles.css'

function Root() {
  const initialized = useAuthStore((state) => state.initialized)
  const accessToken = useAuthStore((state) => state.accessToken)
  const theme = useAuthStore((state) => state.theme)
  // iframe 嵌入时向宿主页请求终端用户 token（callback 模式）；apikey 模式才生效。
  useParentToken()

  useEffect(() => {
    void bootstrapAuth()
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    document.documentElement.style.colorScheme = theme
  }, [theme])

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm:
          theme === 'dark' ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
        cssVar: { prefix: 'meper' },
        token: {
          colorPrimary: '#315f92',
          colorInfo: '#315f92',
          borderRadius: 12,
          fontFamily:
            '"SF Pro Text", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
        },
      }}
    >
      <XProvider>
        <AntApp>
          {!initialized ? (
            <div className="boot-screen">
              <Spin size="large" />
            </div>
          ) : AUTH_MODE === 'apikey' || accessToken ? (
            <ClientApp />
          ) : (
            <LoginPage />
          )}
        </AntApp>
      </XProvider>
    </ConfigProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
