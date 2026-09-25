// Real Chromium + production Vue component/audio element; synthetic HTTP/WAV fixture.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
const require = createRequire(import.meta.url)
const { chromium } = require(process.env.REVIEW_PLAYWRIGHT || 'playwright')
const html = `<div id="app"></div><script type="module">
import { createApp, ref, h } from 'vue';
import VoiceSampleAudition from '/v3/components/VoiceSampleAudition.vue';
import VoiceReviewActions from '/v3/components/VoiceReviewActions.vue';
const initial = { prototype_id:'p', person_id:'person', person_name:'Synthetic',session_id:'s',quality_score:.8,best_score:.8,score_margin:.2,
 representative_clips:[{media_id:'m',start_ms:0,end_ms:3000},{media_id:'m',start_ms:3500,end_ms:6500},{media_id:'other',start_ms:7000,end_ms:10000}] };
createApp({components:{VoiceSampleAudition,VoiceReviewActions},setup(){const candidate=ref(initial); window.replaceCandidate=()=>candidate.value={...candidate.value, prototype_id:'p2'};return {candidate}},
 render(){return h('div',[h(VoiceReviewActions,{candidates:[this.candidate],busyId:'',reviewId:'r'}),h(VoiceSampleAudition,{candidate:this.candidate},{default:()=>h('button','撤回授权')})])}}).mount('#app');
</script>`
const server = await createServer({configFile:false, root:resolve(import.meta.dirname,'..'), plugins:[vue(), {name:'probe-page',configureServer(s){s.middlewares.use('/probe',async (_req,res)=>{res.setHeader('Content-Type','text/html');res.end(await s.transformIndexHtml('/probe',html))})}}], server:{host:'127.0.0.1',port:0}, optimizeDeps:{include:['vue']}})
await server.listen()
const browser = await chromium.launch({executablePath:process.env.REVIEW_BROWSER,headless:true,args:['--autoplay-policy=no-user-gesture-required']})
try {
  const page = await browser.newPage()
  const errors=[]; page.on('pageerror',error=>errors.push(error.message))
  let unavailable=false, corrupt=false, pendingAudio=null
  const wav=Buffer.alloc(44+9*16000*2)
  wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(16000,24);wav.writeUInt32LE(32000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40)
  for(let i=0;i<9*16000;i++) wav.writeInt16LE(Math.round(1000*Math.sin(2*Math.PI*440*i/16000)),44+i*2)
  await page.route('**/api/v3/voice-audition',async route=>{
    const body=route.request().postDataJSON()
    if (pendingAudio && !body.describe) await pendingAudio
    await route.fulfill({json:{audition_key:body.prototype_id, audio_available:!unavailable,audio_unavailable_reason:unavailable?'原音不可用':'', total_ms:9000,window_count:3,
      windows:[{media_id:'m',start_ms:0,end_ms:3000,playback_start_ms:0},{media_id:'m',start_ms:3500,end_ms:6500,playback_start_ms:3000},{media_id:'other',start_ms:7000,end_ms:10000,playback_start_ms:6000}],
      complete_sample:true,data_base64url:(corrupt?Buffer.from('bad'):wav).toString('base64url')}})
  })
  await page.goto(`${server.resolvedUrls.local[0]}probe`)
  const play=page.getByRole('button',{name:'▶ 完整串播'}).first()
  const confirm=page.getByRole('button',{name:'确认此样本',exact:true})
  await play.waitFor(); await page.waitForFunction(()=>!document.querySelector('.sample-audition button').disabled)
  assert(await confirm.isDisabled())
  await play.click(); await page.waitForTimeout(200)
  assert(await confirm.isDisabled(), 'playing is not completed')
  await page.getByRole('button',{name:'停止试听'}).click()
  assert(await confirm.isDisabled())
  console.log('PASS actual browser start and stop do not complete aggregate')
  await play.click(); await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='确认此样本'&&!b.disabled),{},{timeout:15000})
  assert(await confirm.isEnabled()); assert(await page.getByText('所有窗口已播放完成，可以判断',{exact:true}).count()===1)
  console.log('PASS real audio element ended after full 9-second aggregate unlocks only its sample')
  await page.evaluate(()=>window.replaceCandidate())
  assert(await confirm.isDisabled())
  await play.click(); await page.waitForTimeout(100)
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))})
  assert(await confirm.isDisabled())
  await page.reload(); await play.waitFor(); assert(await confirm.isDisabled())
  console.log('PASS candidate replacement, hidden-page lifecycle and recreation reset coverage')
  corrupt=true
  await play.click(); await page.getByText(/试听失败或中断/).first().waitFor()
  assert(await confirm.isDisabled())
  console.log('PASS decode/playback failure never unlocks confirmation')
  unavailable=true;await page.reload();await page.getByText('原音不可用',{exact:true}).first().waitFor()
  assert(await play.isDisabled());assert(await page.getByRole('button',{name:'撤回授权'}).isEnabled())
  assert.deepEqual(errors,[])
  console.log('PASS missing source disables audition while withdrawal remains independent')
} finally { await browser.close(); await server.close() }
