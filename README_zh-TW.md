# ai-roundtable

[English](./README.md) | **繁體中文** | [简体中文](./README_zh-CN.md)

![Python](https://img.shields.io/badge/PYTHON-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/PLATFORM-WINDOWS-0078D6?logo=windows&logoColor=white)
![UI](https://img.shields.io/badge/UI-LOCALHOST-10a37f)
![Status](https://img.shields.io/badge/STATUS-ACTIVE%20LOCAL%20TOOL-orange)

> 一個 localhost 多 AI 圓桌工具，讓多個 CLI 型 AI 席位查閱同一個專案、讀取同一份逐字稿，並在同一個討論室裡回應。

這是一個 Windows 優先的小型本機工具，適合做本地 review、設計討論與第二意見工作流。App 會把目前會議保存成 JSONL 逐字稿，並同步鏡像成 Markdown，讓每個參與席位在回答前都能讀到同一份對話狀態。

---

## 開發狀態

本專案是持續迭代中的個人本機工具。核心工作流已可使用，但 CLI 整合仰賴主機上已安裝的工具；目前不是封裝好的服務，也尚未針對不受信任的網路存取做安全強化。

---

## 概念

`ai-roundtable` 把每個模型供應來源視為同一張桌上的具名席位：

| 席位 | 後端 | 典型用途 |
|---|---|---|
| Codex | Codex CLI | 具 repo 脈絡的實作與 review 意見 |
| DS | 使用 DeepSeek `CODEX_HOME` 的 Codex CLI | 透過 Moon Bridge 取得 DeepSeek 後端第二意見 |
| agy | agy CLI | 依本機 agy 設定提供 Gemini / Claude / GPT-OSS 模型席位 |
| Claude | Claude desktop app 內建的 Claude Code CLI | Claude 系列 reviewer 席位 |

逐字稿就是共享脈絡。席位被呼叫時，會被要求讀取 `data/roundtable.md`、唯讀查閱設定的專案資料夾，並以繁體中文回應且不得編輯檔案。

---

## 功能

- **單一討論室逐字稿**：訊息保存於 `data/transcript.jsonl`，並鏡像到 `data/roundtable.md`。
- **模型席位**：Codex、透過 Codex 的 DeepSeek、agy、Claude 席位都可在 UI 中啟用或停用。
- **各席位模型選擇器**：模型選擇會持久化到 `data/settings.json`。
- **訊息編號**：UI 氣泡顯示 `[n]` 編號，對應 Markdown 逐字稿章節。
- **單席取消**：可取消特定正在執行的 AI 呼叫，不必停止整個 server。
- **共識討論模式**：第一輪平行收集獨立意見；後續輪次 round-robin，直到所有有回應席位標記共識或達到輪次上限。
- **會議歸檔**：開新會議時會歸檔目前逐字稿與 Markdown 鏡像；啟動時可針對同一專案資料夾恢復或重新命名已歸檔會議。
- **專案目標設定**：參與者會查閱啟動時選擇或透過 `AI_ROUNDTABLE_PROJECT_DIR` 指定的專案資料夾。
- **可選 Tailscale 綁定**：若偵測到 Tailscale，server 也會嘗試綁定該機器的 tailnet IPv4 位址。

---

## 技術棧

- **Runtime**：Python 3.10+
- **Server**：Python 標準函式庫 `ThreadingHTTPServer`
- **Frontend**：單檔 HTML / CSS / JavaScript
- **Process model**：每次 AI 席位呼叫對應一個 subprocess，並追蹤以支援取消
- **Persistence**：`data/` 底下的本機 JSON / JSONL / Markdown 檔案
- **目標平台**：Windows 本機桌面工作流

App 本身不需要任何第三方 Python 套件。

---

## Quick Start

**前置要求**：Windows 10 / 11 與 Python 3.10+。

### Setup

```bat
py -3.10 -m venv .venv
start.cmd
```

開啟：

```text
http://127.0.0.1:8787/
```

### 可選 AI 後端

| 後端 | 預期本機路徑 / 設定 |
|---|---|
| Codex | `%APPDATA%\npm\codex.cmd` |
| agy | `%LOCALAPPDATA%\agy\bin\agy.exe` |
| Claude | 安裝 Claude desktop app，並具有內建 Claude Code CLI |
| DeepSeek | Moon Bridge 加上 DeepSeek Codex profile |

當 `MOON_BRIDGE_EXE` 與 `MOON_BRIDGE_CONFIG` 能解析到既有檔案時，`start.cmd` 會嘗試啟動 Moon Bridge。

---

## 設定

環境變數：

| 變數 | 用途 |
|---|---|
| `AI_ROUNDTABLE_PROJECT_DIR` | 參與者可查閱的專案資料夾 |
| `AI_ROUNDTABLE_DS_CODEX_HOME` | DeepSeek 席位使用的 `CODEX_HOME` |
| `MOON_BRIDGE_EXE` | `moonbridge.exe` 路徑 |
| `MOON_BRIDGE_CONFIG` | Moon Bridge `config.yml` 路徑 |
| `AI_ROUNDTABLE_NO_BROWSER=1` | 啟動時不要自動開瀏覽器 |

本機 runtime 檔案會寫入 `data/`，此資料夾刻意由 git 忽略。

---

## 目前進度

| 範疇 | 狀態 |
|---|---|
| Localhost chat UI | 完成 |
| 逐字稿 JSONL + Markdown 鏡像 | 完成 |
| Codex / DS / agy / Claude adapters | 完成 |
| 各席位模型選擇 | 完成 |
| 會議歸檔 / 恢復 / 重新命名 | 完成 |
| 單席取消 | 完成 |
| 共識討論模式 | 完成 |
| UI 逐字稿編號對應 Markdown 章節 | 完成 |
| 自動化測試套件 | 尚未加入 |
| 網路安全強化 / 認證 | 尚未納入範圍 |

---

## 目錄結構

```text
.
├── server.py       # localhost server、狀態、會議歸檔、AI subprocess adapters
├── index.html      # 單頁 chat UI
├── start.cmd       # Windows launcher；可選啟動 Moon Bridge，然後啟動 server.py
├── stop.cmd        # 停止 listen 在 port 8787 的 process
├── data/           # 本機設定、目前逐字稿、歸檔會議；git ignored
├── .agents/        # 本機 agent/tooling metadata
├── .codex/         # 此 repo 的本機 Codex config
└── .claude/        # 本機 Claude tooling config；不要 commit
```

---

## 驗證

基本語法檢查：

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py
```

手動 smoke test：

1. 執行 `start.cmd`。
2. 開啟 `http://127.0.0.1:8787/`。
3. 選擇一個或多個席位。
4. 送出 prompt，確認回覆出現在 UI。
5. 確認 `data/transcript.jsonl` 與 `data/roundtable.md` 有更新。
6. 開新會議，確認前一份逐字稿已歸檔。

---

## 安全備註

本工具設計給受信任的本機環境使用。它沒有登入、CSRF 保護或授權層。若 Tailscale 綁定啟用，任何可連到該 tailnet 位址的人都可能讀取會議狀態並觸發本機 AI subprocess 呼叫。

Prompt 會要求 AI 席位不得建立、修改或刪除檔案。Codex 與 DeepSeek 會以 Codex read-only sandbox 啟動；其他本機 CLI 工具仍取決於自身權限行為與本機設定。

---

## 授權

MIT License。見 [`LICENSE`](./LICENSE)。