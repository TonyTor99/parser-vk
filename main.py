from message_builder import build_fake_match_message
from vk_client import load_vk_config, send_vk_message


def main() -> None:
    config = load_vk_config()
    message = build_fake_match_message()
    message_id = send_vk_message(config, message)
    print(f"Сообщение отправлено. message_id={message_id}")


if __name__ == "__main__":
    main()
