"""Metrics tests."""
from src.observability.metrics import Metrics


def test_empty_snapshot():
    m = Metrics()
    s = m.snapshot()
    assert s["dispatch_total"] == 0
    assert s["avg_latency_ms"] == 0
    assert s["failure_rate"] == 0


def test_record_and_snapshot():
    m = Metrics()
    m.record_dispatch(True, 100.0)
    m.record_dispatch(False, 200.0)
    m.record_dispatch(True, 300.0)
    s = m.snapshot()
    assert s["dispatch_total"] == 3
    assert s["success"] == 2
    assert s["failure"] == 1
    assert s["avg_latency_ms"] == 200.0
    assert s["failure_rate"] == 0.333


def test_reset():
    m = Metrics()
    m.record_dispatch(True, 50)
    m.reset()
    assert m.snapshot()["dispatch_total"] == 0
