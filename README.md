# ai-roundtable

Localhost multi-AI roundtable UI. The app stores the active transcript in
`data/transcript.jsonl` and mirrors it to `data/roundtable.md`.

## Requirements

- Windows
- Python 3.10+
- Codex CLI at `%APPDATA%\npm\codex.cmd`
- Optional: agy CLI at `%LOCALAPPDATA%\agy\bin\agy.exe`
- Optional: Claude desktop app with Claude Code CLI
- Optional: Moon Bridge for the DeepSeek-backed Codex profile

## Setup

```bat
py -3.10 -m venv .venv
start.cmd
```

Open `http://127.0.0.1:8787/`.

## Configuration

The app writes local settings and transcripts under `data/`, which is ignored by
git.

Environment variables:

- `AI_ROUNDTABLE_PROJECT_DIR`: project folder participants may inspect.
- `AI_ROUNDTABLE_DS_CODEX_HOME`: CODEX_HOME used by the DeepSeek seat.
- `MOON_BRIDGE_EXE`: path to `moonbridge.exe`.
- `MOON_BRIDGE_CONFIG`: path to Moon Bridge `config.yml`.
- `AI_ROUNDTABLE_NO_BROWSER=1`: do not auto-open the browser on startup.