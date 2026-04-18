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

# Premium palette
_ACCENT      = "#1a6bc0"
_ACCENT_HVR  = "#2280e0"
_BG_SUB      = "#0c0c0c"
_BG_CTRL     = "#080808"
_BG_BTN      = "#1c1c1c"
_BG_BTN_HVR  = "#252525"
_FG_CURR     = "#f0f0f0"
_FG_PREV     = "#4a4a4a"
_FG_LABEL    = "#505050"
_FG_BTN      = "#888888"
_FG_BTN_ACT  = "#ffffff"
_SEP_LINE    = "#1e1e1e"
_FONT        = "Segoe UI"


def _hover(widget: tk.Widget, bg_on: str, bg_off: str,
           fg_on: str | None = None, fg_off: str | None = None) -> None:
    """Bind enter/leave hover colour change to a widget."""
    def on_enter(_e):
        widget.configure(bg=bg_on, **({"fg": fg_on} if fg_on else {}))
    def on_leave(_e):
        widget.configure(bg=bg_off, **({"fg": fg_off} if fg_off else {}))
    widget.bind("<Enter>", on_enter)
    widget.bind("<Leave>", on_leave)


class SubtitleBar:
    def __init__(self, languages: dict[str, str], cfg: dict | None = None,
                 default_lang: str = "fr",
                 input_languages: dict[str, str] | None = None,
                 initial_mic_lang: str = "en",
                 initial_loopback_lang: str = "en"):
        """
        languages:            {code: name}  translation target languages (FR, IT, etc.)
        cfg:                  overlay block from settings.json
        default_lang:         pre-selected translation language
        input_languages:      {code: name}  all available input languages (for mic/loopback selectors)
        initial_mic_lang:     language code pre-selected for mic
        initial_loopback_lang: language code pre-selected for loopback
        """
        self._languages = {k: languages[k] for k in _LANG_ORDER if k in languages}
        self._input_languages: dict[str, str] = input_languages or {"en": "English"}

        c = cfg or {}
        self._font_sz_curr: int = c.get("font_size_current",   15)
        self._font_sz_prev: int = c.get("font_size_previous",  11)
        self._bg:           str = c.get("background_color",    _BG_SUB)
        self._fg_curr:      str = c.get("text_color_current",  _FG_CURR)
        self._fg_prev:      str = c.get("text_color_previous", _FG_PREV)
        self._divider:      str = c.get("divider_color",       _SEP_LINE)
        self._sub_h:        int = c.get("height",              110)
        self._ctrl_h:       int = c.get("control_bar_height",  38)

        # Pre-select default so transcription starts immediately on launch
        self._sel_code: str = default_lang if default_lang in self._languages else (
            next(iter(self._languages), ""))
        self._sel_name: str = self._languages.get(self._sel_code, "")
        self._on_lang_change: Callable[[str, str], None] | None = None

        # Runtime mic / loopback language state
        self._mic_lang_code:      str = initial_mic_lang
        self._loopback_lang_code: str = initial_loopback_lang
        self._on_mic_lang_change:      Callable[[str, str], None] | None = None
        self._on_loopback_lang_change: Callable[[str, str], None] | None = None

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
        self._status_ring = None
        self._en_prev: tk.Label | None = None
        self._en_curr: tk.Label | None = None
        self._tr_prev: tk.Label | None = None
        self._tr_curr: tk.Label | None = None
        self._mic_lang_var:      tk.StringVar | None = None
        self._loopback_lang_var: tk.StringVar | None = None

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

    def set_on_mic_lang_change(self, callback: Callable[[str, str], None]) -> None:
        self._on_mic_lang_change = callback

    def set_on_loopback_lang_change(self, callback: Callable[[str, str], None]) -> None:
        self._on_loopback_lang_change = callback

    def get_mic_language(self) -> tuple[str, str]:
        return self._mic_lang_code, self._input_languages.get(self._mic_lang_code, self._mic_lang_code)

    def get_loopback_language(self) -> tuple[str, str]:
        return self._loopback_lang_code, self._input_languages.get(self._loopback_lang_code, self._loopback_lang_code)

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
        self._poll_queue()
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
        total_h = self._ctrl_h + self._sub_h + 2  # +2 for accent strip

        root.geometry(f"{sw}x{total_h}+0+{sh - total_h}")
        root.configure(bg=_BG_CTRL)
        root.title("Live Subtitles")
        root.overrideredirect(True)
        root.wm_attributes("-topmost", True)
        root.wm_attributes("-alpha", 0.95)

        # 2px blue accent strip at very top
        tk.Frame(root, bg=_ACCENT, height=2).pack(fill="x", side="top")

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

        wrap = min(sw // 2 - 40, 620)

        # Left panel: English
        left = tk.Frame(sub, bg=self._bg)
        left.pack(side="left", fill="both", expand=True)

        self._en_prev = tk.Label(
            left, text="", fg=self._fg_prev, bg=self._bg,
            font=(_FONT, self._font_sz_prev),
            anchor="w", padx=20, wraplength=wrap,
        )
        self._en_prev.pack(side="top", fill="x", pady=(16, 2))

        self._en_curr = tk.Label(
            left, text="", fg=self._fg_curr, bg=self._bg,
            font=(_FONT, self._font_sz_curr, "bold"),
            anchor="w", padx=20, wraplength=wrap,
        )
        self._en_curr.pack(side="top", fill="x")

        # 1px vertical divider
        tk.Frame(sub, bg=self._divider, width=1).pack(side="left", fill="y", pady=10)

        # Right panel: Translated
        right = tk.Frame(sub, bg=self._bg)
        right.pack(side="left", fill="both", expand=True)

        self._tr_prev = tk.Label(
            right, text="", fg=self._fg_prev, bg=self._bg,
            font=(_FONT, self._font_sz_prev),
            anchor="w", padx=20, wraplength=wrap,
        )
        self._tr_prev.pack(side="top", fill="x", pady=(16, 2))

        self._tr_curr = tk.Label(
            right, text="", fg=self._fg_curr, bg=self._bg,
            font=(_FONT, self._font_sz_curr, "bold"),
            anchor="w", padx=20, wraplength=wrap,
        )
        self._tr_curr.pack(side="top", fill="x")

    def _build_control_bar(self, root: tk.Tk, sw: int) -> None:
        # Thin separator between subtitle and control areas
        tk.Frame(root, bg="#161616", height=1).pack(fill="x", side="top")

        ctrl = tk.Frame(root, bg=_BG_CTRL, height=self._ctrl_h)
        ctrl.pack(fill="x", side="bottom")
        ctrl.pack_propagate(False)

        # ── Status pill ───────────────────────────────────────────────
        pill = tk.Frame(ctrl, bg="#141414", padx=8, pady=3)
        pill.pack(side="left", padx=(12, 10), pady=9)

        self._status_canvas = tk.Canvas(
            pill, width=8, height=8, bg="#141414", highlightthickness=0
        )
        self._status_canvas.pack(side="left", padx=(0, 5))
        self._status_dot = self._status_canvas.create_oval(
            0, 0, 8, 8, fill="#cc0000", outline=""
        )

        tk.Label(
            pill, text="LIVE", fg="#333333", bg="#141414",
            font=(_FONT, 7, "bold"), letterSpacing=2,
        ).pack(side="left")

        # ── Thin vertical rule ────────────────────────────────────────
        def _vsep(parent=ctrl, padx=(0, 10)):
            tk.Frame(parent, bg="#1e1e1e", width=1).pack(
                side="left", fill="y", pady=8, padx=padx
            )

        _vsep(padx=(0, 8))

        # ── Mic language selector ─────────────────────────────────────
        tk.Label(
            ctrl, text="Speak", fg=_FG_LABEL, bg=_BG_CTRL,
            font=(_FONT, 7),
        ).pack(side="left", padx=(0, 3))

        input_codes = list(self._input_languages.keys())
        self._mic_lang_var = tk.StringVar(
            value=self._mic_lang_code.upper()
            if self._mic_lang_code in self._input_languages
            else (input_codes[0].upper() if input_codes else "EN")
        )
        mic_menu = tk.OptionMenu(
            ctrl, self._mic_lang_var,
            *[c.upper() for c in input_codes],
            command=self._on_mic_lang_select,
        )
        mic_menu.config(
            bg=_BG_BTN, fg=_FG_BTN_ACT,
            activebackground=_ACCENT, activeforeground="#ffffff",
            relief="flat", bd=0, highlightthickness=0,
            font=(_FONT, 8, "bold"), width=2, cursor="hand2",
            indicatoron=False,
        )
        mic_menu["menu"].config(
            bg="#1c1c1c", fg="#cccccc",
            activebackground=_ACCENT, activeforeground="#ffffff",
            font=(_FONT, 8),
        )
        mic_menu.pack(side="left", padx=(0, 12))

        # ── Loopback language selector ────────────────────────────────
        tk.Label(
            ctrl, text="Loopback", fg=_FG_LABEL, bg=_BG_CTRL,
            font=(_FONT, 7),
        ).pack(side="left", padx=(0, 3))

        self._loopback_lang_var = tk.StringVar(
            value=self._loopback_lang_code.upper()
            if self._loopback_lang_code in self._input_languages
            else (input_codes[0].upper() if input_codes else "EN")
        )
        lb_menu = tk.OptionMenu(
            ctrl, self._loopback_lang_var,
            *[c.upper() for c in input_codes],
            command=self._on_loopback_lang_select,
        )
        lb_menu.config(
            bg=_BG_BTN, fg=_FG_BTN_ACT,
            activebackground=_ACCENT, activeforeground="#ffffff",
            relief="flat", bd=0, highlightthickness=0,
            font=(_FONT, 8, "bold"), width=2, cursor="hand2",
            indicatoron=False,
        )
        lb_menu["menu"].config(
            bg="#1c1c1c", fg="#cccccc",
            activebackground=_ACCENT, activeforeground="#ffffff",
            font=(_FONT, 8),
        )
        lb_menu.pack(side="left", padx=(0, 12))

        _vsep(padx=(0, 10))

        # ── Translation output language buttons ───────────────────────
        for code, name in self._languages.items():
            is_active = (code == self._sel_code)
            btn = tk.Button(
                ctrl,
                text=code.upper(),
                width=3,
                bg=_ACCENT if is_active else _BG_BTN,
                fg=_FG_BTN_ACT if is_active else _FG_BTN,
                activebackground=_ACCENT_HVR,
                activeforeground="#ffffff",
                relief="flat", bd=0,
                font=(_FONT, 8, "bold"),
                cursor="hand2",
                command=lambda c=code, n=name: self._do_select_lang(c, n),
            )
            btn.pack(side="left", padx=2, pady=6)
            if not is_active:
                _hover(btn, _BG_BTN_HVR, _BG_BTN, _FG_BTN_ACT, _FG_BTN)
            self._lang_buttons[code] = btn

        # Active language name label
        init_label = self._sel_name if self._sel_name else "← select"
        init_fg    = "#aaaaaa"     if self._sel_name else "#3a3a3a"
        self._lang_label = tk.Label(
            ctrl, text=init_label, fg=init_fg, bg=_BG_CTRL,
            font=(_FONT, 8),
        )
        self._lang_label.pack(side="left", padx=(10, 0))

        # ── Exit button (right-anchored) ──────────────────────────────
        exit_btn = tk.Button(
            ctrl,
            text="✕  Exit",
            bg=_BG_CTRL,
            fg="#3a3a3a",
            activebackground="#8b0000",
            activeforeground="#ffffff",
            relief="flat", bd=0,
            font=(_FONT, 8),
            cursor="hand2",
            command=self._on_exit_clicked,
        )
        exit_btn.pack(side="right", padx=(0, 14), pady=8)
        _hover(exit_btn, "#8b0000", _BG_CTRL, "#ffffff", "#3a3a3a")

    # ── OptionMenu handlers (run directly on tkinter thread) ─────────

    def _on_mic_lang_select(self, value: str) -> None:
        code = value.lower()
        name = self._input_languages.get(code, code)
        self._mic_lang_code = code
        if self._on_mic_lang_change:
            self._on_mic_lang_change(code, name)

    def _on_loopback_lang_select(self, value: str) -> None:
        code = value.lower()
        name = self._input_languages.get(code, code)
        self._loopback_lang_code = code
        if self._on_loopback_lang_change:
            self._on_loopback_lang_change(code, name)

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
            active = (c == code)
            btn.configure(
                bg=_ACCENT   if active else _BG_BTN,
                fg="#ffffff"  if active else _FG_BTN,
            )
            if active:
                btn.unbind("<Enter>")
                btn.unbind("<Leave>")
            else:
                _hover(btn, _BG_BTN_HVR, _BG_BTN, _FG_BTN_ACT, _FG_BTN)
        if self._lang_label:
            self._lang_label.configure(text=name, fg="#aaaaaa")
        if not self._lang_selected.is_set():
            self._lang_selected.set()
        elif self._on_lang_change:
            self._on_lang_change(code, name)

    def _do_set_status(self, connected: bool) -> None:
        if self._status_canvas and self._status_dot is not None:
            color = "#00e054" if connected else "#cc0000"
            self._status_canvas.itemconfig(self._status_dot, fill=color)
