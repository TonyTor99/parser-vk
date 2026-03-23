import requests
import os

# Удаляем все возможные proxy-переменные
for key in [
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"
]:
    os.environ.pop(key, None)

token = "vk1.a.i00Q1g2dEWO-bmQH3EhBEE7ybmbU-FZhmrvaLI0jbXcw0nMyVErkQgyWw9SMCLBX_mui_tV4VOfzFn8A7rSjFFoUnuy3vbiEl0OS-kinQfcDybiMzfymR16uEaqNdNLDh3EgQ2jDejUgEtvSjh7CKy4BkrTn194AeqM1sSF5dYB14L-dxJGG-oUn7QMa95AQBdxNcAFShE3xYBrJBGdbtw"
version = "5.199"

url = "https://api.vk.com/method/messages.getConversations"

params = {
    "access_token": token,
    "v": version,
    "count": 200,
}

# Создаём сессию и запрещаем брать proxy из окружения
session = requests.Session()
session.trust_env = False

response = session.get(url, params=params).json()

if "error" in response:
    print("Ошибка VK:")
    print(response["error"])
else:
    for item in response["response"]["items"]:
        conv = item["conversation"]
        peer = conv["peer"]

        if peer["type"] == "chat":
            peer_id = peer["id"]
            chat_id = peer_id - 2000000000
            title = conv.get("chat_settings", {}).get("title", "Без названия")

            print(f"Название: {title}")
            print(f"peer_id: {peer_id}")
            print(f"chat_id: {chat_id}")
            print("-" * 40)