import asyncio
import json
import threading
import time

import requests

from history import (
    calculate_statistics,
    get_history,
)

from telegram import (
    ReplyKeyboardMarkup,
    Update,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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


KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            "🔌 Статус світла",
        ],
        [
            "📈 Статистика",
            "ℹ️ Допомога",
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


last_state = None
outage_start = None
outage_announced = False


def get_voltage() -> float:
    """
    Get the current voltage from Home Assistant.

    unavailable and unknown states are treated as 0 V.
    """
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
    """
    Convert seconds to a Ukrainian duration string.
    """
    seconds = max(0, int(seconds))

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


async def delete_after_delay(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
) -> None:
    """
    Delete a Telegram message after the configured delay.
    """
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


async def delete_request_message(
    update: Update,
) -> None:
    """
    Delete the user's command or keyboard-button message in groups.
    """
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    try:
        await update.message.delete()

    except Exception as error:
        print(
            "Could not delete request message:",
            repr(error),
        )


def schedule_response_deletion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int,
) -> None:
    """
    Schedule a temporary bot response for deletion in groups.
    """
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    context.application.create_task(
        delete_after_delay(
            context,
            update.effective_chat.id,
            message_id,
        )
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Send the current electricity status silently.

    In groups:
    - delete the user's request;
    - delete the bot response after the configured delay.
    """
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

    await delete_request_message(
        update
    )

    response_message = (
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            disable_notification=True,
        )
    )

    schedule_response_deletion(
        update,
        context,
        response_message.message_id,
    )


async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Retrieve and display statistics for the configured history period.

    Routine statistics messages are sent silently.
    """
    await delete_request_message(
        update
    )

    loading_message = (
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📈 Отримую статистику...",
            disable_notification=True,
        )
    )

    try:
        history = await asyncio.to_thread(
            get_history,
            HA_URL,
            SENSOR,
            HEADERS,
            HISTORY_HOURS,
        )

        stats = await asyncio.to_thread(
            calculate_statistics,
            history,
            OFF_VOLTAGE,
            LOW_VOLTAGE_THRESHOLD,
        )

        if not history:
            text = (
                "⚠️ Не вдалося отримати історію "
                "з Home Assistant.\n\n"
                "Перевірте, чи Recorder зберігає "
                "історію цього сенсора."
            )

        else:
            text = (
                f"📈 Статистика за останні "
                f"{HISTORY_HOURS} год\n\n"

                f"🟢 Час з електропостачанням: "
                f"{stats['uptime']}\n"

                f"🔴 Час без електропостачання: "
                f"{stats['downtime']}\n"

                f"📊 Доступність: "
                f"{stats['uptime_percent']:.2f}%\n\n"

                f"⚡ Середня напруга: "
                f"{stats['average_voltage']:.1f} В\n"

                f"🟢 Середня при наявності: "
                f"{stats['average_on_voltage']:.1f} В\n"

                f"⬆ Максимальна напруга: "
                f"{stats['max_voltage']:.1f} В\n"

                f"⬇ Мінімальна напруга: "
                f"{stats['min_voltage']:.1f} В\n\n"

                f"🔌 Відключень: "
                f"{stats['outages']}\n"

                f"⚠️ Подій низької напруги: "
                f"{stats['low_voltage_events']}"
            )

        await loading_message.edit_text(
            text
        )

    except Exception as error:
        print(
            "Statistics error:",
            repr(error),
        )

        try:
            await loading_message.edit_text(
                "⚠️ Помилка під час отримання статистики."
            )

        except Exception as edit_error:
            print(
                "Could not edit statistics error message:",
                repr(edit_error),
            )

    schedule_response_deletion(
        update,
        context,
        loading_message.message_id,
    )


def send_from_monitor(
    app: Application,
    event_loop: asyncio.AbstractEventLoop,
    text: str,
) -> None:
    """
    Safely send a Telegram message from the monitoring thread.

    Outage and restoration alerts are intentionally not silent.
    """
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
    """
    Monitor voltage and announce confirmed outages and restorations.

    Interruptions shorter than OUTAGE_CONFIRMATION_SECONDS are ignored.
    """
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Show the permanent bottom keyboard.
    """
    await update.message.reply_text(
        "⚡ Бот запущено!\n\n"
        "Використовуйте кнопки нижче.",
        reply_markup=KEYBOARD,
        disable_notification=True,
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Display help and restore the persistent keyboard.
    """
    await update.message.reply_text(
        "🤖 Бот контролю електропостачання\n\n"
        "• 🔌 Статус світла\n"
        "• 📈 Статистика за останні 24 години\n"
        "• /status\n"
        "• /statistics\n\n"
        "Звичайні відповіді надсилаються без звуку.\n"
        "Повідомлення про відключення та "
        "відновлення залишаються зі звуком.",
        reply_markup=KEYBOARD,
        disable_notification=True,
    )


async def keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Handle persistent reply-keyboard buttons.
    """
    text = update.message.text

    if text == "🔌 Статус світла":
        await status(
            update,
            context,
        )

    elif text == "📈 Статистика":
        await statistics(
            update,
            context,
        )

    elif text == "ℹ️ Допомога":
        await help_command(
            update,
            context,
        )


async def post_init(
    app: Application,
) -> None:
    """
    Start the voltage-monitoring thread after Telegram initialization.
    """
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
            start,
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
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            keyboard,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
