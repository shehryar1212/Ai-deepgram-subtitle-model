"""
Floating subtitle overlay — always-on-top frameless tkinter window.
Sits at the bottom of the screen, full width.
Left panel: English transcript. Right panel: translated text.

Thread safety: external threads push (cmd, *args) tuples into _ui_queue.
The tkinter mainloop polls this queue every 50ms via root.after() — reliable
on Windows where calling root.after() directly from non-main threads can silently fail.
"""

import ctypes
import queue
import threading
import tkinter as tk
from typing import Callable

# Display order for language buttons
_LANG_ORDER = ["fr", "it", "pt", "es", "el", "bg", "sq"]


class SubtitleBar:
    def __init__(self, languages: dict[str, str], cfg: dict | None = None,
                 default_lang: str = "fr"):
        """
        languages:    {code: name}  e.g. {"fr": "French", "it": "Italian"}
        cfg:          overlay block from settings.json
        default_lang: pre-selected language so transcription starts without a click
        """
        self._languages = {k: languages[k] for k in _LANG_ORDER if k in languages}

        c = cfg or {}
        self._font_sz_curr: int = c.get("font_size_current",   16)
        self._font_sz_prev: int = c.get("font_size_previous",  14)
        self._bg:           str = c.get("background_color",    "#1a1a1a")
        self._fg_curr:      str = c.get("text_color_current",  "#ffffff")
        self._fg_prev:      str = c.get("text_color_previous", "#888888")
        self._divider:      str = c.get("divider_color",       "#333333")
        self._sub_h:        int = c.get("height",              120)
        self._ctrl_h:       int = c.get("control_bar_height",  36)

        # Pre-select default so transcription starts immediately on launch
        self._sel_code: str = default_lang if default_lang in self._languages else (
            next(iter(self._languages), ""))
        self._sel_name: str = self._languages.get(self._sel_code, "")
        self._on_lang_change: Callable[[str, str], None] | None = None

        self._root:   tk.Tk | None     = None
        self._thread: threading.Thread | None = None
        self._ready           = threading.Event()
        self._lang_selected   = threading.Event()
        self._stop_requested  = threading.Event()
        if self._sel_code:
            self._lang_selected.set()

        # Thread-safe command queue — polled by tkinter mainloop every 50ms
        self._ui_queue: queue.Queue = queue.Queue()

        # Two-line rolling state: [previous, current]
        self._en_lines: list[str] = ["", ""]
        self._tr_lines: list[str] = ["", ""]

        # Widget references (set during _build)
        self._lang_buttons:  dict[str, tk.Button] = {}
        self._lang_label:    tk.Label | None = None
        self._status_canvas: tk.Canvas | None = None
        self._status_dot = None
        self._en_prev: tk.Label | None = None
        self._en_curr: tk.Label | None = None
        self._tr_prev: tk.Label | None = None
        self._tr_curr: tk.Label | None = None

    # ── Public API (safe to call from any thread) ─────────────────────

    def start(self) -> None:
        """Launch tkinter mainloop in a background daemon thread."""
        self._thread = threading.Thread(target=self._run_ui, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def wait_for_language(self) -> tuple[str, str]:
        """Block the calling thread until the user clicks a language button."""
        self._lang_selected.wait()
        return self._sel_code, self._sel_name

    def set_on_language_change(self, callback: Callable[[str, str], None]) -> None:
        self._on_lang_change = callback

    def set_english(self, text: str) -> None:
        self._ui_queue.put(("english", text))

    def set_translated(self, text: str) -> None:
        self._ui_queue.put(("translated", text))

    def set_language(self, lang_code: str, lang_name: str) -> None:
        self._ui_queue.put(("language", lang_code, lang_name))

    def set_status(self, connected: bool) -> None:
        self._ui_queue.put(("status", connected))

    def get_selected_language(self) -> tuple[str, str]:
        return self._sel_code, self._sel_name

    def stop(self) -> None:
        self._ui_queue.put(("stop",))

    def stop_requested(self) -> bool:
        """Returns True if the user clicked Exit in the overlay."""
        return self._stop_requested.is_set()

    # ── UI construction ───────────────────────────────────────────────

    def _run_ui(self) -> None:
        self._root = tk.Tk()
        self._build(self._root)
        self._ready.set()
        self._poll_queue()      # start the 50ms polling loop
        self._root.mainloop()

    def _poll_queue(self) -> None:
        """Drain _ui_queue and dispatch commands — called every 50ms on the main thread."""
        try:
            while True:
                item = self._ui_queue.get_nowait()
                cmd = item[0]
                try:
                    if cmd == "english":
                        self._do_set_english(item[1])
                    elif cmd == "translated":
                        self._do_set_translated(item[1])
                    elif cmd == "language":
                        self._do_select_lang(item[1], item[2])
                    elif cmd == "status":
                        self._do_set_status(item[1])
                    elif cmd == "stop":
                        self._root.destroy()
                        return
                except Exception as exc:
                    print(f"[OVERLAY ERROR] cmd={cmd!r}: {exc}", flush=True)
        except queue.Empty:
            pass
        if self._root:
            self._root.after(50, self._poll_queue)

    def _build(self, root: tk.Tk) -> None:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        total_h = self._ctrl_h + self._sub_h

        root.geometry(f"{sw}x{total_h}+0+{sh - total_h}")
        root.configure(bg=self._bg)
        root.title("Live Subtitles")
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.93)

        # Subtitle area at top of window, control bar at bottom.
        # This way the subtitle text is visible above the control bar
        # even when other application windows are maximised below.
        self._build_subtitle_area(root, sw)
        self._build_control_bar(root, sw)

        root.update()
        try:
            hwnd = root.winfo_id()
            GWL_EXSTYLE      = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                                style | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _build_subtitle_area(self, root: tk.Tk, sw: int) -> None:
        sub = tk.Frame(root, bg=self._bg, height=self._sub_h)
        sub.pack(fill="x", side="top")
        sub.pack_propagate(False)

        wrap = min(sw // 2 - 32, 560)  # ~80 chars at Arial 13

        # Left panel: English
        left = tk.Frame(sub, bg=self._bg)
        left.pack(side="left", fill="both", expand=True)

        self._en_prev = tk.Label(
            left, text="", fg=self._fg_prev, bg=self._bg,
            font=("Arial", self._font_sz_prev),
            anchor="w", padx=16, wraplength=wrap,
        )
        self._en_prev.pack(side="top", fill="x", pady=(14, 1))

        self._en_curr = tk.Label(
            left, text="", fg=self._fg_curr, bg=self._bg,
            font=("Arial", self._font_sz_curr, "bold"),
            anchor="w", padx=16, wraplength=wrap,
        )
        self._en_curr.pack(side="top", fill="x")

        # Divider
        tk.Frame(sub, bg=self._divider, width=1).pack(side="left", fill="y")

        # Right panel: Translated
        right = tk.Frame(sub, bg=self._bg)
        right.pack(side="left", fill="both", expand=True)

        self._tr_prev = tk.Label(
            right, text="", fg=self._fg_prev, bg=self._bg,
            font=("Arial", self._font_sz_prev),
            anchor="w", padx=16, wraplength=wrap,
        )
        self._tr_prev.pack(side="top", fill="x", pady=(14, 1))

        self._tr_curr = tk.Label(
            right, text="", fg=self._fg_curr, bg=self._bg,
            font=("Arial", self._font_sz_curr, "bold"),
            anchor="w", padx=16, wraplength=wrap,
        )
        self._tr_curr.pack(side="top", fill="x")

    def _build_control_bar(self, root: tk.Tk, sw: int) -> None:
        ctrl = tk.Frame(root, bg="#111111", height=self._ctrl_h)
        ctrl.pack(fill="x", side="bottom")
        ctrl.pack_propagate(False)

        # Status dot
        self._status_canvas = tk.Canvas(ctrl, width=14, height=14,
                                        bg="#111111", highlightthickness=0)
        self._status_canvas.pack(side="left", padx=(10, 6), pady=11)
        self._status_dot = self._status_canvas.create_oval(
            1, 1, 13, 13, fill="#cc0000", outline="")

        tk.Label(ctrl, text="LIVE SUBTITLES", fg="#444444", bg="#111111",
                 font=("Arial", 8)).pack(side="left", padx=(0, 16))

        # Language buttons
        for code, name in self._languages.items():
            btn = tk.Button(
                ctrl,
                text=code.upper(),
                width=3,
                bg="#2a2a2a", fg="#aaaaaa",
                activebackground="#185FA5", activeforeground="#ffffff",
                relief="flat", bd=0,
                font=("Arial", 9, "bold"),
                cursor="hand2",
                command=lambda c=code, n=name: self._do_select_lang(c, n),
            )
            btn.pack(side="left", padx=2, pady=5)
            self._lang_buttons[code] = btn

        # Active language label
        init_label = self._sel_name if self._sel_name else "← select language"
        init_fg    = "#cccccc"      if self._sel_name else "#555555"
        self._lang_label = tk.Label(ctrl, text=init_label,
                                    fg=init_fg, bg="#111111",
                                    font=("Arial", 9))
        self._lang_label.pack(side="left", padx=(12, 0))

        # Highlight pre-selected button
        if self._sel_code in self._lang_buttons:
            self._lang_buttons[self._sel_code].configure(bg="#185FA5", fg="#ffffff")

        # Exit button (right side)
        tk.Button(
            ctrl,
            text="✕ Exit",
            bg="#111111",
            fg="#888888",
            activebackground="#cc0000",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            font=("Arial", 9, "bold"),
            cursor="hand2",
            command=self._on_exit_clicked,
        ).pack(side="right", padx=(0, 10), pady=8)

    # ── Command handlers (run on tkinter main thread via _poll_queue) ──

    def _on_exit_clicked(self) -> None:
        self._stop_requested.set()

    def _do_set_english(self, text: str) -> None:
        self._en_lines[0] = self._en_lines[1]
        self._en_lines[1] = text
        prev = f"— {self._en_lines[0]}" if self._en_lines[0] else ""
        self._en_prev.configure(text=prev)
        self._en_curr.configure(text=f"— {text}")

    def _do_set_translated(self, text: str) -> None:
        self._tr_lines[0] = self._tr_lines[1]
        self._tr_lines[1] = text
        prev = f"— {self._tr_lines[0]}" if self._tr_lines[0] else ""
        self._tr_prev.configure(text=prev)
        self._tr_curr.configure(text=f"— {text}")

    def _do_select_lang(self, code: str, name: str) -> None:
        self._sel_code = code
        self._sel_name = name
        for c, btn in self._lang_buttons.items():
            btn.configure(
                bg="#185FA5" if c == code else "#2a2a2a",
                fg="#ffffff"  if c == code else "#aaaaaa",
            )
        if self._lang_label:
            self._lang_label.configure(text=name, fg="#cccccc")
        if not self._lang_selected.is_set():
            self._lang_selected.set()
        elif self._on_lang_change:
            self._on_lang_change(code, name)

    def _do_set_status(self, connected: bool) -> None:
        if self._status_canvas and self._status_dot is not None:
            self._status_canvas.itemconfig(
                self._status_dot, fill="#00cc44" if connected else "#cc0000")
