const DEFAULT_API = {
  id: 'meituan',
  label: '美团外卖客服后端',
  baseUrl: import.meta.env.VITE_MEITUAN_API_URL || '/api/meituan',
  localUrl: 'http://127.0.0.1:8011'
}

const STORAGE_KEY = 'echomind.meituan.frontend.settings'

export function createInitialSettings() {
  // 启动时先恢复用户上次调试时的配置。
  const saved = readSettings()
  return {
    apiUrl: saved.apiUrl || DEFAULT_API.baseUrl,
    userId: saved.userId || 'u1001',
    conversationId: saved.conversationId || '',
    orderId: saved.orderId || '',
    phoneLast4: saved.phoneLast4 || ''
  }
}

export function saveSettings(settings) {
  // 只保存调试态需要的少量字段，避免把整个页面状态都塞进本地存储。
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    apiUrl: settings.apiUrl,
    userId: settings.userId,
    conversationId: settings.conversationId,
    orderId: settings.orderId,
    phoneLast4: settings.phoneLast4
  }))
}

export function backendMeta(settings) {
  // 统一把用户输入的地址标准化，避免多余的尾部斜杠影响请求拼接。
  return {
    ...DEFAULT_API,
    baseUrl: normalizeBaseUrl(settings.apiUrl || DEFAULT_API.baseUrl)
  }
}

export async function requestRoot(settings) {
  return requestJson(settings, '/')
}

export async function requestHealth(settings) {
  return requestJson(settings, '/health')
}

export async function requestMonitor(settings) {
  return requestJson(settings, '/monitor')
}

export async function requestKnowledgeStats(settings) {
  return requestJson(settings, '/knowledge/stats')
}

export async function requestSkills(settings) {
  return requestJson(settings, '/skills')
}

export async function reloadSkills(settings) {
  return requestJson(settings, '/skills/reload', { method: 'POST' })
}

export async function requestSearch(settings, query, topK = 5) {
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestJson(settings, `/search?${params}`, { method: 'POST' })
}

export async function requestChat(settings, message) {
  // 聊天接口返回的是后端原始结构，这里再做一层前端友好的归一化。
  const raw = await requestJson(settings, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(buildChatPayload(settings, message))
  })
  return normalizeChatResponse(raw)
}

export async function addKnowledge(settings, documents) {
  return requestJson(settings, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(settings, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

export async function runEvaluation(settings) {
  return requestJson(settings, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}'
  })
}

export function samplePrompts() {
  return [
    '我的外卖还没到，订单号是 MT20260829001',
    '订单号是 MT20260829004，为什么还没骑手接单',
    '订单 MT20260829005 显示送达了，但我没收到',
    '商家出餐太慢了，我要不要退款？',
    '优惠券为什么不能用',
    '验证码一直收不到，怎么办？'
  ]
}

function buildChatPayload(settings, message) {
  // 把界面上的输入收敛成后端需要的最小字段集。
  return compactObject({
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined,
    order_id: settings.orderId || undefined,
    phone_last4: settings.phoneLast4 || undefined
  })
}

function normalizeChatResponse(raw) {
  // 兼容后端 snake_case 字段，前端统一读 camelCase。
  return {
    conversationId: raw.conv_id || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || 'other',
    agentType: raw.agent_type || '',
    agentTypes: raw.agent_types || [],
    primaryAgent: raw.primary_agent || raw.agent_type || '',
    supportingAgents: raw.supporting_agents || [],
    routingReason: raw.routing_reason || '',
    routingConfidence: Number(raw.routing_confidence ?? 0),
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? 0),
    intentSourceScores: raw.intent_source_scores || {},
    trackingInfo: raw.tracking_info || {},
    raw
  }
}

async function requestJson(settings, path, options = {}) {
  // 统一封装 fetch，所有接口都走同一套错误处理。
  const baseUrl = backendMeta(settings).baseUrl
  const url = `${baseUrl}${path}`
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function compactObject(value) {
  // 去掉空值字段，避免给后端传一堆无意义参数。
  return Object.fromEntries(
    Object.entries(value).filter(([, item]) => item !== undefined && item !== '')
  )
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
  } catch {
    return {}
  }
}
