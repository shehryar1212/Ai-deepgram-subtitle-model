"""
Context-aware transcript correction.

Loads correction rules from config/corrections.json.
Each rule has: wrong, right, and optional context list.
Context rules only apply when a context word appears in the transcript.
"""

import json
import re
from pathlib import Path


def _clean_disfluencies(text: str) -> str:
    # Remove immediate word repetitions: "and and" → "and", "it was it was" → "it was"
    # Pattern: word or phrase repeated immediately
    text = re.sub(r'\b(\w+)\s+\1\b', r'\1', text, flags=re.IGNORECASE)
    # Remove repeated 2-word phrases: "it was it was" → "it was"
    text = re.sub(r'\b(\w+ \w+)\s+\1\b', r'\1', text, flags=re.IGNORECASE)
    return text

_CORRECTIONS_PATH = Path("config/corrections.json")


def _load_corrections() -> list[dict]:
    if not _CORRECTIONS_PATH.exists():
        return []
    with open(_CORRECTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


_corrections: list[dict] = _load_corrections()


def correct(text: str) -> tuple[str, str]:
    """Returns (corrected_text, rule_applied)"""
    text = _clean_disfluencies(text)
    for entry in _corrections:
        wrong = entry["wrong"]
        right = entry["right"]
        context = entry.get("context", [])

        pattern = rf'\b{re.escape(wrong)}\b'

        if context:
            if any(c.lower() in text.lower() for c in context):
                if re.search(pattern, text, flags=re.IGNORECASE):
                    text = re.sub(pattern, right, text, flags=re.IGNORECASE)
                    return text, f"{wrong}→{right}"
        else:
            if re.search(pattern, text, flags=re.IGNORECASE):
                text = re.sub(pattern, right, text, flags=re.IGNORECASE)
                return text, f"{wrong}→{right}"

    return text, "none"
