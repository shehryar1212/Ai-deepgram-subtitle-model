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
        fieldbackground="#2a2a2a",
        background="#2a2a2a",
        foreground="#cccccc",
        selectbackground="#185FA5",
        selectforeground="#ffffff",
        arrowcolor="#aaaaaa",
    )
    _STYLE_DONE = True


# -------------------------------------------------------------------------


class DeviceSelector:
    """
    Modal startup dialog — returns (mic_device_idx, loopback_device_idx, input_lang_code).
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

    # ── Public ──────────────────────────────────────────────────────────

    def show(self) -> tuple[int | None, int | None, str]:
        """Block until the user clicks Start or closes window.
        Returns (mic_idx, loopback_idx, input_lang_code).
        """
        self._root = tk.Tk()
        self._root.title("Live Subtitles — Audio Setup")
        self._root.configure(bg="#1a1a1a")
        self._root.resizable(False, False)

        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        w, h = 540, 460
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

        return self._mic_idx, self._loopback_idx, self._input_lang

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

        # ── Title ──
        tk.Label(
            root,
            text="Audio Device Setup",
            fg="#ffffff",
            bg="#1a1a1a",
            font=("Arial", 13, "bold"),
        ).pack(pady=(18, 2))
        tk.Label(
            root,
            text="Select devices and language before starting transcription.",
            fg="#666666",
            bg="#1a1a1a",
            font=("Arial", 9),
        ).pack(pady=(0, 10))

        # ── Mic section ──
        mic_frame = tk.Frame(root, bg="#222222", padx=14, pady=10)
        mic_frame.pack(fill="x", padx=20, pady=(0, 8))

        tk.Label(
            mic_frame,
            text="Microphone  (your voice → right panel translation)",
            fg="#aaaaaa",
            bg="#222222",
            font=("Arial", 9, "bold"),
            anchor="w",
        ).pack(fill="x")

        mic_names = [f"[{i}]  {name}" for i, name in self._input_devices]
        self._mic_var = tk.StringVar()
        mic_combo = ttk.Combobox(
            mic_frame,
            textvariable=self._mic_var,
            values=mic_names,
            state="readonly",
            width=56,
            style="Dark.TCombobox",
        )
        mic_combo.pack(fill="x", pady=(4, 6))
        mic_combo.bind("<<ComboboxSelected>>", self._on_mic_changed)

        # Pre-select current mic
        self._mic_idx = self._select_combo(
            mic_combo, self._mic_idx, self._input_devices, default_first=True
        )

        # VU meter
        vu_row = tk.Frame(mic_frame, bg="#222222")
        vu_row.pack(fill="x")
        tk.Label(
            vu_row, text="Level:", fg="#555555", bg="#222222", font=("Arial", 8), width=6
        ).pack(side="left")
        self._mic_vu = tk.Canvas(
            vu_row, width=380, height=10, bg="#111111", highlightthickness=0
        )
        self._mic_vu.pack(side="left")
        self._mic_bar = self._mic_vu.create_rectangle(0, 0, 0, 10, fill="#00cc44", width=0)

        # ── Input language section ──
        lang_frame = tk.Frame(root, bg="#222222", padx=14, pady=10)
        lang_frame.pack(fill="x", padx=20, pady=(0, 8))

        tk.Label(
            lang_frame,
            text="Speak language  (language you will speak into the mic)",
            fg="#aaaaaa",
            bg="#222222",
            font=("Arial", 9, "bold"),
            anchor="w",
        ).pack(fill="x")

        # Build option strings: "English  [en]"
        lang_opts = [f"{name}  [{code}]" for code, name in self._input_languages.items()]
        self._lang_var = tk.StringVar()
        lang_combo = ttk.Combobox(
            lang_frame,
            textvariable=self._lang_var,
            values=lang_opts,
            state="readonly",
            width=56,
            style="Dark.TCombobox",
        )
        lang_combo.pack(fill="x", pady=(4, 2))
        lang_combo.bind("<<ComboboxSelected>>", self._on_lang_changed)

        # Pre-select English (or first available)
        self._preselect_input_lang(lang_combo)

        # ── Loopback section ──
        lb_frame = tk.Frame(root, bg="#222222", padx=14, pady=10)
        lb_frame.pack(fill="x", padx=20, pady=(0, 14))

        tk.Label(
            lb_frame,
            text="System audio loopback  (audience voice → left panel English)",
            fg="#aaaaaa",
            bg="#222222",
            font=("Arial", 9, "bold"),
            anchor="w",
        ).pack(fill="x")

        lb_opts = ["(disabled — single-mic mode)"] + [
            f"[{i}]  {name}" for i, name in self._loopback_devices
        ]
        self._lb_var = tk.StringVar()
        lb_combo = ttk.Combobox(
            lb_frame,
            textvariable=self._lb_var,
            values=lb_opts,
            state="readonly",
            width=56,
            style="Dark.TCombobox",
        )
        lb_combo.pack(fill="x", pady=(4, 2))
        lb_combo.bind("<<ComboboxSelected>>", self._on_lb_changed)

        if self._loopback_idx is not None and self._loopback_devices:
            self._loopback_idx = self._select_combo(
                lb_combo, self._loopback_idx, self._loopback_devices,
                default_first=True, offset=1  # offset for "(disabled)" entry
            )
        elif self._loopback_devices:
            lb_combo.current(1)
            self._loopback_idx = self._loopback_devices[0][0]
        else:
            lb_combo.current(0)
            self._loopback_idx = None
            tk.Label(
                lb_frame,
                text="No Stereo Mix found. Enable it in Windows Sound → Recording.",
                fg="#cc6600",
                bg="#222222",
                font=("Arial", 8),
            ).pack(anchor="w", pady=(2, 0))

        # ── Buttons ──
        btn_frame = tk.Frame(root, bg="#1a1a1a")
        btn_frame.pack(pady=6)

        tk.Button(
            btn_frame,
            text="Start",
            bg="#185FA5",
            fg="#ffffff",
            activebackground="#1e74cc",
            activeforeground="#ffffff",
            font=("Arial", 10, "bold"),
            width=12,
            relief="flat",
            cursor="hand2",
            command=self._on_start,
        ).pack(side="left", padx=10)

        tk.Button(
            btn_frame,
            text="Exit",
            bg="#3a3a3a",
            fg="#aaaaaa",
            activebackground="#555555",
            activeforeground="#ffffff",
            font=("Arial", 10),
            width=12,
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
