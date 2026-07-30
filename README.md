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
soundtracks/
  upbeat.mp3                      # reusable background-music library
```

1. Set up a project's `config.json` — either hand-write it (schema below), or
   click **+ New Project** on the review page for a form with resolution/fps/
   bitrate dropdowns, a client email field, and a live preview for sizing and
   placing the overlay graphic. Either way it lands at `inbox/<project>/config.json`.
2. Drop footage into that `inbox/<project>/` folder (Finder drag-and-drop is
   fine — the form doesn't upload video files, only the config).
3. The watcher notices the new file once it's finished copying, trims/overlays/
   compresses it with ffmpeg, and writes the result to `Output_ReadytoSend/`.
   The **original** is then moved into an `Edited Footages/` subfolder inside
   its own import folder — so the drop folder stays clean and processed files
   are never picked up again.
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
  "auto_deliver": false,
  "bitrate": "5M",
  "resolution": "1080x1920",
  "aspect_ratio": "9:16",
  "second_resolution": "1920x1080",
  "second_bitrate": "8M",
  "overlay": "overlays/brand.png",
  "overlay_position": "full",
  "overlay_scale": 20,
  "fps": 30,
  "rotation": 0,
  "position_x": 0,
  "position_y": 0,
  "soundtrack": "soundtracks/upbeat.mp3",
  "soundtrack_volume_db": -3,
  "original_volume_db": -60,
  "soundtrack_trim": { "start": "00:00:10", "end": "00:00:40" },
  "source_dir": "/Users/you/Desktop/sd-card-import",
  "drive_folder_id": "1AbCdEfGhIjKlmNoPQRstuVwxyz",
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
- `auto_deliver` (optional, default `false`): skip the manual Approve step
  entirely — see "Full automation" below.
- `trim` is now fully **optional** — leave `start`/`end` blank or omit `trim`
  entirely to export the full clip untrimmed. It applies to every file in the
  project unless overridden per filename under `overrides`.
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
- `rotation` (optional, one of `0`, `90`, `-90`, `180`, default `0`): rotates
  the source before scaling/cropping.
- `position_x` / `position_y` (optional, pixels, default `0`): pans which part
  of the source frame is kept by the fill-crop, instead of always cropping
  dead-center. Never introduces blank space — an extreme value just clamps to
  the edge of the source frame.
- `second_resolution` / `second_bitrate` (optional): export a second
  resolution/aspect ratio alongside the primary one from the same clip — see
  "Dual-resolution export" below.
- `soundtrack` / `soundtrack_volume_db` / `original_volume_db` /
  `soundtrack_trim` (all optional): mix in background music — see
  "Soundtrack" below.
- `source_dir` (optional, absolute path): watch an arbitrary folder on this
  machine for footage instead of `inbox/<project>/` — see "Custom footage
  source folder" below.
- `drive_folder_id` (optional): upload this project's clips to a specific
  Google Drive folder instead of the default one set by `DRIVE_FOLDER_ID` in
  `.env`. Accepts either the bare folder ID or its full share URL.
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

### Full automation

Check **"Fully automatic delivery"** on the New Project form (or set
`"auto_deliver": true` in `config.json`) to skip the manual Approve step
entirely — as soon as a clip finishes processing, it uploads and delivers
itself automatically, using the project's default recipient/email template
(for `email` mode) or generating the kiosk QR photo straight away (for
`qr_only` mode). Works with either delivery method.

Since there's no Approve click for `qr_only` + full automation, open
**`http://127.0.0.1:5000/projects/<project name>/kiosk`** on the venue
monitor instead — a self-updating page (every 5s, no full reload so playback
isn't interrupted) with a grid of every delivered clip's thumbnail + QR code
on the left and, on the right, a **playback panel** that auto-plays (muted,
looping) the single newest clip, switching to a newer one as it lands.

Clips delivered via QR aren't emailed, but you can email any of them after
the fact from **"Email a delivered clip"** on the review page (`/clips`):
pick a clip, enter a recipient/subject/message, and Send — it emails the
clip's existing Drive link (with QR), no re-upload or re-processing.

For this exact combination (`qr_only` + `auto_deliver`), the video,
thumbnail, and the composite QR-code photo all stay together in the
project's `Output/` folder — there's no manual review step "selecting"
anything out of it, so nothing moves to `Selected Output/`/`Instant
Download/` the way a manually-approved `qr_only` clip does.

If an automatic delivery fails (bad credentials, no network, …), the clip
simply stays in the normal review queue with the error shown inline — exactly
like a manual delivery failure — so you can fix the problem and click Approve
once by hand.

### Rotation & repositioning

`rotation` quickly rotates the source ±90°/180° before it's scaled and
cropped to the target resolution. `position_x`/`position_y` (pixels) pan
*which part* of the source frame the fill-crop keeps, instead of always
cropping dead-center — useful when the subject isn't centered in the raw
footage. This never adds blank/letterboxed space: an extreme offset just
clamps to the edge of the available frame.

### Dual-resolution export

Check **"Also export a second resolution"** on the New Project form to
produce two output files from the same source clip in one pass (e.g. a 9:16
vertical cut and a 16:9 horizontal cut). Both stay on **one review card** —
a single Approve click uploads and delivers both together: one email with
both links (add `{link2}` to your subject/body template to place it
precisely, otherwise it's appended automatically), or a kiosk screen showing
two QR codes side by side.

### Soundtrack

Add background music per project: pick **"Upload new file…"** on the New
Project form the first time (saved into a shared `soundtracks/` library) or
choose an already-uploaded track from the dropdown on any later project.
`soundtrack_volume_db` / `original_volume_db` (-60 to +12 dB, default `0`)
balance the music against the clip's own audio — mix both, or push one all
the way down to effectively mute it. `soundtrack_trim` optionally selects
just a portion of a longer track. If the source clip has no audio track at
all, the soundtrack plays alone automatically.

### Custom footage source folder

By default, footage must be dropped into `inbox/<project>/`. Set a
**"Footage source folder"** on the New Project form (with an in-app folder
browser, or just paste/type a path) to instead watch an arbitrary folder
anywhere on this machine — e.g. an SD-card import folder or a Dropbox folder
— while `config.json` and the processed output still live under
`inbox/<project>/` as normal. New/changed source folders are picked up
automatically within about 10 seconds, no restart needed.

**Sharing one folder across multiple projects:** you can point several
projects at the exact same folder (handy for comparing different settings
against the same test clips), but only **one** of them is ever actively
watched at a time — footage dropped there always goes to whichever project
is currently "active" for that folder, never to more than one. The review
page shows a **"Shared footage folders"** section whenever this happens,
listing every project pointing at that folder with a **"Make active"**
button to switch which one receives new footage. The choice is sticky (saved
until you change it again), not silently decided alphabetically.

### Per-project Google Drive folder

By default every project uploads to the single folder set by
`DRIVE_FOLDER_ID` in `.env`. Set **"Google Drive destination folder"** on the
New Project form to send a specific project's clips to a different Drive
folder instead — open the folder in Drive, copy its URL (or just the ID from
the end of it), and paste it in. Leave it blank to keep using the default.

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
4. **(Recommended)** install a full **ffmpeg** so `ffprobe` is available:
   `winget install Gyan.FFmpeg` (then reopen your terminal so it's on PATH).
   The app runs *without* it — it falls back to the bundled `imageio-ffmpeg`
   binary — but that bundle ships only `ffmpeg`, not `ffprobe`, and `ffprobe`
   is what powers **thumbnails/kiosk previews, the live progress %, and
   audio-stream detection** (needed if you mix a soundtrack onto a silent
   clip). Core trimming/overlay/compress works either way.
5. Double-click `Glambot.bat`. First run creates a virtualenv and installs
   dependencies, so it'll take a minute; after that it starts the review app
   the same as `./run.sh` does on macOS, and opens your browser to it
   automatically once it's ready.

`Glambot.bat` is the closest Windows equivalent of the double-click Mac
`.app`: it keeps a console window open (so you can see progress/logs and read
any error before it closes) but auto-opens the review UI in your browser once
the server responds. `run.bat` still exists underneath it with the same
venv/install/`.env`-check logic, without the friendly title, auto-opened
browser, or pause-on-error — handy if you'd rather run it from a terminal.

Notes for Windows:
- Set `INBOX_DIR` in `.env` to a Windows path, e.g. `INBOX_DIR=C:\Glambot\project`.
- The New Project form's **folder browser** starts at your home folder and
  can't hop to other drive letters — to point a project at a folder on another
  drive (e.g. `D:\footage`), just **paste that path** into the field instead of
  browsing.

## Running it

**macOS / Linux:**
```bash
./run.sh
```

**Windows:**
```bat
Glambot.bat
```
(double-clickable from Explorer once Python is installed and `.env` is filled in — opens your
browser automatically once the app is ready; see "Windows setup" above)

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

- The review page (titled **Glambot Automation**) shows a **Now processing**
  section with a live percentage progress bar for whatever clip ffmpeg is
  currently rendering in the background — it fills in real time and, for a
  dual-resolution project, spans both render passes 0→100%.
- Two collapsible logs at the bottom of the review page (hidden by default,
  each remembers its open/closed state): the **output log** lists every clip
  that's been processed/edited regardless of delivery mode, and the **email
  sent log** lists only clips delivered via email mode (with recipient +
  Drive link).
- The monitoring page (`/projects/<name>/kiosk`) shows **all** of a project's
  delivered clips as a newest-first grid of thumbnail + QR code(s), refreshing
  itself every few seconds — a "wall of scan-your-clip" for a venue.
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
4. **Run it**: `./run.sh` on macOS/Linux, `Glambot.bat` on Windows (see "Windows
   setup" above). On a second Mac, you can also rebuild the double-click app
   with `./mac_app/build_app.sh` — it now bakes in whatever path it's built
   from, so this works correctly on any Mac, not just the original one.
