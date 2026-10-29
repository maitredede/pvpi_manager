[Home](Home.md)

# System battery

Optional, off by default. With it, your Pi sees the PV Pi's battery the way a laptop sees its own: `upower` (and any battery indicator) shows the charge, whether it's charging, and whether the solar panel is up.

Linux only lets the kernel create batteries, so this needs a small kernel module, `pvpi_battery`, in the [`kmod/pvpi_battery`](../kmod/pvpi_battery) folder. It adds two power supplies, `pvpi-battery` and `pvpi-pv` (the solar input), and reads nothing itself: the manager writes its readings to them every 10 seconds. If nothing is written for 2 minutes (the manager stopped), the battery's status turns `Unknown` and the solar input offline, so a stale battery doesn't look healthy.

The module is GPL licensed (`GPL-2.0-or-later`), like all Linux kernel code; the rest of pvpi_manager stays MIT.

What the system sees:

- **Charge** (`capacity`): estimated from the battery voltage, like the dashboard's. A LiFePO4 battery's voltage hardly moves between 20 % and 90 %, so treat it as a rough guide.
- **Status**: `Full` once the charger has finished, `Charging` while it charges, otherwise `Not charging` if the panel is up (above 4.2 V, the lowest voltage the charger runs from) or `Discharging` if it isn't.
- **Level** (`capacity_level`): `Critical` from the first reading at or below `low_bat_volt`, when the manager is about to shut the Pi down; `Full` once charged; `Normal` otherwise.
- **Current**: the PV Pi only measures the battery current while charging, so it's 0 the rest of the time.

## Installing the module

You need the repo cloned on your Pi (the module isn't in the pip package). From the pvpi_manager folder:

```shell
sudo apt install dkms linux-headers-rpi-v8      # on a Pi 5: linux-headers-rpi-2712
                                                # or linux-headers-$(uname -r): the running kernel only
sudo dkms install ./kmod/pvpi_battery
```

DKMS builds the module for the running kernel, and builds it again by itself after each kernel update. `sudo dkms status` should show `pvpi_battery/0.1.0, <kernel>, aarch64: installed`.

Let the manager write to it, and load it at every boot:

```shell
sudo cp kmod/pvpi_battery/99-pvpi-battery.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo cp kmod/pvpi_battery/pvpi-battery.conf /etc/modules-load.d/
sudo modprobe pvpi_battery
```

The udev rule lets the `dialout` group write the readings. The manager's user is normally in it already (it's the group for the serial port); check with `id`, and if it isn't: `sudo usermod -aG dialout <user>`, then reboot.

To wait longer (or less) before the readings count as stale, e.g. 5 minutes: `options pvpi_battery stale_secs=300` in `/etc/modprobe.d/pvpi-battery.conf` (`0` never marks them stale).

To update the module after pulling a new version: `sudo dkms remove pvpi_battery/0.1.0 --all`, then `sudo dkms install ./kmod/pvpi_battery` and `sudo modprobe -r pvpi_battery && sudo modprobe pvpi_battery`.

### Module signing and Secure Boot

On Raspberry Pi OS there's nothing to do: its kernel doesn't check module signatures. To check, `grep CONFIG_MODULE_SIG /boot/config-$(uname -r)` prints `# CONFIG_MODULE_SIG is not set`, and `dkms install` says *The kernel is built without module signing facility, modules won't be signed*. (The secure boot of the Pi 4 and 5 signs the boot image, not the modules.)

On a computer with UEFI Secure Boot on (`mokutil --sb-state` says `SecureBoot enabled`), the kernel only loads signed modules. On Debian, DKMS signs them with its own key, made on the first build: `/var/lib/dkms/mok.key`, with the certificate `/var/lib/dkms/mok.pub`. Enroll that certificate once:

```shell
sudo mokutil --import /var/lib/dkms/mok.pub    # asks for a one-time password
sudo reboot                                    # the MOK manager asks to enroll it, with that password
```

(Ubuntu's DKMS uses `/var/lib/shim-signed/mok/MOK.der` instead.)

## Turning it on

Turn on **Show the battery to the system** on the dashboard's Config tab, or set `"system_battery": true` in your `config.json`, then:

```shell
uv run pvpi restart
```

If the module isn't loaded, or the manager can't write to it (no udev rule, user not in `dialout`), the manager logs a warning (`System battery off: ...`) and carries on without it.

Updating the system battery takes four more requests to the PV Pi each pass. It never stops the manager: a failed update is logged once, and the manager leaves the system battery alone for a minute before trying again, so a board slow to answer holds up the loop once a minute at most. While the battery is low, the manager only writes what the battery voltage gives (no more requests), so the low battery checks never wait on it.

To turn it off again, untick it (or set it to `false`), `uv run pvpi restart`, and unload the module: a restart stops the manager without taking the battery away, and it would stay there, `Unknown`.

```shell
sudo modprobe -r pvpi_battery
sudo rm /etc/modules-load.d/pvpi-battery.conf     # not at the next boot either
```

## Checking it

```shell
cat /sys/class/power_supply/pvpi-battery/uevent
cat /sys/class/power_supply/pvpi-pv/uevent
upower -i /org/freedesktop/UPower/devices/battery_pvpi_battery
upower -d        # everything, including "on-battery"
```

## What else changes

With a battery that discharges, the rest of the system knows when your Pi runs on it (at night, or with the panel down):

- `upower` takes action itself on a nearly empty battery: at `PercentageAction` (2 % by default, in `/etc/UPower/UPower.conf`) it runs its `CriticalPowerAction`, a shutdown without the manager's wake alarm and power off. With the default `low_bat_volt` (12.9 V, about 50 %) the manager shuts the Pi down long before; keep `low_bat_volt` above about 12.1 V (2 %) for that to stay true.
- systemd counts the Pi as off AC power (`systemd-ac-power` says `no`), so it skips the services that only run on AC power (`ConditionACPower=true`). On Debian trixie and Raspberry Pi OS: `apt-daily`, `apt-daily-upgrade`, `logrotate`, `man-db` and `e2scrub_all`. `logrotate` runs around midnight, so on a solar Pi it may never run. To run it on battery too:

```shell
sudo mkdir -p /etc/systemd/system/logrotate.service.d
printf '[Unit]\nConditionACPower=\n' | sudo tee /etc/systemd/system/logrotate.service.d/ac-power.conf
sudo systemctl daemon-reload
```

(An empty `ConditionACPower=` clears the service's conditions.) The others can be done the same way if you want them.

## NUT

[NUT](https://networkupstools.org/) (Network UPS Tools) has no driver that reads `/sys/class/power_supply`. To see the PV Pi in NUT, e.g. to watch it from another machine, [`kmod/pvpi_battery/nut/pvpi-battery-nut.sh`](../kmod/pvpi_battery/nut/pvpi-battery-nut.sh) copies the readings into a data file for NUT's `dummy-ups` driver every 10 seconds. `dummy-ups` reads a `.dev` file again each time it changes (its `dummy-once` mode).

| NUT | From |
|---|---|
| `ups.status` | `OL` with the panel up, `OB` without; `CHRG` or `DISCHRG` from the status; `LB` when the level is `Critical` |
| `battery.charge`, `battery.voltage`, `battery.current` | the battery |
| `battery.temperature` | only once something writes the battery's `temp` |
| `input.voltage`, `input.current` | the solar panel |

When the readings are stale, the script leaves the file as it is rather than make up a status: NUT keeps showing the last readings (`dummy-ups` can't tell they're old) until they're fresh again.

Install the script and its service:

```shell
sudo install -m 755 kmod/pvpi_battery/nut/pvpi-battery-nut.sh /usr/local/bin/
sudo cp kmod/pvpi_battery/nut/pvpi-battery-nut.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pvpi-battery-nut.service
```

Then, with `sudo apt install nut-server`, an example setup. In `/etc/nut/nut.conf`, `MODE=netserver` (the driver and `upsd`, for other machines to watch).

`/etc/nut/ups.conf`:

```ini
[pvpi]
    driver = dummy-ups
    port = /run/pvpi-battery-nut/pvpi.dev
    desc = "PV Pi battery"
```

`/etc/nut/upsd.conf` (add the Pi's own address to watch it from other machines):

```ini
LISTEN 127.0.0.1 3493
```

`/etc/nut/upsd.users` (only for `upsmon` on other machines; read the warning below first):

```ini
[monuser]
    password = choose-a-password
    upsmon secondary
```

Check with `upsc pvpi@localhost`. Until the manager has written its first readings, the data file isn't there and the `dummy-ups` driver fails; systemd starts it again 15 seconds later.

The Pi's shutdown is the manager's job: it sets the wake alarm and cuts the power on the way down. Any `upsmon` that monitors the PV Pi (`primary` or `secondary`) shuts its own machine down on `OB LB`, and `LB` comes with the first reading at or below `low_bat_volt`, before the manager has decided anything (a short load spike can do it). So don't run `upsmon` for it on the Pi itself, and on other machines only if they should shut down then too; to just watch it, use `upsc pvpi@<your-pi>`.

Back to [Home](Home.md)
