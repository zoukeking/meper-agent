/** Credentials management page — create / list / delete encrypted secrets. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Form, Input, Modal, Select, Space, message, Tag } from 'antd'
import { DeleteOutlined, PlusOutlined, KeyOutlined } from '@ant-design/icons'
import type { CredentialType, Credential } from '../services/credentials-api'
import { credentialsApi, credentialKeys } from '../services/credentials-api'

const TYPE_COLORS: Record<CredentialType, string> = {
  api_key: 'blue',
  bearer: 'green',
  basic: 'orange',
  oauth2: 'purple',
}

const TYPE_LABELS: Record<CredentialType, string> = {
  api_key: 'API Key',
  bearer: 'Bearer Token',
  basic: 'Basic Auth',
  oauth2: 'OAuth 2.0',
}

export default function CredentialsPage() {
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [form] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: credentialKeys.list(),
    queryFn: () => credentialsApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: credentialsApi.create,
    onSuccess: () => {
      message.success('凭据创建成功')
      queryClient.invalidateQueries({ queryKey: credentialKeys.all })
      setCreateOpen(false)
      form.resetFields()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: credentialsApi.remove,
    onSuccess: () => {
      message.success('凭据已删除')
      queryClient.invalidateQueries({ queryKey: credentialKeys.all })
    },
  })

  const handleDelete = (cred: Credential) => {
    Modal.confirm({
      title: '删除凭据',
      content: `确定删除「${cred.name}」吗？引用此凭据的工具将失去认证。`,
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: () => deleteMutation.mutate(cred._id),
    })
  }

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields()
      // Parse data JSON
      let data: Record<string, string> = {}
      if (values.dataJson) {
        try {
          data = JSON.parse(values.dataJson)
        } catch {
          message.error('凭据数据 JSON 格式错误')
          return
        }
      }
      createMutation.mutate({
        name: values.name,
        type: values.type || 'api_key',
        data,
      })
    } catch {
      // validation error
    }
  }

  const credentials = data?.items || []

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <KeyOutlined /> 凭据管理
          </h1>
          <p className="text-sm text-gray-500 mt-1">
            管理工具认证凭据（API Key、Token 等），加密存储，可被多个工具共享引用。
          </p>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          创建凭据
        </Button>
      </div>

      {isLoading ? (
        <div className="text-center py-12 text-gray-400">加载中...</div>
      ) : credentials.length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <KeyOutlined style={{ fontSize: 48 }} />
          <p className="mt-4">暂无凭据，点击「创建凭据」添加</p>
        </div>
      ) : (
        <div className="grid gap-3">
          {credentials.map((cred) => (
            <div
              key={cred._id}
              className="flex items-center justify-between rounded-xl border border-gray-200 px-5 py-4 hover:shadow-sm transition-shadow"
            >
              <div className="flex items-center gap-4">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{cred.name}</span>
                    <Tag color={TYPE_COLORS[cred.type]}>
                      {TYPE_LABELS[cred.type] || cred.type}
                    </Tag>
                  </div>
                  <div className="flex gap-3 mt-1 text-xs text-gray-400">
                    {Object.entries(cred.masked_data || {}).map(([k, v]) => (
                      <span key={k}>
                        {k}: <code className="text-gray-500">{v}</code>
                      </span>
                    ))}
                  </div>
                </div>
              </div>
              <Space>
                <Button
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => handleDelete(cred)}
                />
              </Space>
            </div>
          ))}
        </div>
      )}

      <Modal
        title="创建凭据"
        open={createOpen}
        onCancel={() => { setCreateOpen(false); form.resetFields() }}
        onOk={handleSubmit}
        confirmLoading={createMutation.isPending}
        okText="创建"
        cancelText="取消"
        width={520}
      >
        <Form form={form} layout="vertical" initialValues={{ type: 'api_key' }}>
          <Form.Item label="名称" name="name" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：GitHub Token" />
          </Form.Item>
          <Form.Item label="类型" name="type">
            <Select
              options={[
                { value: 'api_key', label: 'API Key' },
                { value: 'bearer', label: 'Bearer Token' },
                { value: 'basic', label: 'Basic Auth' },
                { value: 'oauth2', label: 'OAuth 2.0' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="凭据数据（JSON）"
            name="dataJson"
            extra='如 {"token": "ghp_xxxxxxxx"}，加密存储，创建后不可查看明文'
          >
            <Input.TextArea
              rows={4}
              placeholder='{"token": "ghp_xxxxxxxx"}'
              className="font-mono text-sm"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
