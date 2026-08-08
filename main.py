import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from chart import create_statistics_chart
from history import calculate_statistics, get_history
from telegram import InputMediaPhoto
from telegram.ext import Application

CONFIG_PATH = "/data/options.json"
STATE_PATH = "/data/dashboard_state.json"

HA_BASE_URL = "http://homeassistant:8123"
HA_STATES_URL = f"{HA_BASE_URL}/api/states/"
HA_CONFIG_URL = f"{HA_BASE_URL}/api/config"

with open(CONFIG_PATH, encoding="utf-8") as config_file:
    config = json.load(config_file)

BOT_TOKEN = config["telegram_token"]
CHAT_ID = int(config["chat_id"])
HA_TOKEN = config["ha_token"]

SENSOR = config.get(
    "voltage_sensor",
    "sensor.huawei_grid_voltage_l3",
)

OFF_VOLTAGE = float(config.get("off_voltage", 150))
LOW_VOLTAGE_THRESHOLD = float(config.get("low_voltage_threshold", 200))
HISTORY_HOURS = int(config.get("history_hours", 24))
DASHBOARD_UPDATE_MINUTES = max(
    1,
    int(config.get("dashboard_update_minutes", 5)),
)
DASHBOARD_RESET_TIME = str(
    config.get("dashboard_reset_time", "08:00")
).strip()
OUTAGE_CONFIRMATION_SECONDS = max(
    1,
    int(config.get("outage_confirmation_seconds", 10)),
)
CHECK_INTERVAL_SECONDS = 5

HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

last_state = None
outage_start = None
outage_announced = False


def load_dashboard_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as state_file:
            data = json.load(state_file)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def save_dashboard_state(state: dict) -> None:
    temporary_path = f"{STATE_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file)
    os.replace(temporary_path, STATE_PATH)


def get_ha_timezone() -> ZoneInfo:
    try:
        response = requests.get(
            HA_CONFIG_URL,
            headers=HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        timezone_name = str(
            response.json().get("time_zone", "UTC")
        )
        return ZoneInfo(timezone_name)
    except Exception as error:
        print(
            "Could not get Home Assistant timezone:",
            repr(error),
        )
        return ZoneInfo("UTC")


HA_TIMEZONE = get_ha_timezone()


def now_ha() -> datetime:
    return datetime.now(HA_TIMEZONE)


def parse_reset_time() -> tuple[int, int]:
    try:
        hour_text, minute_text = DASHBOARD_RESET_TIME.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour, minute
    except (ValueError, AttributeError):
        print(
            "Invalid dashboard_reset_time. Using 08:00."
        )
        return 8, 0


RESET_HOUR, RESET_MINUTE = parse_reset_time()


def get_voltage() -> float:
    try:
        response = requests.get(
            HA_STATES_URL + SENSOR,
            headers=HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        state = str(
            response.json().get("state", "unknown")
        ).strip().lower()
        if state in {"unavailable", "unknown", "none", ""}:
            return 0.0
        return float(state)
    except (
        requests.RequestException,
        ValueError,
        KeyError,
    ) as error:
        print("HA error:", repr(error))
        return 0.0


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)

    if hours:
        return (
            f"{hours} год "
            f"{minutes} хв "
            f"{remaining_seconds} с"
        )
    if minutes:
        return (
            f"{minutes} хв "
            f"{remaining_seconds} с"
        )
    return f"{remaining_seconds} с"


def formatted_update_time() -> str:
    return now_ha().strftime("%d.%m.%Y %H:%M")


def build_status_text(voltage: float) -> str:
    timestamp = formatted_update_time()

    if voltage >= OFF_VOLTAGE:
        return (
            "🟢 Електропостачання відновлено\n\n"
            f"🔌 Напруга: {voltage:.1f} В\n"
            f"🕒 Оновлено: {timestamp}"
        )

    return (
        "🔴 Електропостачання відсутнє\n\n"
        f"🔌 Напруга: {voltage:.1f} В\n"
        f"🕒 Оновлено: {timestamp}"
    )


def build_chart_caption(stats: dict) -> str:
    timestamp = formatted_update_time()

    return (
        f"📈 Статистика за останні {HISTORY_HOURS} год\n\n"
        f"🟢 З електропостачанням: {stats['uptime']}\n"
        f"🔴 Без електропостачання: {stats['downtime']}\n"
        f"📊 Доступність: {stats['uptime_percent']:.2f}%\n\n"
        f"🔌 Відключень: {stats['outages']}\n"
        f"⚠️ Подій низької напруги: {stats['low_voltage_events']}\n"
        f"🕒 Оновлено: {timestamp}"
    )


async def create_status_message(
    app: Application,
    state: dict,
) -> None:
    voltage = await asyncio.to_thread(get_voltage)

    message = await app.bot.send_message(
        chat_id=CHAT_ID,
        text=build_status_text(voltage),
        disable_notification=True,
    )

    await app.bot.pin_chat_message(
        chat_id=CHAT_ID,
        message_id=message.message_id,
        disable_notification=True,
    )

    state["status_message_id"] = message.message_id
    save_dashboard_state(state)


async def create_chart_message(
    app: Application,
    state: dict,
) -> None:
    chart_path = None

    try:
        history = await asyncio.to_thread(
            get_history,
            HA_STATES_URL,
            SENSOR,
            HEADERS,
            HISTORY_HOURS,
        )

        if not history:
            print("No history available for chart dashboard.")
            return

        stats = await asyncio.to_thread(
            calculate_statistics,
            history,
            OFF_VOLTAGE,
            LOW_VOLTAGE_THRESHOLD,
        )

        chart_path = await asyncio.to_thread(
            create_statistics_chart,
            history,
            stats,
            OFF_VOLTAGE,
            LOW_VOLTAGE_THRESHOLD,
            HISTORY_HOURS,
        )

        with open(chart_path, "rb") as chart_file:
            message = await app.bot.send_photo(
                chat_id=CHAT_ID,
                photo=chart_file,
                caption=build_chart_caption(stats),
                disable_notification=True,
            )

        await app.bot.pin_chat_message(
            chat_id=CHAT_ID,
            message_id=message.message_id,
            disable_notification=True,
        )

        state["chart_message_id"] = message.message_id
        save_dashboard_state(state)

    finally:
        if chart_path and os.path.exists(chart_path):
            try:
                os.remove(chart_path)
            except OSError as error:
                print(
                    "Could not remove temporary chart:",
                    repr(error),
                )


async def ensure_dashboard_exists(
    app: Application,
    state: dict,
) -> None:
    if not state.get("status_message_id"):
        try:
            await create_status_message(app, state)
        except Exception as error:
            print(
                "Could not create status dashboard:",
                repr(error),
            )

    if not state.get("chart_message_id"):
        try:
            await create_chart_message(app, state)
        except Exception as error:
            print(
                "Could not create chart dashboard:",
                repr(error),
            )


async def update_status_message(
    app: Application,
    state: dict,
) -> None:
    message_id = state.get("status_message_id")
    if not message_id:
        return

    voltage = await asyncio.to_thread(get_voltage)

    try:
        await app.bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=message_id,
            text=build_status_text(voltage),
        )
    except Exception as error:
        print(
            "Could not edit status dashboard:",
            repr(error),
        )


async def update_chart_message(
    app: Application,
    state: dict,
) -> None:
    message_id = state.get("chart_message_id")
    if not message_id:
        return

    chart_path = None

    try:
        history = await asyncio.to_thread(
            get_history,
            HA_STATES_URL,
            SENSOR,
            HEADERS,
            HISTORY_HOURS,
        )

        if not history:
            print("No history available for chart update.")
            return

        stats = await asyncio.to_thread(
            calculate_statistics,
            history,
            OFF_VOLTAGE,
            LOW_VOLTAGE_THRESHOLD,
        )

        chart_path = await asyncio.to_thread(
            create_statistics_chart,
            history,
            stats,
            OFF_VOLTAGE,
            LOW_VOLTAGE_THRESHOLD,
            HISTORY_HOURS,
        )

        with open(chart_path, "rb") as chart_file:
            await app.bot.edit_message_media(
                chat_id=CHAT_ID,
                message_id=message_id,
                media=InputMediaPhoto(
                    media=chart_file,
                    caption=build_chart_caption(stats),
                ),
            )

    except Exception as error:
        print(
            "Could not edit chart dashboard:",
            repr(error),
        )

    finally:
        if chart_path and os.path.exists(chart_path):
            try:
                os.remove(chart_path)
            except OSError as error:
                print(
                    "Could not remove temporary chart:",
                    repr(error),
                )


async def delete_dashboard_messages(
    app: Application,
    state: dict,
) -> None:
    for key in (
        "status_message_id",
        "chart_message_id",
    ):
        message_id = state.get(key)

        if not message_id:
            continue

        try:
            await app.bot.delete_message(
                chat_id=CHAT_ID,
                message_id=message_id,
            )
        except Exception as error:
            print(
                f"Could not delete {key}:",
                repr(error),
            )

    state.pop("status_message_id", None)
    state.pop("chart_message_id", None)
    save_dashboard_state(state)


async def recreate_dashboard(
    app: Application,
    state: dict,
) -> None:
    print("Recreating dashboard at scheduled reset time.")

    await delete_dashboard_messages(app, state)

    try:
        await create_status_message(app, state)
    except Exception as error:
        print(
            "Could not recreate status dashboard:",
            repr(error),
        )

    try:
        await create_chart_message(app, state)
    except Exception as error:
        print(
            "Could not recreate chart dashboard:",
            repr(error),
        )


def next_reset_datetime() -> datetime:
    current = now_ha()

    scheduled = current.replace(
        hour=RESET_HOUR,
        minute=RESET_MINUTE,
        second=0,
        microsecond=0,
    )

    if scheduled <= current:
        scheduled += timedelta(days=1)

    return scheduled


async def dashboard_reset_loop(
    app: Application,
    state: dict,
) -> None:
    while True:
        reset_at = next_reset_datetime()

        delay = max(
            1.0,
            (reset_at - now_ha()).total_seconds(),
        )

        print(
            "Next dashboard reset:",
            reset_at.isoformat(),
        )

        await asyncio.sleep(delay)

        try:
            await recreate_dashboard(app, state)
        except Exception as error:
            print(
                "Dashboard reset error:",
                repr(error),
            )

        await asyncio.sleep(60)


async def dashboard_update_loop(
    app: Application,
    state: dict,
) -> None:
    await ensure_dashboard_exists(app, state)

    while True:
        try:
            await update_status_message(app, state)
            await update_chart_message(app, state)
        except Exception as error:
            print(
                "Dashboard update error:",
                repr(error),
            )

        await asyncio.sleep(
            DASHBOARD_UPDATE_MINUTES * 60
        )


def send_from_monitor(
    app: Application,
    event_loop: asyncio.AbstractEventLoop,
    text: str,
) -> None:
    try:
        future = asyncio.run_coroutine_threadsafe(
            app.bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                disable_notification=False,
            ),
            event_loop,
        )
        future.result(timeout=20)
    except Exception as error:
        print(
            "Telegram monitor error:",
            repr(error),
        )


def monitor(
    app: Application,
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    global last_state
    global outage_start
    global outage_announced

    while True:
        voltage = get_voltage()
        power = voltage >= OFF_VOLTAGE

        if last_state is None:
            last_state = power

            if not power:
                outage_start = time.time()
                outage_announced = False

            time.sleep(CHECK_INTERVAL_SECONDS)
            continue

        if not power:
            if outage_start is None:
                outage_start = time.time()
                outage_announced = False

            outage_duration = time.time() - outage_start

            if (
                outage_duration
                >= OUTAGE_CONFIRMATION_SECONDS
                and not outage_announced
            ):
                send_from_monitor(
                    app,
                    event_loop,
                    (
                        "🔴 Електропостачання відсутнє\n\n"
                        f"🔌 Напруга: {voltage:.1f} В"
                    ),
                )

                outage_announced = True

        elif outage_start is not None:
            outage_duration = int(
                time.time() - outage_start
            )

            if outage_announced:
                send_from_monitor(
                    app,
                    event_loop,
                    (
                        "🟢 Електропостачання відновлено\n\n"
                        "⏱ Тривалість відключення: "
                        f"{format_duration(outage_duration)}\n"
                        f"🔌 Напруга: {voltage:.1f} В"
                    ),
                )

            outage_start = None
            outage_announced = False

        last_state = power
        time.sleep(CHECK_INTERVAL_SECONDS)


async def run_bot() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    await app.initialize()
    await app.start()

    event_loop = asyncio.get_running_loop()

    monitoring_thread = threading.Thread(
        target=monitor,
        args=(app, event_loop),
        daemon=True,
        name="electricity-monitor",
    )

    monitoring_thread.start()

    state = load_dashboard_state()

    update_task = asyncio.create_task(
        dashboard_update_loop(app, state)
    )

    reset_task = asyncio.create_task(
        dashboard_reset_loop(app, state)
    )

    print("Electricity dashboard started.")
    print(
        "Home Assistant timezone:",
        str(HA_TIMEZONE),
    )
    print(
        "Dashboard reset time:",
        DASHBOARD_RESET_TIME,
    )

    try:
        await asyncio.Event().wait()
    finally:
        update_task.cancel()
        reset_task.cancel()

        await app.stop()
        await app.shutdown()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
