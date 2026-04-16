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
CONTEXT_URL = "https://raw.githubusercontent.com/AhmedMansour1070/aula-bot/main/context.json"

FEATURE_CHANNELS = {
    "practice":      "practice",
    "homework-help": "homework",
    "exercises":     "exercises",
    "speaking-coach":"speaking",
}

conversation_histories = {}


# ── Context from GitHub ──────────────────────────────────────────────────────
def get_course_context():
    try:
        resp = requests.get(CONTEXT_URL, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def build_vocab_context(context=None):
    if context and context.get("all_vocab"):
        lines = []
        for v in context["all_vocab"]:
            lines.append(f"- {v['word']} = {v['translation']} (example: {v.get('example','')}) [{v.get('session','')}]")
        return "\n".join(lines)
    # Fallback to Google Sheet
    with urllib.request.urlopen(SHEET_CSV_URL) as response:
        content = response.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    rows = [row for row in reader if row.get("Word", "").strip()]
    if not rows:
        return "No vocabulary learned yet."
    return "\n".join(f"- {r['Word']} = {r['Translation']} (example: {r['Example']})" for r in rows)


def build_grammar_context(context=None):
    if not context or not context.get("all_grammar"):
        return ""
    lines = []
    for g in context["all_grammar"]:
        forms = ", ".join(g.get("forms", []))
        lines.append(f"- {g['rule']}: {forms}")
    return "\n".join(lines)


def build_homework_context(context=None):
    if not context or not context.get("sessions"):
        return "No homework found."
    last = context["sessions"][-1]
    lines = [f"Session: {last['session']}"]
    for h in last.get("homework", []):
        lines.append(f"- {h}")
    return "\n".join(lines)


# ── Google Sheets (for daily vocab post) ────────────────────────────────────
def get_all_vocab_sheet():
    with urllib.request.urlopen(SHEET_CSV_URL) as response:
        content = response.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    return [row for row in reader if row.get("Word", "").strip()]


def get_todays_words():
    today = datetime.datetime.now(CAIRO_TZ).date()
    new_words, review_words = [], []
    for row in get_all_vocab_sheet():
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


# ── Claude API ───────────────────────────────────────────────────────────────
def call_claude(system_prompt, messages, max_tokens=1024):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    if not resp.ok:
        log.error("Anthropic error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ── Feature handlers ─────────────────────────────────────────────────────────
async def handle_practice(message, context):
    user_id = message.author.id
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": message.content})
    history = conversation_histories[user_id][-20:]

    vocab = build_vocab_context(context)
    grammar = build_grammar_context(context)
    last_session = context.get("last_session", "unknown") if context else "unknown"

    system = f"""You are a friendly Spanish conversation partner for an A1-level student.
They are currently on {last_session} of their course.

STRICT RULES:
- ONLY use vocabulary and grammar structures the student has learned (listed below)
- Keep sentences short and simple
- If the student makes a grammar mistake, gently correct it in your reply
- Respond mostly in Spanish but explain corrections in English
- Be encouraging and fun
- Never use vocabulary not in the list below

LEARNED VOCABULARY:
{vocab}

LEARNED GRAMMAR STRUCTURES:
{grammar}
"""
    reply = call_claude(system, history)
    conversation_histories[user_id].append({"role": "assistant", "content": reply})
    return reply


async def handle_homework(message, context):
    homework = build_homework_context(context)
    last_session = context.get("last_session", "unknown") if context else "unknown"

    system = f"""You are a Spanish homework checker for an A1-level student on {last_session}.

The actual homework assigned was:
{homework}

Your job:
- If the student pastes their answers, check them against the homework tasks above
- For wrong answers: explain WHY it's wrong in simple English, give the correct answer
- For correct answers: confirm and briefly explain the rule
- If they ask for help understanding a homework task, explain it clearly
- Be encouraging and specific to their actual assignments
"""
    messages = [{"role": "user", "content": message.content}]
    return call_claude(system, messages)


async def handle_exercises(message, context):
    vocab = build_vocab_context(context)
    grammar = build_grammar_context(context)

    system = f"""You are a Spanish exercise generator for an A1-level student.
Generate exactly 10 exercises using ONLY the vocabulary and grammar below.
Mix these types: fill-in-the-blank, translate to Spanish, translate to English, conjugate the verb.
Format each exercise clearly numbered 1-10.
Put answers at the bottom under a "─── ANSWERS ───" separator using spoiler tags: ||answer||
Adjust difficulty and focus based on the student's request.

VOCABULARY TO USE:
{vocab}

GRAMMAR STRUCTURES TO USE:
{grammar}
"""
    prompt = f"Student request: {message.content.strip()}\n\nGenerate 10 exercises based on the above."
    messages = [{"role": "user", "content": prompt}]
    return call_claude(system, messages, max_tokens=2000)


async def handle_speaking(message, context):
    vocab = build_vocab_context(context)
    grammar = build_grammar_context(context)

    system = f"""You are a Spanish speaking coach for an A1-level student.
The student will write a Spanish sentence or paragraph.

Your job:
1. Grade it: ✅ Correct / ⚠️ Minor errors / ❌ Major errors
2. Show the corrected version in bold
3. Explain each mistake in simple English, referencing the grammar rules they've learned
4. Give one actionable tip to improve

Only flag mistakes related to grammar and vocabulary they've already studied:
LEARNED VOCABULARY:
{vocab}

LEARNED GRAMMAR:
{grammar}
"""
    messages = [{"role": "user", "content": f"Please check my Spanish:\n{message.content}"}]
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
            context = get_course_context()

            if feature == "practice":
                reply = await handle_practice(message, context)
            elif feature == "homework":
                reply = await handle_homework(message, context)
            elif feature == "exercises":
                reply = await handle_exercises(message, context)
            elif feature == "speaking":
                reply = await handle_speaking(message, context)
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
