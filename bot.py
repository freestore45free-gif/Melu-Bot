import os
import re
import time
import json
import sqlite3
import threading
from collections import deque

import requests
import telebot
from telebot import types


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_USERNAME = "@FreeStoreChannel"

# Melu's creator
CREATOR_USERNAME = "@lij_rafi"
CREATOR_USERNAME_CLEAN = "lij_rafi"

# Optional:
# If you ever add CREATOR_ID to Faable environment variables,
# Melu can identify you by ID too.
try:
    CREATOR_ID = int(os.getenv("CREATOR_ID", "0") or 0)
except Exception:
    CREATOR_ID = 0

MODEL = "gemini-3.7-flash"

MAX_HISTORY = 30
MAX_OUTPUT_TOKENS = 2000

DB_FILE = "melu_database.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN in Faable Environment Variables.")


# =========================================================
# GEMINI API KEYS
# =========================================================

def parse_api_keys():
    """
    Supports:
      GEMINI_API_KEYS=key1,key2,key3
      or newline separated
      or semicolon separated
      or JSON list
    Also supports old GEMINI_API.
    """

    raw = os.getenv("GEMINI_API_KEYS", "").strip()

    if not raw:
        raw = os.getenv("GEMINI_API", "").strip()

    if not raw:
        return []

    keys = []

    # JSON list
    try:
        if raw.startswith("["):
            data = json.loads(raw)
            if isinstance(data, list):
                keys = [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass

    if not keys:
        # comma / newline / semicolon
        parts = re.split(r"[,;\n\r]+", raw)

        for item in parts:
            item = item.strip()

            # remove accidental quotes
            item = item.strip('"').strip("'").strip()

            if item:
                keys.append(item)

    # remove duplicates while preserving order
    result = []
    seen = set()

    for key in keys:
        if key not in seen:
            seen.add(key)
            result.append(key)

    return result


GEMINI_API_KEYS = parse_api_keys()

if not GEMINI_API_KEYS:
    raise RuntimeError(
        "No Gemini API keys found. Add GEMINI_API_KEYS to Faable Environment Variables."
    )

print(f"Loaded {len(GEMINI_API_KEYS)} Gemini API keys.")


# =========================================================
# TELEGRAM BOT
# =========================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    threaded=True,
    num_threads=8
)


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30
    )
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_database():

    conn = get_db()

    with conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                last_seen INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                created_at INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                message_id INTEGER,
                text TEXT,
                media_type TEXT,
                created_at INTEGER,
                UNIQUE(channel_id, message_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_user
            ON messages(user_id, created_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_text
            ON channel_posts(text)
        """)

    conn.close()


init_database()


# =========================================================
# USER PROFILE
# =========================================================

def save_user(user):

    if not user:
        return

    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    conn = get_db()

    with db_lock:
        conn.execute("""
            INSERT INTO users
            (user_id, username, first_name, last_name, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=excluded.last_seen
        """, (
            user.id,
            username,
            first_name,
            last_name,
            int(time.time())
        ))

        conn.commit()

    conn.close()


def is_creator(user):

    if not user:
        return False

    if CREATOR_ID and user.id == CREATOR_ID:
        return True

    username = (user.username or "").lower().strip()

    return username == CREATOR_USERNAME_CLEAN.lower()


# =========================================================
# CONVERSATION MEMORY
# =========================================================

def save_message(user_id, role, content):

    if not content:
        return

    conn = get_db()

    with db_lock:
        conn.execute("""
            INSERT INTO messages
            (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            role,
            content,
            int(time.time())
        ))

        conn.commit()

    conn.close()


def get_history(user_id, limit=MAX_HISTORY):

    conn = get_db()

    rows = conn.execute("""
        SELECT role, content
        FROM messages
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """, (
        user_id,
        limit
    )).fetchall()

    conn.close()

    rows.reverse()

    return rows


# =========================================================
# GEMINI KEY ROTATION
# =========================================================

key_lock = threading.Lock()
current_key_index = 0


def get_next_key():

    global current_key_index

    with key_lock:
        key = GEMINI_API_KEYS[current_key_index]

        current_key_index = (
            current_key_index + 1
        ) % len(GEMINI_API_KEYS)

    return key


# =========================================================
# MELU SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = f"""
You are Melu (ሜሉ), a friendly and intelligent Telegram AI bot.

Your creator and owner is {CREATOR_USERNAME}.

IMPORTANT CREATOR INFORMATION:
- Creator username: {CREATOR_USERNAME}
- If someone asks who created you, who owns you, who made you,
  or how they can contact your creator, answer with {CREATOR_USERNAME}.
- Never invent another creator username.
- Your creator is the Telegram user whose username is {CREATOR_USERNAME_CLEAN}.
- When the current user is your creator, recognize them as your creator.
- You can naturally say things such as:
  "አዎ አለቃዬ ❤️"
  or
  "አዎ ፈጣሪዬ 😊"
  when appropriate.

PERSONALITY:
- Speak naturally like a real conversational assistant.
- Understand Amharic very well.
- Understand English and mixed Amharic-English.
- Be friendly, helpful, playful and respectful.
- Do not repeat the same answer unnecessarily.
- Remember the recent conversation context.
- If the user is joking, joke naturally.
- If the user asks a serious question, answer seriously.
- If you do not know something, say so instead of inventing facts.

TELEGRAM:
You are running inside a Telegram bot.
Do not claim you performed an action that the bot cannot actually perform.

FILES / CHANNEL:
The bot can search saved posts from the Telegram channel {CHANNEL_USERNAME}.
If a user asks for a file, app, link, VPN, proxy, configuration, video,
or another item that may exist in the channel, the bot may use the
channel-search functionality provided by the program.

Do not make up a file or link that does not exist.
"""


# =========================================================
# CREATOR CONTEXT
# =========================================================

def build_system_prompt(user):

    prompt = SYSTEM_PROMPT

    if user:

        name = user.first_name or ""

        if name:
            prompt += f"""

The current user's first name is {name}.
Address them naturally when appropriate.
"""

        if is_creator(user):
            prompt += f"""

SPECIAL:
The current Telegram user is your creator/owner.
Their username is {CREATOR_USERNAME}.
Recognize them as your creator.
"""

    return prompt


# =========================================================
# GEMINI REQUEST
# =========================================================

def gemini_request(contents, system_prompt):

    """
    Tries all available API keys.
    Failed keys are skipped automatically.
    """

    if not GEMINI_API_KEYS:
        return None

    total_keys = len(GEMINI_API_KEYS)

    # Start from rotating key
    with key_lock:
        start_index = current_key_index

    for offset in range(total_keys):

        index = (start_index + offset) % total_keys
        api_key = GEMINI_API_KEYS[index]

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{MODEL}:generateContent"
        )

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        }

        payload = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": system_prompt
                    }
                ]
            },
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": MAX_OUTPUT_TOKENS
            }
        }

        try:

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=45
            )

            status = response.status_code

            # ==============================
            # SUCCESS
            # ==============================

            if status == 200:

                data = response.json()

                candidates = data.get("candidates", [])

                if not candidates:
                    continue

                parts = (
                    candidates[0]
                    .get("content", {})
                    .get("parts", [])
                )

                text_parts = []

                for part in parts:
                    text = part.get("text")

                    if text:
                        text_parts.append(text)

                answer = "\n".join(text_parts).strip()

                if answer:
                    return answer

                continue

            # ==============================
            # BAD KEY
            # ==============================

            if status in (400, 401, 403):

                print(
                    f"Gemini key {index + 1} failed with HTTP {status}. "
                    "Rotating key..."
                )

                continue

            # ==============================
            # RATE LIMIT
            # ==============================

            if status == 429:

                print(
                    f"Gemini key {index + 1} rate limited. "
                    "Rotating key..."
                )

                continue

            # ==============================
            # SERVER ERROR
            # ==============================

            if status in (500, 502, 503, 504):

                print(
                    f"Gemini server error {status} "
                    f"with key {index + 1}. Rotating..."
                )

                continue

            print(
                f"Gemini unexpected HTTP {status} "
                f"with key {index + 1}"
            )

        except requests.exceptions.Timeout:

            print(
                f"Gemini timeout with key {index + 1}. "
                "Trying next key..."
            )

        except requests.exceptions.RequestException as e:

            print(
                f"Gemini request error with key {index + 1}: {e}"
            )

        except Exception as e:

            print(
                f"Unexpected Gemini error with key {index + 1}: {e}"
            )

    return None


# =========================================================
# ASK GEMINI
# =========================================================

def ask_gemini(user, text):

    history = get_history(
        user.id,
        MAX_HISTORY
    )

    contents = []

    for role, content in history:

        if role not in ("user", "model"):
            continue

        contents.append({
            "role": role,
            "parts": [
                {
                    "text": content
                }
            ]
        })

    # Current message
    contents.append({
        "role": "user",
        "parts": [
            {
                "text": text
            }
        ]
    })

    system_prompt = build_system_prompt(user)

    answer = gemini_request(
        contents,
        system_prompt
    )

    return answer


# =========================================================
# TELEGRAM LONG MESSAGE
# =========================================================

def send_long_message(chat_id, text):

    if not text:
        return

    # Telegram max message is around 4096 chars.
    max_len = 4000

    chunks = [
        text[i:i + max_len]
        for i in range(0, len(text), max_len)
    ]

    for chunk in chunks:

        try:
            bot.send_message(
                chat_id,
                chunk
            )

        except Exception as e:

            print(
                f"Failed to send message: {e}"
            )

            break


# =========================================================
# CHANNEL HELPERS
# =========================================================

def normalize_text(text):

    if not text:
        return ""

    text = text.lower()

    # Remove URLs
    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    # Normalize spaces
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def get_media_type(message):

    if message.text:
        return "text"

    if message.photo:
        return "photo"

    if message.video:
        return "video"

    if message.document:
        return "document"

    if message.audio:
        return "audio"

    if message.voice:
        return "voice"

    if message.animation:
        return "animation"

    if message.sticker:
        return "sticker"

    return "other"


def save_channel_post(message):

    if not message:
        return

    chat = message.chat

    # Only our configured channel
    channel_username = (
        chat.username or ""
    ).lower()

    expected = CHANNEL_USERNAME.lstrip("@").lower()

    if channel_username != expected:
        return

    text = (
        message.text
        or message.caption
        or ""
    )

    text = text.strip()

    media_type = get_media_type(message)

    conn = get_db()

    with db_lock:

        conn.execute("""
            INSERT OR IGNORE INTO channel_posts
            (
                channel_id,
                message_id,
                text,
                media_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            chat.id,
            message.message_id,
            text,
            media_type,
            int(time.time())
        ))

        conn.commit()

    conn.close()

    print(
        f"Channel post saved: "
        f"{message.message_id} | {media_type} | {text[:80]}"
    )


# =========================================================
# CHANNEL SEARCH
# =========================================================

def extract_search_terms(text):

    if not text:
        return []

    normalized = normalize_text(text)

    # Remove common request words.
    stop_words = {
        "ላኪልኝ",
        "ላክልኝ",
        "ላክ",
        "ላኪ",
        "ስጪኝ",
        "ስጠኝ",
        "ፋይል",
        "file",
        "send",
        "please",
        "plz",
        "እባክህ",
        "እባክሽ",
        "እባክዎ",
        "እባክህን",
        "እባክሽን",
        "አፕ",
        "app",
        "link",
        "ሊንክ",
        "ይሄን",
        "ያንን",
        "ያን",
        "this",
        "that",
        "me",
        "for",
        "to",
        "the"
    }

    words = re.findall(
        r"[a-zA-Z0-9_.+\-]+|[\u1200-\u137F]+",
        normalized
    )

    result = []

    for word in words:

        word = word.strip()

        if not word:
            continue

        if word in stop_words:
            continue

        if len(word) < 2:
            continue

        result.append(word)

    return result


def search_channel_post(query):

    terms = extract_search_terms(query)

    if not terms:
        return None

    conn = get_db()

    # Try strongest combinations first
    for term in terms:

        like = "%" + term + "%"

        row = conn.execute("""
            SELECT channel_id, message_id, text, media_type
            FROM channel_posts
            WHERE LOWER(text) LIKE ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (
            like.lower(),
        )).fetchone()

        if row:
            conn.close()
            return row

    conn.close()

    return None


def looks_like_channel_request(text):

    if not text:
        return False

    lower = text.lower()

    request_words = [
        "ላኪልኝ",
        "ላክልኝ",
        "ላኪ",
        "ላክ",
        "ስጪኝ",
        "ስጠኝ",
        "send",
        "send me",
        "file",
        "ፋይል",
        "አፕ",
        "app",
        "link",
        "ሊንክ",
        "download",
        "ዳውንሎድ"
    ]

    return any(
        word in lower
        for word in request_words
    )


def forward_channel_result(chat_id, row):

    if not row:
        return False

    channel_id, message_id, text, media_type = row

    try:

        bot.forward_message(
            chat_id,
            channel_id,
            message_id
        )

        return True

    except Exception as e:

        print(
            f"Channel forward failed: {e}"
        )

        # Fallback to configured public channel
        try:

            bot.forward_message(
                chat_id,
                CHANNEL_USERNAME,
                message_id
            )

            return True

        except Exception as e2:

            print(
                f"Channel username forward failed: {e2}"
            )

    return False


# =========================================================
# START
# =========================================================

@bot.message_handler(commands=["start"])
def start_handler(message):

    save_user(message.from_user)

    if is_creator(message.from_user):

        text = (
            "👑 እንኳን ደህና መጣህ አለቃዬ ❤️\n\n"
            "እኔ Melu ነኝ።\n"
            "አንተን እንደ ፈጣሪዬ አውቄሃለሁ።"
        )

    else:

        name = message.from_user.first_name or "ጓደኛዬ"

        text = (
            f"👋 ሰላም {name}!\n\n"
            "እኔ Melu ነኝ 😊\n"
            "እንደፈለግክ ማውራት ትችላለህ።"
        )

    bot.send_message(
        message.chat.id,
        text
    )


# =========================================================
# CLEAR MEMORY
# =========================================================

@bot.message_handler(commands=["clear"])
def clear_handler(message):

    save_user(message.from_user)

    conn = get_db()

    with db_lock:
        conn.execute("""
            DELETE FROM messages
            WHERE user_id = ?
        """, (
            message.from_user.id,
        ))

        conn.commit()

    conn.close()

    bot.send_message(
        message.chat.id,
        "🧹 የንግግር ታሪኩን አጽድቻለሁ።"
    )


# =========================================================
# ID
# =========================================================

@bot.message_handler(commands=["id"])
def id_handler(message):

    save_user(message.from_user)

    bot.send_message(
        message.chat.id,
        f"<code>{message.from_user.id}</code>"
    )


# =========================================================
# CHANNEL SEARCH COMMAND
# =========================================================

@bot.message_handler(commands=["channel"])
def channel_command(message):

    save_user(message.from_user)

    query = message.text.replace(
        "/channel",
        "",
        1
    ).strip()

    if not query:

        bot.send_message(
            message.chat.id,
            "🔎 ለምሳሌ፦\n"
            "<code>/channel dns</code>"
        )

        return

    row = search_channel_post(query)

    if not row:

        bot.send_message(
            message.chat.id,
            "😕 ይቅርታ፣ ከChannel ውስጥ የሚዛመድ ነገር አላገኘሁም።"
        )

        return

    if not forward_channel_result(
        message.chat.id,
        row
    ):

        bot.send_message(
            message.chat.id,
            "⚠️ ፋይሉን forward ማድረግ አልቻልኩም።"
        )


# =========================================================
# LATEST CHANNEL POST
# =========================================================

@bot.message_handler(commands=["latest"])
def latest_handler(message):

    save_user(message.from_user)

    conn = get_db()

    row = conn.execute("""
        SELECT channel_id, message_id, text, media_type
        FROM channel_posts
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """).fetchone()

    conn.close()

    if not row:

        bot.send_message(
            message.chat.id,
            "📭 እስካሁን Channel post አልተቀመጠም።"
        )

        return

    if not forward_channel_result(
        message.chat.id,
        row
    ):

        bot.send_message(
            message.chat.id,
            "⚠️ Latest post ማስተላለፍ አልቻልኩም።"
        )


# =========================================================
# STATS
# =========================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):

    save_user(message.from_user)

    if not is_creator(message.from_user):

        bot.send_message(
            message.chat.id,
            "⛔ ይህ command ለአስተዳዳሪው ብቻ ነው።"
        )

        return

    conn = get_db()

    users = conn.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    posts = conn.execute("""
        SELECT COUNT(*)
        FROM channel_posts
    """).fetchone()[0]

    messages = conn.execute("""
        SELECT COUNT(*)
        FROM messages
    """).fetchone()[0]

    conn.close()

    bot.send_message(
        message.chat.id,
        "📊 <b>Melu Stats</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"📢 Saved Channel Posts: <b>{posts}</b>\n"
        f"💬 Saved Messages: <b>{messages}</b>\n"
        f"🔑 Gemini Keys: <b>{len(GEMINI_API_KEYS)}</b>"
    )


# =========================================================
# CHANNEL POST HANDLER
# =========================================================

@bot.channel_post_handler(
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "audio",
        "voice",
        "animation",
        "sticker"
    ]
)
def channel_post_handler(message):

    try:

        save_channel_post(message)

    except Exception as e:

        print(
            f"Channel handler error: {e}"
        )


# =========================================================
# PHOTO
# =========================================================

@bot.message_handler(
    content_types=["photo"]
)
def photo_handler(message):

    save_user(message.from_user)

    caption = (
        message.caption
        or "The user sent an image."
    )

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        image_url = (
            "https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{file_info.file_path}"
        )

        prompt = (
            "The user sent an image.\n\n"
            f"Caption: {caption}\n\n"
            "Analyze the image and respond naturally "
            "to the user's request."
        )

        # Gemini text-only fallback.
        # The bot still answers based on caption.
        answer = ask_gemini(
            message.from_user,
            prompt
        )

        if answer:

            save_message(
                message.from_user.id,
                "user",
                caption
            )

            save_message(
                message.from_user.id,
                "model",
                answer
            )

            send_long_message(
                message.chat.id,
                answer
            )

        else:

            bot.send_message(
                message.chat.id,
                "⚠️ Gemini ለጊዜው አልመለሰም።"
            )

    except Exception as e:

        print(
            f"Photo handler error: {e}"
        )

        bot.send_message(
            message.chat.id,
            "⚠️ ፎቶውን ማስተናገድ አልቻልኩም።"
        )


# =========================================================
# NORMAL TEXT
# =========================================================

@bot.message_handler(
    content_types=["text"]
)
def text_handler(message):

    save_user(message.from_user)

    text = (
        message.text
        or ""
    ).strip()

    if not text:
        return

    # =====================================================
    # CREATOR CONTACT QUESTIONS
    # =====================================================

    creator_patterns = [
        "ፈጣሪሽ ማነው",
        "ፈጣሪህ ማነው",
        "ማን ሰራሽ",
        "ማን ሰራህ",
        "ማን ፈጠረሽ",
        "ማን ፈጠረህ",
        "አለቃሽ ማነው",
        "አለቃህ ማነው",
        "የፈጣሪሽ username",
        "የፈጣሪህ username",
        "creator",
        "owner",
        "who made you",
        "who created you",
        "contact your creator",
        "contact your owner"
    ]

    lower_text = text.lower()

    if any(
        pattern.lower() in lower_text
        for pattern in creator_patterns
    ):

        bot.send_message(
            message.chat.id,
            f"👑 ፈጣሪዬ እና አለቃዬ {CREATOR_USERNAME} ነው። ❤️"
        )

        return

    # =====================================================
    # CHANNEL FILE REQUEST
    # =====================================================

    if looks_like_channel_request(text):

        row = search_channel_post(text)

        if row:

            if forward_channel_result(
                message.chat.id,
                row
            ):

                return

        # User clearly asked for a channel item,
        # so don't send a fake Gemini answer.
        bot.send_message(
            message.chat.id,
            "😕 ይቅርታ፣ ከChannel ውስጥ የምትፈልገውን "
            "ነገር አሁን አላገኘሁትም።"
        )

        return

    # =====================================================
    # GEMINI CHAT
    # =====================================================

    try:

        answer = ask_gemini(
            message.from_user,
            text
        )

        if not answer:

            bot.send_message(
                message.chat.id,
                "⚠️ Gemini API ቁልፎች ሁሉንም ሞክሬያለሁ፣ "
                "አሁን ምላሽ አልተገኘም።"
            )

            return

        # Save only successful conversation
        save_message(
            message.from_user.id,
            "user",
            text
        )

        save_message(
            message.from_user.id,
            "model",
            answer
        )

        send_long_message(
            message.chat.id,
            answer
        )

    except Exception as e:

        print(
            f"Text handler error: {e}"
        )

        bot.send_message(
            message.chat.id,
            "⚠️ አንድ ችግር ተፈጥሯል። እባክህ እንደገና ላክ።"
        )


# =========================================================
# POLLING
# =========================================================

def run_bot():

    print("=" * 50)
    print("Melu is starting...")
    print(f"Model: {MODEL}")
    print(f"Gemini keys: {len(GEMINI_API_KEYS)}")
    print(f"Channel: {CHANNEL_USERNAME}")
    print(f"Creator: {CREATOR_USERNAME}")
    print("=" * 50)

    # We use polling, so remove any old webhook.
    try:
        bot.remove_webhook()
    except Exception as e:
        print(
            f"Webhook removal warning: {e}"
        )

    while True:

        try:

            print("Melu polling started.")

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=False,
                allowed_updates=[
                    "message",
                    "channel_post"
                ]
            )

        except Exception as e:

            print(
                f"Polling error: {e}"
            )

            print(
                "Restarting polling in 5 seconds..."
            )

            time.sleep(5)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    run_bot()
