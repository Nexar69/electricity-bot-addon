from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests


UNAVAILABLE_STATES = {
    "unavailable",
    "unknown",
    "none",
    "",
}


def to_float(value: Any) -> float:
    """
    Convert a Home Assistant sensor state to a voltage number.

    unavailable, unknown and invalid values are treated as 0 V.
    """
    try:
        state = str(value).strip().lower()

        if state in UNAVAILABLE_STATES:
            return 0.0

        return float(state)

    except (TypeError, ValueError):
        return 0.0


def parse_timestamp(value: str) -> datetime:
    """
    Convert a Home Assistant ISO timestamp to a datetime object.
    """
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def format_duration(seconds: float) -> str:
    """
    Format seconds as Ukrainian hours, minutes and seconds.
    """
    total_seconds = max(0, int(seconds))

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours} год {minutes} хв {secs} с"

    if minutes:
        return f"{minutes} хв {secs} с"

    return f"{secs} с"


def get_history(
    ha_url: str,
    sensor: str,
    headers: dict[str, str],
    hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Retrieve voltage history from Home Assistant.
    """
    end_time = datetime.now(timezone.utc)

    start_time = end_time - timedelta(
        hours=max(1, hours)
    )

    base_url = ha_url.split(
        "/api/",
        1,
    )[0].rstrip("/")

    url = (
        f"{base_url}/api/history/period/"
        f"{start_time.isoformat()}"
    )

    params = {
        "filter_entity_id": sensor,
        "end_time": end_time.isoformat(),
        "minimal_response": "false",
        "no_attributes": "true",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )

        response.raise_for_status()

        payload = response.json()

        if (
            not isinstance(payload, list)
            or not payload
            or not payload[0]
        ):
            return []

        history = sorted(
            payload[0],
            key=lambda item: item.get(
                "last_changed",
                item.get("last_updated", ""),
            ),
        )

        # Add a final point representing the current time.
        # This allows the last recorded state to contribute
        # to uptime and downtime calculations.
        last_item = history[-1]

        last_timestamp = (
            last_item.get("last_changed")
            or last_item.get("last_updated")
        )

        if last_timestamp:
            last_time = parse_timestamp(last_timestamp)

            if last_time < end_time:
                history.append(
                    {
                        "entity_id": sensor,
                        "state": last_item.get(
                            "state",
                            "unknown",
                        ),
                        "last_changed": end_time.isoformat(),
                        "last_updated": end_time.isoformat(),
                        "_period_boundary": True,
                    }
                )

        return history

    except (
        requests.RequestException,
        ValueError,
        KeyError,
    ) as error:
        print(
            "History error:",
            repr(error),
        )

        return []


def calculate_statistics(
    history: list[dict[str, Any]],
    off_voltage: float = 150.0,
    low_voltage_threshold: float = 200.0,
) -> dict[str, Any]:
    """
    Calculate time-weighted statistics for voltage history.
    """
    empty_statistics = {
        "uptime": "0 с",
        "downtime": "0 с",
        "uptime_seconds": 0,
        "downtime_seconds": 0,
        "uptime_percent": 0.0,
        "outages": 0,
        "average_voltage": 0.0,
        "average_on_voltage": 0.0,
        "max_voltage": 0.0,
        "min_voltage": 0.0,
        "low_voltage_events": 0,
    }

    if len(history) < 2:
        return empty_statistics

    uptime_seconds = 0.0
    downtime_seconds = 0.0

    weighted_voltage_sum = 0.0
    weighted_on_voltage_sum = 0.0

    on_duration = 0.0

    outages = 0
    low_voltage_events = 0

    previous_power = None
    low_voltage_active = False

    on_voltages = []

    for index in range(len(history) - 1):
        item = history[index]
        next_item = history[index + 1]

        timestamp_value = (
            item.get("last_changed")
            or item.get("last_updated")
        )

        next_timestamp_value = (
            next_item.get("last_changed")
            or next_item.get("last_updated")
        )

        if not timestamp_value or not next_timestamp_value:
            continue

        timestamp = parse_timestamp(
            timestamp_value
        )

        next_timestamp = parse_timestamp(
            next_timestamp_value
        )

        duration = max(
            0.0,
            (
                next_timestamp
                - timestamp
            ).total_seconds(),
        )

        voltage = to_float(
            item.get("state")
        )

        power = voltage >= off_voltage

        weighted_voltage_sum += (
            voltage * duration
        )

        if power:
            uptime_seconds += duration
            on_duration += duration

            weighted_on_voltage_sum += (
                voltage * duration
            )

            on_voltages.append(voltage)

        else:
            downtime_seconds += duration

        if previous_power is None:
            if not power:
                outages = 1

        elif previous_power and not power:
            outages += 1

        low_voltage = (
            off_voltage
            <= voltage
            < low_voltage_threshold
        )

        if (
            low_voltage
            and not low_voltage_active
        ):
            low_voltage_events += 1

        low_voltage_active = low_voltage
        previous_power = power

    total_seconds = (
        uptime_seconds
        + downtime_seconds
    )

    if total_seconds <= 0:
        return empty_statistics

    average_voltage = (
        weighted_voltage_sum
        / total_seconds
    )

    if on_duration > 0:
        average_on_voltage = (
            weighted_on_voltage_sum
            / on_duration
        )
    else:
        average_on_voltage = 0.0

    return {
        "uptime": format_duration(
            uptime_seconds
        ),
        "downtime": format_duration(
            downtime_seconds
        ),
        "uptime_seconds": int(
            uptime_seconds
        ),
        "downtime_seconds": int(
            downtime_seconds
        ),
        "uptime_percent": (
            uptime_seconds
            / total_seconds
            * 100
        ),
        "outages": outages,
        "average_voltage": average_voltage,
        "average_on_voltage": average_on_voltage,
        "max_voltage": (
            max(on_voltages)
            if on_voltages
            else 0.0
        ),
        "min_voltage": (
            min(on_voltages)
            if on_voltages
            else 0.0
        ),
        "low_voltage_events": low_voltage_events,
    }
