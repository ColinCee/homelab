"""Shared stats formatting for lifecycle stage comments."""

import re

from services.copilot import CLIResult

STATUS_EMOJI = {
    "complete": "✅",
    "partial": "⚠️",
    "failed": "❌",
}


def format_stage_stats(
    *,
    premium_requests: int = 0,
    elapsed_seconds: float = 0,
    api_time_seconds: int = 0,
    effort: str = "",
    models: dict | None = None,
    tokens_line: str = "",
) -> str:
    """Format a compact stats footer for a lifecycle stage comment."""
    parts = []
    if premium_requests:
        parts.append(f"💰 {premium_requests} premium")
    if elapsed_seconds:
        minutes, secs = divmod(int(elapsed_seconds), 60)
        time_str = f"⏱️ {minutes}m {secs}s"
        if api_time_seconds:
            am, as_ = divmod(api_time_seconds, 60)
            time_str += f" (API: {am}m {as_}s)"
        parts.append(time_str)
    if effort:
        parts.append(f"🧠 {effort}")
    if models:
        for model_name, detail in models.items():
            clean = re.sub(r"\s*\(Est\..*?\)", "", detail).strip().rstrip(",")
            parts.append(f"🤖 {model_name}: {clean}")
    elif tokens_line:
        parts.append(f"📊 {tokens_line}")
    return " · ".join(parts)


def cli_stage_stats(result: CLIResult, effort: str = "") -> str:
    """Format stats from a CLIResult."""
    return format_stage_stats(
        premium_requests=result.total_premium_requests,
        elapsed_seconds=result.session_time_seconds,
        api_time_seconds=result.api_time_seconds,
        effort=effort,
        models=result.models,
        tokens_line=result.tokens_line,
    )
