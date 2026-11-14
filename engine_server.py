#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 CLI 引擎接入服务（codex / claude）。

以非沙箱权限常驻运行，
对外提供 HTTP 接口：
    POST /ask   {"engine":"codex","text":"..."}  ->  {"ok":true,"reply":"...","session":"..."}
    GET  /health                                 ->  {"ok":true,...}
    POST /reset {"engine":"codex"}               ->  清除该引擎的会话

每个引擎维护自己的 CLI 会话（codex exec resume / claude --resume），
所以多轮对话有记忆。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")


def _exe(env_name, *candidates, default=None):
    val = os.environ.get(env_name)
    if val:
        return val
    for cand in candidates:
        cand = os.path.expanduser(cand)
        if cand and os.path.exists(cand):
            return cand
    return shutil.which(default or env_name.replace("_BIN", "").lower()) or (default or "")


CODEX = _exe("CODEX_BIN", r"~/.codex/bin/codex.exe",
             r"~/AppData/Local/Programs/codex/bin/codex.exe", default="codex")
CLAUDE = _exe("CLAUDE_BIN", r"~/.local/bin/claude.exe", r"~/.local/bin/claude",
              default="claude")
STATE_FILE = os.path.join(WS, ".cli_sessions.json")
LOG_FILE = os.path.join(WS, "engine_server.log")
# extra directories the claude engine may touch; comma separated in CLAUDE_EXTRA_DIRS
CLAUDE_DIRS = [d.strip() for d in
               (os.environ.get("CLAUDE_EXTRA_DIRS") or HOME).split(",") if d.strip()]

LOCK = threading.Lock()
SESSIONS = {}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_sessions():
    global SESSIONS
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            SESSIONS = json.load(fh)
    except Exception:
        SESSIONS = {}


def save_sessions():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(SESSIONS, fh, ensure_ascii=False, indent=1)
    except Exception as e:
        log("save sessions failed: %s" % e)


def _run(cmd, timeout):
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                       cwd=WS, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def codex_ask(text, timeout=300):
    sid = SESSIONS.get("codex")
    if sid:
        cmd = [CODEX, "exec", "resume", sid, "--json",
               "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", text]
    else:
        cmd = [CODEX, "exec", "--json", "--skip-git-repo-check",
               "-s", "danger-full-access", "-c", 'approval_policy="never"', text]
    rc, out, err = _run(cmd, timeout)

    thread_id, messages, errors = None, [], []
    for raw in out.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id") or thread_id
        elif t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(item["text"])
        elif t == "error":
            errors.append(str(ev.get("message") or ev))
        elif t == "turn.failed":
            errors.append(str(ev))
    if thread_id:
        SESSIONS["codex"] = thread_id
        save_sessions()
    reply = "\n".join(messages).strip()
    if not reply:
        reply = (err.strip() or out.strip() or "[codex 无输出]")[:2000]
    log("codex rc=%s sid=%s reply_len=%d" % (rc, thread_id, len(reply)))
    return reply


def claude_ask(text, timeout=300):
    sid = SESSIONS.get("claude")
    cmd = [CLAUDE, "-p", text, "--output-format", "json",
           "--dangerously-skip-permissions"]
    for d in CLAUDE_DIRS:
        if os.path.isdir(d):
            cmd += ["--add-dir", d]
    if sid:
        cmd += ["--resume", sid]
    rc, out, err = _run(cmd, timeout)
    reply, new_sid = "", None
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        reply = (data.get("result") or "").strip()
        new_sid = data.get("session_id")
    except Exception:
        reply = out.strip()
    if new_sid:
        SESSIONS["claude"] = new_sid
        save_sessions()
    if not reply:
        reply = (err.strip() or out.strip() or "[claude 无输出]")[:2000]
    log("claude rc=%s sid=%s reply_len=%d" % (rc, new_sid, len(reply)))
    return reply


ENGINES = {"codex": codex_ask, "claude": claude_ask}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "engines": list(ENGINES),
                             "sessions": SESSIONS})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if self.path.startswith("/ask"):
            engine = (body.get("engine") or "").strip().lower()
            text = (body.get("text") or "").strip()
            if engine not in ENGINES:
                self._send(400, {"ok": False, "error": "unknown engine"}); return
            if not text:
                self._send(400, {"ok": False, "error": "empty text"}); return
            try:
                with LOCK:
                    reply = ENGINES[engine](text)
                self._send(200, {"ok": True, "reply": reply,
                                 "session": SESSIONS.get(engine)})
            except subprocess.TimeoutExpired:
                self._send(504, {"ok": False, "error": "timeout"})
            except Exception as e:
                log("ask failed: %r" % e)
                self._send(500, {"ok": False, "error": str(e)})
            return
        if self.path.startswith("/reset"):
            engine = (body.get("engine") or "").strip().lower()
            if engine == "all":
                SESSIONS.clear()
            else:
                SESSIONS.pop(engine, None)
            save_sessions()
            self._send(200, {"ok": True, "sessions": SESSIONS})
            return
        self._send(404, {"ok": False})

    def log_message(self, *args):
        pass


def main():
    load_sessions()
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8899
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log("engine server up: http://127.0.0.1:%d  sessions=%s" % (port, SESSIONS))
    srv.serve_forever()


if __name__ == "__main__":
    main()
