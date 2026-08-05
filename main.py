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

HA_URL = (
    "http://homeassistant:8123/api/states/"
)

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

    except Exception:
        pass


async def schedule_temporary_deletion(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    response_message,
) -> None:
    if update.effective_chat.type not in (
        "group",
        "supergroup",
    ):
        return

    try:
        await update.message.delete()

    except Exception:
        pass

    context.application.create_task(
        delete_after_delay(
            context,
            update.effective_chat.id,
            response_message.message_id,
        )
    )


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    voltage = get_voltage()

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

    response_message = (
        await update.message.reply_text(
            text,
            reply_markup=KEYBOARD,
        )
    )

    await schedule_temporary_deletion(
        update,
        context,
        response_message,
    )


async def statistics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    loading_message = (
        await update.message.reply_text(
            "📈 Отримую статистику...",
            reply_markup=KEYBOARD,
        )
    )

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

    try:
        await loading_message.edit_text(
            text,
            reply_markup=KEYBOARD,
        )

        await schedule_temporary_deletion(
            update,
            context,
            loading_message,
        )

    except Exception as error:
        print(
            "Telegram statistics error:",
            repr(error),
        )


def monitor(app: Application) -> None:
    global last_state
    global outage_start
    global outage_announced

    while True:
        voltage = get_voltage()
        power = voltage >= OFF_VOLTAGE

        if last_state is None:
            last_state = power
            time.sleep(5)
            continue

        if not power:
            if outage_start is None:
                outage_start = time.time()
                outage_announced = False

            duration = (
                time.time()
                - outage_start
            )

            if (
                duration >= 10
                and not outage_announced
            ):
                try:
                    app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=(
                            "🔴 Електропостачання відсутнє\n\n"
                            f"🔌 Напруга: {voltage:.1f} В"
                        ),
                        reply_markup=KEYBOARD,
                    )

                    outage_announced = True

                except Exception as error:
                    print(
                        "Telegram error:",
                        repr(error),
                    )

        else:
            if outage_start is not None:
                duration = int(
                    time.time()
                    - outage_start
                )

                if outage_announced:
                    hours = duration // 3600
                    minutes = (
                        duration % 3600
                    ) // 60
                    seconds = duration % 60

                    if hours:
                        duration_text = (
                            f"{hours} год "
                            f"{minutes} хв "
                            f"{seconds} с"
                        )

                    elif minutes:
                        duration_text = (
                            f"{minutes} хв "
                            f"{seconds} с"
                        )

                    else:
                        duration_text = (
                            f"{seconds} с"
                        )

                    try:
                        app.bot.send_message(
                            chat_id=CHAT_ID,
                            text=(
                                "🟢 Електропостачання відновлено\n\n"
                                "⏱ Тривалість відключення: "
                                f"{duration_text}\n"
                                f"🔌 Напруга: {voltage:.1f} В"
                            ),
                            reply_markup=KEYBOARD,
                        )

                    except Exception as error:
                        print(
                            "Telegram error:",
                            repr(error),
                        )

                outage_start = None
                outage_announced = False

        last_state = power

        time.sleep(5)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.message.reply_text(
        "⚡ Бот запущено!\n\n"
        "Використовуйте кнопки нижче.",
        reply_markup=KEYBOARD,
    )


async def keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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
        await update.message.reply_text(
            "🤖 Бот контролю електропостачання\n\n"
            "• 🔌 Статус світла\n"
            "• 📈 Статистика за останні 24 години\n"
            "• /status\n"
            "• /statistics\n\n"
            "Бот автоматично повідомляє про "
            "відсутність і відновлення "
            "електропостачання.",
            reply_markup=KEYBOARD,
        )


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
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
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            keyboard,
        )
    )

    threading.Thread(
        target=monitor,
        args=(app,),
        daemon=True,
    ).start()

    app.run_polling()


if __name__ == "__main__":
    main()
