"""Telemetry: a battery BMS read over Bluetooth, and the readings sent to Home Assistant over MQTT.

Both are optional: `bms_address` turns the BMS on, `mqtt_host` the MQTT updates. A Bluetooth BMS
usually takes one client at a time, so this service is meant to be its only one; Home Assistant
gets the BMS readings through MQTT instead of its own Bluetooth connection.

The readings go to three retained topics under `<mqtt_topic>/<hostname>/`: `state` (the PV Pi
and the Raspberry Pi), `bms/state` (the BMS), and `status` / `bms/status` for availability.
Home Assistant finds them through device discovery: one device for the PV Pi, one for the BMS.
"""

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from pvpi.client import PvPiChargeState, PvPiClient
from pvpi.config import PvPiConfig
from pvpi.transports import ZmqSerialProxyInterface

_logger = logging.getLogger(__name__)

BMS_FILE = "bms.json"  # the latest BMS readings, in data_log_path, for the dashboard
BMS_SCAN_SECS = 10.0  # how long to look for the BMS before giving up this time
BMS_TIMEOUT_SECS = 60.0  # the most a whole BMS reading (scan, connection, requests) may take
BMS_MAX_WAIT_SECS = 600  # the longest wait before trying a failing BMS again
THERMAL_ROOT = Path("/sys/class/thermal")

_CHARGING = {
    PvPiChargeState.TrickleCharge,
    PvPiChargeState.PreCharge,
    PvPiChargeState.FastCharge,
    PvPiChargeState.TaperCharge,
    PvPiChargeState.TopOffTimerCharge,
}

# Reading key -> its Home Assistant entity: platform, name, device class, unit, extra options.
# Sensors with a unit are measurements; the cells and temperature probes are added per BMS.
_ENTITIES = {
    "soc": ("sensor", "Battery (estimated)", "battery", "%", {}),
    "bat_v": ("sensor", "Battery voltage", "voltage", "V", {}),
    "bat_c": ("sensor", "Battery charge current", "current", "A", {}),
    "pv_v": ("sensor", "Solar voltage", "voltage", "V", {}),
    "pv_c": ("sensor", "Solar current", "current", "A", {}),
    "pv_p": ("sensor", "Solar power", "power", "W", {}),
    "temp": ("sensor", "Board temperature", "temperature", "°C", {"entity_category": "diagnostic"}),
    "charge_state": ("sensor", "Charge state", None, None, {}),
    "charging": ("binary_sensor", "Charging", "battery_charging", None, {}),
    "fault": ("binary_sensor", "Fault", "problem", None, {}),
    "faults": ("sensor", "Faults", None, None, {"entity_category": "diagnostic"}),
    "bms_soc": ("sensor", "Battery", "battery", "%", {}),
    "bms_v": ("sensor", "Voltage", "voltage", "V", {}),
    "bms_c": ("sensor", "Current", "current", "A", {}),
    "bms_p": ("sensor", "Power", "power", "W", {}),
    "bms_temp": ("sensor", "Temperature", "temperature", "°C", {}),
    "bms_stored": ("sensor", "Stored energy", "energy_storage", "Wh", {}),
    "bms_charge": ("sensor", "Stored charge", None, "Ah", {}),
    "bms_cycles": ("sensor", "Cycles", None, None, {"state_class": "total_increasing"}),
    "bms_runtime": ("sensor", "Runtime", "duration", "s", {}),
    "bms_cell_min": ("sensor", "Lowest cell voltage", "voltage", "V", {}),
    "bms_cell_max": ("sensor", "Highest cell voltage", "voltage", "V", {}),
    "bms_cell_delta": ("sensor", "Cell voltage delta", "voltage", "V", {}),
    "bms_charging": ("binary_sensor", "Charging", "battery_charging", None, {}),
    "bms_problem": ("binary_sensor", "Problem", "problem", None, {}),
    "bms_chrg_mosfet": ("binary_sensor", "Charge MOSFET", None, None, {"entity_category": "diagnostic"}),
    "bms_dischrg_mosfet": ("binary_sensor", "Discharge MOSFET", None, None, {"entity_category": "diagnostic"}),
    "bms_balancer": ("binary_sensor", "Balancer", None, None, {"entity_category": "diagnostic"}),
}


def _entity(key: str):
    """The entity for a reading key, including the numbered ones (cells, probes, Pi sensors)."""
    if key in _ENTITIES:
        return _ENTITIES[key]
    if found := re.fullmatch(r"bms_cell_(\d+)", key):
        return ("sensor", f"Cell {found.group(1)} voltage", "voltage", "V", {"entity_category": "diagnostic"})
    if found := re.fullmatch(r"bms_temp_(\d+)", key):
        return ("sensor", f"Temperature {found.group(1)}", "temperature", "°C", {"entity_category": "diagnostic"})
    if found := re.fullmatch(r"pi_(\w+)_temp", key):
        name = found.group(1).replace("_", " ")
        return ("sensor", f"Raspberry Pi {name} temperature", "temperature", "°C", {})
    return None


def pvpi_readings(client: PvPiClient) -> dict:
    """The PV Pi's readings. Raises when the board can't be read."""
    bat_v = client.get_battery_voltage()
    pv_v, pv_c = client.get_pv_voltage(), client.get_pv_current()
    charge_state = client.get_charge_state_code()
    faults = client.get_fault_states()
    return {
        "soc": client.estimated_soc(),
        "bat_v": bat_v,
        "bat_c": client.get_battery_current(),
        "pv_v": pv_v,
        "pv_c": pv_c,
        "pv_p": round(pv_v * pv_c, 2),
        "temp": client.get_board_temp(),
        "charge_state": charge_state.name,
        "charging": charge_state in _CHARGING,
        "fault": bool(faults),
        "faults": ", ".join(faults) or "None",
    }


def pi_temperatures(root: Path = THERMAL_ROOT) -> dict:
    """The Raspberry Pi's temperatures (°C), one per thermal zone, e.g. pi_cpu_temp."""
    readings = {}
    for zone in sorted(root.glob("thermal_zone*")):
        try:
            kind = (zone / "type").read_text().strip()
            millideg = int((zone / "temp").read_text())
        except (OSError, ValueError):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", kind.lower().removesuffix("-thermal")).strip("_") or zone.name
        readings[f"pi_{name}_temp"] = round(millideg / 1000, 1)
    return readings


def bms_readings(sample: dict) -> dict:
    """An aiobmsble sample as flat readings: bms_soc, bms_v, bms_cell_1, ..."""
    names = {
        "battery_level": "bms_soc",
        "voltage": "bms_v",
        "current": "bms_c",
        "power": "bms_p",
        "temperature": "bms_temp",
        "cycle_capacity": "bms_stored",
        "cycle_charge": "bms_charge",
        "cycles": "bms_cycles",
        "runtime": "bms_runtime",
        "delta_voltage": "bms_cell_delta",
        "battery_charging": "bms_charging",
        "problem": "bms_problem",
        "chrg_mosfet": "bms_chrg_mosfet",
        "dischrg_mosfet": "bms_dischrg_mosfet",
        "balancer": "bms_balancer",
    }
    readings = {name: sample[key] for key, name in names.items() if sample.get(key) is not None}
    if "bms_balancer" in readings:
        readings["bms_balancer"] = bool(readings["bms_balancer"])  # some BMS give a bit mask
    cells = sample.get("cell_voltages") or []
    if cells:
        readings["bms_cell_min"], readings["bms_cell_max"] = min(cells), max(cells)
    for i, volts in enumerate(cells, start=1):
        readings[f"bms_cell_{i}"] = volts
    for i, probe in enumerate(sample.get("temp_values") or [], start=1):
        readings[f"bms_temp_{i}"] = float(probe)
    return readings


def save_bms(folder: Path, readings: dict) -> None:
    """Write the latest BMS readings for the dashboard, replacing the file in one step."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / BMS_FILE
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"time": datetime.now().astimezone().isoformat(timespec="seconds"), **readings}))
    os.replace(tmp, path)


def discovery(config: PvPiConfig, host: str, keys, bms: bool) -> tuple[str, dict]:
    """Home Assistant's device discovery topic and payload for the PV Pi's or the BMS's readings.

    `keys` are all the readings seen so far, not just the latest: an entity stays when a reading is
    missing for a while (e.g. the BMS's runtime, only known while discharging), showing unknown.
    """
    node = f"{config.mqtt_topic}_{host}" + ("_bms" if bms else "")
    base = f"{config.mqtt_topic}/{host}"
    device = {"ids": [node], "name": "Battery BMS" if bms else f"PV Pi ({host})"}
    if bms:
        device |= {"mdl": config.bms_type, "via_device": f"{config.mqtt_topic}_{host}"}
    else:
        device |= {"mf": "AutoEcology", "mdl": "PV Pi"}
    availability = [{"t": f"{base}/status"}] + ([{"t": f"{base}/bms/status"}] if bms else [])
    components = {}
    for key in sorted(keys):
        if (entity := _entity(key)) is None:
            continue
        platform, name, device_class, unit, extra = entity
        component = {"p": platform, "name": name, "unique_id": f"{node}_{key}"}
        component["default_entity_id"] = f"{platform}.{config.mqtt_topic}_{key}"
        # "None" (a missing reading) shows as unknown in Home Assistant
        if platform == "binary_sensor":
            value = f"('ON' if value_json.{key} else 'OFF') if value_json.{key} is defined else None"
        else:
            value = f"value_json.{key} | default(None)"
        component["value_template"] = f"{{{{ {value} }}}}"
        # Readings not sent for three periods (the board or BMS can't be read) show as unavailable.
        component["expire_after"] = 3 * config.telemetry_period
        if device_class:
            component["device_class"] = device_class
        if unit:
            component["unit_of_measurement"] = unit
            component["state_class"] = "measurement"
        components[key] = component | extra
    payload = {
        "dev": device,
        "o": {"name": "pvpi_manager", "url": "https://github.com/AutoEcology/pvpi_manager"},
        "avty": availability,
        "avty_mode": "all",
        "state_topic": f"{base}/bms/state" if bms else f"{base}/state",
        "cmps": components,
    }
    return f"{config.mqtt_discovery_prefix}/device/{node}/config", payload


class BmsReader:
    """Reads a BMS over Bluetooth with aiobmsble, keeping the connection between readings.

    After a failure it waits longer each time before trying again (up to BMS_MAX_WAIT_SECS), as
    scanning for a BMS that isn't there takes the radio from the Wi-Fi on boards that share one.
    """

    def __init__(self, address: str, bms_type: str, period: int):
        try:
            from bleak import BleakScanner
            from bleak_retry_connector import close_stale_connections_by_address

            self._bms_class = importlib.import_module(f"aiobmsble.bms.{bms_type}").BMS
        except ImportError as err:
            raise RuntimeError(
                f"Can't read a '{bms_type}' BMS ({err}): install the extra with 'uv sync --extra bms' (Python 3.12+)"
            ) from err
        self._scanner = BleakScanner
        self._close_stale = close_stale_connections_by_address
        self.address, self.period = address, period
        self._bms = None
        self._failures = 0
        self._next_try = 0.0  # time.monotonic() of the next attempt

    async def read(self) -> dict | None:
        """The BMS's readings, or None (logged) when it can't be read now."""
        if time.monotonic() < self._next_try:
            return None
        try:
            readings = await asyncio.wait_for(self._read(), timeout=BMS_TIMEOUT_SECS)
        except Exception as err:
            if not self._failures:
                _logger.warning("Couldn't read the BMS: %s", err or type(err).__name__)
            self._failures += 1
            wait = min(self.period * 2 ** (self._failures - 1), BMS_MAX_WAIT_SECS)
            self._next_try = time.monotonic() + wait
            await self.close()
            return None
        if self._failures:
            _logger.info("BMS read again")
        self._failures = 0
        return readings

    async def _read(self) -> dict:
        if self._bms is None:
            # A connection BlueZ kept from before (e.g. this service killed) hides the BMS from scans.
            await self._close_stale(self.address)
            device = await self._scanner.find_device_by_address(self.address, timeout=BMS_SCAN_SECS)
            if device is None:
                raise ConnectionError(f"{self.address} not found (out of range, or another client connected?)")
            self._bms = self._bms_class(device)
        return bms_readings(await self._bms.async_update())

    async def close(self) -> None:
        if self._bms is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._bms.disconnect(reset=True), timeout=10)
        self._bms = None


class MqttPublisher:
    """Sends the readings to an MQTT broker, with Home Assistant discovery.

    Every message is retained, and all of them are sent again on each connection: the first ones
    may come before the connection is up, and a broker may have restarted without keeping them.
    """

    def __init__(self, config: PvPiConfig, host: str):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:
            raise RuntimeError(f"Can't send to MQTT ({err}): run 'uv sync'") from err

        self.config, self.host = config, host
        self.base = f"{config.mqtt_topic}/{host}"
        self._lock = threading.Lock()  # paho calls _on_connect from its own thread
        self._retained: dict[str, str] = {}  # topic -> latest payload
        self._keys: dict[bool, set[str]] = {False: set(), True: set()}  # readings seen, by device (bms)
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{config.mqtt_topic}_{host}")
        self._client.will_set(f"{self.base}/status", "offline", retain=True)
        if config.mqtt_username:
            try:
                password = config.mqtt_password_path.read_text().strip()
            except OSError as err:
                _logger.warning("No MQTT password (%s)", err)
                password = None
            self._client.username_pw_set(config.mqtt_username, password)
        self._client.on_connect = self._on_connect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.on_disconnect = self._on_disconnect
        self._connected = None  # None until the first try: then each change is logged once
        self._send(f"{self.base}/status", "online")
        _logger.info("Sending the readings to MQTT %s:%i", config.mqtt_host, config.mqtt_port)
        self._client.connect_async(config.mqtt_host, config.mqtt_port)
        self._client.loop_start()

    def _lost(self, why: str) -> None:
        if self._connected is not False:
            _logger.warning("No MQTT connection (%s): trying again", why)
        self._connected = False

    def _on_connect_fail(self, client, userdata) -> None:
        self._lost("can't reach the broker")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._lost(str(reason_code))

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self._lost(f"refused: {reason_code}")
            return
        _logger.info("Connected to the MQTT broker %s:%i", self.config.mqtt_host, self.config.mqtt_port)
        self._connected = True
        with self._lock:
            messages = list(self._retained.items())
        for topic, payload in messages:
            client.publish(topic, payload, retain=True)

    def _send(self, topic: str, payload: str) -> None:
        with self._lock:
            if self._retained.get(topic) == payload and topic.startswith(self.config.mqtt_discovery_prefix):
                return  # discovery: only when it changes
            self._retained[topic] = payload
        self._client.publish(topic, payload, retain=True)  # dropped while not connected: sent on connect

    def publish(self, readings: dict | None, bms: dict | None, bms_on: bool) -> None:
        """Send the latest readings: None for a part that couldn't be read this time."""
        if readings is not None:
            self._state(readings, bms=False)
        if bms_on:
            self._send(f"{self.base}/bms/status", "online" if bms is not None else "offline")
            if bms is not None:
                self._state(bms, bms=True)

    def _state(self, readings: dict, bms: bool) -> None:
        self._keys[bms] |= readings.keys()
        topic, payload = discovery(self.config, self.host, self._keys[bms], bms)
        self._send(topic, json.dumps(payload))
        self._send(f"{self.base}/bms/state" if bms else f"{self.base}/state", json.dumps(readings))

    def close(self, bms_on: bool) -> None:
        """Mark this service (and the BMS) offline, then disconnect."""
        try:
            sent = [self._client.publish(f"{self.base}/status", "offline", retain=True)]
            if bms_on:
                sent.append(self._client.publish(f"{self.base}/bms/status", "offline", retain=True))
            if self._client.is_connected():
                for message in sent:
                    message.wait_for_publish(timeout=5)
        except Exception as err:
            _logger.warning("Couldn't mark the readings offline: %s", err)
        finally:
            self._client.disconnect()
            self._client.loop_stop()


def _host() -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", socket.gethostname()) or "pvpi"


async def run_async(config: PvPiConfig) -> None:
    if config.bms_address is None and config.mqtt_host is None:
        _logger.info("Nothing to do: set bms_address and/or mqtt_host in the config")
        return
    host = _host()
    if config.bms_address:
        _logger.info("Reading the %s BMS %s every %is", config.bms_type, config.bms_address, config.telemetry_period)
    bms = BmsReader(config.bms_address, config.bms_type, config.telemetry_period) if config.bms_address else None
    publisher = MqttPublisher(config, host) if config.mqtt_host else None
    client: PvPiClient | None = None
    failing = set()  # what's failing now: each is logged when it starts, not every period
    try:
        while True:
            started = time.monotonic()
            bms_values = await bms.read() if bms else None
            if bms_values is not None:
                try:
                    save_bms(config.data_log_path, bms_values)
                    failing.discard("bms.json")
                except OSError as err:
                    if "bms.json" not in failing:
                        _logger.warning("Couldn't save the BMS readings for the dashboard: %s", err)
                    failing.add("bms.json")
            if publisher:
                readings = None
                try:
                    if client is None:
                        client = PvPiClient(interface=ZmqSerialProxyInterface())
                    readings = pvpi_readings(client) | pi_temperatures()
                    if "board" in failing:
                        _logger.info("PV PI read again")
                    failing.discard("board")
                except Exception as err:
                    if "board" not in failing:
                        _logger.warning("Couldn't read the PV PI: %s", err)
                    failing.add("board")
                    if client is not None:
                        with contextlib.suppress(Exception):
                            client._interface.close()
                    client = None  # a fresh connection next time
                publisher.publish(readings, bms_values, bms is not None)
            await asyncio.sleep(max(1.0, config.telemetry_period - (time.monotonic() - started)))
    finally:
        if bms:
            await bms.close()
        if publisher:
            publisher.close(bms is not None)


async def _until_stopped(config: PvPiConfig) -> None:
    # systemd stops the service with SIGTERM: cancel, so the BMS is disconnected and MQTT told.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    await run_async(config)


def run(config: PvPiConfig) -> None:
    """Run until stopped. Raises RuntimeError when an optional package it needs isn't installed."""
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        asyncio.run(_until_stopped(config))
