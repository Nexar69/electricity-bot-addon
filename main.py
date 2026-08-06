import asyncio
import json
import os
import threading
import time

import requests

from chart import create_statistics_chart
from history import calculate_statistics, get_history
from telegram import (
    InputMediaPhoto,
    ReplyKeyboardRemove,
)
from telegram.ext import Application

CONFIG_PATH = "/data/options.json"
STATE_PATH = "/data/dashboard_state.json"

with open(CONFIG_PATH, encoding="utf-8") as config_file:
    config = json.load(config_file)

BOT_TOKEN = config["telegram_token"]
CHAT_ID = int(config["chat_id"])
HA_TOKEN = config["ha_token"]

SENSOR = config.get(
    "voltage_sensor",
    "sensor.huawei_grid_voltage_l1",
)

OFF_VOLTAGE = float(config.get("off_voltage", 150))
LOW_VOLTAGE_THRESHOLD = float(config.get("low_voltage_threshold", 200))
HISTORY_HOURS = int(config.get("history_hours", 24))
DASHBOARD_UPDATE_MINUTES = max(
    1,
    int(config.get("dashboard_update_minutes", 5)),
)
OUTAGE_CONFIRMATION_SECONDS = max(
    1,
    int(config.get("outage_confirmation_seconds", 10)),
)
CHECK_INTERVAL_SECONDS = 5
HA_URL = "http://homeassistant:8123/api/states/"

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


def get_voltage() -> float:
    try:
        response = requests.get(
            HA_URL + SENSOR,
            headers=HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        state = str(response.json().get("state", "unknown")).strip().lower()
        if state in {"unavailable", "unknown", "none", ""}:
            return 0.0
        return float(state)
    except (requests.RequestException, ValueError, KeyError) as error:
        print("HA error:", repr(error))
        return 0.0


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} год {minutes} хв {remaining_seconds} с"
    if minutes:
        return f"{minutes} хв {remaining_seconds} с"
    return f"{remaining_seconds} с"


def build_status_text(voltage: float) -> str:
    timestamp = time.strftime("%d.%m.%Y %H:%M")
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
    timestamp = time.strftime("%d.%m.%Y %H:%M")
    return (
        f"📈 Статистика за останні {HISTORY_HOURS} год\n\n"
        f"🟢 З електропостачанням: {stats['uptime']}\n"
        f"🔴 Без електропостачання: {stats['downtime']}\n"
        f"📊 Доступність: {stats['uptime_percent']:.2f}%\n\n"
        f"🟢 Середня при наявності: {stats['average_on_voltage']:.1f} В\n"
        f"⬆ Максимальна: {stats['max_voltage']:.1f} В\n"
        f"⬇ Мінімальна: {stats['min_voltage']:.1f} В\n\n"
        f"🔌 Відключень: {stats['outages']}\n"
        f"⚠️ Подій низької напруги: {stats['low_voltage_events']}\n"
        f"🕒 Оновлено: {timestamp}"
    )


async def ensure_status_message(app: Application, state: dict) -> None:
    voltage = await asyncio.to_thread(get_voltage)
    text = build_status_text(voltage)
    message_id = state.get("status_message_id")

    if message_id:
        try:
            await app.bot.edit_message_text(
                chat_id=CHAT_ID,
                message_id=message_id,
                text=text,
            )
            return
        except Exception as error:
            print("Could not edit status dashboard:", repr(error))

    message = await app.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        disable_notification=True,
    )

    try:
        await app.bot.pin_chat_message(
            chat_id=CHAT_ID,
            message_id=message.message_id,
            disable_notification=True,
        )
    except Exception as error:
        print("Could not pin status dashboard:", repr(error))

    state["status_message_id"] = message.message_id
    save_dashboard_state(state)


async def ensure_chart_message(app: Application, state: dict) -> None:
    chart_path = None

    try:
        history = await asyncio.to_thread(
            get_history,
            HA_URL,
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

        caption = build_chart_caption(stats)
        message_id = state.get("chart_message_id")

        if message_id:
            try:
                with open(chart_path, "rb") as chart_file:
                    await app.bot.edit_message_media(
                        chat_id=CHAT_ID,
                        message_id=message_id,
                        media=InputMediaPhoto(
                            media=chart_file,
                            caption=caption,
                        ),
                    )
                return
            except Exception as error:
                print("Could not edit chart dashboard:", repr(error))

        with open(chart_path, "rb") as chart_file:
            message = await app.bot.send_photo(
                chat_id=CHAT_ID,
                photo=chart_file,
                caption=caption,
                disable_notification=True,
            )

        try:
            await app.bot.pin_chat_message(
                chat_id=CHAT_ID,
                message_id=message.message_id,
                disable_notification=True,
            )
        except Exception as error:
            print("Could not pin chart dashboard:", repr(error))

        state["chart_message_id"] = message.message_id
        save_dashboard_state(state)

    finally:
        if chart_path and os.path.exists(chart_path):
            try:
                os.remove(chart_path)
            except OSError as error:
                print("Could not remove temporary chart:", repr(error))


async def dashboard_loop(app: Application) -> None:
    state = load_dashboard_state()

    while True:
        try:
            await ensure_status_message(app, state)
            await ensure_chart_message(app, state)
        except Exception as error:
            print("Dashboard update error:", repr(error))

        await asyncio.sleep(DASHBOARD_UPDATE_MINUTES * 60)


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
        print("Telegram monitor error:", repr(error))


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
                outage_duration >= OUTAGE_CONFIRMATION_SECONDS
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
            outage_duration = int(time.time() - outage_start)

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


async def remove_old_reply_keyboard(
    app: Application,
) -> None:
    cleanup_message = await app.bot.send_message(
        chat_id=CHAT_ID,
        text="Оновлення меню…",
        reply_markup=ReplyKeyboardRemove(),
        disable_notification=True,
    )

    await asyncio.sleep(1)

    try:
        await app.bot.delete_message(
            chat_id=CHAT_ID,
            message_id=cleanup_message.message_id,
        )

    except Exception as error:
        print(
            "Could not delete keyboard cleanup message:",
            repr(error),
        )


async def post_init(
    app: Application,
) -> None:
    event_loop = asyncio.get_running_loop()

    threading.Thread(
        target=monitor,
        args=(
            app,
            event_loop,
        ),
        daemon=True,
        name="electricity-monitor",
    ).start()

    await remove_old_reply_keyboard(
        app
    )

    app.create_task(
        dashboard_loop(app)
    )

    print(
        "Electricity dashboard "
        "and monitoring started."
    )


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.run_polling(
        allowed_updates=[]
    )


if __name__ == "__main__":
    main()
