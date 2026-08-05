import requests

from datetime import datetime, timedelta


def to_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def format_duration(seconds):
    seconds = int(seconds)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours} год {minutes} хв {secs} с"

    if minutes:
        return f"{minutes} хв {secs} с"

    return f"{secs} с"

def get_history(
    ha_url,
    sensor,
    headers,
    hours=24,
):
    end = datetime.utcnow()

    start = end - timedelta(hours=hours)

    url = (
        f"{ha_url.replace('/api/states/', '')}"
        f"/api/history/period/"
        f"{start.isoformat()}"
    )

    params = {
        "filter_entity_id": sensor,
        "end_time": end.isoformat(),
        "minimal_response": False,
        "no_attributes": True,
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        if not data:
            return []

        return data[0]

    except Exception as e:
        print("History error:", e)
        return []


def calculate_statistics(history):
    if not history:
        return {
            "uptime": "0 с",
            "downtime": "0 с",
            "outages": 0,
            "average_voltage": 0.0,
            "max_voltage": 0.0,
            "min_voltage": 0.0,
        }

    voltages = []
    outages = 0

    uptime_seconds = 0
    downtime_seconds = 0

    previous_time = None
    previous_power = None

    for item in history:

        voltage = to_float(item.get("state"))

        timestamp = datetime.fromisoformat(
            item["last_changed"].replace("Z", "+00:00")
        )

        power = voltage >= 150

        voltages.append(voltage)

        if previous_time is not None:

            seconds = (
                timestamp - previous_time
            ).total_seconds()

            if previous_power:
                uptime_seconds += seconds
            else:
                downtime_seconds += seconds

        if previous_power is True and not power:
            outages += 1

        previous_time = timestamp
        previous_power = power

    return {
        "uptime": format_duration(uptime_seconds),
        "downtime": format_duration(downtime_seconds),
        "outages": outages,
        "average_voltage": sum(voltages) / len(voltages),
        "max_voltage": max(voltages),
        "min_voltage": min(voltages),
    }

def get_low_voltage_events(history, threshold=200):
    events = 0
    active = False

    for item in history:
        voltage = to_float(item.get("state"))

        if voltage < threshold:
            if not active:
                events += 1
                active = True
        else:
            active = False

    return events

def calculate_extra_statistics(history):
    voltages = []

    on_voltages = []

    for item in history:
        voltage = to_float(item.get("state"))

        voltages.append(voltage)

        if voltage >= 150:
            on_voltages.append(voltage)

    if voltages:
        average = sum(voltages) / len(voltages)
    else:
        average = 0

    if on_voltages:
        average_on = sum(on_voltages) / len(on_voltages)
    else:
        average_on = 0

    return {
        "average_voltage": average,
        "average_on_voltage": average_on,
        "maximum_voltage": max(voltages) if voltages else 0,
        "minimum_voltage": min(voltages) if voltages else 0,
        "low_voltage_events": get_low_voltage_events(history),
    }
