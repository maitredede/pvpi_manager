#!/bin/sh
# Feed NUT from the pvpi_battery power supplies: NUT has no driver that reads
# /sys/class/power_supply, so this rewrites a data file for its dummy-ups driver every few
# seconds. dummy-ups reads a *.dev file again whenever its timestamp changes.
#
# Usage: pvpi-battery-nut.sh [data file] [seconds between updates]
set -eu
export LC_ALL=C  # a decimal point, whatever the locale

out=${1:-/run/pvpi-battery-nut/pvpi.dev}
every=${2:-10}
sys=${PVPI_POWER_SUPPLY:-/sys/class/power_supply}
bat=$sys/pvpi-battery
pv=$sys/pvpi-pv

# µV or µA in a sysfs file, as V or A
si() { awk -v v="$(cat "$1")" 'BEGIN { printf "%.3f", v / 1000000 }'; }

stale_logged=
while :; do
    status=$(cat "$bat/status")
    if [ "$status" = Unknown ] || [ "$(cat "$bat/present")" != 1 ]; then
        # No fresh readings (pvpi_manager stopped?): leave the last ones rather than make up a
        # status. "OFF" would be wrong, and upsmon may treat it as a power loss.
        [ -n "$stale_logged" ] || echo "pvpi-battery: no fresh readings, $out not updated" >&2
        stale_logged=1
        sleep "$every"
        continue
    fi
    stale_logged=

    if [ "$(cat "$pv/online")" = 1 ]; then ups=OL; else ups=OB; fi
    case $status in
        Charging) ups="$ups CHRG" ;;
        Discharging) ups="$ups DISCHRG" ;;
    esac
    # Critical: at or below pvpi_manager's low_bat_volt, it's about to shut the Pi down.
    case $(cat "$bat/capacity_level") in
        Critical | Low) ups="$ups LB" ;;
    esac
    temp=$(cat "$bat/temp" 2>/dev/null) || temp=  # only once something writes it

    {
        echo "device.mfr: AutoEcology"
        echo "device.model: PV PI"
        echo "device.type: ups"
        echo "ups.mfr: AutoEcology"
        echo "ups.model: PV PI"
        echo "ups.status: $ups"
        echo "battery.type: LiFePO4"
        echo "battery.charge: $(cat "$bat/capacity")"
        echo "battery.voltage: $(si "$bat/voltage_now")"
        echo "battery.current: $(si "$bat/current_now")"
        [ -z "$temp" ] || awk -v t="$temp" 'BEGIN { printf "battery.temperature: %.1f\n", t / 10 }'
        echo "input.voltage: $(si "$pv/voltage_now")"
        echo "input.current: $(si "$pv/current_now")"
    } >"$out.tmp"
    mv -f "$out.tmp" "$out"  # in one step: dummy-ups never reads half a file
    sleep "$every"
done
