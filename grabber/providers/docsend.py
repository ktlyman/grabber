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

import base64
import json
import logging
import os
import platform
import re
import signal
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

# Suppress img2pdf alpha-channel warnings (very noisy for large documents).
logging.getLogger("img2pdf").setLevel(logging.ERROR)

_DOCSEND_PATTERN = re.compile(r"https?://(www\.)?docsend\.com/")
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _elapsed(start: float) -> str:
    """Format elapsed time since *start* as a human-readable string."""
    secs = time.time() - start
    if secs < 60:
        return f"{secs:.1f}s"
    mins = int(secs // 60)
    remainder = secs % 60
    return f"{mins}m {remainder:.1f}s"


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
        output: Path | None,
        *,
        email: str | None = None,
        cdp_url: str | None = None,
        url_file: str | None = None,
        workers: int = 16,
    ) -> Path:
        if output is not None:
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)

        # Escape hatches — bypass the automatic strategy chain.
        if url_file:
            out = output or Path("output.pdf")
            image_urls = self._load_url_file(url_file)
            results, _ = self._download_images(image_urls, workers=workers)
            image_data = [results[i] for i in sorted(results)]
            return self._compile_pdf(image_data, out)

        if cdp_url:
            out = output or Path("output.pdf")
            image_urls = self._extract_urls_with_cdp(
                url, cdp_url=cdp_url, email=email,
            )
            results, _ = self._download_images(image_urls, workers=workers)
            image_data = [results[i] for i in sorted(results)]
            return self._compile_pdf(image_data, out)

        # Dataroom / folder URLs contain no ``/d/`` segment.
        if self._is_dataroom_url(url):
            return self._fetch_dataroom(
                url, output, email=email, workers=workers,
            )

        # Default: single-document download.
        return self._auto_fetch(url, output, email=email, workers=workers)

    # ------------------------------------------------------------------
    # Chrome lifecycle helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clone_profile(profile_dir: Path, tmp_dir: str) -> None:
        """Clone the user's Chrome profile into *tmp_dir*."""
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

        for name in ("Local State",):
            src = profile_dir / name
            if src.exists():
                shutil.copy2(src, Path(tmp_dir) / name)

    @staticmethod
    def _launch_chrome(
        chrome: str, tmp_dir: str, port: int,
    ) -> subprocess.Popen | None:
        """Launch Chrome with remote debugging.

        On macOS uses ``open -gjn`` to keep the window hidden.  Returns
        the ``Popen`` handle on Linux/Windows, or ``None`` on macOS
        (where Chrome is detached from our process tree).
        """
        if platform.system() == "Darwin":
            subprocess.Popen(
                [
                    "open", "-gjn",
                    "-a", "Google Chrome",
                    "--args",
                    f"--remote-debugging-port={port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--user-data-dir={tmp_dir}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return None

        return subprocess.Popen(
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

    @staticmethod
    def _kill_chrome(
        proc: subprocess.Popen | None, port: int,
    ) -> None:
        """Terminate a Chrome process launched by ``_launch_chrome``."""
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
        else:
            # macOS: ``open`` detached Chrome; find it by port.
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                for line in out.strip().splitlines():
                    os.kill(int(line.strip()), signal.SIGTERM)
            except (subprocess.CalledProcessError, ValueError, OSError):
                pass

    # ------------------------------------------------------------------
    # Strategy chain
    # ------------------------------------------------------------------

    def _auto_fetch(
        self,
        url: str,
        output: Path | None,
        *,
        email: str | None = None,
        workers: int = 16,
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

        t_total = time.time()
        tmp_dir = tempfile.mkdtemp(prefix="grabber_chrome_")
        proc: subprocess.Popen | None = None
        port = 9222

        try:
            # --- Step 1: Clone profile ---
            t_step = time.time()
            print("[grabber] Cloning Chrome profile …")
            self._clone_profile(profile_dir, tmp_dir)
            print(f"[grabber] Profile cloned ({_elapsed(t_step)})")

            # --- Step 2: Launch Chrome ---
            t_step = time.time()
            print(f"[grabber] Launching Chrome (port {port}) …")
            proc = self._launch_chrome(chrome, tmp_dir, port)
            time.sleep(3)
            print(f"[grabber] Chrome launched ({_elapsed(t_step)})")

            # --- Step 3: Extract image URLs via CDP ---
            t_step = time.time()
            image_urls, title = self._extract_via_cdp_port(
                url, port=port, email=email,
            )
            print(f"[grabber] URL extraction finished ({_elapsed(t_step)})")

            # Auto-detect output filename from document title.
            if output is None:
                if title:
                    safe = _UNSAFE_FILENAME.sub("_", title).strip("_ ")
                    output = Path(f"{safe}.pdf")
                    print(f"[grabber] Auto-detected filename: {output}")
                else:
                    output = Path("output.pdf")
            output.parent.mkdir(parents=True, exist_ok=True)

            if not image_urls:
                raise RuntimeError(
                    "DocSend page_data API returned no image URLs.\n\n"
                    "Ensure you have visited docsend.com at least once in "
                    "Chrome so session cookies exist.\n"
                    "Or use --url-file with pre-extracted URLs."
                )

            # --- Step 4: Close browser (free resources) ---
            self._kill_chrome(proc, port)
            proc = None  # Prevent double-kill in finally.

            # --- Step 5: Download images ---
            t_step = time.time()
            results, failed = self._download_images(
                image_urls, workers=workers,
            )
            print(f"[grabber] Image download finished ({_elapsed(t_step)})")

            # --- Step 6: Retry failed pages with fresh URLs ---
            if failed:
                t_step = time.time()
                # 1-based page numbers for the page_data API.
                failed_pages = [i + 1 for i in failed]
                print(
                    f"[grabber] Re-extracting {len(failed)} failed page "
                    f"URLs …"
                )

                proc = self._launch_chrome(chrome, tmp_dir, port)
                time.sleep(3)

                fresh_urls = self._reextract_urls(
                    url, port=port, email=email,
                    page_numbers=failed_pages,
                )
                self._kill_chrome(proc, port)
                proc = None

                if fresh_urls:
                    retry_urls = [
                        fresh_urls[idx]
                        for idx in sorted(fresh_urls)
                    ]
                    retry_results, still_failed = self._download_images(
                        retry_urls, workers=workers,
                    )
                    # Map retry results back to original indices.
                    sorted_failed = sorted(fresh_urls.keys())
                    for retry_idx, orig_idx in enumerate(sorted_failed):
                        if retry_idx in retry_results:
                            results[orig_idx] = retry_results[retry_idx]
                            failed.remove(orig_idx)

                if failed:
                    print(
                        f"[grabber] Warning: {len(failed)} pages still "
                        f"failed after retry: {[i + 1 for i in failed]}"
                    )
                else:
                    print("[grabber] All pages recovered on retry")
                print(f"[grabber] Retry finished ({_elapsed(t_step)})")

            # --- Step 7: Compile PDF ---
            t_step = time.time()
            total_pages = len(image_urls)
            image_data = [results[i] for i in range(total_pages) if i in results]
            result = self._compile_pdf(image_data, output)
            print(f"[grabber] PDF compilation finished ({_elapsed(t_step)})")

            print(f"[grabber] Total time: {_elapsed(t_total)}")
            return result

        finally:
            self._kill_chrome(proc, port)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Dataroom / folder support
    # ------------------------------------------------------------------

    @staticmethod
    def _is_dataroom_url(url: str) -> bool:
        """Return ``True`` if *url* looks like a DocSend dataroom / space.

        Document URLs contain ``/d/`` (e.g. ``/view/SLUG/d/HASH``);
        dataroom URLs do not (e.g. ``/view/SLUG`` or ``/view/s/SLUG``).
        """
        from urllib.parse import urlparse

        path = urlparse(url).path
        return "/d/" not in path

    @staticmethod
    def _enumerate_documents(page: Page) -> list[dict[str, str]]:
        """Extract the document list from a DocSend dataroom page.

        Reads React fiber props on card elements.  The ``folder`` prop
        higher up the fiber tree contains the full folder structure
        including ``contents.nodes`` (documents) and ``ancestors``
        (parent folders).  Each returned dict has ``name``, ``href``,
        and ``section`` (the local directory path for this document,
        e.g. ``""`` for root or ``"SubFolder/Nested"``).
        """
        docs: list[dict[str, str]] = page.evaluate(
            """() => {
            const cards = document.querySelectorAll('[class*="index-module__card"]');
            const results = [];
            const seenHrefs = new Set();

            // First pass: find the folder prop on any card.  The folder
            // prop contains the *complete* contents list (and child
            // folders), so we only need to find it once per folder.
            const processedFolders = new Set();

            function collectFromFolder(folder, parentPath) {
                if (!folder || processedFolders.has(folder.databaseId))
                    return;
                processedFolders.add(folder.databaseId);

                // Build path for this folder.  The root/home folder
                // gets an empty path; sub-folders append their name.
                const isHome =
                    folder.ancestors
                    && folder.ancestors.nodes
                    && folder.ancestors.nodes.length === 0;
                const folderPath = isHome
                    ? parentPath
                    : (parentPath
                        ? parentPath + '/' + folder.name
                        : folder.name);

                // Collect documents in this folder.
                if (folder.contents && folder.contents.nodes) {
                    for (const node of folder.contents.nodes) {
                        if (node.__typename === 'SpaceDocument'
                            && node.href && node.name
                            && !seenHrefs.has(node.href)) {
                            seenHrefs.add(node.href);
                            results.push({
                                name: node.name,
                                href: node.href,
                                section: folderPath,
                            });
                        }
                    }
                }
            }

            for (const card of cards) {
                const fiberKey = Object.keys(card).find(
                    k => k.startsWith('__reactFiber$')
                         || k.startsWith('__reactInternalInstance$')
                );
                if (!fiberKey) continue;
                let fiber = card[fiberKey];
                for (let i = 0; i < 30 && fiber; i++) {
                    if (fiber.memoizedProps
                        && fiber.memoizedProps.folder
                        && fiber.memoizedProps.folder.__typename === 'SpaceFolder') {
                        collectFromFolder(
                            fiber.memoizedProps.folder, '',
                        );
                        break;
                    }
                    fiber = fiber.return;
                }
            }

            // Fallback: if folder-based extraction found nothing,
            // fall back to the simpler SpaceDocument-on-card approach.
            if (results.length === 0) {
                for (const card of cards) {
                    const fiberKey = Object.keys(card).find(
                        k => k.startsWith('__reactFiber$')
                             || k.startsWith('__reactInternalInstance$')
                    );
                    if (!fiberKey) continue;
                    let fiber = card[fiberKey];
                    for (let i = 0; i < 20 && fiber; i++) {
                        if (fiber.memoizedProps
                            && fiber.memoizedProps.href
                            && fiber.memoizedProps.name
                            && fiber.memoizedProps.type === 'SpaceDocument') {
                            const href = fiber.memoizedProps.href;
                            if (!seenHrefs.has(href)) {
                                seenHrefs.add(href);
                                results.push({
                                    name: fiber.memoizedProps.name,
                                    href: href,
                                    section: '',
                                });
                            }
                            break;
                        }
                        fiber = fiber.return;
                    }
                }
            }

            return results;
        }"""
        )
        return docs or []

    @staticmethod
    def _check_dataroom_download(page: Page) -> bool:
        """Check whether the dataroom landing page has a bulk download option."""
        return page.evaluate(
            """() => {
            // Look for a download button/link anywhere on the dataroom page.
            const selectors = [
                'a[href*="download"]',
                'button[aria-label*="ownload"]',
                '[data-testid*="download"]',
                '[class*="download"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const style = getComputedStyle(el);
                    if (style.display !== 'none'
                        && style.visibility !== 'hidden')
                        return true;
                }
            }
            return false;
        }"""
        )

    @staticmethod
    def _check_download_enabled(page: Page) -> bool:
        """Check whether a DocSend document offers a direct download button."""
        return page.evaluate(
            """() => {
            // Toolbar download icon / link.
            const toolbar = document.getElementById('toolbar')
                            || document.querySelector('.presentation-toolbar');
            if (toolbar) {
                const btn = toolbar.querySelector(
                    '[aria-label*="ownload"], a[href*="download"],'
                    + ' [data-testid*="download"]'
                );
                if (btn) return true;
            }
            // presentationConfig flag.
            try {
                const c = window.presentationConfig;
                if (c && (c.allowDownload || c.isDownloadable)) return true;
            } catch {}
            return false;
        }"""
        )

    def _fetch_dataroom(
        self,
        url: str,
        output: Path | None,
        *,
        email: str | None = None,
        workers: int = 16,
    ) -> Path:
        """Download all documents from a DocSend dataroom / space.

        Output defaults to ``~/datarooms/<dataroom-title>/``.  If the
        dataroom contains folders, the local directory structure mirrors
        the remote hierarchy.  A ``_dataroom_index.pdf`` screenshot of
        the landing page is saved in the root of the output directory.
        """
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

        t_total = time.time()
        tmp_dir = tempfile.mkdtemp(prefix="grabber_chrome_")
        proc: subprocess.Popen | None = None
        port = 9222

        try:
            # --- Clone profile & launch Chrome ---
            t_step = time.time()
            print("[grabber] Cloning Chrome profile …")
            self._clone_profile(profile_dir, tmp_dir)
            print(f"[grabber] Profile cloned ({_elapsed(t_step)})")

            t_step = time.time()
            print(f"[grabber] Launching Chrome (port {port}) …")
            proc = self._launch_chrome(chrome, tmp_dir, port)
            time.sleep(3)
            print(f"[grabber] Chrome launched ({_elapsed(t_step)})")

            cdp_endpoint = f"http://127.0.0.1:{port}"

            # --- Enumerate documents ---
            doc_entries: list[dict[str, str]] = []
            dataroom_title: str | None = None
            # Per-document results: (name, section, image_urls).
            extractions: list[tuple[str, str, list[str]]] = []
            download_enabled = False
            landing_screenshot: bytes | None = None

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_endpoint)
                context = (
                    browser.contexts[0]
                    if browser.contexts
                    else browser.new_context()
                )
                page = context.new_page()
                page.set_default_timeout(60_000)
                self._minimize_window(context, page)

                # Navigate to the dataroom landing page.
                print(f"[grabber] Opening {url}")
                page.goto(url, wait_until="domcontentloaded")

                # Wait for the document cards to render.
                try:
                    page.wait_for_selector(
                        '[class*="index-module__card"]',
                        timeout=30_000,
                    )
                except Exception:
                    pass  # Fall through — enumerate will return [].

                # Small pause to let images/thumbnails finish loading.
                time.sleep(1)

                # Capture landing page screenshot for the index PDF.
                # The window is minimized, so we must restore it before
                # taking the screenshot (minimized windows don't render).
                try:
                    cdp_ss = context.new_cdp_session(page)
                    win = cdp_ss.send("Browser.getWindowForTarget")
                    cdp_ss.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": win["windowId"],
                            "bounds": {"windowState": "normal"},
                        },
                    )
                    time.sleep(0.5)
                    # Resize viewport to capture full page height.
                    layout = cdp_ss.send("Page.getLayoutMetrics")
                    cs = layout.get(
                        "contentSize",
                        layout.get("cssContentSize", {}),
                    )
                    full_h = min(int(cs.get("height", 900)), 8000)
                    full_w = int(cs.get("width", 1280))
                    page.set_viewport_size(
                        {"width": full_w, "height": full_h},
                    )
                    time.sleep(0.3)
                    result = cdp_ss.send(
                        "Page.captureScreenshot",
                        {"format": "png"},
                    )
                    landing_screenshot = base64.b64decode(
                        result["data"],
                    )
                    print("[grabber] Captured landing page screenshot")
                    # Re-minimize.
                    cdp_ss.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": win["windowId"],
                            "bounds": {"windowState": "minimized"},
                        },
                    )
                except Exception as exc:
                    print(
                        f"[grabber] Warning: could not capture "
                        f"landing page: {exc}"
                    )

                doc_entries = self._enumerate_documents(page)

                # Extract the dataroom/space title for the output dir.
                dataroom_title = page.evaluate(
                    """() => {
                    // Try headings first.
                    for (const sel of ['h1', 'h2', '[class*="spaceName"]']) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const t = el.innerText.trim();
                            if (t && t !== 'DocSend' && t.length > 2)
                                return t;
                        }
                    }
                    // Fallback: first substantial text node in the
                    // sidebar/nav area (after "Create login").
                    const body = document.body.innerText || '';
                    const lines = body.split('\\n')
                        .map(l => l.trim())
                        .filter(l => l.length > 3
                            && l !== 'DocSend'
                            && l !== 'Create login'
                            && !l.includes('Privacy')
                            && !l.includes('Cookies'));
                    return lines.length ? lines[0] : null;
                }"""
                )

                if not doc_entries:
                    raise RuntimeError(
                        "No documents found in this dataroom.\n"
                        "Ensure you have visited docsend.com at least "
                        "once in Chrome so session cookies exist."
                    )

                # Report documents grouped by section.
                sections: dict[str, list[str]] = {}
                for d in doc_entries:
                    sec = d.get("section", "") or ""
                    sections.setdefault(sec, []).append(d["name"])

                print(
                    f"[grabber] Found {len(doc_entries)} documents "
                    f"in dataroom:"
                )
                for sec, names in sections.items():
                    label = sec if sec else "(root)"
                    for name in names:
                        print(f"[grabber]   {label}/{name}")

                # --- Check download availability ---
                # First check the dataroom landing page itself.
                dataroom_download = self._check_dataroom_download(page)
                if dataroom_download:
                    print(
                        "[grabber] Dataroom-level download "
                        "button detected"
                    )

                # Then check on the first individual document.
                first_url = doc_entries[0]["href"]
                self._navigate_and_gate(page, first_url, email=email)
                page.wait_for_selector(
                    ".toolbar-page-indicator", timeout=30_000,
                )
                download_enabled = self._check_download_enabled(page)

                if download_enabled:
                    print("[grabber] Direct download is enabled")
                elif not dataroom_download:
                    print(
                        "[grabber] Direct download disabled – "
                        "using page_data extraction"
                    )

                # --- Extract image URLs for each document ---
                for i, doc in enumerate(doc_entries):
                    t_doc = time.time()
                    doc_url = doc["href"]
                    doc_name = doc["name"]
                    doc_section = doc.get("section", "") or ""
                    label = (
                        f"{doc_section}/{doc_name}"
                        if doc_section
                        else doc_name
                    )
                    print(
                        f"\n[grabber] [{i + 1}/{len(doc_entries)}] "
                        f"Extracting: {label}"
                    )

                    # First document was already navigated above.
                    if i > 0:
                        self._navigate_and_gate(
                            page, doc_url, email=email,
                        )

                    total_pages = self._get_total_pages(page)
                    if not total_pages:
                        print(
                            f"[grabber]   Warning: could not detect "
                            f"pages for {doc_name}, skipping"
                        )
                        continue

                    image_urls = self._extract_image_urls(
                        page, total_pages,
                    )
                    if not image_urls:
                        print(
                            f"[grabber]   Warning: no image URLs "
                            f"for {doc_name}, skipping"
                        )
                        continue

                    extractions.append(
                        (doc_name, doc_section, image_urls),
                    )
                    print(
                        f"[grabber]   Extracted {len(image_urls)} "
                        f"pages ({_elapsed(t_doc)})"
                    )

                page.close()

            # --- Kill browser before downloading ---
            self._kill_chrome(proc, port)
            proc = None

            # --- Determine output directory ---
            if output is None:
                dir_name = (
                    _UNSAFE_FILENAME.sub("_", dataroom_title).strip("_ ")
                    if dataroom_title
                    else None
                )
                if not dir_name:
                    from urllib.parse import urlparse

                    dir_name = urlparse(url).path.rstrip("/").split(
                        "/",
                    )[-1]
                base_dir = Path.home() / "datarooms"
                out_dir = base_dir / dir_name
                print(f"[grabber] Output directory: {out_dir}/")
            else:
                out_dir = Path(output)
            out_dir.mkdir(parents=True, exist_ok=True)

            # --- Save landing page index PDF ---
            if landing_screenshot:
                index_pdf = out_dir / "_dataroom_index.pdf"
                try:
                    with open(index_pdf, "wb") as f:
                        f.write(img2pdf.convert(landing_screenshot))
                    print(f"[grabber] Saved landing page: {index_pdf}")
                except Exception as exc:
                    print(
                        f"[grabber] Warning: could not save "
                        f"landing page PDF: {exc}"
                    )

            # --- Download and compile each document ---
            for doc_name, doc_section, image_urls in extractions:
                t_doc = time.time()
                label = (
                    f"{doc_section}/{doc_name}"
                    if doc_section
                    else doc_name
                )
                print(f"\n[grabber] Downloading: {label}")

                results, failed = self._download_images(
                    image_urls, workers=workers,
                )

                if failed:
                    # Retry with fresh URLs.
                    t_retry = time.time()
                    failed_pages = [idx + 1 for idx in failed]
                    print(
                        f"[grabber]   Re-extracting {len(failed)} "
                        f"failed page URLs …"
                    )

                    proc = self._launch_chrome(chrome, tmp_dir, port)
                    time.sleep(3)

                    # Find the document URL that matches this name.
                    doc_url = next(
                        d["href"]
                        for d in doc_entries
                        if d["name"] == doc_name
                    )
                    fresh = self._reextract_urls(
                        doc_url,
                        port=port,
                        email=email,
                        page_numbers=failed_pages,
                    )
                    self._kill_chrome(proc, port)
                    proc = None

                    if fresh:
                        retry_urls = [
                            fresh[idx] for idx in sorted(fresh)
                        ]
                        retry_results, still_failed = (
                            self._download_images(
                                retry_urls, workers=workers,
                            )
                        )
                        sorted_failed = sorted(fresh.keys())
                        for retry_idx, orig_idx in enumerate(
                            sorted_failed
                        ):
                            if retry_idx in retry_results:
                                results[orig_idx] = retry_results[
                                    retry_idx
                                ]
                                failed.remove(orig_idx)

                    if failed:
                        print(
                            f"[grabber]   Warning: {len(failed)} "
                            f"pages still failed: "
                            f"{[idx + 1 for idx in failed]}"
                        )
                    else:
                        print("[grabber]   All pages recovered")
                    print(
                        f"[grabber]   Retry finished "
                        f"({_elapsed(t_retry)})"
                    )

                total = len(image_urls)
                image_data = [
                    results[i] for i in range(total) if i in results
                ]
                safe_name = _UNSAFE_FILENAME.sub(
                    "_", doc_name,
                ).strip("_ ")

                # Place the PDF in the correct subfolder.
                if doc_section:
                    safe_section = "/".join(
                        _UNSAFE_FILENAME.sub("_", part).strip("_ ")
                        for part in doc_section.split("/")
                    )
                    doc_dir = out_dir / safe_section
                else:
                    doc_dir = out_dir
                doc_dir.mkdir(parents=True, exist_ok=True)

                doc_output = doc_dir / f"{safe_name}.pdf"
                self._compile_pdf(image_data, doc_output)
                print(f"[grabber]   Done ({_elapsed(t_doc)})")

            print(f"\n[grabber] Dataroom download complete: {out_dir}/")
            print(f"[grabber] Total time: {_elapsed(t_total)}")
            return out_dir

        finally:
            self._kill_chrome(proc, port)
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
    ) -> tuple[list[str], str | None]:
        """Connect to a Chrome instance on *port* and extract image URLs.

        Returns ``(image_urls, title)`` where *title* is the document
        title extracted from the page, or ``None`` if unavailable.
        """
        cdp_url = f"http://127.0.0.1:{port}"
        print(f"[grabber] Connecting via CDP: {cdp_url}")

        title: str | None = None
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )
            page = context.new_page()
            page.set_default_timeout(60_000)

            # Minimize the Chrome window before navigating so the user
            # never sees a visible browser pop up.
            self._minimize_window(context, page)

            self._navigate_and_gate(page, url, email=email)
            total_pages = self._get_total_pages(page)
            title = self._get_document_title(page)

            if not total_pages:
                page.close()
                return [], title

            image_urls = self._extract_image_urls(page, total_pages)
            page.close()

        return image_urls, title

    @staticmethod
    def _minimize_window(context, page) -> None:
        """Minimize the Chrome window via CDP."""
        try:
            cdp = context.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")
            cdp.send(
                "Browser.setWindowBounds",
                {
                    "windowId": win["windowId"],
                    "bounds": {"windowState": "minimized"},
                },
            )
        except Exception:
            pass  # Non-critical — window stays visible.

    def _reextract_urls(
        self,
        url: str,
        *,
        port: int = 9222,
        email: str | None = None,
        page_numbers: list[int],
    ) -> dict[int, str]:
        """Re-launch a CDP session and extract URLs for specific pages.

        Returns a dict mapping 0-based index to fresh signed URL.
        """
        cdp_url = f"http://127.0.0.1:{port}"
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )
            page = context.new_page()
            page.set_default_timeout(60_000)
            self._minimize_window(context, page)

            self._navigate_and_gate(page, url, email=email)
            # Wait for the viewer to load before hitting page_data.
            self._get_total_pages(page)

            fresh = self._extract_specific_urls(page, page_numbers)
            page.close()

        if fresh:
            print(
                f"[grabber] Re-extracted {len(fresh)} fresh URLs"
            )
        return fresh

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
    def _get_document_title(page: Page) -> str | None:
        """Extract the document title from the DocSend page.

        The title lives in a ``data-react-props`` JSON blob on the active
        drawer link element (``fileName`` key).  Falls back to the drawer
        title text and ``document.title``.
        """
        try:
            raw: str = page.evaluate(
                """() => {
                // Primary: fileName from the active drawer link's React props.
                try {
                    const el = document.querySelector(
                        '.drawer_link--active[data-react-props]'
                    );
                    if (el) {
                        const props = JSON.parse(el.getAttribute('data-react-props'));
                        if (props.fileName) return props.fileName;
                    }
                } catch {}
                // Fallback: any drawer link's React props for current URL.
                try {
                    const links = document.querySelectorAll('[data-react-props]');
                    for (const el of links) {
                        const props = JSON.parse(el.getAttribute('data-react-props'));
                        if (props.fileName && props.presentationUrl
                            && window.location.href.includes(props.presentationUrl.split('/d/')[1] || '')) {
                            return props.fileName;
                        }
                    }
                } catch {}
                // Fallback: drawer title text.
                const drawer = document.querySelector('.drawer_title');
                if (drawer) {
                    const t = drawer.innerText.trim();
                    if (t && t !== 'DocSend') return t;
                }
                // Last resort: document.title.
                const t = document.title || '';
                const cleaned = t.replace(/ [-|] DocSend$/i, '').trim();
                return cleaned && cleaned !== 'DocSend' ? cleaned : null;
            }"""
            )
            return raw or None
        except Exception:
            return None

    @staticmethod
    def _extract_image_urls(page: Page, total_pages: int) -> list[str]:
        """Extract signed image URLs from the page_data API.

        Fires all page_data requests concurrently via a single
        ``Promise.all`` — the browser handles HTTP/2 multiplexing.
        """
        print(f"[grabber] Extracting {total_pages} pages via page_data API …")
        image_urls: list[str] = page.evaluate(
            """async (totalPages) => {
            const base = window.location.href.split('?')[0]
                         .replace(/\\/$/, '') + '/page_data/';
            const results = new Array(totalPages).fill(null);
            await Promise.all(
                Array.from({length: totalPages}, (_, i) =>
                    fetch(base + (i + 1), {credentials: 'same-origin'})
                        .then(r => r.ok ? r.json() : null)
                        .then(d => { results[i] = d && d.imageUrl ? d.imageUrl : null; })
                        .catch(() => { results[i] = null; })
                )
            );
            return results.filter(Boolean);
        }""",
            total_pages,
        )
        if image_urls:
            print(f"[grabber] Extracted {len(image_urls)} image URLs")
        return image_urls or []

    @staticmethod
    def _extract_specific_urls(
        page: Page, page_numbers: list[int],
    ) -> dict[int, str]:
        """Extract signed image URLs for specific 1-based page numbers.

        Returns a dict mapping 0-based index to image URL for each page
        that was successfully extracted.
        """
        result: dict[int, str] = page.evaluate(
            """async (pageNumbers) => {
            const base = window.location.href.split('?')[0]
                         .replace(/\\/$/, '') + '/page_data/';
            const out = {};
            await Promise.all(
                pageNumbers.map(n =>
                    fetch(base + n, {credentials: 'same-origin'})
                        .then(r => r.ok ? r.json() : null)
                        .then(d => { if (d && d.imageUrl) out[n] = d.imageUrl; })
                        .catch(() => {})
                )
            );
            return out;
        }""",
            page_numbers,
        )
        # Convert 1-based keys (from JS) to 0-based indices.
        return {int(k) - 1: v for k, v in result.items()}

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
        image_urls: list[str],
        *,
        workers: int = 16,
        retries: int = 2,
    ) -> tuple[dict[int, bytes], list[int]]:
        """Download page images concurrently with automatic retries.

        Signed CloudFront URLs expire after ~3.5 min so we use a thread pool
        to parallelise downloads and finish before they go stale.  Failed
        downloads are retried up to *retries* times with a short back-off.

        Returns ``(results, failed)`` where *results* maps 0-based page
        index to image bytes, and *failed* lists 0-based indices that
        could not be downloaded.
        """
        total = len(image_urls)
        print(f"[grabber] Downloading {total} pages ({workers} workers) …")

        results: dict[int, bytes] = {}

        def _fetch_one(idx: int, img_url: str) -> tuple[int, bytes | None]:
            for attempt in range(1 + retries):
                try:
                    resp = requests.get(img_url, timeout=30)
                    if resp.status_code == 200:
                        return idx, resp.content
                except requests.RequestException:
                    pass
                if attempt < retries:
                    time.sleep(1 * (attempt + 1))
            return idx, None

        failed: list[int] = []

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
                    failed.append(idx)
                print(
                    f"[grabber] Downloaded {done_count}/{total}"
                    + (
                        f" ({len(failed)} failed)"
                        if failed
                        else ""
                    ),
                    end="\r",
                )

        print()
        if failed:
            failed.sort()
            print(
                f"[grabber] Warning: failed pages: "
                f"{[i + 1 for i in failed]}"
            )

        return results, failed

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
