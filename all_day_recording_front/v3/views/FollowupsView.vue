<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { request } from '../core/api'
import { identityLabel, chatApi, type ChatContext } from '../core/chat'

type Item = { source_key: string; event_id: string; revision: number; category: string; status: string; updated_at: string; ignored: boolean; conflict: unknown; payload: {title?: string}; chat: { actor_is_from_self?: boolean|null; identity_conflict?:boolean; historical?: boolean; progress_unknown?: boolean; actor_account_id?: string; recipient_account_ids?: string[]; uncertainty?: string[]; time?: {expression?: string; date?: string; at?: string; precision?: string; uncertainty?: string}; evidence?: {record_id: string; quote: string; sent_at: number | null; is_from_self?: boolean|null; self_identity_basis?: string; sender_account_id?: string; sender_display_name?: string}[] } }
type Job = {id: string; state: string; calls: number; position: number; batch_count?: number; unique_messages?: number; applied: number; failed_candidates: number; processing_watermark?: number; error?: string; coverage?: {received?: number; complete: boolean; reason?: string}[]}
type Status = {identity_refresh?: {state:string;success:number;failed:number;remaining:number;total:number;error?:string}; identity_rebuild?:{reinterpreted:number;evidence_unavailable:number};self_identity?: {automatic:unknown[];manual:Record<string,unknown>;conflicts:{key:string;reason:string}[]};local_handoff?:{state:string;error?:string;acceptance?:{unique_messages:number;historical_messages:number;recent_messages:number;calls:number;coverage_complete:boolean}};scope_count: number; scope_mode?: string; selected_scopes: {platform:string;source_account_id:string;conversation_id?:string}[]; timezone: string | null; self_mapping_count: number; calendar: string; jobs: Job[]}
type ReviewCandidate = {job_id:string;batch:number;candidate:number;binding:Record<string,string>;title:string;actor_is_from_self:boolean|null;category:string;accepted:boolean;time:{expression?:string;uncertainty?:string};uncertainty:string[];evidence:{record_id:string;quote:string}[]}
const reviewCandidates=ref<ReviewCandidate[]>([])
const items = ref<Item[]>([]), status = ref<Status | null>(null), error = ref(''), busy = ref(false)
const filter = ref('all'), offset = ref(0), more = ref(false), context = ref<ChatContext | null>(null)
const history=ref<{event_revision:number;operation_kind:string;created_at:string;actor:string}[] | null>(null)
const editing = ref<Item | null>(null), title = ref(''), date = ref(''), at = ref(''), link = ref('')
const choices = ref<{key:string;label:string;platform:string;source:string;account:string}[]>([]), chosen=ref('')
const platform = ref('qq'), source = ref(''), account = ref(''), timezone = ref('')
const labels: Record<string,string> = {self:'需要我处理',waiting:'等对方',pending:'已相关 / 待明确',ended:'已结束',cared:'关心的近况'}
const get = <T>(path: string) => request<T>('/api/v3/chat/followups/'+path)
const post = <T>(path: string, body: unknown) => request<T>('/api/v3/chat/followups/'+path,{method:'POST',body:JSON.stringify(body)})
let timer: ReturnType<typeof setInterval> | undefined
async function load(reset = false) {
  try {
    if(reset) offset.value=0
    const page=await get<{items:Item[];has_more:boolean}>(`items?limit=15&offset=${offset.value}`)
    items.value=reset?page.items:[...items.value,...page.items];more.value=page.has_more
    status.value=await get<Status>('status')
    reviewCandidates.value=(await get<{items:ReviewCandidate[]}>('review-candidates')).items
  } catch(e) {error.value=e instanceof Error?e.message:String(e)}
}
async function run(action: string, body: unknown={}) {
  busy.value=true;error.value=''
  try {await post(action,body);await load(true)} catch(e){error.value=e instanceof Error?e.message:String(e)} finally{busy.value=false}
}
async function acceptCandidate(c:ReviewCandidate){await run('accept-candidate',{job_id:c.job_id,batch:c.batch,candidate:c.candidate,binding:c.binding})}
async function act(item: Item, action: string, changes?: unknown) {await run('act',{source_key:item.source_key,action,expected_revision:item.revision,...(changes?{changes}:{})})}
async function changes(item: Item){try{history.value=(await request<{items:{event_revision:number;operation_kind:string;created_at:string;actor:string}[]}>(`/api/v3/events/${encodeURIComponent(item.event_id)}/operations`)).items}catch(e){error.value=e instanceof Error?e.message:String(e)}}
async function view(record: string) {try{context.value=await chatApi.context(record)}catch(e){error.value=e instanceof Error?e.message:String(e)}}
function edit(item: Item) {editing.value=item;title.value=item.payload.title??'';date.value=item.chat.time?.date??'';at.value=item.chat.time?.at??'';link.value=''}
async function save() {if(editing.value){await act(editing.value,'edit',{title:title.value,date:date.value||null,at:at.value||null});if(!error.value)editing.value=null}}
async function accounts(){
  choices.value=[]
  try {for(const scope of status.value?.selected_scopes??[]){const page=await chatApi.directory('accounts',{platform:scope.platform,source_account_id:scope.source_account_id});for(const row of page.items){if(row.role==='sender'&&typeof row.id==='string')choices.value.push({key:JSON.stringify([scope.platform,scope.source_account_id,row.id]),label:String(row.display_name??'未命名')+' · '+row.id,platform:scope.platform,source:scope.source_account_id,account:row.id})}}}
  catch(e){error.value=e instanceof Error?e.message:String(e)}
}
async function saveChosen(){const c=choices.value.find(x=>x.key===chosen.value);if(c)await run('account',{platform:c.platform,source:c.source,account:c.account,person:'self'})}
async function useSelection(){
  try {const scopes=Array.from(new Map((status.value?.selected_scopes??[]).map(x=>[JSON.stringify([x.platform,x.source_account_id]),{platform:x.platform,source_account_id:x.source_account_id}])).values());await run('config',{scopes,timezone:timezone.value||null})}
  catch(e){error.value=e instanceof Error?e.message:String(e)}
}
onMounted(()=>{void load(true);timer=setInterval(()=>{if(status.value?.jobs.some(j=>['pending','running','collecting'].includes(j.state)))void load(true)},2000)})
onUnmounted(()=>{if(timer)clearInterval(timer)})
</script>

<template>
  <section class="followups">
    <header><p>CHAT / FOLLOW UP</p><h1>跟进事项</h1><p>只收录有个人关联证据的事项。未知或无关群聊仍可在聊天查询中检索。</p></header>
    <p v-if="error" role="alert" class="error">{{error}}</p>
    <div class="toolbar"><button :disabled="busy" @click="run('start')">刷新最近 7 天</button><button :disabled="busy" @click="run('stop')">停止分析</button><button @click="load(true)">重载列表</button></div>
    <section id="followup-candidates" aria-label="本次分析候选"><h2>本次分析候选（{{reviewCandidates.length}}）</h2><p>未被自动规则接纳的候选也显示在这里。核对原话后，可以接纳为个人跟进；不需要录音任务 ID，不追加推理或创建提醒。</p><article v-for="c in reviewCandidates" :key="c.binding.source_key"><h3>{{c.title}}</h3><p>承担者：{{c.actor_is_from_self===true?'我本人':c.actor_is_from_self===false?'非本人':'身份待明确'}} · {{c.accepted?'已接纳到个人跟进':'等待你接纳'}}</p><p>原时间表达：{{c.time.expression||'未明确'}}；{{c.time.uncertainty}}</p><p v-for="u in c.uncertainty" :key="u">{{u}}</p><blockquote v-for="e in c.evidence" :key="e.record_id">{{e.quote}} <button @click="view(e.record_id)">查看原文上下文</button></blockquote><button :disabled="busy||c.accepted" @click="acceptCandidate(c)">{{c.accepted?'已接纳':'接纳到个人跟进'}}</button><button v-if="c.accepted" @click="filter=c.category">查看对应分类</button></article><p v-if="!reviewCandidates.length">本次已完成响应中暂无可接纳候选。</p></section>
    <p v-if="status">{{status.scope_mode==='accounts'?'全部会话 · 已选账号':'指定会话'}} {{status.scope_count}} 个 · 本人映射 {{status.self_mapping_count}} 个 · 时区 {{status.timezone ?? '未知'}}</p>
    <p v-if="status?.self_identity">自动本人映射 {{status.self_identity.automatic.length}} 个 · 人工映射 {{Object.keys(status.self_identity.manual).length}} 个；身份冲突 {{status.self_identity.conflicts.length}} 个。</p>
    <details v-if="status?.self_identity?.conflicts.length"><summary>核对身份冲突（人工选择已保留）</summary><p v-for="c in status.self_identity.conflicts" :key="c.key">{{c.key}} · 消息方向与人工映射或其他消息依据不一致</p></details>
    <p v-if="status?.identity_refresh">旧缓存身份刷新：{{status.identity_refresh.state}} · 成功 {{status.identity_refresh.success}} / {{status.identity_refresh.total}} · 失败 {{status.identity_refresh.failed}} · 剩余 {{status.identity_refresh.remaining}} {{status.identity_refresh.error}}</p>
    <p v-if="status?.local_handoff?.acceptance">本地验收工作集：{{status.local_handoff.acceptance.unique_messages}} 条消息（历史 {{status.local_handoff.acceptance.historical_messages}} / 近期 {{status.local_handoff.acceptance.recent_messages}}），已完成 {{status.local_handoff.acceptance.calls}} 次有限推理；{{status.local_handoff.acceptance.coverage_complete?'固定工作集覆盖已完成':'近期范围仍有未分析内容'}}。离线应用不追加推理；无关或相关性未知的候选不进入个人列表。</p>
    <p v-if="status?.local_handoff?.state==='pending'" role="alert">本地候选等待恢复：{{status.local_handoff.error}}</p>
    <p>不限制分析消息条数，按最近 7 天固定范围完整分页。模型分析默认最多 12 次推理、15 分钟预算；完整快照准备后可明确授权剩余批次的有限预算；可停止并保留断点。历史相关事项保留“进展未知”，不会自动激活提醒。本人发言、同群或认识发言者都不单独构成任务。</p>
    <details><summary>范围与本人身份</summary><p>账号范围覆盖该账号所有群聊和私聊，完整读取固定最近 7 天；旧的指定会话配置可切换为所属账号的全部会话。Mac 已明确的本人消息无需手选；未知身份仍可人工确认。人工纠错保留来源，不覆盖消息方向。</p><input v-model="timezone" placeholder="明确来源时区，如 Asia/Shanghai" aria-label="来源时区"/><button @click="useSelection">覆盖这些账号全部会话</button><br/><button @click="accounts">从已选账号加载发送者</button><select v-model="chosen" aria-label="选择本人发送者"><option value="">请选择你本人的发送者</option><option v-for="c in choices" :key="c.key" :value="c.key">{{c.label}}</option></select><button :disabled="!chosen" @click="saveChosen">这是我本人</button><details><summary>手工填写稳定账号</summary><select v-model="platform" aria-label="平台"><option value="qq">QQ</option><option value="wechat">微信</option></select><input v-model="source" placeholder="采集账号 ID" aria-label="采集账号 ID"/><input v-model="account" placeholder="本人发送账号 ID" aria-label="本人发送账号 ID"/><button @click="run('account',{platform,source,account,person:'self'})">保存本人映射</button></details></details>
    <div v-for="j in status?.jobs.slice(0,3)" :key="j.id" class="progress">{{j.state}} · {{j.position}} 批处理完 · {{j.calls}} 次推理 · {{j.unique_messages??0}} 条消息 · {{j.applied}} 次状态写入 · 连续完成水位 {{j.processing_watermark??j.position}} · 校验失败 {{j.failed_candidates}}<p v-if="j.error">{{j.error}}</p><p v-for="(c,i) in j.coverage" :key="i">收到 {{c.received??0}} 条 · {{c.complete?'此查询范围已取完':'覆盖未完成'}} {{c.reason}}</p><button v-if="['failed','paused','interrupted','partial'].includes(j.state)" @click="run('start',{resume_id:j.id})">从断点继续（原预算）</button><button v-if="['FOLLOWUP_PREPARED','FOLLOWUP_BUDGET_EXHAUSTED'].includes(j.error??'') &amp;&amp; (j.batch_count??0)&gt;j.position" @click="run('start',{resume_id:j.id,model_budget:{calls:(j.batch_count??0)-j.position,seconds:10800}})">分析剩余 {{(j.batch_count??0)-j.position}} 批（最多同次数推理 / 3 小时）</button><button v-if="j.error==='FOLLOWUP_BUDGET_EXHAUSTED' &amp;&amp; j.calls&lt;12" @click="run('start',{resume_id:j.id,renew_model_window:true,additional_calls:Math.min(12-j.calls,(j.batch_count??j.position+12)-j.position)})">继续剩余分析（延长 15 分钟，累计最多 12 次推理）</button></div>
    <nav aria-label="事项分类"><button v-for="(label,key) in labels" :key="key" :aria-pressed="filter===key" @click="filter=String(key)">{{label}}</button><button @click="filter='all'">全部</button></nav>
    <article v-for="item in items.filter(i=>filter==='all'||i.category===filter)" :key="item.source_key">
      <h2>{{item.payload.title}}</h2><p>{{labels[item.category]}} · {{item.chat.historical?'历史约定 / 进展未知':'当前跟进'}}</p>
      <p>承接人：{{item.chat.actor_is_from_self===true?"我本人":item.chat.actor_is_from_self===false?"非本人":"身份待明确"}} · {{item.chat.evidence?.find(e=>e.sender_account_id===item.chat.actor_account_id)?.sender_display_name??item.chat.actor_account_id??'未知'}} · 涉及账号：{{item.chat.recipient_account_ids?.join('、')||'未明确'}}</p>
      <p>最近处理：{{new Date(item.updated_at).toLocaleString()}} <button @click="changes(item)">查看变化历史</button></p>
      <p v-if="item.chat.identity_conflict">身份依据与既有判定不一致，请核对；你的人工修改和处理状态已保留。</p>
      <p>约定时间：{{item.chat.time?.at||item.chat.time?.date||'未确定'}} · {{item.chat.time?.expression}} {{item.chat.time?.uncertainty}}</p>
      <p v-for="u in item.chat.uncertainty" :key="u">{{u}}</p>
      <blockquote v-for="(e,i) in item.chat.evidence" :key="e.record_id+':'+i"><p>{{e.quote}}</p><small>{{e.sent_at===null?'发送时间未知':new Date(e.sent_at*1000).toLocaleString()}} · {{identityLabel(e)}} · {{e.sender_account_id}}</small><button @click="view(e.record_id)">查看原文上下文</button></blockquote>
      <div class="toolbar"><button v-if="item.category==='pending'||item.chat.historical" @click="act(item,'confirm',{category:'self'})">确认由我处理</button><button v-if="item.category==='pending'||item.chat.historical" @click="act(item,'confirm',{category:'waiting'})">确认等对方</button><button v-if="item.category!=='ended'" @click="act(item,'complete')">已完成 / 已收到</button><button v-if="item.category!=='ended'" @click="act(item,'ignore')">忽略</button><button v-if="item.category==='ended'" @click="act(item,'reopen')">重新开启</button><button @click="edit(item)">修改 / 关联录音任务</button></div>
      <p v-if="item.conflict">新证据与你的决定存在冲突，原状态已保留。<button @click="act(item,'accept_change')">采用新变化</button><button @click="act(item,'dismiss_change')">保留我的决定</button></p>
      <small>事项 {{item.event_id}} · 修订 {{item.revision}}</small>
    </article>
    <p v-if="!items.some(i=>filter==='all'||i.category===filter)">当前已加载的范围中没有这一类事项；这不代表全部历史没有事项。</p>
    <button v-if="more" @click="offset+=15;load()">加载更多（保留所有候选）</button>
    <p>{{status?.calendar}}</p>
    <dialog :open="!!editing"><h2>修改事项</h2><input v-model="title" aria-label="事项内容"/><input v-model="date" type="date" aria-label="约定日期"/><input v-model="at" placeholder="仅有明确小时才填带时区时间" aria-label="明确时间"/><button @click="save">保存</button><p>明确关联已有录音任务后，两类证据保留在同一任务。相似标题不会自动合并。</p><input v-model="link" placeholder="已有录音任务 ID" aria-label="录音任务 ID"/><button :disabled="!link" @click="editing&&act(editing,'link',{event_id:link})">关联</button><button @click="editing=null">关闭</button></dialog>
    <dialog :open="!!history"><h2>事项变化历史</h2><p v-for="h in history" :key="h.event_revision">{{h.event_revision}} · {{({create:'建立',update:'更新',complete:'完成',cancel:'取消 / 忽略',reopen:'重新开启'} as Record<string,string>)[h.operation_kind]??h.operation_kind}} · {{new Date(h.created_at).toLocaleString()}} · {{h.actor==='desktop-user'?'你的操作':'有原文依据的派生变化'}}</p><button @click="history=null">关闭</button></dialog>
    <dialog :open="!!context"><h2>原文上下文</h2><p v-if="context?.origin==='cache'">当前使用本地已缓存原文，远端状态未更新。</p><div v-for="r in context?.items" :key="r.record_id"><small>{{identityLabel(r)}} · {{r.sender_account_id}} · {{r.sent_at===null?'未知时间':new Date(r.sent_at*1000).toLocaleString()}}</small><pre>{{r.text}}</pre></div><button @click="context=null">关闭</button></dialog>
  </section>
</template>

<style scoped>
.followups{max-width:1100px;margin:auto;padding:32px}header p,small{color:#65717b}h1{font-size:32px}article,.progress,details{border:1px solid #d3dedb;border-radius:12px;padding:18px;margin:16px 0;background:#fff}button,input,select{padding:8px 12px;margin:4px;border:1px solid #adbeb7;border-radius:7px}button{cursor:pointer}button[aria-pressed=true]{background:#d7ece3}.toolbar{display:flex;flex-wrap:wrap;gap:6px}blockquote{border-left:3px solid #60a38b;padding-left:12px;margin:14px 0}.error{color:#a52020}dialog[open]{position:fixed;top:8vh;max-height:80vh;overflow:auto;width:min(760px,85vw);z-index:20;border:1px solid #65717b;border-radius:12px;box-shadow:0 10px 80px #0004}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
