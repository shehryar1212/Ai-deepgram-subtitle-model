"""
Translation module using OpenAI gpt-4o-mini.
Translates final English transcripts to a target language.
Glossary terms are injected into the system prompt to prevent translation.
Translation memory provides domain-specific term translations.
"""

import json
from pathlib import Path

from openai import OpenAI

from config import load

_cfg = load()["openai"]
_client = OpenAI(api_key=_cfg["api_key"])
_model = _cfg.get("model", "gpt-4o-mini")

_GLOSSARY_PATH = Path("config/glossary.json")
_MEMORY_PATH = Path("config/translation_memory.json")

# Lang code → full name mapping (for translation memory lookup)
_LANG_CODES = {
    "Italian": "it", "French": "fr", "Portuguese": "pt",
    "Greek": "el", "Bulgarian": "bg", "Albanian": "sq", "Spanish": "es",
}


def _load_protected_terms() -> list[str]:
    if not _GLOSSARY_PATH.exists():
        return []
    try:
        with open(_GLOSSARY_PATH, encoding="utf-8") as f:
            return json.load(f).get("protected_terms", [])
    except Exception:
        return []


def _load_translation_memory() -> dict:
    if not _MEMORY_PATH.exists():
        return {}
    try:
        with open(_MEMORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_protected_terms: list[str] = _load_protected_terms()
_translation_memory: dict = _load_translation_memory()


def _build_term_hints(target_lang: str) -> str:
    """Return a formatted list of term→translation hints for the target language."""
    lang_code = _LANG_CODES.get(target_lang, "")
    if not lang_code:
        return ""
    lines = []
    for term, translations in _translation_memory.items():
        translated = translations.get(lang_code, "")
        if translated and translated.lower() != term.lower():
            lines.append(f"- {term} → {translated}")
    if not lines:
        return ""
    return "The following terms must be translated exactly as specified:\n" + "\n".join(lines)


def translate(text: str, target_lang: str) -> str:
    """Translate text to target_lang (full language name). Returns translated string."""
    protected = _protected_terms
    protected_terms = ", ".join(protected) if protected else "none"
    term_hints = _build_term_hints(target_lang)

    term_hints_block = f"\n\n{term_hints}" if term_hints else ""

    system_prompt = f"""You are a professional subtitle translator.

Translate the following text from English to {target_lang}.

Rules:
- Translate only what is given — do not add, complete, or invent content
- Output must sound natural in {target_lang}
- Keep it concise — this is for live subtitles
- Never translate these terms, keep them exactly as written: {protected_terms}
- Return only the translated text, nothing else{term_hints_block}"""

    response = _client.chat.completions.create(
        model=_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        max_tokens=200,
    )

    return response.choices[0].message.content.strip()
