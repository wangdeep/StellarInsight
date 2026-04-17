# Stellar Insight — Build Guide

## Before you build

### 1. Register your EVE developer application

Go to https://developers.eveonline.com and create a new application:

- **Connection Type**: Authentication & API Access
- **Application Type**: Native (enables PKCE — no client secret required)
- **Callback URL**: `http://localhost:7742/eve/callback`
- **Scopes**: Add all the scopes you want users to have (wallet, skills, industry, PI, corp, etc.)

Copy the **Client ID** from the application dashboard.

### 2. Set your Client ID in the app

Open `app.py` and set:

```python
EVE_CLIENT_ID: str = os.environ.get("EVE_SSO_CLIENT_ID", "your_client_id_here")
```

Or set it via environment variable at build time:

```batch
set EVE_SSO_CLIENT_ID=your_client_id_here
pyinstaller xylon_eve.spec
```

---

## Building the Windows executable

```batch
:: Install all runtime + build dependencies (one-time on your dev machine)
pip install -r requirements.txt
pip install pyinstaller

:: Build the single .exe
pyinstaller xylon_eve.spec
```

Output: `dist\StellarInsight.exe` — single file, roughly 60–120 MB.

---

## What the end user gets

One `.exe` file. They double-click it. The app starts silently, a **Stellar Insight** icon appears in the system tray, and their default browser opens to the app automatically.

On first run they click **Add Character**, which opens their browser to the EVE SSO login page. They authorise, the browser redirects back to the local app, and their character is linked. All data is stored locally in `%APPDATA%\StellarInsight\`.

**Right-clicking the tray icon** shows:

| Menu item | Effect |
|-----------|--------|
| Open Stellar Insight | Opens/focuses the browser tab |
| Start Relay Server | Starts the local relay on port 777 (for self-hosting) |
| Stop Relay Server | Stops the local relay |
| Quit | Closes the app completely |

---

## The relay server

`relay_server.py` is bundled inside the `.exe`. End users who want to self-host a corp sharing relay can start it from the tray menu — it runs on port 777 and stores data in `%USERPROFILE%\.stellar_insight\relay.db`.

The default shared relay hosted by the developer runs at `http://insight.stellarforge.nexus/share` (port 777, Cloudflare Tunnel). Most users will use this and don't need to run their own.

---

## Security notes

- **No client secret is bundled** — PKCE authentication means the app only contains your public Client ID, which is safe to distribute.
- **Tokens are encrypted at rest** — refresh tokens in the local SQLite database are encrypted using Fernet (AES-128-CBC + HMAC).
- **Local only** — the embedded server binds to `127.0.0.1` only and is never reachable from the network.

---

## Development (running without building)

```batch
pip install -r requirements.txt
python main.py
```

Or run just the API server directly (no tray, no browser open):

```batch
python -m uvicorn app:app --host 127.0.0.1 --port 7742 --reload
```

Run the relay server separately if needed:

```batch
python relay_server.py --port 777
```
