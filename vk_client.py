import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv

VK_API_URL = "https://api.vk.com/method/messages.send"
VK_GET_MESSAGES_UPLOAD_SERVER_URL = "https://api.vk.com/method/photos.getMessagesUploadServer"
VK_SAVE_MESSAGES_PHOTO_URL = "https://api.vk.com/method/photos.saveMessagesPhoto"


@dataclass
class VkConfig:
    user_token: str
    admin_user_ids: list[int]
    api_version: str = "5.199"
    use_system_proxy: bool = False


def parse_bool_env(value: str, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "y", "on"}


def load_vk_config() -> VkConfig:
    load_dotenv()

    token = os.getenv("VK_USER_TOKEN", "").strip()
    admin_user_ids_raw = os.getenv("VK_ADMIN_USER_ID", "").strip()
    api_version = os.getenv("VK_API_VERSION", "5.199").strip()
    use_system_proxy = parse_bool_env(
        os.getenv("VK_USE_SYSTEM_PROXY", "0"),
        default=False,
    )

    if not token:
        raise ValueError("Не задан VK_USER_TOKEN в .env")
    if not admin_user_ids_raw:
        raise ValueError("Не задан VK_ADMIN_USER_ID в .env")

    id_parts = [part.strip() for part in admin_user_ids_raw.split(",") if part.strip()]
    if not id_parts:
        raise ValueError("VK_ADMIN_USER_ID должен содержать хотя бы один ID")

    admin_user_ids: list[int] = []
    for part in id_parts:
        try:
            admin_user_ids.append(int(part))
        except ValueError as exc:
            raise ValueError(
                "VK_ADMIN_USER_ID должен быть числом или списком чисел через запятую"
            ) from exc

    return VkConfig(
        user_token=token,
        admin_user_ids=admin_user_ids,
        api_version=api_version,
        use_system_proxy=use_system_proxy,
    )


def _extract_vk_error(data: dict) -> RuntimeError:
    error = data.get("error", {})
    return RuntimeError(
        f"VK API error {error.get('error_code')}: {error.get('error_msg')}"
    )


def _vk_api_post(session: requests.Session, method_url: str, config: VkConfig, payload: dict) -> dict:
    response = session.post(method_url, data=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise _extract_vk_error(data)
    return data


def upload_vk_message_photo_from_url(config: VkConfig, image_url: str) -> str:
    if not image_url:
        raise ValueError("Не передан URL изображения")

    with requests.Session() as session:
        session.trust_env = config.use_system_proxy

        upload_server_data = _vk_api_post(
            session,
            VK_GET_MESSAGES_UPLOAD_SERVER_URL,
            config,
            {
                "access_token": config.user_token,
                "v": config.api_version,
            },
        )
        upload_url = upload_server_data.get("response", {}).get("upload_url")
        if not upload_url:
            raise RuntimeError(f"Неожиданный ответ VK API (upload server): {upload_server_data}")

        image_response = session.get(image_url, timeout=20)
        image_response.raise_for_status()
        content_type = image_response.headers.get("Content-Type", "image/jpeg")

        upload_response = session.post(
            upload_url,
            files={"photo": ("forecast.jpg", image_response.content, content_type)},
            timeout=30,
        )
        upload_response.raise_for_status()
        upload_data = upload_response.json()

        photo = upload_data.get("photo")
        server = upload_data.get("server")
        upload_hash = upload_data.get("hash")
        if not photo or server is None or not upload_hash:
            raise RuntimeError(f"Неожиданный ответ upload-сервера: {upload_data}")

        save_data = _vk_api_post(
            session,
            VK_SAVE_MESSAGES_PHOTO_URL,
            config,
            {
                "access_token": config.user_token,
                "v": config.api_version,
                "photo": photo,
                "server": server,
                "hash": upload_hash,
            },
        )

    saved_list = save_data.get("response")
    if not isinstance(saved_list, list) or not saved_list:
        raise RuntimeError(f"Неожиданный ответ VK API (save photo): {save_data}")

    first = saved_list[0]
    owner_id = first.get("owner_id")
    photo_id = first.get("id")
    if owner_id is None or photo_id is None:
        raise RuntimeError(f"Неожиданный формат сохраненного фото: {first}")

    return f"photo{owner_id}_{photo_id}"


def send_vk_message(
    config: VkConfig,
    message: str,
    attachment: str = "",
    recipient_ids: Optional[list[int]] = None,
) -> int:
    targets = recipient_ids or config.admin_user_ids
    if not targets:
        raise ValueError("Не задан список получателей VK")

    last_message_id: Optional[int] = None

    with requests.Session() as session:
        session.trust_env = config.use_system_proxy

        for idx, user_id in enumerate(targets):
            payload = {
                "access_token": config.user_token,
                "v": config.api_version,
                "message": message,
                "random_id": int((time.time_ns() + idx) % (2**31 - 1)),
            }
            # Для ЛС используем user_id, для чатов — peer_id (2000000000 + chat_id).
            if user_id >= 2_000_000_000:
                payload["peer_id"] = user_id
            else:
                payload["user_id"] = user_id
            if attachment:
                payload["attachment"] = attachment

            data = _vk_api_post(session, VK_API_URL, config, payload)
            message_id = data.get("response")
            if not isinstance(message_id, int):
                raise RuntimeError(f"Неожиданный ответ VK API: {data}")
            last_message_id = message_id

    if last_message_id is None:
        raise RuntimeError("VK не вернул message_id ни для одного получателя")

    return last_message_id
