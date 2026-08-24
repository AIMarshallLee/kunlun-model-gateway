from __future__ import annotations

from app.observability import MetricsRegistry, readiness_report, safe_metric_label


def test_metrics_are_prometheus_scrapable_and_never_include_sensitive_values():
    metrics = MetricsRegistry()
    metrics.inc("gateway_requests_total", labels={"route": "/v1/chat", "status": "200"})
    metrics.inc("gateway_security_events_total", labels={"kind": "invalid_api_key"})
    metrics.observe("gateway_request_duration_seconds", 0.125, labels={"route": "/v1/chat"})

    output = metrics.scrape()
    assert "gateway_requests_total" in output
    assert 'route="/v1/chat"' in output
    assert "gateway_request_duration_seconds" in output
    assert "sk_live_secret" not in output
    assert "prompt正文" not in output


def test_metric_labels_are_bounded_and_escaped():
    assert safe_metric_label("route", "a\nsecret") == "a secret"
    assert len(safe_metric_label("provider", "x" * 500)) <= 128
    metrics = MetricsRegistry()
    metrics.inc("gateway_requests_total", labels={"route": 'x"y\\z'})
    assert 'route="x\\"y\\\\z"' in metrics.scrape()


def test_readiness_report_is_fail_closed_and_does_not_leak_check_details():
    report = readiness_report(
        {
            "database": (True, "ok"),
            "migrations": (False, "secret database password"),
        }
    )
    assert report["status"] == "not_ready"
    assert report["checks"]["database"] == "ok"
    assert report["checks"]["migrations"] == "failed"
    assert "password" not in str(report)
