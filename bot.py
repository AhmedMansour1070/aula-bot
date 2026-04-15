import os
import asyncio
import datetime
import logging
import csv
import io
import urllib.request
import discord
import pytz

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# --- Config from environment variables ---
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
SHEET_ID = "1DCF9ug2Xb4f_eV-rK5e5TfI0PLcmFIl-syHpca8RM1k"
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&sheet=Sheet1"

CAIRO_TZ = pytz.timezone("Africa/Cairo")
POST_HOUR = 8
POST_MINUTE = 0


# --- Google Sheets helper (no credentials needed — sheet is public) ---
def get_todays_word():
    with urllib.request.urlopen(SHEET_CSV_URL) as response:
        content = response.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(content))
    today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")

    for row in reader:
        if str(row.get("Date", "")).strip() == today:
            return row
    return None


# --- Message formatter ---
def format_message(row: dict) -> str:
    return (
        f"📖 **Palabra del día** — {row['Unit']}\n\n"
        f"🇪🇸  **{row['Word']}**\n"
        f"🇬🇧  {row['Translation']}\n\n"
        f"💬 **Example:**\n{row['Example']}\n\n"
        f"¡Practica usándola hoy! 💪"
    )


# --- Discord bot ---
intents = discord.Intents.default()
client = discord.Client(intents=intents)


async def post_daily_word():
    """Wait until 08:00 Cairo time, then post; repeat every 24 h."""
    await client.wait_until_ready()
    try:
        channel = await client.fetch_channel(CHANNEL_ID)
    except Exception as e:
        log.error("Channel %s not found: %s. Check CHANNEL_ID and bot permissions.", CHANNEL_ID, e)
        return

    while not client.is_closed():
        now = datetime.datetime.now(CAIRO_TZ)
        target = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        log.info("Next post in %.0f seconds (at %s Cairo time).", wait_seconds, target.strftime("%Y-%m-%d %H:%M"))
        await asyncio.sleep(wait_seconds)

        try:
            row = get_todays_word()
            if row:
                msg = format_message(row)
                await channel.send(msg)
                log.info("Posted word: %s", row.get("Word"))
            else:
                log.warning("No vocab entry found for today (%s).", datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d"))
        except Exception as exc:
            log.error("Failed to post word: %s", exc)

        await asyncio.sleep(60)


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)


async def main():
    async with client:
        asyncio.get_event_loop().create_task(post_daily_word())
        await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
