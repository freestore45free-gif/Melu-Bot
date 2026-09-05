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
# ENV VARIABLES
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# GEMINI_API_KEYS can be:
# ["key1", "key2", "key3"]
# OR:
# key1,key2,key3
raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()


def load_api_keys(raw):
    keys = []

    if not raw:
        return keys

    try:
        # JSON format
        if raw.startswith("["):
            data = json.loads(raw)

            if isinstance(data, list):
                for key in data:
                    if isinstance(key, str):
                        key = key.strip()
                        if key:
                            keys.append(key)

        else:
            # Comma separated format
            cleaned = (
                raw.replace("[", "")
                   .replace("]", "")
                   .replace('"', "")
                   .replace("'", "")
            )

            for key in cleaned.split(","):
                key = key.strip()
                if key:
                    keys.append(key)

    except Exception as e:
        print("API KEY LOAD ERROR:", e)

        cleaned = (
            raw.replace("[", "")
               .replace("]", "")
               .replace('"', "")
               .replace("'", "")
        )

        for key in cleaned.split(","):
            key = key.strip()
            if key:
                keys.append(key)

    # Remove duplicates while keeping order
    unique_keys = []
    seen = set()

    for key in keys:
        if key not in seen:
            seen.add(key)
            unique_keys.append(key)

    return unique_keys


GEMINI_API_KEYS = load_api_keys(raw_keys)


# =========================================================
# CHECK CONFIGURATION
# =========================================================

print("=" * 55)
print("🤖 MELU BOT")
print("=" * 55)
print(f"🔑 Gemini API Keys Loaded: {len(GEMINI_API_KEYS)}")

if len(GEMINI_API_KEYS) == 33:
    print("✅ All 33 Gemini API keys loaded successfully!")
else:
    print(f"⚠️ WARNING: Expected 33 keys, but loaded {len(GEMINI_API_KEYS)}")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN is missing!")

if not GEMINI_API_KEYS:
    print("❌ GEMINI_API_KEYS is missing!")

print("=" * 55)


# =========================================================
# GEMINI KEY ROTATOR
# =========================================================

class GeminiKeyRotator:

    def __init__(self, keys):
        self.keys = keys
        self.index = 0

        # Protects rotation when many users request simultaneously
        self.lock = threading.Lock()

        # Temporarily disabled keys
        self.disabled_until = {}

    def get_next_key(self):
        if not self.keys:
            return None

        with self.lock:

            total = len(self.keys)

            for _ in range(total):

                index = self.index
                self.index = (self.index + 1) % total

                key = self.keys[index]

                disabled_time = self.disabled_until.get(key, 0)

                # Skip temporarily disabled key
                if time.time() < disabled_time:
                    continue

                return key

        return None

    def disable_key(self, key, seconds=60):
        if not key:
            return

        with self.lock:
            self.disabled_until[key] = time.time() + seconds

    def count(self):
        return len(self.keys)


key_rotator = GeminiKeyRotator(GEMINI_API_KEYS)


# =========================================================
# TELEGRAM BOT
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured!")

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

def call_gemini(payload, timeout=20):

    total_keys = key_rotator.count()

    if total_keys == 0:
        return None

    # Try every available key once
    for attempt in range(total_keys):

        api_key = key_rotator.get_next_key()

        if not api_key:
            continue

        print(
            f"🔑 Gemini request: "
            f"key attempt {attempt + 1}/{total_keys}"
        )

        url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/gemini-3.7-flash:generateContent"
            f"?key={api_key}"
        )

        headers = {
            "Content-Type": "application/json"
        }

        try:

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout
            )

            status = response.status_code

            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

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

                    if parts:

                        answer = parts[0].get(
                            "text",
                            ""
                        )

                        if answer:

                            print(
                                f"✅ Gemini success "
                                f"with attempt {attempt + 1}"
                            )

                            return answer.strip()

                print("⚠️ Gemini returned empty response")
                continue

            # -------------------------------------------------
            # INVALID / BLOCKED KEY
            # -------------------------------------------------

            elif status in (400, 401, 403):

                print(
                    f"❌ Gemini key rejected: HTTP {status}"
                )

                # Temporarily skip this key
                key_rotator.disable_key(
                    api_key,
                    seconds=300
                )

                continue

            # -------------------------------------------------
            # RATE LIMIT
            # -------------------------------------------------

            elif status == 429:

                print(
                    "⏳ Gemini key rate limited (429)"
                )

                # Skip this key for 60 minutes
                key_rotator.disable_key(
                    api_key,
                    seconds=3600
                )

                continue

            # -------------------------------------------------
            # SERVER ERROR
            # -------------------------------------------------

            elif status >= 500:

                print(
                    f"⚠️ Gemini server error: HTTP {status}"
                )

                # Short temporary disable
                key_rotator.disable_key(
                    api_key,
                    seconds=30
                )

                continue

            # -------------------------------------------------
            # OTHER ERROR
            # -------------------------------------------------

            else:

                print(
                    f"⚠️ Gemini HTTP error: {status}"
                )

                continue

        except requests.exceptions.Timeout:

            print("⏱️ Gemini request timeout")
            continue

        except requests.exceptions.RequestException as e:

            print(
                f"🌐 Gemini network error: {e}"
            )

            continue

        except Exception as e:

            print(
                f"❌ Gemini unexpected error: {e}"
            )

            continue

    print(
        "❌ All available Gemini API keys failed."
    )

    return None


# =========================================================
# GEMINI TEXT
# =========================================================

def ask_gemini(user_id, user_name, text):

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
አንቺ ሜሉ (Melu) የተባልሽ የTelegram bot ነሽ።
ፈጣሪሽ እና አለቃሽ @lij_rafi ነው።
የ @FreeStoreChannel ረዳት ነሽ።

1. ተጠቃሚው ለጠየቀው ማንኛውም ጥያቄ፣
ወሬ ወይም ንግግር አጭር፣ ግልጽ እና
ፍቅር በተሞላበት መልኩ በአማርኛ መልስ ስጥ።

2. የቅርብ ጊዜ መረጃዎች:
{files_context}

3. የውይይት ታሪክ:
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

    return call_gemini(
        payload,
        timeout=20
    )


# =========================================================
# GEMINI IMAGE
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

    system_instruction = f"""
አንቺ ሜሉ (Melu) የተባልሽ የTelegram bot ነሽ።
ፈጣሪሽ እና አለቃሽ @lij_rafi ነው።
የ @FreeStoreChannel ረዳት ነሽ።

ተጠቃሚው የላከውን ስክሪንሾት
በጥንቃቄ መርምረህ ምስሉ ላይ
የሚታየውን ችግር ወይም ሁኔታ
አጥንተህ ደረጃ በደረጃ በአማርኛ
ግልጽ መፍትሄ ስጥ።

በፍቅር እና በአጋዥ መልኩ አነጋግረው።

የውይይት ታሪክ:
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

    return call_gemini(
        payload,
        timeout=30
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

        except Exception:
            pass


# =========================================================
# PHOTO HANDLER
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

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

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
            "አጥንተህ ችግሩን እና መፍትሄውን "
            "ደረጃ በደረጃ አብራርተህ ንገረኝ።"
        )

        answer = ask_gemini_with_image(
            user_id,
            user_name,
            caption,
            downloaded_file
        )

        if not answer:
            answer = (
                "ውዴ 😅 አሁን ሁሉም Gemini "
                "API keys ችግር አለባቸው። "
                "ትንሽ ቆይተህ እንደገና ሞክር ❤️"
            )

        bot.send_message(
            message.chat.id,
            answer
        )

    except Exception as e:

        print(
            "Photo error:",
            e
        )

        bot.send_message(
            message.chat.id,
            "ስክሪንሾቱን መቀበል አልቻልኩም!"
        )


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

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    if message.text.strip().lower() == "/stats":

        total_users = len(
            load_users()
        )

        bot.send_message(
            message.chat.id,
            f"📊 አጠቃላይ ቦቱን "
            f"እየተጠቀሙ ያሉ ሰዎች "
            f"ብዛት: {total_users} 👥"
        )

        return

    text_lower = message.text.lower()

    if any(
        kw in text_lower
        for kw in [
            "ehi",
            "pkg",
            "telegram",
            "vpn",
            "config",
            "فايبر",
            "net"
        ]
    ):

        saved_files = load_files()

        if saved_files:

            target_file = saved_files[-1]

            for f in reversed(saved_files):

                if any(
                    word in f["text"]
                    for word in text_lower.split()
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
                    "Forward error:",
                    e
                )

        bot.send_message(
            message.chat.id,
            "እስካሁን ምንም ፋይል አልተገኘም!"
        )

        return

    try:

        answer = ask_gemini(
            user_id,
            user_name,
            message.text
        )

        if not answer:

            answer = (
                "ውዴ 😅 አሁን Gemini API "
                "keys ሁሉ ላይ ችግር አለ። "
                "ትንሽ ቆይተህ እንደገና ሞክር ❤️"
            )

        bot.send_message(
            message.chat.id,
            answer
        )

    except Exception as e:

        print(
            "Chat handler error:",
            e
        )


# =========================================================
# START
# =========================================================

print("=" * 55)
print("🚀 MELU BOT STARTED")
print(f"🔑 TOTAL GEMINI KEYS: {len(GEMINI_API_KEYS)}")
print("🔄 API KEY ROTATION: ENABLED")
print("=" * 55)


while True:

    try:

        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=30,
            skip_pending=True
        )

    except Exception as e:

        print(
            "Polling error:",
            e
        )

        time.sleep(3)
