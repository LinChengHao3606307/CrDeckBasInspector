"""Receiver: manages MediaMTX (RTMP server) - download, configuration, startup and stream status."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from pathlib import Path

import psutil
import requests

ROOT = Path(__file__).resolve().parent
TOOLS_DIR = ROOT / "tools"
MEDIAMTX_DIR = TOOLS_DIR / "mediamtx"
MEDIAMTX_BIN = MEDIAMTX_DIR / "mediamtx.exe"
CONFIG_PATH = MEDIAMTX_DIR / "mediamtx.yml"
LOG_PATH = TOOLS_DIR / "mediamtx.log"

MEDIAMTX_VERSION = "v1.21.4"
ASSET_NAME = f"mediamtx_{MEDIAMTX_VERSION}_windows_amd64.zip"
DOWNLOAD_URLS = (
    f"https://ghproxy.net/https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/{ASSET_NAME}",
    f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/{ASSET_NAME}",
)

RTMP_PORT = 1935
RTSP_PORT = 8554
HLS_PORT = 8888
WEBRTC_PORT = 8889
API_PORT = 9997
DEFAULT_STREAM_KEY = "crdeck"
FIREWALL_RULE = "CrDeckInspector RTMP 1935"

CONFIG_TEXT = f"""# Generated automatically by CrDeckInspector
logLevel: info
logDestinations: [stdout]

api: yes
apiAddress: 127.0.0.1:{API_PORT}

rtmp: yes
rtmpAddress: :{RTMP_PORT}
rtmpEncryption: "no"

rtsp: yes
rtspAddress: :{RTSP_PORT}

hls: yes
hlsAddress: :{HLS_PORT}

webrtc: yes
webrtcAddress: :{WEBRTC_PORT}
webrtcLocalUDPAddress: :8189
webrtcIPsFromInterfaces: yes

srt: no

paths:
  all_others:
"""


def default_route_ip() -> str | None:
    """Return the IPv4 address of the default route (usually the Wi-Fi adapter)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("223.5.5.5", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def lan_ips() -> list[str]:
    """List usable LAN IPv4 addresses, with the default route address first."""
    skip = ("vmware", "virtualbox", "vethernet", "loopback", "tailscale", "bluetooth")
    result: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        if any(word in name.lower() for word in skip):
            continue
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            ip = addr.address
            if ip.startswith(("127.", "169.254.")):
                continue
            if ip not in result:
                result.append(ip)
    primary = default_route_ip()
    if primary and not primary.startswith(("127.", "169.254.")):
        if primary in result:
            result.remove(primary)
        result.insert(0, primary)
    return result


def active_paths(timeout: float = 1.5) -> list[str]:
    """Query MediaMTX API for the paths that are currently publishing."""
    try:
        resp = requests.get(f"http://127.0.0.1:{API_PORT}/v3/paths/list", timeout=timeout)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception:
        return []
    return [item["name"] for item in items if item.get("ready") and item.get("name")]


def firewall_rule_exists() -> bool:
    try:
        proc = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    except Exception:
        pass
    return _app_firewall_allowed()


def _app_firewall_allowed() -> bool:
    """Check whether Windows already has an inbound allow rule for mediamtx.exe."""
    if not MEDIAMTX_BIN.exists():
        return False
    script = (
        f"Get-NetFirewallApplicationFilter -Program '{MEDIAMTX_BIN}' -ErrorAction SilentlyContinue | "
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' } | "
        "Select-Object -First 1 -ExpandProperty DisplayName"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=15,
        )
        return bool(proc.stdout.strip())
    except Exception:
        return False


def ensure_firewall_rule() -> bool:
    """Allow 1935/TCP for the phone; fails silently when not running as administrator."""
    if firewall_rule_exists():
        return True
    try:
        proc = subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={FIREWALL_RULE}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={RTMP_PORT}",
                "profile=any",
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=15,
        )
        return proc.returncode == 0 and firewall_rule_exists()
    except Exception:
        return False


class MediaMtxServer:
    def __init__(self, log=print):
        self._log = log
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.logs: deque[str] = deque(maxlen=300)
        self.last_error = ""
        self.download_progress = 0.0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_ready(self, progress=None) -> None:
        MEDIAMTX_DIR.mkdir(parents=True, exist_ok=True)
        if not MEDIAMTX_BIN.exists():
            self._download(progress)
        CONFIG_PATH.write_text(CONFIG_TEXT, encoding="utf-8")

    def _download(self, progress) -> None:
        last_error: Exception | str = "unknown error"
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = TOOLS_DIR / "mediamtx_download.zip"
        for url in DOWNLOAD_URLS:
            try:
                with requests.get(url, stream=True, timeout=(10, 90)) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    with open(zip_path, "wb") as file:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            file.write(chunk)
                            done += len(chunk)
                            if progress and total:
                                self.download_progress = done / total
                                progress(self.download_progress)
                with zipfile.ZipFile(zip_path) as archive:
                    member = next(
                        (info for info in archive.infolist() if info.filename.endswith("mediamtx.exe")),
                        None,
                    )
                    if member is None:
                        raise RuntimeError("mediamtx.exe not found in the archive")
                    MEDIAMTX_BIN.write_bytes(archive.read(member))
                zip_path.unlink(missing_ok=True)
                if not MEDIAMTX_BIN.exists():
                    raise RuntimeError("failed to extract mediamtx.exe")
                return
            except Exception as exc:  # try the next download source
                last_error = exc
                zip_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download MediaMTX: {last_error}")

    def start(self) -> None:
        if self.alive:
            return
        self.ensure_ready()
        self._stop.clear()
        self.last_error = ""
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [str(MEDIAMTX_BIN), str(CONFIG_PATH)],
            cwd=str(MEDIAMTX_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._read_logs, args=(self._proc,), daemon=True).start()

    def _read_logs(self, proc: subprocess.Popen) -> None:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as file:
                for line in proc.stdout or []:
                    line = line.rstrip()
                    if not line:
                        continue
                    self.logs.append(line)
                    file.write(line + "\n")
                    self._log(line)
                    if "ERR" in line and not self.last_error:
                        self.last_error = line
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()
