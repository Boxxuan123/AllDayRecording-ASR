<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { request } from '../core/api'
interface Contact { id:string; label:string; platform:string; source_account_id:string }
interface Trial { state:string; choices:Contact[]; contacts?:Contact[]; first?:string; last?:string; started_at?:number; ends_at?:number; last_check_at?:number; latest_message_at?:number; last_completed_at?:number; new_messages?:number; historical_backfill?:number; source_revisions?:number; processed_messages?:number; pending_messages?:number; excluded_messages?:number; failed_index_messages?:number; failures?:number; source_failed_checks?:number; error?:string; source_error?:string; last_memory_revision?:number; baseline_memory_revision?:number; model_calls:number; discussion_counts?:{processed_discussions:number;deferred_discussions:number;excluded_discussions:number}; phone_sync?:{updated_at:string;acknowledged_current_memory:boolean}[] }
const value=ref<Trial|null>(null), error=ref(''), busy=ref(false), selected=ref<string[]>([])
let timer:ReturnType<typeof setInterval>|undefined, closed=false
function time(v?:number){return v?new Date(v*1000).toLocaleString('zh-CN',{timeZone:'Asia/Singapore'}):'尚无记录'}
async function load(){try{const v=await request<Trial>('/api/v3/memory/trial');if(!closed){value.value=v;error.value=''}}catch(e){if(!closed)error.value=String(e)}}
async function act(action:string){busy.value=true;try{await request('/api/v3/memory/trial',{method:'POST',body:JSON.stringify(action==='start'?{action,contacts:selected.value}:{action})});await load()}catch(e){error.value=String(e)}finally{busy.value=false}}
onMounted(()=>{void load();timer=setInterval(()=>void load(),5000)});onBeforeUnmount(()=>{closed=true;if(timer)clearInterval(timer)})
</script>
<template><section aria-label="日常增量试运行"><h3>真实新消息 · 三日增量试运行</h3><p v-if="error" role="alert">状态读取或操作失败：{{error}}</p><p v-if="!value">正在读取试运行状态…</p><template v-else>
<p>状态：{{ ({not_started:'尚未启动',running:'已启用',paused:'已暂停',stopped:'已停止',ended:'观察窗口已结束'} as Record<string,string>)[value.state]??value.state }} · 模型请求 {{value.model_calls}}</p>
<template v-if="value.state==='not_started'"><p>使用已有试点联系人，最多三位、同一来源账号，启动当日至第三日结束自动关闭。需电脑接收工作台运行、Mac 聊天服务可达；不会重跑旧历史。</p><label v-for="p in value.choices" :key="p.id"><input type="checkbox" v-model="selected" :value="p.id" :disabled="selected.length>=3&&!selected.includes(p.id)">{{p.label}} · {{p.platform}}</label><button :disabled="busy||!selected.length" @click="act('start')">启动三日试运行</button></template>
<template v-else><p>{{value.first}} 至 {{value.last}} · Asia/Singapore · {{value.contacts?.map(p=>p.label+'（'+p.platform+'）').join('、')}}</p><p>来源账号：{{value.contacts?.[0]?.source_account_id}} · 开始 {{time(value.started_at)}} · 自动结束 {{time(value.ends_at)}}</p>
<p>来源最后检查 {{time(value.last_check_at)}} · 最新新消息 {{time(value.latest_message_at)}} · 处理完成 {{time(value.last_completed_at)}}</p>
<p>真正新消息 {{value.new_messages??0}} · 历史补录 {{value.historical_backfill??0}} · 来源修订 {{value.source_revisions??0}}</p>
<p>消息：处理 {{value.processed_messages??0}} · 等待 {{value.pending_messages??0}} · 排除 {{value.excluded_messages??0}} · 索引失败 {{value.failed_index_messages??0}}；处理失败次数 {{value.failures??0}} · 来源失败检查 {{value.source_failed_checks??0}}</p>
<p>讨论：处理 {{value.discussion_counts?.processed_discussions??0}} · 等待 {{value.discussion_counts?.deferred_discussions??0}} · 排除 {{value.discussion_counts?.excluded_discussions??0}} · 最近记忆修订 {{value.last_memory_revision}}（启动前 {{value.baseline_memory_revision}}）</p>
<p v-if="value.phone_sync?.length">手机最后游标确认 {{value.phone_sync[0].updated_at}} · {{value.phone_sync[0].acknowledged_current_memory?'已确认当前记忆':'尚未确认当前记忆'}}（沿用同步回执；前台运行时自动拉取）</p><p v-else>手机尚无成功同步确认。</p>
<p v-if="value.error||value.source_error" role="alert">未完成原因：{{value.source_error||value.error}}。游标及等待内容保留；请检查来源连接或现有缓存索引状态。</p>
<button v-if="value.state==='running'" :disabled="busy" @click="act('pause')">暂停试运行</button><button v-if="value.state==='paused'" :disabled="busy" @click="act('resume')">恢复试运行</button><button v-if="['running','paused'].includes(value.state)" :disabled="busy" @click="act('stop')">停止试运行</button>
<p>暂停及停止只关闭本轮触发，已保存记忆、原系统正常同步与人工决定保留。Mac 当前接口仅支持新增；真实撤回/修订能力尚未提供。</p></template></template></section></template>
