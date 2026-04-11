# Live Subtitle Translator

Real-time speech transcription and translation overlay for presentations and meetings.  
Captures microphone audio, transcribes via Deepgram, translates via OpenAI GPT, and displays a floating subtitle bar.  
Optionally captures system audio (audience language) via loopback for bidirectional translation.

---

## How It Works

```
Mic → Deepgram (English) → GPT → Right panel (target language)
System audio → Deepgram (audience lang) → GPT → Left panel (English)
```

---

## Prerequisites

- Windows 10 or 11
- Python 3.11+
- A [Deepgram](https://deepgram.com) API key (free tier available)
- An [OpenAI](https://platform.openai.com) API key

---

## Project Structure

```
faster-whisper/
├── main.py                        # Entry point
├── setup.bat                      # First-time setup (run once)
├── run.bat                        # Daily launcher
├── requirements.txt
├── .env                           # API keys (create from .env.example)
├── .env.example                   # Key template
│
├── config/
│   ├── __init__.py                # Config loader (reads .env + settings.json)
│   ├── settings.json              # All runtime settings
│   ├── glossary.json              # Protected terms (never translated)
│   └── translation_memory.json   # Term-by-term translation hints for GPT
│
├── audio_input/
│   ├── mic_capture.py             # Microphone input stream
│   └── loopback_capture.py        # WASAPI loopback / Stereo Mix capture
│
├── transcription/
│   ├── deepgram_engine.py         # Deepgram Nova-3 streaming client
│   ├── corrector.py               # Word corrections + disfluency cleanup
│   └── audience_pipeline.py       # Second pipeline for system audio
│
├── translation/
│   └── translator.py              # OpenAI GPT translation
│
├── overlay/
│   ├── device_selector.py         # Startup device selection dialog
│   └── subtitle_bar.py            # Floating always-on-top subtitle window
│
├── output/
│   └── writer.py                  # Writes subtitle.txt for OBS
│
└── logs/                          # Session logs (auto-created)
```

---

## Setup

### 1. First-time install

Double-click **`setup.bat`** — it will:

1. Check Python is installed (3.10+ required — [download here](https://www.python.org/downloads/))
2. Create the virtual environment automatically
3. Install all dependencies from `requirements.txt`

> **Only needs to be run once.** After setup completes, use `run.bat` every time.

### 2. Configure API keys

Copy `.env.example` to `.env` and fill in your keys:

```
DEEPGRAM_API_KEY=your_deepgram_key_here
OPENAI_API_KEY=your_openai_key_here
```

`.env` is loaded automatically at startup. Never commit it to version control.

### 3. Configure settings

Edit `config/settings.json` — all parameters are documented below.

---

## Configuration Reference (`settings.json`)

```jsonc
{
  "debug": false,          // true = verbose terminal output (RAW/CORRECTED/LATENCY)

  "deepgram": {
    "model": "nova-3",     // Deepgram model
    "language": "en",      // Speaker language code
    "endpointing": 2000,   // ms of silence before Deepgram sends is_final
    "utterance_end_ms": 2500,
    "buffer_timeout_seconds": 3.0,
    "buffer_max_words": 35,
    "keywords": []         // Deepgram keyterm hints ("word:boost")
  },

  "audio": {
    "mic_gain": 1.5,       // Amplify mic input (1.0–3.0)
    "blocksize": 4096,
    "channels": 1,
    "device": null         // null = use device selector; or set numeric index to skip selector
  },

  "languages": {           // Translation targets shown as buttons in overlay
    "fr": "French",
    "it": "Italian"
  },

  "overlay": {
    "default_language": "fr",
    "font_size_current": 13,
    "font_size_previous": 11,
    "background_color": "#1a1a1a",
    "text_color_current": "#ffffff",
    "text_color_previous": "#888888",
    "height": 120,
    "control_bar_height": 36
  },

  "audience": {
    "enabled": true,         // false = disable loopback pipeline entirely
    "language_code": "fr",   // language the OTHER person speaks (not your language)
    "language_name": "French",
    "min_words": 2
  }
}
```

---

## Audio Device Setup (Windows)

### Microphone

A device selector dialog appears at startup. All input devices are listed by name. Select your mic and confirm the **VU meter** shows movement when you speak (green = good level, orange = low, grey = silence).

### System Audio Loopback (Audience Pipeline)

Captures audio playing through your speakers — i.e. the other person speaking in a Teams/Meet call.

**Requires VB-Audio Virtual Cable (free):**

1. Download and install from [vb-audio.com](https://vb-audio.com/Cable/) — restart after install
2. Right-click the speaker icon in the taskbar → Open Sound settings → set Output to **CABLE Input (VB-Audio Virtual Cable)**
3. Press `Win + R`, type `mmsys.cpl`, press Enter → go to **Recording** tab → right-click **CABLE Output** → **Properties**
4. Go to the **Listen** tab → check **"Listen to this device"** → select your headphones in the dropdown → click **OK**
5. Test: play any audio — you should still hear it through your headphones

The device selector automatically detects CABLE Output once installed.

**Microphone permissions:** Settings → Privacy & Security → Microphone → allow access.

### Setting the Audience Language

Before each session, set the language the **other person** speaks in `config/settings.json`:

```json
"audience": {
  "enabled": true,
  "language_code": "fr",
  "language_name": "French"
}
```

Supported language codes:

| Language | `language_code` | `language_name` |
|---|---|---|
| French | `fr` | `French` |
| Italian | `it` | `Italian` |
| Spanish | `es` | `Spanish` |
| Portuguese | `pt` | `Portuguese` |
| Greek | `el` | `Greek` |
| Bulgarian | `bg` | `Bulgarian` |
| Albanian | `sq` | `Albanian` |
| English | `en` | `English` |

> If the other person speaks English (e.g. you are testing with an English YouTube video), set `language_code` to `en`.

---

## Running the App

### Option A — Double-click (recommended)

| File | Purpose |
|------|---------|
| `setup.bat` | First-time install only — creates venv, installs dependencies |
| `run.bat` | Daily launcher — activate venv and start the app |

1. Run `setup.bat` once on first install
2. From then on, double-click `run.bat` every time

### Option B — Terminal

```bat
venv\Scripts\activate
python main.py
```

### Startup sequence

1. **Device selector** — pick mic and loopback, watch VU meter, click **Start**
2. **Subtitle overlay** appears at bottom of screen
3. Select target language (default: French)
4. Speak — left panel shows English, right panel shows translation
5. Click **✕ Exit** in the overlay or Ctrl+C in terminal to stop

---

## Debug Mode

Set `"debug": true` in `settings.json`:

```
[RAW]:             Deepgram transcript before correction
[CORRECTED]:       After word fixes and disfluency cleanup
[TRANSLATED]:      Final GPT output
[MERGED]:          Whether this segment was joined with the previous
[CORRECTION_RULE]: Which correction rule applied
[LATENCY]:         GPT round-trip time in ms
[PARTIAL]:         Live interim transcripts while speaking
```

---

## OBS Integration

The app writes `subtitle.txt` after each transcription.

**In OBS:**

1. Add a **Text (GDI+)** source
2. Enable **Read from file**
3. Point to `subtitle.txt` in the project folder
4. Style font, colour, and position as needed

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No transcription | Check Deepgram key in `.env`. Confirm VU meter moves when speaking. |
| Wrong language transcribed | Set `deepgram.language` in settings to match your speaking language. |
| Audience pipeline disabled | The loopback device was set to disabled in the device selector, or `audience.enabled` is false in settings. |
| Loopback captures silence | Confirm CABLE Input is set as default Windows playback and "Listen to this device" is enabled on CABLE Output. |
| Wrong language from audience | Update `audience.language_code` and `audience.language_name` in `settings.json` to match the language the other person speaks. |
| VU meter flat | Wrong mic selected, or mic permissions denied in Windows Privacy settings. |
| Overlay not visible | Look at the very bottom of screen. May appear behind the taskbar if taskbar is auto-hiding. |
| High transcription latency | Lower `endpointing` (e.g. 1500 ms) for faster sentence finalization. |
| Repeated or merged sentences | Raise `endpointing` to give Deepgram more silence time before finalizing. |
| App crashes on start | Run from terminal to see the full error. Check both keys are in `.env`. |
