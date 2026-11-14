#!/usr/bin/env python3
"""多模型群聊 + 私信指挥台 v3.

- 群聊：多个智能体围绕主题讨论，可加投票与主持人裁决；用户消息在会议中作为主席指令，
  会议结束后则让全体成员依次回应。
- 私信：每个成员一个独立对话框，用户可以直接与它一对一聊天。
- 权限：codex / claude 以完全访问权限运行（无沙箱、无审批）；harness 带本机工具循环
  （可执行 PowerShell、读写文件、列目录）。也就是说它们能真实操作这台电脑。

启动：
  python panel_server.py --port 8765
打开 http://127.0.0.1:8765/
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import group_chat as gc  # noqa: E402

MEMBERS = ["codex", "claude", "harness", "workbuddy"]
LOCK = threading.Lock()
QUEUE = []          # 会议中收到的用户指令
THREAD = None


def now():
    return time.strftime("%H:%M:%S")


def new_state():
    return {
        "status": "idle",
        "topic": "", "rounds": 1, "vote": False,
        "members": ["codex", "claude", "harness"],
        "current": None, "votes": {}, "error": "",
        "startedAt": None, "finishedAt": None,
        "channels": {k: {"messages": [], "current": None} for k in ["group"] + MEMBERS},
    }


STATE = new_state()

RECORDS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "records")


def save_record(channel, rolling=False, title=None):
    """Save a conversation to records/ (group: timestamped file; DM: rolling per member)."""
    try:
        os.makedirs(RECORDS, exist_ok=True)
        msgs = STATE["channels"].get(channel, {}).get("messages", [])
        if not msgs:
            return
        lines = [f"# {title or channel} 记录", "",
                 f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        for m in msgs:
            lines += [f"## {m['who']}（{m['ts']}）", "", m["text"], ""]
        if rolling:
            fn = os.path.join(RECORDS, f"dm-{channel}.md")
        else:
            fn = os.path.join(RECORDS, f"{channel}-{time.strftime('%Y%m%d-%H%M%S')}.md")
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


def add(channel, kind, who, text):
    with LOCK:
        STATE["channels"].setdefault(channel, {"messages": [], "current": None})
        STATE["channels"][channel]["messages"].append(
            {"who": who, "text": text, "kind": kind, "ts": now()})


def set_current(channel, who):
    with LOCK:
        STATE["channels"].setdefault(channel, {"messages": [], "current": None})
        STATE["channels"][channel]["current"] = who
        STATE["current"] = who


def history(channel, kinds=("message", "user")):
    msgs = STATE["channels"].get(channel, {}).get("messages", [])
    return gc.transcript_text([(m["who"], m["text"]) for m in msgs if m["kind"] in kinds])


def drain():
    with LOCK:
        out = list(QUEUE)
        QUEUE.clear()
    return out


# ---------------- group meeting ----------------

def run_meeting(topic, rounds, members, vote):
    global THREAD
    try:
        with LOCK:
            STATE.update(status="running", topic=topic, rounds=rounds, vote=vote,
                         members=list(members), votes={}, error="",
                         startedAt=time.strftime("%Y-%m-%d %H:%M:%S"), finishedAt=None)
            STATE["channels"]["group"]["messages"] = []
            STATE["channels"]["group"]["current"] = None
            STATE["current"] = None
        add("group", "system", "主持人", f"会议开始：{topic}（成员 {', '.join(members)}，{rounds} 轮"
                                        f"{'，含投票裁决' if vote else ''}）")
        for rnd in range(1, rounds + 1):
            for name in members:
                cmds = drain()
                for c in cmds:
                    add("group", "user", "你（主席指令）", c)
                set_current("group", name)
                steer = ("\n【主席刚刚下达的指令，必须优先遵守】\n" + "\n".join(cmds)) if cmds else ""
                prompt = (f"这是多智能体群聊的第 {rnd} 轮。\n主题：{topic}\n\n"
                          f"目前的群聊记录：\n{history('group')}\n{steer}\n"
                          f"请以你的身份发言：{gc.SYSTEM.get(name, '')}")
                reply = gc.CALLERS[name](prompt)
                add("group", "message", name, reply)
                set_current("group", None)
        if vote:
            add("group", "system", "主持人", "进入投票环节")
            names = [n for n in members if n != "harness"] or members
            vp = (f"这是多智能体群聊的投票环节。\n主题：{topic}\n\n讨论记录：\n{history('group')}\n\n"
                  f"请只从这些方案提出者中选一个你支持的：{'、'.join(names)}。\n"
                  f"严格按此格式回复一行：投票=<名字>；理由=<一句话不超过40字>")
            with LOCK:
                STATE["votes"] = {}
            for name in members:
                set_current("group", name + "（投票）")
                reply = gc.CALLERS[name](vp)
                m = re.search(r"投票\s*[=:：]\s*(codex|claude|harness|workbuddy)", reply, re.I)
                if m:
                    k = m.group(1).lower()
                    with LOCK:
                        STATE["votes"][k] = STATE["votes"].get(k, 0) + 1
                add("group", "vote", name, reply)
                set_current("group", None)
            add("group", "system", "主持人", "进入主持人裁决")
            set_current("group", "主持人")
            verdict = gc.adjudicate(topic, [(m["who"], m["text"]) for m in
                                            STATE["channels"]["group"]["messages"]
                                            if m["kind"] in ("message", "user")], STATE["votes"])
            add("group", "verdict", "主持人", verdict)
            set_current("group", None)
        with LOCK:
            STATE["status"] = "done"
            STATE["finishedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        add("group", "system", "主持人", "会议结束。可以直接在下面发消息继续，或点「开始新会议」")
        save_record("group", title="群聊：" + topic)
    except Exception as e:
        with LOCK:
            STATE["status"] = "error"
            STATE["error"] = str(e)
        add("group", "system", "主持人", f"会议出错：{e}")
    finally:
        THREAD = None


def run_followup(text, members):
    global THREAD
    try:
        with LOCK:
            STATE["status"] = "running"
        add("group", "system", "主持人", "收到你的消息，成员们开始回应…")
        for name in members:
            set_current("group", name)
            prompt = (f"这是多智能体群聊的继续讨论。\n主题：{STATE['topic'] or '（自由讨论）'}\n\n"
                      f"之前的群聊记录：\n{history('group')}\n\n"
                      f"主席刚刚发来新消息：「{text}」\n"
                      f"请以你的身份直接回应这条消息：{gc.SYSTEM.get(name, '')}")
            reply = gc.CALLERS[name](prompt)
            add("group", "message", name, reply)
            set_current("group", None)
        with LOCK:
            STATE["status"] = "done"
            STATE["finishedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        add("group", "system", "主持人", "本轮回应结束。可以继续发消息。")
        save_record("group", title="群聊：" + (STATE['topic'] or "自由讨论"))
    except Exception as e:
        with LOCK:
            STATE["status"] = "error"
            STATE["error"] = str(e)
        add("group", "system", "主持人", f"回应出错：{e}")
    finally:
        THREAD = None


# ---------------- private chat ----------------

def run_dm(member, text):
    global THREAD
    try:
        set_current(member, member)
        hist = history(member)
        prompt = (
            f"这是你与用户的私聊。你是「{member}」，拥有这台电脑的完全访问权限"
            f"（可以运行命令、读写任意文件、启动程序）。\n"
            f"私聊历史：\n{hist or '（这是第一条消息）'}\n\n"
            f"用户说：{text}\n\n"
            f"请直接回应，需要动手时直接调用工具完成并汇报结果：{gc.SYSTEM.get(member, '')}"
        )
        reply = gc.CALLERS[member](prompt)
        add(member, "message", member, reply)
        save_record(member, rolling=True, title="私信 " + member)
    except Exception as e:
        add(member, "system", "系统", f"调用出错：{e}")
    finally:
        set_current(member, None)


HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>多模型群聊 + 私信指挥台</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{--bg:#0f1115;--card:#171a21;--line:#262b35;--txt:#e6e9ef;--dim:#8b93a7}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
 header{position:sticky;top:0;background:#12151b;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:9}
 header h1{font-size:15px;margin:0;font-weight:600}
 .badge{padding:2px 8px;border-radius:10px;font-size:12px;background:#222a36;color:#9fd3ff}
 .badge.running{background:#1d3a2a;color:#7ef0aa}
 .badge.done{background:#20303f;color:#9fd3ff}
 .badge.error{background:#3a1d1d;color:#ff9f9f}
 .tabs{display:flex;gap:8px;padding:10px 16px 0;flex-wrap:wrap}
 .tab{padding:7px 14px;border:1px solid var(--line);border-radius:8px 8px 0 0;background:#12151b;cursor:pointer;color:var(--dim)}
 .tab.active{background:var(--card);color:var(--txt);border-bottom-color:var(--card)}
 .tab .live{color:#7ef0aa;font-size:11px;margin-left:4px}
 .main{display:flex;gap:16px;padding:16px;align-items:flex-start}
 #feed{flex:1;min-width:0}
 .msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:10px}
 .msg .who{font-weight:600;margin-bottom:4px;display:flex;gap:8px;align-items:center}
 .who .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
 .who .t{color:var(--dim);font-weight:400;font-size:12px;margin-left:auto}
 .msg.codex .dot{background:#f0a35e}.msg.claude .dot{background:#c58ef0}
 .msg.harness .dot{background:#5ec8f0}.msg.workbuddy .dot{background:#7ef0aa}
 .msg.user{background:#1b2230;border-color:#2f4560}
 .msg.verdict{background:#14281f;border-color:#2b5a3f}
 .msg.vote{background:#1a1c2a;border-color:#3a3550}
 .msg.system{background:transparent;border:1px dashed var(--line);color:var(--dim);font-size:13px}
 aside{width:300px;position:sticky;top:78px}
 .panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
 .panel h3{margin:0 0 8px;font-size:13px;color:var(--dim);font-weight:600}
 input,select,button,textarea{font:inherit;color:var(--txt);background:#0e1117;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
 button{background:#1f6feb;border:none;cursor:pointer;color:#fff}
 button:disabled{opacity:.5;cursor:default}
 #cmd{width:100%;min-height:64px;resize:vertical}
 .kv{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;margin:3px 0}
 .empty{color:var(--dim);text-align:center;padding:60px 0}
 .speaking{color:#7ef0aa;font-size:12px}
 .hint{color:var(--dim);font-size:12px}
</style></head>
<body>
<header>
  <h1>🧭 多模型群聊 + 私信指挥台</h1>
  <span id="status" class="badge">idle</span>
  <span id="speaking" class="speaking"></span>
  <span class="hint">/codex /claude /harness 私信 · /group 群聊</span>
</header>
<div class="tabs" id="tabs"></div>
<div class="main">
  <div id="feed"></div>
  <aside>
    <div class="panel" id="startPanel">
      <h3>群聊：开会</h3>
      <div style="display:grid;gap:8px">
        <input id="topic" placeholder="会议主题（越具体越好）">
        <div style="display:flex;gap:8px">
          <select id="rounds"><option>1</option><option>2</option></select>
          <select id="members">
            <option value="codex,claude,harness">codex + claude + harness</option>
            <option value="codex,claude">codex + claude</option>
            <option value="codex,harness">codex + harness</option>
            <option value="claude,harness">claude + harness</option>
          </select>
        </div>
        <label class="hint"><input type="checkbox" id="vote" checked> 加投票 + 主持人裁决</label>
        <button id="startBtn" onclick="startMeeting()">开始会议</button>
      </div>
    </div>
    <div class="panel">
      <h3 id="composeTitle">发消息</h3>
      <textarea id="cmd" placeholder="在这里打字，Enter 发送（Shift+Enter 换行）"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button onclick="sendMsg()" style="flex:1">发送</button>
      </div>
      <div class="kv"><span id="cmdHint">就绪</span><span id="cmdCount">0</span></div>
    </div>
    <div class="panel" id="groupState">
      <h3>群聊状态</h3>
      <div class="kv"><span>主题</span><span id="sTopic">-</span></div>
      <div class="kv"><span>轮数</span><span id="sRounds">-</span></div>
      <div class="kv"><span>投票</span><span id="sVote">-</span></div>
      <div class="kv"><span>统计</span><span id="sTally">-</span></div>
      <div class="kv"><span>开始</span><span id="sStart">-</span></div>
    </div>
  </aside>
</div>
<script>
let channel = 'group', cmds = 0, lastCounts = {};
const TABS = [['group','👥 群聊'],['codex','🟠 codex'],['claude','🟣 claude'],['harness','🔵 harness'],['workbuddy','🟢 workbuddy']];
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function renderTabs(st){
  const t = document.getElementById('tabs');
  t.innerHTML = TABS.map(([k,label]) => {
    const ch = st.channels[k] || {messages:[], current:null};
    const live = ch.current ? '<span class="live">●</span>' : '';
    return `<div class="tab ${channel===k?'active':''}" onclick="switchTab('${k}')">${label}${live}</div>`;
  }).join('');
}
function switchTab(k){ channel = k; document.getElementById('startPanel').style.display = (k==='group'?'block':'none'); poll(); }
function cssClass(m){
  if (m.kind !== 'message') return m.kind;
  if (m.who.includes('codex')) return 'codex';
  if (m.who.includes('claude')) return 'claude';
  if (m.who.includes('harness')) return 'harness';
  if (m.who.includes('workbuddy')) return 'workbuddy';
  return '';
}
function render(st){
  document.getElementById('status').textContent = st.status;
  document.getElementById('status').className = 'badge ' + st.status;
  const ch = st.channels[channel] || {messages:[], current:null};
  document.getElementById('speaking').textContent = ch.current ? ('正在输入：' + ch.current) : '';
  document.getElementById('cmdHint').textContent = channel === 'group' ? '群聊模式：大家都会看到' : ('私信：只有 ' + channel + ' 会看到');
  document.getElementById('composeTitle').textContent = channel === 'group' ? '发到群聊' : ('私信 ' + channel);
  document.getElementById('sTopic').textContent = st.topic || '-';
  document.getElementById('sRounds').textContent = st.rounds || '-';
  document.getElementById('sVote').textContent = st.vote ? '是' : '否';
  document.getElementById('sTally').textContent = Object.keys(st.votes||{}).length ? Object.entries(st.votes).map(([k,v])=>k+': '+v).join('、') : '-';
  document.getElementById('sStart').textContent = st.startedAt || '-';
  document.getElementById('startBtn').disabled = st.status === 'running';
  renderTabs(st);
  if ((lastCounts[channel] || 0) === ch.messages.length) return;
  lastCounts[channel] = ch.messages.length;
  const feed = document.getElementById('feed');
  if (!ch.messages.length) { feed.innerHTML = '<div class="empty">还没有消息。' +
      (channel==='group' ? '右侧填主题后点「开始会议」，或直接在下面发消息。' : '在下面发消息开始私信。') + '</div>'; return; }
  feed.innerHTML = ch.messages.map(m => {
    const cls = cssClass(m);
    const label = m.kind === 'user' ? '你' : m.who;
    return `<div class="msg ${cls}"><div class="who"><span class="dot"></span>${esc(label)}<span class="t">${esc(m.ts||'')}</span></div><div>${esc(m.text).replace(/\n/g,'<br>')}</div></div>`;
  }).join('');
  feed.lastElementChild && feed.lastElementChild.scrollIntoView({block:'end'});
}
async function poll(){
  try{ const r = await fetch('/state?t=' + Date.now()); render(await r.json()); }catch(e){}
  setTimeout(poll, 1200);
}
async function startMeeting(){
  const topic = document.getElementById('topic').value.trim();
  if(!topic){ alert('先填会议主题'); return; }
  const body = {topic, rounds:+document.getElementById('rounds').value,
    members: document.getElementById('members').value,
    vote: document.getElementById('vote').checked};
  await fetch('/start',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  channel = 'group'; switchTab('group');
}
async function sendMsg(){
  const el = document.getElementById('cmd');
  const t = el.value.trim();
  if(!t) return;
  const hint = document.getElementById('cmdHint');
  hint.textContent = '发送中…';
  try{
    const r = await fetch('/say',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({channel, text:t})});
    const j = await r.json();
    if(!j.ok) throw new Error(j.error||'发送失败');
    el.value=''; cmds++; document.getElementById('cmdCount').textContent = cmds;
    hint.textContent = j.mode === 'queued' ? '已送达，下一位发言时生效' : '已送达，等待回应…';
    poll();
  }catch(e){ hint.textContent = '发送失败：' + e.message; alert('发送失败：' + e.message); }
}
document.getElementById('cmd').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/state"):
            with LOCK:
                self._send(200, json.dumps(STATE, ensure_ascii=False))
        else:
            self._send(200, HTML, "text/html")

    def do_POST(self):
        global THREAD
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if self.path.startswith("/say"):
            text = (body.get("text") or "").strip()
            ch = (body.get("channel") or "group").strip()
            if not text:
                self._send(400, json.dumps({"ok": False, "error": "empty"})); return
            if ch == "group":
                with LOCK:
                    running = STATE["status"] == "running"
                    members = [m for m in (STATE["members"] or ["codex", "claude", "harness"])
                               if m in gc.CALLERS]
                if running:
                    with LOCK: QUEUE.append(text)
                    add("group", "user", "你（主席指令）", text)
                    self._send(200, json.dumps({"ok": True, "mode": "queued"}))
                else:
                    add("group", "user", "你", text)
                    THREAD = threading.Thread(target=run_followup, args=(text, members), daemon=True)
                    THREAD.start()
                    self._send(200, json.dumps({"ok": True, "mode": "followup"}))
                return
            if ch in MEMBERS and ch in gc.CALLERS:
                add(ch, "user", "你", text)
                THREAD = threading.Thread(target=run_dm, args=(ch, text), daemon=True)
                THREAD.start()
                self._send(200, json.dumps({"ok": True, "mode": "dm"}))
                return
            self._send(400, json.dumps({"ok": False, "error": "unknown channel"})); return
        if self.path.startswith("/start"):
            if STATE["status"] == "running":
                self._send(409, json.dumps({"ok": False, "error": "meeting already running"})); return
            topic = (body.get("topic") or "").strip()
            if not topic:
                self._send(400, json.dumps({"ok": False, "error": "no topic"})); return
            members = [m.strip() for m in (body.get("members") or "codex,claude,harness").split(",")
                       if m.strip() in gc.CALLERS]
            rounds = max(1, min(3, int(body.get("rounds") or 1)))
            vote = bool(body.get("vote"))
            with LOCK:
                STATE["members"] = members
            THREAD = threading.Thread(target=run_meeting, args=(topic, rounds, members, vote), daemon=True)
            THREAD.start()
            self._send(200, json.dumps({"ok": True}))
            return
        self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"panel: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
