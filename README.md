# Recap MCP

Lets Claude (on your phone) fill in your Staff App recaps by voice. It runs on this PC and Claude reaches it through an ngrok tunnel.

## Before you start

- Install Python 3.13 from python.org and tick "Add python.exe to PATH" in the installer.
- This PC must stay on and not sleep: Settings -> System -> Power -> Sleep: Never.
- Make a free ngrok account and claim a free static domain (the free tier has usage limits).

## 1. Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium
```

## 2. Save your login

The `python -m recap_mcp ...` commands below need the venv active first (`.venv\Scripts\activate`), or write `.venv\Scripts\python -m recap_mcp ...` instead.

Change your Staff App password first, in the Staff App under Your Profile -> Change Password (you are about to store it in Windows Credential Manager, so use one you don't reuse elsewhere). Then:

```
python -m recap_mcp set-credentials --public-host <your-ngrok-domain>
```

Enter your username and password. It prints your connector URL, `https://<domain>/<secret>/mcp`. The secret part is what protects the server: treat the URL like a password. Optional sanity check: `python -m recap_mcp check` lists your pending recaps.

## 3. ngrok

Sign up at ngrok.com (free), install it, run `ngrok config add-authtoken <token>`, and claim your free static domain in the ngrok dashboard (Domains). Then:

```
python -m recap_mcp serve --public-host <domain>
ngrok http --url=<domain> 8765
```

(two terminals). Logs go to `data\server.log`.

## 4. Start automatically at logon

Run once in PowerShell, replacing `<domain>` (the ngrok path below is the Microsoft Store install; use `where ngrok` otherwise). No time limit: Windows' default would stop them after 72 hours.

```
$dir = "C:\Users\vboym\Documents\Web Projects\automate-recaps"; $domain = "<domain>"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -Force -TaskName RecapMCP -Trigger $trigger -Settings $settings -Action (New-ScheduledTaskAction -Execute wscript.exe -Argument "`"$dir\hidden.vbs`" `"$dir\.venv\Scripts\python.exe`" -m recap_mcp serve --public-host $domain" -WorkingDirectory $dir)
Register-ScheduledTask -Force -TaskName RecapNgrok -Trigger $trigger -Settings $settings -Action (New-ScheduledTaskAction -Execute wscript.exe -Argument "`"$dir\hidden.vbs`" `"$env:LOCALAPPDATA\Microsoft\WindowsApps\ngrok.exe`" http --url=$domain 8765" -WorkingDirectory $dir)
```

Both run hidden (`hidden.vbs` starts them with no window). Logs: `data\server.log`; tunnel status: http://127.0.0.1:4040. Restart the server with `.\restart.ps1`. `Stop-ScheduledTask` only stops the hidden wrapper, not the program: to stop for real, end `python.exe` / `ngrok.exe` in Task Manager.

To restart the server automatically after every commit, pull, checkout or rebase, install the git hooks once:

```
foreach ($h in "post-commit","post-merge","post-checkout","post-rewrite") { Set-Content ".git\hooks\$h" "#!/bin/sh`npowershell -NoProfile -ExecutionPolicy Bypass -File restart.ps1`n" -Encoding ascii -NoNewline }
```

## 5. Add it to Claude

claude.ai -> Settings -> Connectors -> Add custom connector. Paste `https://<domain>/<secret>/mcp`. It then shows up in the mobile app.

## Things to say

- "What recaps do I need to do?"
- "Start the recap for Red Zone last night."
- "Attendance was about 80, I engaged around 40 people." (it will mark estimates)
- "Give me a link to upload my photos."
- "I uploaded the receipt, read it and fill in the spend. Then save the draft."
- "Which shifts still need a claim?" / "Submit the claim for Fletchers, I worked the scheduled times."

## Safety

- It only saves recap **drafts**. It never submits a recap. You review and submit in the Staff App.
- Pay claims: `check_claims` lists shifts and their claim status; `submit_claim` files the time claim (scheduled times unless you say otherwise) only after reading it back and getting your yes. It never withdraws or deletes claims; edit those in the Staff App.
- Crowd photos go **live in the event photo gallery** on the site, not just into the draft.
- Anyone with the connector URL can use the server, so don't share it. To rotate it, delete the `connector_secret` entry for `recap-mcp` in Windows Credential Manager and run `set-credentials` again.
