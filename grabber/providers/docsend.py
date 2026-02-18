"""DocSend provider – downloads documents from docsend.com viewer URLs."""

from __future__ import annotations

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
    ) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        print(f"[grabber] DocSend: opening {url}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle")

            # --- Handle email gate ---
            self._handle_email_gate(page, email)

            # --- Detect page count and base API URL ---
            meta = page.evaluate(
                """() => {
                const indicator = document.querySelector('.toolbar-page-indicator');
                let total = 0;
                if (indicator) {
                    const parts = indicator.innerText.split('/');
                    if (parts.length > 1) total = parseInt(parts[1].trim());
                }
                const baseUrl = window.location.href
                    .split('?')[0]
                    .replace(/\\/$/, '')
                    + '/page_data/';
                return { totalPages: total, baseUrl: baseUrl };
            }"""
            )

            total_pages: int = meta["totalPages"]
            base_url: str = meta["baseUrl"]

            if not total_pages:
                browser.close()
                raise RuntimeError(
                    "Could not detect page count. "
                    "The page may not have loaded correctly or the DOM structure changed."
                )

            print(f"[grabber] Detected {total_pages} pages")

            # --- Download page images ---
            image_data: list[bytes] = []

            for i in range(1, total_pages + 1):
                image_url: str | None = page.evaluate(
                    """async (pageNum) => {
                    const res = await fetch('%s' + pageNum);
                    if (!res.ok) return null;
                    const data = await res.json();
                    return data.imageUrl || null;
                }"""
                    % base_url,
                    i,
                )

                if image_url:
                    resp = requests.get(image_url, timeout=30)
                    if resp.status_code == 200:
                        image_data.append(resp.content)

                print(
                    f"[grabber] Downloaded page {i}/{total_pages}",
                    end="\r",
                )
                time.sleep(0.1)

            browser.close()

        print()  # newline after \r progress

        if not image_data:
            raise RuntimeError("No page images were downloaded.")

        # --- Compile images into PDF ---
        print(f"[grabber] Compiling {len(image_data)} pages into PDF …")
        with open(output, "wb") as f:
            f.write(img2pdf.convert(image_data))

        print(f"[grabber] Saved to {output}")
        return output

    # ------------------------------------------------------------------
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
            # No gate or already past it – continue
            pass
