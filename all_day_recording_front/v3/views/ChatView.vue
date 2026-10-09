<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { chatApi, type ChatRecord, type ChatPage, type ChatContext, type ChatScope, type ChatStatus, type ChatJob } from '../core/chat'

const connectionUrl = ref(''), connectionToken = ref(''), savingConnection = ref(false)
const platform = ref(''), source = ref(''), conversation = ref(''), sender = ref(''), keyword = ref('')
const from = ref(new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10)), to = ref('')
const accounts = ref<Record<string, unknown>[]>([]), conversations = ref<Record<string, unknown>[]>([])
const accountCursor = ref<string | null>(null), conversationCursor = ref<string | null>(null)
const accountMore = ref(false), conversationMore = ref(false)
const status = ref<ChatStatus | null>(null), page = ref<ChatPage | null>(null), context = ref<ChatContext | null>(null)
const selected = ref<ChatRecord | null>(null), job = ref<ChatJob | null>(null)
const question = ref(''), error = ref(''), notice = ref(''), busy = ref(false), contextBusy = ref(false), directoryBusy = ref(false)
const expanded = ref(10), frozen = ref<ChatScope>({}), cacheOnly = ref(false)
let active = true, timer: ReturnType<typeof setInterval> | undefined
const scope = computed<ChatScope>(() => ({
  ...(platform.value ? { platform: platform.value } : {}), ...(source.value ? { source_account_id: source.value } : {}),
  ...(conversation.value ? { conversation_id: conversation.value } : {}), ...(sender.value ? { sender_account_id: sender.value } : {}),
  ...(keyword.value ? { keyword: keyword.value } : {}),
  ...(from.value ? { sent_from: Math.floor(new Date(from.value + 'T00:00:00').getTime() / 1000) } : {}),
  ...(to.value ? { sent_to: Math.floor(new Date(to.value + 'T23:59:59').getTime() / 1000) } : {}),
}))
const directoryScope = computed(() => ({ ...(platform.value ? { platform: platform.value } : {}), ...(source.value ? { source_account_id: source.value } : {}) }))
const sourceAccounts = computed(() => accounts.value.filter(a => a.role === 'source'))
const senderAccounts = computed(() => accounts.value.filter(a => a.role === 'sender'))
const date = (value: number | null | undefined) => value ? new Date(value * 1000).toLocaleString('zh-CN') : '时间未知'
const identifier = (item: Record<string, unknown>) => String(item.id ?? item.account_id ?? item.conversation_id ?? item.source_account_id ?? '')
const label = (item: Record<string, unknown>) => String(item.display_name ?? item.name ?? identifier(item))
const excerpt = (record: ChatRecord) => {
  if (record.text === null) return '正文未提取/解码失败，不能推断附件内容'
  const hit = frozen.value.keyword ? record.text.indexOf(frozen.value.keyword) : 0
  const start = Math.max(0, hit - 80), end = start + 500
  return (start ? '…' : '') + record.text.slice(start, end) + (end < record.text.length ? '…' : '')
}
const failure = (e: unknown) => { error.value = e instanceof Error ? e.message : String(e) }
async function refreshStatus() { try { status.value = await chatApi.status(); connectionUrl.value = status.value.connection.base_url } catch (e) { failure(e) } }
async function saveConnection() {
  savingConnection.value = true; error.value = ''; notice.value = ''
  try {
    await chatApi.configure(connectionUrl.value, connectionToken.value)
    connectionToken.value = ''
    notice.value = '本地连接配置已保存，凭据不会返回到界面。'
    await refreshStatus()
    if (!status.value?.error) await directories()
  } catch (e) { failure(e) } finally { savingConnection.value = false }
}
async function directories(reset = true, kind = '') {
  if (directoryBusy.value) return
  directoryBusy.value = true; error.value = ''
  if (reset) { accounts.value = []; conversations.value = []; accountCursor.value = null; conversationCursor.value = null }
  try {
    if (!kind || kind === 'accounts') {
      const result = await chatApi.directory('accounts', directoryScope.value, accountCursor.value)
      accounts.value.push(...result.items); accountCursor.value = result.next_cursor; accountMore.value = result.has_more
    }
    if (!kind || kind === 'conversations') {
      const result = await chatApi.directory('conversations', directoryScope.value, conversationCursor.value)
      conversations.value.push(...result.items); conversationCursor.value = result.next_cursor; conversationMore.value = result.has_more
    }
  } catch (e) { failure(e) } finally { directoryBusy.value = false }
}
async function filterChanged() { conversation.value = ''; sender.value = ''; if (!platform.value) source.value = ''; await directories() }
async function search(more = false) {
  if (busy.value) return
  busy.value = true; error.value = ''; job.value = null
  try {
    if (!more) frozen.value = { ...scope.value }
    const result = await chatApi.search(frozen.value, more ? page.value?.next_cursor : undefined, cacheOnly.value ? 'cache' : more ? page.value?.origin : 'mac')
    if (!more) frozen.value = result.scope
    page.value = more && page.value ? { ...result, items: [...page.value.items, ...result.items] } : result
    selected.value = null; context.value = null
  } catch (e) { failure(e) } finally { busy.value = false }
}
async function open(record: ChatRecord) {
  selected.value = record; contextBusy.value = true; error.value = ''; expanded.value = 10
  try { context.value = await chatApi.context(record.record_id, expanded.value) } catch (e) { failure(e) } finally { contextBusy.value = false }
}
async function expand() {
  if (!selected.value || contextBusy.value) return
  contextBusy.value = true; expanded.value = 25
  try { context.value = await chatApi.context(selected.value.record_id, 25) } catch (e) { failure(e) } finally { contextBusy.value = false }
}
async function sync(reinitialize = false) {
  error.value = ''; notice.value = ''
  try {
    const selectedScope = { ...scope.value }; delete selectedScope.keyword
    await chatApi.sync(selectedScope, reinitialize)
    notice.value = '已登记会话范围并开始有界同步；每批最多 100 条，首批工作集最多 5000 条。'
    await refreshStatus()
  } catch (e) { failure(e) }
}
async function control(action: string, scopeId?: string) { try { await chatApi.control(action, scopeId); await refreshStatus() } catch (e) { failure(e) } }
async function answer() {
  if (!page.value || !question.value.trim()) return
  error.value = ''; notice.value = ''; job.value = null
  try { job.value = await chatApi.answer(question.value, frozen.value, page.value.origin) } catch (e) { failure(e) }
}
async function poll() {
  if (!active) return
  if (job.value && ['pending', 'running'].includes(job.value.state)) {
    try { job.value = await chatApi.job(job.value.id) } catch (e) { failure(e) }
  } else if (job.value?.state === 'done') {
    try { job.value = await chatApi.job(job.value.id) } catch (e) { failure(e) }
  }
}
function citation(id: string) { return job.value?.value?.evidence.find(r => r.record_id === id) }
onMounted(async () => { await refreshStatus(); if (!active) return; if (!status.value?.error) await directories(); timer = setInterval(poll, 1500) })
onUnmounted(() => { active = false; clearInterval(timer); if (job.value && ['pending', 'running'].includes(job.value.state)) void chatApi.cancel(job.value.id).catch(() => {}) })
</script>

<template>
  <section class="page chat-page">
    <header class="page-header"><div><p class="eyebrow">CHAT / LOCAL EVIDENCE</p><h1>聊天查询</h1></div><button class="secondary-button" @click="refreshStatus">刷新状态</button></header>
    <section class="panel chat-status" v-if="status">
      <strong>{{ status.error ? `来源暂不可用 · ${status.error}` : 'Mac 数据服务已连接' }}</strong>
      <p>最近连接：{{ date(status.last_contact_at) }} · 已接收 {{ status.cache.received }} · 索引完成 {{ status.cache.indexed }} · 待索引 {{ status.cache.pending }} · 失败 {{ status.cache.failed }}</p>
      <p>{{ status.source_limitations }}</p>
      <div class="chat-actions"><button @click="control(status.paused ? 'resume' : 'pause')">{{ status.paused ? '恢复已登记范围' : '暂停同步' }}</button><button v-if="status.cache.failed" @click="control('retry-index')">重试失败索引</button></div>
      <details><summary>数据覆盖、采集时间与缺失来源</summary><pre>{{ JSON.stringify(status.coverage, null, 2) }}</pre><p v-if="!status.coverage">尚未取得 Mac 覆盖声明，不能判断可查询历史是否齐全。</p></details>
      <div v-for="range in status.scopes" :key="range.id" class="chat-range"><span>{{ range.scope.platform }} · {{ range.scope.conversation_id }} · 从 {{ date(range.scope.sent_from) }} · 接收 {{ range.received }} · {{ range.phase === 'snapshot' ? '快照未完成' : '快照完成，增量接续' }}</span><strong v-if="range.error">{{ range.error }}</strong><button v-if="range.enabled" @click="control('retire-scope', range.id)">停止此范围</button><span v-else>此范围已停用，缓存保留</span></div>
    </section>
    <details class="panel chat-status" :open="status?.error === 'CHAT_NOT_CONFIGURED'">
      <summary>Mac 连接配置</summary>
      <form class="chat-actions" @submit.prevent="saveConnection">
        <label>服务地址 <input v-model="connectionUrl" required placeholder="http://Mac局域网IP:8091" /></label>
        <label>访问凭据 <input v-model="connectionToken" required type="password" autocomplete="new-password" minlength="32" placeholder="与 Mac 相同的本地凭据" /></label>
        <button type="submit" :disabled="savingConnection">{{ savingConnection ? '保存中…' : '保存并检查连接' }}</button>
      </form><p>仅保存到本机私有状态目录；凭据不进入 Git 或日志。环境变量优先于文件配置，环境变量变更需重启工作台。</p>
    </details>
    <form class="panel chat-filters" @submit.prevent="search()">
      <label>关键词<input v-model="keyword" maxlength="200" placeholder="杭电、报名、PDF 或原话；按字面搜索" /></label>
      <label>平台<select v-model="platform" @change="source = ''; filterChanged()"><option value="">QQ + 微信</option><option value="qq">QQ</option><option value="wechat">微信</option></select></label>
      <label>采集账号<input v-model="source" list="chat-sources" placeholder="稳定账号 ID" @change="filterChanged" /><datalist id="chat-sources"><option v-for="a in sourceAccounts" :key="String(a.platform) + String(a.role) + identifier(a)" :value="identifier(a)">{{ a.platform }} · {{ label(a) }}</option></datalist></label>
      <label>会话<input v-model="conversation" list="chat-conversations" placeholder="稳定会话 ID" /><datalist id="chat-conversations"><option v-for="c in conversations" :key="identifier(c)" :value="identifier(c)">{{ label(c) }}</option></datalist></label>
      <label>发送者账号<input v-model="sender" list="chat-senders" placeholder="明确账号；同名不会自动合并" /><datalist id="chat-senders"><option v-for="a in senderAccounts" :key="String(a.platform) + String(a.role) + identifier(a)" :value="identifier(a)">{{ a.platform }} · {{ label(a) }}</option></datalist></label>
      <label>开始日期<input v-model="from" type="date" /></label><label>结束日期<input v-model="to" type="date" /></label>
      <div class="chat-actions"><button type="submit" class="primary-button" :disabled="busy">{{ busy ? '查询中…' : '查找原文' }}</button><label class="chat-check"><input type="checkbox" v-model="cacheOnly" />仅查本地缓存</label><button type="button" :disabled="directoryBusy" @click="directories()">刷新账号/会话</button><button type="button" v-if="accountMore" @click="directories(false, 'accounts')">更多账号</button><button type="button" v-if="conversationMore" @click="directories(false, 'conversations')">更多会话</button><button type="button" :disabled="!platform || !source || !conversation" @click="sync()">缓存此会话范围</button><button type="button" :disabled="!platform || !source || !conversation" @click="sync(true)">重新初始化此范围</button></div>
      <p>默认最近 30 天；改日期可检索旧历史。缓存同步需指定平台、采集账号和会话，最多登记 5 个会话。清空日期仍使用最近 30 天。</p>
    </form>
    <p v-if="error" role="alert" class="chat-error">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="page">
      <p class="chat-scope">{{ page.origin === 'mac' ? 'Mac 已捕获原文' : '本地缓存局部结果' }} · {{ page.items.length }} 条 · 从 {{ date(frozen.sent_from) }} 至 {{ frozen.sent_to ? date(frozen.sent_to) : '当前' }}{{ frozen.keyword ? ` · 关键词：${frozen.keyword}` : '' }}<span v-if="page.source_error"> · {{ page.source_error }}</span></p>
      <div class="chat-layout">
        <section class="panel chat-results"><p v-if="!page.items.length" class="chat-empty">当前范围未找到记录。来源缺口和所有历史是否存在仍未知。</p>
          <button v-for="r in page.items" :key="r.record_id" class="chat-message" @click="open(r)"><small>{{ r.platform }} · {{ r.conversation_display_name || r.conversation_id }} · {{ r.sender_display_name || r.sender_account_id || '发送者未知' }} · {{ date(r.sent_at) }}</small><p>{{ excerpt(r) }}</p><small>修订 {{ r.record_revision }} · {{ r.message_type }}</small></button>
          <button v-if="page.has_more" :disabled="busy" @click="search(true)">加载下一页</button>
        </section>
        <section class="panel chat-context" v-if="selected"><div class="chat-actions"><strong>原文与上下文</strong><button @click="selected = null; context = null">返回结果</button></div><p v-if="contextBusy">读取上下文…</p>
          <template v-if="context"><p>{{ context.origin === 'cache' ? '离线缓存，前后文可能有缺口' : '同会话上下文' }} · 引用状态 {{ context.reference_status }}</p><p v-if="context.truncated_before || context.truncated_after">前后仍有记录，当前仅展示有限上下文。</p><button :disabled="expanded >= 25 || contextBusy" @click="expand">扩展到前后各 25 条</button>
            <article v-for="r in context.items" :key="r.record_id" class="chat-turn" :class="{ anchor: r.record_id === selected.record_id }"><small>{{ r.sender_display_name || r.sender_account_id || '发送者未知' }} · {{ date(r.sent_at) }}</small><p>{{ r.text ?? '正文不可用' }}</p><details><summary>来源、附件与引用元数据</summary><pre>{{ JSON.stringify({ source: r.source_locator, revision: r.record_revision, attachments: r.attachments, reply: r.reply, quote: r.quote, forward: r.forward }, null, 2) }}</pre></details></article>
            <details v-if="context.references?.length"><summary>引用链（保留引用自身的来源和会话）</summary><pre>{{ JSON.stringify(context.references, null, 2) }}</pre><button v-for="reference in context.references" :key="String(reference.record_id)" v-show="reference.record_id" @click="open(reference as unknown as ChatRecord)">打开引用原文 {{ reference.record_id }}</button></details>
          </template>
        </section>
      </div>
      <section class="panel chat-answer"><h2>依据原文回答</h2><p>按当前已执行的筛选范围检索，补取有限前后文和后续消息；不运行全历史模型分析。</p><div class="chat-actions"><input v-model="question" maxlength="2000" placeholder="最后定在什么时候？哪些内容仍不确定？" /><button :disabled="!question.trim() || ['pending', 'running'].includes(job?.state ?? '')" @click="answer">生成回答</button><button v-if="job && ['pending', 'running'].includes(job.state)" @click="chatApi.cancel(job.id)">停止</button></div>
        <p v-if="job && ['pending', 'running'].includes(job.state)">正在有限补取证据并生成回答；搜索仍可使用。</p><p v-if="job?.error" role="alert">回答未完成：{{ job.error }}</p><p v-if="job?.stale" class="chat-error">来源已更新，此回答已失效，请重新生成。</p>
        <template v-if="job?.value && !job.stale"><article v-for="(claim, i) in job.value.claims" :key="i"><p><strong>{{ { quote: '原话', summary: '归纳', inference: '推断' }[claim.kind as 'quote'] }}：</strong>{{ claim.text }}</p><button v-for="id in claim.citations" :key="id" @click="citation(id) && open(citation(id)!)">{{ citation(id)?.platform }} · {{ citation(id)?.sender_display_name || citation(id)?.sender_account_id || '未知发送者' }} · {{ date(citation(id)?.sent_at) }} · 查看原文</button></article><p v-for="(unknown, i) in job.value.unknown" :key="i">无法确定：{{ unknown }}</p><p v-for="(gap, i) in job.value.known_gaps" :key="i">证据缺口：{{ gap }}</p><p>{{ job.value.disclaimer }}</p></template>
      </section>
    </template>
  </section>
</template>

<style scoped>
.chat-page { max-width: 1400px; }.chat-status,.chat-filters,.chat-results,.chat-context,.chat-answer { padding: 22px; margin-bottom: 18px; }.chat-status p,.chat-filters p,.chat-scope { color: var(--muted); line-height: 1.6; }.chat-filters { display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px; }.chat-filters label { display:grid;gap:7px; }.chat-filters p,.chat-filters > .chat-actions { grid-column:1/-1; }.chat-page input,.chat-page select { padding:10px;border:1px solid var(--line);border-radius:8px;min-width:0; }.chat-page button { padding:9px 12px;border:1px solid var(--line);border-radius:8px;cursor:pointer; }.chat-page button:disabled { opacity:.55;cursor:default; }.chat-actions { display:flex;gap:10px;align-items:center;flex-wrap:wrap; }.chat-check { display:flex!important;align-items:center; }.chat-layout { display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px; }.chat-results:only-child { grid-column:1/-1; }.chat-message { display:block;width:100%;text-align:left;background:transparent;border-width:0 0 1px!important;border-radius:0!important; }.chat-message p,.chat-turn p { white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.7; }.chat-message small,.chat-turn small { color:var(--muted);overflow-wrap:anywhere; }.chat-turn { padding:14px 10px;border-bottom:1px solid var(--line); }.anchor { border-left:3px solid var(--teal);background:#edf5eb; }.chat-page pre { max-height:260px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px; }.chat-page summary { cursor:pointer;padding:10px 0; }.chat-error { color:#a33628; }.chat-answer input { flex:1;min-width:230px; }.chat-range { display:flex;gap:10px;flex-wrap:wrap;overflow-wrap:anywhere;margin-top:10px;font-size:12px; }.chat-context { align-self:start; }.chat-empty { color:var(--muted);padding:20px; }@media(max-width:950px) { .chat-layout { grid-template-columns:1fr; }.chat-filters { grid-template-columns:repeat(2,minmax(0,1fr)); }}@media(max-width:600px) { .chat-filters { grid-template-columns:1fr; }}
</style>
