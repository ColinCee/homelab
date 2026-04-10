"""Prometheus metrics for the agent service."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

TASK_TYPES = ("review", "implement", "fix")
TASK_STATUSES = ("complete", "failed", "partial")

METRICS_REGISTRY = CollectorRegistry()

TASK_DURATION_SECONDS = Histogram(
    "agent_task_duration_seconds",
    "Wall-clock time spent running agent background tasks.",
    labelnames=("task_type", "status"),
    registry=METRICS_REGISTRY,
)
TASK_TOTAL = Counter(
    "agent_task_total",
    "Total number of agent tasks by task type and terminal status.",
    labelnames=("task_type", "status"),
    registry=METRICS_REGISTRY,
)
PREMIUM_REQUESTS_TOTAL = Counter(
    "agent_premium_requests_total",
    "Total Copilot premium requests consumed by agent tasks.",
    labelnames=("task_type",),
    registry=METRICS_REGISTRY,
)
TASK_IN_PROGRESS = Gauge(
    "agent_task_in_progress",
    "Current number of in-flight agent tasks.",
    labelnames=("task_type",),
    registry=METRICS_REGISTRY,
)


def _initialize_metrics() -> None:
    for task_type in TASK_TYPES:
        TASK_IN_PROGRESS.labels(task_type=task_type).set(0)
        PREMIUM_REQUESTS_TOTAL.labels(task_type=task_type)
        for status in TASK_STATUSES:
            TASK_DURATION_SECONDS.labels(task_type=task_type, status=status)
            TASK_TOTAL.labels(task_type=task_type, status=status)


def reset_metrics() -> None:
    """Clear collector state between tests."""
    for collector in (
        TASK_DURATION_SECONDS,
        TASK_TOTAL,
        PREMIUM_REQUESTS_TOTAL,
        TASK_IN_PROGRESS,
    ):
        with collector._lock:
            collector._metrics.clear()
    _initialize_metrics()


_initialize_metrics()
