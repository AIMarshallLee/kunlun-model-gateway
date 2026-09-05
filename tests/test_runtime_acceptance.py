import pytest


@pytest.mark.parametrize("url", [
    "sqlite:///test.db", "postgresql+psycopg://kunlun_runtime:inert@remote.example/kunlun_ci",
    "postgresql+psycopg://postgres:inert@127.0.0.1/kunlun_ci",
    "postgresql+psycopg://kunlun_runtime:inert@127.0.0.1/production",
    "postgresql+psycopg://kunlun_runtime:inert@127.0.0.1/kunlun_ci?host=remote.example",
])
def test_runtime_fixture_rejects_wrong_database_before_startup(url):
    from runtime_ci_fixture import fixture_database_url
    with pytest.raises(ValueError, match="disposable"):
        fixture_database_url({"KUNLUN_RUNTIME_DATABASE_URL": url,
                              "KUNLUN_CI_ISOLATED_DATABASE": "kunlun-ci-disposable"})


def test_runtime_fixture_requires_explicit_disposable_acknowledgement():
    from runtime_ci_fixture import fixture_database_url
    with pytest.raises(ValueError, match="disposable"):
        fixture_database_url({"KUNLUN_RUNTIME_DATABASE_URL":
            "postgresql+psycopg://kunlun_runtime:inert@127.0.0.1:55440/kunlun_ci"})


def test_runtime_fixture_accepts_only_runtime_local_connection():
    from runtime_ci_fixture import fixture_database_url
    result = fixture_database_url({"KUNLUN_RUNTIME_DATABASE_URL":
        "postgresql+psycopg://kunlun_runtime:inert@127.0.0.1:55440/kunlun_ci",
        "KUNLUN_CI_ISOLATED_DATABASE": "kunlun-ci-disposable"})
    assert result.port == 55440 and result.username == "kunlun_runtime"


def test_latency_report_is_measured_not_a_claimed_sla():
    from scripts.verify_runtime_postgres import latency_summary
    assert latency_summary([0.01, 0.04, 0.02, 0.03]) == {
        "count": 4, "p50_ms": 20.0, "p95_ms": 40.0, "max_ms": 40.0}
    with pytest.raises(ValueError):
        latency_summary([])


@pytest.mark.parametrize("crash", [False, True])
def test_only_the_owned_child_handle_is_stopped(crash):
    from scripts.verify_runtime_postgres import stop_child
    events = []
    class Child:
        def poll(self):
            return None
        def terminate(self):
            events.append("terminate")
        def kill(self):
            events.append("kill")
        def wait(self, timeout):
            events.append(("wait", timeout))
    stop_child(Child(), crash=crash)
    assert events == ["kill" if crash else "terminate", ("wait", 10)]


def test_finished_child_is_not_signalled_again():
    from scripts.verify_runtime_postgres import stop_child
    class Finished:
        def poll(self):
            return 0
        def terminate(self):
            pytest.fail("signalled a finished child")
        kill = terminate
    stop_child(Finished())
