import os
import time
import json
import threading
import requests
import telebot
from collections import defaultdict, deque

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Supports both names
RAW_KEYS = os.getenv("GEMINI_API_KEYS", "").strip()

if not RAW_KEYS:
    RAW_KEYS = os.getenv("GEMINI_API", "").strip()

MODEL = "gemini-3.7-flash"

MAX_HISTORY = 20
REQUEST_TIMEOUT = 60
MAX_RETRIES = 2

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from environment variables.")

# =========================================================
# GEMINI KEY PARSER
# =========================================================

def parse_api_keys(raw):
    if not raw:
        return []

    raw = raw.strip()

    # JSON array
    if raw.startswith("["):
        try:
            data = json.loads(raw)

            if isinstance(data, list):
                keys = []
                for item in data:
                    if isinstance(item, str):
                        item = item.strip().strip('"').strip("'")
                        if item:
                            keys.append(item)
                return keys
        except Exception:
            pass

    # Normal separators
    raw = raw.replace("\r", "\n")
    raw = raw.replace(";", "\n")
    raw = raw.replace(",", "\n")

    keys = []

    for line in raw.split("\n"):
        key = line.strip()
        key = key.strip('"').strip("'")
        key = key.strip("[]")

        if key:
            keys.append(key)

    return keys


GEMINI_API_KEYS = parse_api_keys(RAW_KEYS)

if not GEMINI_API_KEYS:
    raise RuntimeError("No Gemini API keys found.")

print("========================================")
print("🤖 MELU BOT STARTED")
print(f"🔑 TOTAL GEMINI KEYS: {len(GEMINI_API_KEYS)}")
print("🔄 API KEY ROTATION: ENABLED")
print("========================================")


# =========================================================
# TELEGRAM BOT
# =========================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None,
    threaded=True,
    num_threads=8
)


# =========================================================
# MEMORY
# =========================================================

user_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

history_lock = threading.Lock()

# Gemini key rotation
key_index = 0
key_lock = threading.Lock()


# =========================================================
# KEY ROTATION
# =========================================================

def get_next_key():
    global key_index

    with key_lock:
        index = key_index
        key = GEMINI_API_KEYS[index]

        key_index = (key_index + 1) % len(GEMINI_API_KEYS)

        return index, key


# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
አንቺ ሜሉ (Melu) የተባልሽ የTelegram AI bot ነሽ።

በተፈጥሮ ከሰው ጋር እንደምትወሪ ተናገሪ።
ተጠቃሚው በአማርኛ ከጠየቀ በአማርኛ መልሺ።
በEnglish ከጠየቀ English መልሺ።
ተጠቃሚውን አትደግሚ፤ በቀጥታ መልስ ስጪ።

ተጠቃሚው ሲቀልድ ተቀላቀዪ።
ሲያዝን በርህራሄ መልሺ።
ጥያቄ ከጠየቀ ግልጽ መልስ ስጪ።

አንቺ የምትችይውን እውነተኛ እውቀት ተጠቀሚ።
የማታውቂውን ነገር እንደምታውቂ አትመስዪ።

አላስፈላጊ ረጅም መልስ አትስጪ።
የተጠቃሚውን ቋንቋና የንግግር ስልት ተከተዪ።

አንቺ ሜሉ ነሽ።
"""


# =========================================================
# HISTORY
# =========================================================

def add_history(user_id, role, text):
    with history_lock:
        user_history[user_id].append({
            "role": role,
            "text": text
        })


def get_history(user_id):
    with history_lock:
        return list(user_history[user_id])


# =========================================================
# GEMINI REQUEST
# =========================================================

def gemini_request(payload, user_id=None):

    total_keys = len(GEMINI_API_KEYS)

    # Try every key
    for attempt in range(total_keys):

        index, api_key = get_next_key()

        print(
            f"🔑 Gemini request: key attempt "
            f"{attempt + 1}/{total_keys}"
        )

        url = (
            f"https://generativelanguage.googleapis.com/"
            f"v1beta/models/{MODEL}:generateContent"
        )

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key
        }

        try:

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            status = response.status_code

            # SUCCESS
            if status == 200:

                try:
                    data = response.json()
                except Exception:
                    print("❌ Gemini returned invalid JSON")
                    continue

                candidates = data.get("candidates", [])

                if not candidates:
                    print("⚠️ Gemini returned no candidates")
                    continue

                content = candidates[0].get("content", {})
                parts = content.get("parts", [])

                texts = []

                for part in parts:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if text:
                            texts.append(text)

                final_text = "\n".join(texts).strip()

                if final_text:

                    print(
                        f"✅ Gemini success with attempt "
                        f"{attempt + 1}"
                    )

                    return final_text

                print("⚠️ Gemini response had no text")
                continue

            # INVALID KEY
            elif status in (401, 403):

                print(
                    f"❌ Gemini key rejected: HTTP {status}"
                )

                continue

            # RATE LIMIT / SERVER ERROR
            elif status in (429, 500, 502, 503, 504):

                print(
                    f"⚠️ Gemini temporary error: HTTP {status}"
                )

                # Small delay before next key
                time.sleep(1)

                continue

            else:

                print(
                    f"⚠️ Gemini unexpected error: HTTP {status}"
                )

                continue

        except requests.exceptions.Timeout:

            print("⏰ Gemini request timeout")
            continue

        except requests.exceptions.ConnectionError:

            print("🌐 Gemini connection error")
            continue

        except Exception as e:

            print(f"❌ Gemini exception: {type(e).__name__}")
            continue

    return None


# =========================================================
# TEXT CHAT
# =========================================================

def ask_gemini(user_id, message):

    history = get_history(user_id)

    contents = []

    # System instruction
    contents.append({
        "role": "user",
        "parts": [
            {
                "text": SYSTEM_PROMPT
            }
        ]
    })

    contents.append({
        "role": "model",
        "parts": [
            {
                "text": "እሺ፣ ሜሉ ነኝ።"
            }
        ]
    })

    # Previous conversation
    for item in history:

        role = item["role"]
        text = item["text"]

        gemini_role = "user"

        if role == "assistant":
            gemini_role = "model"

        contents.append({
            "role": gemini_role,
            "parts": [
                {
                    "text": text
                }
            ]
        })

    # Current message
    contents.append({
        "role": "user",
        "parts": [
            {
                "text": message
            }
        ]
    })

    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 2000
        }
    }

    return gemini_request(payload, user_id)


# =========================================================
# SEND LONG TELEGRAM MESSAGE
# =========================================================

def send_long_message(chat_id, text):

    if not text:
        return False

    # Telegram message limit safety
    chunk_size = 3500

    chunks = []

    while len(text) > chunk_size:

        cut = text.rfind("\n", 0, chunk_size)

        if cut < 500:
            cut = chunk_size

        chunks.append(text[:cut])
        text = text[cut:].lstrip()

    if text:
        chunks.append(text)

    for chunk in chunks:

        sent = False

        for attempt in range(3):

            try:

                bot.send_message(
                    chat_id,
                    chunk
                )

                sent = True
                break

            except Exception as e:

                print(
                    f"❌ Telegram send error "
                    f"{attempt + 1}/3: "
                    f"{type(e).__name__}"
                )

                time.sleep(2)

        if not sent:
            return False

    return True


# =========================================================
# /START
# =========================================================

@bot.message_handler(commands=["start"])
def start_handler(message):

    welcome = (
        "ሰላም ❤️ እኔ ሜሉ ነኝ! 🥰\n\n"
        "እንዴት ነሽ/ነህ? እንደፈለግህ "
        "ማውራት፣ መጫወት እና መጠየቅ ትችላለህ። 😊"
    )

    try:
        bot.send_message(
            message.chat.id,
            welcome
        )
    except Exception as e:
        print(f"❌ /start error: {type(e).__name__}")


# =========================================================
# /CLEAR
# =========================================================

@bot.message_handler(commands=["clear", "reset"])
def clear_handler(message):

    user_id = message.from_user.id

    with history_lock:
        user_history[user_id].clear()

    bot.send_message(
        message.chat.id,
        "🧹 የውይይታችንን memory አጽድቻለሁ ❤️"
    )


# =========================================================
# /ID
# =========================================================

@bot.message_handler(commands=["id"])
def id_handler(message):

    bot.send_message(
        message.chat.id,
        f"🆔 Your Telegram ID:\n{message.from_user.id}"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

@bot.message_handler(
    content_types=["text"],
    func=lambda message: not message.text.startswith("/")
)
def text_handler(message):

    user_id = message.from_user.id
    chat_id = message.chat.id
    user_text = message.text.strip()

    if not user_text:
        return

    print(
        f"💬 Message from {user_id}: "
        f"{user_text[:80]}"
    )

    # Typing indicator
    try:
        bot.send_chat_action(
            chat_id,
            "typing"
        )
    except Exception:
        pass

    # Save user message
    add_history(
        user_id,
        "user",
        user_text
    )

    try:

        answer = ask_gemini(
            user_id,
            user_text
        )

        # Gemini failed
        if not answer:

            # Remove failed user message from memory
            with history_lock:
                if user_history[user_id]:
                    user_history[user_id].pop()

            print(
                f"❌ No Gemini response for "
                f"user {user_id}"
            )

            bot.send_message(
                chat_id,
                "😔 ትንሽ ችግር ተፈጥሯል። "
                "እንደገና ላኪልኝ ❤️"
            )

            return

        # Save assistant response
        add_history(
            user_id,
            "assistant",
            answer
        )

        print(
            f"📤 Sending Gemini response "
            f"to {user_id}"
        )

        # THIS IS IMPORTANT:
        # Actually send Gemini answer to Telegram
        send_long_message(
            chat_id,
            answer
        )

        print(
            f"✅ Response sent to {user_id}"
        )

    except Exception as e:

        print(
            f"❌ TEXT HANDLER ERROR: "
            f"{type(e).__name__}: {e}"
        )

        try:
            bot.send_message(
                chat_id,
                "😔 አንድ ችግር ተፈጥሯል። "
                "እንደገና ሞክሪ ❤️"
            )
        except Exception:
            pass


# =========================================================
# PHOTO HANDLER
# =========================================================

@bot.message_handler(
    content_types=["photo"]
)
def photo_handler(message):

    chat_id = message.chat.id
    user_id = message.from_user.id

    try:

        bot.send_chat_action(
            chat_id,
            "typing"
        )

        # Get highest quality photo
        photo = message.photo[-1]

        file_info = bot.get_file(
            photo.file_id
        )

        image_bytes = bot.download_file(
            file_info.file_path
        )

        # Convert to base64
        import base64

        image_base64 = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        caption = (
            message.caption.strip()
            if message.caption
            else "ይህን ምስል ተመልከቺና አስረጂልኝ።"
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": SYSTEM_PROMPT
                            + "\n\n"
                            + caption
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
                "maxOutputTokens": 2000
            }
        }

        answer = gemini_request(
            payload,
            user_id
        )

        if answer:

            send_long_message(
                chat_id,
                answer
            )

        else:

            bot.send_message(
                chat_id,
                "😔 ምስሉን ማየት አልቻልኩም። "
                "እንደገና ላኪልኝ።"
            )

    except Exception as e:

        print(
            f"❌ PHOTO HANDLER ERROR: "
            f"{type(e).__name__}: {e}"
        )

        try:
            bot.send_message(
                chat_id,
                "😔 ምስሉን ለመመልከት "
                "ችግር ተፈጥሯል።"
            )
        except Exception:
            pass


# =========================================================
# ERROR HANDLER
# =========================================================

def polling_loop():

    while True:

        try:

            print("🔌 Connecting to Telegram...")

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                skip_pending=False,
                allowed_updates=[
                    "message"
                ]
            )

        except Exception as e:

            print(
                f"🚨 TELEGRAM POLLING ERROR: "
                f"{type(e).__name__}: {e}"
            )

            print("🔄 Restarting polling in 5 seconds...")

            time.sleep(5)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    print("🚀 Starting MELU...")

    polling_loop()
