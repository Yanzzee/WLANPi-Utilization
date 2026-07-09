# WLANPi Beacon Live

This project builds a Python live 802.11 beacon and channel-utilization analyzer for Raspberry Pi / WLAN Pi hardware.

## Design rules

- Keep hardware-specific code isolated.
- Most code must be testable without WLAN Pi hardware.
- Do not use pandas in the live capture path.
- Prefer dataclasses, pure functions, and small modules.
- Use pytest for all parser and aggregation behavior.
- Treat AP QBSS channel utilization and local survey channel utilization as separate metrics.
- Treat QBSS station count and observed client count as separate metrics.
- Do not call observed clients "associated station count."

## Important fields

TShark beacon fields:
- frame.time_epoch
- wlan.ssid
- wlan.bssid
- wlan.qbss.cu
- wlan.qbss.scount
- wlan.qbss.adc

Local survey source:
- iw dev <iface> survey dump

## Development targets

- Laptop replay mode must work without Wi-Fi hardware.
- Pi live mode can require sudo, tshark, iw, and a monitor-mode adapter.
- The first UI target is terminal output.
- WLAN Pi display/menu integration comes later.