"""Small, dependency-free observability primitives for the production gateway.

The registry deliberately accepts only bounded labels and values.  It is not a
request logger: prompts, customer material, credentials and provider payloads
must never be placed in metrics.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Any, Iterable, Mapping


_MAX_LABELS = 8
_MAX_LABEL_LENGTH = 128
_MAX_METRIC_NAME = 128


def safe_metric_label(name: str, value: Any) -> str:
    """Return a bounded, single-line Prometheus label value."""
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text[:_MAX_LABEL_LENGTH]


def _metric_name(name: str) -> str:
    text = str(name).strip()
    if not text or len(text) > _MAX_METRIC_NAME or not all(c.isalnum() or c == "_" for c in text):
        raise ValueError("invalid metric name")
    return text


def _labels(labels: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    if len(labels) > _MAX_LABELS:
        raise ValueError("too many metric labels")
    result: list[tuple[str, str]] = []
    for key, value in sorted(labels.items()):
        label = str(key).strip()
        if not label or len(label) > 64 or not all(c.isalnum() or c == "_" for c in label):
            raise ValueError("invalid metric label")
        result.append((label, safe_metric_label(label, value)))
    return tuple(result)


def _format_labels(labels: Iterable[tuple[str, str]]) -> str:
    pairs = []
    for key, value in labels:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        pairs.append(f'{key}="{escaped}"')
    return "{" + ",".join(pairs) + "}" if pairs else ""


class MetricsRegistry:
    """Thread-safe counters and observations with Prometheus text output."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: defaultdict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)

    def inc(self, name: str, value: float = 1, *, labels: Mapping[str, Any] | None = None) -> None:
        if value < 0:
            raise ValueError("counter increment must be non-negative")
        key = (_metric_name(name), _labels(labels))
        with self._lock:
            self._counters[key] += float(value)

    def observe(self, name: str, value: float, *, labels: Mapping[str, Any] | None = None) -> None:
        key = (_metric_name(name), _labels(labels))
        with self._lock:
            values = self._observations[key]
            if len(values) < 10_000:
                values.append(float(value))

    def scrape(self) -> str:
        with self._lock:
            counters = list(self._counters.items())
            observations = [(key, list(values)) for key, values in self._observations.items()]
        lines: list[str] = []
        for (name, labels), value in sorted(counters):
            lines.append(f"{name}{_format_labels(labels)} {value:g}")
        for (name, labels), values in sorted(observations):
            if not values:
                continue
            suffix = _format_labels(labels)
            lines.append(f"{name}_count{suffix} {len(values)}")
            lines.append(f"{name}_sum{suffix} {sum(values):g}")
        return "\n".join(lines) + ("\n" if lines else "")


def readiness_report(checks: Mapping[str, tuple[bool, str] | bool]) -> dict[str, Any]:
    """Normalize dependency checks without exposing exception text or secrets."""
    normalized: dict[str, str] = {}
    all_ready = True
    for name, result in checks.items():
        ok = result[0] if isinstance(result, tuple) else bool(result)
        normalized[safe_metric_label(name, name)] = "ok" if ok else "failed"
        all_ready = all_ready and ok
    return {"status": "ready" if all_ready else "not_ready", "checks": normalized}


__all__ = ["MetricsRegistry", "readiness_report", "safe_metric_label"]
