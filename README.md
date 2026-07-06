# ai-roundtable

**English** | [繁體中文](./README_zh-TW.md) | [简体中文](./README_zh-CN.md)

![Python](https://img.shields.io/badge/PYTHON-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/PLATFORM-WINDOWS-0078D6?logo=windows&logoColor=white)
![UI](https://img.shields.io/badge/UI-LOCALHOST-10a37f)
![Status](https://img.shields.io/badge/STATUS-ACTIVE%20LOCAL%20TOOL-orange)

> A localhost multi-AI roundtable for asking several CLI-based AI seats to inspect the same project, read the same transcript, and respond in one shared discussion room.

It is a small Windows-first tool for local review, design discussion, and second-opinion workflows. The app keeps the current meeting as a JSONL transcript and mirrors it to Markdown so every participant can read the same conversation state before answering.

---

## Development Status

This is a personal local tool under active iteration. The core workflow is usable, but the CLI integrations depend on tools installed on the host machine, and the app is not packaged as a service or hardened for untrusted network access.

---

## Concept

`ai-roundtable` treats each model provider as a named seat at the same table:

| Seat | Backend | Typical role |
|---|---|---|
| Codex | Codex CLI | Repo-aware implementation and review opinions |
| DS | Codex CLI with a DeepSeek `CODEX_HOME` | DeepSeek-backed second opinion through Moon Bridge |
| agy | agy CLI | Gemini / Claude / GPT-OSS model seat, depending on local agy config |
| Claude | Claude desktop app bundled Claude Code CLI | Claude-family reviewer seat |

The transcript is the shared context. When a seat is called, it is instructed to read `data/roundtable.md`, inspect the configured project folder read-only, and respond in Traditional Chinese without editing files.

---

## Features

- **Single-room transcript**: messages are stored in `data/transcript.jsonl` and mirrored to `data/roundtable.md`.
- **Model seats**: Codex, DeepSeek via Codex, agy, and Claude seats can be enabled or disabled from the UI.
- **Per-seat model picker**: selected models are persisted under `data/settings.json`.
- **Message numbers**: UI bubbles show `[n]` indexes that match the Markdown transcript sections.
- **Single-seat cancellation**: cancel a specific running AI call without stopping the whole server.
- **Consensus discussion mode**: first round collects independent opinions in parallel; later rounds run round-robin until all responding seats mark consensus or the round limit is reached.
- **Session archive**: starting a new meeting archives the current transcript and Markdown mirror; the startup prompt can restore or rename archived sessions for the same project folder.
- **Project targeting**: participants inspect the project directory selected at startup or supplied through `AI_ROUNDTABLE_PROJECT_DIR`.
- **Optional Tailscale bind**: if Tailscale is available, the server also attempts to bind to the machine's tailnet IPv4 address.

---

## Tech Stack

- **Runtime**: Python 3.10+
- **Server**: Python standard library `ThreadingHTTPServer`
- **Frontend**: single-file HTML / CSS / JavaScript
- **Process model**: one subprocess per AI seat call, tracked for cancellation
- **Persistence**: local JSON / JSONL / Markdown files under `data/`
- **Platform target**: Windows local desktop workflow

No third-party Python package is required by the app itself.

---

## Quick Start

**Requirements**: Windows 10 / 11 and Python 3.10+.

### Setup

```bat
py -3.10 -m venv .venv
start.cmd
```

Open:

```text
http://127.0.0.1:8787/
```

### Optional AI Backends

| Backend | Expected local path / setup |
|---|---|
| Codex | `%APPDATA%\npm\codex.cmd` |
| agy | `%LOCALAPPDATA%\agy\bin\agy.exe` |
| Claude | Claude desktop app with bundled Claude Code CLI |
| DeepSeek | Moon Bridge plus a DeepSeek Codex profile |

`start.cmd` will try to start Moon Bridge when `MOON_BRIDGE_EXE` and `MOON_BRIDGE_CONFIG` resolve to existing files.

---

## Configuration

Environment variables:

| Variable | Purpose |
|---|---|
| `AI_ROUNDTABLE_PROJECT_DIR` | Project folder participants may inspect |
| `AI_ROUNDTABLE_DS_CODEX_HOME` | `CODEX_HOME` used by the DeepSeek seat |
| `MOON_BRIDGE_EXE` | Path to `moonbridge.exe` |
| `MOON_BRIDGE_CONFIG` | Path to Moon Bridge `config.yml` |
| `AI_ROUNDTABLE_NO_BROWSER=1` | Disable automatic browser launch on startup |

Local runtime files are written under `data/`, which is intentionally ignored by git.

---

## Current Progress

| Area | Status |
|---|---|
| Localhost chat UI | Done |
| Transcript JSONL + Markdown mirror | Done |
| Codex / DS / agy / Claude adapters | Done |
| Per-seat model selection | Done |
| Session archive / restore / rename | Done |
| Single-seat cancellation | Done |
| Consensus discussion mode | Done |
| UI transcript indexes matching Markdown sections | Done |
| Automated test suite | Not yet added |
| Network hardening / authentication | Not in scope yet |

---

## Directory Structure

```text
.
├── server.py       # localhost server, state, session archive, AI subprocess adapters
├── index.html      # single-page chat UI
├── start.cmd       # Windows launcher; optionally starts Moon Bridge, then server.py
├── stop.cmd        # stops the process listening on port 8787
├── data/           # local settings, active transcript, archived sessions; ignored by git
├── .agents/        # local agent/tooling metadata
├── .codex/         # local Codex config for this repo
└── .claude/        # local Claude tooling config; do not commit
```

---

## Validation

Basic syntax check:

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py
```

Manual smoke test:

1. Run `start.cmd`.
2. Open `http://127.0.0.1:8787/`.
3. Select one or more seats.
4. Send a prompt and confirm replies appear in the UI.
5. Confirm `data/transcript.jsonl` and `data/roundtable.md` update.
6. Start a new meeting and confirm the previous transcript is archived.

---

## Security Notes

This tool is designed for trusted local use. There is no login, CSRF protection, or authorization layer. If Tailscale binding is active, anyone who can reach the exposed tailnet address may be able to read meeting state and trigger local AI subprocess calls.

The prompts instruct AI seats not to create, modify, or delete files. Codex and DeepSeek are launched with Codex read-only sandboxing, while other local CLI tools still depend on their own permission behavior and local configuration.

---

## License

MIT License. See [`LICENSE`](./LICENSE).
