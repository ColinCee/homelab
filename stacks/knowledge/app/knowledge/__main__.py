"""CLI entrypoint: python -m knowledge <command> ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .ingest import DEFAULT_DIRECTORY_GLOB, ingest_directory, ingest_file, ingest_text
from .search import DEFAULT_RESULT_LIMIT, format_search_results, search


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(prog="knowledge", description="Knowledge base CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest documents")
    ingest_parser.add_argument("--path", type=Path, help="File to ingest (.md or .txt)")
    ingest_parser.add_argument("--dir", type=Path, help="Directory of files to ingest")
    ingest_parser.add_argument("--text", help="Raw text to ingest")
    ingest_parser.add_argument("--title", help="Title for raw text (required with --text)")
    ingest_parser.add_argument(
        "--source-id", help="Stable ID for text notes (disambiguates duplicate titles)"
    )
    ingest_parser.add_argument(
        "--glob",
        default=DEFAULT_DIRECTORY_GLOB,
        help=f"Glob pattern for directory ingest (default: {DEFAULT_DIRECTORY_GLOB})",
    )

    search_parser = subparsers.add_parser("search", help="Search ingested chunks")
    search_parser.add_argument("query", help="Natural language query text")
    search_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_RESULT_LIMIT,
        help=f"Maximum number of results to return (default: {DEFAULT_RESULT_LIMIT})",
    )

    args = parser.parse_args()

    try:
        if args.command == "ingest":
            _handle_ingest(args)
        elif args.command == "search":
            _handle_search(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _handle_ingest(args: argparse.Namespace) -> None:
    source_flags = [args.path is not None, args.dir is not None, args.text is not None]
    if sum(source_flags) > 1:
        print("Error: provide exactly one of --path, --dir, or --text", file=sys.stderr)
        sys.exit(1)

    if not any(source_flags):
        print("Error: provide --path, --dir, or --text", file=sys.stderr)
        sys.exit(1)

    if args.glob != DEFAULT_DIRECTORY_GLOB and args.dir is None:
        print("Error: --glob requires --dir", file=sys.stderr)
        sys.exit(1)

    if args.text and not args.title:
        print("Error: --title is required with --text", file=sys.stderr)
        sys.exit(1)

    if args.path:
        path = Path(args.path)
        if not path.is_file():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        result = ingest_file(path)
    elif args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            print(f"Error: directory not found: {directory}", file=sys.stderr)
            sys.exit(1)
        result = ingest_directory(directory, glob_pattern=args.glob)
    else:
        result = ingest_text(args.text, title=args.title, source_id=args.source_id)

    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _handle_search(args: argparse.Namespace) -> None:
    results = search(args.query, limit=args.limit)
    print(format_search_results(results))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return parsed


if __name__ == "__main__":
    main()
