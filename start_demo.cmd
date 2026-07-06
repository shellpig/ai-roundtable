@echo off
rem 公開 demo 模式（雙擊執行）：關閉 loopback 自動 HOST，公網訪客一律用邀請碼。
rem 用法：
rem   1) 先另開一個視窗跑：tailscale funnel 8787   （對外曝露）
rem   2) 雙擊本檔啟動 server；終端會問專案資料夾 -> 輸入拋棄用的資料夾
rem   3) 終端印出「HOST 進場連結」(127.0.0.1) 點它進 HOST；
rem      在 HOST 介面按「邀請」產生 guest 連結（網址會自動抓 Tailscale 公開網址）
rem   4) demo 完關掉 tailscale funnel：tailscale funnel --bg off
set "AI_ROUNDTABLE_PUBLIC=1"
echo [demo] 公開模式已開啟。請確認已在另一視窗執行 tailscale funnel 8787。
call "%~dp0start.cmd"
