#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向常驻的 engine_server 发送一句话，打印真实 CLI 引擎的回复。

用法：
    python ask_engine.py codex "你好"
    python ask_engine.py claude "你好"
    python ask_engine.py reset codex
"""

import json
import os
import sys
import urllib.request

URL = os.environ.get("ENGINE_SERVER_URL", "http://127.0.0.1:8899")


def post(path, payload, timeout=600):
    req = urllib.request.Request(
        URL + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if len(sys.argv) < 2:
        print("usage: ask_engine.py <codex|claude|reset> <text>")
        return 1
    engine = sys.argv[1].strip().lower()
    text = " ".join(sys.argv[2:]).strip()
    if engine == "reset":
        print(json.dumps(post("/reset", {"engine": text or "all"}), ensure_ascii=False))
        return 0
    if engine not in ("codex", "claude"):
        print("unknown engine: " + engine)
        return 1
    try:
        data = post("/ask", {"engine": engine, "text": text})
    except Exception as e:
        print("[调用 engine_server 失败: %s]" % e)
        return 1
    if not data.get("ok"):
        print("[引擎返回错误: %s]" % data.get("error"))
        return 1
    print(data["reply"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
