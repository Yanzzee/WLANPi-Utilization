Features & changes

1. DONE     CU screen - review the BSSID that is shown/used for data - this is currently the strongest signal/latest.
    don't change if the signal is about the same <3dB difference, or if thre is another way to tell same radio
    suspected same radio - use RSSI, then prefer the BSSID with most stations, still use latest of that BSSID
2. DONE     additional graph/screen - show admission capacity instead of channel utilization
    need to consider how to select the BSSID, or show when the reported one has changed
    use strongest signal and then BSSID with most stations
3. DONE     additional screen - count the number of BSSIDs on channel, and number of them reporting QBSS IEs
    identify the number of discreet radios (identify & deduplicate SSIDs on same radio)
    identify number of unique APs if possible (don't do this, same as radio count)
    list the BSSIDs by station count (separate screen?)
    include the number (#) of BSSIDs that are likely from the same radio prepended to the SSID on this screen only
4. DONE     additional graph/screen - total station count
    swap values for CU and SUM in display
    list the SSID/BSSID with highest station count
5. DONE     additional graph/screen - % retries
    retries as a percentage of total frames received
    include beacon rate of strongest signal as a percentage of expected (separate screen?)
    list the SSID/BSSID with highest retry rates
6. DONE     Navigation - enable scrolling between screens with up/down on control stick
7. DONE     Menu - add logging only start/stop without any display
    do any screens need to be a separate menu?
8. DONE     Documentation
    reduce README to user information only
    create appropriate documents in docs folder 
9. DONE     when logging, periodically check for free disk space and close if nearly full
    add message to end of log that the disk is full
    for long term logging, write separate files periodically
10. how much can metrics also be found from hardware instead of just using beacons
        channel utilization - depends on hardware
        admission capacity - no
        retries - local only
        beacon count - local only, add to retries or separate screen
        clients - both - use unique MAC addresses
11. DONE    screenshot on device
        * when a button is pressed, take the current rendered screen image
        * write it to a PNG file on disk in the /var/log/wlanpi-beacon-live folder
        * name it with a timestamp, screen name, and maybe channel/BSSID
        * keep the capture/analysis loop running normally
        * disable/override other buttons while the display application is running
12. look into the possibility of scanning multiple channels
        probably not very feasible, beacons are 10 per second per BSSID
13. classroom mode
        find likely radios in classroom
        scan beacons for those radios
        record and show analytics from the study
14. return the wlan adapter to standard mode when application quits instead of monitor mode? what is default?
15. optimize code for processor utilization and/or use multithreading for multiple cores
16. DONE    change the channel screen label to "Utilization"
17. sync colors for TOP staitons and BSSID, how to signify what BSSID is shown on the bottom? is this needed?
        consider how to tie what metric is used to select the displayed BSSID, likely color
        this is for RSSI except some screens
            stations
            retries
18. additional graph/screen - noise/SNR
        from adapter if possible
19. DONE    make the text dynamic per line - only decrease size on the line needed, otherwise size 10 font.
        this should only ever affect line 2 if there is 100%
        line 3 for SSID should just be trunkated
20. DONE    on the Stations screen - instead of Max station count, include the locally detected station count from frames
        do not include probe requests or other frames that are not from an associated client
        include clients that were detected within the last 2 minutes
        can call this MAC or stay with MAX
        possibly overlay this as a line or bar on the graph
21. additional screen - Beacons
        for all BSSIDs associated with the strongest radio, count all received beacons and divide by the number of expected beacons
        this may need to track beacon timing instead of a simple 10 beacons per second, because it is actually one beacon per 102.4ms. or 10 beacons per 1.024 seconds, or 9.765625 beacons per second. sometimes there will be 9 per second and often there will be 10 per second. this graph may need to be delayed by one second in order to see if the additional beacons were received in the following window
        alternatively, we could look at all BSSIDs collectively, including those that are far away, but there will be a higher probability that beacons are not received because they are too weak to be demodulated, not because they were dropped because of contention.
22. improve vendor discover through IE fields - currently Cisco, Aruba, Extreme, Aerohive. Add Mist, Ubiquiti, etc
        add better discovery if possible - MLD identity, controller identifiers, richer vendor-specific device IDs ?
23. DONE    reorder screens in a logical way
24. DONE    logging only seems to be broken
        stop logging does not work
        start logging hangs for a second before the screen can be navigated again
        add a status as it starts or change the menu to change text
        add confirmation that logging has stopped - logging channel X has started... etc
25. unify app name - beacon-live and Utilization
        update log path if necessary
26. update the stations TOP field to show the top BSSID whether reported by QBSS (current) or total MAC for the SSID
        as this is updated per second, it's not likely to be used very often unless there is a busy AP with no QBSS