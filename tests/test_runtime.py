from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Event

import pytest

from beacon_live.models import MetricsSnapshot
from beacon_live.runtime import (
    LiveSessionConfig,
    ManagedRuntime,
    SessionConflictError,
    SessionPhase,
)
from beacon_live.screen_manager import ScreenManager


def sample(second, **changes):
    return replace(
        MetricsSnapshot.empty().current, second=second, sample_available=True, **changes
    )


def test_consumers_attach_while_idle_without_starting_capture():
    runtime = ManagedRuntime(runner=lambda **kwargs: pytest.fail("capture started"))
    first, second = runtime.subscribe(), runtime.subscribe()
    assert first.read() is second.read()
    assert first.wait(0, timeout=0).phase is SessionPhase.IDLE
    first.close()
    assert second.read().session_id is None
    assert runtime.stop("missing") is False


def test_real_capture_owner_rejects_http_worker_thread_before_startup():
    runtime = ManagedRuntime()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, LiveSessionConfig())
        with pytest.raises(RuntimeError, match="capture owner's main thread"):
            future.result(timeout=2)
    assert runtime.read().phase is SessionPhase.IDLE


def test_multiple_late_and_slow_consumers_share_original_metrics_and_history():
    rows = (
        sample(1000, retry_percent=0.25, local_cu_percent=None),
        sample(1001, retry_percent=0.0, selected_qbss_cu_percent=None),
    )
    snapshot = replace(MetricsSnapshot.empty(), current=rows[-1], history=rows)
    calls = []

    def runner(**kwargs):
        calls.append(kwargs)
        dashboard = kwargs["_dashboard"]
        assert runtime.read().phase is SessionPhase.STARTING
        dashboard.set_capture_started()
        dashboard.set_collecting(False)
        dashboard.publish_stats(rows)
        dashboard.refresh(snapshot)
        dashboard.set_capture_width("20 MHz")
        dashboard.set_logging_status(active=True, paths=(Path("stats.csv"),))
        dashboard.set_notice("survey unavailable")
        late = runtime.subscribe()
        assert early.read() is slow.read() is late.read()
        assert late.read().snapshot is snapshot
        assert late.read().completed_stats[0] is rows[0]
        assert slow.wait(0, timeout=0).revision > 1
        manager = ScreenManager(snapshot=late.read().snapshot, debounce_seconds=0)
        for index in range(manager.screen_count * 2):
            manager.set_active_index(index)
            manager.view
        assert manager.snapshot.history is snapshot.history
        late.close()
        early.close()
        slow.close()
        assert dashboard.poll_input() is False
        assert runtime.read().phase is SessionPhase.RUNNING
        return 0

    runtime = ManagedRuntime(runner=runner)
    early, slow = runtime.subscribe(), runtime.subscribe()
    assert runtime.run(LiveSessionConfig()) == 0
    final = runtime.subscribe().read()
    assert len(calls) == 1
    assert calls[0]["capture_backend"] == "direct"
    assert final.snapshot is snapshot
    assert final.completed_stats == rows
    assert final.phase is SessionPhase.STOPPED
    assert final.exit_code == 0
    assert final.logging.active is False
    assert final.logging.paths == (Path("stats.csv"),)
    assert final.capture_width == "20 MHz"
    assert final.notice == "survey unavailable"
    assert final.collecting is False
    with pytest.raises(FrozenInstanceError):
        final.revision = 0


def test_stop_during_startup_remains_stopping_until_owner_cleanup():
    def runner(**kwargs):
        dashboard = kwargs["_dashboard"]
        session_id = runtime.read().session_id
        assert runtime.stop(session_id)
        revision = runtime.read().revision
        assert runtime.stop(session_id)
        assert runtime.read().revision == revision
        dashboard.set_capture_started()
        assert runtime.read().phase is SessionPhase.STOPPING
        assert dashboard.poll_input()
        with pytest.raises(SessionConflictError):
            runtime.run(LiveSessionConfig(channel="1", band="2.4"))
        return 0

    runtime = ManagedRuntime(runner=runner)
    assert runtime.run(LiveSessionConfig()) == 0
    assert runtime.read().phase is SessionPhase.STOPPED


def test_stop_is_nonblocking_and_does_not_allow_replacement_before_cleanup():
    started, stop_seen, release_cleanup = Event(), Event(), Event()

    def runner(**kwargs):
        dashboard = kwargs["_dashboard"]
        dashboard.set_capture_started()
        started.set()
        assert command_sent.wait(2)
        assert dashboard.poll_input()
        stop_seen.set()
        assert release_cleanup.wait(2)
        return 0

    runtime = ManagedRuntime(runner=runner)
    command_sent = Event()
    # Only the hardware-free injected runner uses a thread in these tests.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run, LiveSessionConfig())
        try:
            assert started.wait(2)
            assert not runtime.stop("stale-session")
            assert runtime.stop(runtime.read().session_id)
            command_sent.set()
            assert stop_seen.wait(2)
            assert runtime.read().phase is SessionPhase.STOPPING
            with pytest.raises(SessionConflictError):
                runtime.run(LiveSessionConfig())
        finally:
            command_sent.set()
            release_cleanup.set()
        assert future.result(timeout=2) == 0
    assert runtime.read().phase is SessionPhase.STOPPED


@pytest.mark.parametrize("started", [False, True])
def test_startup_and_runtime_failures_retain_state_and_can_restart(started):
    snapshot = replace(MetricsSnapshot.empty(), history=(sample(1001),))
    bridges = []

    def runner(**kwargs):
        dashboard = kwargs["_dashboard"]
        bridges.append(dashboard)
        if started:
            dashboard.set_capture_started()
        dashboard.refresh(snapshot)
        raise ValueError("decoder unavailable")

    runtime = ManagedRuntime(runner=runner)
    with pytest.raises(ValueError, match="decoder unavailable"):
        runtime.run(LiveSessionConfig())
    failed = runtime.read()
    assert failed.phase is SessionPhase.FAILED
    assert failed.error == "decoder unavailable"
    assert failed.snapshot is snapshot
    assert not runtime.stop(failed.session_id)
    with pytest.raises(ValueError):
        runtime.run(LiveSessionConfig())
    restarted = runtime.read()
    assert restarted.session_id != failed.session_id
    assert restarted.revision > failed.revision
    bridges[0].refresh(MetricsSnapshot.empty())
    assert runtime.read() is restarted


@pytest.mark.parametrize("result", [0, 2])
def test_eof_and_nonzero_exit_are_distinct(result):
    runtime = ManagedRuntime(runner=lambda **kwargs: result)
    assert runtime.run(LiveSessionConfig(capture_backend="core")) == result
    publication = runtime.read()
    assert publication.phase is (SessionPhase.STOPPED if result == 0 else SessionPhase.FAILED)
    assert publication.exit_code == result
    assert (publication.error is None) == (result == 0)


def test_interrupt_before_live_loop_records_stopped_state():
    def runner(**kwargs):
        raise KeyboardInterrupt

    runtime = ManagedRuntime(runner=runner)
    with pytest.raises(KeyboardInterrupt):
        runtime.run(LiveSessionConfig())
    assert runtime.read().phase is SessionPhase.STOPPED
    assert runtime.read().exit_code == 130


def test_explicit_restart_clears_history_and_rejects_old_controls_and_publications():
    consumer = None
    first_session = None
    first_bridge = None

    def runner(**kwargs):
        nonlocal first_session, first_bridge
        dashboard = kwargs["_dashboard"]
        if first_session is None:
            first_session = runtime.read().session_id
            first_bridge = dashboard
            dashboard.refresh(replace(MetricsSnapshot.empty(), history=(sample(1001),)))
            dashboard.publish_stats((sample(1001),))
        else:
            assert consumer.read().snapshot.history == ()
            assert consumer.read().completed_stats == ()
            assert not runtime.stop(first_session)
            before = runtime.read()
            first_bridge.refresh(MetricsSnapshot.empty())
            first_bridge.publish_stats((sample(2000),))
            first_bridge.set_capture_started()
            assert runtime.read() is before
            assert not dashboard.poll_input()
        return 0

    runtime = ManagedRuntime(runner=runner)
    consumer = runtime.subscribe()
    runtime.run(LiveSessionConfig())
    assert consumer.read().snapshot.history
    runtime.run(LiveSessionConfig(band="2.4", channel="1"))
    assert consumer.read().session_id != first_session


def test_completed_statistics_retention_is_bounded_by_capture_seconds():
    rows = tuple(sample(second) for second in range(300))

    def runner(**kwargs):
        dashboard = kwargs["_dashboard"]
        dashboard.publish_stats(rows)
        assert runtime.read().completed_stats == rows[-120:]
        dashboard.publish_stats((sample(500, retry_percent=None),))
        assert runtime.read().completed_stats == (sample(500, retry_percent=None),)
        return 0

    runtime = ManagedRuntime(runner=runner)
    runtime.run(LiveSessionConfig())


def test_wait_sees_new_publication_and_close_wakes_waiter():
    runtime = ManagedRuntime(runner=lambda **kwargs: 0)
    consumer = runtime.subscribe()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(consumer.wait, 0, timeout=2)
        runtime.run(LiveSessionConfig())
        assert future.result(timeout=2).revision > 0
        publication = consumer.read()
        future = executor.submit(consumer.wait, publication.revision, timeout=2)
        consumer.close()
        with pytest.raises(RuntimeError, match="Consumer is closed"):
            future.result(timeout=2)
    assert runtime.read() is publication
