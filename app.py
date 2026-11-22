"""CrDeckBasInspector: live phone preview, embedded virtual browser and browser controls.

Layout (left to right):
  * live  - RTMP stream from the phone's SplitCam Live, decoded and previewed;
  * middle- the QtWebEngine virtual browser, embedded as a child window;
  * right - browser navigation, API service control and the traffic log.

The live stream and the browser are intentionally not linked yet.
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from rtmp_server import (
    DEFAULT_STREAM_KEY,
    RTMP_PORT,
    RTSP_PORT,
    WEBRTC_PORT,
    MediaMtxServer,
    active_paths,
    default_route_ip,
    ensure_firewall_rule,
    firewall_rule_exists,
    lan_ips,
)

BG = "#0d1117"
PANEL = "#161b22"
PANEL_2 = "#21262d"
FG = "#e6edf3"
MUTED = "#8b949e"
GREEN = "#3fb950"
YELLOW = "#d29922"
RED = "#f85149"
BLUE = "#4fa8ff"
PURPLE = "#b48cff"
FONT = "Segoe UI"

STATUS_COLORS = {"wait": YELLOW, "live": GREEN, "error": RED}

ROOT = Path(__file__).resolve().parent
BROWSER_READY = "@@READY@@"
EVENT_PREFIX = "@@EV@@"
STATE_PREFIX = "@@STATE@@"
LOG_TAGS = {
    "request": ("req", BLUE),
    "response": ("resp", GREEN),
    "error": ("err", RED),
    "console": ("err", RED),
    "ws": ("ws", PURPLE),
    "info": ("info", MUTED),
}


def _shorten(text: str, limit: int = 150) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "\u2026"


def _human_size(size) -> str:
    try:
        n = int(size)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def format_event(entry: dict) -> tuple[str, str]:
    """Return (text, tag) for one browser traffic entry."""
    kind = entry.get("type")
    if kind == "request":
        text = "\u2192 %s %s [%s]" % (
            entry.get("method", "GET"),
            _shorten(entry.get("url", "")),
            entry.get("resource", ""),
        )
        return text, "req"
    if kind == "response":
        source = entry.get("source", "")
        if entry.get("error"):
            return "\u2190 ERR %s %s [%s]" % (
                _shorten(entry.get("url", "")), entry.get("error"), source), "err"
        status = entry.get("status", 0)
        bits = []
        size = _human_size(entry.get("size"))
        if size:
            bits.append(size)
        if entry.get("ms") is not None:
            bits.append("%s ms" % entry.get("ms"))
        if entry.get("contentType"):
            bits.append(str(entry["contentType"]).split(";")[0])
        if source:
            bits.append(source)
        tag = "resp" if 200 <= int(status or 0) < 400 else "err"
        return "\u2190 %s %s (%s)" % (
            status, _shorten(entry.get("url", "")), " \u00b7 ".join(bits)), tag
    if kind == "ws-open":
        return "\u21c6 WS OPEN %s" % _shorten(entry.get("url", "")), "ws"
    if kind == "ws-message":
        return "\u21c6 WS MSG %s %s" % (
            _shorten(entry.get("url", "")), _shorten(entry.get("data", ""))), "ws"
    if kind == "ws-close":
        return "\u21c6 WS CLOSE %s %s %s" % (
            _shorten(entry.get("url", "")), entry.get("code", ""), entry.get("reason", "")), "ws"
    if kind == "console":
        return "[console %s] %s (%s:%s)" % (
            entry.get("level", ""), _shorten(entry.get("message", "")),
            _shorten(entry.get("source", ""), 60), entry.get("line", "")), "err"
    if kind == "nav":
        return "[page] %s %s" % (entry.get("url", ""), entry.get("note", "")), "info"
    if kind == "api":
        return "[api] %s %s %s" % (
            entry.get("method", ""), entry.get("path", ""), entry.get("note", "")), "info"
    if kind == "info":
        return "[info] %s" % entry.get("message", ""), "info"
    return _shorten(json.dumps(entry, ensure_ascii=False), 220), "info"


class CrDeckInspectorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.server = MediaMtxServer(log=self._server_log)
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()

        self._pending_status: tuple[str, str] | None = None
        self._pending_hint: bool | None = None
        self._pending_ips: list[str] | None = None
        self._pending_firewall: bool | None = None
        self._status_text = ""
        self._hint_visible = True
        self._booted = False
        self._firewall_ok = True
        self._ips: list[str] = []
        self._url = ""
        self._fps = 0.0
        self._fps_count = 0
        self._fps_since = time.time()
        self._resolution = ""

        self._browser: subprocess.Popen | None = None
        self._browser_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._browser_ready = False
        self._server_running = False
        self._sash_placed = False
        self._url_dirty = False
        self._annotating = False
        self._annotate_events: queue.Queue[tuple[str, str, str]] = queue.Queue()

        # Live battle OCR: sample the live frames and look up the opponent deck
        # in the embedded browser once per battle.
        self._battle_active = False
        self._battle_hits = 0
        self._idle_hits = 0
        self._ocr_frame_lock = threading.Lock()
        self._ocr_frame: np.ndarray | None = None
        self._ocr_wake = threading.Event()
        self._ocr_events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._last_ocr_at = 0.0
        self._lookup_busy = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(15, self._pump)
        self.root.after(100, self._boot_async)
        self.root.after(400, self._start_browser)
        threading.Thread(target=self._ocr_loop, daemon=True).start()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        self.root.title("CrDeckBasInspector - Live + Virtual Browser")
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win_w = min(1560, int(screen_w * 0.94))
        win_h = min(900, int(screen_h * 0.90))
        self.root.geometry(
            "%dx%d+%d+%d" % (win_w, win_h, max(0, (screen_w - win_w) // 2),
                             max(0, (screen_h - win_h) // 2))
        )
        self.root.minsize(1180, 700)
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=FG,
            arrowcolor=FG,
            bordercolor=PANEL_2,
        )
        style.map("Dark.TCombobox", fieldbackground=[("readonly", PANEL_2)])
        style.configure("Dark.TPanedwindow", background=BG)
        style.configure("Dark.Vertical.TScrollbar", background=PANEL_2,
                        troughcolor=BG, arrowcolor=FG, bordercolor=BG)

        header = tk.Frame(self.root, bg=PANEL, padx=18, pady=12)
        header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(
            header, text="CrDeckBasInspector", bg=PANEL, fg=FG, font=(FONT, 15, "bold")
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="Phone Live  |  Virtual Browser  |  Browser Control & Traffic Log",
            bg=PANEL,
            fg=MUTED,
            font=(FONT, 10),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.status_label = tk.Label(
            header, text="\u25cf Initializing...", bg=PANEL, fg=YELLOW, font=(FONT, 10, "bold")
        )
        self.status_label.pack(side=tk.RIGHT)

        footer = tk.Frame(self.root, bg=PANEL, padx=18, pady=12)
        footer.pack(side=tk.BOTTOM, fill=tk.X)

        title_row = tk.Frame(footer, bg=PANEL)
        title_row.pack(fill=tk.X)
        tk.Label(
            title_row,
            text="Phone setup (SplitCam Live -> Edit Channel -> Platform: Custom)",
            bg=PANEL,
            fg=FG,
            font=(FONT, 10, "bold"),
        ).pack(side=tk.LEFT)

        self.ip_box = ttk.Combobox(
            title_row, width=16, state="readonly", style="Dark.TCombobox", font=(FONT, 9)
        )
        self.ip_box.pack(side=tk.RIGHT)
        self.ip_box.bind("<<ComboboxSelected>>", lambda _event: self._update_url())
        tk.Label(title_row, text="PC address", bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        grid = tk.Frame(footer, bg=PANEL)
        grid.pack(fill=tk.X, pady=(8, 0))
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="STREAM URL", bg=PANEL, fg=MUTED, font=(FONT, 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        self.url_entry = tk.Entry(
            grid, bg=PANEL_2, fg=FG, readonlybackground=PANEL_2, insertbackground=FG,
            selectbackground="#264f78", relief=tk.FLAT, highlightthickness=0,
            font=("Consolas", 11),
        )
        self.url_entry.grid(row=0, column=1, sticky="ew", ipady=5)
        self._make_button(grid, "Copy", self._copy_url).grid(row=0, column=2, padx=(8, 0))

        tk.Label(grid, text="STREAM KEY", bg=PANEL, fg=MUTED, font=(FONT, 9)).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0)
        )
        self.key_entry = tk.Entry(
            grid, bg=PANEL_2, fg=FG, readonlybackground=PANEL_2, insertbackground=FG,
            selectbackground="#264f78", relief=tk.FLAT, highlightthickness=0,
            font=("Consolas", 11),
        )
        self.key_entry.grid(row=1, column=1, sticky="ew", ipady=5, pady=(8, 0))
        self._make_button(grid, "Copy", self._copy_key).grid(
            row=1, column=2, padx=(8, 0), pady=(8, 0)
        )

        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill=tk.X, pady=(12, 0))
        self._make_button(
            actions, "Open Low-Latency Browser Preview", self._open_browser, primary=True
        ).pack(side=tk.LEFT)
        self._make_button(actions, "Restart Receiver", self._restart_server).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.annotate_button = self._make_button(
            actions, "视频标注", self._pick_videos_for_annotation
        )
        self.annotate_button.pack(side=tk.LEFT, padx=(8, 0))

        self.firewall_label = tk.Label(actions, text="", bg=PANEL, fg=YELLOW, font=(FONT, 9))
        self.firewall_label.pack(side=tk.RIGHT)
        self.firewall_button = self._make_button(actions, "Allow Port (Admin)", self._elevate_firewall)

        # ---- three panes: live | browser | controls + log ----
        body = tk.Frame(self.root, bg=BG)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.panes = ttk.Panedwindow(body, orient=tk.HORIZONTAL, style="Dark.TPanedwindow")
        self.panes.pack(fill=tk.BOTH, expand=True)
        self.panes.bind("<Configure>", self._place_sashes)

        live_pane = tk.Frame(self.panes, bg="#000000")
        self.panes.add(live_pane, weight=3)

        self.video_label = tk.Label(live_pane, bg="#000000")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        self.stats_label = tk.Label(
            live_pane, text="", bg="#000000", fg=MUTED, font=(FONT, 9), padx=10, pady=6
        )
        self.stats_label.place(relx=1.0, rely=1.0, anchor="se")

        self.hint_label = tk.Label(
            live_pane, text="", bg="#000000", fg=MUTED, font=(FONT, 12),
            justify=tk.CENTER, wraplength=520,
        )
        self.hint_label.place(relx=0.5, rely=0.5, anchor="center")

        self.browser_pane = tk.Frame(self.panes, bg="#05070a")
        self.panes.add(self.browser_pane, weight=4)
        self.browser_placeholder = tk.Label(
            self.browser_pane, text="Starting virtual browser...", bg="#05070a",
            fg=MUTED, font=(FONT, 11),
        )
        self.browser_placeholder.place(relx=0.5, rely=0.5, anchor="center")

        side = tk.Frame(self.panes, bg=PANEL, width=330)
        self.panes.add(side, weight=2)
        self._build_side_panel(side)

        self._refresh_ips()
        self._render_hint()

    def _build_side_panel(self, side: tk.Frame) -> None:
        pad = {"padx": 12}

        tk.Label(side, text="BROWSER", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(
            anchor="w", pady=(12, 4), **pad
        )

        self.url_var = tk.StringVar()
        self.browser_url_entry = tk.Entry(
            side, textvariable=self.url_var, bg=PANEL_2, fg=FG, insertbackground=FG,
            selectbackground="#264f78", relief=tk.FLAT, highlightthickness=0,
            font=("Consolas", 9),
        )
        self.browser_url_entry.pack(fill=tk.X, ipady=5, **pad)
        self.browser_url_entry.bind("<Return>", lambda _event: self._browser_nav())
        self.browser_url_entry.bind("<Key>", lambda _event: self._mark_url_dirty())

        nav = tk.Frame(side, bg=PANEL)
        nav.pack(fill=tk.X, pady=(8, 0), **pad)
        self._mini_button(nav, "Back", lambda: self._browser_send("BACK")).pack(side=tk.LEFT)
        self._mini_button(nav, "Forward", lambda: self._browser_send("FORWARD")).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self._mini_button(nav, "Reload", lambda: self._browser_send("RELOAD")).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self._mini_button(nav, "Go", self._browser_nav, primary=True).pack(side=tk.RIGHT)

        tk.Frame(side, bg=PANEL_2, height=1).pack(fill=tk.X, pady=(14, 0), **pad)

        tk.Label(side, text="API SERVICE", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(
            anchor="w", pady=(12, 4), **pad
        )
        svc = tk.Frame(side, bg=PANEL)
        svc.pack(fill=tk.X, **pad)
        tk.Label(svc, text="Port", bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="8765")
        tk.Entry(
            svc, textvariable=self.port_var, width=7, bg=PANEL_2, fg=FG,
            insertbackground=FG, relief=tk.FLAT, highlightthickness=0,
            font=("Consolas", 10),
        ).pack(side=tk.LEFT, padx=(8, 8), ipady=3)
        self.service_button = self._mini_button(svc, "Start", self._toggle_service)
        self.service_button.pack(side=tk.LEFT)

        self.service_status = tk.Label(
            side, text="\u25cf not started", bg=PANEL, fg=MUTED, font=(FONT, 9)
        )
        self.service_status.pack(anchor="w", pady=(6, 0), **pad)

        self.direct_lookup_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            side, text="Token \u76f4\u67e5\uff08\u4e0d\u6a21\u62df\u4eba\u5de5\uff09",
            variable=self.direct_lookup_var, bg=PANEL, fg=MUTED,
            activebackground=PANEL, activeforeground=FG, selectcolor=PANEL_2,
            highlightthickness=0, bd=0, font=(FONT, 9),
        ).pack(anchor="w", pady=(6, 0), **pad)

        tk.Frame(side, bg=PANEL_2, height=1).pack(fill=tk.X, pady=(14, 0), **pad)

        tk.Label(side, text="VIRTUAL POINTER", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(
            anchor="w", pady=(12, 4), **pad
        )
        ptr_row = tk.Frame(side, bg=PANEL)
        ptr_row.pack(fill=tk.X, **pad)
        self.pointer_var = tk.BooleanVar(value=True)
        self.pointer_trail_var = tk.BooleanVar(value=False)
        for text, var, flag in (("Position ring", self.pointer_var, "POS"),
                                ("Trail", self.pointer_trail_var, "TRAIL")):
            tk.Checkbutton(
                ptr_row, text=text, variable=var, bg=PANEL, fg=MUTED,
                activebackground=PANEL, activeforeground=FG, selectcolor=PANEL_2,
                highlightthickness=0, bd=0, font=(FONT, 9),
                command=lambda f=flag, v=var: self._set_pointer_flag(f, v),
            ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Frame(side, bg=PANEL_2, height=1).pack(fill=tk.X, pady=(14, 0), **pad)

        log_head = tk.Frame(side, bg=PANEL)
        log_head.pack(fill=tk.X, pady=(12, 4), **pad)
        tk.Label(log_head, text="TRAFFIC LOG", bg=PANEL, fg=MUTED, font=(FONT, 9, "bold")).pack(
            side=tk.LEFT
        )
        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            log_head, text="Pause", variable=self.pause_var, bg=PANEL, fg=MUTED,
            activebackground=PANEL, activeforeground=FG, selectcolor=PANEL_2,
            highlightthickness=0, bd=0, font=(FONT, 8),
        ).pack(side=tk.RIGHT)
        self._mini_button(log_head, "Clear", self._clear_log).pack(side=tk.RIGHT, padx=(0, 6))

        log_wrap = tk.Frame(side, bg=PANEL)
        log_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 12), **pad)
        self.log_text = tk.Text(
            log_wrap, bg="#0b1016", fg="#c3d0dd", insertbackground=FG, relief=tk.FLAT,
            wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED, height=10,
            padx=6, pady=4,
        )
        scroll = ttk.Scrollbar(log_wrap, command=self.log_text.yview, style="Dark.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for tag, color in (("req", BLUE), ("resp", GREEN), ("err", RED),
                           ("ws", PURPLE), ("info", MUTED)):
            self.log_text.tag_configure(tag, foreground=color)

    def _make_button(self, parent, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=GREEN if primary else PANEL_2,
            fg="#04260f" if primary else FG,
            activebackground="#2ea043" if primary else "#30363d",
            activeforeground="#04260f" if primary else FG,
            relief=tk.FLAT,
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            font=(FONT, 9, "bold" if primary else "normal"),
        )

    def _mini_button(self, parent, text: str, command, primary: bool = False) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=GREEN if primary else PANEL_2,
            fg="#04260f" if primary else FG,
            activebackground="#2ea043" if primary else "#30363d",
            activeforeground="#04260f" if primary else FG,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            font=(FONT, 8, "bold" if primary else "normal"),
        )

    def _place_sashes(self, event=None) -> None:
        if self._sash_placed:
            return
        total = self.panes.winfo_width()
        if total < 600:
            return
        self._sash_placed = True
        try:
            self.panes.sashpos(0, 430)
            self.panes.sashpos(1, total - 340)
        except tk.TclError:
            pass

    # ---------------- Browser process ----------------

    def _start_browser(self) -> None:
        self.browser_pane.update_idletasks()
        hwnd = self.browser_pane.winfo_id()
        command = [
            sys.executable,
            str(ROOT / "browser_embed.py"),
            "--parent-hwnd",
            str(hwnd),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Tk does not follow the system DPI scale, so keep the embedded Qt window
        # at 1:1 pixels to make both panes agree on sizes.
        env = dict(
            os.environ,
            PYTHONIOENCODING="utf-8",
            QT_ENABLE_HIGHDPI_SCALING="0",
            QT_AUTO_SCREEN_SCALE_FACTOR="0",
            QT_SCALE_FACTOR="1",
        )
        try:
            self._browser = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=flags,
            )
        except Exception as exc:
            self._set_browser_placeholder("Browser failed to start: %r" % (exc,))
            self._append_log("[error] browser failed to start: %r" % (exc,), "err")
            return
        threading.Thread(target=self._browser_reader, args=(self._browser,), daemon=True).start()

    def _browser_reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith(EVENT_PREFIX):
                    try:
                        self._browser_events.put(("event", json.loads(line[len(EVENT_PREFIX):])))
                    except ValueError:
                        pass
                elif line.startswith(STATE_PREFIX):
                    try:
                        self._browser_events.put(("state", json.loads(line[len(STATE_PREFIX):])))
                    except ValueError:
                        pass
                elif line == BROWSER_READY:
                    self._browser_events.put(("ready", None))
        except (OSError, ValueError):
            pass
        self._browser_events.put(("exit", proc.poll()))

    def _browser_send(self, line: str) -> bool:
        proc = self._browser
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _mark_url_dirty(self) -> None:
        self._url_dirty = True

    def _set_pointer_flag(self, flag: str, var: tk.BooleanVar) -> None:
        value = "on" if var.get() else "off"
        self._browser_send("POINTER %s %s" % (flag, value))

    def _browser_nav(self) -> None:
        url = self.url_var.get().strip()
        if url:
            self._url_dirty = False
            self._browser_send("NAV " + url)

    def _toggle_service(self) -> None:
        if self._server_running:
            self._browser_send("SERVER STOP")
        else:
            port = self.port_var.get().strip() or "8765"
            self._browser_send("SERVER START " + port)

    def _set_browser_placeholder(self, text: str) -> None:
        self.browser_placeholder.configure(text=text)
        self.browser_placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.browser_placeholder.lift()

    def _drain_browser_events(self) -> None:
        while True:
            try:
                kind, payload = self._browser_events.get_nowait()
            except queue.Empty:
                return
            if kind == "ready":
                self._browser_ready = True
                self.browser_placeholder.place_forget()
            elif kind == "event":
                text, tag = format_event(payload)
                self._append_log(text, tag)
            elif kind == "state":
                self._apply_browser_state(payload)
            elif kind == "exit":
                self._browser_ready = False
                self._server_running = False
                self.service_status.configure(text="\u25cf browser stopped", fg=RED)
                self.service_button.configure(text="Start")
                self._set_browser_placeholder("Virtual browser stopped")
                self._append_log("[info] virtual browser stopped", "info")

    def _apply_browser_state(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "url":
            url = payload.get("url", "")
            if not self._url_dirty:
                self.url_var.set(url)
        elif kind == "server":
            running = bool(payload.get("running"))
            self._server_running = running
            text = payload.get("text") or ("running" if running else "stopped")
            self.service_status.configure(
                text="\u25cf " + text, fg=GREEN if running else MUTED
            )
            self.service_button.configure(text="Stop" if running else "Start")
        elif kind == "pointer":
            var = getattr(self, "pointer_var", None)
            if var is not None:
                var.set(bool(payload.get("pos", True)))
                self.pointer_trail_var.set(bool(payload.get("trail", False)))

    def _append_log(self, text: str, tag: str = "info") -> None:
        if self.pause_var.get():
            return
        widget = self.log_text
        stamp = time.strftime("%H:%M:%S")
        widget.configure(state=tk.NORMAL)
        widget.insert(tk.END, "%s  %s\n" % (stamp, text), tag)
        lines = int(widget.index("end-1c").split(".")[0])
        if lines > 1500:
            widget.delete("1.0", "%d.0" % (lines - 1200))
        widget.see(tk.END)
        widget.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ---------------- Startup ----------------

    def _boot_async(self) -> None:
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self) -> None:
        self._set_pending_status("Preparing receiver...", "wait")
        try:
            self.server.ensure_ready(progress=self._on_download_progress)
        except Exception as exc:
            self._set_pending_status(f"Receiver setup failed: {exc}", "error")
            return

        self._firewall_ok = ensure_firewall_rule()
        self._pending_firewall = self._firewall_ok
        try:
            self.server.start()
        except Exception as exc:
            self._set_pending_status(f"Failed to start receiver: {exc}", "error")
            return

        for _ in range(20):
            if self.server.alive:
                break
            time.sleep(0.25)
        if not self.server.alive:
            self._set_pending_status(
                f"Failed to start receiver: {self.server.last_error or 'see tools/mediamtx.log'}", "error"
            )
            return

        self._booted = True
        self._pending_ips = lan_ips()
        self._set_pending_status("Waiting for phone stream...", "wait")
        threading.Thread(target=self._viewer_loop, daemon=True).start()

    def _on_download_progress(self, ratio: float) -> None:
        self._set_pending_status(
            f"First run: downloading receiver component {ratio * 100:.0f}%", "wait"
        )

    def _set_pending_status(self, text: str, kind: str = "wait") -> None:
        self._pending_status = (text, kind)

    def _server_log(self, line: str) -> None:
        # Called from a background thread; UI refresh is handled by _pump.
        if "is publishing" in line or "stream is" in line:
            self._pending_hint = False

    # ---------------- Viewer ----------------

    def _viewer_loop(self) -> None:
        cap: cv2.VideoCapture | None = None
        current_path = ""
        last_probe = 0.0
        opened_at = 0.0

        while not self.stop_event.is_set():
            if cap is None:
                now = time.time()
                if now - last_probe > 1.0:
                    last_probe = now
                    paths = active_paths()
                    current_path = paths[0] if paths else ""
                if not current_path:
                    self._set_pending_status(
                        "Waiting for phone stream... (tap Start Live in SplitCam)", "wait"
                    )
                    self._pending_hint = True
                    time.sleep(0.8)
                    continue

                cap = cv2.VideoCapture(f"rtsp://127.0.0.1:{RTSP_PORT}/{current_path}")
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self._set_pending_status("Stream detected, connecting to video...", "wait")
                    time.sleep(1.0)
                    continue
                opened_at = time.time()
                self._set_pending_status(f"Receiving: {current_path}", "live")
                self._pending_hint = False

            ok, frame = cap.read()
            if not ok or frame is None:
                # Just after connecting, the decoder may still be waiting for a keyframe.
                if time.time() - opened_at < 8:
                    time.sleep(0.05)
                    continue
                cap.release()
                cap = None
                current_path = ""
                self._set_pending_status("Video interrupted, waiting to reconnect...", "wait")
                self._pending_hint = True
                time.sleep(1.0)
                continue

            self._push_frame(frame)
            self._offer_ocr_frame(frame)

    def _push_frame(self, frame: np.ndarray) -> None:
        if self.frames.full():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass

    # ---------------- UI refresh ----------------

    def _pump(self) -> None:
        if self._pending_status is not None:
            text, kind = self._pending_status
            self._pending_status = None
            self._apply_status(text, kind)

        if self._pending_ips is not None:
            self._ips = self._pending_ips
            self._pending_ips = None
            self.ip_box["values"] = self._ips
            if self._ips:
                self.ip_box.set(self._ips[0])
            self._update_url()

        if self._pending_firewall is not None:
            self._firewall_ok = self._pending_firewall
            self._pending_firewall = None
            self._apply_firewall_state()

        frame = None
        try:
            while True:
                frame = self.frames.get_nowait()
        except queue.Empty:
            pass
        if frame is not None:
            self._show_frame(frame)

        if self._pending_hint is not None:
            self._hint_visible = self._pending_hint
            self._pending_hint = None
            if self._hint_visible:
                self.hint_label.place(relx=0.5, rely=0.5, anchor="center")
                self.hint_label.lift()
            else:
                self.hint_label.place_forget()

        if self._booted and not self.server.alive:
            self._apply_status('Receiver stopped, click "Restart Receiver"', "error")

        self._drain_browser_events()
        self._drain_annotation_events()
        self._drain_ocr_events()
        self.root.after(15, self._pump)

    def _apply_status(self, text: str, kind: str) -> None:
        self._status_text = text
        self.status_label.configure(text=f"\u25cf {text}", fg=STATUS_COLORS.get(kind, MUTED))

    def _show_frame(self, frame: np.ndarray) -> None:
        width = self.video_label.winfo_width()
        height = self.video_label.winfo_height()
        if width < 20 or height < 20:
            return

        frame_h, frame_w = frame.shape[:2]
        scale = min(width / frame_w, height / frame_h)
        target = (max(1, int(frame_w * scale)), max(1, int(frame_h * scale)))
        resized = cv2.resize(frame, target, interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.video_label.configure(image=photo)
        self.video_label.image = photo

        self._fps_count += 1
        now = time.time()
        if now - self._fps_since >= 1.0:
            self._fps = self._fps_count / (now - self._fps_since)
            self._fps_count = 0
            self._fps_since = now
        if self._resolution != f"{frame_w}x{frame_h}":
            self._resolution = f"{frame_w}x{frame_h}"
        self.stats_label.configure(text=f"{self._resolution}  |  {self._fps:.1f} fps")

    def _render_hint(self) -> None:
        ip = self._ips[0] if self._ips else "PC_IP"
        self.hint_label.configure(
            text=(
                "Waiting for phone stream...\n\n"
                "SplitCam Live -> Edit Channel\n"
                "PLATFORM: Custom\n"
                f"STREAM URL: rtmp://{ip}:{RTMP_PORT}/live\n"
                f"STREAM KEY: {DEFAULT_STREAM_KEY}\n\n"
                "Tap Start Live after filling in the fields"
            )
        )

    # ---------------- Actions ----------------

    def _refresh_ips(self) -> None:
        ips = lan_ips()
        if not ips:
            primary = default_route_ip()
            ips = [primary] if primary else []
        self._ips = ips
        self.ip_box["values"] = ips
        if ips:
            self.ip_box.set(ips[0])
        self._update_url()
        self._render_hint()

    def _apply_firewall_state(self) -> None:
        if self._firewall_ok:
            self.firewall_label.configure(text="")
            self.firewall_button.pack_forget()
        else:
            self.firewall_label.configure(
                text="Port 1935 is not allowed, the phone may fail to connect ->"
            )
            self.firewall_button.pack(side=tk.RIGHT, padx=(0, 10))

    def _elevate_firewall(self) -> None:
        script = ROOT / "allow_firewall.ps1"
        command = (
            "Start-Process powershell -Verb RunAs -ArgumentList "
            f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{script}'"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                timeout=20,
            )
        except Exception as exc:
            self._apply_status(f"Cannot request administrator rights: {exc}", "error")
            return
        self._apply_status('Click "Yes" in the UAC prompt to allow port 1935', "wait")
        threading.Thread(target=self._watch_firewall, daemon=True).start()

    def _watch_firewall(self) -> None:
        for _ in range(60):
            if firewall_rule_exists():
                self._pending_firewall = True
                self._set_pending_status("Port allowed", "live")
                return
            time.sleep(1.0)

    def _update_url(self) -> None:
        ip = self.ip_box.get() or (self._ips[0] if self._ips else "PC_IP")
        self._url = f"rtmp://{ip}:{RTMP_PORT}/live"
        self.url_entry.configure(state=tk.NORMAL)
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, self._url)
        self.url_entry.configure(state="readonly")
        self.key_entry.configure(state=tk.NORMAL)
        self.key_entry.delete(0, tk.END)
        self.key_entry.insert(0, DEFAULT_STREAM_KEY)
        self.key_entry.configure(state="readonly")
        self._render_hint()

    def _copy_url(self) -> None:
        self._copy(self._url)

    def _copy_key(self) -> None:
        self._copy(DEFAULT_STREAM_KEY)

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._apply_status(f"Copied: {text}", "live")

    def _open_browser(self) -> None:
        paths = active_paths(timeout=1.0)
        path = paths[0] if paths else "live/" + DEFAULT_STREAM_KEY
        webbrowser.open(f"http://127.0.0.1:{WEBRTC_PORT}/{path}")

    def _pick_videos_for_annotation(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="选择要标注的对战视频",
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
        )
        if not paths:
            return
        if self._annotating:
            self._append_log("[annotate] a job is already running", "info")
            return
        self._annotating = True
        self.annotate_button.configure(state=tk.DISABLED)
        self._append_log(
            "[annotate] queued %d video(s), output -> %s" % (len(paths), ROOT), "info"
        )
        threading.Thread(
            target=self._annotate_worker, args=(list(paths),), daemon=True
        ).start()

    def _annotate_worker(self, paths: list[str]) -> None:
        def emit(line: str, tag: str = "info") -> None:
            self._annotate_events.put(("line", line, tag))

        try:
            import battle_annotator

            battle_annotator.annotate_videos(
                paths, out_dir=ROOT, log=emit
            )
        except Exception as exc:
            emit("[annotate] failed: %s" % (exc,), "err")
        else:
            emit("[annotate] all videos finished", "resp")
        finally:
            self._annotate_events.put(("done", "", ""))

    def _drain_annotation_events(self) -> None:
        while True:
            try:
                kind, text, tag = self._annotate_events.get_nowait()
            except queue.Empty:
                return
            if kind == "done":
                self._annotating = False
                self.annotate_button.configure(state=tk.NORMAL)
                continue
            self._append_log(text, tag)

    # ---------------- Live battle OCR ----------------

    def _offer_ocr_frame(self, frame: np.ndarray) -> None:
        # Called from the viewer thread; keep at most one frame per second.
        now = time.time()
        if now - self._last_ocr_at < 1.0:
            return
        self._last_ocr_at = now
        with self._ocr_frame_lock:
            self._ocr_frame = frame.copy()
        self._ocr_wake.set()

    def _ocr_loop(self) -> None:
        recognizer = None
        while not self.stop_event.is_set():
            self._ocr_wake.wait(timeout=0.5)
            self._ocr_wake.clear()
            with self._ocr_frame_lock:
                frame = self._ocr_frame
                self._ocr_frame = None
            if frame is None:
                continue
            try:
                if recognizer is None:
                    from battle_annotator import BattleRecognizer

                    recognizer = BattleRecognizer(log=lambda _line: None)
                result = recognizer.detect(frame)
            except Exception as exc:
                self._ocr_events.put(("[ocr] recognition failed: %r" % (exc,), "err"))
                time.sleep(1.0)
                continue
            self._update_battle_state(result)

    def _update_battle_state(self, result: dict) -> None:
        if result.get("battle"):
            self._battle_hits += 1
            self._idle_hits = 0
        else:
            self._idle_hits += 1
            self._battle_hits = 0

        if not self._battle_active and self._battle_hits >= 2:
            self._battle_active = True
            name = ((result.get("name") or {}).get("text") or "").strip()
            clan = ((result.get("clan") or {}).get("text") or "").strip()
            self._ocr_events.put(
                ("[battle] opponent: %s / %s" % (name or "?", clan or "-"), "resp")
            )
            if name:
                self._start_deck_lookup(name, clan)
            else:
                self._ocr_events.put(
                    ("[battle] opponent name not recognised, deck lookup skipped", "err")
                )
        elif self._battle_active and self._idle_hits >= 2:
            self._battle_active = False
            self._ocr_events.put(("[battle] finished", "info"))

    @staticmethod
    def _player_tag(text: str) -> str:
        value = (text or "").strip().upper().replace(" ", "")
        has_hash = value.startswith("#")
        if has_hash:
            value = value[1:]
        if not 3 <= len(value) <= 12:
            return ""
        if not set(value) <= set("0289PYLQGRJCUV"):
            return ""
        # Without "#" a short string is more likely an OCR name than a tag.
        if not has_hash and len(value) < 6:
            return ""
        return "#" + value

    @staticmethod
    def _usable_clan(clan: str) -> str:
        value = (clan or "").strip()
        if value in {"", "无部落", "無部落", "无", "無", "-"}:
            return ""
        return value

    def _start_deck_lookup(self, name: str, clan: str) -> None:
        if self._lookup_busy:
            return
        self._lookup_busy = True
        threading.Thread(
            target=self._lookup_deck_worker,
            args=(name, self._usable_clan(clan)),
            daemon=True,
        ).start()

    def _ensure_browser_service(self, port: str) -> None:
        if self._server_running:
            return
        self._browser_send("SERVER START " + port)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:%s/health" % port, timeout=1
                ):
                    return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("browser API did not start on port %s" % port)

    def _lookup_deck_worker(self, name: str, clan: str) -> None:
        try:
            port = self.port_var.get().strip() or "8765"
            self._ensure_browser_service(port)
            direct = bool(
                getattr(self, "direct_lookup_var", None) and self.direct_lookup_var.get()
            )
            tag = self._player_tag(name)
            query = {}
            if tag:
                query["tag"] = tag
            else:
                query["name"] = name
                if clan:
                    query["clan"] = clan
            if direct:
                query["human"] = "0"
            url = "http://127.0.0.1:%s/royaleapi/deck/?%s" % (
                port,
                urllib.parse.urlencode(query),
            )
            label = tag or (name if not clan else "%s / %s" % (name, clan))
            mode = "tag" if tag else "name"
            self._ocr_events.put(
                ("[deck] %s lookup: %s" % (mode, label), "info")
                if not direct
                else ("[deck] %s direct lookup: %s" % (mode, label), "info")
            )
            with urllib.request.urlopen(url, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            if not payload.get("ok", True):
                raise RuntimeError(payload.get("error") or "deck lookup failed")
            deck = payload.get("deck") or []
            if isinstance(deck, list) and deck and isinstance(deck[0], dict):
                cards = ", ".join(str(card.get("name", "")) for card in deck)
            else:
                cards = str(deck)
            self._ocr_events.put(("[deck] %s" % (cards or "no deck"), "resp"))
        except Exception as exc:
            self._ocr_events.put(("[deck] %s" % (exc,), "err"))
        finally:
            self._lookup_busy = False

    def _drain_ocr_events(self) -> None:
        while True:
            try:
                text, tag = self._ocr_events.get_nowait()
            except queue.Empty:
                return
            self._append_log(text, tag)

    def _restart_server(self) -> None:
        def work() -> None:
            self._set_pending_status("Restarting receiver...", "wait")
            try:
                self.server.restart()
            except Exception as exc:
                self._set_pending_status(f"Restart failed: {exc}", "error")
                return
            self._set_pending_status("Waiting for phone stream...", "wait")
            self._pending_hint = True

        threading.Thread(target=work, daemon=True).start()

    def _stop_browser(self) -> None:
        proc, self._browser = self._browser, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    def _on_close(self) -> None:
        self.stop_event.set()
        self._stop_browser()
        self.server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    CrDeckInspectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
