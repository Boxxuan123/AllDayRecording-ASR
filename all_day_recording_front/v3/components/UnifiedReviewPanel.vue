<script setup lang="ts">
import { ref, watch, computed, onBeforeUnmount } from 'vue'
import { unifiedSections, coverageExplanation, recordingIdentity } from '../core/unifiedReview'
import { request } from '../core/api'
import { playSessionRange, release } from '../core/media'
import { navigate } from '../core/router'
import { chatApi, type ChatContext } from '../core/chat'
interface Evidence { participant?:{kind?:string;label?:string;status?:string}; event_ids?:string[]; evidence_id:string; source_type:string; text:string; speaker?:string; sent_at?:number; platform?:string; record_id?:string; revision:number; session_id?:string; start_ms?:number; end_ms?:number; start_at?:string; context?:object; audio_ranges?:{media_id:string;start_ms:number;end_ms:number}[] }
interface Claim { section:string; text:string; certainty:string; perspective:string; event_ids:string[]; evidence:{evidence_id:string;quote:string}[] }
interface Review { status:string; date:string; timezone:string; generated_at?:string; revised_at?:string; error?:string; coverage?:{chat_records_in_day:number;chat_records_selected:number;chat_records_excluded:number;recording_events:number;recording_evidence:number;period_start:string;period_end:string;scope:string;missing_sources:string[]}; claims?:Claim[]; evidence?:Evidence[]; limitations?:string[] }
const props=defineProps<{day:string;timezone:string}>()
const review=ref<Review|null>(null), busy=ref(false), error=ref(''), expanded=ref(-1), context=ref<ChatContext|null>(null)
const sections = computed(()=>unifiedSections.filter(s=>review.value?.claims?.some(c=>c.section===s.key)))
const perspectives:Record<string,string>={self:'本人',other:'他人',observed:'听到／看到的内容',unknown:'身份未确认'}
let generation=0
async function load() { const current=++generation; try {const v=await request<Review>(`/api/v3/unified-review?date=${encodeURIComponent(props.day)}&timezone=${encodeURIComponent(props.timezone)}`);if(current===generation){review.value=v;error.value='';expanded.value=-1}} catch(e){if(current===generation)error.value=e instanceof Error?e.message:String(e)} }
async function generate() {busy.value=true;error.value='';const day=props.day, timezone=props.timezone;try { const v=await request<Review>('/api/v3/unified-review/generate',{method:'POST',body:JSON.stringify({date:day,timezone})});if(day===props.day&&timezone===props.timezone)review.value=v }catch(e){error.value=e instanceof Error?e.message:String(e)}finally{busy.value=false}}
function sources(claim:Claim) {return claim.evidence.map(r=>({ref:r,source:review.value?.evidence?.find(e=>e.evidence_id===r.evidence_id)}))}
function openTask(claim:Claim,id:string) { const chat=sources(claim).some(r=>r.source?.source_type==='chat'&&r.source.event_ids?.includes(id));navigate((chat?'/followups':'/reminders')+'?event='+encodeURIComponent(id)) }
function play(e:Evidence) {if(e.session_id&&e.start_ms!==undefined&&e.end_ms!==undefined)playSessionRange(e.session_id,e.start_ms,e.end_ms)}
async function openChat(e:Evidence) {if(!e.record_id)return;try {context.value=await chatApi.context(e.record_id)}catch(err){error.value=err instanceof Error?err.message:String(err)}}
watch(()=>[props.day,props.timezone],()=>void load(),{immediate:true})
onBeforeUnmount(()=>{generation++;release()})
</script>
<template>
  <article class="panel unified-review">
    <h2>聊天＋录音 · 每日回顾</h2>
    <p>{{ day }} · {{ timezone }}<span v-if="review?.generated_at"> · 生成于 {{ new Date(review.generated_at).toLocaleString() }}</span></p>

    <p v-if="review?.coverage?.missing_sources.length">缺少来源：{{ review.coverage.missing_sources.map(s=>s==='chat'?'聊天':'录音').join('、') }}</p>
    <p v-if="review?.status==='failed'||review?.status==='input_changed'">{{ review.status==='failed'?'本次生成失败，保留上次有效回顾。':'资料已更新，以下为上次回顾。' }} {{ review.error }}</p>
    <p v-if="review?.revised_at">修订于 {{ new Date(review.revised_at).toLocaleString() }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="review?.status==='not_generated'">该日尚未生成统一回顾。</p>
    <template v-for="section in sections" :key="section.key">
      <h3>{{ section.label }}</h3>
      <template v-for="(claim,index) in review?.claims??[]" :key="index">
        <section v-if="claim.section===section.key">
          <button class="review-claim" @click="expanded=expanded===index?-1:index">【{{ perspectives[claim.perspective]??'身份未确认' }}】{{ claim.certainty==='uncertain'?'【不确定】':'' }}{{ claim.text }} · 查看依据</button>
          <div v-if="expanded===index">
            <div v-for="row in sources(claim)" :key="row.ref.evidence_id">
              <blockquote>{{ row.ref.quote }}</blockquote>
              <template v-if="row.source">
                <small>{{ row.source.source_type==='chat'?'聊天 · '+row.source.platform+' · '+(row.source.speaker??'身份未确认'):'录音转写 · '+recordingIdentity(row.source) }} · {{ row.source.sent_at?new Date(row.source.sent_at*1000).toLocaleString():row.source.start_at }} · 原文修订 {{ row.source.revision }}</small>
                <details><summary>完整原文与上下文</summary><pre>{{ row.source.text }}</pre><pre v-if="row.source.context">{{ JSON.stringify(row.source.context,null,2) }}</pre></details>
                <button v-if="row.source.source_type==='chat'" @click="openChat(row.source)">打开聊天上下文</button>
                <template v-else><button v-if="row.source.audio_ranges?.length" @click="play(row.source)">播放引用片段</button><button @click="navigate('/recordings/'+encodeURIComponent(row.source.session_id??'')+'?tab=timeline')">打开录音转写</button></template>
              </template>
            </div>
            <button v-for="id in claim.event_ids" :key="id" @click="openTask(claim,id)">打开关联事项</button>
          </div>
        </section>
      </template>
    </template>
    <h3 v-if="review?.coverage">资料覆盖与不确定性</h3>
    <p v-if="review?.coverage">聊天缓存 {{ review.coverage.chat_records_in_day }} 条，选取个人相关互动及上下文 {{ review.coverage.chat_records_selected }} 条；录音 {{ review.coverage.recording_events }} 个事件、{{ review.coverage.recording_evidence }} 段转写。<br>{{ review.coverage.period_start }} 至 {{ review.coverage.period_end }}<br>{{ review.coverage.scope }}</p>
    <p v-if="review?.coverage">{{ coverageExplanation }}</p>
    <p v-for="line in review?.limitations??[]" :key="line">{{ line }}</p>
    <button :disabled="busy" @click="generate">{{ busy?'正在生成…':'生成／更新这一天的统一回顾' }}</button>
    <dialog :open="!!context"><h3>聊天原文与上下文</h3><p v-for="r in context?.items" :key="r.record_id">{{ r.sender_display_name }}：{{ r.text }}</p><button @click="context=null">关闭</button></dialog>
  </article>
</template>
<style scoped>.review-claim{text-align:left;width:100%;padding:10px;margin-bottom:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{border-left:3px solid #4d7b70;padding-left:12px}h3{margin-top:20px}</style>
