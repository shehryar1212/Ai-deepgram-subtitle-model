# Scraper Agent Instructions

General instructions for building web scrapers in this project. Apply these to every scraper, regardless of target site.

---

## 1. Before Writing Any Code — Manual Recon First

**Always ask the user to open the browser and tell you:**
- The exact CSS selector or attribute of the element you need to interact with (button, dropdown, input)
- What happens in the network tab when the action fires (XHR/fetch calls, URL changes)
- What the DOM looks like after the action (is it a new page? a modal? same page with updated content?)

**Why this matters:** Guessing at selectors costs hours. The user spending 2 minutes in DevTools saves the whole session.

**What to ask specifically:**
```
1. Right-click the element → Inspect
2. Tell me the tag, class names, id, or any data-* attributes
3. Do you see a network request fire when you click it?
4. Does the URL change? Does the page reload?
```

Do not attempt to automate an interaction until the user has confirmed the selector works manually.

---

## 2. Identify the Rendering Architecture First

Before scraping anything, determine:

| Question | How to check | Why it matters |
|---|---|---|
| Is it a SPA (React/Vue/Angular)? | Does the URL use `#/` or does content load without full page reload? | Raw `requests` won't work — need a real browser |
| Does it use lazy rendering? | Check for `v-lazy`, `IntersectionObserver`, virtualized lists | Elements only exist in DOM when scrolled into view |
| Is content behind auth? | Try opening the URL in incognito | Need login flow before scraping |
| Does it use SSR? | Does `curl` return full content? | May not need a browser at all |

**If it's a SPA with lazy-rendered components (Vuetify v-lazy, React Virtuoso, etc.):**
- Elements that are off-screen do not exist in the DOM
- `scrollIntoView()` does NOT trigger IntersectionObserver in a background/headless Chrome window
- You must either: (a) make the window visible and focused, or (b) use a human-in-the-loop step for that interaction

---

## 3. Browser Automation — pydoll (CDP)

This project uses **pydoll** for browser control via Chrome DevTools Protocol.

### Key behaviors

**`tab.execute_script()` return format:**
```python
# Raw return from pydoll is nested:
result = await tab.execute_script("return document.title;")
# result == {'result': {'result': {'type': 'string', 'value': 'Page Title'}}}

# Always unwrap with:
def _js_value(result):
    if isinstance(result, str):
        return result
    try:
        return result["result"]["result"]["value"]
    except (KeyError, TypeError):
        return None
```

**Always get live DOM via JS, not page source:**
```python
async def get_dom(tab) -> str:
    result = await tab.execute_script("return document.documentElement.outerHTML;")
    return _js_value(result) or ""
```

**Wait for SPA render before scraping:**
```python
# SPAs render async — raw HTML after navigation is empty
# Wait for body text to exceed a threshold, then wait for JS frameworks to settle
await tab.execute_script("return document.readyState;")  # 'complete' is not enough for SPAs
# Better: poll document.body.innerText.length > N
```

**Navigation in SPAs:**
- `tab.go_to(url)` may return before the JS framework has rendered
- Always add a render-wait after navigation
- Hash-based routing (`/#/page`) does not trigger a full page load — use `window.location.hash` to verify arrival

---

## 4. Human-in-the-Loop for Complex UI Interactions

When an interaction involves:
- Dropdowns with lazy-rendered options (Vuetify VSelect, MUI Autocomplete)
- File upload dialogs
- OAuth popups
- CAPTCHA or 2FA
- Any action that requires the window to be focused/visible

**Use a manual prompt instead of automation:**

```python
print(f"\n  ┌─ ACTION REQUIRED ──────────────────────────────────────┐")
print(f"  │  Please do X in the browser window                      │")
print(f"  │  1. Step one                                             │")
print(f"  │  2. Step two                                             │")
print(f"  └──────────────────────────────────────────────────────────┘")
await asyncio.get_event_loop().run_in_executor(
    None, input, "\n  Press Enter when done... "
)
```

This is not a failure — it is the correct design for actions the browser will not reliably perform in a background window. A 10-second human action beats 3 hours of debugging automation.

---

## 5. Progress Files — Always Implement First

Before scraping anything in a loop, implement a progress/resume system:

```python
PROGRESS_PATH = Path(__file__).parent / ".scraper_progress.json"

def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {}

def save_progress(data: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# In the loop:
progress = load_progress()
for item in items:
    if item["id"] in progress:
        continue  # already done
    result = scrape(item)
    progress[item["id"]] = result
    save_progress(progress)  # save after EACH item, not at the end
```

**Why save after each item:** If the scraper crashes at item 47/50, you don't re-scrape 46 items.

Add `.scraper_progress.json` to `.gitignore`.

---

## 6. Text Extraction from SPAs

Use BeautifulSoup on the live DOM (not raw source):

```python
from bs4 import BeautifulSoup

def extract_labels(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    # Remove non-content tags first
    for tag in soup(["script", "style", "head", "meta", "link", "noscript", "svg", "path"]):
        tag.decompose()
    seen: set[str] = set()
    labels: list[str] = []
    for node in soup.find_all(string=True):
        t = node.strip()
        if t and t not in seen:
            seen.add(t)
            labels.append(t)
    return labels
```

**Filter noise aggressively.** Common junk to exclude:
- Pure numbers / percentages / timestamps
- Emails and URLs
- Strings under 2 characters
- Known framework placeholder strings
- Strings over 60–80 characters (usually dynamic content, not UI labels)

---

## 7. Multi-Language / Multi-State Scraping

When scraping the same page in multiple states (languages, filters, date ranges):

**Identify which pages have stable label counts first.**
- Settings/config pages: most stable — same fields in every language
- Navigation/sidebar: stable
- Dashboard with live data: less stable — numbers change, some labels may appear/disappear
- Reports/data tables: least stable — content is dynamic

**Use stable pages for index-aligned translation mapping. Never use dynamic pages.**

**For translation maps specifically:**
1. Build from most stable page first (settings → navigation)
2. Use LLM translation (GPT) only as a fallback for terms not found in scraped data
3. Keep industry-standard acronyms (OEE, API, SKU) untranslated in all languages
4. After building, manually verify 5–10 spot-check terms against the live UI

---

## 8. CLI Flags for Reprocessing Without Re-Scraping

Always separate the scraping step from the post-processing step:

```python
if __name__ == "__main__":
    if "--rebuild-map" in sys.argv:
        # Re-run post-processing on existing raw data
        data = json.loads(RAW_OUTPUT.read_text(encoding="utf-8"))
        build_output(data)
    else:
        asyncio.run(main())
```

Scraping is slow and hits a live server. Post-processing (building maps, reformatting, filtering) should always be re-runnable in isolation without re-scraping.

---

## 9. Debugging Checklist

When a scraper is not finding/clicking the expected element:

1. **Ask the user for the selector** — do not guess more than once
2. **Dump the DOM to a file** and inspect it: `Path("debug_dom.html").write_text(html)`
3. **Check if the element is inside a shadow DOM** — CDP cannot reach inside shadow roots with standard selectors
4. **Check if it's lazy-rendered** — search the DOM dump for the element; if absent, it hasn't rendered yet
5. **Check the network tab** — the data you want might come from an API endpoint directly, bypassing the need to scrape HTML entirely
6. **Check if auth expired** — re-login and retry before assuming the scraper is broken

---

## 10. Dependencies

```
pydoll-python   # CDP browser automation
beautifulsoup4  # HTML parsing
openai          # LLM fallback for translation/classification (optional)
python-dotenv   # Load .env for API keys
```

API keys go in `.env`, loaded via `python-dotenv`. Never hardcode credentials.
