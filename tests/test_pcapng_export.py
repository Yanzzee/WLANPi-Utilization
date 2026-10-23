"""Offline export publication and independent TShark/analyzer fidelity checks."""
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.live import available_tshark_fields, build_tshark_stream_command, select_tshark_frame_fields
from beacon_live.parser import parse_tshark_live_record
from beacon_live.pcapng import BeaconFilter, PcapngError, iter_blocks
from scripts import export_beacons
from test_pcapng import AP, CLIENT, GROUP, block, section, interface, packet, radiotap, wlan_frame, option

TSHARK = os.environ.get("WLANPI_TEST_TSHARK") or shutil.which("tshark")
ROOT = Path(__file__).resolve().parents[1]


def test_offline_output_is_closed_and_published_without_replacing_files(tmp_path):
    source, destination = tmp_path / "source.pcapng", tmp_path / "beacons.pcapng"
    data = section() + interface() + packet() + packet(radiotap(wlan_frame(0x08)))
    source.write_bytes(data)
    stats = export_beacons.export_file(source, destination)
    assert stats.beacons == 1 and stats.non_beacons == 1
    assert len(destination.read_bytes()) == stats.selected_bytes
    assert destination.stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == data
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        export_beacons.export_file(source, destination)
    with pytest.raises(FileExistsError):
        export_beacons.export_file(source, source)
    assert destination.read_bytes() == before and source.read_bytes() == data
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("failure", ["malformed", "disk_full", "publish_race"])
def test_failed_export_leaves_no_owned_partial_or_false_completed_output(tmp_path, monkeypatch, failure):
    source, destination = tmp_path / "source.pcapng", tmp_path / "beacons.pcapng"
    data = section() + interface() + packet()
    source.write_bytes(data[:-1] if failure == "malformed" else data)
    if failure == "disk_full":
        def fail(*args, **kwargs):
            raise OSError(errno.ENOSPC, "fixture disk full")
        monkeypatch.setattr(export_beacons, "write_beacons", fail)
    elif failure == "publish_race":
        original = export_beacons.os.link
        def race(temporary, output):
            output.write_bytes(b"another writer owns this path")
            original(temporary, output)
        monkeypatch.setattr(export_beacons.os, "link", race)
    with pytest.raises((PcapngError, OSError)):
        export_beacons.export_file(source, destination)
    assert destination.exists() is (failure == "publish_race")
    if destination.exists():
        assert destination.read_bytes() == b"another writer owns this path"
    assert not list(tmp_path.glob("*.partial"))


def test_offline_cli_reports_counts_and_invalid_input_without_claiming_success(tmp_path):
    source, destination = tmp_path / "source.pcapng", tmp_path / "beacons.pcapng"
    source.write_bytes(section() + interface() + packet())
    command = [sys.executable, str(ROOT / "scripts/export_beacons.py"), str(source), str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["beacons"] == 1
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "already exists" in result.stderr
    assert not result.stdout


@pytest.mark.skipif(not TSHARK, reason="Optional real TShark pcapng export check")
@pytest.mark.parametrize("endian", ["<", ">"])
def test_tshark_reads_export_with_same_times_interface_ids_and_radiotap_as_source(tmp_path, endian):
    import struct
    ids = interface(endian, linktype=1) + interface(endian, options=
        option(9, b"\x09", endian) + option(14, struct.pack(endian + "q", -123), endian))
    frames = packet(endian=endian, iface=1) + packet(radiotap(wlan_frame(0x08)), endian=endian, iface=1)
    frames += packet(endian=endian, iface=1, ticks=1700000000987654321, options=option(1, b"original beacon", endian))
    source, destination = tmp_path / "source.pcapng", tmp_path / "beacons.pcapng"
    source.write_bytes(section(endian, length=len(ids + frames)) + ids + frames)
    stats = export_beacons.export_file(source, destination)
    def decode(path, filtered=False):
        command = [TSHARK, "-r", str(path)]
        if filtered:
            command += ["-Y", "wlan.fc.type_subtype == 8"]
        command += ["-T", "fields"]
        for name in ("frame.time_epoch", "frame.interface_id", "radiotap.length", "wlan_radio.signal_dbm", "wlan.ssid"):
            command += ["-e", name]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()
    original, exported = decode(source, True), decode(destination)
    assert original == exported and len(exported) == stats.beacons == 2
    assert exported[0].startswith("1699999877.123456789\t")
    assert "\t15\t-47\t" in exported[0]


@pytest.mark.skipif(not TSHARK, reason="Optional real analyzer/export fan-out check")
def test_export_branch_preserves_every_source_frame_for_existing_analyzer(tmp_path):
    frames = [
        radiotap(),
        radiotap(wlan_frame(0x0908, ra=AP, ta=CLIENT)),
        radiotap(wlan_frame(0x0908, ra=AP, ta=CLIENT)),
        radiotap(wlan_frame(0x0108, ra=AP, ta=CLIENT)),
        radiotap(wlan_frame(0x0808, ra=GROUP)),
        radiotap(wlan_frame(0x0840, ra=GROUP, ta=CLIENT)),
        radiotap(wlan_frame(0x00D4, ra=CLIENT)),
        radiotap(wlan_frame(0x08E0, ra=CLIENT)),
        radiotap(wlan_frame(0x08B0, ra=CLIENT)),
    ]
    raw = section() + interface()
    for second in range(3):
        for index, payload in enumerate(frames):
            ticks = (1700000000 + second) * 1000000 + (index + 1) * 100000
            raw += packet(payload, ticks=ticks)
    decoder_input, exported = io.BytesIO(), io.BytesIO()
    selector = BeaconFilter()
    for item in iter_blocks(io.BytesIO(raw)):
        # Simulate the future source fan-out: original bytes to analysis, only
        # selected blocks to export. This is not a deployed live capture hook.
        decoder_input.write(item.raw)
        selected = selector.select(item)
        if selected is not None:
            exported.write(selected)
    assert decoder_input.getvalue() == raw
    fields = select_tshark_frame_fields(available_tshark_fields(tshark=TSHARK))
    command = build_tshark_stream_command(field_names=fields)
    command[0] = TSHARK
    def analyze(data):
        result = subprocess.run(command, input=data, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        analyzer = Analyzer(streaming=True, target_primary_frequency_mhz=2412)
        analyzer.set_history_start_second(1700000001)
        count = 0
        for row in result.stdout.decode().splitlines():
            for frame in parse_tshark_live_record(row, field_names=fields, ssid_encoding="hex"):
                analyzer.ingest(frame, publish_snapshot=False)
                count += 1
        analyzer.publish_capture_complete(None)
        return analyzer.snapshot, count
    baseline, before = analyze(raw)
    with_export, after = analyze(decoder_input.getvalue())
    beacon_only, beacon_count = analyze(exported.getvalue())
    assert with_export == baseline and before == after == 27
    assert selector.stats.beacons == beacon_count == 3
    assert baseline.current.retry_eligible_frame_count == 4
    assert baseline.current.retry_frame_count == 3 and baseline.current.retry_percent == 75
    assert baseline.current.unique_client_mac_count == 1
    assert baseline.current.qbss_station_count_sum == 12
    assert beacon_only.current.retry_eligible_frame_count == 0
