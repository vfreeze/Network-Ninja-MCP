# Recap MCP

Lets Claude (on your phone) fill in your Staff App recaps by voice. It runs on this PC and Claude reaches it through an ngrok tunnel.

## 1. Install

```
python -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium
```

## 2. Save your login

The `python -m recap_mcp ...` commands below need the venv active first (`.venv\Scriptsctivate`), or write `.venv\Scripts\python -m recap_mcp ...` instead.

Change your Staff App password first (you are about to store it in Windows Credential Manager, so use one you don't reuse elsewhere). Then:

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

Find your ngrok path with `where ngrok` (scheduled tasks may not have it on PATH, so use the full path). Run once in a terminal, replacing the ngrok path and `<domain>`:

```
schtasks /create /tn "RecapMCP" /sc onlogon /tr "\"C:\Users\vboym\Documents\Web Projects\automate-recaps\.venv\Scripts\python.exe\" -m recap_mcp serve --public-host <domain>"
schtasks /create /tn "RecapNgrok" /sc onlogon /tr "\"C:\path\to\ngrok.exe\" http --url=<domain> 8765"
```

## 5. Add it to Claude

claude.ai -> Settings -> Connectors -> Add custom connector. Paste `https://<domain>/<secret>/mcp`. It then shows up in the mobile app.

## Things to say

- "What recaps do I need to do?"
- "Start the recap for Red Zone last night."
- "Attendance was about 80, I engaged around 40 people." (it will mark estimates)
- "Give me a link to upload my photos."
- "I uploaded the receipt, read it and fill in the spend. Then save the draft."

## Safety

- It only saves **drafts**. It never submits a recap and never touches claims. You review and submit in the Staff App.
- Crowd photos go **live in the event photo gallery** on the site, not just into the draft.
- Anyone with the connector URL can use the server, so don't share it. To rotate it, delete the `connector_secret` entry for `recap-mcp` in Windows Credential Manager and run `set-credentials` again.
