# 多模型群聊指挥台 · Multi-Agent Group Chat Panel

把本机上的 **Codex CLI**、**Claude Code CLI**、**DeepSeek 直连智能体**和 **WorkBuddy** 放进同一间「群聊」：
围绕一个主题多轮发言、互相投票、由主持人裁决出结论；也可以随时私信某一个成员。

另附一个常驻的 **引擎桥（engine bridge）**，把 `codex` / `claude` 这两个真实 CLI 包装成 HTTP 接口，
带会话续接，方便任何宿主（例如 DeepSeek Harness 的会话）把「真人」接进来。

全部本地运行，只监听 `127.0.0.1`。

## 功能

- **群聊会议**：填主题 → 选成员 / 轮数 / 是否投票 → 各成员依次发言，记录自动落盘为 Markdown。
- **投票裁决**：成员互相投票，`harness` 主持人给出结论 + 可执行动作清单。
- **私信**：按成员标签单独聊天，消息只有该成员可见。
- **真实 CLI 引擎桥**：`codex exec --json` 与 `claude -p --output-format json` 的接口化封装，
  每个引擎各自保留 CLI 会话（`resume`），可跨轮次记忆。
- **零依赖**：只用 Python 标准库（3.7+），不需要 `pip install`。

## 架构

```
浏览器 ──HTTP──> panel_server.py (:8765) ──> group_chat.py ──> codex.exe / claude.exe / DeepSeek API
                        │
                        └─ records/*.md（会议记录）

任意宿主 ──HTTP──> engine_server.py (:8899) ──> codex.exe / claude.exe（带 resume 会话）
```

| 文件 | 作用 |
|---|---|
| `panel_server.py` | 面板 + 群聊/私信 HTTP 服务（默认 8765） |
| `group_chat.py` | 会议引擎：成员调用、投票、主持人裁决 |
| `engine_server.py` | 常驻引擎桥：把 codex/claude CLI 包装成 HTTP（默认 8899） |
| `ask_engine.py` | 命令行向某个引擎提问 / 清空其记忆 |
| `start-panel.cmd` | Windows 一键启动：面板 + 引擎桥 + 打开页面 |
| `start-engines.cmd` | Windows 一键启动：只起引擎桥 |

## 快速开始

前置条件：

1. Python 3.7+（`python` 或 `py` 在 PATH 里）
2. 至少一个可用引擎：
   - `codex` CLI（`CODEX_BIN` 可指定路径）
   - `claude` CLI（`CLAUDE_BIN` 可指定路径）
   - 一个 DeepSeek API Key（`harness` 成员走 `https://api.deepseek.com`）

配置（PowerShell）：

```powershell
setx DEEPSEEK_API_KEY "sk-..."                 # harness 成员用
setx CODEX_BIN  "C:\path\to\codex.exe"          # 可选，默认从 PATH 找
setx CLAUDE_BIN "C:\path\to\claude.exe"         # 可选，默认从 PATH 找
```

启动：

```powershell
# Windows：双击 start-panel.cmd，或在终端
python panel_server.py --port 8765
# 浏览器打开 http://127.0.0.1:8765/
```

引擎桥（可选，让 CLI 引擎以 HTTP 方式接入别的宿主）：

```powershell
python engine_server.py --port 8899
python ask_engine.py codex "你好"      # 打印 codex 的回答
python ask_engine.py claude "你好"
python ask_engine.py reset codex       # 清空 codex 的 CLI 会话记忆
```

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `DEEPSEEK_API_KEY` | harness 成员的 API Key | 回退读 `CODEX_CONFIG` 里的 bearer token |
| `CODEX_BIN` / `CLAUDE_BIN` | CLI 可执行文件路径 | 从 PATH 找 |
| `DSH_BIN` / `WORKBUDDY_CLI` | WorkBuddy 成员用的宿主与 CLI | 常见安装位置 |
| `CODEBUDDY_API_KEY` | 启用 WorkBuddy 成员 | 未设置则该成员会被跳过 |
| `CODEX_CONFIG` | codex 配置（用于取 bearer token） | `~/.codex/config.toml` |
| `GROUP_CHAT_WORKDIR` | 成员执行命令的工作目录 | 当前目录 |
| `ENGINE_SERVER_URL` | `ask_engine.py` 要连的引擎桥 | `http://127.0.0.1:8899` |
| `CLAUDE_EXTRA_DIRS` | 允许 claude 额外访问的目录，逗号分隔 | 用户主目录 |

## HTTP 接口

面板（`panel_server.py`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/state` | 当前状态与各频道消息（JSON） |
| `POST` | `/say` | `{"text": "...", "channel": "group"}` 群聊追问；`channel` 换成成员名即私信 |
| `POST` | `/start` | `{"topic": "...", "members": "codex,claude,harness", "rounds": 1, "vote": true}` 开一场会议 |

引擎桥（`engine_server.py`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查，返回已用会话 id |
| `POST` | `/ask` | `{"engine": "codex", "text": "..."}` → `{"reply": "..."}` |
| `POST` | `/reset` | `{"engine": "codex"}` 或 `{"engine": "all"}` 清空会话 |

## 安全说明

这些成员是以**完全访问权限**运行的：codex 用 `danger-full-access`，claude 用
`--dangerously-skip-permissions`，`harness` 成员自带 PowerShell / 读写文件工具。
也就是说，**模型可以真实执行命令、读写这台电脑上的文件**。

- 只在本机、自用场景下开；
- 不要把它暴露到公网（服务只绑 `127.0.0.1` 就是这个原因）；
- 需要收紧权限时，改 `group_chat.py` 里 `call_codex` / `call_claude` 的参数（如 `-s read-only`、去掉
  `--dangerously-skip-permissions`），或给 `harness` 的 `HARNESS_TOOLS` 做白名单。

## 已知限制

- 面向 Windows 编写（启动脚本是 `.cmd`，`harness` 成员调用 `powershell.exe`）；Linux/macOS 可去掉这两处直接用 Python 启动。
- `workbuddy` 成员依赖本机 WorkBuddy 安装与 `CODEBUDDY_API_KEY`，未配置时会自动跳过。
- 每次发言都会消耗对应的 API 额度。

## License

MIT © 2026 Sjn06
