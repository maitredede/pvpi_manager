import json

import pytest

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
    charging = payload["cmps"]["charging"]["value_template"]
    assert charging == "{{ ('ON' if value_json.charging else 'OFF') if value_json.charging is defined else None }}"
    assert bat_v["value_template"] == "{{ value_json.bat_v | default(None) }}" and bat_v["expire_after"] == 90
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


class FakeMqtt:
    """paho's Client as far as MqttPublisher uses it; publishes are kept while 'connected'."""

    def __init__(self, *args, **kwargs):
        self.sent, self.connected = [], False

    def will_set(self, *args, **kwargs):
        pass

    def connect_async(self, *args):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload, retain=False):
        if self.connected:
            self.sent.append((topic, payload))
        return self

    def wait_for_publish(self, timeout=None):
        if not self.connected:
            raise RuntimeError("not connected")


def test_mqtt_sends_everything_again_on_connect(monkeypatch):
    import paho.mqtt.client

    from pvpi.services.telemetry import MqttPublisher

    monkeypatch.setattr(paho.mqtt.client, "Client", FakeMqtt)
    publisher = MqttPublisher(PvPiConfig(mqtt_host="broker", bms_address="D0:18:04:01:3F:7D"), "pvpi")
    client = publisher._client
    publisher.publish({"bat_v": 13.2}, None, bms_on=True)  # before the connection is up: dropped by paho
    assert client.sent == []

    class Ok:
        is_failure = False

    client.connected = True
    publisher._on_connect(client, None, None, Ok(), None)
    topics = dict(client.sent)
    assert topics["pvpi/pvpi/status"] == "online" and topics["pvpi/pvpi/bms/status"] == "offline"
    assert json.loads(topics["pvpi/pvpi/state"]) == {"bat_v": 13.2}
    assert "homeassistant/device/pvpi_pvpi/config" in topics

    client.sent.clear()
    publisher.publish({"bat_v": 13.3}, None, bms_on=True)
    assert {topic for topic, _ in client.sent} == {"pvpi/pvpi/bms/status", "pvpi/pvpi/state"}  # discovery unchanged

    client.sent.clear()
    publisher.publish({"bat_v": 13.3, "temp": 30}, None, bms_on=True)
    assert "homeassistant/device/pvpi_pvpi/config" in dict(client.sent)  # a new reading: discovery again
    publisher.publish({"temp": 31}, None, bms_on=True)
    discovered = json.loads(publisher._retained["homeassistant/device/pvpi_pvpi/config"])
    assert {"bat_v", "temp"} <= discovered["cmps"].keys()  # a missing reading keeps its entity


def test_mqtt_close_while_not_connected(monkeypatch):
    import paho.mqtt.client

    from pvpi.services.telemetry import MqttPublisher

    monkeypatch.setattr(paho.mqtt.client, "Client", FakeMqtt)
    MqttPublisher(PvPiConfig(mqtt_host="broker"), "pvpi").close(bms_on=False)  # doesn't raise


def test_bms_reader_waits_longer_after_each_failure(monkeypatch, caplog):
    import asyncio

    pytest.importorskip("aiobmsble")
    from pvpi.services import telemetry

    scans = []

    class Scanner:
        @staticmethod
        async def find_device_by_address(address, timeout):
            scans.append(address)

    async def no_stale(address):
        pass

    now = [1000.0]
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now[0])
    reader = telemetry.BmsReader("D0:18:04:01:3F:7D", "daly_bms", period=30)
    reader._scanner, reader._close_stale = Scanner, no_stale

    async def attempts():
        results = []
        for advance in (0, 10, 25, 30, 60):  # tries at 0, 30 (after 30 s) and 90 (after 60 s more)
            now[0] += advance
            results.append(await reader.read())
        return results

    with caplog.at_level("WARNING", logger="pvpi"):
        assert asyncio.run(attempts()) == [None] * 5
    assert len(scans) == 3
    assert len([r for r in caplog.records if "Couldn't read the BMS" in r.message]) == 1
