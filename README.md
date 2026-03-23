# VK матч-уведомления в ЛС (тестовый шаблон)

Минимальный Python-проект: отправка сообщений в ЛС администратору через VK API от **личного аккаунта**.
Есть веб-интерфейс для входа в Alpinbet и фоновый парсер новых активных матчей.

## Что уже реализовано

- загрузка настроек из `.env`
- тестовая отправка сообщений в VK из веб-формы
- отправка в ЛС через `messages.send`
- базовая обработка ошибок VK API

## Быстрый старт

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# заполни .env
python main.py
```

## Веб-форма: вход + постоянный парсер

Если 2FA-код ты вводишь сам, запускай локальную страницу:

```bash
python app.py
```

Открой `http://127.0.0.1:5050`:

1. Введи пароль (логин/e-mail берется из `TARGET_LOGIN_USERNAME` в `.env`).
2. Подтверди код из почты.
3. Добавь одну или несколько ссылок на матчи, настрой интервал и включи парсинг.

Что делает приложение:
- отслеживает вкладку **Активные**;
- ищет новые активные матчи (без дублей) сразу по нескольким ссылкам;
- отправляет в VK структуру: команды, турнир, коэффициент, описание ставки, ссылка на матч;
- позволяет отдельно включать/выключать каждую ссылку;
- позволяет менять интервал проверки прямо в панели управления;
- поддерживает кнопки: `Тест отправки в VK`, `Выключить парсинг`, `Сбросить вход`.

### Что заполнить в `.env` для `app.py`

- `TARGET_LOGIN_URL` — URL страницы входа
- `TARGET_DATA_URL` — URL страницы с данными для парсинга (первая ссылка по умолчанию)
- `TARGET_OPEN_LOGIN_SELECTOR` — CSS селектор кнопки, которая открывает попап логина (если логин через модалку)
- `TARGET_LOGIN_USERNAME` — логин/e-mail для входа (веб-форма email не запрашивает)
- `TARGET_EMAIL_SELECTOR` — CSS селектор поля логина/e-mail
- `TARGET_PASSWORD_SELECTOR` — CSS селектор поля password
- `TARGET_SUBMIT_SELECTOR` — CSS селектор кнопки входа
- `TARGET_CODE_SELECTOR` — CSS селектор поля кода
- `TARGET_CODE_SUBMIT_SELECTOR` — CSS селектор кнопки подтверждения кода
- `TARGET_PARSE_ITEM_SELECTOR` — CSS селектор карточек/строк с нужными данными
- `TARGET_PANEL_CONTAINER_SELECTOR` — контейнер с активными/новыми матчами (например `.panel-container`)
- `PARSER_INTERVAL_SECONDS` — интервал проверки в секундах (минимум 10, можно менять из панели)
- `PARSER_SEND_EXISTING_ON_START` — отправлять текущий активный матч сразу после запуска (`1`/`0`)
- `APP_LOG_LEVEL` — уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`)
- `TARGET_HEADLESS` — `0` (видимый браузер) или `1` (headless)

## Какие данные нужны и где их взять

1. `VK_USER_TOKEN` (токен личного аккаунта VK)
- Нужен токен пользователя с правами: `messages,offline`.
- Получается через OAuth VK для `implicit flow`.
- Схема:
  1. Создай приложение VK (тип Standalone) в кабинете разработчика VK.
  2. Перейди по OAuth-ссылке (подставь `CLIENT_ID`):

```text
https://oauth.vk.com/authorize?client_id=CLIENT_ID&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=messages,offline&response_type=token&v=5.199
```

  3. После подтверждения VK редиректит на `blank.html#access_token=...`.
  4. Скопируй `access_token` и вставь в `.env`.

2. `VK_ADMIN_USER_ID` (ID получателя/получателей)
- Можно указать один числовой ID или список ID через запятую.
- Примеры: `123456` или `123456,654321,777888`.
- Как получить:
  - если ссылка вида `vk.com/id123456`, то ID = `123456`;
  - если короткий адрес (`vk.com/username`), можно узнать ID через любой резолвер ID или через VK API метод `utils.resolveScreenName`.

3. `VK_API_VERSION`
- Версия API VK, по умолчанию: `5.199`.

## Структура

- `main.py` — загрузка конфига и отправка сообщения
- `message_builder.py` — генерация тестового матчевого текста
- `.env.example` — шаблон переменных

## Прокси и ошибка SOCKS

Если видишь ошибку `Missing dependencies for SOCKS support`, это значит что в системе выставлен `socks5://` прокси, а библиотека SOCKS не установлена.

В проекте по умолчанию прокси **выключен** для VK-запроса:
- `VK_USE_SYSTEM_PROXY=0` (рекомендуется оставить так)

Если нужно отправлять через SOCKS-прокси:
```bash
pip install "requests[socks]"
```
и в `.env`:
```env
VK_USE_SYSTEM_PROXY=1
```

## Важно

- Токен личного аккаунта хранить только локально, не коммитить в Git.
- Отправка сообщений может зависеть от настроек приватности получателя и ограничений VK.
- Для продакшена лучше добавить логирование, ретраи и очередь отправки.


ССЫЛКА НА ТОКЕН ВК:
https://oauth.vk.com/authorize?client_id=2685278&display=page&redirect_uri=https://oauth.vk.com/blank.html&scope=messages,offline&response_type=token&v=5.199
