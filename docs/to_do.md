Features & changes

1. CU screen - review the BSSID that is shown/used for data - this is currently the strongest signal/latest.
    don't change if the signal is about the same <3dB difference, or if thre is another way to tell same radio
    suspected same radio - use RSSI, then prefer the BSSID with most stations, still use latest of that BSSID
2. additional graph/screen - show admission capacity instead of channel utilization
    need to consider how to select the BSSID, or show when the reported one has changed
    use strongest signal and then BSSID with most stations
3. additional screen - count the number of BSSIDs on channel, and number of them reporting QBSS IEs
    identify the number of discreet radios (identify & deduplicate SSIDs on same radio)
    identify number of unique APs if possible
    list the BSSIDs by station count (separate screen?)
4. additional graph/screen - total station count
    swap values for CU and SUM in display
    list the SSID/BSSID with highest station count
5. additional graph/screen - % retries
    retries as a percentage of total frames received
    include beacon rate of strongest signal as a percentage of expected
    list the SSID/BSSID with highest retry rates
6. Navigation - enable scrolling between screens with up/down on control stick
    disable other buttons while the display application is running
7. Menu - add logging only start/stop without any display
    do any screens need to be a separate menu?
8. Documentation
    reduce README to user information only
    create appropriate documents in docs folder 
9. when logging, periodically check for free disk space and close if nearly full
    add message to end of log that the disk is full
    for long term logging, write separate files periodically