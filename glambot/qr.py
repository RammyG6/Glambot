"""Render a URL to a QR code PNG for on-screen scan-to-download."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image


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


def make_delivery_photo(thumbnail_path: Path, url: str) -> bytes:
    """Compose a single portrait JPEG: the video thumbnail on top, a white
    panel with the QR code centered underneath — a printable/shareable
    "instant download" photo for a live kiosk setup."""
    thumb = Image.open(thumbnail_path).convert("RGB")
    target_w = 800
    scale = target_w / thumb.width
    thumb = thumb.resize((target_w, round(thumb.height * scale)))

    qr_img = qrcode.make(url).convert("RGB")
    qr_size = int(target_w * 0.55)
    qr_img = qr_img.resize((qr_size, qr_size))

    margin = 40
    panel_h = qr_size + margin * 2
    canvas = Image.new("RGB", (target_w, thumb.height + panel_h), "white")
    canvas.paste(thumb, (0, 0))
    qr_x = (target_w - qr_size) // 2
    qr_y = thumb.height + margin
    canvas.paste(qr_img, (qr_x, qr_y))

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
