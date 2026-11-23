# Restarts the hidden MCP server (RecapMCP task). Stop-ScheduledTask alone only kills
# the hidden.vbs wrapper and leaves python running, so kill by command line instead.
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='wscript.exe'" |
  Where-Object { $_.CommandLine -like '*recap_mcp*serve*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
# Start is ignored while the task still counts as running, so wait for it to settle.
for ($i = 0; $i -lt 20 -and (Get-ScheduledTask RecapMCP).State -eq 'Running'; $i++) { Start-Sleep -Milliseconds 250 }
Start-ScheduledTask RecapMCP
