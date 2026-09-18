"""Optional PIN gate for when Glambot is reachable beyond localhost.

Glambot has always been a single-machine, no-auth local app. These features
(LAN guest downloads, iPad control) mean the same Flask process now answers
requests from other devices on the network, so it needs a way to keep the
operator UI private without getting in the way of the normal local setup.

Design:
  * If GLAMBOT_PIN is unset, nothing changes - every request is allowed, exactly
    like before. Setting a PIN is opt-in and only matters once BIND_HOST exposes
    the app to the LAN.
  * Loopback requests (127.0.0.1 / ::1) are always allowed. That's how the
    packaged pywebview window and a browser tab on the Glambot PC itself keep
    working with zero configuration.
  * The guest blueprint runs its own per-project download PIN (see guest.py);
    this module defers to it rather than applying the operator PIN there.

One `before_request` hook is the entire enforcement surface.
"""
from __future__ import annotations

import hmac
import logging
import os
from urllib.parse import urlparse

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

logger = logging.getLogger(__name__)

# Endpoints reachable without a session - the login page itself, static assets,
# and the PWA manifest/icon (Safari fetches those before the user can log in).
_ALWAYS_ALLOWED = {"static", "auth.login", "auth.logout", "glambot_manifest", "app_icon"}

_LOOPBACK_ADDRS = {"127.0.0.1", "::1", "localhost"}

auth_bp = Blueprint("auth", __name__)


def pin_configured() -> bool:
    return bool(os.environ.get("GLAMBOT_PIN", "").strip())


def check_pin(candidate: str) -> bool:
    expected = os.environ.get("GLAMBOT_PIN", "").strip()
    if not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


def is_loopback(req) -> bool:
    return (req.remote_addr or "") in _LOOPBACK_ADDRS


def _safe_next(raw: str | None) -> str:
    """Only ever redirect to a path on this same host - never an absolute URL a
    crafted ?next= could point at."""
    if not raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return "/"
    return raw if raw.startswith("/") else "/"


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not pin_configured():
        return redirect("/")
    nxt = _safe_next(request.args.get("next") or request.form.get("next"))
    error = None
    if request.method == "POST":
        if check_pin(request.form.get("pin", "")):
            session["op_authed"] = True
            session.permanent = True
            return redirect(nxt)
        error = "Wrong PIN."
    return render_template("login.html", error=error, next=nxt), (401 if error else 200)


@auth_bp.post("/logout")
def logout():
    session.pop("op_authed", None)
    return redirect(url_for("auth.login"))


def init_auth(app) -> None:
    """Register the single before_request enforcement hook."""
    app.register_blueprint(auth_bp)

    @app.before_request
    def _require_operator_pin():
        endpoint = request.endpoint or ""
        if endpoint in _ALWAYS_ALLOWED:
            return None
        if is_loopback(request):
            return None
        # The guest blueprint gates itself per-project (guest.py).
        if request.blueprint == "guest":
            return None
        if not pin_configured():
            return None
        if session.get("op_authed") is True:
            return None
        if request.method == "GET":
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return ("PIN required.", 401)
