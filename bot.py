import os
import asyncio
import datetime
import logging
import csv
import io
import json
import urllib.request
import urllib.error
import requests
import discord
import pytz

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# --- Config from environment variables ---
DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
CHANNEL_ID      = int(os.environ["CHANNEL_ID"])
ANTHROPIC_KEY   = os.environ["ANTHROPIC_KEY"]
SHEET_ID        = "1DCF9ug2Xb4f_eV-rK5e5TfI0PLcmFIl-syHpca8RM1k"
SHEET_CSV_URL   = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&sheet=Sheet1"

CAIRO_TZ     = pytz.timezone("Africa/Cairo")
POST_HOUR    = 8
POST_MINUTE  = 0

REVIEW_INTERVALS = [0, 1, 3, 7, 30]

# Channel name → feature mapping (bot reads channel name from message)
FEATURE_CHANNELS = {
    "practice":      "practice",
    "homework-help": "homework",
    "exercises":     "exercises",
    "speaking-coach":"speaking",
}

# Per-user conversation history for #practice
conversation_histories = {}


# ── Google Sheets ────────────────────────────────────────────────────────────
def get_all_vocab():
    with urllib.request.urlopen(SHEET_CSV_URL) as response:
        content = response.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    return [row for row in reader if row.get("Word", "").strip()]


def get_todays_words():
    today = datetime.datetime.now(CAIRO_TZ).date()
    new_words, review_words = [], []
    for row in get_all_vocab():
        date_str = str(row.get("Date", "")).strip()
        if not date_str:
            continue
        try:
            word_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        delta = (today - word_date).days
        if delta == 0:
            new_words.append(row)
        elif delta in REVIEW_INTERVALS[1:]:
            review_words.append(row)
    return new_words, review_words


def build_vocab_context():
    vocab = get_all_vocab()
    if not vocab:
        return "No vocabulary learned yet."
    lines = []
    for v in vocab:
        lines.append(f"- {v['Word']} = {v['Translation']} (example: {v['Example']}) [Unit: {v['Unit']}]")
    return "\n".join(lines)


# ── Claude API ───────────────────────────────────────────────────────────────
def call_claude(system_prompt, messages, max_tokens=1024):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ── Feature handlers ─────────────────────────────────────────────────────────
async def handle_practice(message, vocab_context):
    user_id = message.author.id
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": message.content})

    # Keep last 20 messages to avoid token overflow
    history = conversation_histories[user_id][-20:]

    system = f"""You are a friendly Spanish conversation partner for an A1-level student.
STRICT RULES:
- Only use vocabulary and grammar the student has already learned (listed below)
- Keep sentences short and simple
- If the student makes a grammar mistake, gently correct it in your reply
- Respond mostly in Spanish but explain corrections in English
- Be encouraging and fun

LEARNED VOCABULARY:
{vocab_context}
"""
    reply = call_claude(system, history)
    conversation_histories[user_id].append({"role": "assistant", "content": reply})
    return reply


async def handle_homework(message, vocab_context):
    system = f"""You are a Spanish homework checker for an A1-level student.
- Check their answers carefully
- For wrong answers: explain WHY it's wrong in simple English, give the correct answer
- For correct answers: confirm and give a brief explanation of the rule
- Be encouraging
- Student's learned vocabulary for context: {vocab_context[:500]}
"""
    messages = [{"role": "user", "content": f"Please check my homework:\n\n{message.content}"}]
    return call_claude(system, messages)


async def handle_exercises(message, vocab_context):
    system = """You are a Spanish exercise generator for an A1-level student.
Generate exactly 10 exercises using ONLY the vocabulary provided.
Mix these types: fill-in-the-blank, translate to Spanish, translate to English, conjugate the verb.
Format each exercise clearly numbered 1-10.
Put answers at the bottom under a "─── ANSWERS ───" separator using spoiler tags: ||answer||
"""
    messages = [{"role": "user", "content": f"Generate 10 exercises using this vocabulary:\n{vocab_context}"}]
    return call_claude(system, messages, max_tokens=2000)


async def handle_speaking(message, vocab_context):
    system = f"""You are a Spanish speaking coach for an A1-level student.
The student will write a Spanish sentence or paragraph.
Your job:
1. Grade it: ✅ Correct / ⚠️ Minor errors / ❌ Major errors
2. Show the corrected version in bold
3. Explain each mistake in simple English
4. Give one tip to improve
Keep feedback short and encouraging.
Student's learned vocabulary: {vocab_context[:500]}
"""
    messages = [{"role": "user", "content": f"Please check my Spanish: {message.content}"}]
    return call_claude(system, messages)


# ── Daily vocab post ─────────────────────────────────────────────────────────
def format_word(row, is_review=False):
    label = "🔁 **Repaso**" if is_review else "🆕 **Palabra nueva**"
    return (
        f"{label} — {row['Unit']}\n"
        f"🇪🇸  **{row['Word']}**\n"
        f"🇬🇧  {row['Translation']}\n"
        f"💬 _{row['Example']}_"
    )


def format_daily_message(new_words, review_words):
    lines = ["📚 **Palabras del día** — ¡Practica usándolas hoy! 💪\n", "─" * 30]
    for row in new_words:
        lines += [format_word(row, False), "─" * 30]
    if review_words:
        lines += ["\n🔁 **Repaso de palabras anteriores:**\n", "─" * 30]
        for row in review_words:
            lines += [format_word(row, True), "─" * 30]
    return "\n".join(lines)


async def post_daily_word():
    await client.wait_until_ready()
    try:
        channel = await client.fetch_channel(CHANNEL_ID)
    except Exception as e:
        log.error("Channel %s not found: %s", CHANNEL_ID, e)
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
            new_words, review_words = get_todays_words()
            if new_words or review_words:
                msg = format_daily_message(new_words, review_words)
                await channel.send(msg)
                log.info("Posted %d new + %d review words.", len(new_words), len(review_words))
            else:
                log.warning("No vocab entries for today.")
        except Exception as exc:
            log.error("Failed to post word: %s", exc)

        await asyncio.sleep(60)


# ── Discord events ───────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)


@client.event
async def on_message(message):
    if message.author.bot:
        return

    channel_name = message.channel.name
    feature = FEATURE_CHANNELS.get(channel_name)
    if not feature:
        return

    async with message.channel.typing():
        try:
            vocab_context = build_vocab_context()

            if feature == "practice":
                reply = await handle_practice(message, vocab_context)
            elif feature == "homework":
                reply = await handle_homework(message, vocab_context)
            elif feature == "exercises":
                reply = await handle_exercises(message, vocab_context)
            elif feature == "speaking":
                reply = await handle_speaking(message, vocab_context)
            else:
                return

            # Split long replies
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i+1900])

        except Exception as e:
            log.error("Error handling message in %s: %s", channel_name, e)
            await message.channel.send("⚠️ Something went wrong. Try again in a moment.")


async def main():
    async with client:
        asyncio.get_event_loop().create_task(post_daily_word())
        await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
