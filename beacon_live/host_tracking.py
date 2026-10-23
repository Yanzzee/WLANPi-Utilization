"""Bounded host details derived alongside the existing wireless-client count."""
from dataclasses import dataclass, replace
from heapq import nlargest

from beacon_live.models import HostState

MAX_HOST_DETAILS = 1024


@dataclass(slots=True)
class HostSummary:
    bssid: str
    last_activity: float
    last_tx_timestamp: float | None = None
    last_tx_rssi_dbm: int | None = None
    vendor: str | None = None
    vendor_timestamp: float = float("-inf")
    tx_frames: int = 0
    rx_frames: int = 0
    tx_eligible: int = 0
    tx_retries: int = 0

    def observe(self, mac, frame, bssid, retry_eligible):
        if frame.timestamp >= self.last_activity:
            self.last_activity, self.bssid = frame.timestamp, bssid
        transmitted = mac == frame.transmitter_address
        self.tx_frames += transmitted
        self.rx_frames += mac == frame.receiver_address
        if transmitted:
            self.tx_eligible += retry_eligible
            self.tx_retries += retry_eligible and frame.retry_flag is True
            if self.last_tx_timestamp is None or frame.timestamp >= self.last_tx_timestamp:
                self.last_tx_timestamp = frame.timestamp
                self.last_tx_rssi_dbm = frame.rssi_dbm
        vendor = frame.transmitter_vendor if transmitted else frame.receiver_vendor
        if vendor and frame.timestamp >= self.vendor_timestamp:
            self.vendor, self.vendor_timestamp = vendor, frame.timestamp

    def merge(self, source):
        self.tx_frames += source.tx_frames
        self.rx_frames += source.rx_frames
        self.tx_eligible += source.tx_eligible
        self.tx_retries += source.tx_retries
        if source.last_activity >= self.last_activity:
            self.last_activity, self.bssid = source.last_activity, source.bssid
        if source.last_tx_timestamp is not None and (
                self.last_tx_timestamp is None or source.last_tx_timestamp >= self.last_tx_timestamp):
            self.last_tx_timestamp, self.last_tx_rssi_dbm = source.last_tx_timestamp, source.last_tx_rssi_dbm
        if source.vendor and source.vendor_timestamp >= self.vendor_timestamp:
            self.vendor, self.vendor_timestamp = source.vendor, source.vendor_timestamp


def observe_host(hosts, mac, frame, bssid, retry_eligible):
    """Return whether details were omitted; never alter the client-count set."""
    host = hosts.get(mac)
    if host is None:
        if len(hosts) >= MAX_HOST_DETAILS:
            return True
        host = hosts[mac] = HostSummary(bssid, frame.timestamp)
    host.observe(mac, frame, bssid, retry_eligible)
    return False


def merge_hosts(projections):
    # At most 120 bounded per-second maps on the standard live window. Select
    # recent identities first, then merge only their counters, never raw frames.
    latest = {}
    limited = any(projection.hosts_limited for projection in projections)
    for projection in projections:
        for mac, host in projection.hosts.items():
            latest[mac] = max(latest.get(mac, float("-inf")), host.last_activity)
    selected = set(nlargest(MAX_HOST_DETAILS, latest, key=lambda mac: (latest[mac], mac)))
    limited |= len(latest) > MAX_HOST_DETAILS
    merged = {}
    for projection in projections:
        for mac, host in projection.hosts.items():
            if mac in selected:
                if mac not in merged:
                    merged[mac] = replace(host)
                else:
                    merged[mac].merge(host)
    return merged, limited


def host_states(projection, bssids):
    by_address = {state.bssid.lower(): state for state in bssids}
    return tuple(HostState(
        mac=mac, vendor=None if int(mac[:2], 16) & 2 else summary.vendor,
        locally_administered=bool(int(mac[:2], 16) & 2),
        ssid=by_address[summary.bssid].ssid if summary.bssid in by_address else None,
        bssid=summary.bssid, last_activity=summary.last_activity,
        last_tx_timestamp=summary.last_tx_timestamp, last_tx_rssi_dbm=summary.last_tx_rssi_dbm,
        tx_frames=summary.tx_frames, rx_frames=summary.rx_frames,
        tx_retry_percent=summary.tx_retries / summary.tx_eligible * 100 if summary.tx_eligible else None,
    ) for mac, summary in sorted(projection.hosts.items(), key=lambda item: (-item[1].last_activity, item[0])))
