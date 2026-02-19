# CLAUDE.md — grabber

## Validation Commands

- You MUST verify the package installs cleanly: `pip install -e .`
- You MUST verify the CLI entry point works: `grabber --help`
- You MUST verify provider detection: `python -c "from grabber.providers import detect_provider; assert detect_provider('https://docsend.com/view/x')"`
- You SHOULD run the CLAUDE.md linter: `npx github:LaymanAI/linter . --fail-under 80`

## Architecture

- `grabber/cli.py` — CLI entry point; parses args, detects the provider, and calls `fetch()`
- `grabber/providers/base.py` — abstract `BaseProvider` class that all providers MUST subclass
- `grabber/providers/__init__.py` — provider registry and `detect_provider(url)` auto-detection
- `grabber/providers/docsend.py` — DocSend provider; handles both single documents and dataroom/folder URLs; launches system Chrome with cloned user profile, extracts page image URLs via page_data API, downloads concurrently, compiles PDF(s) with img2pdf
- `grabber/scripts/docsend_console.js` — browser console script for in-browser extraction; used for manual and MCP/agent workflows

## Provider Development

- You MUST create a new file in `grabber/providers/` named after the service in lowercase
- You MUST subclass `BaseProvider` and implement `can_handle(url)` and `fetch()`
- You MUST register the provider in `grabber/providers/__init__.py` by importing it and adding it to the `PROVIDERS` dict
- You SHOULD follow the existing DocSend provider pattern: launch browser, handle access gates, extract image URLs, download, compile to PDF

## Code Standards

- You MUST use Python 3.10+ features (`from __future__ import annotations`, union syntax)
- You MUST keep all provider logic self-contained within its own module
- You SHOULD prefix all user-facing print output with `[grabber]`
- You MUST NOT hardcode page counts or document metadata; instead, detect them dynamically from the DOM

## Known Constraints

- DocSend's page_data API requires session cookies from an established Chrome profile; fresh profiles (including Playwright's bundled Chromium) always receive 403
- The default strategy clones the user's Chrome profile to a temp directory and launches Chrome with `--remote-debugging-port`; this requires Google Chrome to be installed
- Signed CloudFront image URLs expire after ~3.5 minutes; downloads MUST use concurrent workers (default 16) to finish before expiry
- CORS blocks in-browser `fetch()` of CloudFront image URLs; the console script works because `<img src>` bypasses CORS
- MCP browser extensions may filter signed URLs, JWTs, and encoded data from JS return values; the console script avoids this by rendering images directly in the page
- Dataroom URLs (without `/d/` in the path) are detected automatically and trigger multi-document download; each document is extracted sequentially while the browser is alive, then all images are downloaded after the browser is closed
- Dataroom output defaults to `~/datarooms/<name>/`; a `_dataroom_index.pdf` screenshot of the landing page is saved in the root
- Dataroom document enumeration uses React fiber props (`__reactFiber$`) on card elements; the `folder` prop (type `SpaceFolder`) higher in the fiber tree contains the complete `contents.nodes` array with all documents, plus `ancestors` and `childFolderIds` for hierarchy
- Dataroom folder hierarchy is replicated locally: documents in subfolders are placed in corresponding subdirectories

## Security

- API keys, tokens, and credentials MUST NEVER appear in source code; instead, pass them via CLI args or environment variables
- You MUST NOT commit `.env` files or downloaded PDFs to version control; instead, ensure they are covered by `.gitignore`
- You MUST NOT execute arbitrary code from fetched page content; instead, only extract data URLs from JSON API responses
- You MUST NOT log or print sensitive values like emails or access tokens; instead, redact or omit them from output
- You SHOULD treat all URLs and email addresses passed via CLI as untrusted user input
- The cloned Chrome profile is written to a temp directory and deleted after use; it MUST NOT persist
