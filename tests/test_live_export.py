"""Same-source delivery, independently readable rotation and storage isolation."""
from dataclasses import replace
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
from types import SimpleNamespace

import pytest

from beacon_live.beacon_export import BeaconExport, ExportLimits, RotatingBeaconWriter
from beacon_live.capture_target import CaptureTarget
from beacon_live.packet_source import build_dumpcap_command, forward_packets
from beacon_live.pcapng import BeaconFilter, ENHANCED_PACKET, iter_blocks
from test_pcapng import section, interface, packet, radiotap, wlan_frame, option


def blocks(raw):
    return tuple(iter_blocks(io.BytesIO(raw)))


def offer(exporter, raw):
    for item in blocks(raw):
        exporter.offer(item)


def until(exporter, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = exporter.read()
        if predicate(status):
            return status
        time.sleep(0.01)
    pytest.fail(f"Export condition timed out: {status}")


def limits(**changes):
    return replace(ExportLimits(min_free_bytes=0), **changes)


class SlowWriter(RotatingBeaconWriter):
    def write(self, context, packet):
        time.sleep(10)
        super().write(context, packet)


class FullDiskWriter(RotatingBeaconWriter):
    def _write(self, data):
        raise OSError(28, "fixture disk full")


@pytest.mark.parametrize("endian", ["<", ">"])
def test_rotated_segments_keep_original_packets_and_complete_interface_context(tmp_path, endian):
    context = (section(endian), interface(endian, linktype=1), interface(endian, options=
        option(9, b"\x09", endian) + option(14, struct.pack(endian + "q", -123), endian)))
    packets = [packet(endian=endian, iface=1, ticks=1700000000000000000 + i,
                      options=option(1, b"unchanged", endian)) for i in range(4)]
    size = sum(map(len, context)) + len(packets[0])
    writer = RotatingBeaconWriter(tmp_path, limits(segment_bytes=size))
    for item in packets:
        writer.write(context, item)
    writer.close_segment()
    assert writer.exported_beacons == 4 and len(writer.closed) == 4
    for index, item in enumerate(writer.closed):
        assert item.path.read_bytes() == b"".join(context) + packets[index]
        assert item.path.stat().st_mode & 0o777 == 0o600
        selector = BeaconFilter()
        for block in blocks(item.path.read_bytes()):
            selector.select(block)
        assert selector.stats.beacons == 1
        assert selector.interfaces[1].timestamp_resolution == 9
        assert selector.interfaces[1].timestamp_offset_seconds == -123
    assert not list(tmp_path.glob("*.partial"))


def test_time_and_interface_changes_rotate_without_rewriting_packet_ids(tmp_path):
    now = [0.0]
    writer = RotatingBeaconWriter(tmp_path, limits(rotation_seconds=5), clock=lambda: now[0])
    context = (section(), interface())
    writer.write(context, packet())
    now[0] = 5
    writer.maintain()
    assert writer.stream is None and len(writer.closed) == 1
    writer.write(context, packet())
    new_context = context + (interface(linktype=105),)
    writer.write(new_context, packet(wlan_frame(), iface=1))
    writer.close_segment()
    assert len(writer.closed) == 3
    assert writer.closed[-1].path.read_bytes() == b"".join(new_context) + packet(wlan_frame(), iface=1)


def test_retention_survives_new_writer_and_cleans_only_owned_stale_partials(tmp_path):
    context = (section(), interface())
    size = sum(map(len, context)) + len(packet())
    policy = limits(segment_bytes=size, retained_bytes=size * 2, retained_files=2)
    unrelated = tmp_path / "keep.pcapng"
    unrelated.write_bytes(b"unrelated")
    partial = tmp_path / ("utilization_beacons_" + "b" * 32 + "_000001.pcapng.partial")
    partial.write_bytes(b"partial")
    link = tmp_path / ("utilization_beacons_" + "c" * 32 + "_000001.pcapng")
    link.symlink_to(unrelated)
    first = RotatingBeaconWriter(tmp_path, policy)
    for _ in range(3):
        first.write(context, packet())
    first.close_segment()
    assert len(first.retained) == 2 and first.deleted_segments == 1
    first.close()
    second = RotatingBeaconWriter(tmp_path, policy)
    second.write(context, packet())
    second.close_segment()
    assert len(second.retained) == 2 and second.deleted_segments == 1
    assert second.retained_bytes <= size * 2
    assert unrelated.read_bytes() == b"unrelated" and link.is_symlink() and not partial.exists()


def test_low_disk_and_oversized_record_refuse_output_without_deleting_unrelated_files(tmp_path):
    context = (section(), interface())
    writer = RotatingBeaconWriter(tmp_path, limits(min_free_bytes=100),
                                  disk_usage=lambda _: SimpleNamespace(free=100))
    with pytest.raises(OSError, match="low disk"):
        writer.write(context, packet())
    assert not list(tmp_path.glob("*.pcapng*"))
    writer.close()
    writer = RotatingBeaconWriter(tmp_path, limits(segment_bytes=100))
    with pytest.raises(OSError, match="segment size"):
        writer.write(context, packet())
    assert not list(tmp_path.glob("*.pcapng*"))


def test_original_stream_delivery_and_live_writer_publish_only_beacons(tmp_path):
    raw = section() + interface(options=option(9, b"\x09"))
    beacons = [packet(ticks=1700000000000000000 + i) for i in range(4)]
    raw += b"".join(beacon + packet(radiotap(wlan_frame(0x0908))) for beacon in beacons)
    exporter = BeaconExport(tmp_path, enabled=True, limits=limits(segment_bytes=220))
    original = io.BytesIO()
    try:
        forward_packets(io.BytesIO(raw), original, exporter)
    finally:
        exporter.close()
    state = exporter.read()
    assert original.getvalue() == raw
    assert state.exported_beacons == 4 and state.skipped_beacons == 0 and not state.error
    assert state.selection.non_beacons == 4
    exported = [item.raw for log in exporter.closed_logs for item in blocks(log.path.read_bytes())
                if item.kind == ENHANCED_PACKET]
    assert exported == beacons
    tshark = os.environ.get("WLANPI_TEST_TSHARK") or shutil.which("tshark")
    if tshark:
        for log in exporter.closed_logs:
            result = subprocess.run([tshark, "-r", str(log.path), "-T", "fields", "-e", "wlan.fc.type_subtype"],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert set(result.stdout.splitlines()) == {"0x0008"}


def test_export_toggle_reuses_source_context_and_closes_segments(tmp_path):
    exporter = BeaconExport(tmp_path, limits=limits())
    try:
        offer(exporter, section() + interface() + packet())
        assert not list(tmp_path.glob("*.pcapng"))
        exporter.set_enabled(True)
        exporter.offer(blocks(section() + interface() + packet())[-1])
        until(exporter, lambda state: state.active_beacons == 1)
        exporter.set_enabled(False)
        until(exporter, lambda state: not state.active and state.exported_beacons == 1)
        exporter.set_enabled(True)
        exporter.offer(blocks(section() + interface() + packet())[-1])
        until(exporter, lambda state: state.active_beacons == 1)
    finally:
        exporter.close()
    assert exporter.read().exported_beacons == 2
    assert len(exporter.closed_logs) == 2


def test_slow_disk_queue_overflow_does_not_hold_decoder_and_stop_is_bounded(tmp_path):
    exporter = BeaconExport(tmp_path, limits=limits(queue_records=1, queue_bytes=1000), enabled=True,
                            writer_factory=SlowWriter)
    raw = section() + interface() + packet() * 100
    output = io.BytesIO()
    started = time.monotonic()
    forward_packets(io.BytesIO(raw), output, exporter)
    assert time.monotonic() - started < 1
    assert output.getvalue() == raw
    assert exporter.read().queue_full > 0 and exporter.read().queued_bytes <= 1000
    started = time.monotonic()
    exporter.close(timeout=0.2)
    assert time.monotonic() - started < 1
    assert exporter.read().incomplete and "did not stop" in exporter.read().error


def test_disk_full_disables_only_export_and_accounts_for_missing_packets(tmp_path):
    exporter = BeaconExport(tmp_path, limits=limits(), enabled=True, writer_factory=FullDiskWriter)
    try:
        raw = section() + interface() + packet() + packet(radiotap(wlan_frame(0x08)))
        output = io.BytesIO()
        forward_packets(io.BytesIO(raw), output, exporter)
        state = until(exporter, lambda state: state.error is not None)
        assert not state.active and state.incomplete
        assert output.getvalue() == raw
    finally:
        exporter.close()
    assert exporter.read().exported_beacons == 0
    assert exporter.read().skipped_beacons == 1
    assert not list(tmp_path.glob("*.pcapng*"))


def test_bad_export_options_disable_branch_without_truncating_decoder_stream(tmp_path):
    exporter = BeaconExport(tmp_path, limits=limits(), enabled=True)
    raw = section(options=option(19372, b"abcd")) + interface() + packet()
    output = io.BytesIO()
    try:
        forward_packets(io.BytesIO(raw), output, exporter)
    finally:
        exporter.close()
    assert output.getvalue() == raw
    assert "Non-copyable" in exporter.read().error
    assert not list(tmp_path.glob("*.pcapng"))


def test_dumpcap_opens_one_full_packet_source_in_requested_namespace():
    command = build_dumpcap_command(CaptureTarget("mon0", "lab"))
    assert command == ["ip", "netns", "exec", "lab", "dumpcap", "-q", "-i", "mon0", "-B", "16", "-s", "0", "-n", "-w", "-"]
    assert "-f" not in command


def test_real_direct_facade_retains_mixed_frame_analyzer_results_with_export_on_off(tmp_path):
    import sys
    from beacon_live.analyzer import Analyzer
    from beacon_live.live import available_tshark_fields, build_tshark_stream_command, select_tshark_frame_fields
    from beacon_live.packet_source import DirectCaptureProcess
    from beacon_live.parser import parse_tshark_live_record
    from test_pcapng import AP, CLIENT, GROUP
    tshark = os.environ.get("WLANPI_TEST_TSHARK") or shutil.which("tshark")
    if not tshark:
        pytest.skip("Real TShark is required for source/analyzer fidelity")
    frames = [radiotap(), radiotap(wlan_frame(0x0908, ra=AP, ta=CLIENT)),
              radiotap(wlan_frame(0x0908, ra=AP, ta=CLIENT)),
              radiotap(wlan_frame(0x0108, ra=AP, ta=CLIENT)),
              radiotap(wlan_frame(0x0808, ra=GROUP)),
              radiotap(wlan_frame(0x0840, ra=GROUP, ta=CLIENT)),
              radiotap(wlan_frame(0x00D4, ra=CLIENT)),
              radiotap(wlan_frame(0x08E0, ra=CLIENT)),
              radiotap(wlan_frame(0x08B0, ra=CLIENT))]
    raw = section() + interface()
    for second in range(3):
        for index, frame in enumerate(frames):
            raw += packet(frame, ticks=(1700000000 + second) * 1000000 + (index + 1) * 100000)
    source_path = tmp_path / "source.pcapng"
    source_path.write_bytes(raw)
    fields = select_tshark_frame_fields(available_tshark_fields(tshark=tshark))
    def launch(command, **options):
        if command[0] == "dumpcap":
            command = [sys.executable, "-c", "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())", str(source_path)]
        else:
            assert "-i" not in command
            command[0] = tshark
        return subprocess.Popen(command, **options)
    results = []
    for enabled in (False, True):
        exporter = BeaconExport(tmp_path / str(enabled), enabled=enabled, limits=limits(segment_bytes=240))
        process = DirectCaptureProcess("fixture0", field_names=fields, exporter=exporter, popen=launch)
        try:
            lines = process.stdout.read().decode()
            assert process.decoder.wait(timeout=5) == 0
            assert not process.capture_error
        finally:
            process.close()
            exporter.close()
        analyzer = Analyzer(streaming=True, target_primary_frequency_mhz=2412)
        analyzer.set_history_start_second(1700000001)
        count = 0
        for row in lines.splitlines():
            for frame in parse_tshark_live_record(row, field_names=fields, ssid_encoding="hex"):
                analyzer.ingest(frame, publish_snapshot=False)
                count += 1
        analyzer.publish_capture_complete(None)
        results.append((analyzer.snapshot, count))
        assert exporter.read().exported_beacons == (3 if enabled else 0)
        assert not exporter.read().error
    assert results[0] == results[1]
    snapshot, count = results[1]
    assert count == 27
    assert snapshot.current.retry_eligible_frame_count == 4
    assert snapshot.current.retry_frame_count == 3 and snapshot.current.retry_percent == 75
    assert snapshot.current.unique_client_mac_count == 1 and snapshot.current.qbss_station_count_sum == 12


def test_live_export_sections_and_changed_interface_context_are_independent(tmp_path):
    first = packet(iface=1)
    second = packet(wlan_frame(), endian=">", ticks=1234567)
    raw = section() + interface(linktype=1) + interface(options=option(9, b"\x09")) + first
    raw += section(">") + interface(">", linktype=105) + second
    exporter = BeaconExport(tmp_path, enabled=True, limits=limits())
    try:
        output = io.BytesIO()
        forward_packets(io.BytesIO(raw), output, exporter)
    finally:
        exporter.close()
    assert output.getvalue() == raw
    assert len(exporter.closed_logs) == 2
    assert [blocks(log.path.read_bytes())[-1].raw for log in exporter.closed_logs] == [first, second]
    for log in exporter.closed_logs:
        selector = BeaconFilter()
        for item in blocks(log.path.read_bytes()):
            selector.select(item)
        assert selector.stats.beacons == 1


def test_exclusive_writer_lease_prevents_cleanup_of_an_active_segment(tmp_path):
    writer = RotatingBeaconWriter(tmp_path, limits())
    writer.write((section(), interface()), packet())
    active = writer.path
    with pytest.raises(OSError, match="already has a writer"):
        RotatingBeaconWriter(tmp_path, limits())
    assert active.exists()
    writer.abort()
    assert not active.exists()
    replacement = RotatingBeaconWriter(tmp_path, limits())
    replacement.close()


@pytest.mark.parametrize("changes", [
    {"segment_bytes": 0}, {"retained_bytes": 1}, {"retained_files": 0},
    {"rotation_seconds": 0}, {"rotation_seconds": float("nan")},
    {"rotation_seconds": float("inf")}, {"min_free_bytes": -1},
    {"queue_records": 5000}, {"queue_bytes": 300 * 1024 * 1024},
])
def test_export_limits_reject_unbounded_or_invalid_policy(changes):
    with pytest.raises(ValueError, match="export limits"):
        limits(**changes).validate()


@pytest.mark.parametrize("disk_failure", [False, True])
def test_owner_live_hook_publishes_final_export_without_second_capture(monkeypatch, tmp_path, disk_failure):
    from contextlib import nullcontext
    from beacon_live import live
    from test_live import _prepare_one_interval_live_run, _FakeTsharkProcess
    from beacon_live.channel import RadioCapabilities
    clock = time.monotonic
    _prepare_one_interval_live_run(monkeypatch)
    monkeypatch.setattr(live.time, "monotonic", clock)
    monkeypatch.setattr(live, "RadioSession", lambda *args, **kwargs: nullcontext(SimpleNamespace(
        original_mode="managed", prepare=lambda: None)))
    monkeypatch.setattr(live, "available_tshark_fields", lambda: frozenset(live.TSHARK_FRAME_FIELD_NAMES))
    monkeypatch.setattr(live, "_read_radio_capabilities_safely", lambda target: RadioCapabilities.ht20_only())
    monkeypatch.setattr(live, "_read_actual_channel_safely", lambda target, requested: (requested, True))
    launches = []
    states = []
    notices = []
    class Source(_FakeTsharkProcess):
        def close(self):
            launches.append("closed")
    def start(target, *, field_names, exporter):
        launches.append(target)
        offer(exporter, section() + interface() + packet())
        return Source()
    monkeypatch.setattr("beacon_live.packet_source.DirectCaptureProcess", start)
    dashboard = live.NullDashboard()
    if disk_failure:
        factory = live.BeaconExport
        monkeypatch.setattr(live, "BeaconExport", lambda *args, **kwargs: factory(
            *args, **kwargs, writer_factory=FullDiskWriter))
        dashboard.set_notice = notices.append
    else:
        dashboard.set_beacon_export = lambda status, logs: states.append((status, logs))
    assert live.run_live(iface="wlan0", band="2.4", channel="1", export_directory=tmp_path,
                         initial_export_enabled=True, export_limits=limits(), _dashboard=dashboard) == 0
    assert launches == ["wlan0", "closed"]
    if disk_failure:
        assert any("fixture disk full" in message and "Live analysis continues" in message for message in notices)
        assert not list(tmp_path.glob("*.pcapng"))
    else:
        assert states[-1][0].exported_beacons == 1 and not states[-1][0].active
        assert states[-1][1][0].path.exists()


def test_unexpected_export_worker_death_does_not_stop_original_delivery(tmp_path):
    exporter = BeaconExport(tmp_path, enabled=True, limits=limits())
    exporter.worker.terminate()
    exporter.worker.join(timeout=2)
    raw = section() + interface() + packet() + packet(radiotap(wlan_frame(0x08)))
    output = io.BytesIO()
    try:
        forward_packets(io.BytesIO(raw), output, exporter)
        state = exporter.read()
        assert not state.active and state.incomplete and "exited unexpectedly" in state.error
        assert output.getvalue() == raw
    finally:
        exporter.close()
