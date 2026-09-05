import telebot
import requests
from collections import defaultdict
import time
import os
from datetime import datetime
import base64
import json
import threading
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIG
# =========================================================

# Telegram token — NOW READS FROM FAABLE
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from environment variables")

# =========================================================
# GEMINI API KEYS
# Supports BOTH:
# GEMINI_API
# GEMINI_API_KEYS
# =========================================================

raw_keys = (
    os.getenv("GEMINI_API_KEYS", "").strip()
    or os.getenv("GEMINI_API", "").strip()
)

GEMINI_API_KEYS = []


def load_gemini_keys(raw):
    if not raw:
        return []

    raw = raw.strip()

    try:
        # JSON list:
        # ["key1","key2","key3"]
        if raw.startswith("["):
            data = json.loads(raw)

            if isinstance(data, list):
                return [
                    str(k).strip()
                    for k in data
                    if str(k).strip()
                ]

    except Exception:
        pass

    # Fallback for comma separated:
    # key1,key2,key3
    cleaned = (
        raw
        .replace("[", "")
        .replace("]", "")
        .replace('"', "")
        .replace("'", "")
    )

    return [
        k.strip()
        for k in cleaned.split(",")
        if k.strip()
    ]


GEMINI_API_KEYS = load_gemini_keys(raw_keys)

if not GEMINI_API_KEYS:
    raise RuntimeError(
        "No Gemini API keys found. "
        "Set GEMINI_API or GEMINI_API_KEYS in Faable."
    )


# =========================================================
# KEY ROTATION
# =========================================================

current_key_index = 0
key_lock = threading.Lock()

# Keys that returned permanent authentication errors
bad_keys = set()


def get_next_api_key():
    global current_key_index

    with key_lock:
        total = len(GEMINI_API_KEYS)

        for _ in range(total):
            key = GEMINI_API_KEYS[current_key_index]
            current_key_index = (
                current_key_index + 1
            ) % total

            if key not in bad_keys:
                return key

    return None


def mark_key_bad(api_key):
    if api_key:
        with key_lock:
            bad_keys.add(api_key)


def reset_bad_keys_if_needed():
    """
    If all keys became bad, don't permanently kill the bot.
    Start checking them again.
    """
    with key_lock:
        if len(bad_keys) >= len(GEMINI_API_KEYS):
            bad_keys.clear()


# =========================================================
# TELEGRAM BOT
# =========================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    threaded=True
)

user_histories = defaultdict(list)

USERS_FILE = "users.txt"
FILES_FILE = "files.txt"


# =========================================================
# USERS
# =========================================================

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return set(
                line.strip()
                for line in f
                if line.strip().isdigit()
            )

    return set()


def save_user(user_id):
    users = load_users()

    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(f"{user_id}\n")


# =========================================================
# FILES
# =========================================================

def load_files():
    files = []

    if os.path.exists(FILES_FILE):
        with open(
            FILES_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            for line in f:
                parts = line.strip().split("|")

                if len(parts) >= 4:
                    try:
                        files.append({
                            "date": parts[0],
                            "chat_id": int(parts[1]),
                            "message_id": int(parts[2]),
                            "text": parts[3]
                        })
                    except Exception:
                        pass

    return files


def save_file(post_info):
    with open(
        FILES_FILE,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            f"{post_info['date']}|"
            f"{post_info['chat_id']}|"
            f"{post_info['message_id']}|"
            f"{post_info['text']}\n"
        )


# =========================================================
# GEMINI REQUEST
# =========================================================

GEMINI_MODEL = "gemini-3.7-flash"

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/"
    f"v1beta/models/{GEMINI_MODEL}:generateContent"
)


def gemini_request(payload, timeout=30):

    total_keys = len(GEMINI_API_KEYS)

    reset_bad_keys_if_needed()

    for attempt in range(total_keys):

        api_key = get_next_api_key()

        if not api_key:
            reset_bad_keys_if_needed()
            continue

        print(
            f"🔑 Gemini request: "
            f"key attempt {attempt + 1}/{total_keys}"
        )

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        }

        try:

            response = requests.post(
                GEMINI_URL,
                headers=headers,
                json=payload,
                timeout=timeout
            )

            status = response.status_code

            # =================================================
            # SUCCESS
            # =================================================

            if status == 200:

                data = response.json()

                candidates = data.get(
                    "candidates",
                    []
                )

                if candidates:

                    content = candidates[0].get(
                        "content",
                        {}
                    )

                    parts = content.get(
                        "parts",
                        []
                    )

                    for part in parts:

                        text = part.get("text")

                        if text:
                            print(
                                f"✅ Gemini success "
                                f"with attempt "
                                f"{attempt + 1}"
                            )

                            return text.strip()

                print(
                    "⚠️ Gemini returned "
                    "empty response"
                )

                continue

            # =================================================
            # INVALID / UNAUTHORIZED KEY
            # =================================================

            elif status == 401:

                print(
                    "❌ Gemini key rejected: "
                    "HTTP 401"
                )

                # Don't keep wasting time
                # on this key.
                mark_key_bad(api_key)

                continue

            # =================================================
            # PERMISSION
            # =================================================

            elif status == 403:

                print(
                    "❌ Gemini key permission "
                    "error: HTTP 403"
                )

                mark_key_bad(api_key)

                continue

            # =================================================
            # RATE LIMIT
            # =================================================

            elif status == 429:

                print(
                    "⚠️ Gemini rate limit: "
                    "HTTP 429"
                )

                # Wait briefly, then try another key
                time.sleep(2)

                continue

            # =================================================
            # SERVER ERRORS
            # =================================================

            elif status in (500, 502, 503, 504):

                print(
                    f"⚠️ Gemini server error: "
                    f"HTTP {status}"
                )

                # Retry after short delay
                time.sleep(2)

                continue

            # =================================================
            # OTHER ERROR
            # =================================================

            else:

                print(
                    f"⚠️ Gemini HTTP error: "
                    f"{status}"
                )

                try:
                    print(
                        response.text[:300]
                    )
                except Exception:
                    pass

                continue

        except requests.exceptions.Timeout:

            print(
                "⏰ Gemini request timeout"
            )

            continue

        except requests.exceptions.RequestException as e:

            print(
                f"🌐 Gemini connection error: "
                f"{e}"
            )

            continue

        except Exception as e:

            print(
                f"❌ Gemini unexpected error: "
                f"{e}"
            )

            continue

    # =========================================================
    # ALL KEYS FAILED
    # =========================================================

    print(
        "❌ All Gemini API keys failed "
        "for this request."
    )

    return None


# =========================================================
# IMAGE REQUEST
# =========================================================

def ask_gemini_with_image(
    user_id,
    user_name,
    text,
    image_bytes
):

    image_base64 = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    history = user_histories[user_id]

    history.append(
        f"ተጠቃሚ ({user_name}) "
        f"[ስክሪንሾት ላከ]: {text}"
    )

    if len(history) > 14:
        history.pop(0)

    context_history = "\n".join(history)

    saved_files = load_files()

    files_context = "\n".join(
        [
            f"- ፋይል: {f['text']}"
            for f in saved_files[-5:]
        ]
    )

    system_instruction = f"""
አንቺ ሜሉ (Melu) የተባልሽ
የTelegram bot ነሽ።

ፈጣሪሽ እና አለቃሽ @lij_rafi ነው።
የ @FreeStoreChannel ረዳት ነሽ።

አስፈላጊ ሕጎች:

1. ተጠቃሚው የላከውን
ስክሪንሾት ወይም ፎቶ
በጥንቃቄ መርምሪ።

2. ችግሩን ከተረዳሽ
ደረጃ በደረጃ
በግልጽ አማርኛ አብራሪ።

3. አጋዥ፣ ተረጋጋና
ፍቅር ያለበት ንግግር ተጠቀሚ።

4. የውይይት ታሪክ:
{context_history}

5. የቅርብ ጊዜ ፋይሎች:
{files_context}
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text":
                        f"{system_instruction}\n\n"
                        f"ጥያቄ: {text}"
                    },
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_base64
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2000
        }
    }

    answer = gemini_request(
        payload,
        timeout=40
    )

    if answer:
        return answer

    return (
        "ውዴ 😅 Gemini ላይ በዚህ "
        "ጊዜ ችግር አጋጥሟል። "
        "እባክህ እንደገና ላክልኝ ❤️"
    )


# =========================================================
# TEXT REQUEST
# =========================================================

def ask_gemini(
    user_id,
    user_name,
    text
):

    history = user_histories[user_id]

    history.append(
        f"ተጠቃሚ ({user_name}): {text}"
    )

    if len(history) > 14:
        history.pop(0)

    context_history = "\n".join(history)

    saved_files = load_files()

    files_context = "\n".join(
        [
            f"- ፋይል: {f['text']}"
            for f in saved_files[-5:]
        ]
    )

    system_instruction = f"""
አንቺ ሜሉ (Melu) የተባልሽ
የTelegram bot ነሽ።

ፈጣሪሽ እና አለቃሽ @lij_rafi ነው።
የ @FreeStoreChannel ረዳት ነሽ።

1. ተጠቃሚው ለጠየቀው
ጥያቄ ግልጽ፣ አጭር
እና አጋዥ መልስ ስጪ።

2. ተጠቃሚው መደበኛ
ውይይት ካደረገ በተፈጥሮ
መልስ ስጪ።

3. የቅርብ ጊዜ ፋይሎች:
{files_context}

4. የውይይት ታሪክ:
{context_history}
"""

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text":
                        f"{system_instruction}\n\n"
                        f"ጥያቄ: {text}"
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2000
        }
    }

    answer = gemini_request(
        payload,
        timeout=30
    )

    if answer:
        return answer

    return (
        "ውዴ 😅 ሁሉንም Gemini keys "
        "ሞክሬ ለጊዜው መልስ "
        "ማግኘት አልቻልኩም። "
        "እንደገና ሞክሪ ❤️"
    )


# =========================================================
# CHANNEL POSTS
# =========================================================

@bot.channel_post_handler(
    content_types=[
        "document",
        "audio",
        "video",
        "text",
        "photo"
    ]
)
def handle_channel_posts(message):

    today_date = datetime.now().strftime(
        "%Y-%m-%d"
    )

    post_info = {
        "date": today_date,
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "text": (
            message.text
            or message.caption
            or ""
        ).lower()
    }

    save_file(post_info)

    all_users = load_users()

    for user_id_str in all_users:

        try:

            uid = int(user_id_str)

            bot.forward_message(
                uid,
                message.chat.id,
                message.message_id
            )

        except Exception as e:

            print(
                f"Channel forward error: {e}"
            )


# =========================================================
# PHOTOS
# =========================================================

@bot.message_handler(
    content_types=["photo"]
)
def handle_photos(message):

    user_id = message.from_user.id

    user_name = (
        message.from_user.first_name
        or "ማል"
    )

    save_user(user_id)

    try:

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        caption = (
            message.caption
            or
            "እባክህ ይህንን ስክሪንሾት "
            "አጥንተህ ችግሩን እና "
            "መፍትሄውን ደረጃ በደረጃ "
            "አብራርተህ ንገረኝ።"
        )

        answer = ask_gemini_with_image(
            user_id,
            user_name,
            caption,
            downloaded_file
        )

        bot.send_message(
            message.chat.id,
            answer
        )

    except Exception as e:

        print(
            f"Photo handler error: {e}"
        )

        try:
            bot.send_message(
                message.chat.id,
                "ውዴ 😅 ፎቶውን ማስተንተን "
                "ላይ ችግር ተፈጥሯል። "
                "እንደገና ላኪልኝ ❤️"
            )
        except Exception:
            pass


# =========================================================
# TEXT CHAT
# =========================================================

@bot.message_handler(
    func=lambda message: True
)
def chat(message):

    if not message.text:
        return

    user_id = message.from_user.id

    user_name = (
        message.from_user.first_name
        or "ማል"
    )

    save_user(user_id)

    try:

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

    except Exception:
        pass

    # =====================================================
    # STATS
    # =====================================================

    if (
        message.text.strip().lower()
        == "/stats"
    ):

        total_users = len(
            load_users()
        )

        bot.send_message(
            message.chat.id,
            f"📊 አጠቃላይ ተጠቃሚዎች: "
            f"{total_users} 👥"
        )

        return

    # =====================================================
    # FILE SEARCH
    # =====================================================

    text_lower = (
        message.text.lower()
    )

    keywords = [
        "ehi",
        "pkg",
        "telegram",
        "vpn",
        "config",
        "فايبر",
        "net"
    ]

    if any(
        kw in text_lower
        for kw in keywords
    ):

        saved_files = load_files()

        if saved_files:

            target_file = saved_files[-1]

            for f in reversed(
                saved_files
            ):

                if any(
                    word in f["text"]
                    for word
                    in text_lower.split()
                    if len(word) > 3
                ):

                    target_file = f
                    break

            try:

                bot.forward_message(
                    message.chat.id,
                    target_file["chat_id"],
                    target_file["message_id"]
                )

                return

            except Exception as e:

                print(
                    f"Forward error: {e}"
                )

        bot.send_message(
            message.chat.id,
            "እስካሁን ምንም ፋይል "
            "አልተገኘም!"
        )

        return

    # =====================================================
    # NORMAL GEMINI CHAT
    # =====================================================

    try:

        answer = ask_gemini(
            user_id,
            user_name,
            message.text
        )

        bot.send_message(
            message.chat.id,
            answer
        )

    except Exception as e:

        print(
            f"Chat handler error: {e}"
        )


# =========================================================
# START
# =========================================================

print("=" * 55)
print("🤖 MELU BOT")
print("=" * 55)
print(
    f"🔑 Gemini API Keys Loaded: "
    f"{len(GEMINI_API_KEYS)}"
)
print(
    "🔄 API KEY ROTATION: ENABLED"
)
print(
    "♻️ 401 BAD KEYS: AUTO SKIP"
)
print(
    "🔁 429/5xx: AUTO RETRY"
)
print("=" * 55)


# =========================================================
# INFINITE TELEGRAM POLLING
# =========================================================

while True:

    try:

        print(
            "🚀 MELU BOT STARTED"
        )

        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=30,
            skip_pending=True
        )

    except Exception as e:

        print(
            f"⚠️ Telegram polling error: {e}"
        )

        print(
            "🔄 Restarting polling in 5 seconds..."
        )

        time.sleep(5)
