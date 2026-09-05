import telebot
import requests
from collections import defaultdict
import time
import os
from datetime import datetime
import base64
import json
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = "8984876119:AAG7Z3Nzu5-6IbjhJ-p5XbzXbapG5er0P7Y"

raw_keys = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = []
try:
    if raw_keys.strip().startswith("["):
        GEMINI_API_KEYS = [k.strip() for k in json.loads(raw_keys) if k.strip()]
    else:
        cleaned = raw_keys.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
        GEMINI_API_KEYS = [k.strip() for k in cleaned.split(",") if k.strip()]
except Exception:
    cleaned = raw_keys.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    GEMINI_API_KEYS = [k.strip() for k in cleaned.split(",") if k.strip()]

current_key_index = 0
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
user_histories = defaultdict(list)

USERS_FILE = "users.txt"
FILES_FILE = "files.txt"

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip().isdigit())
    return set()

def save_user(user_id):
    users = load_users()
    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(f"{user_id}\n")

def load_files():
    files = []
    if os.path.exists(FILES_FILE):
        with open(FILES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split("|")
                if len(parts) >= 4:
                    files.append({
                        "date": parts[0],
                        "chat_id": int(parts[1]),
                        "message_id": int(parts[2]),
                        "text": parts[3]
                    })
    return files

def save_file(post_info):
    with open(FILES_FILE, "a", encoding="utf-8") as f:
        f.write(f"{post_info['date']}|{post_info['chat_id']}|{post_info['message_id']}|{post_info['text']}\n")

def get_next_api_key():
    global current_key_index
    if not GEMINI_API_KEYS:
        return ""
    key = GEMINI_API_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_API_KEYS)
    return key

@bot.channel_post_handler(content_types=['document', 'audio', 'video', 'text', 'photo'])
def handle_channel_posts(message):
    today_date = datetime.now().strftime("%Y-%m-%d")
    post_info = {
        "date": today_date,
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "text": (message.text or message.caption or "").lower()
    }
    save_file(post_info)

    all_users = load_users()
    for user_id_str in all_users:
        try:
            uid = int(user_id_str)
            bot.forward_message(uid, message.chat.id, message.message_id)
        except Exception:
            pass

def ask_gemini(user_id, user_name, text):
    history = user_histories[user_id]
    history.append(f"ተጠቃሚ ({user_name}): {text}")
    if len(history) > 14:
        history.pop(0)

    context_history = "\n".join(history)
    saved_files = load_files()
    files_context = "\n".join([f"- ፋይል: {f['text']}" for f in saved_files[-5:]])

    system_instruction = f"""
አንቺ ሜሉ (Melu) የተባልሽ የTelegram bot ነሽ። ፈጣሪሽ እና አለቃሽ @lij_rafi ነው። የ @FreeStoreChannel ረዳት ነሽ።
1. ተጠቃሚው ለጠየቀው ማንኛውም ጥያቄ፣ ወሬ ወይም ንግግር አጭር፣ ግልጽ እና ፍቅር በተሞላበት መልኩ በአማርኛ መልስ ስጥ።
2. የቅርብ ጊዜ መረጃዎች: {files_context}
3. የውይይት ታሪክ: {context_history}
"""

    payload = {
        "contents": [{"parts": [{"text": f"{system_instruction}\n\nጥያቄ:{text}"}]}]
    }

    for _ in range(len(GEMINI_API_KEYS)):
        api_key = get_next_api_key()
        if not api_key:
            break
        
        # ጉግል በሎጉ ላይ እንዳዘዘው gemini-3.6-flash ሞዴልን መጠቀም
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                answer = data["candidates"][0]["content"]["parts"][0]["text"]
                if answer:
                    return answer.strip()
            else:
                print(f"Gemini API Error ({response.status_code}):", response.text)
        except Exception as e:
            print("Request Exception:", e)
            continue
    return "ውዴ 💖 አሁን መልስ ይዤ መጣሁልህ!"

@bot.message_handler(func=lambda message: True)
def chat(message):
    if not message.text:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "ማል"
    save_user(user_id)
    bot.send_chat_action(message.chat.id, "typing")

    if message.text.strip().lower() == "/stats":
        total_users = len(load_users())
        bot.send_message(message.chat.id, f"📊 አጠቃላይ ቦቱን እየተጠቀሙ ያሉ ሰዎች ብዛት: {total_users} 👥")
        return

    text_lower = message.text.lower()
    if any(kw in text_lower for kw in ["ehi", "pkg", "telegram", "vpn", "config", "فايبر", "net"]):
        saved_files = load_files()
        if saved_files:
            target_file = saved_files[-1]
            for f in reversed(saved_files):
                if any(word in f["text"] for word in text_lower.split() if len(word) > 3):
                    target_file = f
                    break
            try:
                bot.forward_message(message.chat.id, target_file["chat_id"], target_file["message_id"])
                return
            except Exception as e:
                print("Forward error:", e)
        bot.send_message(message.chat.id, "እስካሁን ምንም ፋይል አልተገኘም!")
        return

    try:
        answer = ask_gemini(user_id, user_name, message.text)
        bot.send_message(message.chat.id, answer)
    except Exception as e:
        print("Chat handler error:", e)

print("=" * 45)
print("🤖 MELU BOT STARTED (Gemini 3.6 Flash Fixed)")
print("=" * 45)

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
    except Exception as e:
        print("Polling error:", e)
        time.sleep(3)
