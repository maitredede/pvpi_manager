import pytest


@pytest.fixture
def power_supply(tmp_path):
    """A fake /sys/class/power_supply with the pvpi_battery module's supplies, writable."""
    root = tmp_path / "power_supply"
    for supply, readings in (("pvpi-battery", "status"), ("pvpi-pv", "online")):
        (root / supply).mkdir(parents=True)
        (root / supply / readings).write_text("")
    return root
