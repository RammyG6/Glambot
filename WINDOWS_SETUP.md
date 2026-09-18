# Running & fixing Glambot on Windows

This guide sets up a Windows laptop to **run** Glambot and to **fix** it there with Claude
Code (so Windows-only problems can be debugged on the machine where they happen — a Mac
can't reproduce them).

> The "stuck at processing" hang some Windows machines hit was an ffmpeg pipe-buffer deadlock
> in the progress reader. It's fixed in the current code — make sure you're on the latest
> version (clone/pull from GitHub, step 5), not an old copied folder.

## 1. Install the prerequisites

Install each of these (accept defaults unless noted):

| Tool | Where | Notes |
|------|-------|-------|
| **Git for Windows** | https://git-scm.com/download/win | Gives you `git` and "Git Bash". |
| **Python 3.11+** | https://www.python.org/downloads/windows/ | **Tick "Add python.exe to PATH"** on the first install screen. |
| **ffmpeg** (recommended) | `winget install Gyan.FFmpeg` in a terminal | See note below — this fixes/avoids two processing problems. |
| **Node.js LTS** | https://nodejs.org | Needed only for Claude Code (step 6). |

**Why install ffmpeg (strongly recommended):** the app *can* run without it (it falls back to
a bundled ffmpeg), but installing a real one:
- provides **ffprobe**, which powers thumbnails/kiosk previews, the live progress %, and
  audio-stream detection; and
- avoids the bundled `imageio-ffmpeg` trying to **download** its ffmpeg binary over the network
  on first use — a slow or blocked download is another way "processing" can look stuck.

After installing, **open a new terminal** and confirm: `ffmpeg -version` and `python --version`
both print a version.

## 2–5. Get the current code

```powershell
cd C:\           # or wherever you want it, e.g. C:\Glambot
git clone https://github.com/RammyG6/Glambot.git
cd Glambot
```

If you had **copied the folder over before**, don't reuse it — clone fresh (it has the fixes).
If you must reuse a copied folder, **delete its `.venv` folder first** (a macOS virtualenv
cannot run on Windows; `run.bat` will build a fresh one).

**Copy your secrets in** (these are intentionally *not* in GitHub). From the Mac's project
folder, copy these into the cloned `Glambot` folder:
- `.env`
- `credentials.json`
- `token.json`

Then edit `.env` and set a Windows path for the footage root, e.g.:

```
INBOX_DIR=C:\Glambot\project
```

(If you don't have the Mac's `.env`, copy `.env.example` to `.env` and fill in your Drive
folder ID + SMTP settings — see `README.md`.)

## 6. (To fix things) Install Claude Code

```powershell
npm install -g @anthropic-ai/claude-code
```

Then, **inside the `Glambot` folder**, run:

```powershell
claude
```

The first run walks you through signing in. Now Claude is running in the repo on Windows and
can see real Windows errors/logs — ask it to look at whatever's failing. Commit & push fixes
with normal `git` so the Mac can pull them too.

## 7. Run the app

Double-click **`Glambot.bat`**. First run creates a virtualenv and installs dependencies (takes
a minute), then starts the app and **opens your browser automatically** at
http://127.0.0.1:5000 (or whatever `HOST:PORT` you set in `.env`) once it's ready. The console
window stays open so you can watch progress/logs, and it waits for a keypress before closing if
something goes wrong, so errors are never lost to a flashing window.

(`run.bat` still works too — same thing without the friendly title, auto-opened browser, or
pause-on-error; useful if you want to run it from a terminal instead of double-clicking, e.g.
while debugging.)

## Serving guests / an iPad over the LAN

By default Glambot only listens on `127.0.0.1` (this PC). To let guest phones
download clips, or to control Glambot from an iPad, you need a shared network
and two `.env` changes.

**Network** — either works, no internet required:
- **A travel Wi-Fi router** — plug the Glambot PC in (Ethernet or Wi-Fi) and
  have guests join the same router's SSID.
- **Windows Mobile Hotspot** — Settings → Network & internet → Mobile hotspot.
  The PC broadcasts its own Wi-Fi; guests join that. (~8 device limit, and it
  can't run at the same time as the PC using Wi-Fi for internet.)

**`.env`:**
```
BIND_HOST=0.0.0.0
GLAMBOT_PIN=1234          # required once you're on the LAN - any 4+ chars
LAN_SSID=YourEventWifi    # optional: adds a "Join Wi-Fi" QR to the kiosk/photo
LAN_PASSWORD=...
PUBLIC_BASE_URL=          # optional: force a URL, e.g. http://192.168.8.1:5000
```

Then restart Glambot. Find this PC's address with `ipconfig` (the IPv4 address,
e.g. `192.168.8.101`) — guests/iPad open `http://192.168.8.101:5000`.

- The **GlambotSetup.exe installer** adds a Windows Firewall rule for TCP 5000
  automatically. If you run from source, Windows will prompt once to "allow"
  Python through the firewall — click **Allow**.
- The tray/desktop window keeps working with no PIN prompt — loopback requests
  are always trusted.
- Per-project guest downloads still need that project's own **download PIN**
  (set on the project form), separate from `GLAMBOT_PIN`.

## Camera FTP auto-import (FX6 etc.)

Glambot has a **built-in FTP server** — you no longer need to run FileZilla
Server. Open the **FTP import** tab in the sidebar:

1. Set the **Import folder** (default `D:\GlambotAuto_Import`) — click *Create it*
   if it doesn't exist yet.
2. Leave **Allow anonymous upload** ticked for a dedicated camera↔PC network, or
   set a username/password.
3. Click **Start**. Once running it auto-starts with Glambot on future launches.
4. On the camera's FTP profile, enter the values shown in the **Camera setup**
   card: this PC's IP, port **2121**, passive mode, upload directory `/`.
5. In **Projects → Edit**, point that project's *footage source folder* at the
   same Import folder. Uploaded clips are then processed automatically.

The port (default 2121) avoids needing administrator rights. If Windows Firewall
isn't allowing camera connections, the tab shows an **Apply firewall rule**
button (and prompts on server start) — click it and confirm the one Windows
permission dialog.

## Troubleshooting

- **A window flashes and closes / "python not recognized":** Python isn't on PATH — reinstall
  Python with "Add python.exe to PATH" ticked. `Glambot.bat` now pauses on this error instead of
  closing immediately, or you can run `run.bat` from a terminal to read the error.
- **Stuck at processing a clip:** make sure you're on the latest code (`git pull`), confirm
  `ffmpeg -version` works, and watch the terminal window for the `Processing job … : <cmd>`
  line. If it still hangs, run `claude` in the repo and ask it to investigate — it can see the
  live logs and the exact ffmpeg command.
- **Thumbnails / progress bar not working:** install ffmpeg (step 1) so `ffprobe` is available.
- **Folder browser can't reach another drive** (e.g. `D:\`): just paste the full path into the
  "Footage source folder" field instead of browsing to it.
