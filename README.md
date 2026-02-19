# grabber

Download documents from viewer-only systems (DocSend, etc.) that prevent direct download, and save them as local PDFs.

## Supported providers

| Provider | URL pattern | Status |
|----------|-------------|--------|
| DocSend  | `docsend.com/view/...` | Working |

## Prerequisites

- Python 3.10+
- Google Chrome installed (the tool uses your existing Chrome profile)
- Playwright: `pip install -e . && playwright install chromium`

## Setup

```bash
pip install -e .
playwright install chromium
```

## Usage

### CLI

```bash
# Just works — launches Chrome, extracts images, compiles PDF
grabber "https://docsend.com/view/ABCDEF" -o deck.pdf

# Bypass email gate
grabber "https://docsend.com/view/ABCDEF" --email you@example.com -o deck.pdf
```

That's it. The tool automatically:
1. Clones your Chrome profile to a temp directory (for session cookies)
2. Launches Chrome with remote debugging
3. Navigates to the DocSend URL and handles the email gate
4. Extracts all page image URLs via the page_data API
5. Downloads images concurrently (8 threads by default)
6. Compiles them into a PDF with img2pdf
7. Closes Chrome and cleans up

**Prerequisite:** You must have visited `docsend.com` at least once in Chrome
so that session cookies exist. If you've ever viewed a DocSend document in
Chrome, you're good.

### Python (for agent integration)

```python
from pathlib import Path
from grabber.providers.docsend import DocsendProvider

provider = DocsendProvider()
provider.fetch("https://docsend.com/view/ABCDEF", Path("deck.pdf"),
               email="agent@co.com")
```

Or use auto-detection:

```python
from pathlib import Path
from grabber.providers import detect_provider

url = "https://docsend.com/view/ABCDEF"
provider_cls = detect_provider(url)
provider = provider_cls()
pdf_path = provider.fetch(url, Path("output.pdf"), email="agent@co.com")
```

### Advanced / escape hatches

```bash
# Connect to an existing Chrome instance via CDP
# (launch Chrome with: google-chrome --remote-debugging-port=9222)
grabber URL --cdp ws://127.0.0.1:9222 -o deck.pdf

# Use pre-extracted image URLs (from console script or browser automation)
grabber URL --url-file grabber_urls.json -o deck.pdf

# More download threads (default 8) — useful for large documents
grabber URL --url-file urls.json --workers 16
```

### Browser console (manual fallback)

If you're viewing a DocSend document in a browser and just want to grab it manually:

1. Open DevTools console (F12)
2. Type `allow pasting` and press Enter
3. Paste the contents of `grabber/scripts/docsend_console.js`
4. Press Enter — wait for extraction to finish
5. Cmd+P / Ctrl+P to print as PDF

The script also auto-downloads a `grabber_urls.json` file that can be fed back
to the CLI: `grabber URL --url-file grabber_urls.json -o doc.pdf`

## Adding a new provider

1. Create `grabber/providers/yourservice.py`
2. Subclass `BaseProvider` from `grabber.providers.base`
3. Implement `can_handle(url)` and `fetch(...)`
4. Register it in `grabber/providers/__init__.py`
