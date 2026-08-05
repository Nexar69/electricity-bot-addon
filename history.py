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
