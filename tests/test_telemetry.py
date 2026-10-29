import json

from pvpi.client import PvPiClient
from pvpi.config import PvPiConfig
from pvpi.services.telemetry import BMS_FILE, bms_readings, discovery, pi_temperatures, pvpi_readings, save_bms

REPLIES = {
    b"GET_BAT_V": "MILLIVOLTS,13200",
    b"GET_BAT_C": "MILLIAMPS,500",
    b"GET_PV_V": "MILLIVOLTS,18000",
    b"GET_PV_C": "MILLIAMPS,1200",
    b"GET_TEMP": "TEMP,25",
    b"GET_CHARGE_STATE": "CHARGE_STATE,3",
    b"GET_FAULT_CODE": "FAULT_CODE,0",
}


class Board:
    def write(self, message):
        return REPLIES[message]

    def close(self):
        pass


SAMPLE = {
    "battery_level": 87,
    "voltage": 13.31,
    "current": -1.2,
    "power": -15.97,
    "temperature": 21.0,
    "cycle_capacity": 1180.4,
    "cycle_charge": 88.7,
    "cycles": 12,
    "delta_voltage": 0.004,
    "battery_charging": False,
    "problem": False,
    "chrg_mosfet": True,
    "dischrg_mosfet": True,
    "balancer": 0,
    "cell_voltages": [3.327, 3.329, 3.331, 3.328],
    "temp_values": [20.0, 22.0],
    "runtime": None,  # not known while discharging slowly: left out
}


def test_pvpi_readings():
    readings = pvpi_readings(PvPiClient(interface=Board()))
    assert readings["bat_v"] == 13.2 and readings["pv_p"] == 21.6
    assert readings["charge_state"] == "FastCharge" and readings["charging"] is True
    assert readings["fault"] is False and readings["faults"] == "None"


def test_pi_temperatures(tmp_path):
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "type").write_text("cpu-thermal\n")
    (zone / "temp").write_text("44016\n")
    (tmp_path / "thermal_zone1").mkdir()  # unreadable: skipped
    assert pi_temperatures(tmp_path) == {"pi_cpu_temp": 44.0}


def test_bms_readings():
    readings = bms_readings(SAMPLE)
    assert readings["bms_soc"] == 87 and readings["bms_c"] == -1.2
    assert readings["bms_cell_min"] == 3.327 and readings["bms_cell_max"] == 3.331
    assert readings["bms_cell_4"] == 3.328 and readings["bms_temp_2"] == 22.0
    assert readings["bms_balancer"] is False
    assert "bms_runtime" not in readings


def test_save_bms(tmp_path):
    save_bms(tmp_path / "logs", {"bms_soc": 87})
    saved = json.loads((tmp_path / "logs" / BMS_FILE).read_text())
    assert saved["bms_soc"] == 87 and "time" in saved


def test_discovery_pvpi_device():
    config = PvPiConfig(mqtt_host="broker")
    topic, payload = discovery(config, "pvpi", {"bat_v": 13.2, "charging": True, "pi_cpu_temp": 44.0}, bms=False)
    assert topic == "homeassistant/device/pvpi_pvpi/config"
    assert payload["state_topic"] == "pvpi/pvpi/state"
    assert payload["avty"] == [{"t": "pvpi/pvpi/status"}]
    bat_v = payload["cmps"]["bat_v"]
    assert bat_v["default_entity_id"] == "sensor.pvpi_bat_v" and bat_v["unique_id"] == "pvpi_pvpi_bat_v"
    assert bat_v["unit_of_measurement"] == "V" and bat_v["state_class"] == "measurement"
    assert payload["cmps"]["charging"]["value_template"] == "{{ 'ON' if value_json.charging else 'OFF' }}"
    assert payload["cmps"]["pi_cpu_temp"]["device_class"] == "temperature"


def test_discovery_bms_device_follows_the_cells():
    config = PvPiConfig(mqtt_host="broker", bms_address="D0:18:04:01:3F:7D")
    topic, payload = discovery(config, "pvpi", bms_readings(SAMPLE), bms=True)
    assert topic == "homeassistant/device/pvpi_pvpi_bms/config"
    assert payload["dev"]["via_device"] == "pvpi_pvpi"
    assert payload["state_topic"] == "pvpi/pvpi/bms/state"
    assert {"t": "pvpi/pvpi/bms/status"} in payload["avty"] and payload["avty_mode"] == "all"
    assert payload["cmps"]["bms_soc"]["default_entity_id"] == "sensor.pvpi_bms_soc"
    assert payload["cmps"]["bms_cycles"]["state_class"] == "total_increasing"
    assert {f"bms_cell_{i}" for i in range(1, 5)} <= payload["cmps"].keys()


def test_discovery_skips_unknown_readings():
    _, payload = discovery(PvPiConfig(mqtt_host="broker"), "pvpi", {"something_else": 1}, bms=False)
    assert payload["cmps"] == {}
