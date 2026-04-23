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
import fitz  # pymupdf

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

APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbzOAOdJRzhmDqSMEtBclPgw9nDLrs-sqxzIyjApRvOM8QoWYNSlcXABhXRgVStusK_iRA/exec"
APPS_SCRIPT_KEY = "aulabot2026"

DAILY_LIMIT      = 25
USAGE_FILE       = "usage.json"
STREAKS_FILE     = "streaks.json"
_raw_admins      = os.environ.get("BOT_ADMINS", "")
ADMIN_IDS        = set(int(x) for x in _raw_admins.split(",") if x.strip().isdigit())
STREAKS_CHANNEL  = "streaks"

TEXTBOOK_LFS_URL = "https://media.githubusercontent.com/media/AhmedMansour1070/aula-bot/main/Aula%20Internacional%20Plus%201%20(A1).pdf"
TEXTBOOK_PATH    = "textbook.pdf"

conversation_histories = {}
study_sessions = {}  # user_id -> {pages: [...], current: 0, history: []}


# ── Textbook PDF ─────────────────────────────────────────────────────────────
def ensure_textbook():
    if os.path.exists(TEXTBOOK_PATH):
        return True
    log.info("Downloading textbook PDF from GitHub LFS...")
    try:
        resp = requests.get(TEXTBOOK_LFS_URL, timeout=60, stream=True)
        resp.raise_for_status()
        with open(TEXTBOOK_PATH, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        log.info("Textbook downloaded (%.1f MB).", os.path.getsize(TEXTBOOK_PATH) / 1e6)
        return True
    except Exception as e:
        log.error("Failed to download textbook: %s", e)
        return False


def extract_pages(start: int, end: int) -> dict[int, str]:
    """Extract text from printed page numbers start–end. Returns {page_num: text}."""
    doc = fitz.open(TEXTBOOK_PATH)
    results = {}
    page_number_pattern = __import__("re").compile(r'\b(' + '|'.join(str(p) for p in range(start, end + 1)) + r')\b')

    for pdf_page in doc:
        text = pdf_page.get_text()
        # Check last 150 chars for printed page number
        tail = text[-150:]
        match = page_number_pattern.search(tail)
        if match:
            pnum = int(match.group(1))
            if pnum not in results:
                results[pnum] = text.strip()
    doc.close()
    return results


# ── Usage tracking ───────────────────────────────────────────────────────────
def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_usage(data):
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)


def load_streaks():
    try:
        with open(STREAKS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_streaks(data):
    with open(STREAKS_FILE, "w") as f:
        json.dump(data, f)


def record_practice(user_id: int, name: str):
    """Record that a user practiced today and update their streak."""
    today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now(CAIRO_TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    data = load_streaks()
    key = str(user_id)
    entry = data.get(key, {"name": name, "streak": 0, "last_date": "", "best_streak": 0})
    entry["name"] = name

    if entry["last_date"] == today:
        # Already recorded today, nothing to change
        data[key] = entry
        save_streaks(data)
        return

    if entry["last_date"] == yesterday:
        entry["streak"] += 1
    else:
        entry["streak"] = 1  # reset streak

    entry["last_date"] = today
    entry["best_streak"] = max(entry.get("best_streak", 0), entry["streak"])
    data[key] = entry
    save_streaks(data)


def get_streak(user_id: int) -> int:
    today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now(CAIRO_TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    data = load_streaks()
    entry = data.get(str(user_id), {})
    if entry.get("last_date") in (today, yesterday):
        return entry.get("streak", 0)
    return 0


def check_and_increment(user_id: int) -> tuple[bool, int]:
    """Returns (allowed, count_after_increment). Resets if date changed."""
    today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    data = load_usage()
    key = str(user_id)
    entry = data.get(key, {})

    if entry.get("date") != today:
        entry = {"date": today, "count": 0, "name": entry.get("name", "")}

    if entry["count"] >= DAILY_LIMIT:
        save_usage(data)
        return False, entry["count"]

    entry["count"] += 1
    data[key] = entry
    save_usage(data)
    return True, entry["count"]


def record_username(user_id: int, name: str):
    data = load_usage()
    key = str(user_id)
    if key in data:
        data[key]["name"] = name
        save_usage(data)


def build_streak_board() -> str:
    data = load_streaks()
    today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.datetime.now(CAIRO_TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    if not data:
        return "🔥 **Streak Board** — No streaks yet. Start practicing!\n"

    entries = []
    for uid, entry in data.items():
        last = entry.get("last_date", "")
        streak = entry.get("streak", 0) if last in (today, yesterday) else 0
        best = entry.get("best_streak", 0)
        entries.append((entry.get("name", f"User {uid}"), streak, best))

    entries.sort(key=lambda x: x[1], reverse=True)

    lines = ["🔥 **Streak Board** — Keep the flame alive!\n", "```"]
    lines.append(f"{'Name':<20} {'Streak':<10} {'Best':<8} {'Visual'}")
    lines.append("─" * 55)
    for name, streak, best in entries:
        if streak >= 30:
            flame = "🏆"
        elif streak >= 14:
            flame = "🔥🔥🔥"
        elif streak >= 7:
            flame = "🔥🔥"
        elif streak >= 1:
            flame = "🔥"
        else:
            flame = "💤"
        fire_bar = "█" * min(streak, 20) + ("░" * (20 - min(streak, 20)))
        lines.append(f"{name:<20} {str(streak) + ' days':<10} {str(best) + ' days':<8} {fire_bar} {flame}")
    lines.append("```")
    lines.append(f"_Updated: {today} • Practice in any AI channel to keep your streak!_")
    return "\n".join(lines)


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
    lines = [f"Session: {last['session']}", "Homework assigned:"]
    for h in last.get("homework", []):
        lines.append(f"- {h}")
    pages = last.get("homework_pages", {})
    if pages:
        lines.append("\nActual textbook exercise content:")
        for page_num, text in pages.items():
            lines.append(f"\n--- Page {page_num} ---\n{text[:2000]}")
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


# ── Study session ────────────────────────────────────────────────────────────
STUDY_SYSTEM = """You are an expert Spanish language tutor teaching an A1-level student directly from their textbook pages.

You have the full content of the current page(s) below. Your job is to TEACH everything on these pages like a real teacher would — not just summarize.

HOW TO TEACH:
- **Vocabulary lists**: teach each word with pronunciation hint, meaning, a memory trick if possible, and use it in a sentence. Then quiz the student on them.
- **Grammar rules**: explain the rule clearly in English with a simple analogy, show the full conjugation table or pattern, give 2-3 examples, then drill the student with exercises.
- **Exercises on the page**: actually run the exercise with the student. Show them the task, let them answer each item one by one, then correct and explain.
- **Dialogues**: read through the dialogue, explain new phrases, ask comprehension questions, then have the student practice parts of it.
- **Images/visual content**: if the text hints at images (labels, descriptions), explain what they likely show and teach the vocabulary around them.
- **Mixed pages**: break the page into sections and teach each section before moving on.

INTERACTION RULES:
- Never dump everything at once. Teach in small chunks, then pause and interact.
- After each chunk: ask a question, run a mini exercise, or ask them to translate something.
- Wait for the student's response before continuing.
- When they answer: give detailed feedback — what was right, what was wrong, WHY, and the correct version.
- Be encouraging but honest. Don't just say "great job" if the answer was wrong.
- Use formatting: bold for Spanish words, code blocks for conjugation tables, ✅ for correct, ❌ for wrong.
- Track what you've covered on this page. When everything is taught and practiced, tell them to type !next for the next page.
- If they type !next before finishing: briefly summarize what's left and ask if they want to skip or continue.

COMMANDS the student can use:
- !next → move to next page
- !stop → end session
- !repeat → re-explain the last concept differently
- !harder → make exercises harder
- !easier → simplify the explanation
"""

async def handle_study_message(message):
    user_id = message.author.id
    session = study_sessions.get(user_id)
    content = message.content.strip()

    # End session
    if content.lower() == "!stop":
        study_sessions.pop(user_id, None)
        await message.channel.send("📕 Study session ended. ¡Buen trabajo! 🇪🇸")
        return

    # Start new session: !study 45-50
    if content.lower().startswith("!study "):
        parts = content[7:].strip().split("-")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            await message.channel.send("Usage: `!study 45-50`")
            return
        start, end = int(parts[0]), int(parts[1])
        if end - start > 20:
            await message.channel.send("⚠️ Maximum 20 pages per session. Try a smaller range.")
            return

        await message.channel.send("📖 Loading textbook... give me a moment.")
        async with message.channel.typing():
            if not ensure_textbook():
                await message.channel.send("⚠️ Could not load the textbook. Try again later.")
                return

            pages = extract_pages(start, end)
            if not pages:
                await message.channel.send(f"⚠️ Could not find pages {start}–{end} in the textbook. Check the page numbers.")
                return

            sorted_pages = sorted(pages.items())
            # Build full content string for context
            full_content = "\n\n".join(
                f"=== PAGE {pnum} ===\n{text}" for pnum, text in sorted_pages
            )
            study_sessions[user_id] = {
                "pages": sorted_pages,
                "current": 0,
                "full_content": full_content,
                "history": []
            }
            session = study_sessions[user_id]

            # Initial teaching prompt
            page_num, page_text = sorted_pages[0]
            system = STUDY_SYSTEM + f"\n\nALL PAGES IN THIS SESSION:\n{full_content[:6000]}"
            opening = (
                f"The student just started studying pages {start}–{end}. "
                f"Begin by giving a one-line overview of what these pages cover, "
                f"then start teaching page {page_num} from the very beginning. "
                f"Teach the first concept/section only, then interact."
            )
            history = [{"role": "user", "content": opening}]
            reply = call_claude(system, history, max_tokens=800)
            history.append({"role": "assistant", "content": reply})
            session["history"] = history
            session["system"] = system

            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i+1900])
        return

    # Continue existing session
    if not session:
        return

    pages = session["pages"]
    idx = session["current"]

    if idx >= len(pages):
        study_sessions.pop(user_id, None)
        await message.channel.send("🎉 You've completed all the pages! ¡Excelente trabajo! Type `!study X-Y` to start a new session.")
        return

    page_num = pages[idx][0]
    history = session["history"]
    system = session["system"]

    # Handle !next
    if content.lower() == "!next":
        session["current"] += 1
        idx = session["current"]
        if idx >= len(pages):
            study_sessions.pop(user_id, None)
            await message.channel.send("🎉 You've completed all the pages! ¡Excelente trabajo!")
            return
        page_num, page_text = pages[idx]
        history.append({"role": "user", "content": f"!next — move to page {page_num}."})
        prompt_note = f"The student moved to page {page_num}. Start teaching this page from the beginning."
        history.append({"role": "user", "content": prompt_note})
    elif content.lower() == "!repeat":
        history.append({"role": "user", "content": "Please explain that last concept again but differently — use a different example or analogy."})
    elif content.lower() == "!harder":
        history.append({"role": "user", "content": "Make the exercises harder from now on."})
    elif content.lower() == "!easier":
        history.append({"role": "user", "content": "Simplify your explanations — I'm finding this difficult."})
    else:
        history.append({"role": "user", "content": content})

    async with message.channel.typing():
        reply = call_claude(system, history[-20:], max_tokens=800)
        history.append({"role": "assistant", "content": reply})
        session["history"] = history

        for i in range(0, len(reply), 1900):
            await message.channel.send(reply[i:i+1900])


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


async def update_streak_board():
    """Updates the streak board message in #streaks channel every day after the daily word post."""
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.datetime.now(CAIRO_TZ)
        # Post streak board 1 minute after daily word (8:01 AM)
        target = now.replace(hour=POST_HOUR, minute=POST_MINUTE + 1, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        try:
            streaks_ch = discord.utils.get(client.get_all_channels(), name=STREAKS_CHANNEL)
            if not streaks_ch:
                log.warning("No #streaks channel found.")
                await asyncio.sleep(60)
                continue

            board = build_streak_board()

            # Find existing bot message to edit, otherwise post new
            async for msg in streaks_ch.history(limit=20):
                if msg.author == client.user and "Streak Board" in msg.content:
                    await msg.edit(content=board)
                    log.info("Streak board updated.")
                    break
            else:
                await streaks_ch.send(board)
                log.info("Streak board posted.")
        except Exception as e:
            log.error("Streak board error: %s", e)

        await asyncio.sleep(60)


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
intents.members = True
client = discord.Client(intents=intents)


async def handle_addword(message, context):
    """!addword <spanish word> — auto-translates, generates example, adds to sheet."""
    word = message.content[len("!addword"):].strip()
    if not word:
        await message.channel.send("Usage: `!addword <spanish word>`")
        return

    async with message.channel.typing():
        last_session = context.get("last_session", "CLASE.1") if context else "CLASE.1"

        # Use Claude to translate and generate example
        system = "You are a Spanish language assistant. Return ONLY valid JSON, no extra text."
        prompt = f"""For the Spanish word "{word}", return this JSON:
{{
  "word": "{word}",
  "translation": "English translation here",
  "example": "A simple example sentence in Spanish using this word"
}}"""
        result_text = call_claude(system, [{"role": "user", "content": prompt}], max_tokens=200)

        try:
            if "```" in result_text:
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
            word_data = json.loads(result_text.strip())
        except Exception:
            await message.channel.send(f"⚠️ Could not process `{word}`. Try again.")
            return

        today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
        payload = {
            "key": APPS_SCRIPT_KEY,
            "action": "add_word",
            "date": today,
            "word": word_data["word"],
            "translation": word_data["translation"],
            "example": word_data["example"],
            "unit": last_session
        }

        try:
            resp = requests.post(APPS_SCRIPT_URL, json=payload, timeout=15)
            resp.raise_for_status()
            await message.channel.send(
                f"✅ **Added to your vocab sheet!**\n"
                f"🇪🇸 **{word_data['word']}** → 🇬🇧 {word_data['translation']}\n"
                f"💬 _{word_data['example']}_"
            )
        except Exception as e:
            log.error("Sheet write error: %s", e)
            await message.channel.send("⚠️ Word processed but failed to write to sheet.")


@client.event
async def on_member_join(member):
    try:
        embed = discord.Embed(
            title="👋 ¡Bienvenido/a a Aula Española!",
            description="Your personal AI-powered Spanish learning hub. Here's everything you need to know to get started.",
            color=0xFF4500  # Spanish red
        )

        embed.set_thumbnail(url="https://upload.wikimedia.org/wikipedia/en/thumb/9/9a/Flag_of_Spain.svg/200px-Flag_of_Spain.svg.png")

        embed.add_field(
            name="📅 Daily Practice",
            value=(
                "**#vocab-of-the-day**\n"
                "New words every day at 8 AM Cairo time, with smart spaced repetition so you never forget."
            ),
            inline=False
        )

        embed.add_field(
            name="🤖 AI Spanish Tutor",
            value=(
                "**#practice** — Chat in Spanish. The AI only uses words *you've learned*.\n"
                "**#speaking-coach** — Write a sentence, get instant grading & corrections.\n"
                "**#homework-help** — Paste your answers, AI checks them against real textbook exercises.\n"
                "**#exercises** — Type any request: `hard conjugation drills`, `translate to Spanish`, etc."
            ),
            inline=False
        )

        embed.add_field(
            name="📚 Auto-Updated After Each Class",
            value=(
                "**#session-summary** — Topics, vocab & grammar from class\n"
                "**#homework** — Exactly what was assigned\n"
                "**#next-session** — What to prepare before next class"
            ),
            inline=False
        )

        embed.add_field(
            name="📖 Resources",
            value=(
                "**#grammar-reference** — Conjugation tables & grammar rules\n"
                "**#useful-links** — Videos & websites from your teacher"
            ),
            inline=False
        )

        embed.add_field(
            name="🔥 Streaks & Community",
            value=(
                "**#streaks** — Live leaderboard updated daily. Practice every day to keep your streak!\n"
                "**#introductions** — Tell us who you are and why you're learning Spanish\n"
                "**#progress-sharing** — Share your wins, no matter how small\n"
                "**#questions** — Ask anything about Spanish"
            ),
            inline=False
        )

        embed.add_field(
            name="⌨️ Commands",
            value=(
                "`!define <word>` — look up any Spanish word (or translate if not in bank)\n"
                "`!addword <word>` — add a word to the class word bank\n"
                "`!streak` — see your current practice streak\n"
                "`!usage` — see how many AI messages you have left today"
            ),
            inline=False
        )

        embed.add_field(
            name="💡 Tips",
            value=(
                "• The AI only knows what **you've studied** — no overwhelm\n"
                "• Use **#practice** daily, even 5 minutes makes a difference\n"
                "• All channels update automatically after every class"
            ),
            inline=False
        )

        embed.set_footer(text="¡Buena suerte! 🇪🇸 • Aula Española Bot")

        await member.send(embed=embed)
        log.info("Sent welcome embed to %s", member.name)
    except Exception as e:
        log.warning("Could not DM %s: %s", member.name, e)


@client.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", client.user, client.user.id)


@client.event
async def on_message(message):
    if message.author.bot:
        return

    # !usage — personal or admin overview
    if message.content.strip().lower() in ("!usage", "!usage all"):
        today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
        data = load_usage()
        show_all = message.content.strip().lower() == "!usage all"

        if show_all:
            if ADMIN_IDS and message.author.id not in ADMIN_IDS:
                await message.channel.send("⛔ Only admins can see everyone's usage.")
                return
            if not data:
                await message.channel.send("📊 No usage recorded yet.")
                return
            lines = ["📊 **Daily usage — today**\n"]
            for uid, entry in data.items():
                count = entry.get("count", 0) if entry.get("date") == today else 0
                name = entry.get("name") or f"User {uid}"
                bar = "█" * count + "░" * (DAILY_LIMIT - count)
                lines.append(f"`{name:<20}` {bar} {count}/{DAILY_LIMIT}")
            await message.channel.send("\n".join(lines))
        else:
            key = str(message.author.id)
            entry = data.get(key, {})
            count = entry.get("count", 0) if entry.get("date") == today else 0
            remaining = DAILY_LIMIT - count
            await message.channel.send(
                f"📊 **Your usage today:** {count}/{DAILY_LIMIT} messages used  "
                f"({remaining} remaining)"
            )
        return

    # !study — interactive textbook study session (must intercept before feature channels)
    cmd = message.content.strip().lower()
    if (cmd.startswith("!study") or
        cmd in ("!next", "!stop", "!repeat", "!harder", "!easier") or
        message.author.id in study_sessions):
        await handle_study_message(message)
        return

    # !streak — show personal streak
    if message.content.strip().lower() == "!streak":
        today = datetime.datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
        yesterday = (datetime.datetime.now(CAIRO_TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        data = load_streaks()
        entry = data.get(str(message.author.id), {})
        streak = entry.get("streak", 0) if entry.get("last_date") in (today, yesterday) else 0
        best = entry.get("best_streak", 0)
        flames = "🔥" * min(streak, 10) if streak > 0 else "💤"
        await message.channel.send(
            f"**{message.author.display_name}'s streak**\n"
            f"{flames}\n"
            f"Current: **{streak} day{'s' if streak != 1 else ''}** | Best: **{best} day{'s' if best != 1 else ''}**"
        )
        return

    # !define — look up a word in the bank, or translate if not found
    if message.content.lower().startswith("!define "):
        word = message.content[8:].strip()
        context = get_course_context()
        all_vocab = context.get("all_vocab", []) if context else []

        # Search word bank
        match = next((v for v in all_vocab if v["word"].lower() == word.lower()), None)

        async with message.channel.typing():
            if match:
                await message.channel.send(
                    f"📖 **{match['word']}**\n"
                    f"🇬🇧 {match['translation']}\n"
                    f"💬 _{match.get('example', 'No example available')}_\n"
                    f"📚 From: {match.get('session', 'unknown')}"
                )
            else:
                # Not in bank — ask Claude to translate + give examples
                system = "You are a Spanish language assistant. Return ONLY valid JSON, no extra text."
                prompt = f"""For the Spanish word or phrase "{word}", return this JSON:
{{
  "translation": "English translation",
  "examples": [
    "Simple A1 example sentence in Spanish using this word.",
    "Another simple example sentence."
  ]
}}"""
                try:
                    result = call_claude(system, [{"role": "user", "content": prompt}], max_tokens=300)
                    if "```" in result:
                        result = result.split("```")[1]
                        if result.startswith("json"):
                            result = result[4:]
                    word_data = json.loads(result.strip())
                    examples = "\n".join(f"💬 _{ex}_" for ex in word_data.get("examples", []))
                    await message.channel.send(
                        f"📖 **{word}**\n"
                        f"🇬🇧 {word_data['translation']}\n"
                        f"{examples}\n"
                        f"⚠️ _This word is not in your class word bank yet. Use `!addword {word}` to add it._"
                    )
                except Exception:
                    await message.channel.send(f"⚠️ Could not look up `{word}`. Try again.")
        return

    # !addword command — works in any channel
    if message.content.startswith("!addword"):
        context = get_course_context()
        await handle_addword(message, context)
        return

    channel_name = message.channel.name
    feature = FEATURE_CHANNELS.get(channel_name)
    if not feature:
        return

    # Rate limit check + streak recording
    record_username(message.author.id, message.author.display_name)
    record_practice(message.author.id, message.author.display_name)
    allowed, count = check_and_increment(message.author.id)
    if not allowed:
        await message.channel.send(
            f"⛔ {message.author.mention} You've used all **{DAILY_LIMIT} messages** for today. "
            f"Come back tomorrow! 🌙"
        )
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

            # Warn when 3 messages remain
            if DAILY_LIMIT - count == 3:
                await message.channel.send(
                    f"⚠️ {message.author.mention} Only **3 messages left** for today!"
                )

        except Exception as e:
            log.error("Error handling message in %s: %s", channel_name, e)
            await message.channel.send("⚠️ Something went wrong. Try again in a moment.")


async def main():
    async with client:
        asyncio.get_event_loop().create_task(post_daily_word())
        asyncio.get_event_loop().create_task(update_streak_board())
        await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
