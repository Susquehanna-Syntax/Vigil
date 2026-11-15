"""psutil's ``sensors_temperatures`` walk, best-effort.

Laptop ECIs and mainboard chips disagree about naming; psutil returns whatever
the chip reports, so the sensor name becomes a label verbatim. On a host with
no hwmon data (most VMs) this returns nothing at all.
"""

from ._common import _point, psutil


def collect_temperatures() -> list[dict]:
    points = []
    try:
        all_temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return points
    for sensor_name, entries in all_temps.items():
        for entry in entries:
            label = entry.label or sensor_name
            labels = {"sensor": sensor_name, "label": label}
            points.append(_point("temperature", "celsius", entry.current, labels))
            if entry.high is not None:
                points.append(_point("temperature", "high_celsius", entry.high, labels))
            if entry.critical is not None:
                points.append(_point("temperature", "critical_celsius", entry.critical, labels))
    return points


