import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_SETTINGS_PATH = Path(__file__).parent / "settings.json"


def load() -> dict:
    """Load settings, injecting API keys from .env."""
    with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["deepgram"]["api_key"] = os.environ["DEEPGRAM_API_KEY"]
    cfg["openai"]["api_key"] = os.environ["OPENAI_API_KEY"]
    return cfg
