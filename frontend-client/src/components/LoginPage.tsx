import {
  EyeInvisibleOutlined,
  EyeTwoTone,
  LockOutlined,
  MoonOutlined,
  SunOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Alert, Button, Form, Input, Typography } from 'antd'
import { useState } from 'react'

import { login } from '../api/auth'
import { ApiError } from '../api/client'
import { useAuthStore } from '../store/auth'

interface LoginValues {
  identifier: string
  password: string
}

export function LoginPage() {
  const theme = useAuthStore((state) => state.theme)
  const toggleTheme = useAuthStore((state) => state.toggleTheme)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (values: LoginValues) => {
    setSubmitting(true)
    setError(null)
    try {
      await login(
        values.identifier.trim(),
        values.password,
      )
    } catch (reason: unknown) {
      if (
        reason instanceof ApiError &&
        (reason.code === 'account_locked' || reason.message.includes('账号已锁定'))
      ) {
        setError('账号已锁定，请在 15 分钟后重试')
      } else {
        setError(reason instanceof Error ? reason.message : '登录失败，请稍后重试')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page">
      <Button
        className="theme-button"
        type="text"
        icon={theme === 'dark' ? <SunOutlined /> : <MoonOutlined />}
        onClick={toggleTheme}
        aria-label="切换主题"
      />
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-brand">
          <img src="/AFLogo.png" alt="Agent Flow" />
          <div>
            <Typography.Title id="login-title" level={2}>
              Agent Flow
            </Typography.Title>
            <Typography.Text type="secondary">
              登录后选择 Agent 开始对话
            </Typography.Text>
          </div>
        </div>

        <Form<LoginValues>
          layout="vertical"
          requiredMark={false}
          onFinish={submit}
          size="large"
        >
          <Form.Item
            label="用户名"
            name="identifier"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="请输入用户名"
              autoComplete="username"
              autoFocus
            />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="请输入密码"
              autoComplete="current-password"
              iconRender={(visible) =>
                visible ? <EyeTwoTone /> : <EyeInvisibleOutlined />
              }
            />
          </Form.Item>
          {error ? (
            <Alert className="login-error" type="error" showIcon message={error} />
          ) : null}
          <Button
            type="primary"
            htmlType="submit"
            block
            loading={submitting}
          >
            登录
          </Button>
        </Form>
        <Typography.Text className="login-footnote" type="secondary">
          连续 5 次失败将锁定账号 15 分钟
        </Typography.Text>
      </section>
    </main>
  )
}
