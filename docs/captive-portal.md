# One-scan guest joining — captive portal

Goal: a guest scans **one** QR (the Join-Wi-Fi QR), joins the event Wi-Fi, and
their phone automatically opens the Glambot download gallery — no second scan,
no typing a URL.

The only way to get true "one scan" is a **captive portal** on the network the
guest joins — so joining the Wi-Fi auto-opens a page. Glambot itself can't do
this (the PC is a client of the router, not the gateway), so it has to be set on
the router, or on a small dedicated guest router (see Fallback B).

## Fill in your values

| Placeholder | Where to find it |
|---|---|
| `<PC_IP>` | `ipconfig` on the PC — the IPv4 of the adapter connected to the event router. Also the host in `PUBLIC_BASE_URL` in `.env`. |
| `<PC_MAC>` | `ipconfig /all` — the Physical Address of that same adapter. |
| `<PORT>` | `PORT` in `.env` (default `5000`). |
| `<SSID>` / `<WIFI_PASSWORD>` | `LAN_SSID` / `LAN_PASSWORD` in `.env`. |
| Router admin | usually `http://192.168.<x>.1` or router.asus.com. |

Landing page: **`http://<PC_IP>:<PORT>/welcome`** — redirects to the running
event's gallery (a short chooser if more than one LAN/offline project exists).

> **Heads-up.** ASUS's Guest-Network **Captive Portal / "Free Wi-Fi"** is a
> newer / business-router feature. Older models (e.g. RT-AC58U) may have **no
> captive-portal option at all** — Step 3 is a checkpoint; if it's not there,
> jump to **Fallbacks**.

---

## Step 0 — Get Glambot running and reachable

1. Start Glambot (`Glambot.bat`).
2. On the PC, open `http://<PC_IP>:<PORT>/welcome` — it should load the event
   gallery.
3. On a phone that's on your **normal** Wi-Fi, open the same URL. If it loads,
   the PC is reachable on the LAN and the firewall is fine. If not, check the
   inbound rule: `netsh advfirewall firewall show rule name="Glambot LAN"`
   should be **Enabled: Yes**, LocalPort `<PORT>`, Profiles Domain,Private,Public.

Don't touch the router until this works.

---

## Step 1 — Give the PC a permanent IP

So the portal URL never breaks.

1. router.asus.com → sign in.
2. **LAN** → **DHCP Server** tab.
3. **Enable Manual Assignment**: **Yes**.
4. Add: MAC `<PC_MAC>` → IP `<PC_IP>`, click **+**.
5. **Apply**.

---

## Step 2 — Create the guest network

1. **Guest Network** (left menu).
2. In the **2.4 GHz** section, click an empty slot → **Enable**.
3. Fill in:
   - **Network Name (SSID)**: `<SSID>`
   - **Authentication Method**: `WPA2-Personal`
   - **WPA Pre-Shared Key**: `<WIFI_PASSWORD>`
   - **Access time**: **Limitless**
   - **Access Intranet**: **Enable**  ← important. If this is *Disable*, guest
     phones can't reach the PC and the portal page won't load.
4. **Apply**.
5. Do the **same in the 5 GHz section** — same SSID, same password, Access
   Intranet **Enable**.

Guests can now join `<SSID>` and reach `http://<PC_IP>:<PORT>/welcome` — but
they'd still have to open it themselves. Step 3 makes it automatic.

---

## Step 3 — CHECKPOINT: turn on the captive portal

Look on the **Guest Network** page (and any **Captive Portal** / **Free Wi-Fi**
tab) for **"Enable captive portal"**, **"Free Wi-Fi"**, or **"Captive Portal"**.

### If you see it

1. Enable it and attach it to the `<SSID>` guest network.
2. Terms-of-service / splash text: optional.
3. Find the field named **"Redirect"**, **"Redirection URL"**, **"Landing
   page"**, or **"The URL clients see after they sign in"** and set it to:

   ```
   http://<PC_IP>:<PORT>/welcome
   ```
4. **Apply.**

### If you don't see it

This firmware doesn't support a captive portal → go to **Fallbacks**. Guests
will just scan two QRs (or you add a small guest router).

---

## Step 4 — Test with a phone

1. On the phone: **forget** any saved `<SSID>` network.
2. Join `<SSID>` (scan the kiosk's "Join Wi-Fi" QR, or pick it from the list).
3. Within a few seconds a sign-in sheet should pop up showing the Glambot
   gallery. Tap a clip.
4. If the video download stalls inside that pop-up (it's a cut-down browser),
   use the share icon → **Open in Safari / Chrome** and download there.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sign-in sheet never appears | Phone cached "already signed in". Forget the network and rejoin, or toggle Wi-Fi off/on. iOS: Settings → Wi-Fi → ⓘ next to the SSID. |
| Sheet appears but page won't load | *Access Intranet* is not **Enable** on the guest network; or Glambot isn't running; or the "Glambot LAN" firewall rule is off. |
| Redirect goes to the router's own page, not the gallery | Firmware has no post-sign-in redirect field → use a Fallback. |
| Works on 5 GHz phones only (or 2.4 only) | You only enabled the guest network on one band. Do both. |
| IP changed, portal broke | Redo Step 1 (manual IP assignment). |

---

## Fallbacks

### A. Two QRs — works right now, nothing to set up

Glambot already shows a **"Join Wi-Fi" QR** on the kiosk screen and every
delivery photo, next to the clip's download QR. The guest scans the Wi-Fi QR
**once** on arrival, then one scan per clip. Not "one scan total", but the Wi-Fi
step is one-time.

### B. Add a GL.iNet travel router as the guest access point — recommended for events

A ~£30 GL.iNet router (e.g. **GL-MT300N-V2 "Mango"**, or **GL-AXT1800 "Slate
AX"**) runs OpenWrt with a **built-in captive portal**.

1. Plug the GL.iNet **WAN** port into a spare LAN port on the event router.
2. In the GL.iNet admin (`http://192.168.8.1`): set its Wi-Fi SSID/password to
   your `<SSID>` / `<WIFI_PASSWORD>`.
3. **Applications → Captive Portal** (or install `opennds`): enable it, set the
   "after authentication" URL to `http://<PC_IP>:<PORT>/welcome`.
4. Put the GL.iNet in **Access Point / Bridge mode**, or add a static route, so
   guest clients can reach `<PC_IP>`.

Because the GL.iNet is the gateway for guests, the captive portal is reliable
(this is how hotels do it).

### C. PC as the Wi-Fi hotspot + Glambot's own captive portal — last resort, needs code

Switch from the router to **Windows Mobile Hotspot** so the PC is the gateway.
Glambot would then need a small addition: bind port 80 and answer the OS "is
there internet?" probe URLs (`/hotspot-detect.html`, `/generate_204`,
`/ncsi.txt`, `/connecttest.txt`) with a redirect to `/welcome`. Downsides:
Windows Mobile Hotspot caps at ~8 devices, and it's a separate build.

---

## Note on the captive-portal mini-browser

On iOS and Android the captive-portal pop-up is a stripped-down webview: no
tabs, limited downloads, and it closes once the phone decides it has internet.
It's great for **showing** the gallery and previewing a clip, but for the actual
save-to-camera-roll the guest often needs "Open in Safari/Chrome". The captive
portal removes the second *scan*, not always the extra tap.
