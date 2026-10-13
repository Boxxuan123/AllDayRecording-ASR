<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { request } from '../core/api'
import { chatApi, type ChatContext } from '../core/chat'
import { navigate } from '../core/router'
import { playSessionRange, release } from '../core/media'
interface Contact { id:string; label:string; identity_basis:string; person_id?:string; platform:string; discussion_ids?:string[]; last_evidence_at?:string; dates?:string[] }
interface Evidence { source_type:string; quote:string; revision:number; occurred_at:string; learned_at?:string; assertion:string; status:string; speaker:string; is_from_self?:boolean; identity_basis?:string; role?:string; record_id?:string; session_id?:string; start_ms?:number; end_ms?:number; utterance_id?:string }
interface Discussion { id:string; title:string; summary?:string; contacts:Contact[]; thing_id:string; link_basis:string; occurred_at:string; evidence_status:string; evidence:Evidence[]; event_id?:string; pending_change:boolean; evidence_total?:number; evidence_page?:number; window_label?:string }
interface Thing { id:string; title:string; discussion_ids:string[]; progress:string; last_evidence_at?:string; needs_confirmation:boolean }
interface Index { page?:{number:number;total:number;discussion_total:number}; id:string; revision:number; status:string; scope?:{first:string;last:string;coverage:{missing_sources:string[];incremental?:{deferred_discussions:number;excluded_discussions:number;processed_discussions?:number}}}; people:Contact[]; discussions:Discussion[]; things:Thing[]; topics:{label:string;discussion_ids:string[]}[]; candidates:{source:string;target:string;reason:string}[] }
const props=defineProps<{personId?:string}>()
const value=ref<Index|null>(null), loading=ref(true), error=ref(''), busy=ref(false), first=ref('2026-10-05'), last=ref('2026-10-07'), choices=ref<Contact[]>([]), selected=ref<string[]>([]), person=ref(''), thing=ref(''), topic=ref(''), expanded=ref(''), target=ref(''), context=ref<ChatContext|null>(null)
const page=ref(0)
let generation=0, initialized=false
const people=computed(()=>value.value?.people.filter(p=>!props.personId||p.person_id===props.personId)??[])
const visibleThings=computed(()=>value.value?.things.filter(t=>value.value?.discussions.some(r=>t.discussion_ids.includes(r.id)&&(!person.value||r.contacts.some(p=>p.id===person.value))&&(!props.personId||r.contacts.some(p=>p.person_id===props.personId))))??[])
const rows=computed(()=>value.value?.discussions.filter(r=>(!person.value||r.contacts.some(p=>p.id===person.value))&&(!props.personId||r.contacts.some(p=>p.person_id===props.personId))&&(!thing.value||r.thing_id===thing.value)&&(!topic.value||value.value?.topics.find(t=>t.label===topic.value)?.discussion_ids.includes(r.id))).sort((a,b)=>a.occurred_at.localeCompare(b.occurred_at))??[])
async function load(){const n=++generation;loading.value=true;try{const v=await request<Index>('/api/v3/memory?page='+page.value);if(n===generation){value.value=v;if(!initialized&&v.scope){first.value=v.scope.first;last.value=v.scope.last}initialized=true;error.value=''}}catch(e){if(n===generation)error.value=e instanceof Error?e.message:String(e)}finally{if(n===generation)loading.value=false}}
async function act(call:()=>Promise<unknown>){busy.value=true;error.value='';try{await call();await load()}catch(e){error.value=e instanceof Error?e.message:String(e)}finally{busy.value=false}}
async function inventory(){busy.value=true;error.value='';try{const v=await request<{contacts:Contact[]}>('/api/v3/memory/inventory?first='+first.value+'&last='+last.value);choices.value=v.contacts;selected.value=[]}catch(e){error.value=e instanceof Error?e.message:String(e)}finally{busy.value=false}}
function nextPage(n:number){page.value=n;person.value='';thing.value='';topic.value='';expanded.value='';void load()}
function continueBatch(){return act(()=>request('/api/v3/memory/update',{method:'POST',body:JSON.stringify({first:value.value?.scope?.first,last:value.value?.scope?.last,contacts:value.value?.scope&&(value.value.scope as unknown as {contacts:Contact[]}).contacts.map(p=>p.id),continue_pending:true,batch_id:crypto.randomUUID()})}))}
async function evidencePage(r:Discussion,n:number){try{const v=await request<Index>('/api/v3/memory?discussion='+encodeURIComponent(r.id)+'&evidence_page='+n);r.evidence=v.discussions[0].evidence;r.evidence_page=n}catch(e){error.value=String(e)}}
function update(){page.value=0;return act(()=>request('/api/v3/memory/update',{method:'POST',body:JSON.stringify({first:first.value,last:last.value,contacts:selected.value})}))}
function decide(source:string,target:string,action:string){return act(()=>request('/api/v3/memory/decision',{method:'POST',body:JSON.stringify({source,target,action,expected_revision:value.value?.revision})}))}
async function openChat(e:Evidence){if(!e.record_id)return;try{context.value=await chatApi.context(e.record_id)}catch(err){error.value=err instanceof Error?err.message:String(err)}}
function time(value?:string){return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Singapore'}):'证据已失效'}
function chooseThing(id:string){thing.value=id;topic.value='';expanded.value=''}
function sibling(r:Discussion){return value.value?.discussions.find(o=>o.id!==r.id&&o.thing_id===r.thing_id)?.id??''}
onMounted(()=>void load());onBeforeUnmount(()=>{generation++;release()})
</script>
<template>
<article class="panel cross-day-memory">
<h2>有依据的跨天互动与事情</h2>
<p v-if="loading">正在读取记忆…</p><p v-if="error" role="alert">加载或操作失败：{{ error }} <button @click="load">重试</button></p>
<p v-if="value?.status==='not_selected'">尚未选择输入范围。按缓存人物选择后即可查看；无需生成每日总结。</p>
<p v-if="value?.scope">{{ value.scope.first }} 至 {{ value.scope.last }} · Asia/Singapore · 所选缓存范围</p>
<p v-if="value?.scope?.coverage.incremental?.deferred_discussions">待处理 {{ value.scope.coverage.incremental.deferred_discussions }} 段；已处理 {{ value.scope.coverage.incremental.processed_discussions??0 }} 段，排除 {{ value.scope.coverage.incremental.excluded_discussions }} 段。每批最多处理 7 段。 <button :disabled="busy" @click="continueBatch">继续下一有限批次</button></p>
<p v-if="value?.scope?.coverage.missing_sources.length">资料缺失：{{ value.scope.coverage.missing_sources.join('、') }}</p>
<details v-if="!personId"><summary>按日期和联系人更新（不调用模型）</summary>
<label>开始日期 <input type="date" v-model="first"></label><label>结束日期 <input type="date" v-model="last"></label><button :disabled="busy" @click="inventory">盘点缓存</button>
<p>最多三个联系人、三个完整自然日；只更新已选范围。再次更新读取新缓存，相同输入复用结果。</p>
<label v-for="p in choices" :key="p.id"><input type="checkbox" :value="p.id" v-model="selected" :disabled="selected.length>=3&&!selected.includes(p.id)">{{ p.label }} · {{ p.platform }} · {{ p.dates?.join('、') }}</label>
<button :disabled="busy||!selected.length" @click="update">选取并更新</button></details>
<p v-if="value?.status==='no_data'">已选择范围，尚无可用讨论。</p>
<div v-if="value?.status==='ready'">
<div class="toolbar"><button :disabled="page===0||busy" @click="nextPage(page-1)">前一页</button><span>第 {{ page+1 }}/{{ value.page?.total??1 }} 页 · 历史记录保留</span><button :disabled="page+1>=(value.page?.total??1)||busy" @click="nextPage(page+1)">后一页／历史</button><button @click="navigate('/reviews')">所有窗口的待确认建议</button><button @click="person='';thing='';topic=''">全部讨论</button><button v-for="p in people" :key="p.id" @click="person=p.id;thing='';topic=''">{{ p.label }} · 最近互动</button></div>
<div v-for="p in people.filter(p=>!person||p.id===person)" :key="p.id"><h3>{{ p.label }} · {{ p.platform }}</h3><p>{{ p.identity_basis }} · 最后证据 {{ time(p.last_evidence_at) }}</p><button v-if="p.person_id" @click="navigate('/people?person='+encodeURIComponent(p.person_id))">已有人物详情与身份纠正</button><button v-else @click="navigate('/chat')">来源账号与身份纠正</button></div>
<h3>具体事情／独立讨论</h3><button v-for="t in visibleThings" :key="t.id" @click="chooseThing(t.id)">{{ t.title }} · {{ t.discussion_ids.length }} 次记录</button>
<p v-if="thing">{{ value.things.find(t=>t.id===thing)?.progress }} <span v-if="value.things.find(t=>t.id===thing)?.needs_confirmation">· 进展待确认，进入现有跟进审核</span></p>
<h3>话题下分别发生的讨论</h3><button v-for="t in value.topics" :key="t.label" @click="topic=t.label;thing='';person=''">{{ t.label }} · {{ t.discussion_ids.length }} 次独立讨论</button><p>话题按有依据的讨论内容分组，不能据此合并事情或推断兴趣。</p>
<p v-if="!rows.length">所选人物或筛选范围尚无可用互动记录。</p>
<section v-for="r in rows" :key="r.id">
<small v-if="r.window_label">历史窗口：{{ r.window_label }}</small><h3><button @click="expanded=expanded===r.id?'':r.id">{{ r.title }} · 查看依据</button></h3><small>{{ time(r.occurred_at) }} · {{ r.link_basis==='existing_event'?'已有稳定事件关联':r.link_basis==='human_confirmed'?'人工关联':r.link_basis==='manually_detached'?'人工恢复独立':'独立讨论，未确认具体事情' }}</small>
<p v-if="r.summary">{{ r.summary }}</p>
<p v-if="r.evidence_status!=='valid'">证据已变化或缺失：以下是原修订，不能继续当作已确认事实。</p>
<div v-if="expanded===r.id"><p v-if="(r.evidence_total??0)>8"><button :disabled="(r.evidence_page??0)===0" @click="evidencePage(r,(r.evidence_page??0)-1)">前一页原文</button> 原文第 {{ (r.evidence_page??0)+1 }} 页／{{ r.evidence_total }} 条 <button :disabled="((r.evidence_page??0)+1)*8>=(r.evidence_total??0)" @click="evidencePage(r,(r.evidence_page??0)+1)">后一页原文</button></p><div v-for="(e,i) in r.evidence" :key="i"><blockquote>{{ e.quote }}</blockquote><small>{{ e.speaker }} · is_from_self={{ e.is_from_self??'未知' }} · {{ e.identity_basis??'来源身份依据' }} · {{ e.role==='context'?'邻近上下文，不作为核心事实':'核心证据' }} · {{ time(e.occurred_at) }} · {{ e.assertion }} · 原文修订 {{ e.revision }} · {{ e.status==='valid'?'原文可用':'原文已失效' }}<span v-if="e.learned_at"> · 获知于 {{ time(e.learned_at) }}</span></small><button v-if="e.source_type==='chat'" @click="openChat(e)">聊天原文与上下文</button><template v-else><button :disabled="e.status!=='valid'" @click="playSessionRange(e.session_id!,e.start_ms!,e.end_ms!)">播放片段</button><button @click="navigate('/recordings/'+e.session_id+'?tab=timeline&utterance='+e.utterance_id)">打开转写与人物纠正</button></template></div>
<button v-if="r.event_id" @click="navigate((r.evidence[0]?.source_type==='chat'?'/followups':'/reminders')+'?event='+r.event_id)">关联跟进详情</button>
<label>关联到已有记录 <select v-model="target"><option value="">选择具体讨论</option><option v-for="o in value.discussions.filter(o=>o.id!==r.id&&o.thing_id!==r.thing_id)" :key="o.id" :value="o.id">{{ o.title }} · {{ o.evidence[0]?.quote.slice(0,40) }}</option></select></label><button :disabled="busy||!target||r.evidence_status!=='valid'" @click="decide(r.id,target,'link')">确认是同一件事</button>
<button v-if="sibling(r)" :disabled="busy" @click="decide(r.id,sibling(r),'detach')">撤销此记录的事情关联，恢复独立</button></div>
</section>
<h3 v-if="value.candidates.length">有原文依据的关联建议</h3><section v-for="c in value.candidates" :key="c.source+c.target"><p>{{ value.discussions.find(r=>r.id===c.source)?.title }} ↔ {{ value.discussions.find(r=>r.id===c.target)?.title }} · {{ c.reason }}</p><button :disabled="busy" @click="decide(c.source,c.target,'link')">关联到这件事</button><button :disabled="busy" @click="decide(c.source,c.target,'reject')">不是同一件事</button></section>
</div><section v-if="context" aria-label="聊天原文"><h3>聊天原文与上下文</h3><button @click="context=null">关闭原文</button><p v-if="context.truncated_before||context.truncated_after">展示原消息附近的有限上下文。</p><p v-if="!context.items.length">原文资料暂不可用。</p><article v-for="m in context.items" :key="m.record_id"><small>{{ m.sender_display_name??m.sender_account_id??'未知发送人' }} · {{ m.sent_at?new Date(m.sent_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Singapore'}):'时间缺失' }} · {{ m.record_id===context.anchor_record_id?'引用原消息':'' }}</small><blockquote>{{ m.text??'该消息没有可展示文字' }}</blockquote></article></section>
</article>
</template>
