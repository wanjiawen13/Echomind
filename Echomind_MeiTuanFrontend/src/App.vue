<template>
  <main class="app-shell">
    <aside class="sidebar">
      <section class="brand">
        <div class="brand-mark">MT</div>
        <div>
          <h1>美团外卖客服台</h1>
          <p>配送、退款、平台问题联调</p>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>后端连接</h2>
          <span :class="['status-badge', healthOk ? 'online' : 'offline']">{{ healthLabel }}</span>
        </div>
        <label>
          <span>API 地址</span>
          <input v-model="settings.apiUrl" @change="persist" placeholder="/api/meituan" />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="settings.userId" @change="persist" placeholder="u1001" />
        </label>
        <label>
          <span>会话 ID</span>
          <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
        </label>
        <label>
          <span>订单号</span>
          <input v-model="settings.orderId" @change="persist" placeholder="可选，例如 MT20260829001" />
        </label>
        <label>
          <span>手机号后四位</span>
          <input v-model="settings.phoneLast4" @change="persist" maxlength="4" placeholder="可选，例如 1234" />
        </label>
        <div class="actions two">
          <button @click="checkHealth">健康检查</button>
          <button class="secondary" @click="refreshDashboard">刷新面板</button>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>示例问题</h2>
          <span class="pill">{{ promptList.length }}</span>
        </div>
        <div class="prompt-list">
          <button v-for="prompt in promptList" :key="prompt" class="prompt-button" @click="usePrompt(prompt)">
            {{ prompt }}
          </button>
        </div>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>服务概览</h2>
          <span class="pill soft">{{ backend.baseUrl }}</span>
        </div>
        <dl class="kv-list">
          <div>
            <dt>知识片段</dt>
            <dd>{{ knowledgeCount }}</dd>
          </div>
          <div>
            <dt>Skills</dt>
            <dd>{{ skills.length }}</dd>
          </div>
          <div>
            <dt>最近意图</dt>
            <dd>{{ lastResponse?.intent || '-' }}</dd>
          </div>
          <div>
            <dt>最近 Agent</dt>
            <dd>{{ lastResponse?.agentType || '-' }}</dd>
          </div>
        </dl>
      </section>
    </aside>

    <section class="workspace">
      <header class="workspace-header">
        <div>
          <span class="eyebrow">Echomind_MeiTuan</span>
          <h2>外卖客服闭环调试</h2>
          <p>{{ backend.label }} · {{ backend.baseUrl }}</p>
        </div>
        <div class="header-actions">
          <a :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
          <button class="secondary" @click="clearChat">清空对话</button>
        </div>
      </header>

      <section class="main-grid">
        <section class="chat-panel">
          <div class="messages" ref="messageList">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? '用户' : '客服助手' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <p>{{ item.content }}</p>
            </article>
            <div v-if="messages.length === 0" class="empty-state">
              <h3>开始一次外卖客服对话</h3>
              <p>输入订单、配送、退款、优惠券或验证码问题，界面会展示路由和查询结果。</p>
            </div>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <textarea v-model="draft" rows="3" placeholder="例如：我的外卖还没到，订单号是 MT20260829001"></textarea>
            <button :disabled="busy || !draft.trim()">{{ busy ? '发送中' : '发送' }}</button>
          </form>
        </section>

        <aside class="inspector">
          <section class="panel inspector-panel">
            <div class="panel-heading">
              <h2>意图与路由</h2>
              <span class="pill">{{ percent(lastResponse?.routingConfidence) }}</span>
            </div>
            <dl class="kv-list">
              <div>
                <dt>意图</dt>
                <dd>{{ lastResponse?.intent || '-' }}</dd>
              </div>
              <div>
                <dt>意图组</dt>
                <dd>{{ lastResponse?.intentGroup || '-' }}</dd>
              </div>
              <div>
                <dt>主 Agent</dt>
                <dd>{{ lastResponse?.primaryAgent || '-' }}</dd>
              </div>
              <div>
                <dt>辅助 Agent</dt>
                <dd>{{ formatList(lastResponse?.supportingAgents) }}</dd>
              </div>
              <div>
                <dt>知识库</dt>
                <dd>{{ lastResponse?.knowledgeUsed ? '已使用' : '未使用' }}</dd>
              </div>
              <div>
                <dt>转人工</dt>
                <dd>{{ lastResponse?.escalated ? '是' : '否' }}</dd>
              </div>
            </dl>
            <p v-if="lastResponse?.routingReason" class="note">{{ lastResponse.routingReason }}</p>
          </section>

          <section class="panel inspector-panel">
            <div class="panel-heading">
              <h2>订单查询</h2>
              <span :class="['pill', order ? '' : 'soft']">{{ order ? '已命中' : '暂无' }}</span>
            </div>
            <dl v-if="order" class="kv-list">
              <div>
                <dt>订单号</dt>
                <dd>{{ order.order_id }}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>{{ order.status_label || order.status }}</dd>
              </div>
              <div>
                <dt>预计时间</dt>
                <dd>{{ etaText(order.eta_minutes) }}</dd>
              </div>
              <div>
                <dt>商家状态</dt>
                <dd>{{ merchantLabel(order.merchant_status) }}</dd>
              </div>
              <div>
                <dt>骑手</dt>
                <dd>{{ order.rider_name || '-' }}</dd>
              </div>
              <div>
                <dt>可取消</dt>
                <dd>{{ order.can_cancel ? '是' : '否' }}</dd>
              </div>
              <div>
                <dt>可退款</dt>
                <dd>{{ order.can_refund ? '是' : '否' }}</dd>
              </div>
            </dl>
            <p v-if="order?.abnormal_reason" class="warning">{{ order.abnormal_reason }}</p>
            <p v-if="!order" class="note">{{ trackingMessage }}</p>
          </section>
        </aside>
      </section>

      <section class="tools-grid">
        <article class="tool-panel">
          <div class="panel-heading">
            <h2>知识库检索</h2>
            <span class="pill soft">RAG</span>
          </div>
          <div class="inline-form">
            <input v-model="searchQuery" placeholder="退款多久能到账" />
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">检索</button>
          </div>
          <div class="result-list">
            <article v-for="item in searchResults" :key="item.id || item.title" class="result-item">
              <strong>{{ item.title || '未命名结果' }}</strong>
              <span>score {{ item.score ?? '-' }}</span>
              <p>{{ item.content }}</p>
            </article>
            <p v-if="searchResults.length === 0" class="note">暂无检索结果。</p>
          </div>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>导入知识</h2>
            <span class="pill soft">Docs</span>
          </div>
          <label>
            <span>标题</span>
            <input v-model="docTitle" placeholder="配送补充规则" />
          </label>
          <label>
            <span>内容</span>
            <textarea v-model="docContent" rows="5" placeholder="输入知识库内容"></textarea>
          </label>
          <div class="actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="file-button">
              上传文件
              <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
            </label>
          </div>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>Skills</h2>
            <button class="secondary compact" @click="reloadSkillList">热加载</button>
          </div>
          <div class="skill-list">
            <article v-for="skill in skills" :key="skill.path" class="skill-item">
              <div>
                <strong>{{ skill.name }}</strong>
                <span>{{ formatList(skill.agents) }}</span>
              </div>
              <p>{{ skill.description }}</p>
            </article>
            <p v-if="skills.length === 0" class="note">暂无已加载 Skill。</p>
          </div>
          <p v-if="skillErrors.length" class="warning">{{ skillErrors.join('；') }}</p>
        </article>

        <article class="tool-panel">
          <div class="panel-heading">
            <h2>评测与监控</h2>
            <button class="secondary compact" @click="runEval" :disabled="busy">运行评测</button>
          </div>
          <dl class="kv-list">
            <div>
              <dt>通过率</dt>
              <dd>{{ evalReport ? percent(evalReport.pass_rate) : '-' }}</dd>
            </div>
            <div>
              <dt>用例</dt>
              <dd>{{ evalReport ? `${evalReport.passed}/${evalReport.total}` : '-' }}</dd>
            </div>
            <div>
              <dt>意图准确率</dt>
              <dd>{{ evalReport?.avg_scores?.intent_accuracy ? percent(evalReport.avg_scores.intent_accuracy) : '-' }}</dd>
            </div>
          </dl>
          <pre v-if="statusText">{{ statusText }}</pre>
        </article>
      </section>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  reloadSkills,
  requestChat,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestRoot,
  requestSearch,
  requestSkills,
  runEvaluation,
  samplePrompts,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

const settings = reactive(createInitialSettings())
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const skills = ref([])
const skillErrors = ref([])
const searchQuery = ref('配送超时怎么处理')
const searchResults = ref([])
const docTitle = ref('配送补充规则')
const docContent = ref('高峰期骑手紧张时，应先说明当前订单状态和预计送达时间，再根据异常原因判断是否转人工。')
const evalReport = ref(null)
const rootInfo = ref(null)
const lastResponse = ref(null)
const messageList = ref(null)
const promptList = samplePrompts()

const backend = computed(() => backendMeta(settings))
const docsUrl = computed(() => `${backend.value.baseUrl}/docs`)
const order = computed(() => lastResponse.value?.trackingInfo?.order || null)
const trackingMessage = computed(() => lastResponse.value?.trackingInfo?.message || '发送包含订单号的问题后，这里会展示配送查询结果。')

watch(
  () => [settings.apiUrl, settings.userId, settings.conversationId, settings.orderId, settings.phoneLast4],
  () => persist(),
  { deep: true }
)

onMounted(() => {
  refreshDashboard()
})

function persist() {
  saveSettings(settings)
}

function usePrompt(prompt) {
  draft.value = prompt
}

function clearChat() {
  messages.value = []
  lastResponse.value = null
  statusText.value = ''
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content) return
  messages.value.push({ id: makeId(), role: 'user', content })
  draft.value = ''
  busy.value = true
  try {
    const response = await requestChat(settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    lastResponse.value = response
    messages.value.push({
      id: makeId(),
      role: 'assistant',
      content: response.response,
      meta: responseMeta(response)
    })
  } catch (error) {
    messages.value.push({
      id: makeId(),
      role: 'assistant',
      content: error.message,
      meta: '请求失败'
    })
  } finally {
    busy.value = false
    await scrollToBottom()
  }
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message
  }
}

async function refreshDashboard() {
  const [root, health, stats, monitor, skillData] = await Promise.allSettled([
    requestRoot(settings),
    requestHealth(settings),
    requestKnowledgeStats(settings),
    requestMonitor(settings),
    requestSkills(settings)
  ])
  if (root.status === 'fulfilled') rootInfo.value = root.value
  if (health.status === 'fulfilled') {
    healthOk.value = health.value.status === 'ok'
    healthLabel.value = health.value.status || 'ok'
  } else {
    healthOk.value = false
    healthLabel.value = '不可用'
  }
  if (stats.status === 'fulfilled') {
    knowledgeCount.value = stats.value.total_chunks ?? '-'
  }
  if (monitor.status === 'fulfilled') {
    statusText.value = JSON.stringify(monitor.value, null, 2)
  }
  if (skillData.status === 'fulfilled') {
    applySkills(skillData.value)
  }
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings, searchQuery.value, 5)
    searchResults.value = data.results || []
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function submitKnowledge() {
  busy.value = true
  try {
    const data = await addKnowledge(settings, [
      { title: docTitle.value.trim(), content: docContent.value.trim() }
    ])
    statusText.value = JSON.stringify(data, null, 2)
    await refreshDashboard()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    const data = await uploadKnowledge(settings, file)
    statusText.value = JSON.stringify(data, null, 2)
    await refreshDashboard()
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function reloadSkillList() {
  busy.value = true
  try {
    const data = await reloadSkills(settings)
    applySkills(data)
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

async function runEval() {
  busy.value = true
  try {
    const data = await runEvaluation(settings)
    evalReport.value = data
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    statusText.value = error.message
  } finally {
    busy.value = false
  }
}

function applySkills(data) {
  skills.value = data.skills || []
  skillErrors.value = data.errors || []
}

function responseMeta(response) {
  return [
    response.intent,
    response.intentGroup,
    response.agentType,
    response.knowledgeUsed ? 'RAG' : '',
    response.escalated ? '转人工' : ''
  ].filter(Boolean).join(' · ')
}

function percent(value) {
  const num = Number(value || 0)
  return `${Math.round(num * 100)}%`
}

function etaText(value) {
  if (value === null || value === undefined || value === '') return '-'
  return `${value} 分钟`
}

function merchantLabel(value) {
  const labels = {
    preparing: '备餐中',
    ready: '已出餐',
    picked_up: '已取餐',
    completed: '已完成'
  }
  return labels[value] || value || '-'
}

function formatList(value) {
  if (!value || value.length === 0) return '-'
  return value.join(', ')
}

async function scrollToBottom() {
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
}

function makeId() {
  if (crypto?.randomUUID) return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}
</script>
