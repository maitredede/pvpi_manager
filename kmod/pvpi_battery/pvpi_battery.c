// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * pvpi_battery: the PV PI's battery and solar input as power supplies, so the system (upower,
 * desktop battery indicators...) sees them like a laptop battery and its charger.
 *
 * The module reads nothing itself: pvpi_manager writes the readings to the standard sysfs
 * attributes (/sys/class/power_supply/pvpi-battery/capacity, ...), which this module makes
 * writable. If nothing is written for stale_secs, the battery status turns Unknown and the
 * solar input offline, so a stopped manager doesn't leave a healthy battery on show.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

static unsigned int stale_secs = 120;
module_param(stale_secs, uint, 0444);
MODULE_PARM_DESC(stale_secs, "Seconds without a write before the readings are shown as unknown (0: never)");

static DEFINE_SPINLOCK(lock);
static bool exiting;

static struct {
	int status, present, voltage_uv, current_ua, capacity, capacity_level, temp;
	bool has_temp;
} bat = {
	.status = POWER_SUPPLY_STATUS_UNKNOWN,
	.capacity_level = POWER_SUPPLY_CAPACITY_LEVEL_UNKNOWN,
};

static struct {
	int online, voltage_uv, current_ua;
} pv;

static struct platform_device *pdev; /* the supplies' parent */
static struct power_supply *bat_psy, *pv_psy;
static struct delayed_work stale_work;

static void stale_fn(struct work_struct *work)
{
	spin_lock(&lock);
	bat.status = POWER_SUPPLY_STATUS_UNKNOWN;
	pv.online = 0;
	spin_unlock(&lock);
	pr_warn("nothing written for %u s, readings are stale\n", stale_secs);
	power_supply_changed(bat_psy);
	power_supply_changed(pv_psy);
}

/*
 * Store a written value, called with the lock held. The change is announced under the lock too
 * (power_supply_changed() only queues work), so once pvpi_battery_exit() has set exiting no
 * write can queue work on a supply being unregistered.
 */
static int store(struct power_supply *psy, int *field, int v, bool changed)
{
	if (exiting)
		return -ENODEV;
	*field = v;
	if (stale_secs) /* fresh again for stale_secs */
		mod_delayed_work(system_wq, &stale_work, (unsigned long)stale_secs * HZ);
	if (changed)
		power_supply_changed(psy);
	return 0;
}

static const enum power_supply_property bat_props[] = {
	POWER_SUPPLY_PROP_STATUS,
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_TECHNOLOGY,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_CAPACITY,
	POWER_SUPPLY_PROP_CAPACITY_LEVEL,
	POWER_SUPPLY_PROP_TEMP,
	POWER_SUPPLY_PROP_SCOPE,
	POWER_SUPPLY_PROP_MODEL_NAME,
	POWER_SUPPLY_PROP_MANUFACTURER,
};

static int bat_get(struct power_supply *psy, enum power_supply_property psp, union power_supply_propval *val)
{
	int ret = 0;

	spin_lock(&lock);
	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		val->intval = bat.status;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		val->intval = bat.present;
		break;
	case POWER_SUPPLY_PROP_TECHNOLOGY:
		val->intval = POWER_SUPPLY_TECHNOLOGY_LiFe;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		val->intval = bat.voltage_uv;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		val->intval = bat.current_ua;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		val->intval = bat.capacity;
		break;
	case POWER_SUPPLY_PROP_CAPACITY_LEVEL:
		val->intval = bat.capacity_level;
		break;
	case POWER_SUPPLY_PROP_TEMP:
		if (bat.has_temp)
			val->intval = bat.temp;
		else
			ret = -ENODATA; /* not written yet: left out of the uevent */
		break;
	case POWER_SUPPLY_PROP_SCOPE:
		val->intval = POWER_SUPPLY_SCOPE_SYSTEM;
		break;
	case POWER_SUPPLY_PROP_MODEL_NAME:
		val->strval = "PV PI";
		break;
	case POWER_SUPPLY_PROP_MANUFACTURER:
		val->strval = "AutoEcology";
		break;
	default:
		ret = -EINVAL;
	}
	spin_unlock(&lock);
	return ret;
}

static int bat_set(struct power_supply *psy, enum power_supply_property psp, const union power_supply_propval *val)
{
	int v = val->intval, *field, ret;
	bool changed;

	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		if (v < POWER_SUPPLY_STATUS_UNKNOWN || v > POWER_SUPPLY_STATUS_FULL)
			return -EINVAL;
		field = &bat.status;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		if (v != 0 && v != 1)
			return -EINVAL;
		field = &bat.present;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		field = &bat.voltage_uv;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		field = &bat.current_ua;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		if (v < 0 || v > 100)
			return -EINVAL;
		field = &bat.capacity;
		break;
	case POWER_SUPPLY_PROP_CAPACITY_LEVEL:
		if (v < POWER_SUPPLY_CAPACITY_LEVEL_UNKNOWN || v > POWER_SUPPLY_CAPACITY_LEVEL_FULL)
			return -EINVAL;
		field = &bat.capacity_level;
		break;
	case POWER_SUPPLY_PROP_TEMP:
		field = &bat.temp;
		break;
	default:
		return -EINVAL;
	}

	spin_lock(&lock);
	changed = *field != v || (psp == POWER_SUPPLY_PROP_TEMP && !bat.has_temp);
	ret = store(psy, field, v, changed);
	if (!ret && psp == POWER_SUPPLY_PROP_TEMP)
		bat.has_temp = true;
	spin_unlock(&lock);
	return ret;
}

static const enum power_supply_property pv_props[] = {
	POWER_SUPPLY_PROP_ONLINE,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_CURRENT_NOW,
};

static int pv_get(struct power_supply *psy, enum power_supply_property psp, union power_supply_propval *val)
{
	int ret = 0;

	spin_lock(&lock);
	switch (psp) {
	case POWER_SUPPLY_PROP_ONLINE:
		val->intval = pv.online;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		val->intval = pv.voltage_uv;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		val->intval = pv.current_ua;
		break;
	default:
		ret = -EINVAL;
	}
	spin_unlock(&lock);
	return ret;
}

static int pv_set(struct power_supply *psy, enum power_supply_property psp, const union power_supply_propval *val)
{
	int v = val->intval, *field, ret;

	switch (psp) {
	case POWER_SUPPLY_PROP_ONLINE:
		if (v != 0 && v != 1)
			return -EINVAL;
		field = &pv.online;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		field = &pv.voltage_uv;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		field = &pv.current_ua;
		break;
	default:
		return -EINVAL;
	}

	spin_lock(&lock);
	ret = store(psy, field, v, *field != v);
	spin_unlock(&lock);
	return ret;
}

static int writeable(struct power_supply *psy, enum power_supply_property psp)
{
	switch (psp) {
	case POWER_SUPPLY_PROP_TECHNOLOGY:
	case POWER_SUPPLY_PROP_SCOPE:
	case POWER_SUPPLY_PROP_MODEL_NAME:
	case POWER_SUPPLY_PROP_MANUFACTURER:
		return 0;
	default:
		return 1;
	}
}

static const struct power_supply_desc bat_desc = {
	.name = "pvpi-battery",
	.type = POWER_SUPPLY_TYPE_BATTERY,
	.properties = bat_props,
	.num_properties = ARRAY_SIZE(bat_props),
	.get_property = bat_get,
	.set_property = bat_set,
	.property_is_writeable = writeable,
	.no_thermal = true, /* the temperature is a reading passed on, not a sensor to throttle on */
};

static const struct power_supply_desc pv_desc = {
	.name = "pvpi-pv",
	.type = POWER_SUPPLY_TYPE_MAINS,
	.properties = pv_props,
	.num_properties = ARRAY_SIZE(pv_props),
	.get_property = pv_get,
	.set_property = pv_set,
	.property_is_writeable = writeable,
	.no_thermal = true,
};

static char *pv_supplicants[] = { "pvpi-battery" };

static int __init pvpi_battery_init(void)
{
	struct power_supply_config pv_cfg = {
		.supplied_to = pv_supplicants,
		.num_supplicants = ARRAY_SIZE(pv_supplicants),
	};
	struct power_supply_config bat_cfg = {};
	int ret;

	INIT_DELAYED_WORK(&stale_work, stale_fn);

	pdev = platform_device_register_simple("pvpi-battery", PLATFORM_DEVID_NONE, NULL, 0);
	if (IS_ERR(pdev))
		return PTR_ERR(pdev);

	bat_psy = power_supply_register(&pdev->dev, &bat_desc, &bat_cfg);
	if (IS_ERR(bat_psy)) {
		ret = PTR_ERR(bat_psy);
		goto err_pdev;
	}

	pv_psy = power_supply_register(&pdev->dev, &pv_desc, &pv_cfg);
	if (IS_ERR(pv_psy)) {
		ret = PTR_ERR(pv_psy);
		goto err_bat;
	}
	return 0;

err_bat:
	spin_lock(&lock);
	exiting = true; /* a write may have started the stale timer */
	spin_unlock(&lock);
	cancel_delayed_work_sync(&stale_work);
	power_supply_unregister(bat_psy);
err_pdev:
	platform_device_unregister(pdev);
	return ret;
}

static void __exit pvpi_battery_exit(void)
{
	/* No write past this point (see store()), and no stale timer left, before the supplies go. */
	spin_lock(&lock);
	exiting = true;
	spin_unlock(&lock);
	cancel_delayed_work_sync(&stale_work);
	power_supply_unregister(pv_psy);
	power_supply_unregister(bat_psy);
	platform_device_unregister(pdev);
}

module_init(pvpi_battery_init);
module_exit(pvpi_battery_exit);

MODULE_DESCRIPTION("PV PI battery and solar input as power supplies, fed from user space");
MODULE_AUTHOR("Damien Daly");
MODULE_LICENSE("GPL");
