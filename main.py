import asyncio
import json
import os
import threading
import time

import requests

from chart import create_statistics_chart
from history import (
    calculate_statistics,
    get_history,
)

from telegram import (
    Update,
)

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


CONFIG_PATH = "/data/options.json"


with open(
    CONFIG_PATH,
    encoding="utf-8",
) as config_file:
    config = json.load(config_file)


BOT_TOKEN = config["telegram_token"]
CHAT_ID = config["chat_id"]
HA_TOKEN = config["ha_token"]

SENSOR = config.get(
    "voltage_sensor",
    "sensor.huawei_grid_voltage_l1",
)

OFF_VOLTAGE = float(
    config.get(
        "off_voltage",
        150,
    )
)

LOW_VOLTAGE_THRESHOLD = float(
    config.get(
        "low_voltage_threshold",
        200,
    )
)

HISTORY_HOURS = int(
    config.get(
        "history_hours",
        24,
    )
)

AUTO_DELETE_SECONDS = int(
    config.get(
        "auto_delete_seconds",
        20,
    )
)

OUTAGE_CONFIRMATION_SECONDS = int(
    config.get(
        "outage_confirmation_seconds",
        10,
    )
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


def get_voltage() -> float:
    try:
        response = requests.get(
            HA_URL + SENSOR,
            headers=HEADERS,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        state = str(
            data.get(
                "state",
                "unknown",
            )
        ).strip().lower()

        if state in (
            "unavailable",
            "unknown",
            "none",
            "",
        ):
            return 0.0

        return float(state)

    except (
        requests.RequestException,
        ValueError,
        KeyError,
    ) as error:
        print(
            "HA error:",
            repr(error),
        )

        return 0.0


def format_duration(seconds: int) -> str:
    seconds = max(
        0,
        int(seconds),
    )

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, remaining_seconds = divmod(
        remainder,
        60,
    )

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


def is_group_chat(update: Update) -> bool:
    return update.effective_chat.type in (
        "group",
        "supergroup",
    )


async def delete_after_delay(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> None:
    await asyncio.sleep(
        AUTO_DELETE_SECONDS
    )

    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )

    except Exception as error:
        print(
            "Could not delete temporary message:",
            repr(error),
        )


def schedule_message_deletion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int,
) -> None:
    if not is_group_chat(update):
        return

    context.application.create_task(
        delete_after_delay(
            context,
            update.effective_chat.id,
            message_id,
        )
    )


async def delete_command_message(
    update: Update,
) -> None:
    if (
        not is_group_chat(update)
        or update.message is None
    ):
        return

    try:
        await update.message.delete()

    except Exception as error:
        print(
            "Could not delete command message:",
            repr(error),
        )


async def acknowledge_button(
    update: Update,
) -> None:
    if update.callback_query is None:
        return

    try:
        await update.callback_query.answer()

    except Exception as error:
        print(
            "Could not acknowledge callback:",
            repr(error),
        )


async def send_temporary_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
):
    message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        disable_notification=True,
    )

    schedule_message_deletion(
        update,
        context,
        message.message_id,
    )

    return message


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Do not create another menu.

    The group uses the already-pinned inline menu.
    """
    await delete_command_message(
        update
    )

    await send_temporary_message(
        update,
        context,
        (
            "📌 Використовуйте закріплене "
            "повідомлення з меню бота."
        ),
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await acknowledge_button(
        update
    )

    await delete_command_message(
        update
    )

    voltage = await asyncio.to_thread(
        get_voltage
    )

    if voltage >= OFF_VOLTAGE:
        text = (
            "🟢 Електропостачання відновлено\n\n"
            f"🔌 Напруга: {voltage:.1f} В"
        )

    else:
        text = (
            "🔴 Електропостачання відсутнє\n\n"
            f"🔌 Напруга: {voltage:.1f} В"
        )

    await send_temporary_message(
        update,
        context,
        text,
    )


async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await acknowledge_button(
        update
    )

    await delete_command_message(
        update
    )

    loading_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="📈 Створюю графік...",
        disable_notification=True,
    )

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
            await loading_message.edit_text(
                "⚠️ Не вдалося отримати історію "
                "з Home Assistant.\n\n"
                "Перевірте, чи Recorder зберігає "
                "історію цього сенсора."
            )

            schedule_message_deletion(
                update,
                context,
                loading_message.message_id,
            )

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

        caption = (
            f"📈 Статистика за останні "
            f"{HISTORY_HOURS} год\n\n"
            f"🟢 З електропостачанням: "
            f"{stats['uptime']}\n"
            f"🔴 Без електропостачання: "
            f"{stats['downtime']}\n"
            f"📊 Доступність: "
            f"{stats['uptime_percent']:.2f}%\n\n"
            f"🟢 Середня при наявності: "
            f"{stats['average_on_voltage']:.1f} В\n"
            f"⬆ Максимальна: "
            f"{stats['max_voltage']:.1f} В\n"
            f"⬇ Мінімальна: "
            f"{stats['min_voltage']:.1f} В\n\n"
            f"🔌 Відключень: "
            f"{stats['outages']}\n"
            f"⚠️ Подій низької напруги: "
            f"{stats['low_voltage_events']}"
        )

        try:
            await loading_message.delete()
        except Exception:
            pass

        with open(
            chart_path,
            "rb",
        ) as chart_file:
            chart_message = (
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=chart_file,
                    caption=caption,
                    disable_notification=True,
                )
            )

        schedule_message_deletion(
            update,
            context,
            chart_message.message_id,
        )

    except Exception as error:
        print(
            "Statistics chart error:",
            repr(error),
        )

        try:
            await loading_message.edit_text(
                "⚠️ Помилка під час створення графіка."
            )

            schedule_message_deletion(
                update,
                context,
                loading_message.message_id,
            )

        except Exception as edit_error:
            print(
                "Could not edit chart error message:",
                repr(edit_error),
            )

    finally:
        if (
            chart_path
            and os.path.exists(chart_path)
        ):
            try:
                os.remove(chart_path)

            except OSError as error:
                print(
                    "Could not remove temporary chart:",
                    repr(error),
                )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await acknowledge_button(
        update
    )

    await delete_command_message(
        update
    )

    await send_temporary_message(
        update,
        context,
        (
            "🤖 Бот контролю електропостачання\n\n"
            "🔌 Статус — поточний стан і напруга.\n"
            "📈 Статистика — PNG-графік та дані "
            f"за останні {HISTORY_HOURS} год.\n\n"
            "Для керування використовуйте "
            "закріплене повідомлення."
        ),
    )


async def handle_menu_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    if query.data == "status":
        await status(
            update,
            context,
        )

    elif query.data == "statistics":
        await statistics(
            update,
            context,
        )

    elif query.data == "help":
        await help_command(
            update,
            context,
        )

    else:
        await query.answer(
            "Невідома команда.",
            show_alert=False,
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

        future.result(
            timeout=20
        )

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

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

            continue

        if not power:
            if outage_start is None:
                outage_start = time.time()
                outage_announced = False

            outage_duration = (
                time.time()
                - outage_start
            )

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

        else:
            if outage_start is not None:
                outage_duration = int(
                    time.time()
                    - outage_start
                )

                if outage_announced:
                    duration_text = format_duration(
                        outage_duration
                    )

                    send_from_monitor(
                        app,
                        event_loop,
                        (
                            "🟢 Електропостачання відновлено\n\n"
                            "⏱ Тривалість відключення: "
                            f"{duration_text}\n"
                            f"🔌 Напруга: {voltage:.1f} В"
                        ),
                    )

                outage_start = None
                outage_announced = False

        last_state = power

        time.sleep(
            CHECK_INTERVAL_SECONDS
        )


async def post_init(
    app: Application,
) -> None:
    event_loop = asyncio.get_running_loop()

    monitoring_thread = threading.Thread(
        target=monitor,
        args=(
            app,
            event_loop,
        ),
        daemon=True,
        name="electricity-monitor",
    )

    monitoring_thread.start()

    print(
        "Electricity monitoring thread started."
    )


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "status",
            status,
        )
    )

    app.add_handler(
        CommandHandler(
            "statistics",
            statistics,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_menu_button,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
