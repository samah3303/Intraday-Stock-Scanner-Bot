import os
import requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")

if not token:
    print("TELEGRAM_BOT_TOKEN missing in .env")
    exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
print(f"Fetching recent messages/chats for bot token: {token[:10]}...")

try:
    resp = requests.get(url, timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print("API Error:", data)
        exit(1)

    results = data.get("result", [])
    if not results:
        print("\n[!] No recent updates found.")
        print("ACTION REQUIRED:")
        print("1. Open your Telegram Group.")
        print("2. Add your bot (@your_bot_username) to the group.")
        print("3. Type any message inside the group (e.g. 'hello bot').")
        print("4. Re-run this script: python get_telegram_chat_id.py")
        exit(0)

    print("\n--- CHATS & GROUPS FOUND ---")
    seen = set()
    for item in results:
        chat = item.get("message", {}).get("chat") or item.get("my_chat_member", {}).get("chat")
        if chat:
            chat_id = chat.get("id")
            title = chat.get("title") or chat.get("username") or chat.get("first_name")
            chat_type = chat.get("type")
            if chat_id not in seen:
                seen.add(chat_id)
                print(f"Type: {chat_type:<12} | Title: {title:<25} | CHAT_ID: {chat_id}")
                
except Exception as e:
    print("Failed to fetch updates:", e)
