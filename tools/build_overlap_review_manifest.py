"""Private PC acoustic review queue and independent JSON ledger; no production DB."""

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from short_self_dataset import digest, read, write

DECISIONS = ("real_foreign", "target_only", "uncertain")
LOCK = Lock()

HTML = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>短重叠声学审核箱</title>
<style>body{font:16px system-ui;max-width:950px;margin:32px auto;padding:0 20px;background:#f4f6fa;color:#172033}article{background:white;padding:24px;margin:20px 0;border-radius:14px}audio{width:100%}button{padding:12px;margin:8px;border:1px solid #aab4c8;border-radius:8px;background:white;cursor:pointer}.saved{color:#176534}small{color:#59677e}h1{font-size:26px}</style>
<h1>短重叠声学审核箱</h1><p>先听完整上下文，再听边界区域。判断前后两个声音是否同时存在；听不清时选“不确定”。</p><p>每段音频可重复播放或循环。记录只进入本次审计账本。</p><div id="status"></div><main id="list"></main>
<script>
const statusEl=document.getElementById('status'), listEl=document.getElementById('list');
const labels={real_foreign:'明确听到第二个人',target_only:'只听到目标人',uncertain:'听不清／不确定'};
async function load(){const queue=await fetch('/queue.json').then(r=>r.json());const ledger=await fetch('/review-ledger').then(r=>r.json());
statusEl.textContent=`待审核 ${queue.items.length} 条；已记录 ${ledger.reviews.length} 次审核`;
for(const item of queue.items){const card=document.createElement('article');const title=document.createElement('h2');title.textContent=(item.anchor?'固定 Case B':'样本 '+item.event_id.slice(0,8));card.append(title);
const info=document.createElement('p');info.textContent=`完整上下文中需辨别的区域位于 ${item.overlap_in_full_context_seconds.map(x=>x.toFixed(3)).join('–')} 秒。先比较区域前后的声音，再判断是否同时出声。`;card.append(info);const heard=new Set();
for(const [name,label] of [['full-context','完整上下文'],['target-boundary','边界区域'],['overlap-only','重叠区域原始片段'],['pre-overlap-context','前一段声音'],['post-overlap-context','后一段声音']]){if(!item.audio[name])continue;const p=document.createElement('p');p.textContent=label;card.append(p);const audio=document.createElement('audio');audio.controls=true;audio.preload='none';audio.src=item.audio[name];audio.addEventListener('ended',()=>heard.add(name));card.append(audio);const repeat=document.createElement('button');repeat.textContent='循环播放';repeat.onclick=()=>{audio.loop=!audio.loop;repeat.textContent=audio.loop?'停止循环':'循环播放';};card.append(repeat);audio.addEventListener('timeupdate',()=>{if(audio.currentTime>audio.duration*.95)heard.add(name);});}
const result=document.createElement('p');const previous=ledger.reviews.filter(r=>r.event_id===item.event_id&&r.component_index===item.component_index).at(-1);if(previous)result.textContent='上次记录：'+labels[previous.decision];
for(const decision of ['real_foreign','target_only','uncertain']){const button=document.createElement('button');button.textContent=labels[decision];button.onclick=async()=>{if(!heard.has('full-context')||!heard.has('target-boundary')){result.textContent='请先播放完整上下文和边界区域。';return;}const response=await fetch('/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:item.event_id,component_index:item.component_index,decision,listened_context:true})});result.textContent=response.ok?'已记录：'+labels[decision]:'记录失败，请重试';result.className=response.ok?'saved':'';};card.append(button);}card.append(result);listEl.append(card);}}
load().catch(()=>statusEl.textContent='审核箱加载失败，请重试。');</script></html>"""


def validate_root(output):
    output = Path(output).resolve()
    # Require a frozen audit manifest, and refuse a production state directory.
    if (output / "core.sqlite3").exists() or (output / "identity").exists():
        raise ValueError("review output must not be production state")
    manifest = read(output / "manifest.json")
    if manifest.get("format") != "short-regular-overlap-acoustic-audit-v1" or digest(
        output / "manifest.json"
    ) != (output / "manifest.sha256").read_text(encoding="ascii"):
        raise ValueError("invalid frozen audit manifest")
    return output, manifest


def build(output):
    output, manifest = validate_root(output)
    events = {e["event_id"]: e for e in manifest["strict_human_events"]}
    items = []
    for item in read(output / "audio-index.json"):
        event = events[item["event_id"]]
        clips = item["clips"]
        start = clips["full-context"]["requested_range_ms"][0]
        items.append(
            {
                "event_id": item["event_id"],
                "component_index": item["component_index"],
                "anchor": event["anchor"],
                "component": item["component"],
                "overlap_in_full_context_seconds": [
                    (item["component"][k] - start) / 1000
                    for k in ("start_ms", "end_ms")
                ],
                "audio": {
                    name: f"/audio/{item['event_id']}/{item['component_index']}/{name}.wav"
                    for name in clips
                },
            }
        )
    items.sort(
        key=lambda i: (
            not i["anchor"],
            i["component"]["duration_ms"],
            i["event_id"],
            i["component_index"],
        )
    )
    write(
        output / "queue.json",
        {"purpose": "acoustic overlap, not speaker identity", "items": items},
    )
    (output / "review.html").write_text(HTML, encoding="utf-8")
    if not (output / "overlap-review-ledger.json").exists():
        write(
            output / "overlap-review-ledger.json",
            {"format": "independent-acoustic-review-v1", "reviews": []},
        )
    return len(items)


def record(output, event_id, component_index, decision, *, reviewer, listened_context):
    output, _ = validate_root(output)
    if decision not in DECISIONS or listened_context is not True or not reviewer:
        raise ValueError(
            "explicit acoustic decision and listened-context attestation required"
        )
    item = next(
        (
            i
            for i in read(output / "audio-index.json")
            if i["event_id"] == event_id and i["component_index"] == component_index
        ),
        None,
    )
    if item is None:
        raise ValueError("unknown audit audio item")
    for clip in item["clips"].values():
        if digest(clip["path"]) != clip["sha256"]:
            raise ValueError("review audio fingerprint changed")
    with LOCK:
        ledger = read(output / "overlap-review-ledger.json")
        ledger["reviews"].append(
            {
                "event_id": event_id,
                "component_index": component_index,
                "decision": decision,
                "reviewer": reviewer,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
                "listened_context": True,
                "audio_sha256": {k: v["sha256"] for k, v in item["clips"].items()},
                "manifest_sha256": digest(output / "manifest.json"),
                "scope": "independent acoustic audit only; no identity/profile/learning/truth updates",
            }
        )
        temporary = output / "overlap-review-ledger.tmp"
        write(temporary, ledger)
        temporary.replace(output / "overlap-review-ledger.json")
    return ledger["reviews"][-1]


def serve(output, port):
    output, _ = validate_root(output)
    build(output)
    files = {}
    for item in read(output / "audio-index.json"):
        for name, clip in item["clips"].items():
            path = Path(clip["path"]).resolve()
            if not path.is_relative_to(output):
                raise ValueError("audio outside private audit directory")
            files[f"/audio/{item['event_id']}/{item['component_index']}/{name}.wav"] = (
                path
            )

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, raw, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            allowed = {
                "/": output / "review.html",
                "/queue.json": output / "queue.json",
                "/review-ledger": output / "overlap-review-ledger.json",
            } | files
            if path not in allowed:
                self.respond(404, b"not found", "text/plain")
                return
            kind = (
                "audio/wav"
                if path in files
                else "text/html; charset=utf-8"
                if path == "/"
                else "application/json"
            )
            self.respond(200, allowed[path].read_bytes(), kind)

        def do_POST(self):
            if (
                self.path != "/review"
                or self.headers.get("Origin") != f"http://127.0.0.1:{port}"
            ):
                self.respond(403, b"forbidden origin", "text/plain")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("invalid request size")
                data = json.loads(self.rfile.read(size))
                result = record(
                    output,
                    data["event_id"],
                    data["component_index"],
                    data["decision"],
                    reviewer="private_pc_acoustic_review",
                    listened_context=data.get("listened_context"),
                )
                self.respond(200, json.dumps(result).encode(), "application/json")
            except (ValueError, KeyError, TypeError):
                self.respond(400, b"invalid review", "text/plain")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        print(f"Private acoustic review: http://127.0.0.1:{port}/", flush=True)
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    print(serve(args.output, args.port) if args.serve else build(args.output))
