@echo off
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { Stop-Process -Id $c.OwningProcess -Force; Write-Host ('roundtable server stopped (PID ' + $c.OwningProcess + ')') } else { Write-Host 'server is not running' }"
pause
