"""Built-in FTP server for auto-importing footage straight off a camera
(e.g. a Sony FX6's FTP push), replacing a separately-run FileZilla Server.

Nothing here touches the review/render pipeline: incoming files simply land in
a plain folder on disk. Point a project's footage source folder (config
`source_dir`) at that same folder and the existing `InboxWatcher` picks the
clips up exactly as if they'd been copied in by hand — `watcher.py` already
has FTP-aware completion handling (`_is_locked`, size-settle).

Settings are UI-managed (see the "FTP import" tab in app.py) and persisted to
`inbox_dir/.glambot/ftp_import.json`, next to jobs.sqlite.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import lan

logger = logging.getLogger(__name__)

# Full read/write/delete/rename/mkdir permissions - a camera push needs to
# create dirs and overwrite partial files on retry. See pyftpdlib docs.
_FULL_PERM = "elradfmwMT"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "port": 2121,
    "root_dir": r"D:\GlambotAuto_Import",
    "anonymous": True,
    "username": "glambot",       # blank always resolves back to this
    "password": "",              # stored as-is (local-only app, same posture as .env)
    "passive_host": "",          # "" => auto-detect this PC's LAN IP
    "passive_ports": "50000-50050",
}

DEFAULT_USERNAME = "glambot"

_FIREWALL_RULE_NAME = "Glambot FTP import"


def _settings_path(inbox_dir: Path) -> Path:
    return Path(inbox_dir) / ".glambot" / "ftp_import.json"


def load_ftp_settings(inbox_dir: Path) -> dict[str, Any]:
    """Current settings, with every default filled in for missing keys."""
    merged = dict(DEFAULT_SETTINGS)
    path = _settings_path(inbox_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read %s - using defaults", path)
    if not str(merged.get("username", "")).strip():
        merged["username"] = DEFAULT_USERNAME
    return merged


def save_ftp_settings(inbox_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge `updates` onto the stored settings and write them back."""
    current = load_ftp_settings(inbox_dir)
    current.update({k: v for k, v in updates.items() if k in DEFAULT_SETTINGS})
    if not str(current.get("username", "")).strip():
        current["username"] = DEFAULT_USERNAME
    path = _settings_path(inbox_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def parse_passive_ports(raw: str) -> tuple[int, int] | None:
    """'50000-50050' -> (50000, 50050); None if malformed / out of range."""
    try:
        lo_s, hi_s = str(raw).split("-", 1)
        lo, hi = int(lo_s.strip()), int(hi_s.strip())
    except (ValueError, AttributeError):
        return None
    if not (1024 <= lo < hi <= 65535):
        return None
    return lo, hi


# --- Windows Defender Firewall helpers -----------------------------------

def _firewall_local_ports(port: int, passive: tuple[int, int]) -> str:
    return f"{port},{passive[0]}-{passive[1]}"


def firewall_command(port: int, passive: tuple[int, int]) -> str:
    """The netsh line that opens the FTP control + passive data ports. Shown
    on the page as a copy-paste fallback / on non-Windows."""
    return (
        f'netsh advfirewall firewall add rule name="{_FIREWALL_RULE_NAME}" '
        f'dir=in action=allow protocol=TCP localport={_firewall_local_ports(port, passive)}'
    )


def firewall_rule_state(port: int, passive: tuple[int, int]) -> str:
    """'ok' | 'missing' | 'stale' | 'unsupported'. Read-only - no admin
    rights needed for `show rule`."""
    if os.name != "nt":
        return "unsupported"
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={_FIREWALL_RULE_NAME}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unsupported"
    out = (result.stdout or "")
    if result.returncode != 0 or "No rules match" in out:
        return "missing"
    # Find the LocalPort line and check the current ports are all covered.
    want = {str(port)}
    want |= {str(p) for p in range(passive[0], passive[1] + 1)}
    # `show rule name=X` can print several rule blocks (e.g. an old stale one
    # plus a fresh one) each with its own LocalPort line - the rule set is
    # "ok" if ANY block covers the ports we need.
    found_localport = False
    for line in out.splitlines():
        if line.strip().lower().startswith("localport"):
            found_localport = True
            spec = line.split(":", 1)[1].strip()
            covered: set[str] = set()
            for chunk in spec.replace(" ", "").split(","):
                if "-" in chunk:
                    try:
                        a, b = (int(x) for x in chunk.split("-", 1))
                        covered |= {str(p) for p in range(a, b + 1)}
                    except ValueError:
                        pass
                elif chunk.lower() == "any":
                    return "ok"
                else:
                    covered.add(chunk)
            if want <= covered:
                return "ok"
    return "stale" if found_localport else "missing"


def apply_firewall_rule(port: int, passive: tuple[int, int]) -> str | None:
    """Add/refresh the inbound rule, elevating via a single UAC prompt.
    Returns None once the elevated run has finished (success is then confirmed
    by re-polling `firewall_rule_state`), or an error string.

    Implemented via a throwaway .bat run elevated with `Start-Process -Verb
    RunAs -Wait`: cmd's `;` is not a separator and nested quote-escaping
    through `powershell -Command "... -ArgumentList '...'"` mangles the rule
    name, so a plain batch file with ordinary quotes is the reliable path."""
    if os.name != "nt":
        return "Automatic firewall changes are only supported on Windows."
    local_ports = _firewall_local_ports(port, passive)
    import tempfile

    bat = Path(tempfile.gettempdir()) / f"glambot_fw_{os.getpid()}.bat"
    bat.write_text(
        "@echo off\r\n"
        f'netsh advfirewall firewall delete rule name="{_FIREWALL_RULE_NAME}" >nul 2>&1\r\n'
        f'netsh advfirewall firewall add rule name="{_FIREWALL_RULE_NAME}" '
        f'dir=in action=allow protocol=TCP localport={local_ports}\r\n',
        encoding="ascii",
    )
    ps = (
        f"Start-Process -Verb RunAs -WindowStyle Hidden -Wait "
        f"-FilePath '{bat}'"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not launch the elevated helper: {exc}"
    finally:
        bat.unlink(missing_ok=True)
    if result.returncode != 0:
        err = (result.stderr or "")
        if "1223" in err or "canceled" in err.lower() or "cancelled" in err.lower():
            return "Firewall change was cancelled at the Windows prompt."
        return err.strip() or "The elevated firewall command failed."
    return None


class FtpImportServer:
    """Wraps pyftpdlib's threaded FTP server with start/stop/restart and a
    small in-memory activity view for the status page. Every operation is
    guarded so an FTP problem can never take down pipeline startup or a Flask
    request - callers get an error string back instead of an exception."""

    def __init__(self, inbox_dir: Path, watcher: Any = None):
        self.inbox_dir = Path(inbox_dir)
        self.watcher = watcher
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # One row per client IP (not per FTP session - a camera opens a fresh
        # session per file, which would otherwise spam the log).
        self._clients: dict[str, dict[str, Any]] = {}
        self._uploads: deque[dict[str, Any]] = deque(maxlen=100)
        self._sessions = 0
        self._uploads_total = 0
        self._firewall_state = "unsupported"
        self._firewall_checked_at = 0.0
        # Live values captured at start(), so the page reflects exactly what
        # the running server is using rather than the on-disk settings.
        self._running: dict[str, Any] | None = None

    # -- activity (called from handler callbacks, any thread) -----------

    def _note_connect(self, ip: str) -> None:
        row = self._clients.get(ip)
        if row is None:
            row = {"ip": ip, "connections": 0, "last_t": 0.0}
            self._clients[ip] = row
        row["connections"] += 1
        row["last_t"] = time.time()

    def _note_upload(self, path: str, ip: str) -> None:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        self._uploads_total += 1
        self._uploads.appendleft({"name": Path(path).name, "bytes": size, "ip": ip, "t": time.time()})

    # -- lifecycle ------------------------------------------------------

    def start(self, settings: dict[str, Any] | None = None) -> str | None:
        """Start the server. Returns None on success, or an error string."""
        with self._lock:
            if self._server is not None:
                return None
            cfg = settings or load_ftp_settings(self.inbox_dir)
            root = Path(str(cfg.get("root_dir", "")).strip())
            if not str(root):
                return "No import folder set."
            if not root.is_dir():
                return f"Import folder does not exist: {root}"

            ports = parse_passive_ports(cfg.get("passive_ports", ""))
            if ports is None:
                return "Passive port range must look like '50000-50050' (1024-65535, low < high)."
            try:
                port = int(cfg.get("port", 2121))
            except (TypeError, ValueError):
                return "Port must be a number."
            if not (1 <= port <= 65535):
                return "Port must be between 1 and 65535."

            anonymous = bool(cfg.get("anonymous", True))
            username = str(cfg.get("username", "")).strip() or DEFAULT_USERNAME
            password = str(cfg.get("password", ""))

            try:
                from pyftpdlib.authorizers import DummyAuthorizer
                from pyftpdlib.handlers import FTPHandler
                from pyftpdlib.servers import ThreadedFTPServer
            except ImportError:
                return "pyftpdlib is not installed - run: pip install -r requirements.txt"

            # A client that sends an empty USER (some cameras don't send one at
            # all) is normalised to a real account for every lookup pyftpdlib
            # does: to "anonymous" when anonymous upload is on (any/no password),
            # otherwise to the configured user (its password still enforced).
            blank_alias = "anonymous" if anonymous else username

            class _Authorizer(DummyAuthorizer):
                def _n(self, u):
                    return blank_alias if u == "" else u

                def validate_authentication(self, u, p, h):
                    return super().validate_authentication(self._n(u), p, h)

                def has_user(self, u):
                    return super().has_user(self._n(u))

                def get_home_dir(self, u):
                    return super().get_home_dir(self._n(u))

                def has_perm(self, u, perm, path=None):
                    return super().has_perm(self._n(u), perm, path)

                def get_perms(self, u):
                    return super().get_perms(self._n(u))

                def get_msg_login(self, u):
                    return super().get_msg_login(self._n(u))

                def get_msg_quit(self, u):
                    return super().get_msg_quit(self._n(u))

            authorizer = _Authorizer()
            root_str = str(root)
            if anonymous:
                authorizer.add_anonymous(root_str, perm=_FULL_PERM)
            try:
                authorizer.add_user(username, password or username, root_str, perm=_FULL_PERM)
            except ValueError as exc:
                return f"Bad username/password: {exc}"

            server_self = self

            class _Handler(FTPHandler):
                def pre_process_command(self, line, cmd, arg):
                    # Some cameras send a literal empty `USER` (or none at all
                    # before PASS). Rewrite it to the alias account so the
                    # login goes through instead of "501 needs an argument".
                    if cmd == "USER" and not arg:
                        return super().pre_process_command(
                            "USER " + blank_alias, "USER", blank_alias)
                    return super().pre_process_command(line, cmd, arg)

                def on_connect(self):
                    server_self._sessions += 1
                    server_self._note_connect(self.remote_ip)

                def on_disconnect(self):
                    server_self._sessions = max(0, server_self._sessions - 1)

                def on_file_received(self, file):
                    server_self._note_upload(file, self.remote_ip)
                    if server_self.watcher is not None:
                        try:
                            server_self.watcher.rescan_now()
                        except Exception:
                            logger.exception("watcher.rescan_now() after FTP upload failed")

            handler = _Handler
            handler.authorizer = authorizer
            passive_host = str(cfg.get("passive_host", "")).strip()
            handler.masquerade_address = passive_host or lan.lan_ip()
            handler.passive_ports = list(range(ports[0], ports[1] + 1))
            handler.banner = "Glambot FTP import ready."

            try:
                self._server = ThreadedFTPServer(("0.0.0.0", port), handler)
            except OSError as exc:
                self._server = None
                return f"Could not bind port {port}: {exc}"

            self._thread = threading.Thread(
                target=self._serve_forever, name="ftp-import", daemon=True
            )
            self._thread.start()
            self._running = {
                "port": port,
                "root_dir": root_str,
                "anonymous": anonymous,
                "username": username,
                "passive_host": passive_host,
                "passive_ports": f"{ports[0]}-{ports[1]}",
                "connect_host": passive_host or lan.lan_ip(),
                "_ports_tuple": ports,
            }
            # Best-effort silent attempt (works if Glambot is already
            # elevated); the page offers a UAC-prompt button otherwise.
            self._try_firewall_silent(port, ports)
            self._firewall_state = firewall_rule_state(port, ports)
            self._firewall_checked_at = time.time()
            logger.info("FTP import server listening on 0.0.0.0:%s -> %s", port, root_str)
            return None

    def _serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        except Exception:
            logger.exception("FTP import server crashed")

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            try:
                self._server.close_all()
            except Exception:
                logger.exception("Error stopping FTP import server")
            self._server = None
            self._running = None
            self._sessions = 0
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("FTP import server stopped")

    def restart(self, settings: dict[str, Any] | None = None) -> str | None:
        self.stop()
        return self.start(settings)

    @property
    def running(self) -> bool:
        return self._server is not None

    def refresh_firewall_state(self) -> str:
        """Re-check the live firewall rule (e.g. after the operator applied it
        via the UAC prompt) and cache the result."""
        run = self._running
        if run is not None:
            port, ports = run["port"], run["_ports_tuple"]
        else:
            cfg = load_ftp_settings(self.inbox_dir)
            ports = parse_passive_ports(cfg.get("passive_ports", "")) or (50000, 50050)
            port = int(cfg.get("port", 2121))
        self._firewall_state = firewall_rule_state(port, ports)
        self._firewall_checked_at = time.time()
        return self._firewall_state

    def apply_firewall(self) -> str | None:
        run = self._running
        if run is None:
            cfg = load_ftp_settings(self.inbox_dir)
            ports = parse_passive_ports(cfg.get("passive_ports", "")) or (50000, 50050)
            port = int(cfg.get("port", 2121))
        else:
            port, ports = run["port"], run["_ports_tuple"]
        err = apply_firewall_rule(port, ports)
        # Give the elevated process a beat to finish, then re-poll.
        for _ in range(6):
            time.sleep(0.5)
            if firewall_rule_state(port, ports) == "ok":
                break
        self.refresh_firewall_state()
        return err

    def _try_firewall_silent(self, port: int, passive: tuple[int, int]) -> None:
        if os.name != "nt":
            return
        local_ports = _firewall_local_ports(port, passive)
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={_FIREWALL_RULE_NAME}"],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={_FIREWALL_RULE_NAME}", "dir=in", "action=allow",
                 "protocol=TCP", f"localport={local_ports}"],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def status(self) -> dict[str, Any]:
        run = self._running
        cfg = load_ftp_settings(self.inbox_dir)
        if run is not None:
            base = {k: run[k] for k in (
                "port", "root_dir", "anonymous", "username", "passive_host",
                "passive_ports", "connect_host")}
        else:
            base = {
                "port": cfg.get("port"),
                "root_dir": cfg.get("root_dir"),
                "anonymous": bool(cfg.get("anonymous", True)),
                "username": str(cfg.get("username", "")).strip() or DEFAULT_USERNAME,
                "passive_host": cfg.get("passive_host", ""),
                "passive_ports": cfg.get("passive_ports", ""),
                "connect_host": str(cfg.get("passive_host", "")).strip() or lan.lan_ip(),
            }
        clients = sorted(self._clients.values(), key=lambda r: r["last_t"], reverse=True)
        fw_ports = parse_passive_ports(str(base["passive_ports"])) or (50000, 50050)
        # Cheap netsh call, but not every 3s poll - cache for 30s.
        if time.time() - self._firewall_checked_at > 30:
            self.refresh_firewall_state()
        return {
            "running": self.running,
            "lan_ip": lan.lan_ip(),
            "sessions": self._sessions,
            "uploads_total": self._uploads_total,
            "firewall_state": self._firewall_state,
            "firewall_command": firewall_command(int(base["port"] or 2121), fw_ports),
            "clients": clients,
            "uploads": list(self._uploads),
            **base,
        }
