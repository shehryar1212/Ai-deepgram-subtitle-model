"""
One-time conversion script: terms.json → config/glossary.json + config/translation_memory.json
"""

import json
from pathlib import Path

LANGS = ["fr", "it", "pt", "el", "bg", "sq", "es"]
TERMS_PATH = Path("terms.json")
GLOSSARY_PATH = Path("config/glossary.json")
MEMORY_PATH = Path("config/translation_memory.json")

# Acronyms that must never be translated (user-specified)
PROTECTED_TERMS = ["OEE", "OOE", "TEEP", "KPI"]

data = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
terms = data["terms"]

# Build translation memory: all terms that have at least one target-lang translation
translation_memory = {}
for term, entry in terms.items():
    trans = entry.get("translations", {})
    row = {lang: trans[lang] for lang in LANGS if lang in trans}
    if row:
        translation_memory[term] = row

# Write glossary.json
glossary = {"protected_terms": PROTECTED_TERMS}
GLOSSARY_PATH.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {GLOSSARY_PATH} ({len(PROTECTED_TERMS)} protected terms)")

# Write translation_memory.json
MEMORY_PATH.write_text(json.dumps(translation_memory, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Wrote {MEMORY_PATH} ({len(translation_memory)} terms)")
