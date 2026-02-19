"""CLI entry point for grabber."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grabber.providers import PROVIDERS, detect_provider


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="grabber",
        description="Download documents from viewer-only systems (DocSend, etc.) as PDF.",
    )
    parser.add_argument("url", help="URL of the document viewer page")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "Output path. For single docs: PDF file path "
            "(default: auto-detected from title). "
            "For datarooms: directory path "
            "(default: ~/datarooms/<name>/)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help=(
            "Number of concurrent download threads (default: 16). "
            "Higher values help finish before signed URLs expire (~3.5 min)."
        ),
    )

    # Let each provider register its own flags in a named group.
    for name, cls in PROVIDERS.items():
        group = parser.add_argument_group(f"{name} options")
        cls.add_arguments(group)

    args = parser.parse_args(argv)

    provider_cls = detect_provider(args.url)
    if provider_cls is None:
        print(f"Error: no provider can handle URL: {args.url}", file=sys.stderr)
        sys.exit(1)

    provider = provider_cls()
    kwargs = vars(args)
    # Pop universal args that fetch() takes as positional/explicit params.
    url = kwargs.pop("url")
    output_raw = kwargs.pop("output")
    output = Path(output_raw) if output_raw else None

    provider.fetch(url=url, output=output, **kwargs)


if __name__ == "__main__":
    main()
