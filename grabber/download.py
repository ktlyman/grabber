"""Shared image download and PDF compilation helpers.

These are site-agnostic utilities used by any provider that follows the
*extract image URLs → download concurrently → compile PDF* pattern.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import img2pdf
import requests

# Suppress img2pdf alpha-channel warnings (very noisy for large documents).
logging.getLogger("img2pdf").setLevel(logging.ERROR)


def download_images(
    image_urls: list[str],
    *,
    workers: int = 16,
    retries: int = 2,
) -> tuple[dict[int, bytes], list[int]]:
    """Download page images concurrently with automatic retries.

    Signed URLs (e.g. CloudFront) may expire quickly, so we use a thread
    pool to parallelise downloads.  Failed downloads are retried up to
    *retries* times with a short back-off.

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


def compile_pdf(image_data: list[bytes], output: Path) -> Path:
    """Compile downloaded image bytes into a PDF with img2pdf."""
    if not image_data:
        raise RuntimeError("No page images were downloaded.")
    print(f"[grabber] Compiling {len(image_data)} pages into PDF …")
    with open(output, "wb") as f:
        f.write(img2pdf.convert(image_data))
    print(f"[grabber] Saved to {output}")
    return output
