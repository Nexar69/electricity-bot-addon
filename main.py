import time
import threading
import requests
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import os
import json

from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

CONFIG_PATH = "/data/options.json"

with open(CONFIG_PATH) as f:
    config = __import__("json").load(f)

BOT_TOKEN = config["telegram_token"]
CHAT_ID = config["chat_id"]
HA_TOKEN = config["ha_token"]

SENSOR = config.get(
    "voltage_sensor",
    "sensor.huawei_grid_voltage_l1"
)

OFF_VOLTAGE = config.get("off_voltage", 150)

HA_URL = "http://homeassistant:8123/api/states/"
HA_HISTORY_URL = "http://homeassistant:8123/api/history/period"

headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔌 Статус світла"],
        ["📈 Статистика", "ℹ️ Допомога"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

last_state = None
outage_start = None
outage_announced = False

def create_statistics_chart(times, voltages):

def get_voltage():
    try:
        r = requests.get(
            HA_URL + SENSOR,
            headers=headers,
            timeout=10
        )

        data = r.json()
        state = str(data["state"]).lower()

        if state in ("unavailable", "unknown", "none"):
            return 0.0

        return float(state)

    except Exception as e:
        print("HA error:", repr(e))
        return 0.0

def debug_history():
    try:
        url = (
            "http://homeassistant:8123/api/history/period"
            f"?filter_entity_id={SENSOR}"
        )

        r = requests.get(
            url,
            headers=headers,
            timeout=30,
        )

        print("\n========== HISTORY STATUS ==========")
        print("HTTP:", r.status_code)
        print("Headers:", r.headers)
        print("====================================")
        print(r.text)
        print("=========== END HISTORY ============\n")

    except Exception as e:
        print("History error:", repr(e))

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voltage = get_voltage()

    if voltage >= OFF_VOLTAGE:
        text = (
            "🟢 Електроенергія є\n\n"
            f"🔌 Напруга: {voltage:.1f} В"
        )
    else:
        text = (
            "🔴 Електроенергії немає\n\n"
            f"🔌 Напруга: {voltage:.1f} В"
        )

    status_message = await update.message.reply_text(
        text,
        reply_markup=KEYBOARD,
    )

    if update.effective_chat.type in ("group", "supergroup"):
        try:
            await update.message.delete()
        except Exception:
            pass

        context.application.create_task(
            delete_after_delay(
                context,
                update.effective_chat.id,
                status_message.message_id,
            )
        )


async def delete_after_delay(context, chat_id, message_id):
    import asyncio

    await asyncio.sleep(20)

    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        pass


def monitor(app):
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

            duration = time.time() - outage_start

            if duration >= 10 and not outage_announced:
                try:
                    app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=(
                            "🔴 ВІДКЛЮЧЕННЯ ЕЛЕКТРОЕНЕРГІЇ\n\n"
                            f"🔌 Напруга: {voltage:.1f} В"
                        ),
                        reply_markup=KEYBOARD,
                    )
                    outage_announced = True
                except Exception as e:
                    print("Telegram error:", e)

        else:
            if outage_start is not None:
                duration = int(time.time() - outage_start)

                if outage_announced:
                    hours = duration // 3600
                    minutes = (duration % 3600) // 60
                    seconds = duration % 60

                    if hours:
                        duration_text = f"{hours} год {minutes} хв {seconds} с"
                    elif minutes:
                        duration_text = f"{minutes} хв {seconds} с"
                    else:
                        duration_text = f"{seconds} с"

                    try:
                        app.bot.send_message(
                            chat_id=CHAT_ID,
                            text=(
                                "🟢 ЕЛЕКТРОЕНЕРГІЯ ПОВЕРНУЛАСЯ\n\n"
                                f"⏱ Тривалість відключення: {duration_text}\n"
                                f"🔌 Напруга: {voltage:.1f} В"
                            ),
                            reply_markup=KEYBOARD,
                        )
                    except Exception as e:
                        print("Telegram error:", e)

                outage_start = None
                outage_announced = False

        last_state = power
        time.sleep(5)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ Бот запущено!\n\n"
        "Використовуйте кнопки нижче.",
        reply_markup=KEYBOARD,
    )


async def keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔌 Статус світла":
        await status(update, context)

    elif text == "📈 Статистика":
        voltage = get_voltage()

        await update.message.reply_text(
        "📈 Генерую статистику...",
        reply_markup=KEYBOARD,
    )

    elif text == "ℹ️ Допомога":
        await update.message.reply_text(
            "🤖 Я повідомляю про відключення та "
            "відновлення електроенергії.\n\n"
            "• 🔌 Статус світла\n"
            "• 📊 Поточна напруга\n"
            "• /status",
            reply_markup=KEYBOARD,
        )


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            keyboard,
        )
    )

    threading.Thread(
        target=monitor,
        args=(app,),
        daemon=True,
    ).start()

    debug_history()
    
    app.run_polling()


if __name__ == "__main__":
    main()
