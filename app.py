"""CrDeckInspector: receives an RTMP stream from the phone's SplitCam Live and previews it locally."""

from __future__ import annotations

import os

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import queue
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

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
FONT = "Segoe UI"

STATUS_COLORS = {"wait": YELLOW, "live": GREEN, "error": RED}
ROOT = Path(__file__).resolve().parent


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

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(15, self._pump)
        self.root.after(100, self._boot_async)

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        self.root.title("CrDeckInspector - Phone Stream Preview")
        self.root.geometry("1280x860")
        self.root.minsize(880, 640)
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

        header = tk.Frame(self.root, bg=PANEL, padx=18, pady=12)
        header.pack(side=tk.TOP, fill=tk.X)
        tk.Label(
            header,
            text="CrDeckInspector",
            bg=PANEL,
            fg=FG,
            font=(FONT, 15, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="Phone SplitCam Live -> Local RTMP -> Live Preview",
            bg=PANEL,
            fg=MUTED,
            font=(FONT, 10),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.status_label = tk.Label(
            header,
            text="● Initializing...",
            bg=PANEL,
            fg=YELLOW,
            font=(FONT, 10, "bold"),
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
            title_row,
            width=16,
            state="readonly",
            style="Dark.TCombobox",
            font=(FONT, 9),
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
            grid,
            bg=PANEL_2,
            fg=FG,
            readonlybackground=PANEL_2,
            insertbackground=FG,
            selectbackground="#264f78",
            relief=tk.FLAT,
            highlightthickness=0,
            font=("Consolas", 11),
        )
        self.url_entry.grid(row=0, column=1, sticky="ew", ipady=5)
        self._make_button(grid, "Copy", self._copy_url).grid(row=0, column=2, padx=(8, 0))

        tk.Label(grid, text="STREAM KEY", bg=PANEL, fg=MUTED, font=(FONT, 9)).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0)
        )
        self.key_entry = tk.Entry(
            grid,
            bg=PANEL_2,
            fg=FG,
            readonlybackground=PANEL_2,
            insertbackground=FG,
            selectbackground="#264f78",
            relief=tk.FLAT,
            highlightthickness=0,
            font=("Consolas", 11),
        )
        self.key_entry.grid(row=1, column=1, sticky="ew", ipady=5, pady=(8, 0))
        self._make_button(grid, "Copy", self._copy_key).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

        actions = tk.Frame(footer, bg=PANEL)
        actions.pack(fill=tk.X, pady=(12, 0))
        self._make_button(actions, "Open Low-Latency Browser Preview", self._open_browser, primary=True).pack(
            side=tk.LEFT
        )
        self._make_button(actions, "Restart Receiver", self._restart_server).pack(side=tk.LEFT, padx=(8, 0))

        self.firewall_label = tk.Label(
            actions,
            text="",
            bg=PANEL,
            fg=YELLOW,
            font=(FONT, 9),
        )
        self.firewall_label.pack(side=tk.RIGHT)
        self.firewall_button = self._make_button(actions, "Allow Port (Admin)", self._elevate_firewall)

        video_box = tk.Frame(self.root, bg="#000000")
        video_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.video_label = tk.Label(video_box, bg="#000000")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        self.stats_label = tk.Label(
            video_box,
            text="",
            bg="#000000",
            fg=MUTED,
            font=(FONT, 9),
            padx=10,
            pady=6,
        )
        self.stats_label.place(relx=1.0, rely=1.0, anchor="se")

        self.hint_label = tk.Label(
            video_box,
            text="",
            bg="#000000",
            fg=MUTED,
            font=(FONT, 12),
            justify=tk.CENTER,
            wraplength=760,
        )
        self.hint_label.place(relx=0.5, rely=0.5, anchor="center")

        self._refresh_ips()
        self._render_hint()

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
        self._set_pending_status(f"First run: downloading receiver component {ratio * 100:.0f}%", "wait")

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
                    self._set_pending_status("Waiting for phone stream... (tap Start Live in SplitCam)", "wait")
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

        self.root.after(15, self._pump)

    def _apply_status(self, text: str, kind: str) -> None:
        self._status_text = text
        self.status_label.configure(text=f"● {text}", fg=STATUS_COLORS.get(kind, MUTED))

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
            self.firewall_label.configure(text="Port 1935 is not allowed, the phone may fail to connect ->")
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

    def _on_close(self) -> None:
        self.stop_event.set()
        self.server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    CrDeckInspectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
