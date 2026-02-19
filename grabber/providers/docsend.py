"""DocSend provider – downloads documents from docsend.com viewer URLs.

DocSend blocks headless browsers from accessing document content (the viewer
iframe and page_data API both return 403).  There are two working approaches:

1. **CDP mode** (``--cdp``): Connect Playwright to an already-running Chrome
   instance launched with ``--remote-debugging-port=9222``.  The real browser
   session has the cookies/headers that DocSend trusts.

2. **URL-file mode** (``--url-file``): Provide a JSON file containing an array
   of signed image URLs (one per page).  These can be extracted in-browser with
   the console script (``grabber/scripts/docsend_console.js``) or by an agent
   using browser automation (e.g. Claude in Chrome) to call the page_data API
   and save the URLs.

In either case, the signed CloudFront image URLs are downloadable with plain
HTTP requests — CORS only blocks in-browser ``fetch()`` from a different origin.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import img2pdf
import requests
from playwright.sync_api import sync_playwright

from grabber.providers.base import BaseProvider

_DOCSEND_PATTERN = re.compile(r"https?://(www\.)?docsend\.com/")


class DocsendProvider(BaseProvider):
    @staticmethod
    def can_handle(url: str) -> bool:
        return bool(_DOCSEND_PATTERN.search(url))

    def fetch(
        self,
        url: str,
        output: Path,
        *,
        email: str | None = None,
        headless: bool = True,
        cdp_url: str | None = None,
        url_file: str | None = None,
    ) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        if url_file:
            image_urls = self._load_url_file(url_file)
        elif cdp_url:
            image_urls = self._extract_urls_cdp(
                url, cdp_url=cdp_url, email=email
            )
        else:
            image_urls = self._extract_urls_cdp(
                url, headless=headless, email=email
            )

        if not image_urls:
            raise RuntimeError("No page image URLs were extracted.")

        # Download images — signed CloudFront URLs work with plain HTTP
        print(f"[grabber] Downloading {len(image_urls)} pages …")
        image_data: list[bytes] = []
        for i, img_url in enumerate(image_urls, 1):
            resp = requests.get(img_url, timeout=30)
            if resp.status_code == 200:
                image_data.append(resp.content)
            else:
                print(f"\n[grabber] Warning: page {i} HTTP {resp.status_code}")
            print(f"[grabber] Downloaded page {i}/{len(image_urls)}", end="\r")
            time.sleep(0.1)

        print()

        if not image_data:
            raise RuntimeError("No page images were downloaded.")

        print(f"[grabber] Compiling {len(image_data)} pages into PDF …")
        with open(output, "wb") as f:
            f.write(img2pdf.convert(image_data))

        print(f"[grabber] Saved to {output}")
        return output

    # ------------------------------------------------------------------
    # URL extraction strategies
    # ------------------------------------------------------------------

    @staticmethod
    def _load_url_file(path: str) -> list[str]:
        """Load image URLs from a JSON file."""
        with open(path) as f:
            urls = json.load(f)
        print(f"[grabber] Loaded {len(urls)} URLs from {path}")
        return urls

    @classmethod
    def _extract_urls_cdp(
        cls,
        url: str,
        *,
        cdp_url: str | None = None,
        headless: bool = True,
        email: str | None = None,
    ) -> list[str]:
        """Open the DocSend page and extract image URLs via the page_data API."""
        print(f"[grabber] DocSend: opening {url}")

        with sync_playwright() as pw:
            if cdp_url:
                print(f"[grabber] Connecting to Chrome via CDP: {cdp_url}")
                browser = pw.chromium.connect_over_cdp(cdp_url)
                context = (
                    browser.contexts[0]
                    if browser.contexts
                    else browser.new_context()
                )
                page = context.new_page()
                owns_browser = False
            else:
                browser = pw.chromium.launch(headless=headless)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page = context.new_page()
                owns_browser = True

            page.set_default_timeout(60000)
            page.goto(url, wait_until="domcontentloaded")

            cls._handle_email_gate(page, email)

            page.wait_for_selector(
                ".toolbar-page-indicator", timeout=30000
            )

            total_pages = page.evaluate(
                """() => {
                const ind = document.querySelector('.toolbar-page-indicator');
                if (!ind) return 0;
                const parts = ind.innerText.split('/');
                return parts.length > 1 ? parseInt(parts[1].trim()) : 0;
            }"""
            )

            if not total_pages:
                if owns_browser:
                    browser.close()
                raise RuntimeError("Could not detect page count.")

            print(f"[grabber] Detected {total_pages} pages")

            # Extract image URLs via page_data API
            image_urls = page.evaluate(
                """async (totalPages) => {
                const base = window.location.href.split('?')[0].replace(/\\/$/, '') + '/page_data/';
                const urls = [];
                for (let i = 1; i <= totalPages; i++) {
                    try {
                        const res = await fetch(base + i);
                        if (!res.ok) continue;
                        const data = await res.json();
                        if (data.imageUrl) urls.push(data.imageUrl);
                    } catch {}
                }
                return urls;
            }""",
                total_pages,
            )

            if cdp_url:
                page.close()
            else:
                browser.close()

        if not image_urls:
            raise RuntimeError(
                "DocSend page_data API returned no image URLs.\n\n"
                "This typically happens with headless browsers. Try one of:\n"
                "  1. Launch Chrome with: google-chrome --remote-debugging-port=9222\n"
                "     Then: grabber URL --cdp ws://127.0.0.1:9222\n\n"
                "  2. Extract URLs in-browser with the console script, save as JSON,\n"
                "     Then: grabber URL --url-file urls.json\n"
            )

        print(f"[grabber] Extracted {len(image_urls)} image URLs")
        return image_urls

    @staticmethod
    def _handle_email_gate(page, email: str | None) -> None:
        """If DocSend shows an email gate, fill it in and proceed."""
        try:
            locator = page.locator('input[name="visitor[email]"]')
            if locator.is_visible(timeout=3000):
                gate_email = email or "viewer@example.com"
                print(f"[grabber] Email gate detected – entering {gate_email}")
                locator.fill(gate_email)
                page.click('button[type="submit"]')
                page.wait_for_selector(
                    ".toolbar-page-indicator", timeout=15000
                )
        except Exception:
            pass
