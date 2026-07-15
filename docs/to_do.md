Features & changes
1. additional graph/screen - show admission capacity instead of channel utilization
    need to consider how to select the BSSID, or show when the reported one has changed
    use strongest signal and then BSSID with most stations
2. CU screen - review the BSSID that is shown/used for data - this is currently the strongest signal/latest.
    don't change if the signal is about the same <3dB difference
    then prefer the one with more stations
3. additional screen - count the number of BSSIDs on channel, and number of them reporting QBSS IEs
    identify the number of discreet radios (identify & deduplicate SSIDs on same radio)
    list the BSSIDs by station count (separate screen?)
4. additional graph/screen - total station count
    swap values for CU and SUM in display
5. Navigation - enable scrolling between screens with up/down on control stick
    disable other buttons while the display application is running
6. Menu - add loggin only start/stop
7. Documentation
    reduce README to user information only
    create appropriate documents in docs folder
8. additional graph/screen - % retries
    retries as a percentage of total frames received