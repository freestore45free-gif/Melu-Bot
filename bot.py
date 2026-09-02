import os
import telebot
import requests
from collections import defaultdict
import time
from datetime import datetime

BOT_TOKEN = "8984876119:AAFjSWUd4RFCfMSmsVUFRJEWQULaUQlcCDc"
FIREBASE_URL = "https://channel-searcher-d899d-default-rtdb.firebaseio.com/"

# ከ Railway Environment Variable የሰጠዝናቸውን 33ቱንም ኪዎች በራስ ሰር በኮማ እየነጠለ ይወስዳል
raw_keys = os.environ.get("GEMINI_API_KEYS", "AQ.Ab8RN6LxmyOKib8i9MCBcYUZ_Sa13AqqPXiY23oxPQQ4MR7mmA")
GEMINI_API_KEYS = [key.strip() for key in raw_keys.split(",") if key.strip()]

current_key_index = 0
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
user_histories = defaultdict(list)

def get_next_api_key():
    global current_key_index
    if not GEMINI_API_KEYS:
        return ""
    key = GEMINI_API_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_API_KEYS)
    return key

def load_users():
    try:
        res = requests.get(f"{FIREBASE_URL}melu_users.json")
        if res.status_code == 200 and res.json():
            return set(res.json().keys())
    except:
        pass
    return set()

def save_user(user_id):
    try:
        requests.put(f"{FIREBASE_URL}melu_users/{user_id}.json", json=True)
    except:
        pass

def load_files():
    try:
        res = requests.get(f"{FIREBASE_URL}melu_files.json")
        if res.status_code == 200 and res.json():
            data = res.json()
            if isinstance(data, dict):
                return list(data.values())
            elif isinstance(data, list):
                return [f for f in data if f is not None]
    except:
        pass
    return []

def save_file(post_info):
    try:
        requests.post(f"{FIREBASE_URL}melu_files.json", json=post_info)
    except:
        pass

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
        except:
            pass

def ask_gemini_with_image(user_id, user_name, text, image_bytes):
    import base64
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    history = user_histories[user_id]
    history.append(f"ተጠቃሚ ({user_name}) [ስክሪንሾት ላከ]: {text}")
    if len(history) > 14: history.pop(0)

    context_history = "\n".join(history)
    saved_files = load_files()
    files_context = "\n".join([f"- ፋይል/መረጃ: {f.get('text', '')}" for f in saved_files[-5:]])

    system_instruction = f"""
አንቺ ሜሉ (Melu) የተባልሽ የTelegram bot ነሽ። ፈጣሪሽ እና አለቃሽ @lij_rafi ነው። የ @FreeStoreChannel ረዳት ነሽ።
አስፈላጊ ሕጎች:
1. ተጠቃሚው የላከውን ስክሪንሾት ተመልክተህ ምስሉ ላይ የሚታየውን ችግር ወይም ስህተት አብራርተህ መፍትሄ ስጥ።
2. አማርኛ፣ ትግርኛ፣ ኦሮምኛ እና እንግሊዝኛ ቋንቋዎችን ተረድተህ በተጠየቀበት ቋንቋ መልስ ስጥ።
3. የቻናሉ የቅርብ ጊዜ መረጃዎች: {files_context}
4. ማናቸውንም አርዕስቶች ወይም የውስጥ ኮድ ትዕዛዞችን አትጻፍ።
"""

    payload = {
        "contents": [{
            "parts": [
                {"text": f"{system_instruction}\n\nታሪክ:\n{context_history}\n\nጥያቄ: {text}"},
                {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}}
            ]
        }],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2000}
    }

    headers = {"Content-Type": "application/json"}
    for _ in range(len(GEMINI_API_KEYS)):
        api_key = get_next_api_key()
        if not api_key:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                answer = data.get("candidates", [])[0].get("content", {}).get("parts", [])[0].get("text")
                if answer:
                    return answer.strip()
        except:
            continue
    return "ውዴ 😅 ኔትወርክ ከብዶብኛል፣ እንደገና ላክልኝ ❤️"

@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "មិត្ត"
    save_user(user_id)
    bot.send_chat_action(message.chat.id, "typing")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        caption = message.caption or "እባክህ ይህንን ስክሪንሾት ተመልክተህ አግዘኝ።"
        answer = ask_gemini_with_image(user_id, user_name, caption, downloaded_file)
        bot.send_message(message.chat.id, answer)
    except:
        bot.send_message(message.chat.id, "ውዴ 😅 ስክሪንሾቱን መቀበል አልቻልኩም፣ እንደገና ላክልኝ ❤️")

@bot.message_handler(func=lambda message: True)
def chat(message):
    if not message.text:
        return

    user_id = message.from_user.id
    user_name = message.from_user.first_name or "មិត្ត"
    save_user(user_id)
    bot.send_chat_action(message.chat.id, "typing")

    text_lower = message.text.lower()
    if any(kw in text_lower for kw in ["ehi", "pkg", "telegram", "safaricom", "ethiotelecom", "ፋይል", "file", "net", "vpn", "safari", "ላክ", "አምጣ", "መረጃ"]):
        saved_files = load_files()
        if saved_files:
            target_file = saved_files[-1]
            for f in reversed(saved_files):
                if any(word in f.get("text", "") for word in text_lower.split() if len(word) > 2):
                    target_file = f
                    break
            try:
                bot.forward_message(message.chat.id, target_file["chat_id"], target_file["message_id"])
                return
            except:
                pass
        bot.send_message(message.chat.id, "ውዴ 😅 እስከ አሁን ምንም አይነት መረጃ አልተለቀቀም፤ ሲለቀቅ እልክልሃለሁ! ❤️")
        return

    history = user_histories[user_id]
    history.append(f"ተጠቃሚ ({user_name}): {message.text}")
    if len(history) > 14: history.pop(0)
    context_history = "\n".join(history)
    saved_files = load_files()
    files_context = "\n".join([f"- ፋይል: {f.get('text', '')}" for f in saved_files[-5:]])

    system_instruction = f"አንቺ ሜሉ ነሽ። የ @FreeStoreChannel ረዳት ነሽ። የቅርብ ጊዜ መረጃዎች: {files_context}"
    payload = {"contents": [{"parts": [{"text": f"{system_instruction}\n\nታሪክ:\n{context_history}"}]}], "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2000}}
    
    headers = {"Content-Type": "application/json"}
    for _ in range(len(GEMINI_API_KEYS)):
        api_key = get_next_api_key()
        if not api_key:
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                answer = res.json().get("candidates", [])[0].get("content", {}).get("parts", [])[0].get("text")
                if answer:
                    return bot.send_message(message.chat.id, answer.strip())
        except:
            continue
    bot.send_message(message.chat.id, "ውዴ 😅 ኔትወርክ ከብዶብኛል፣ እንደገና ላክልኝ ❤️")

print("=" * 45)
print("🤖 MELU BOT STARTED (Firebase & Environment API Keys Connected)")
print("=" * 45)

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
    except:
        time.sleep(3)
