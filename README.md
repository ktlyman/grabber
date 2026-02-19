# grabber

Download documents from viewer-only systems (DocSend, etc.) that prevent direct download, and save them as local PDFs.

## Supported providers

| Provider | URL pattern | Status |
|----------|-------------|--------|
| DocSend  | `docsend.com/view/...` | Working |

## Setup

```bash
pip install -e .
playwright install chromium
```

## Usage

### CLI

```bash
# Basic usage — auto-detects provider from URL
grabber "https://docsend.com/view/ABCDEF"

# Custom output path
grabber "https://docsend.com/view/ABCDEF" -o deck.pdf

# Bypass email gate
grabber "https://docsend.com/view/ABCDEF" --email you@example.com

# Visible browser for debugging
grabber "https://docsend.com/view/ABCDEF" --no-headless

# More download threads (default 8) — useful for large documents
grabber "https://docsend.com/view/ABCDEF" --url-file urls.json --workers 16
```

### Python (for agent integration)

```python
from pathlib import Path
from grabber.providers import detect_provider

url = "https://docsend.com/view/ABCDEF"
provider_cls = detect_provider(url)
provider = provider_cls()
pdf_path = provider.fetch(url, Path("output.pdf"), email="agent@co.com")
```

Or use the provider directly:

```python
from pathlib import Path
from grabber.providers.docsend import DocsendProvider

provider = DocsendProvider()
provider.fetch("https://docsend.com/view/ABCDEF", Path("deck.pdf"))
```

### Agent / MCP workflow (recommended for browser agents)

For agents with browser control (e.g. Claude in Chrome MCP), the console script
is the most reliable approach. It works entirely within the browser, avoiding
CORS restrictions and signed-URL expiry issues.

1. Navigate to the DocSend URL in the browser
2. Handle the email gate if present (fill & submit)
3. Execute `grabber/scripts/docsend_console.js` via JS execution
4. The script fetches all page images and renders them in a print-friendly layout
5. Trigger `window.print()` — user saves as PDF from the print dialog

The script also auto-downloads a `grabber_urls.json` file that can be fed back
to the CLI for server-side PDF compilation:

```bash
grabber "https://docsend.com/view/ABCDEF" --url-file grabber_urls.json -o doc.pdf
```

**Why not just download the images directly?** Signed CloudFront image URLs
expire after ~3.5 minutes and CORS blocks in-browser `fetch()`. The `<img src>`
approach in the console script bypasses CORS because the browser handles image
loading natively.

### Browser console (manual fallback)

If you're viewing a DocSend document in a browser and just want to grab it manually:

1. Open DevTools console (F12)
2. Type `allow pasting` and press Enter
3. Paste the contents of `grabber/scripts/docsend_console.js`
4. Press Enter — wait for extraction to finish
5. Cmd+P / Ctrl+P to print as PDF

## Adding a new provider

1. Create `grabber/providers/yourservice.py`
2. Subclass `BaseProvider` from `grabber.providers.base`
3. Implement `can_handle(url)` and `fetch(...)`
4. Register it in `grabber/providers/__init__.py`
