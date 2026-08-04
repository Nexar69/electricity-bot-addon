import os
import time
import requests
import asyncio

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

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

OFF_VOLTAGE = config.get(
    "off_voltage",
    100
)

HA_URL = "http://homeassistant:8123/api/states/"


headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}


last_state = None
outage_start = None


def get_voltage():
    try:
        r = requests.get(
            HA_URL + SENSOR,
            headers=headers,
            timeout=10
        )

        data = r.json()
        return float(data["state"])

    except Exception as e:
        print("HA error:", e)
        return None


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voltage = get_voltage()

    if voltage is None:
        text = "❓ Не можу отримати дані"

    elif voltage < OFF_VOLTAGE:
        text = (
            "🔴 Електроенергії немає\n\n"
            f"🔌 Напруга: {voltage} В"
        )

    else:
        text = (
            "🟢 Електроенергія є\n\n"
            f"🔌 Напруга: {voltage} В"
        )

    await update.message.reply_text(text)


async def monitor(app):
    global last_state, outage_start

    while True:

        voltage = get_voltage()

        if voltage is not None:

            power = voltage >= OFF_VOLTAGE

            if last_state is None:
                last_state = power

            elif power != last_state:

                if not power:
                    outage_start = time.time()

                    await app.bot.send_message(
                        CHAT_ID,
                        "🔴 ВІДКЛЮЧЕННЯ ЕЛЕКТРОЕНЕРГІЇ\n\n"
                        f"🔌 Напруга: {voltage} В"
                    )

                else:
                    duration = ""

                    if outage_start:
                        seconds = int(
                            time.time() - outage_start
                        )

                        minutes = seconds // 60
                        hours = minutes // 60

                        minutes %= 60

                        if hours:
                            duration = f"{hours} год {minutes} хв"
                        else:
                            duration = f"{minutes} хв"

                    await app.bot.send_message(
                        CHAT_ID,
                        "🟢 ЕЛЕКТРОЕНЕРГІЯ ПОВЕРНУЛАСЯ\n\n"
                        f"⏱ Не було: {duration}\n"
                        f"🔌 Напруга: {voltage} В"
                    )

                    outage_start = None

                last_state = power

        await asyncio.sleep(5)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("status", status)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
