/**
 * ClarificationFormCard — ask_clarification 向导模式渲染卡片。
 *
 * 当 ask_clarification 提供 fields 时，渲染为向导：一次展示一个问题，
 * 答完跳下一个，支持返回上一题修改，全部答完才提交。
 *
 * 每个问题（字段）的展示规则：
 * - 提供 options（3-5 个推荐）→ 显示推荐选项按钮，用户可点选；
 *   同时底部始终保留一个自由输入框，输入会覆盖选项选择。
 * - 未提供 options（如密码、纯自由输入）→ 只显示输入框。
 *
 * 提交时把所有答案序列化为 JSON 字符串（如 {"audience":"管理层","format":"PDF"}）
 * 通过 onSubmit 回调传出，复用现有 resume 传输通道（answer: string）。
 * 已答态：把 result（JSON 串）解析后渲染为键值对摘要。
 */
import { useMemo, useState } from 'react'
import { Input, InputNumber, Button } from 'antd'
import { LeftOutlined, RightOutlined, SendOutlined } from '@ant-design/icons'

/** 后端 ClarificationField 的前端镜像。 */
export interface ClarificationField {
  name: string
  label: string
  field_type: 'text' | 'number' | 'boolean' | 'select'
  required: boolean
  options?: string[] | null
  default?: string | number | boolean | null
  description?: string | null
}

interface Props {
  /** 表单标题（取自 ask_clarification 的 question） */
  question: string
  /** 背景说明（取自 context） */
  context?: string | null
  fields: ClarificationField[]
  /** 是否已回答（result 存在时为 true，渲染只读摘要态） */
  answered: boolean
  /** 用户的答案（JSON 字符串） */
  result?: string
  /** 提交回调，入参为答案的 JSON 字符串 */
  onSubmit: (jsonStr: string) => void
}

export function ClarificationFormCard({
  question,
  context,
  fields,
  answered,
  result,
  onSubmit,
}: Props) {
  // 当前问题下标（向导步进）
  const [step, setStep] = useState(0)
  // 各字段的最终答案：键为字段 name。值含义：
  //   select/text/number → 字符串/数字；boolean → true/false。
  //   undefined 表示未作答。
  const [answers, setAnswers] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {}
    for (const f of fields) {
      if (f.default !== null && f.default !== undefined) init[f.name] = f.default
    }
    return init
  })

  const total = fields.length
  const current = fields[step] ?? fields[0]
  const isLast = step >= total - 1

  const options = (current?.options ?? []) as string[]

  /** 把当前题的答案写入 answers（值可能为 undefined 表示清空）。 */
  const commitAnswer = (val: unknown) => {
    setAnswers((prev) => ({ ...prev, [current.name]: val }))
  }

  /** 选中某个推荐选项：把选项写入答案（自由输入框值由 isFreeTextValue 推导，会自动清空）。 */
  const pickOption = (opt: string) => {
    commitAnswer(opt)
  }

  /** 自由输入框内容变化：有内容则覆盖选项选择，空则恢复为 undefined。 */
  const handleInputChange = (val: string | number | null) => {
    if (val === '' || val === null || val === undefined) {
      commitAnswer(undefined)
    } else {
      commitAnswer(val)
    }
  }

  /** 下一题：必填校验通过后步进。 */
  const handleNext = () => {
    const val = answers[current.name]
    const empty = val === undefined || val === '' || val === null
    if (current.required && current.field_type !== 'boolean' && empty) {
      return // 必填未答，不前进（按钮也处于 disabled）
    }
    if (isLast) {
      // 全部答完 → 提交。跳过空值。
      const out: Record<string, unknown> = {}
      for (const f of fields) {
        const v = answers[f.name]
        if (v === undefined || v === '' || v === null) continue
        out[f.name] = v
      }
      onSubmit(JSON.stringify(out))
    } else {
      setStep((s) => Math.min(s + 1, total - 1))
    }
  }

  /** 上一题。 */
  const handlePrev = () => {
    setStep((s) => Math.max(s - 1, 0))
  }

  // 当前问题的答案 + 是否已作答。
  const currentVal = answers[current.name]
  const isAnswered =
    current.field_type === 'boolean'
      ? currentVal !== undefined
      : currentVal !== undefined && currentVal !== '' && currentVal !== null

  // 自由输入框的值：直接由当前答案推导。
  // - 答案是「不在 options 中的字符串」→ 显示该字符串（自由输入态）
  // - 否则 → 空串（用户选了选项或未作答，输入框留空）
  const freeInputValue =
    typeof currentVal === 'string' && currentVal !== '' && !options.includes(currentVal)
      ? currentVal
      : ''

  // ── 已答态：解析 result 为键值对摘要 ──
  const answeredValues = useMemo<Record<string, unknown>>(() => {
    if (!result) return {}
    try {
      const parsed = JSON.parse(result)
      return typeof parsed === 'object' && parsed !== null ? parsed : {}
    } catch {
      return {}
    }
  }, [result])

  const renderValue = (f: ClarificationField, val: unknown): string => {
    if (val === undefined || val === null) return '—'
    if (f.field_type === 'text' && f.name.toLowerCase().includes('key')) {
      // 疑似密钥类字段做简单掩码（只露首尾）
      const s = String(val)
      if (s.length <= 8) return '••••'
      return `${s.slice(0, 3)}••••${s.slice(-3)}`
    }
    if (f.field_type === 'boolean') return val ? '是' : '否'
    return String(val)
  }

  if (answered) {
    return (
      <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3">
        <div className="flex items-start gap-2.5">
          <span className="text-blue-500 text-sm mt-0.5">📋</span>
          <div className="flex-1 min-w-0">
            {question && (
              <div className="text-sm text-[#1E40AF] whitespace-pre-wrap leading-relaxed mb-2">
                {question}
              </div>
            )}
            <div className="space-y-1">
              {fields.map((f) => (
                <div key={f.name} className="flex items-baseline gap-2 text-xs">
                  <span className="text-[#3B82F6] shrink-0">{f.label}:</span>
                  <span className="text-[#1E40AF] font-medium break-all">
                    {renderValue(f, answeredValues[f.name])}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ── 当前问题作答态 ──
  return (
    <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3">
      <div className="flex items-start gap-2.5">
        <span className="text-blue-500 text-sm mt-0.5">📋</span>
        <div className="flex-1 min-w-0">
          {question && (
            <div className="text-xs text-[#6366F1] mb-1.5">{question}</div>
          )}
          {context && (
            <div className="text-[11px] text-[#94A3B8] mb-2 whitespace-pre-wrap">
              {context}
            </div>
          )}

          {/* 进度 */}
          <div className="text-[11px] text-[#94A3B8] mb-2">
            第 {step + 1} / {total} 题
          </div>

          {/* 当前问题标题 */}
          <div className="text-sm text-[#1E40AF] font-medium mb-2">
            {current.label}
            {current.required && current.field_type !== 'boolean' && (
              <span className="text-red-400 ml-0.5">*</span>
            )}
          </div>
          {current.description && (
            <p className="text-[11px] text-[#64748B] mb-2">{current.description}</p>
          )}

          {/* boolean → 开关 */}
          {current.field_type === 'boolean' && (
            <div className="flex gap-2 mb-3">
              <Button
                size="small"
                type={currentVal === true ? 'primary' : 'default'}
                onClick={() => commitAnswer(true)}
              >
                是
              </Button>
              <Button
                size="small"
                type={currentVal === false ? 'primary' : 'default'}
                onClick={() => commitAnswer(false)}
              >
                否
              </Button>
            </div>
          )}

          {/* 推荐选项（number 不提供按钮；select/text 有 options 时提供） */}
          {current.field_type !== 'boolean' && options.length > 0 && (
            <div className="flex flex-col gap-1.5 mb-2">
              {options.map((opt) => {
                const selected = currentVal === opt
                return (
                  <button
                    key={opt}
                    type="button"
                    onClick={() => pickOption(opt)}
                    className={`text-left px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                      selected
                        ? 'bg-blue-100 border-blue-400 text-blue-800 font-medium'
                        : 'bg-white border-blue-300 text-blue-700 hover:bg-blue-100 hover:border-blue-400 cursor-pointer'
                    }`}
                  >
                    {opt}
                    {selected && <span className="ml-1.5 text-blue-500">✓</span>}
                  </button>
                )
              })}
            </div>
          )}

          {/* 底部自由输入框：始终展示（除 boolean 外） */}
          {current.field_type === 'number' ? (
            <InputNumber
              size="small"
              className="!w-full"
              value={freeInputValue === '' ? undefined : Number(freeInputValue)}
              placeholder="或在此输入自定义内容…"
              onChange={(v) => handleInputChange(v)}
            />
          ) : current.field_type !== 'boolean' ? (
            <Input
              size="small"
              value={freeInputValue}
              placeholder="或在此输入自定义内容…"
              onChange={(e) => handleInputChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  if (current.required && !isAnswered) return
                  handleNext()
                }
              }}
            />
          ) : null}

          {/* 导航按钮 */}
          <div className="flex justify-between items-center mt-3">
            <Button
              size="small"
              type="text"
              icon={<LeftOutlined />}
              disabled={step === 0}
              onClick={handlePrev}
            >
              上一题
            </Button>
            <Button
              size="small"
              type="primary"
              disabled={current.required && current.field_type !== 'boolean' && !isAnswered}
              onClick={handleNext}
            >
              {isLast ? (
                <>
                  <SendOutlined /> 提交
                </>
              ) : (
                <>
                  下一题 <RightOutlined />
                </>
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
