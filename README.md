# Glambot Footage Pipeline

Watches a single inbox folder for newly saved footage, trims/overlays/compresses
it with ffmpeg per a project config, and holds it for **manual approval** in a
local web app before uploading it to Google Drive and emailing the recipient a
download link (with an on-screen QR code).

Nothing is uploaded or sent until you click **Approve** on a clip.

## How it works

```
inbox/
  <project-name>/                 # one folder per client/project
    config.json                   # bitrate, resolution, aspect ratio, overlay, recipient, trim
    footage1.mp4
    footage2.mov
    Output_ReadytoSend/           # processed clips, created automatically
    Email Sent File/              # archive, created automatically after sending
overlays/
  brand.png                       # overlay graphics referenced by config.json
```

1. Set up a project's `config.json` — either hand-write it (schema below), or
   click **+ New Project** on the review page for a form with resolution/fps/
   bitrate dropdowns, a client email field, and a live preview for sizing and
   placing the overlay graphic. Either way it lands at `inbox/<project>/config.json`.
2. Drop footage into that `inbox/<project>/` folder (Finder drag-and-drop is
   fine — the form doesn't upload video files, only the config).
3. The watcher notices the new file once it's finished copying, trims/overlays/
   compresses it with ffmpeg, and writes the result to `Output_ReadytoSend/`.
4. Open the review app (`http://127.0.0.1:5000`), preview the clip, choose a
   **delivery method** if you want to override the project default, edit the
   recipient email and the email subject/message if needed, and click **Approve**
   (or **Reject**).
5. On Approve: the file uploads to your designated Google Drive folder and the
   file moves to `Email Sent File/`. What happens next depends on the delivery
   method:
   - **Email to client** — a shareable link + QR code appear on screen, and the
     recipient gets a **link-only** email.
   - **Instant QR download (kiosk)** — no email is sent. Instead a full-screen
     kiosk view opens showing a thumbnail of the clip next to a large QR code,
     meant to be shown directly to the client on a monitor at the venue so they
     can scan and download it themselves on the spot (no client email needed).

## `config.json` schema

```json
{
  "recipient_email": "client@example.com",
  "delivery_mode": "email",
  "bitrate": "5M",
  "resolution": "1080x1920",
  "aspect_ratio": "9:16",
  "overlay": "overlays/brand.png",
  "overlay_position": "full",
  "overlay_scale": 20,
  "fps": 30,
  "trim": { "start": "00:00:02", "end": "00:00:30" },
  "overrides": {
    "footage2.mov": { "trim": { "start": "00:00:00", "end": "00:00:12" } }
  }
}
```

- `delivery_mode` (optional, `email` or `qr_only`, default `email`): the
  project's default delivery method — see "Delivery methods" below. This is
  only a **default**; it can be overridden per clip on the review screen.
- `recipient_email` is only a **default** — you can change it per clip on the
  review screen before sending. Required when `delivery_mode` is `email`;
  optional (and can be left blank) when it's `qr_only`.
- `trim` applies to every file in the project unless overridden per filename
  under `overrides`.
- `overlay_position`: `full` (fills the frame), `top-left` / `top-right` /
  `bottom-left` / `bottom-right` (fixed 20px margin), or `custom` (needs
  `overlay_x` / `overlay_y`, 0–100, the overlay's top-left corner as a
  percentage of frame width/height).
- `overlay_scale` (optional, 1–100): resizes the overlay to that percentage of
  the frame width, preserving its own aspect ratio, before placing it. Without
  it, `full` fills the whole frame and corner positions use the overlay's
  native pixel size.
- `fps` (optional): forces an output frame rate. Without it, the source's
  frame rate is kept.
- `overlay` path is relative to the repo root (e.g. put shared graphics in
  `overlays/`).

The **+ New Project** form on the review page generates this file for you —
resolution/fps/bitrate come from dropdowns (with a Custom option for each),
and the overlay position/size/x/y are set with sliders next to a live preview,
so you never have to remember this schema by hand.

### Delivery methods

Every project has a default delivery method (set on the New Project form, or the
`delivery_mode` key in `config.json`), and the operator can switch it per clip on
the review screen before approving:

- **Email to client** — the default. On Approve, the file uploads to Drive and
  the recipient gets a link-only email with an embedded QR code. Requires a
  client email address.
- **Instant QR download (kiosk)** — for in-person events (think arcade/photo-booth
  kiosks). On Approve, the file still uploads to Drive as normal, but **no email
  is sent** and no client email is required. Instead, a full-screen view opens
  showing a thumbnail grabbed from the finished clip next to a large QR code —
  put that on a monitor at the venue and the client scans it themselves to
  download instantly.

## One-time setup

```bash
cp .env.example .env   # then fill in the values below
```

ffmpeg isn't a hard requirement to install separately — if it's not found on `PATH`,
Glambot automatically falls back to the static ffmpeg binary bundled by the
`imageio-ffmpeg` pip package (this works on macOS, Windows, and Linux, via
`./run.sh` / `run.bat` / the Mac app equally). A real system ffmpeg install is
still worth having for one specific reason: thumbnail generation (used by the
Instant QR download / kiosk mode) needs `ffprobe`, which `imageio-ffmpeg` does
**not** bundle — install a full ffmpeg suite (`brew install ffmpeg` on macOS,
`winget install ffmpeg` on Windows) if you want thumbnails for QR-kiosk clips.

### Google Drive (for the download link)

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select a
   project and enable the **Google Drive API**.
2. Create an **OAuth Client ID** of type **Desktop app**, download the JSON, and
   save it as `credentials.json` in the repo root.
3. Create (or reuse) a Drive folder to receive approved deliveries, copy its
   folder ID from the URL, and set `DRIVE_FOLDER_ID` in `.env`.
4. The first time you click Approve, a browser window opens for a one-time
   Google consent; after that, a cached `token.json` keeps it non-interactive.

### Email (SMTP)

Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `FROM_ADDR` in `.env`.
For Google Workspace (`g6moco.com`), use an **app password**, not your normal
login password. If your Workspace admin has disabled app passwords, switch to
Gmail API OAuth instead (not included here, but `glambot/emailer.py` is the
only file that would need to change).

### Default email message

`templates/email_default.txt` is the starting subject/body shown on the review
screen for every clip (fully editable per-send before you approve). It
supports `{project}`, `{filename}`, and `{link}` placeholders — `{link}` is
filled in automatically once the Drive upload finishes, so leave it as-is
unless you want the literal text removed.

## Windows setup

1. Install [Python 3.9+](https://www.python.org/downloads/windows/) — during
   install, check **"Add python.exe to PATH"**.
2. `git clone` the repo (see "Migrating to another machine" below), or copy
   the folder over some other way.
3. Copy in the gitignored secrets from an already-working machine (`.env`,
   `credentials.json`, `token.json`) and your real `overlays/*` images — see
   "Migrating to another machine" for the full list.
4. Double-click `run.bat` (or run it from a Command Prompt/PowerShell window).
   First run creates a virtualenv and installs dependencies, so it'll take a
   minute; after that it starts the review app the same as `./run.sh` does on
   macOS.

There's no Windows equivalent of the double-click Mac `.app` in this repo —
`run.bat` is the Windows launcher; it's plain-double-click-friendly once
Python and `.env` are set up, it just briefly shows a console window.

## Running it

**macOS / Linux:**
```bash
./run.sh
```

**Windows:**
```bat
run.bat
```
(double-clickable from Explorer once Python is installed and `.env` is filled in)

Either script creates a virtualenv, installs dependencies, checks for `.env`, then
starts both the inbox watcher and the review app at `http://127.0.0.1:5000`
(or whatever `HOST`/`PORT` you set in `.env`).

## Trying it without real footage

```bash
source .venv/bin/activate
ffmpeg -f lavfi -i testsrc=duration=20:size=1280x720:rate=30 \
       -f lavfi -i sine=frequency=440:duration=20 \
       -pix_fmt yuv420p inbox/demo/sample.mp4
mkdir -p inbox/demo overlays
# put a transparent PNG at overlays/brand.png, then:
cat > inbox/demo/config.json <<'EOF'
{
  "recipient_email": "you@example.com",
  "bitrate": "3M",
  "resolution": "720x1280",
  "aspect_ratio": "9:16",
  "overlay": "overlays/brand.png",
  "overlay_position": "bottom-right",
  "trim": { "start": "00:00:02", "end": "00:00:12" }
}
EOF
```

Then run `./run.sh` and watch `inbox/demo/Output_ReadytoSend/` appear.

## Status / troubleshooting

- Invalid or missing `config.json` → the clip won't process; the error shows
  up in the review app's **Errors** section once a footage file has landed.
- Failed Drive upload or email send on Approve → the job **stays in the
  review queue** (not moved to Errors) with the failure message shown inline
  on its card, and the file is **not** moved/archived, so you can fix
  credentials/network and just click Approve again.

## macOS: standalone double-click app

If you'd rather not use Terminal, build `Glambot.app` once and just double-click
it going forward — it starts the pipeline in the background and opens the
review page in your browser automatically. No `brew install ffmpeg` needed for
this path either (it falls back to a bundled ffmpeg binary automatically).

```bash
brew install --cask platypus
/bin/sh "/Applications/Platypus.app/Contents/Resources/InstallCommandLineTool.sh" \
  "/Applications/Platypus.app/Contents/Resources"
./mac_app/build_app.sh
```

This builds `~/Applications/Glambot.app`. Since it's unsigned (no Apple
Developer account involved), the **first launch** needs a right-click →
**Open** instead of a plain double-click, to get past Gatekeeper's
"unidentified developer" warning — after that, double-clicking works normally.

Quit the app the normal way (Cmd+Q or Dock → Quit) to stop the watcher and
server cleanly. It still reads the same `.env` / `credentials.json` /
`token.json` / `inbox/` in this repo, so anything already configured for
`./run.sh` carries over unchanged. Re-run `./mac_app/build_app.sh` any time
you pull code changes, to rebuild the app with the latest version.

## Migrating to another machine

The code lives on GitHub (`git remote -v` shows `origin`); to set Glambot up
on another Mac or a Windows PC:

1. **Push the current code** (if you haven't already):
   ```bash
   git add -A
   git commit -m "..."
   git push
   ```
2. **On the new machine**, clone it:
   ```bash
   git clone https://github.com/RammyG6/Glambot.git
   ```
3. **Copy over the secrets that `.gitignore` deliberately keeps out of git** —
   these don't come along with `git clone`, so copy them manually (AirDrop,
   USB, a private cloud folder, etc.):
   - `.env` — your Drive folder ID + SMTP credentials.
   - `credentials.json` — the Google OAuth desktop client secret.
   - `token.json` — the cached Drive consent, so you're not prompted to
     re-authorize on the new machine. (If you skip this, Approve will just
     open a one-time browser consent the first time instead.)
   - `overlays/*` — your real brand overlay images (only `overlays/.gitkeep`
     is tracked in git).
   - `inbox/` — only copy this if you want to carry over in-progress
     footage/job history; otherwise leave it out and start fresh.
4. **Run it**: `./run.sh` on macOS/Linux, `run.bat` on Windows (see "Windows
   setup" above). On a second Mac, you can also rebuild the double-click app
   with `./mac_app/build_app.sh` — it now bakes in whatever path it's built
   from, so this works correctly on any Mac, not just the original one.
