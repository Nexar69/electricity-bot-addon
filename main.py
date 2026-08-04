import time
import threading
import requests

from telegram import Update
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

OFF_VOLTAGE = config.get("off_voltage", 150)

HA_URL = "http://homeassistant:8123/api/states/"

headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

last_state = None
outage_start = None
outage_announced = False


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

    await update.message.reply_text(text)


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
                        )
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
                            )
                        )
                    except Exception as e:
                        print("Telegram error:", e)

                # Reset whether outage was announced or not
                outage_start = None
                outage_announced = False

        last_state = power
        time.sleep(5)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("status", status)
    )

    threading.Thread(
        target=monitor,
        args=(app,),
        daemon=True
    ).start()

    app.run_polling()


if __name__ == "__main__":
    main()
