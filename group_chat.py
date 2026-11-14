#!/usr/bin/env python3
"""多模型群聊指挥台 v2 (multi-agent group chat orchestrator).

Members (each one round = one API call):
  codex      : Codex CLI
  claude     : Claude Code CLI
  harness    : DSH-style agent, direct DeepSeek chat completions (host / summarizer)
  workbuddy  : WorkBuddy bundled CodeBuddy CLI (requires CODEBUDDY_API_KEY env)

Modes:
  1) discussion: every member sees topic + transcript, replies once per round.
  2) vote (--vote): each member then votes for one proposal (by member name) with a reason;
     harness adjudicates the final decision and an action list.

The transcript is written to markdown.

Examples:
  python group_chat.py --topic "..." --rounds 1                       # quick discussion
  python group_chat.py --topic "..." --rounds 2 --vote                # discussion + vote + ruling
  python group_chat.py --topic "..." --members codex,claude,workbuddy --vote
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

def _exe(env_name, *candidates, default=None):
    """Resolve an executable path: env var first, then PATH, then known locations."""
    val = os.environ.get(env_name)
    if val:
        return val
    for cand in candidates:
        cand = os.path.expanduser(cand)
        if cand and os.path.exists(cand):
            return cand
    found = shutil.which(default or env_name.replace("_BIN", "").lower())
    return found or (default or "")


CODEX = _exe("CODEX_BIN",
             r"~/.codex/bin/codex.exe",
             r"~/AppData/Local/Programs/codex/bin/codex.exe",
             default="codex")
CLAUDE = _exe("CLAUDE_BIN",
              r"~/.local/bin/claude.exe",
              r"~/.local/bin/claude",
              default="claude")
DSH_EXE = _exe("DSH_BIN",
               r"~/AppData/Local/Programs/DSH NEXT/DSH NEXT.exe",
               default="dsh")
WB_CLI = os.environ.get(
    "WORKBUDDY_CLI",
    r"~/AppData/Local/Programs/WorkBuddy/resources/app.asar/cli/dist/codebuddy-lite-wb.mjs")
CODEX_CONFIG = os.environ.get("CODEX_CONFIG", os.path.expanduser("~/.codex/config.toml"))
WORKDIR = os.environ.get("GROUP_CHAT_WORKDIR") or os.getcwd()
# extra directories the claude member may touch; comma separated in CLAUDE_EXTRA_DIRS
CLAUDE_DIRS = [d.strip() for d in
               (os.environ.get("CLAUDE_EXTRA_DIRS") or os.path.expanduser("~")).split(",")
               if d.strip()]


SYSTEM = {
    "codex": "你是 Codex，一名严谨的软件工程智能体。用简洁中文发言，给出可执行、具体的方案；不超过 180 字。",
    "claude": "你是 Claude，一名善于分析与表达的技术顾问。用简洁中文发言，给出结构清晰、考虑周全的方案；不超过 180 字。",
    "harness": "你是 DSH（DeepSeek Harness）智能体，负责统筹与落地。用简洁中文归纳共识并按优先级给出可执行步骤；不超过 180 字。",
    "workbuddy": "你是 WorkBuddy，腾讯的办公与开发协作智能体。用简洁中文发言，给出务实、可落地的建议；不超过 180 字。",
}


def deepseek_key():
    """DeepSeek API key: DEEPSEEK_API_KEY env first, then the codex config bearer token."""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key.strip()
    try:
        text = open(CODEX_CONFIG, "r", encoding="utf-8", errors="replace").read()
        m = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except Exception:
        return None


def run_cmd(cmd, timeout, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=WORKDIR, encoding="utf-8", errors="replace", env=env)
        out = (p.stdout or "").strip()
        if not out and (p.stderr or "").strip():
            out = "[stderr] " + p.stderr.strip()[:500]
        return out or "[无输出]"
    except subprocess.TimeoutExpired:
        return f"[超时 {timeout}s]"
    except Exception as e:
        return f"[调用失败: {e}]"


def call_codex(prompt, timeout=300):
    # full access: no sandbox, no approval prompts -> can run commands / edit files
    return run_cmd([CODEX, "exec", "--skip-git-repo-check",
                    "-s", "danger-full-access",
                    "-c", 'approval_policy="never"', prompt], timeout)


def call_claude(prompt, timeout=240):
    # full access: bypass all permission checks; extra dirs come from CLAUDE_EXTRA_DIRS
    cmd = [CLAUDE, "-p", prompt, "--output-format", "text",
           "--dangerously-skip-permissions"]
    for d in CLAUDE_DIRS:
        if os.path.isdir(d):
            cmd += ["--add-dir", d]
    return run_cmd(cmd, timeout)


def call_harness(prompt, timeout=120):
    key = deepseek_key()
    if not key:
        return "[harness] 未找到 DeepSeek API key"
    body = json.dumps({
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": SYSTEM["harness"]},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 800,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[harness 调用失败: {e}]"


# ---- harness with local tools (full computer access) ----

def _tool_run_pwsh(command, timeout=120):
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=WORKDIR, encoding="utf-8", errors="replace")
        out = ((p.stdout or "") + ("\n[stderr]\n" + p.stderr if (p.stderr or "").strip() else "")).strip()
        return out[:6000] or "[无输出]"
    except subprocess.TimeoutExpired:
        return f"[超时 {timeout}s]"
    except Exception as e:
        return f"[执行失败: {e}]"


def _tool_read_file(path):
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
        return text[:8000] + ("\n…(截断)" if len(text) > 8000 else "")
    except Exception as e:
        return f"[读取失败: {e}]"


def _tool_write_file(path, content):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return f"[已写入 {len(content)} 字符: {path}]"
    except Exception as e:
        return f"[写入失败: {e}]"


def _tool_list_dir(path):
    try:
        names = sorted(os.listdir(path))
        return "\n".join(names[:200]) or "[空目录]"
    except Exception as e:
        return f"[列目录失败: {e}]"


HARNESS_TOOLS = [
    {"type": "function", "function": {
        "name": "run_pwsh",
        "description": "在本机 Windows 上执行 PowerShell 命令（完全访问：可读写任意文件、启动程序、管理系统）。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "要执行的 PowerShell 命令"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取本机任意文本文件的内容。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "写入（创建/覆盖）本机任意文本文件。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "列出本机目录内容。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
]


def _harness_tool_call(name, args):
    try:
        a = json.loads(args or "{}")
    except Exception:
        return "[参数解析失败]"
    if name == "run_pwsh":
        return _tool_run_pwsh(a.get("command", ""))
    if name == "read_file":
        return _tool_read_file(a.get("path", ""))
    if name == "write_file":
        return _tool_write_file(a.get("path", ""), a.get("content", ""))
    if name == "list_dir":
        return _tool_list_dir(a.get("path", ""))
    return "[未知工具]"


def call_harness_agent(prompt, timeout=240):
    """harness 成员：带本机工具的智能体循环（可执行命令、读写文件）。"""
    key = deepseek_key()
    if not key:
        return "[harness] 未找到 DeepSeek API key"
    sys_prompt = (SYSTEM["harness"] +
                  "\n你可以调用本机工具（run_pwsh/read_file/write_file/list_dir），"
                  "需要检查或修改电脑时直接调用，不要只描述步骤。完成后用中文简要汇报你做了什么。")
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt}]
    last = ""
    for _ in range(8):
        body = json.dumps({
            "model": "deepseek-flash",
            "messages": messages,
            "tools": HARNESS_TOOLS,
            "max_tokens": 1200,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return (last + f"\n[harness 调用失败: {e}]").strip()
        msg = data["choices"][0]["message"]
        last = (msg.get("content") or "").strip()
        calls = msg.get("tool_calls") or []
        if not calls:
            return last or "[无输出]"
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            result = _harness_tool_call(fn.get("name", ""), fn.get("arguments", ""))
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": result[:4000]})
    return (last + "\n[工具调用轮数达上限]").strip()


def call_workbuddy(prompt, timeout=240):
    key = os.environ.get("CODEBUDDY_API_KEY")
    if not key:
        return "[workbuddy 未配置] 需要 CODEBUDDY_API_KEY 环境变量（WorkBuddy/CodeBuddy 的 API Key）；当前已跳过该成员。"
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["CODEBUDDY_API_KEY"] = key
    env["CODEBUDDY_SKIP_GIT_BASH_CHECK"] = "1"
    return run_cmd([DSH_EXE, WB_CLI, "-p", prompt, "--output-format", "text"], timeout, env=env)


CALLERS = {"codex": call_codex, "claude": call_claude, "harness": call_harness_agent,
           "workbuddy": call_workbuddy}


def transcript_text(transcript):
    return "\n\n".join(f"【{who}】{text}" for who, text in transcript) or "（还没有人发言）"


def vote_phase(topic, transcript, members):
    """Each member votes for one proposal (by member name); returns (votes, details)."""
    hist = transcript_text(transcript)
    names = [n for n in members if n != "harness"] or members
    options = "、".join(names)
    vote_prompt = (
        f"这是多智能体群聊的投票环节。\n主题：{topic}\n\n讨论记录：\n{hist}\n\n"
        f"请只从这些方案提出者中选一个你支持的：{options}。\n"
        f"严格按此格式回复一行：投票=<名字>；理由=<一句话不超过40字>"
    )
    votes, details = {}, []
    for name in members:
        reply = CALLERS[name](vote_prompt)
        m = re.search(r"投票\s*[=:：]\s*(codex|claude|harness|workbuddy)", reply, re.I)
        who = m.group(1).lower() if m else None
        if who:
            votes[who] = votes.get(who, 0) + 1
        details.append((name, reply))
    return votes, details


def adjudicate(topic, transcript, votes):
    hist = transcript_text(transcript)
    tally = "、".join(f"{k} {v} 票" for k, v in sorted(votes.items())) or "（无有效投票）"
    prompt = (
        f"你是这场多智能体会议的主持人。\n主题：{topic}\n\n讨论记录：\n{hist}\n\n"
        f"投票统计：{tally}\n\n"
        f"请做最终裁决：1) 一句话结论；2) 采纳哪些要点；3) 按优先级列出 3-5 条可执行动作。"
        f"不超过 250 字。"
    )
    return call_harness(prompt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--members", default="codex,claude,harness")
    ap.add_argument("--vote", action="store_true", help="add vote + host adjudication phase")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    members = [m.strip() for m in args.members.split(",") if m.strip() in CALLERS]
    if not members:
        sys.exit("no valid members; choose from " + ",".join(CALLERS))
    transcript, votes, verdict = [], {}, None

    for rnd in range(1, args.rounds + 1):
        for name in members:
            prompt = (f"这是多智能体群聊的第 {rnd} 轮。\n主题：{args.topic}\n\n"
                      f"目前的群聊记录：\n{transcript_text(transcript)}\n\n"
                      f"请以你的身份发言：{SYSTEM.get(name, '')}")
            started = time.time()
            reply = CALLERS[name](prompt)
            transcript.append((name, reply))
            print(f"\n===== 第{rnd}轮 · {name}（{int(time.time()-started)}s）=====\n{reply}")
            sys.stdout.flush()

    if args.vote:
        print("\n===== 投票环节 =====")
        votes, details = vote_phase(args.topic, transcript, members)
        for who, text in details:
            transcript.append((f"{who}（投票）", text))
            print(f"\n--- {who} 投票 ---\n{text}")
        print("\n===== 主持人裁决 =====")
        verdict = adjudicate(args.topic, transcript, votes)
        transcript.append(("主持人裁决", verdict))
        print(f"\n{verdict}")

    out = args.out or os.path.join(WORKDIR, "chat-" + time.strftime("%Y%m%d-%H%M%S") + ".md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    lines = ["# 多模型群聊记录", "", f"- 主题：{args.topic}",
             f"- 成员：{', '.join(members)}", f"- 轮数：{args.rounds}",
             f"- 投票：{'是' if args.vote else '否'}",
             f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for who, text in transcript:
        lines += [f"## {who}", "", text, ""]
    if args.vote:
        lines += ["## 投票统计", "", "、".join(f"{k}: {v}" for k, v in sorted(votes.items())) or "（无）", ""]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\n[transcript saved] {out}")


if __name__ == "__main__":
    main()
