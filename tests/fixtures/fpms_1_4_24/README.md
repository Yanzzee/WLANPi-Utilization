# FPMS Trixie regression fixtures

Unmodified source snapshots from WLAN-Pi/wlanpi-fpms commit
`6ec8d92` (FPMS 1.4.24-1 shipped with WLAN Pi OS 26.10 DeadEye):

- `fpms.py`: `fpms/fpms.py`
- `buttons.py`: `fpms/modules/nav/buttons.py`

Source: https://github.com/WLAN-Pi/wlanpi-fpms/tree/6ec8d92

These fixtures exercise installation against the upstream layout without
importing GPIO, D-Bus, or display dependencies. The upstream BSD license is
retained in `LICENSE`. Keep the snapshots unchanged; test malformed layouts
using temporary copies.
