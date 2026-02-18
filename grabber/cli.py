"""CLI entry point for grabber."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grabber.providers import detect_provider


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="grabber",
        description="Download documents from viewer-only systems (DocSend, etc.) as PDF.",
    )
    parser.add_argument("url", help="URL of the document viewer page")
    parser.add_argument(
        "-o",
        "--output",
        default="output.pdf",
        help="Output PDF path (default: output.pdf)",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Email to bypass an access gate (if required)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run the browser in visible (non-headless) mode for debugging",
    )

    args = parser.parse_args(argv)

    provider_cls = detect_provider(args.url)
    if provider_cls is None:
        print(f"Error: no provider can handle URL: {args.url}", file=sys.stderr)
        sys.exit(1)

    provider = provider_cls()
    provider.fetch(
        url=args.url,
        output=Path(args.output),
        email=args.email,
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    main()
