#!/usr/bin/env python3
"""
Evocon UI label scraper.

Extracts all UI text labels across 8 languages and 6 pages using pydoll (CDP)
for browser control and BeautifulSoup for text extraction.

Outputs:
  evocon_glossary.json        — raw labels per language per page
  evocon_translation_map.json — flat English → all languages mapping
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup
from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL  = "https://app.evocon.com"

LANGUAGES = ["en", "el", "fr", "it", "es", "pt", "bg", "sq"]

PAGE_URLS = {
    "dashboard":        f"{BASE_URL}/#/dashboard",
    "batch_process":    f"{BASE_URL}/#/shiftview/25/14392",
    "reports":          f"{BASE_URL}/#/reports2",
    "settings":         f"{BASE_URL}/#/settings",
    "factory_overview": f"{BASE_URL}/#/factory-view/realtime",
    "profile":          f"{BASE_URL}/#/settings/profile",
}

OUT_DIR       = Path(__file__).parent
GLOSSARY_PATH = OUT_DIR / "evocon_glossary.json"
MAP_PATH      = OUT_DIR / "evocon_translation_map.json"
PROGRESS_PATH = OUT_DIR / ".scraper_progress.json"

# ── Text filtering ─────────────────────────────────────────────────────────────

_RE_EMAIL     = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_RE_URL       = re.compile(r"https?://|www\.")
_RE_DATE      = re.compile(r"^\d{1,2}[./_-]\d{1,2}([./_-]\d{2,4})?$")
_RE_TIME      = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_RE_NUMS_ONLY = re.compile(r"^[\d\s.,:%+\-/()\u00b0]+$")


_PLACEHOLDER_STRINGS = {"built files will be auto injected"}


def is_valid_label(text: str) -> bool:
    t = text.strip()
    if t in _PLACEHOLDER_STRINGS:
        return False
    if len(t) < 2 or len(t) > 60:
        return False
    if _RE_NUMS_ONLY.match(t):
        return False
    if _RE_EMAIL.search(t):
        return False
    if _RE_URL.search(t):
        return False
    if _RE_DATE.match(t):
        return False
    if _RE_TIME.match(t):
        return False
    return True


def extract_labels(html: str) -> list[str]:
    """Extract all valid UI labels from HTML in DOM order, deduped."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "meta", "link", "noscript", "svg", "path"]):
        tag.decompose()
    seen: set[str] = set()
    labels: list[str] = []
    for node in soup.find_all(string=True):
        t = node.strip()
        if t and is_valid_label(t) and t not in seen:
            seen.add(t)
            labels.append(t)
    return labels


def extract_nav_labels(html: str) -> list[str]:
    """Extract labels from nav/sidebar elements only."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "meta", "link", "noscript"]):
        tag.decompose()
    nav_elements = (
        soup.select("nav") +
        soup.select("aside") +
        soup.select("[role='navigation']") +
        soup.select("[class*='sidebar']") +
        soup.select("[class*='Sidebar']") +
        soup.select("[class*='navigation']") +
        soup.select("[class*='Navigation']") +
        soup.select("[class*='menu']") +
        soup.select("[class*='Menu']")
    )
    seen: set[str] = set()
    labels: list[str] = []
    for el in nav_elements:
        for node in el.find_all(string=True):
            t = node.strip()
            if t and is_valid_label(t) and t not in seen:
                seen.add(t)
                labels.append(t)
    return labels


# ── Progress ───────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    for path in (PROGRESS_PATH, GLOSSARY_PATH):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and any(k in LANGUAGES for k in data):
                    return data
            except Exception:
                pass
    return {}


def save_progress(glossary: dict) -> None:
    PROGRESS_PATH.write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    GLOSSARY_PATH.write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[INFO] Progress saved → {GLOSSARY_PATH.name}")



CONTENT_INDICATORS = {
    "batch_process": ["Downtime", "Speed loss", "Scrap", "Product changeover", "Batch"],
    "reports": ["Report", "Export", "Filter", "Date"],
    "factory_overview": ["Factory", "Station", "Operating", "OEE"],
}

# ── Render helpers ────────────────────────────────────────────────────────────

def _js_value(result):
    """Extract the JS return value from pydoll's raw CDP execute_script response."""
    if isinstance(result, str):
        return result
    try:
        return result["result"]["result"]["value"]
    except (KeyError, TypeError):
        return None


async def get_dom(tab) -> str:
    """Return the live rendered DOM via JS (not raw page source)."""
    result = await tab.execute_script("return document.documentElement.outerHTML;")
    val = _js_value(result)
    return val if isinstance(val, str) else ""


async def wait_for_render(tab, timeout: int = 20) -> None:
    """Wait until React has rendered meaningful content (>500 chars of body text)."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            result = await tab.execute_script(
                "return document.body ? document.body.innerText.trim().length : 0;"
            )
            char_count = _js_value(result)
            if isinstance(char_count, (int, float)) and char_count > 500:
                await asyncio.sleep(2)
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    print("[WARN] Render timeout — grabbing whatever is available")


async def wait_for_content(tab, page_key: str) -> None:
    """For pages with deferred API data, wait until known content indicators appear.
    If none appear within 10s, scroll down and wait 3s more before giving up.
    """
    indicators = CONTENT_INDICATORS.get(page_key)
    if not indicators:
        return
    start = time.time()
    while time.time() - start < 10:
        try:
            result = await tab.execute_script(
                "return document.body ? document.body.innerText : '';"
            )
            body_text = _js_value(result) or ""
            if any(ind in body_text for ind in indicators):
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    # No indicators found — scroll to trigger lazy load then wait 3s more
    await scroll_page(tab)
    await asyncio.sleep(3)


async def scroll_page(tab) -> None:
    await tab.execute_script("""
        window.scrollTo(0, document.body.scrollHeight / 2);
    """)
    await asyncio.sleep(1)
    await tab.execute_script("window.scrollTo(0, 0);")
    await asyncio.sleep(0.5)


# ── Login ──────────────────────────────────────────────────────────────────────

async def login(tab) -> None:
    print("[INFO] Navigating to login page...")
    await tab.go_to(f"{BASE_URL}/login")
    await asyncio.sleep(2)

    print("\n" + "="*60)
    print("  Browser is open. Please log in manually.")
    print("  Once you are logged in and the dashboard is visible,")
    print("  come back here and press Enter to start scraping.")
    print("="*60)

    # Run blocking input() in a thread so the event loop stays alive
    await asyncio.get_event_loop().run_in_executor(None, input, "\n  Press Enter when logged in... ")

    current = await tab.current_url
    if "login" in str(current).lower():
        print("[WARN] Still on login page — make sure you are fully logged in before pressing Enter.")
        await asyncio.get_event_loop().run_in_executor(None, input, "  Press Enter again when ready... ")
    else:
        print(f"[INFO] Logged in. URL: {current}")

    await asyncio.sleep(5)


# ── Language change ────────────────────────────────────────────────────────────

async def navigate_to(tab, full_url: str) -> None:
    """Navigate to a hash URL by setting window.location.hash directly.
    full_url is the complete URL e.g. https://app.evocon.com/#/reports
    Extracts the hash fragment and sets it — updates the URL bar and triggers React Router."""
    hash_fragment = full_url.split("#", 1)[1] if "#" in full_url else "/"
    await tab.execute_script(f"window.location.hash = '{hash_fragment}';")
    await asyncio.sleep(2)
    await wait_for_render(tab)



# Maps language code → terms that may appear as the option text in the UI dropdown.
# Evocon may show native names, English names, or the code itself.
_LANG_TERMS: dict[str, list[str]] = {
    "en": ["english", "en"],
    "el": ["ελληνικά", "ελληνικα", "greek", "el"],
    "fr": ["français", "francais", "french", "fr"],
    "it": ["italiano", "italian", "it"],
    "es": ["español", "espanol", "spanish", "es"],
    "pt": ["português", "portugues", "portuguese", "pt"],
    "bg": ["български", "bulgarski", "bulgarian", "bg"],
    "sq": ["shqip", "albanian", "sq"],
}


_FIND_LIST_JS = """
(function(pos) {
    // Walk up from first [role="option"] to find the scrollable container
    var list = null;
    var opt = document.querySelector('[role="option"]');
    if (opt) {
        var el = opt.parentElement;
        while (el && el !== document.body) {
            if (el.scrollHeight > el.clientHeight + 10) { list = el; break; }
            el = el.parentElement;
        }
    }
    // Fallback: Vuetify overlay/menu containers
    if (!list) {
        var cands = Array.from(document.querySelectorAll(
            '[class*="v-overlay__content"],[class*="v-menu__content"],[class*="v-select__content"]'
        )).filter(function(el) { return el.scrollHeight > el.clientHeight + 10; });
        if (cands.length) list = cands[0];
    }
    if (!list) return 0;
    list.scrollTop = pos;
    return list.scrollHeight;
})(SCROLL_POS)
"""


async def _find_dropdown_list(tab) -> int:
    """Reset dropdown scroll to top and return its scrollHeight (0 if not found)."""
    result = await tab.execute_script(_FIND_LIST_JS.replace("SCROLL_POS", "0"))
    return int(_js_value(result) or 0)


async def _scroll_dropdown_to(tab, pos: int) -> None:
    """Set the open dropdown list's scrollTop to pos."""
    await tab.execute_script(_FIND_LIST_JS.replace("SCROLL_POS", str(pos)))


async def change_language(tab, lang_code: str) -> bool:
    """Navigate to profile, select target language, save. Returns True if switched."""
    lang_switched = False
    await navigate_to(tab, PAGE_URLS["profile"])
    # Wait explicitly for the profile form (language select) to be present.
    # Profile page has few text nodes so wait_for_render often times out — use a
    # direct element check with a longer window (30 × 0.5s = 15s).
    for _ in range(30):
        result = await tab.execute_script("""
return !!(document.querySelector('select') ||
          document.querySelector('[role="combobox"]') ||
          document.querySelector('[class*="v-select"]'));
""")
        if _js_value(result):
            break
        await asyncio.sleep(0.5)
    await scroll_page(tab)

    terms = _LANG_TERMS.get(lang_code, [lang_code])
    terms_js = json.dumps(terms)  # safe JSON array for injection

    # Attempt 1: native <select> with a matching option
    changed = await tab.execute_script(f"""
(function() {{
    var terms = {terms_js};
    var selects = document.querySelectorAll("select");
    for (var s of selects) {{
        for (var opt of s.options) {{
            var v = opt.value.toLowerCase();
            var t = opt.text.toLowerCase();
            if (terms.some(function(k) {{ return v === k || t === k || t.startsWith(k + ' '); }})) {{
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value').set;
                setter.call(s, opt.value);
                s.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return 'native-select:' + opt.value;
            }}
        }}
    }}
    return null;
}})();
""")

    changed = _js_value(changed)
    if changed:
        print(f"  [INFO] Language set via native select: {changed}")
        lang_switched = True
    else:
        # Attempt 2: React Select / custom dropdown — find Language label, open it, pick option
        opened = await tab.execute_script(f"""
(function() {{
    var allText = document.querySelectorAll(
        "label, [class*='label'], [class*='Label'], span, p"
    );
    for (var el of allText) {{
        var t = el.textContent.trim().toLowerCase();
        var langWords = ['language','langue','lingua','idioma','γλώσσα','език','gjuha','sprache','taal','keel','jazyk','jezik'];
        if (langWords.indexOf(t) !== -1) {{
            var parent = el.closest('[class*="field"], [class*="Field"], [class*="form-group"], [class*="formGroup"]')
                       || el.parentElement;
            if (parent) {{
                var control = parent.querySelector(
                    '[class*="control"], [class*="Control"], ' +
                    '[class*="select"], [class*="Select"], ' +
                    '[class*="dropdown"], [class*="Dropdown"], ' +
                    '[role="combobox"], [role="button"]'
                );
                if (control) {{ control.click(); return 'opened-via-label'; }}
                if (el.nextElementSibling) {{ el.nextElementSibling.click(); return 'opened-via-sibling'; }}
            }}
        }}
    }}
    return null;
}})();
""")

        opened = _js_value(opened)
        if opened:
            # Strategy 1: wait for initial v-lazy render (up to 4s)
            rendered = 0
            for _ in range(14):
                result = await tab.execute_script(
                    "return Array.from(document.querySelectorAll('[role=\"option\"]'))"
                    ".filter(function(o){return o.textContent.trim();}).length;"
                )
                rendered = int(_js_value(result) or 0)
                if rendered > 0:
                    break
                await asyncio.sleep(0.3)

            if rendered == 0:
                # Strategy 2: Vue component internals — get items list, click by index.
                # Works even when v-lazy hasn't rendered text (IntersectionObserver not fired
                # because Chrome window is in background).
                vue_click = await tab.execute_script(f"""
(function() {{
    var terms = {terms_js};
    // Walk up from combobox to find the VSelect component instance via Vue internals.
    // __vueParentComponent on any DOM element gives the component that rendered it.
    // Walking .parent traverses up the component tree to reach VSelect which has props.items.
    var root = document.querySelector('[role="combobox"]') ||
               document.querySelector('[aria-haspopup="listbox"]') ||
               document.querySelector('[class*="v-select"]');
    if (!root) return 'no-combobox';
    var el = root;
    while (el && el !== document.body) {{
        if (el.__vueParentComponent) {{
            var comp = el.__vueParentComponent;
            var depth = 0;
            while (comp && depth < 25) {{
                if (comp.props && Array.isArray(comp.props.items) && comp.props.items.length > 3) {{
                    var items = comp.props.items;
                    var opts = document.querySelectorAll('[role="option"]');
                    for (var i = 0; i < items.length; i++) {{
                        var item = items[i];
                        var t = (typeof item === 'string' ? item :
                            (item.title || item.label || item.text || item.value || '')).toLowerCase();
                        if (terms.some(function(k) {{ return t === k || t.indexOf(k) === 0; }})) {{
                            if (opts[i]) {{
                                opts[i].click();
                                return 'vue-click:' + t + ':idx' + i;
                            }}
                        }}
                    }}
                    // No match — dump items for debugging
                    var dump = Array.from(items).map(function(it, idx) {{
                        return idx + ':' + (typeof it === 'string' ? it : JSON.stringify(it));
                    }}).join(' | ');
                    return 'vue-no-match(' + items.length + '): ' + dump.slice(0, 400);
                }}
                comp = comp.parent;
                depth++;
            }}
            break;
        }}
        el = el.parentElement;
    }}
    return 'no-vue-instance';
}})();
""")
                vue_click = _js_value(vue_click) or ''
                print(f"  [DEBUG] Vue internals: {vue_click[:150]}")
                if vue_click.startswith('vue-click:'):
                    lang_switched = True

            if not lang_switched:
                # Strategy 3: match by visible textContent (works when options did render)
                selected = await tab.execute_script(f"""
(function() {{
    var terms = {terms_js};
    var options = document.querySelectorAll('[role="option"]');
    var found = null;
    options.forEach(function(o) {{
        var v = (o.getAttribute('data-value') || o.getAttribute('value') || '').toLowerCase();
        var t = o.textContent.trim().toLowerCase();
        if (!found && terms.some(function(k) {{ return v === k || t === k || t.startsWith(k + ' '); }})) {{
            found = o;
        }}
    }});
    if (found) {{
        found.scrollIntoView();
        found.click();
        return 'selected: ' + found.textContent.trim();
    }}
    // Debug: dump ALL options with their text
    var dump = Array.from(options).map(function(o) {{
        var txt = o.textContent.trim();
        var dv  = o.getAttribute('data-value') || '';
        var val = o.getAttribute('value') || '';
        var cls = (o.className || '').slice(0, 40);
        return txt + ' [dv=' + dv + ' val=' + val + ' cls=' + cls + ']';
    }});
    return 'NO-MATCH (' + options.length + ' options):\\n' + dump.join('\\n');
}})();
""")
                selected = _js_value(selected)
                if selected and selected.startswith('selected:'):
                    print(f"  [INFO] Language option {selected}")
                    lang_switched = True
                else:
                    print(f"  [WARN] Language option not matched for '{lang_code}':")
                    for line in (selected or '').split('\\n'):
                        print(f"         {line}")
            else:
                print(f"  [INFO] Language option selected via Vue internals")
        else:
            # Dump all short text nodes on the page to identify the label text
            label_dump = await tab.execute_script("""
(function() {
    var seen = {};
    var out = [];
    document.querySelectorAll('label, [class*="label"], [class*="Label"], span, p').forEach(function(el) {
        var t = el.textContent.trim();
        if (t && t.length < 30 && !seen[t]) { seen[t] = 1; out.push(t); }
    });
    return out.slice(0, 60).join(' | ');
})();
""")
            label_dump = _js_value(label_dump) or ''
            print(f"  [WARN] Could not find language dropdown for '{lang_code}'")
            print(f"         Page short-text labels: {label_dump[:300]}")

    if not lang_switched:
        print(f"  [SKIP] Language switch failed — skipping scrape to avoid saving wrong data.")
        return False

    # Click Save
    saved = await tab.execute_script("""
(function() {
    var kws = ['save', 'salva', 'enregistrer', 'guardar', 'αποθήκευση', 'запазване', 'ruaj'];
    var spans = document.querySelectorAll('[id="evocon-button-text"]');
    for (var s of spans) {
        var t = s.textContent.trim().toLowerCase();
        if (kws.some(function(k) { return t === k; })) {
            var btn = s.closest('button');
            if (btn) { btn.click(); return 'clicked-by-id: ' + s.textContent.trim(); }
        }
    }
    var btns = document.querySelectorAll("button, input[type='submit']");
    for (var b of btns) {
        var txt = (b.textContent || b.value || '').trim().toLowerCase();
        if (kws.some(function(k) { return txt === k; })) {
            b.click();
            return 'clicked-by-text: ' + (b.textContent || b.value).trim();
        }
    }
    return null;
})();
""")

    saved = _js_value(saved)
    if saved:
        print(f"  [INFO] Profile saved: {saved}")
    else:
        print(f"  [WARN] Save button not found — language may save on change")

    await asyncio.sleep(2)
    await tab.go_to(BASE_URL)
    await asyncio.sleep(3)
    await wait_for_render(tab)
    return True


_LANG_NATIVE: dict[str, str] = {
    "en": "English",
    "el": "Ελληνικά",
    "fr": "Français",
    "it": "Italiano",
    "es": "Español",
    "pt": "Português",
    "bg": "Български",
    "sq": "Shqip",
}


# ── Scrape one language ────────────────────────────────────────────────────────

async def scrape_language(tab, lang: str, glossary: dict) -> bool:
    native = _LANG_NATIVE.get(lang, lang)

    # Navigate to profile page so it's ready for the user
    await navigate_to(tab, PAGE_URLS["profile"])

    print(f"\n  ┌─ ACTION REQUIRED ─────────────────────────────────────────┐")
    print(f"  │  Switch the UI language to: {native} ({lang})")
    print(f"  │  1. The profile page is open in the browser")
    print(f"  │  2. Find the Language dropdown → select '{native}'")
    print(f"  │  3. Click Save")
    print(f"  │  4. Wait for the page to reload in {native}")
    print(f"  └────────────────────────────────────────────────────────────┘")
    await asyncio.get_event_loop().run_in_executor(
        None, input, f"\n  Press Enter when the UI is showing in {native}... "
    )

    lang_data: dict[str, list[str]] = {}

    for page_key, hash_path in PAGE_URLS.items():
        print(f"  [INFO] Scraping: {page_key}  →  {hash_path}")
        try:
            await navigate_to(tab, hash_path)
            await scroll_page(tab)
            await wait_for_content(tab, page_key)
            source = await get_dom(tab)
            labels = extract_labels(source)
            lang_data[page_key] = labels
            print(f"         ✓ {len(labels)} labels")
            if len(labels) < 5:
                print(f"  [DEBUG] Low label count — page source preview:")
                print(source[:500])
        except Exception as exc:
            print(f"         ✗ Failed ({exc})")
            lang_data[page_key] = []

    # Extract navigation items from dashboard
    try:
        await navigate_to(tab, PAGE_URLS["dashboard"])
        await scroll_page(tab)
        dash_source = await get_dom(tab)
        nav_labels = extract_nav_labels(dash_source)
        lang_data["navigation"] = nav_labels
        print(f"  [INFO] Navigation: {len(nav_labels)} labels")
    except Exception as exc:
        print(f"  [WARN] Navigation extraction failed: {exc}")
        lang_data["navigation"] = []

    glossary[lang] = lang_data
    return True


# ── Interactive elements scrape ────────────────────────────────────────────────

async def scrape_interactive_elements(tab, lang: str, glossary: dict) -> None:
    """Scrape labels from interactive UI elements (modals, panels, table headers).
    Saves results into glossary[lang]['interactive_elements'].
    """
    collected: list[str] = []
    seen: set[str] = set()

    def _add_labels(labels: list[str]) -> None:
        for lbl in labels:
            if lbl not in seen:
                seen.add(lbl)
                collected.append(lbl)

    # Interaction 1 — Products page table headers
    print(f"  [INFO] Interactive: products page")
    try:
        await navigate_to(tab, f"{BASE_URL}/#/settings/products")
        await scroll_page(tab)
        source = await get_dom(tab)
        _add_labels(extract_labels(source))
        print(f"         ✓ {len(collected)} labels so far")
    except Exception as exc:
        print(f"         ✗ Products page failed ({exc})")

    # Interaction 2 — Add Widget modal
    print(f"  [INFO] Interactive: add widget modal")
    try:
        await navigate_to(tab, f"{BASE_URL}/#/dashboard")
        await tab.execute_script("""
            var btns = document.querySelectorAll('button, [class*="button"], [class*="Button"]');
            for (var b of btns) {
                if (b.textContent.trim().toUpperCase().includes('ADD WIDGET') ||
                    b.textContent.trim().toUpperCase().includes('WIDGET')) {
                    b.click();
                    return 'clicked';
                }
            }
            return null;
        """)
        await asyncio.sleep(3)
        await wait_for_render(tab)
        source = await get_dom(tab)
        before = len(collected)
        _add_labels(extract_labels(source))
        print(f"         ✓ +{len(collected) - before} new labels")
    except Exception as exc:
        print(f"         ✗ Add widget modal failed ({exc})")

    # Interaction 3 — Shift View stats panel
    print(f"  [INFO] Interactive: shift view stats panel")
    try:
        await navigate_to(tab, f"{BASE_URL}/#/shiftview/25/14392")
        await wait_for_render(tab)
        await tab.execute_script("""
            var btns = document.querySelectorAll('[class*="info"], [class*="Info"], [class*="stats"], [class*="Stats"], [class*="panel"], [role="button"]');
            for (var b of btns) {
                var rect = b.getBoundingClientRect();
                if (rect.top < 200 && rect.right > window.innerWidth - 200) {
                    b.click();
                    return 'clicked';
                }
            }
            return null;
        """)
        await asyncio.sleep(2)
        source = await get_dom(tab)
        before = len(collected)
        _add_labels(extract_labels(source))
        print(f"         ✓ +{len(collected) - before} new labels")
    except Exception as exc:
        print(f"         ✗ Shift view stats panel failed ({exc})")

    # Interaction 4 — View Settings modal (still on shift view page)
    print(f"  [INFO] Interactive: view settings modal")
    try:
        await tab.execute_script("""
            var btns = document.querySelectorAll('[class*="settings"], [class*="Settings"], svg[class*="gear"], button');
            for (var b of btns) {
                var rect = b.getBoundingClientRect();
                if (rect.top < 200) {
                    b.click();
                    return 'clicked';
                }
            }
            return null;
        """)
        await asyncio.sleep(2)
        source = await get_dom(tab)
        before = len(collected)
        _add_labels(extract_labels(source))
        print(f"         ✓ +{len(collected) - before} new labels")
    except Exception as exc:
        print(f"         ✗ View settings modal failed ({exc})")

    glossary[lang]["interactive_elements"] = collected
    print(f"  [INFO] interactive_elements total: {len(collected)} labels")


# ── Translation map ────────────────────────────────────────────────────────────

def build_translation_map(glossary: dict) -> None:
    """
    Build translation map using ONLY GPT translation.
    Do not use index alignment at all — it is unreliable for this app.
    Navigation terms are aligned correctly so use those directly.
    Everything else goes through GPT.
    """
    from openai import OpenAI
    import os

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

    tmap: dict[str, dict[str, str]] = {}

    # Step 1: Navigation terms only — perfectly aligned across languages
    en_nav = glossary.get("en", {}).get("navigation", [])
    for i, en_term in enumerate(en_nav):
        tmap[en_term] = {"en": en_term}
        for lang in LANGUAGES:
            if lang == "en":
                continue
            lang_nav = glossary.get(lang, {}).get("navigation", [])
            if i < len(lang_nav):
                tmap[en_term][lang] = lang_nav[i]

    # Step 2: ALL other terms go through GPT — no index alignment
    # Collect all unique English terms from all pages
    all_en_terms: set[str] = set()
    for page_key, labels in glossary.get("en", {}).items():
        for label in labels:
            all_en_terms.add(label)

    # Remove nav terms already handled
    nav_terms = set(en_nav)
    terms_to_translate = all_en_terms - nav_terms

    # Filter out noise — only translate meaningful UI labels
    def is_ui_label(t: str) -> bool:
        if len(t) < 2 or len(t) > 80:
            return False
        if re.match(r'^[\d\s.,:%+\-/()\u00b0]+$', t):
            return False
        if re.match(r'^\d{1,2}[./_-]\d{1,2}', t):
            return False
        skip_patterns = [
            'Factory', 'Widget ', 'SKU', 'FBF ', 'CMF ', 'GF ', 'PF ',
            'John', '100ppm', '200ppm', 'Hour Mix', '10PPM', '20PPM',
            'Belgrade', 'Smithline', 'Randford', 'Toronto', 'Dublin',
            'London', 'Tartu', 'Aber', 'Clonden', 'Rexodol', 'Redmount',
            'Gladon', 'Philomat', 'Lemonade', 'Cola', 'Formont',
            'Χαρτι', 'Kib.', 'τμχ', 'min', 'mins', 'pcs', 'Lt',
            '750ml', '500ml', '250ml', '1L', 'sec/min',
        ]
        for p in skip_patterns:
            if p in t:
                return False
        return True

    clean_terms = [t for t in terms_to_translate if is_ui_label(t)]

    lang_names = {
        "el": "Greek", "fr": "French", "it": "Italian",
        "es": "Spanish", "pt": "Portuguese", "bg": "Bulgarian", "sq": "Albanian",
    }

    # Translate in batches of 20 terms per GPT call
    batch_size = 20
    for i in range(0, len(clean_terms), batch_size):
        batch = clean_terms[i:i + batch_size]
        print(f"  [GPT] Translating batch {i // batch_size + 1}: {batch[:3]}...")

        terms_json = json.dumps(batch, ensure_ascii=False)
        lang_list = ", ".join([f"{v} ({k})" for k, v in lang_names.items()])

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Translate these manufacturing OEE software UI labels into: {lang_list}\n\n"
                    f"Terms: {terms_json}\n\n"
                    "Rules:\n"
                    "- Keep OEE, API, SKU untranslated in all languages\n"
                    "- These are UI labels from Evocon manufacturing software\n"
                    "- Keep translations concise — they appear as button/field labels\n"
                    "- Return ONLY a JSON object where each key is an English term and value is an object with language codes as keys\n"
                    f'- Format: {{"Term": {{"el": "...", "fr": "...", "it": "...", "es": "...", "pt": "...", "bg": "...", "sq": "..."}}}}'
                ),
            }],
            temperature=0.1,
            max_tokens=2000,
        )

        try:
            raw = response.choices[0].message.content
            if not raw or not raw.strip():
                print(f"  [WARN] GPT empty response for batch {i // batch_size + 1} — check OPENAI_API_KEY")
                continue
            raw = raw.strip().replace('```json', '').replace('```', '').strip()
            result = json.loads(raw)
            for term, translations in result.items():
                if term not in tmap:
                    tmap[term] = {"en": term}
                for lang, translation in translations.items():
                    if lang in lang_names:
                        tmap[term][lang] = translation
        except Exception as e:
            raw_preview = (response.choices[0].message.content or "")[:200]
            print(f"  [WARN] Batch parse failed: {e} | raw: {raw_preview!r}")

    # Step 3: sec/min stays untranslated in all languages
    tmap["sec/min"] = {lang: "sec/min" for lang in LANGUAGES}

    MAP_PATH.write_text(
        json.dumps(tmap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[INFO] Translation map → {MAP_PATH.name}  ({len(tmap)} terms)")


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    # Clear stale progress entries for languages not in the current LANGUAGES list
    glossary = {k: v for k, v in load_progress().items() if k in LANGUAGES}

    options = ChromiumOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")

    try:
        async with Chrome(options=options) as browser:
            tab = await browser.start()

            await login(tab)

            total = len(LANGUAGES)
            for idx, lang in enumerate(LANGUAGES, start=1):
                if lang in glossary:
                    print(f"[INFO] Skipping {lang} — already scraped.")
                    continue

                print(f"\n[INFO] Scraping language: {lang} ({idx}/{total})...")
                try:
                    success = await scrape_language(tab, lang, glossary)
                    if success:
                        save_progress(glossary)
                    else:
                        print(f"[WARN] '{lang}' skipped — language switch failed, not saved.")
                except Exception as exc:
                    print(f"[ERROR] Language '{lang}' failed: {exc}")
                    save_progress(glossary)
    except PermissionError:
        pass

    print("\n[INFO] Building translation map...")
    build_translation_map(glossary)
    print("[INFO] Scraping complete.")


async def scrape_interactive_main() -> None:
    """Open browser, login, then scrape interactive elements for each language.
    Loads existing glossary and adds/updates only the 'interactive_elements' key.
    Rebuilds translation map afterwards.
    """
    glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))

    options = ChromiumOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")

    try:
        async with Chrome(options=options) as browser:
            tab = await browser.start()
            await login(tab)

            for lang in LANGUAGES:
                if lang not in glossary:
                    print(f"[WARN] '{lang}' not in glossary — skipping interactive scrape.")
                    continue

                native = _LANG_NATIVE.get(lang, lang)
                await navigate_to(tab, PAGE_URLS["profile"])

                print(f"\n  ┌─ ACTION REQUIRED ─────────────────────────────────────────┐")
                print(f"  │  Switch the UI language to: {native} ({lang})")
                print(f"  │  1. Find the Language dropdown → select '{native}'")
                print(f"  │  2. Click Save and wait for the page to reload")
                print(f"  └────────────────────────────────────────────────────────────┘")
                await asyncio.get_event_loop().run_in_executor(
                    None, input, f"\n  Press Enter when the UI is showing in {native}... "
                )

                print(f"\n[INFO] Scraping interactive elements for: {lang}")
                try:
                    await scrape_interactive_elements(tab, lang, glossary)
                    save_progress(glossary)
                except Exception as exc:
                    print(f"[ERROR] Interactive scrape failed for '{lang}': {exc}")
                    save_progress(glossary)
    except PermissionError:
        pass

    print("\n[INFO] Rebuilding translation map...")
    build_translation_map(glossary)
    print("[INFO] Interactive scrape complete.")


if __name__ == "__main__":
    if "--rebuild-map" in sys.argv:
        glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
        build_translation_map(glossary)
    elif "--scrape-interactive" in sys.argv:
        asyncio.run(scrape_interactive_main())
    else:
        asyncio.run(main())
