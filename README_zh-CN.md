# ai-roundtable

[English](./README.md) | [繁體中文](./README_zh-TW.md) | **简体中文**

![Python](https://img.shields.io/badge/PYTHON-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/PLATFORM-WINDOWS-0078D6?logo=windows&logoColor=white)
![UI](https://img.shields.io/badge/UI-LOCALHOST-10a37f)
![Status](https://img.shields.io/badge/STATUS-ACTIVE%20LOCAL%20TOOL-orange)

> 一个 localhost 多 AI 圆桌工具，让多个 CLI 型 AI 席位查阅同一个项目、读取同一份逐字稿，并在同一个讨论室里回应。

这是一个 Windows 优先的小型本机工具，适合做本地 review、设计讨论与第二意见工作流。App 会把当前会议保存成 JSONL 逐字稿，并同步镜像成 Markdown，让每个参与席位在回答前都能读到同一份对话状态。

---

## 开发状态

本项目是持续迭代中的个人本机工具。核心工作流已可使用，但 CLI 集成依赖主机上已安装的工具；目前不是封装好的服务，也尚未针对不受信任的网络访问做安全强化。

---

## 概念

`ai-roundtable` 把每个模型供应来源视为同一张桌上的具名席位：

| 席位 | 后端 | 典型用途 |
|---|---|---|
| Codex | Codex CLI | 具 repo 上下文的实现与 review 意见 |
| DS | 使用 DeepSeek `CODEX_HOME` 的 Codex CLI | 通过 Moon Bridge 取得 DeepSeek 后端第二意见 |
| agy | agy CLI | 依本机 agy 设置提供 Gemini / Claude / GPT-OSS 模型席位 |
| Claude | Claude desktop app 内置的 Claude Code CLI | Claude 系列 reviewer 席位 |

逐字稿就是共享上下文。席位被调用时，会被要求读取 `data/roundtable.md`、只读查阅设置的项目文件夹，并以繁体中文回应且不得编辑文件。

---

## 功能

- **单一讨论室逐字稿**：消息保存于 `data/transcript.jsonl`，并镜像到 `data/roundtable.md`。
- **模型席位**：Codex、通过 Codex 的 DeepSeek、agy、Claude 席位都可在 UI 中启用或停用。
- **各席位模型选择器**：模型选择会持久化到 `data/settings.json`。
- **消息编号**：UI 气泡显示 `[n]` 编号，对应 Markdown 逐字稿章节。
- **单席取消**：可取消特定正在执行的 AI 调用，不必停止整个 server。
- **共识讨论模式**：第一轮并行收集独立意见；后续轮次 round-robin，直到所有有回应席位标记共识或达到轮次上限。
- **会议归档**：开新会议时会归档当前逐字稿与 Markdown 镜像；启动时可针对同一项目文件夹恢复或重新命名已归档会议。
- **项目目标设置**：参与者会查阅启动时选择或通过 `AI_ROUNDTABLE_PROJECT_DIR` 指定的项目文件夹。
- **可选 Tailscale 绑定**：若检测到 Tailscale，server 也会尝试绑定该机器的 tailnet IPv4 地址。

---

## 技术栈

- **Runtime**：Python 3.10+
- **Server**：Python 标准库 `ThreadingHTTPServer`
- **Frontend**：单文件 HTML / CSS / JavaScript
- **Process model**：每次 AI 席位调用对应一个 subprocess，并追踪以支持取消
- **Persistence**：`data/` 下的本机 JSON / JSONL / Markdown 文件
- **目标平台**：Windows 本机桌面工作流

App 本身不需要任何第三方 Python 包。

---

## Quick Start

**前置要求**：Windows 10 / 11 与 Python 3.10+。

### Setup

```bat
py -3.10 -m venv .venv
start.cmd
```

打开：

```text
http://127.0.0.1:8787/
```

### 可选 AI 后端

| 后端 | 预期本机路径 / 设置 |
|---|---|
| Codex | `%APPDATA%\npm\codex.cmd` |
| agy | `%LOCALAPPDATA%\agy\bin\agy.exe` |
| Claude | 安装 Claude desktop app，并具有内置 Claude Code CLI |
| DeepSeek | Moon Bridge 加上 DeepSeek Codex profile |

当 `MOON_BRIDGE_EXE` 与 `MOON_BRIDGE_CONFIG` 能解析到既有文件时，`start.cmd` 会尝试启动 Moon Bridge。

---

## 设置

环境变量：

| 变量 | 用途 |
|---|---|
| `AI_ROUNDTABLE_PROJECT_DIR` | 参与者可查阅的项目文件夹 |
| `AI_ROUNDTABLE_DS_CODEX_HOME` | DeepSeek 席位使用的 `CODEX_HOME` |
| `MOON_BRIDGE_EXE` | `moonbridge.exe` 路径 |
| `MOON_BRIDGE_CONFIG` | Moon Bridge `config.yml` 路径 |
| `AI_ROUNDTABLE_NO_BROWSER=1` | 启动时不要自动打开浏览器 |

本机 runtime 文件会写入 `data/`，此文件夹刻意由 git 忽略。

---

## 当前进度

| 范畴 | 状态 |
|---|---|
| Localhost chat UI | 完成 |
| 逐字稿 JSONL + Markdown 镜像 | 完成 |
| Codex / DS / agy / Claude adapters | 完成 |
| 各席位模型选择 | 完成 |
| 会议归档 / 恢复 / 重新命名 | 完成 |
| 单席取消 | 完成 |
| 共识讨论模式 | 完成 |
| UI 逐字稿编号对应 Markdown 章节 | 完成 |
| 自动化测试套件 | 尚未加入 |
| 网络安全强化 / 认证 | 尚未纳入范围 |

---

## 目录结构

```text
.
├── server.py       # localhost server、状态、会议归档、AI subprocess adapters
├── index.html      # 单页 chat UI
├── start.cmd       # Windows launcher；可选启动 Moon Bridge，然后启动 server.py
├── stop.cmd        # 停止 listen 在 port 8787 的 process
├── data/           # 本机设置、当前逐字稿、归档会议；git ignored
├── .agents/        # 本机 agent/tooling metadata
├── .codex/         # 此 repo 的本机 Codex config
└── .claude/        # 本机 Claude tooling config；不要 commit
```

---

## 验证

基本语法检查：

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py
```

手动 smoke test：

1. 执行 `start.cmd`。
2. 打开 `http://127.0.0.1:8787/`。
3. 选择一个或多个席位。
4. 送出 prompt，确认回复出现在 UI。
5. 确认 `data/transcript.jsonl` 与 `data/roundtable.md` 有更新。
6. 开新会议，确认前一份逐字稿已归档。

---

## 安全备注

本工具设计给受信任的本机环境使用。它没有登录、CSRF 保护或授权层。若 Tailscale 绑定启用，任何可连到该 tailnet 地址的人都可能读取会议状态并触发本机 AI subprocess 调用。

Prompt 会要求 AI 席位不得创建、修改或删除文件。Codex 与 DeepSeek 会以 Codex read-only sandbox 启动；其他本机 CLI 工具仍取决于自身权限行为与本机设置。

---

## 授权

MIT License。见 [`LICENSE`](./LICENSE)。