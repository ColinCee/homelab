"""CLI entrypoint: python -m knowledge ingest ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ingest import ingest_file, ingest_text


def main() -> None:
    parser = argparse.ArgumentParser(prog="knowledge", description="Knowledge base CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest documents")
    ingest_parser.add_argument("--workspace", required=True, help="Target workspace name")
    ingest_parser.add_argument("--path", type=Path, help="File to ingest (.md or .txt)")
    ingest_parser.add_argument("--text", help="Raw text to ingest")
    ingest_parser.add_argument("--title", help="Title for raw text (required with --text)")
    ingest_parser.add_argument(
        "--source-id", help="Stable ID for text notes (disambiguates duplicate titles)"
    )

    args = parser.parse_args()

    if args.command == "ingest":
        _handle_ingest(args)


def _handle_ingest(args: argparse.Namespace) -> None:
    if args.path and args.text:
        print("Error: provide --path or --text, not both", file=sys.stderr)
        sys.exit(1)

    if not args.path and not args.text:
        print("Error: provide --path or --text", file=sys.stderr)
        sys.exit(1)

    if args.text and not args.title:
        print("Error: --title is required with --text", file=sys.stderr)
        sys.exit(1)

    if args.path:
        path = Path(args.path)
        if not path.is_file():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        result = ingest_file(path, workspace=args.workspace)
    else:
        result = ingest_text(
            args.text, title=args.title, workspace=args.workspace, source_id=args.source_id
        )

    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
