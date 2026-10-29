"""The system battery: its status, and what it writes to the kernel module's power supplies (a
fake sysfs here)."""

import os

import pytest

from pvpi.client import PvPiChargeState
from pvpi.system_battery import SystemBattery, battery_status

State = PvPiChargeState


@pytest.mark.parametrize(
    "charge_state, pv_online, status",
    [
        (State.ChargeTerminationDone, True, "Full"),
        (State.TrickleCharge, True, "Charging"),
        (State.PreCharge, True, "Charging"),
        (State.FastCharge, True, "Charging"),
        (State.TaperCharge, True, "Charging"),
        (State.TopOffTimerCharge, True, "Charging"),
        (State.NotCharging, True, "Not charging"),
        (State.NA, True, "Not charging"),
        (State.NotCharging, False, "Discharging"),
        (State.NA, False, "Discharging"),
    ],
)
def test_the_status_follows_the_charger_then_the_sun(charge_state, pv_online, status):
    assert battery_status(charge_state, pv_online) == status


def read(sysfs, supply):
    return {f.name: f.read_text().strip() for f in (sysfs / supply).iterdir()}


def test_the_readings_are_written_in_the_kernels_units(power_supply):
    battery = SystemBattery(root=power_supply)
    battery.update(13.314, 1.25, 18.2, 0.9, State.FastCharge, 91.6)
    assert read(power_supply, "pvpi-battery") == {
        "present": "1",
        "voltage_now": "13314000",
        "current_now": "1250000",
        "capacity": "92",
        "capacity_level": "Normal",
        "status": "Charging",
    }
    assert read(power_supply, "pvpi-pv") == {"online": "1", "voltage_now": "18200000", "current_now": "900000"}

    battery.update(12.95, 0.0, 0.14, 0.0, State.NotCharging, 54.6)  # at night
    assert read(power_supply, "pvpi-battery")["status"] == "Discharging"
    assert read(power_supply, "pvpi-pv")["online"] == "0"

    battery.low(12.8, 40)  # about to shut down: no more than the voltage gives
    assert read(power_supply, "pvpi-battery") == {
        "present": "1",
        "voltage_now": "12800000",
        "current_now": "0",
        "capacity": "40",
        "capacity_level": "Critical",
        "status": "Discharging",
    }

    battery.close()
    assert read(power_supply, "pvpi-battery")["present"] == "0"


def test_without_the_module_it_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="pvpi_battery kernel module"):
        SystemBattery(root=tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_without_the_right_to_write_it_says_so(power_supply):
    (power_supply / "pvpi-battery" / "status").chmod(0o444)
    with pytest.raises(PermissionError, match="dialout"):
        SystemBattery(root=power_supply)
