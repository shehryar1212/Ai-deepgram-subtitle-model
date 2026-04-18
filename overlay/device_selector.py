"""
Device selection dialog — shown at startup so the user can choose
mic, loopback, and transcription (input) language before transcription begins.

Features:
  - Dropdown of all input devices for mic selection
  - Dropdown of detected loopback/Stereo Mix devices for audience capture
  - Input language selection (language you will speak in → sent to Deepgram)
  - Live VU meter for the selected mic (confirms audio is flowing)
  - Dark theme matching the subtitle overlay
"""

import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
import sounddevice as sd

_LOOPBACK_KEYWORDS = ("stereo mix", "loopback", "what u hear", "wave out mix", "cable output", "vb-audio", "virtual cable")

_ACCENT  = "#1a6bc0"
_BG_ROOT = "#0e0e0e"
_BG_CARD = "#161616"
_BG_CTRL = "#1c1c1c"
_FG_HEAD = "#e0e0e0"
_FG_SUB  = "#555555"
_FG_HINT = "#404040"
_FONT    = "Segoe UI"

# --- ttk style for dark combobox -----------------------------------------
_STYLE_DONE = False


def _apply_dark_style(root: tk.Tk) -> None:
    global _STYLE_DONE
    if _STYLE_DONE:
        return
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "Dark.TCombobox",
        fieldbackground=_BG_CTRL,
        background=_BG_CTRL,
        foreground="#cccccc",
        selectbackground=_ACCENT,
        selectforeground="#ffffff",
        arrowcolor="#555555",
        bordercolor="#222222",
        lightcolor="#222222",
        darkcolor="#222222",
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", _BG_CTRL)],
        foreground=[("readonly", "#cccccc")],
    )
    _STYLE_DONE = True


# -------------------------------------------------------------------------


class DeviceSelector:
    """
    Modal startup dialog — returns (mic_device_idx, loopback_device_idx, input_lang_code, loopback_lang_code).
    loopback_device_idx is None if the user disables loopback.
    Raises SystemExit if the user closes the window or clicks Exit.
    """

    def __init__(
        self,
        current_mic: int | None = None,
        current_loopback: int | None = None,
        input_languages: dict[str, str] | None = None,
    ):
        self._mic_idx = current_mic
        self._loopback_idx = current_loopback
        self._cancelled = False
        self._root: tk.Tk | None = None

        # Input language options: code → name  (English is always available)
        self._input_languages: dict[str, str] = input_languages or {"en": "English"}
        self._input_lang: str = "en"  # default: English
        self._loopback_lang: str = "en"  # default: English

        # Level monitor state
        self._level_stream: sd.InputStream | None = None
        self._level_rms: float = 0.0
        self._level_lock = threading.Lock()

        # Device lists built in show()
        self._input_devices: list[tuple[int, str]] = []
        self._loopback_devices: list[tuple[int, str]] = []

        # Widget refs
        self._mic_vu: tk.Canvas | None = None
        self._mic_bar = None
        self._mic_var: tk.StringVar | None = None
        self._lb_var: tk.StringVar | None = None
        self._lang_var: tk.StringVar | None = None
        self._lb_lang_var: tk.StringVar | None = None

    # ── Public ──────────────────────────────────────────────────────────

    def show(self) -> tuple[int | None, int | None, str, str]:
        """Block until the user clicks Start or closes window.
        Returns (mic_idx, loopback_idx, input_lang_code, loopback_lang_code).
        """
        self._root = tk.Tk()
        self._root.title("Live Subtitles — Audio Setup")
        self._root.configure(bg=_BG_ROOT)
        self._root.resizable(False, False)

        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        w, h = 560, 490
        self._root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        _apply_dark_style(self._root)
        self._scan_devices()
        self._build()
        self._root.protocol("WM_DELETE_WINDOW", self._on_exit)
        self._start_level_monitor()
        self._poll_vu()
        self._root.mainloop()

        if self._cancelled:
            raise SystemExit(0)

        return self._mic_idx, self._loopback_idx, self._input_lang, self._loopback_lang

    # ── Device discovery ─────────────────────────────────────────────────

    def _scan_devices(self) -> None:
        devices = sd.query_devices()
        self._input_devices = [
            (i, dev["name"])
            for i, dev in enumerate(devices)
            if dev["max_input_channels"] > 0
        ]
        self._loopback_devices = [
            (i, dev["name"])
            for i, dev in enumerate(devices)
            if dev["max_input_channels"] > 0
            and any(kw in dev["name"].lower() for kw in _LOOPBACK_KEYWORDS)
        ]

    # ── UI construction ──────────────────────────────────────────────────

    def _build(self) -> None:
        root = self._root

        # ── Accent strip ──
        tk.Frame(root, bg=_ACCENT, height=3).pack(fill="x", side="top")

        # ── Title ──
        header = tk.Frame(root, bg=_BG_ROOT)
        header.pack(fill="x", padx=24, pady=(16, 4))
        tk.Label(
            header,
            text="Audio Device Setup",
            fg=_FG_HEAD,
            bg=_BG_ROOT,
            font=(_FONT, 13, "bold"),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            root,
            text="Configure inputs before starting transcription.",
            fg=_FG_SUB,
            bg=_BG_ROOT,
            font=(_FONT, 8),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(0, 12))

        def _card(label_text: str) -> tk.Frame:
            outer = tk.Frame(root, bg=_BG_ROOT)
            outer.pack(fill="x", padx=20, pady=(0, 6))
            card = tk.Frame(outer, bg=_BG_CARD, padx=14, pady=10)
            card.pack(fill="x")
            tk.Label(
                card, text=label_text, fg=_FG_HINT, bg=_BG_CARD,
                font=(_FONT, 7, "bold"), anchor="w",
            ).pack(fill="x", pady=(0, 4))
            return card

        # ── Mic section ──
        mic_frame = _card("MICROPHONE  —  your voice → right panel")

        mic_names = [f"[{i}]  {name}" for i, name in self._input_devices]
        self._mic_var = tk.StringVar()
        mic_combo = ttk.Combobox(
            mic_frame,
            textvariable=self._mic_var,
            values=mic_names,
            state="readonly",
            width=58,
            style="Dark.TCombobox",
        )
        mic_combo.pack(fill="x", pady=(0, 6))
        mic_combo.bind("<<ComboboxSelected>>", self._on_mic_changed)

        self._mic_idx = self._select_combo(
            mic_combo, self._mic_idx, self._input_devices, default_first=True
        )

        # VU meter
        vu_row = tk.Frame(mic_frame, bg=_BG_CARD)
        vu_row.pack(fill="x")
        tk.Label(
            vu_row, text="Level", fg=_FG_HINT, bg=_BG_CARD,
            font=(_FONT, 7), width=5,
        ).pack(side="left")
        self._mic_vu = tk.Canvas(
            vu_row, width=390, height=6, bg="#0c0c0c", highlightthickness=0
        )
        self._mic_vu.pack(side="left", pady=2)
        self._mic_bar = self._mic_vu.create_rectangle(0, 0, 0, 6, fill="#00cc44", width=0)

        # ── Speak language section ──
        lang_frame = _card("SPEAK LANGUAGE  —  language you speak into the mic")

        lang_opts = [f"{name}  [{code}]" for code, name in self._input_languages.items()]
        self._lang_var = tk.StringVar()
        lang_combo = ttk.Combobox(
            lang_frame,
            textvariable=self._lang_var,
            values=lang_opts,
            state="readonly",
            width=58,
            style="Dark.TCombobox",
        )
        lang_combo.pack(fill="x")
        lang_combo.bind("<<ComboboxSelected>>", self._on_lang_changed)
        self._preselect_input_lang(lang_combo)

        # ── Loopback device section ──
        lb_frame = _card("SYSTEM AUDIO LOOPBACK  —  incoming voice → left panel")

        lb_opts = ["(disabled — single-mic mode)"] + [
            f"[{i}]  {name}" for i, name in self._loopback_devices
        ]
        self._lb_var = tk.StringVar()
        lb_combo = ttk.Combobox(
            lb_frame,
            textvariable=self._lb_var,
            values=lb_opts,
            state="readonly",
            width=58,
            style="Dark.TCombobox",
        )
        lb_combo.pack(fill="x")
        lb_combo.bind("<<ComboboxSelected>>", self._on_lb_changed)

        if self._loopback_idx is not None and self._loopback_devices:
            self._loopback_idx = self._select_combo(
                lb_combo, self._loopback_idx, self._loopback_devices,
                default_first=True, offset=1
            )
        elif self._loopback_devices:
            lb_combo.current(1)
            self._loopback_idx = self._loopback_devices[0][0]
        else:
            lb_combo.current(0)
            self._loopback_idx = None
            tk.Label(
                lb_frame,
                text="No loopback device found — enable Stereo Mix in Windows Sound → Recording.",
                fg="#a05020",
                bg=_BG_CARD,
                font=(_FONT, 7),
                anchor="w",
            ).pack(fill="x", pady=(4, 0))

        # ── Loopback language section ──
        lb_lang_frame = _card("INCOMING VOICE LANGUAGE  —  language heard through loopback")

        lb_lang_opts = [f"{name}  [{code}]" for code, name in self._input_languages.items()]
        self._lb_lang_var = tk.StringVar()
        lb_lang_combo = ttk.Combobox(
            lb_lang_frame,
            textvariable=self._lb_lang_var,
            values=lb_lang_opts,
            state="readonly",
            width=58,
            style="Dark.TCombobox",
        )
        lb_lang_combo.pack(fill="x")
        lb_lang_combo.bind("<<ComboboxSelected>>", self._on_lb_lang_changed)
        self._preselect_loopback_lang(lb_lang_combo)

        # ── Buttons ──
        btn_frame = tk.Frame(root, bg=_BG_ROOT)
        btn_frame.pack(pady=(10, 6))

        start_btn = tk.Button(
            btn_frame,
            text="▶  Start",
            bg=_ACCENT,
            fg="#ffffff",
            activebackground="#2280e0",
            activeforeground="#ffffff",
            font=(_FONT, 10, "bold"),
            width=14,
            relief="flat",
            cursor="hand2",
            command=self._on_start,
        )
        start_btn.pack(side="left", padx=10)

        tk.Button(
            btn_frame,
            text="Exit",
            bg="#1e1e1e",
            fg="#555555",
            activebackground="#2a2a2a",
            activeforeground="#aaaaaa",
            font=(_FONT, 9),
            width=10,
            relief="flat",
            cursor="hand2",
            command=self._on_exit,
        ).pack(side="left", padx=10)

    @staticmethod
    def _select_combo(
        combo: ttk.Combobox,
        target_idx: int | None,
        device_list: list[tuple[int, str]],
        default_first: bool = True,
        offset: int = 0,
    ) -> int | None:
        """Select the combo row matching target_idx. Returns the device index."""
        if target_idx is not None:
            for j, (i, _) in enumerate(device_list):
                if i == target_idx:
                    combo.current(j + offset)
                    return target_idx
        if default_first and device_list:
            combo.current(0 + offset)
            return device_list[0][0]
        return None

    def _preselect_input_lang(self, combo: ttk.Combobox) -> None:
        """Pre-select English if available, otherwise first entry."""
        codes = list(self._input_languages.keys())
        if "en" in codes:
            combo.current(codes.index("en"))
            self._input_lang = "en"
        elif codes:
            combo.current(0)
            self._input_lang = codes[0]

    def _preselect_loopback_lang(self, combo: ttk.Combobox) -> None:
        """Pre-select English as the default loopback language."""
        codes = list(self._input_languages.keys())
        if "en" in codes:
            combo.current(codes.index("en"))
            self._loopback_lang = "en"
        elif codes:
            combo.current(0)
            self._loopback_lang = codes[0]

    # ── Event handlers ───────────────────────────────────────────────────

    def _on_mic_changed(self, _event=None) -> None:
        sel = self._mic_var.get()
        try:
            idx = int(sel.split("]")[0].lstrip("["))
            self._mic_idx = idx
            self._start_level_monitor()
        except (ValueError, IndexError):
            pass

    def _on_lb_changed(self, _event=None) -> None:
        sel = self._lb_var.get()
        if sel.startswith("(disabled"):
            self._loopback_idx = None
            return
        try:
            self._loopback_idx = int(sel.split("]")[0].lstrip("["))
        except (ValueError, IndexError):
            pass

    def _on_lb_lang_changed(self, _event=None) -> None:
        sel = self._lb_lang_var.get()
        try:
            self._loopback_lang = sel.split("[")[1].rstrip("]").strip()
        except (IndexError, AttributeError):
            pass

    def _on_lang_changed(self, _event=None) -> None:
        sel = self._lang_var.get()
        # Format: "English  [en]" — extract code from brackets
        try:
            self._input_lang = sel.split("[")[1].rstrip("]").strip()
        except (IndexError, AttributeError):
            pass

    def _on_start(self) -> None:
        self._stop_level_monitor()
        if self._root:
            self._root.destroy()
            self._root = None

    def _on_exit(self) -> None:
        self._cancelled = True
        self._stop_level_monitor()
        if self._root:
            self._root.destroy()
            self._root = None

    # ── VU meter ─────────────────────────────────────────────────────────

    def _start_level_monitor(self) -> None:
        self._stop_level_monitor()
        if self._mic_idx is None:
            return
        try:
            info = sd.query_devices(self._mic_idx, "input")
            rate = int(info["default_samplerate"])

            def _cb(indata, frames, time, status):
                rms = float(np.sqrt(np.mean(indata ** 2)))
                with self._level_lock:
                    self._level_rms = rms

            self._level_stream = sd.InputStream(
                samplerate=rate,
                channels=1,
                dtype="float32",
                device=self._mic_idx,
                blocksize=1024,
                callback=_cb,
            )
            self._level_stream.start()
        except Exception as e:
            print(f"[WARN] Level monitor: {e}", flush=True)

    def _stop_level_monitor(self) -> None:
        if self._level_stream is not None:
            try:
                self._level_stream.stop()
                self._level_stream.close()
            except Exception:
                pass
            self._level_stream = None
        with self._level_lock:
            self._level_rms = 0.0

    def _poll_vu(self) -> None:
        if self._root is None:
            return
        with self._level_lock:
            rms = self._level_rms
        # Scale: 0.1 RMS = full bar (speech is typically 0.02–0.15)
        ratio = min(rms / 0.1, 1.0)
        w = int(380 * ratio)
        if self._mic_vu and self._mic_bar is not None:
            self._mic_vu.coords(self._mic_bar, 0, 0, w, 10)
            color = "#00cc44" if ratio > 0.15 else "#ffaa00" if ratio > 0.02 else "#555555"
            self._mic_vu.itemconfig(self._mic_bar, fill=color)
        if self._root:
            self._root.after(50, self._poll_vu)
