# Recap MCP

Fill in your Network Ninja Staff App shift recaps by talking to Claude.

After a bar activation you open Claude on your phone and talk through how the night went: attendance, how many people you engaged, the spend, what the bartender said. Claude turns that into the recap's answers, collects your photos through an upload link, reads the receipt, and saves the recap as a draft. When you're happy with it, Claude can submit it for you, but only after reading it back and getting your "yes". It can also file your pay (time) claims the same way.

It is an [MCP](https://modelcontextprotocol.io) server: a small program that runs on your own Windows PC and gives Claude a set of tools for the Staff App. Your login never leaves your PC.

## What it can do

| Ask for | What happens |
|---|---|
| Find recaps | Lists the shifts that still need a recap, including ones with a saved draft. |
| Fill a recap | Matches what you say to the recap's questions, marks numbers you hedged on ("about 80") as estimates, and tells you what's still missing. |
| Photos | Gives you a 60-minute upload link for setup, receipt, feature sign and crowd photos. |
| Receipts | Reads the uploaded receipt and fills in the spend and tip, checking that the totals add up. |
| Save a draft | Saves the recap as a draft you can see and edit in the Staff App. If you edited it there, your edits win. |
| Submit a recap | Finishes the recap, like the Finish button. Needs everything required filled in and your explicit yes. |
| Pay claims | Shows which shifts still need a claim and files the claim (scheduled times unless you say otherwise), after your yes. |

## How it works

```
Claude  ──>  ngrok tunnel  ──>  Recap MCP on your PC  ──>  Staff App
             (public URL)       (127.0.0.1:8765)         (your account)
```

- The server runs on your PC and logs in to the Staff App as you, with a headless browser for the login and the Staff App's own API after that.
- ngrok gives your PC a fixed public web address so Claude can reach the server from anywhere.
- The connector URL contains a long random secret. Only someone with that URL can use the server.
- Your username, password and the secret are kept in Windows Credential Manager, not in files. Local data (answers, photos, logs) lives in the `data\` folder.

## What you need

- A Windows PC that stays on while you want to use it. Turn off sleep: Settings -> System -> Power -> Sleep: Never.
- Python 3.13 from python.org. In the installer, tick "Add python.exe to PATH".
- A free ngrok account with a free static domain (the free tier has usage limits).
- A Claude account that can add custom connectors.

## Setup

Open PowerShell in this project folder for all the steps below.

### 1. Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium
```

Commands below that start with `python -m recap_mcp` need the venv active (`.venv\Scripts\activate`). Or run `.venv\Scripts\python -m recap_mcp ...` instead.

### 2. Set up ngrok

1. Sign up at [ngrok.com](https://ngrok.com).
2. Install ngrok. Either `winget install ngrok.ngrok`, or, if `winget` isn't recognized (common on Windows 10), download the Windows zip from [ngrok.com/download](https://ngrok.com/download) and extract `ngrok.exe` somewhere like `C:\ngrok\`.
3. Connect it to your account: `ngrok config add-authtoken <token>` (the token is on your ngrok dashboard).
4. In the ngrok dashboard, under Domains, claim your free static domain, for example `something-random.ngrok-free.dev`. That's `<domain>` below.

### 3. Save your Staff App login

Your password will be stored in Windows Credential Manager, so first change your Staff App password to one you don't use anywhere else (Staff App -> Your Profile -> Change Password). Then:

```
python -m recap_mcp set-credentials --public-host <domain>
```

Enter your username and password. It prints your **connector URL**, `https://<domain>/<secret>/mcp`. Treat it like a password.

Optional check that the login works: `python -m recap_mcp check` lists your pending recaps.

### 4. Start it automatically

This registers two Windows scheduled tasks that start the server and the tunnel at logon, with no windows, and restart them if they crash. Run it once, in PowerShell, from the project folder. Replace `<domain>`, and set `$ngrok` to where your `ngrok.exe` is (`where.exe ngrok` shows it; the default below is the winget/Store location).

```
$dir = (Get-Location).Path; $domain = "<domain>"; $ngrok = "$env:LOCALAPPDATA\Microsoft\WindowsApps\ngrok.exe"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
Register-ScheduledTask -Force -TaskName RecapMCP -Trigger $trigger -Settings $settings -Action (New-ScheduledTaskAction -Execute wscript.exe -Argument "`"$dir\hidden.vbs`" `"$dir\.venv\Scripts\python.exe`" -m recap_mcp serve --public-host $domain" -WorkingDirectory $dir)
Register-ScheduledTask -Force -TaskName RecapNgrok -Trigger $trigger -Settings $settings -Action (New-ScheduledTaskAction -Execute wscript.exe -Argument "`"$dir\hidden.vbs`" `"$ngrok`" http --url=$domain 8765" -WorkingDirectory $dir)
Start-ScheduledTask RecapMCP; Start-ScheduledTask RecapNgrok
```

The time limit is turned off on purpose: by default Windows stops scheduled tasks after 72 hours.

Prefer to run it by hand instead? Use two terminals: `python -m recap_mcp serve --public-host <domain>` and `ngrok http --url=<domain> 8765`.

### 5. Add it to Claude

claude.ai -> Settings -> Connectors -> Add custom connector, and paste your connector URL (`https://<domain>/<secret>/mcp`). It then shows up in the Claude mobile app too. To stop Claude asking for approval on every step, set the tools to "always allow" in the connector's settings.

## Using it

Just talk normally. For example:

- "What recaps do I need to do?"
- "Start the recap for Red Zone last night."
- "Attendance was about 80, I engaged around 40 people." (it marks these as estimates)
- "Give me a link to upload my photos."
- "I uploaded the receipt, read it and fill in the spend. Then save the draft."
- "Submit the Red Zone recap." (it reads a summary back and waits for your yes)
- "Which shifts still need a claim?" / "Submit the claim for Fletchers, I worked the scheduled times."

A typical recap: Claude asks the yes/no questions that unlock other sections (manager check-in, spend, feature sign, menu listing), sends you the photo link, asks "How did the account go?" and lets you ramble, then asks only for what's still missing. When nothing required is missing it saves the draft and asks whether to submit.

## Managing the server

- **Is it running?** Tunnel status: http://127.0.0.1:4040. Server log: `data\server.log`.
- **Restart the server:** `.\restart.ps1`.
- **Stop it:** end `python.exe` (server) or `ngrok.exe` (tunnel) in Task Manager. `Stop-ScheduledTask` alone isn't enough: it only stops the hidden launcher, not the program.
- **Restart automatically after code changes:** install these git hooks once, and the server restarts after every commit, pull, checkout or rebase:

  ```
  foreach ($h in "post-commit","post-merge","post-checkout","post-rewrite") { Set-Content ".git\hooks\$h" "#!/bin/sh`npowershell -NoProfile -ExecutionPolicy Bypass -File restart.ps1`n" -Encoding ascii -NoNewline }
  ```

  After a change to `pyproject.toml` (new packages), also run `pip install -e .`.

## Safety and privacy

- **Drafts by default.** Recaps are saved as drafts. Submitting only happens when you ask, when nothing required is missing, and after Claude reads a summary back and you say yes. Submitting is final.
- **Pay claims** are filed only after the same read-back and yes. It never withdraws or deletes claims; change those in the Staff App.
- **Crowd photos go live** in the event's photo gallery on the site as soon as the draft is saved, not just into the recap.
- **Claude only uses what you said.** It's told never to invent numbers, quotes or feedback, and to ask rather than guess.
- **Keep the connector URL private.** Anyone with it can use the server as you. To replace it, delete the `connector_secret` entry for `recap-mcp` in Windows Credential Manager, run `set-credentials` again, and update the connector in Claude.
- **Your secrets stay on your PC**, in Windows Credential Manager. The server's log hides the login token and the secret path.

## Troubleshooting

- **Claude says the connector can't be reached:** check http://127.0.0.1:4040 (is the tunnel up?) and the end of `data\server.log` (did the server start?). Then `.\restart.ps1`. Also check the PC is awake.
- **`winget` is not recognized:** install ngrok from the zip instead (Setup step 2).
- **Login failed:** run `python -m recap_mcp check`. If it fails, re-save your login with `set-credentials`. Your Staff App password may have changed.
- **A new tool doesn't show up in Claude:** restart the server, then reconnect or refresh the connector in Claude's settings.

## Development

```
pip install -e .[dev]
python -m pytest -q
```

The code is in `recap_mcp/`: `server.py` (the tools Claude sees), `site.py` (Staff App API and login), `recap.py` (recap form logic), `claims.py` (pay claims), `upload.py` (photo upload page), `store.py` (local SQLite), and `instructions.md` (how Claude is told to run a recap). Design notes are in `docs/`.
