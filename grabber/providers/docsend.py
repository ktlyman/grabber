"""DocSend provider – downloads documents from docsend.com viewer URLs.

By default, ``grabber URL`` automatically launches the system Chrome browser
with a copy of the user's profile (to reuse existing DocSend session cookies),
extracts page images via the ``page_data`` API, downloads them concurrently,
and compiles a PDF with ``img2pdf``.  The browser opens briefly and closes
automatically — zero user interaction required.

DocSend's ``page_data`` API requires session cookies that are only present in
an established browser profile.  Fresh browser profiles (including Playwright's
bundled Chromium) always receive 403.  The workaround is to clone the user's
real Chrome profile into a temporary directory and launch Chrome from there.

Escape hatches for special situations:

- ``--cdp ws://...`` — connect to an already-running Chrome instance
- ``--url-file urls.json`` — skip the browser entirely; provide pre-extracted URLs

**Important:** Signed CloudFront image URLs expire after ~3.5 minutes.
Downloads use concurrent workers (configurable via ``--workers``) to finish
before expiry.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import img2pdf
import requests
from playwright.sync_api import Page, sync_playwright

from grabber.providers.base import BaseProvider

_DOCSEND_PATTERN = re.compile(r"https?://(www\.)?docsend\.com/")


def _find_chrome() -> str | None:
    """Locate the system Chrome binary."""
    system = platform.system()
    if system == "Darwin":
        path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(path):
            return path
    elif system == "Linux":
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
    elif system == "Windows":
        for base in (
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            path = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.exists(path):
                return path
    return None


def _chrome_profile_dir() -> Path | None:
    """Return the path to the user's default Chrome profile directory."""
    system = platform.system()
    if system == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif system == "Linux":
        p = Path.home() / ".config" / "google-chrome"
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        p = Path(local) / "Google" / "Chrome" / "User Data" if local else None
    else:
        return None
    return p if p and p.exists() else None


class DocsendProvider(BaseProvider):
    """Download documents from DocSend viewer URLs."""

    @staticmethod
    def can_handle(url: str) -> bool:
        return bool(_DOCSEND_PATTERN.search(url))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        output: Path,
        *,
        email: str | None = None,
        headless: bool = True,
        cdp_url: str | None = None,
        url_file: str | None = None,
        workers: int = 8,
    ) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        # Escape hatches — bypass the automatic strategy chain.
        if url_file:
            image_urls = self._load_url_file(url_file)
            image_data = self._download_images(image_urls, workers=workers)
            return self._compile_pdf(image_data, output)

        if cdp_url:
            image_urls = self._extract_urls_with_cdp(
                url, cdp_url=cdp_url, email=email,
            )
            image_data = self._download_images(image_urls, workers=workers)
            return self._compile_pdf(image_data, output)

        # Default: launch Chrome with user profile.
        return self._auto_fetch(url, output, email=email, workers=workers)

    # ------------------------------------------------------------------
    # Strategy chain
    # ------------------------------------------------------------------

    def _auto_fetch(
        self,
        url: str,
        output: Path,
        *,
        email: str | None = None,
        workers: int = 8,
    ) -> Path:
        """Launch system Chrome with user profile, extract, download, compile."""
        chrome = _find_chrome()
        profile_dir = _chrome_profile_dir()

        if not chrome:
            raise RuntimeError(
                "Could not find Google Chrome on this system.\n"
                "Install Chrome, or use --cdp / --url-file instead."
            )

        if not profile_dir:
            raise RuntimeError(
                "Could not find a Chrome profile directory.\n"
                "Open Chrome and visit any DocSend link once, then retry.\n"
                "Or use --cdp / --url-file instead."
            )

        tmp_dir = tempfile.mkdtemp(prefix="grabber_chrome_")
        proc: subprocess.Popen | None = None

        try:
            # Copy the user's Default profile — skip large caches.
            print("[grabber] Cloning Chrome profile …")
            src_default = profile_dir / "Default"
            dst_default = Path(tmp_dir) / "Default"
            if src_default.exists():
                shutil.copytree(
                    src_default,
                    dst_default,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache",
                        "Service Worker", "Storage", "blob_storage",
                        "File System", "IndexedDB", "Sessions",
                    ),
                )
            else:
                dst_default.mkdir(parents=True)

            # Also copy top-level state files Chrome expects.
            for name in ("Local State",):
                src = profile_dir / name
                if src.exists():
                    shutil.copy2(src, Path(tmp_dir) / name)

            # Launch Chrome with remote debugging.
            port = 9222
            print(f"[grabber] Launching Chrome (port {port}) …")
            proc = subprocess.Popen(
                [
                    chrome,
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={tmp_dir}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(3)

            # Connect via CDP and extract.
            image_urls = self._extract_via_cdp_port(
                url, port=port, email=email,
            )

            if not image_urls:
                raise RuntimeError(
                    "DocSend page_data API returned no image URLs.\n\n"
                    "Ensure you have visited docsend.com at least once in "
                    "Chrome so session cookies exist.\n"
                    "Or use --url-file with pre-extracted URLs."
                )

            # Download and compile.
            image_data = self._download_images(
                image_urls, workers=workers,
            )
            return self._compile_pdf(image_data, output)

        finally:
            if proc:
                proc.terminate()
                proc.wait(timeout=10)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # CDP extraction helpers
    # ------------------------------------------------------------------

    def _extract_via_cdp_port(
        self,
        url: str,
        *,
        port: int = 9222,
        email: str | None = None,
    ) -> list[str]:
        """Connect to a Chrome instance on *port* and extract image URLs."""
        cdp_url = f"http://127.0.0.1:{port}"
        print(f"[grabber] Connecting via CDP: {cdp_url}")

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )
            page = context.new_page()
            page.set_default_timeout(60_000)

            self._navigate_and_gate(page, url, email=email)
            total_pages = self._get_total_pages(page)

            if not total_pages:
                page.close()
                return []

            image_urls = self._extract_image_urls(page, total_pages)
            page.close()

        return image_urls

    def _extract_urls_with_cdp(
        self,
        url: str,
        *,
        cdp_url: str,
        email: str | None = None,
    ) -> list[str]:
        """Connect via an explicit CDP URL and extract image URLs."""
        print(f"[grabber] Connecting to Chrome via CDP: {cdp_url}")

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )
            page = context.new_page()
            page.set_default_timeout(60_000)

            self._navigate_and_gate(page, url, email=email)
            total_pages = self._get_total_pages(page)

            if not total_pages:
                page.close()
                raise RuntimeError("Could not detect page count.")

            image_urls = self._extract_image_urls(page, total_pages)
            page.close()

        if not image_urls:
            raise RuntimeError(
                "DocSend page_data API returned no image URLs via CDP.\n\n"
                "Try extracting URLs in-browser with the console script,\n"
                "save as JSON, then: grabber URL --url-file urls.json\n"
            )
        return image_urls

    # ------------------------------------------------------------------
    # Shared browser helpers
    # ------------------------------------------------------------------

    @classmethod
    def _navigate_and_gate(
        cls, page: Page, url: str, *, email: str | None = None,
    ) -> None:
        """Navigate to a DocSend URL and handle the email gate."""
        print(f"[grabber] Opening {url}")
        page.goto(url, wait_until="domcontentloaded")
        cls._handle_email_gate(page, email)

    @staticmethod
    def _get_total_pages(page: Page) -> int:
        """Detect total page count from the DocSend toolbar indicator."""
        try:
            page.wait_for_selector(
                ".toolbar-page-indicator", timeout=30_000,
            )
        except Exception:
            return 0

        total: int = page.evaluate(
            """() => {
            const ind = document.querySelector('.toolbar-page-indicator');
            if (!ind) return 0;
            const parts = ind.innerText.split('/');
            return parts.length > 1 ? parseInt(parts[1].trim()) : 0;
        }"""
        )
        if total:
            print(f"[grabber] Detected {total} pages")
        return total or 0

    @staticmethod
    def _extract_image_urls(page: Page, total_pages: int) -> list[str]:
        """Extract signed image URLs from the page_data API."""
        print(f"[grabber] Extracting {total_pages} pages via page_data API …")
        image_urls: list[str] = page.evaluate(
            """async (totalPages) => {
            const base = window.location.href.split('?')[0]
                         .replace(/\\/$/, '') + '/page_data/';
            const urls = [];
            for (let i = 1; i <= totalPages; i++) {
                try {
                    const res = await fetch(base + i, {credentials: 'same-origin'});
                    if (!res.ok) continue;
                    const data = await res.json();
                    if (data.imageUrl) urls.push(data.imageUrl);
                } catch {}
            }
            return urls;
        }""",
            total_pages,
        )
        if image_urls:
            print(f"[grabber] Extracted {len(image_urls)} image URLs")
        return image_urls or []

    @staticmethod
    def _handle_email_gate(page: Page, email: str | None) -> None:
        """If DocSend shows an email gate, fill it in and proceed."""
        try:
            locator = page.locator('input[name="visitor[email]"]')
            if locator.is_visible(timeout=3000):
                gate_email = email or "viewer@example.com"
                print("[grabber] Email gate detected – submitting")
                locator.fill(gate_email)
                page.click('button[type="submit"]')
                page.wait_for_selector(
                    ".toolbar-page-indicator", timeout=15_000,
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Image download (concurrent)
    # ------------------------------------------------------------------

    @staticmethod
    def _download_images(
        image_urls: list[str], *, workers: int = 8,
    ) -> list[bytes]:
        """Download page images concurrently.

        Signed CloudFront URLs expire after ~3.5 min so we use a thread pool
        to parallelise downloads and finish before they go stale.
        """
        total = len(image_urls)
        print(f"[grabber] Downloading {total} pages ({workers} workers) …")

        results: dict[int, bytes] = {}
        failed: list[int] = []

        def _fetch_one(idx: int, img_url: str) -> tuple[int, bytes | None]:
            try:
                resp = requests.get(img_url, timeout=30)
                if resp.status_code == 200:
                    return idx, resp.content
                return idx, None
            except requests.RequestException:
                return idx, None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_fetch_one, i, u): i
                for i, u in enumerate(image_urls)
            }
            done_count = 0
            for future in as_completed(futures):
                idx, data = future.result()
                done_count += 1
                if data is not None:
                    results[idx] = data
                else:
                    failed.append(idx + 1)  # 1-indexed for display
                print(
                    f"[grabber] Downloaded {done_count}/{total}"
                    + (f" ({len(failed)} failed)" if failed else ""),
                    end="\r",
                )

        print()
        if failed:
            print(f"[grabber] Warning: failed pages: {failed}")

        # Return in original page order.
        return [results[i] for i in sorted(results)]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_url_file(path: str) -> list[str]:
        """Load image URLs from a JSON file."""
        with open(path) as f:
            urls = json.load(f)
        print(f"[grabber] Loaded {len(urls)} URLs from {path}")
        return urls

    @staticmethod
    def _compile_pdf(image_data: list[bytes], output: Path) -> Path:
        """Compile downloaded image bytes into a PDF with img2pdf."""
        if not image_data:
            raise RuntimeError("No page images were downloaded.")
        print(f"[grabber] Compiling {len(image_data)} pages into PDF …")
        with open(output, "wb") as f:
            f.write(img2pdf.convert(image_data))
        print(f"[grabber] Saved to {output}")
        return output
