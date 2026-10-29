[Home](Home.md)

# BMS and Home Assistant

The optional telemetry service (`pvpi_telemetry`) can:

- read your battery's **BMS** over Bluetooth (Daly and the other BMS [aiobmsble](https://github.com/patman15/aiobmsble) supports) and show it on the dashboard's Metrics page, and
- send the PV Pi's readings, the Raspberry Pi's temperatures and the BMS readings to **Home Assistant** over MQTT, where they show up as two devices through MQTT discovery.

Each part is turned on by its own setting, `bms_address` and `mqtt_host`; with neither set, the service stops at once.

## 1. Install the extras

```shell
uv sync --extra bms --extra mqtt
uv run pvpi install
```

Leave out the extra you don't need. `pvpi install` adds the `pvpi_telemetry` service (run it again if you installed before this service existed).

## 2. The BMS

Most Bluetooth BMS take **one client at a time**: while the Home Assistant Bluetooth integration (or a phone app) is connected, the Pi can't connect, and the other way round. Remove the BMS from Home Assistant's own Bluetooth integration (e.g. *BLE Battery Management*) and let Home Assistant get it through MQTT instead (step 3).

Raspberry Pi OS starts with Bluetooth blocked. Turn it on and find your BMS's address (Daly BMS are named `DL-…`):

```shell
sudo rfkill unblock bluetooth
bluetoothctl power on
bluetoothctl --timeout 20 scan on | grep -i dl-
```

Then set it in the config, with the BMS's type if it isn't a Daly (the module name in aiobmsble's `bms` folder, e.g. `jbd_bms`):

| Option | Default | What it's for |
|---|---|---|
| `bms_address` | `null` | The BMS's Bluetooth address, e.g. `D0:18:04:01:3F:7D` |
| `bms_type` | `daly_bms` | The BMS's aiobmsble module |
| `telemetry_period` | `30` | Seconds between readings (10 to 3600) |

The service keeps the Bluetooth connection open between readings, and connects again when it's lost. The latest readings are kept in `bms.json` in the log folder, which the dashboard shows.

On a Pi 3 or 4, Bluetooth needs the PL011 UART: keep the PV Pi on `/dev/ttyS0` (see [Getting started](Getting-started.md#2-turn-on-the-uart)) rather than using `dtoverlay=disable-bt`.

## 3. Home Assistant (MQTT)

You need an MQTT broker that Home Assistant uses, e.g. the Mosquitto broker app with the MQTT integration. Create a Home Assistant user for the Pi (or a broker login), and put its password in a file only you can read, kept out of `config.json` (the dashboard shows the config to anyone who can reach it):

```shell
install -m 600 /dev/null ~/.config/pvpi/mqtt-password
nano ~/.config/pvpi/mqtt-password
```

| Option | Default | What it's for |
|---|---|---|
| `mqtt_host` | `null` | The broker, e.g. `homeassistant.local` |
| `mqtt_port` | `1883` | The broker's port |
| `mqtt_username` | `null` | The broker user |
| `mqtt_password_file` | `null` | The file holding its password |
| `mqtt_topic` | `pvpi` | First level of the topics, and of the entity ids |
| `mqtt_discovery_prefix` | `homeassistant` | Home Assistant's discovery prefix |

Then `uv run pvpi restart`. Home Assistant finds two devices:

- **PV Pi (*hostname*)**: battery voltage and charge current, estimated charge, solar voltage, current and power, charge state, charging, faults, board temperature, and the Raspberry Pi's temperatures. Entity ids like `sensor.pvpi_bat_v`, `sensor.pvpi_pv_p`, `sensor.pvpi_pi_cpu_temp`.
- **Battery BMS**: charge, voltage, current and power (+ charging, − discharging), temperatures, stored energy and charge, cycles, each cell's voltage with the lowest, highest and their difference, the MOSFETs, balancer and problem flags. Entity ids like `sensor.pvpi_bms_soc`, `sensor.pvpi_bms_p`, `sensor.pvpi_bms_cell_1`.

The topics, all retained:

| Topic | What's in it |
|---|---|
| `pvpi/<hostname>/state` | The PV Pi and Raspberry Pi readings, as JSON |
| `pvpi/<hostname>/bms/state` | The BMS readings, as JSON |
| `pvpi/<hostname>/status` | `online`, or `offline` when the service stops |
| `pvpi/<hostname>/bms/status` | `online`, or `offline` while the BMS can't be read |

The PV Pi's own charge current can't show discharging (it only measures charging): the BMS's current and power can.

## Checking it

```shell
systemctl status pvpi_telemetry
journalctl -u pvpi_telemetry -f
```

`Couldn't read the BMS: … not found` usually means another client (Home Assistant, a phone) is connected to the BMS, or it's out of range.

Next: [Configuration](Configuration.md)
