"""Render a URL to a QR code PNG for on-screen scan-to-download."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont


def make_qr_png_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_qr_data_uri(url: str) -> str:
    """A data: URI suitable for embedding directly in an <img src=...> in
    both the review app and the outgoing email."""
    png_bytes = make_qr_png_bytes(url)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def make_delivery_photo(thumbnail_path: Path, url: str, *, url2: str | None = None,
                         label: str | None = None, label2: str | None = None) -> bytes:
    """Compose a single portrait JPEG: the video thumbnail on top, a white
    panel with the QR code(s) underneath — a printable/shareable "instant
    download" photo for a live kiosk setup. When `url2` is given (a project
    exporting two resolutions), both QR codes are shown side by side, each
    with its own optional caption (e.g. the aspect ratio)."""
    thumb = Image.open(thumbnail_path).convert("RGB")
    target_w = 800
    scale = target_w / thumb.width
    thumb = thumb.resize((target_w, round(thumb.height * scale)))

    margin = 40
    label_h = 30 if (label or label2) else 0
    font = ImageFont.load_default()

    def _qr(u: str, size: int) -> Image.Image:
        return qrcode.make(u).convert("RGB").resize((size, size))

    if url2:
        qr_size = int(target_w * 0.38)
        gap = margin
        qr1, qr2 = _qr(url, qr_size), _qr(url2, qr_size)
        row_w = qr_size * 2 + gap
        panel_h = qr_size + label_h + margin * 2
        canvas = Image.new("RGB", (target_w, thumb.height + panel_h), "white")
        canvas.paste(thumb, (0, 0))
        start_x = (target_w - row_w) // 2
        qr_y = thumb.height + margin + label_h
        canvas.paste(qr1, (start_x, qr_y))
        canvas.paste(qr2, (start_x + qr_size + gap, qr_y))
        if label or label2:
            draw = ImageDraw.Draw(canvas)
            label_y = thumb.height + margin // 2
            if label:
                draw.text((start_x + qr_size // 2, label_y), label, fill="black", font=font, anchor="mm")
            if label2:
                draw.text((start_x + qr_size + gap + qr_size // 2, label_y), label2, fill="black", font=font, anchor="mm")
    else:
        qr_size = int(target_w * 0.55)
        qr1 = _qr(url, qr_size)
        panel_h = qr_size + label_h + margin * 2
        canvas = Image.new("RGB", (target_w, thumb.height + panel_h), "white")
        canvas.paste(thumb, (0, 0))
        qr_x = (target_w - qr_size) // 2
        qr_y = thumb.height + margin + label_h
        canvas.paste(qr1, (qr_x, qr_y))
        if label:
            draw = ImageDraw.Draw(canvas)
            draw.text((target_w // 2, thumb.height + margin // 2), label, fill="black", font=font, anchor="mm")

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
