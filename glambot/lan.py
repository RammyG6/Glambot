"""Helpers for serving guests directly over a local network (a travel router
or the PC's own Wi-Fi hotspot) when there is no internet for Google Drive.

Nothing here opens a socket - `pipeline.py` / `glambot_launcher.py` already bind
the one Flask server. This module just works out what URL a phone on the same
network should point at, and builds the two QR payloads a delivery photo needs.
"""
from __future__ import annotations

import os
import secrets
import socket


def lan_ip() -> str:
    """This machine's address on the local network. Uses the standard
    UDP-socket trick (no packet is actually sent) to find which local interface
    would be used to reach the outside world, which is almost always the one
    the router/hotspot handed out. Falls back to the hostname lookup, then
    loopback."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def _port() -> str:
    return os.environ.get("PORT", "5000")


def public_base_url() -> str:
    """Base URL to bake into guest links / QR codes. An explicit
    PUBLIC_BASE_URL wins (for a fixed hotspot gateway IP or an mDNS name);
    otherwise auto-detect the LAN IP and current port."""
    override = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"http://{lan_ip()}:{_port()}"


def wifi_qr_payload(ssid: str, password: str, auth: str = "WPA") -> str:
    """The `WIFI:` string iOS/Android camera apps recognise as "join this
    network". Special characters in the SSID/password must be backslash-escaped."""
    def esc(v: str) -> str:
        for ch in ("\\", ";", ",", ":", '"'):
            v = v.replace(ch, "\\" + ch)
        return v

    if not password:
        return f"WIFI:T:nopass;S:{esc(ssid)};;"
    return f"WIFI:T:{auth};S:{esc(ssid)};P:{esc(password)};;"


def mint_download_token() -> str:
    """Unguessable per-clip token for the guest download URL."""
    return secrets.token_urlsafe(12)


def wifi_ssid() -> str:
    return os.environ.get("LAN_SSID", "").strip()


def wifi_password() -> str:
    return os.environ.get("LAN_PASSWORD", "")
