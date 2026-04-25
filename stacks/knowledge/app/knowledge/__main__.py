"""CLI entrypoint: python -m knowledge <command> ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from .database import connect, run_migrations
from .ingest import DEFAULT_DIRECTORY_GLOB, ingest_directory, ingest_file, ingest_text
from .related import format_related_results, related
from .save import save_url
from .search import DEFAULT_RESULT_LIMIT, format_search_results, search

EventValue = str | int | float | bool | None


class CLIError(Exception):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def main() -> None:
    _configure_logging()

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

    related_parser = subparsers.add_parser("related", help="List related documents")
    related_parser.add_argument("source_path", help="Document source_path to inspect")

    save_parser = subparsers.add_parser("save", help="Save a URL as a note")
    save_parser.add_argument("url", help="URL to fetch and save as a note")
    save_parser.add_argument(
        "--notes-dir",
        type=Path,
        default=Path("/notes"),
        help="Path to the notes repository (default: /notes)",
    )

    args = parser.parse_args()
    started_at = time.monotonic()

    try:
        _run_migrations()
        summary = _run_command(args)
    except KeyboardInterrupt:
        _emit_task_completion(
            command=args.command,
            status="cancelled",
            exit_code=130,
            duration_seconds=_duration_seconds(started_at),
            error="Interrupted",
        )
        raise SystemExit(130) from None
    except CLIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _emit_task_completion(
            command=args.command,
            status="failed",
            exit_code=exc.exit_code,
            duration_seconds=_duration_seconds(started_at),
            error=str(exc),
        )
        raise SystemExit(exc.exit_code) from None
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _emit_task_completion(
            command=args.command,
            status="failed",
            exit_code=1,
            duration_seconds=_duration_seconds(started_at),
            error=str(exc),
        )
        raise SystemExit(1) from None

    _emit_task_completion(
        command=args.command,
        status="succeeded",
        exit_code=0,
        duration_seconds=_duration_seconds(started_at),
        summary=summary,
    )


def _run_migrations() -> None:
    db = connect()
    try:
        run_migrations(db)
    finally:
        db.close()


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("jieba").setLevel(logging.WARNING)


def _run_command(args: argparse.Namespace) -> dict[str, EventValue]:
    if args.command == "ingest":
        return _handle_ingest(args)
    if args.command == "search":
        return _handle_search(args)
    if args.command == "related":
        return _handle_related(args)
    if args.command == "save":
        return _handle_save(args)
    raise CLIError(f"unknown command: {args.command}")


def _handle_ingest(args: argparse.Namespace) -> dict[str, EventValue]:
    source_flags = [args.path is not None, args.dir is not None, args.text is not None]
    if sum(source_flags) > 1:
        raise CLIError("provide exactly one of --path, --dir, or --text")

    if not any(source_flags):
        raise CLIError("provide --path, --dir, or --text")

    if args.glob != DEFAULT_DIRECTORY_GLOB and args.dir is None:
        raise CLIError("--glob requires --dir")

    if args.text and not args.title:
        raise CLIError("--title is required with --text")

    if args.path:
        path = Path(args.path)
        if not path.is_file():
            raise CLIError(f"file not found: {path}")
        result = ingest_file(path)
    elif args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            raise CLIError(f"directory not found: {directory}")
        result = ingest_directory(directory, glob_pattern=args.glob)
    else:
        result = ingest_text(args.text, title=args.title, source_id=args.source_id)

    payload = result.model_dump(mode="json")
    print(json.dumps(payload, indent=2))
    if args.dir:
        _check_directory_ingest_health(result)
    return _normalize_event_fields(payload)


def _check_directory_ingest_health(result: object) -> None:
    files_failed = getattr(result, "files_failed", 0)
    documents_processed = getattr(result, "documents_processed", 0)
    if files_failed > 0 and documents_processed == 0:
        raise CLIError(
            f"directory ingest failed: {files_failed} files failed and 0 were processed "
            "— likely a systemic issue (see logs above)",
            exit_code=2,
        )


def _handle_search(args: argparse.Namespace) -> dict[str, EventValue]:
    results = search(args.query, limit=args.limit)
    print(format_search_results(results))
    return {"result_count": len(results)}


def _handle_related(args: argparse.Namespace) -> dict[str, EventValue]:
    results = related(args.source_path)
    print(format_related_results(results))
    return {"result_count": len(results)}


def _handle_save(args: argparse.Namespace) -> dict[str, EventValue]:
    notes_dir = Path(args.notes_dir)
    if not notes_dir.is_dir():
        raise CLIError(f"notes directory not found: {notes_dir}")
    saved_path = save_url(args.url, notes_dir=notes_dir)
    print(f"Saved: {saved_path}")
    return {"saved_path": str(saved_path)}


def _emit_task_completion(
    *,
    command: str,
    status: str,
    exit_code: int,
    duration_seconds: float,
    summary: Mapping[str, object] | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, EventValue] = {
        "event": "task_completed",
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
    }
    if summary is not None:
        payload.update(_normalize_event_fields(summary))
    if error is not None:
        payload["error"] = error
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def _normalize_event_fields(fields: Mapping[str, object]) -> dict[str, EventValue]:
    normalized: dict[str, EventValue] = {}

    for key, value in fields.items():
        if isinstance(value, Path):
            normalized[key] = str(value)
        elif isinstance(value, str | int | float | bool) or value is None:
            normalized[key] = value

    return normalized


def _duration_seconds(started_at: float) -> float:
    return time.monotonic() - started_at


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be greater than zero")
    return parsed


if __name__ == "__main__":
    main()
