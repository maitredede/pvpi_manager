"""The PV PI's battery shown to the system like a laptop's: the readings written to the power
supplies of the pvpi_battery kernel module (kmod/pvpi_battery), which upower and battery
indicators read. See docs/System-battery.md."""

import os
from pathlib import Path

from pvpi.client import PvPiChargeState

POWER_SUPPLY = Path("/sys/class/power_supply")
BATTERY, PV = "pvpi-battery", "pvpi-pv"  # the module's power supplies
# The charger (BQ25756) runs from 4.2 V up: below that the panel can't charge, so it's offline.
PV_ONLINE_VOLT = 4.2

_CHARGING = {
    PvPiChargeState.TrickleCharge,
    PvPiChargeState.PreCharge,
    PvPiChargeState.FastCharge,
    PvPiChargeState.TaperCharge,
    PvPiChargeState.TopOffTimerCharge,
}


def battery_status(charge_state: PvPiChargeState, pv_online: bool) -> str:
    """The battery's status as the kernel names it (POWER_SUPPLY_STATUS_*)."""
    if charge_state == PvPiChargeState.ChargeTerminationDone:
        return "Full"
    if charge_state in _CHARGING:
        return "Charging"
    # Not charging: the panel is up but not charging (e.g. too little sun), or there's no sun.
    return "Not charging" if pv_online else "Discharging"


def _micro(value: float) -> int:
    return round(value * 1_000_000)  # V to µV, A to µA


def _percent(soc: float) -> int:
    return min(100, max(0, round(soc)))


class SystemBattery:
    """The module's power supplies. Raises FileNotFoundError if the module isn't loaded,
    PermissionError if they can't be written."""

    def __init__(self, root: Path = POWER_SUPPLY):
        self._battery, self._pv = Path(root) / BATTERY, Path(root) / PV
        for supply in (self._battery, self._pv):
            if not supply.is_dir():
                raise FileNotFoundError(f"No {supply}: is the pvpi_battery kernel module loaded?")
        for readings in (self._battery / "status", self._pv / "online"):
            if not os.access(readings, os.W_OK):
                raise PermissionError(f"Can't write {readings}: is the udev rule installed, and the user in dialout?")

    def update(
        self, bat_v: float, bat_c: float, pv_v: float, pv_c: float, charge_state: PvPiChargeState, soc: float
    ) -> None:
        """Write one pass's readings. `soc`: the state of charge (%), estimated from the voltage
        or from another source."""
        pv_online = pv_v >= PV_ONLINE_VOLT
        status = battery_status(charge_state, pv_online)
        _write(self._pv, online=int(pv_online), voltage_now=_micro(pv_v), current_now=_micro(pv_c))
        _write(
            self._battery,
            present=1,
            voltage_now=_micro(bat_v),
            current_now=_micro(bat_c),  # the charger only measures it while charging: 0 otherwise
            capacity=_percent(soc),
            capacity_level="Full" if status == "Full" else "Normal",
            status=status,
        )

    def low(self, bat_v: float, soc: float) -> None:
        """A low battery reading: the Pi is about to shut down. Writes only what the battery voltage
        gives, so the low battery checks never wait on more requests to the board."""
        _write(self._battery, voltage_now=_micro(bat_v), capacity=_percent(soc), capacity_level="Critical")

    def close(self) -> None:
        """The manager is stopping: no readings from now on."""
        _write(self._battery, present=0)


def _write(supply: Path, **values) -> None:
    for name, value in values.items():
        (supply / name).write_text(f"{value}\n")
