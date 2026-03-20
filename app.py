import os
import threading
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request, url_for
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

from vk_client import (
    VkConfig,
    load_vk_config,
    parse_bool_env,
    send_vk_message,
    upload_vk_message_photo_from_url,
)

app = Flask(__name__)
logging.basicConfig(
    level=getattr(logging, os.getenv("APP_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alpinbet_parser")

DEFAULT_PARSER_URL = "https://alpinbet.com/dispatch/id1631660353/pbd-1-fon"
DEFAULT_PARSE_ITEM_SELECTOR = ".rTableLine"
DEFAULT_PANEL_CONTAINER_SELECTOR = ".panel-container"
DEFAULT_PARSER_INTERVAL_SECONDS = 10


@dataclass
class TargetConfig:
    login_url: str
    data_url: str
    open_login_selector: str
    login_username: str
    email_selector: str
    password_selector: str
    submit_selector: str
    code_selector: str
    code_submit_selector: str
    parse_item_selector: str
    panel_container_selector: str
    login_form_selector: str
    login_error_selector: str
    parser_interval_seconds: int
    parser_send_existing_on_start: bool
    headless: bool


@dataclass(frozen=True)
class ParsedMatch:
    home_team: str
    away_team: str
    tournament: str
    rate: str
    rate_description: str
    href: str
    image_url: str
    unique_key: str


class BrowserState:
    def __init__(self) -> None:
        self.lock = threading.RLock()

        self.playwright: Optional[Playwright] = None
        self.page: Optional[Page] = None
        self.auth_storage_state: Optional[dict[str, Any]] = None

        self.step: str = "idle"  # idle | await_code | ready
        self.error: str = ""
        self.info: str = ""

        self.preview: str = ""
        self.last_message_id: Optional[int] = None

        self.parser_thread: Optional[threading.Thread] = None
        self.parser_stop_event: Optional[threading.Event] = None
        self.parser_running: bool = False

        self.parser_url: str = ""
        self.parser_interval_seconds: int = DEFAULT_PARSER_INTERVAL_SECONDS
        self.seen_match_keys: set[str] = set()

        self.parser_last_check_at: str = ""
        self.parser_last_sent_at: str = ""
        self.parser_last_match_title: str = ""
        self.parser_error: str = ""

    def clear_runtime(self) -> None:
        with self.lock:
            page = self.page
            playwright = self.playwright
            self.page = None
            self.playwright = None

        if page is not None:
            try:
                page.context.browser.close()
            except PlaywrightError:
                pass

        if playwright is not None:
            try:
                playwright.stop()
            except PlaywrightError:
                pass

    def stop_parser(self) -> None:
        with self.lock:
            stop_event = self.parser_stop_event
            thread = self.parser_thread
            self.parser_stop_event = None

        if stop_event is not None:
            stop_event.set()

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=8)

        with self.lock:
            self.parser_thread = None
            self.parser_running = False

    def reset(self) -> None:
        self.stop_parser()
        self.clear_runtime()

        with self.lock:
            self.step = "idle"
            self.error = ""
            self.info = ""
            self.preview = ""
            self.last_message_id = None
            self.auth_storage_state = None

            self.parser_url = ""
            self.parser_interval_seconds = DEFAULT_PARSER_INTERVAL_SECONDS
            self.seen_match_keys = set()
            self.parser_last_check_at = ""
            self.parser_last_sent_at = ""
            self.parser_last_match_title = ""
            self.parser_error = ""


state = BrowserState()


def now_label() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def normalize_text(value: str) -> str:
    return " ".join((value or "").split())


def mask_token(token: str) -> str:
    token = normalize_text(token)
    if not token:
        return "не задан"
    if len(token) <= 10:
        return token[:2] + "..." + token[-2:]
    return token[:6] + "..." + token[-6:]


def upsert_env_value(key: str, value: str, env_path: Optional[Path] = None) -> None:
    target_path = env_path or (Path(__file__).resolve().parent / ".env")
    line_re = re.compile(rf"^\\s*{re.escape(key)}=")

    if target_path.exists():
        lines = target_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line_re.match(line):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"{key}={value}")

    target_path.write_text("\\n".join(new_lines) + "\\n", encoding="utf-8")


def load_target_config() -> TargetConfig:
    load_dotenv()

    interval_raw = os.getenv("PARSER_INTERVAL_SECONDS", str(
        DEFAULT_PARSER_INTERVAL_SECONDS)).strip()
    try:
        parser_interval_seconds = int(interval_raw)
    except ValueError as exc:
        raise ValueError("PARSER_INTERVAL_SECONDS должен быть числом") from exc

    if parser_interval_seconds < 10:
        parser_interval_seconds = 10

    cfg = TargetConfig(
        login_url=os.getenv("TARGET_LOGIN_URL", "").strip(),
        data_url=os.getenv("TARGET_DATA_URL",
                           DEFAULT_PARSER_URL).strip() or DEFAULT_PARSER_URL,
        open_login_selector=os.getenv(
            "TARGET_OPEN_LOGIN_SELECTOR", "").strip(),
        login_username=os.getenv("TARGET_LOGIN_USERNAME", "").strip(),
        email_selector=os.getenv(
            "TARGET_EMAIL_SELECTOR", "#loginform-username").strip(),
        password_selector=os.getenv(
            "TARGET_PASSWORD_SELECTOR", "#loginform-password").strip(),
        submit_selector=os.getenv(
            "TARGET_SUBMIT_SELECTOR",
            "#login-form button[type='submit']",
        ).strip(),
        code_selector=os.getenv("TARGET_CODE_SELECTOR",
                                "input[name*='code']").strip(),
        code_submit_selector=os.getenv(
            "TARGET_CODE_SUBMIT_SELECTOR",
            "button[type='submit']",
        ).strip(),
        parse_item_selector=os.getenv(
            "TARGET_PARSE_ITEM_SELECTOR",
            DEFAULT_PARSE_ITEM_SELECTOR,
        ).strip(),
        panel_container_selector=os.getenv(
            "TARGET_PANEL_CONTAINER_SELECTOR",
            DEFAULT_PANEL_CONTAINER_SELECTOR,
        ).strip(),
        login_form_selector=os.getenv(
            "TARGET_LOGIN_FORM_SELECTOR", "#login-form").strip(),
        login_error_selector=os.getenv(
            "TARGET_LOGIN_ERROR_SELECTOR",
            "#login-form .help-block",
        ).strip(),
        parser_interval_seconds=parser_interval_seconds,
        parser_send_existing_on_start=parse_bool_env(
            os.getenv("PARSER_SEND_EXISTING_ON_START", "1"),
            default=True,
        ),
        headless=parse_bool_env(
            os.getenv("TARGET_HEADLESS", "0"), default=False),
    )

    required = [
        ("TARGET_LOGIN_URL", cfg.login_url),
        ("TARGET_PASSWORD_SELECTOR", cfg.password_selector),
        ("TARGET_SUBMIT_SELECTOR", cfg.submit_selector),
        ("TARGET_CODE_SELECTOR", cfg.code_selector),
        ("TARGET_CODE_SUBMIT_SELECTOR", cfg.code_submit_selector),
    ]
    missing = [name for name, value in required if not value]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Не заполнены переменные в .env: {joined}")

    return cfg


def build_active_match_message(match: ParsedMatch, source_url: str) -> str:
    rate = match.rate or "Не указан"
    rate_description = match.rate_description or "Нет текстового описания прогноза"
    return (
        "🚨 Новый активный матч\n"
        "------------------------------\n"
        f"⚽ Команды: {match.home_team} - {match.away_team}\n"
        f"🏆 Турнир: {match.tournament}\n"
        f"📌 Ставка: {rate_description}\n"
        f"📈 Коэффициент: {rate}\n"
        f"🔗 Ссылка на матч: {match.href}"
    )


def try_wait_visible(page: Page, selector: str, timeout_ms: int = 2500) -> bool:
    try:
        page.locator(selector).first.wait_for(
            state="visible", timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_visible_texts(page: Page, selector: str, limit: int = 8) -> list[str]:
    texts: list[str] = []
    if not selector:
        return texts

    nodes = page.locator(selector)
    count = min(nodes.count(), limit)
    for i in range(count):
        node = nodes.nth(i)
        try:
            if not node.is_visible():
                continue
            raw = node.inner_text().strip()
        except Exception:  # noqa: BLE001
            continue

        normalized = normalize_text(raw)
        if normalized:
            texts.append(normalized)
    return texts


def is_login_form_visible(page: Page, selector: str) -> bool:
    if not selector:
        return False

    try:
        locator = page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def click_active_tab(page: Page) -> None:
    try:
        active_now = page.locator(
            ".tab.tab_lg.active-tab:has-text('Активные')")
        if active_now.count() > 0 and active_now.first.is_visible():
            return
    except Exception:  # noqa: BLE001
        pass

    active_tab_selectors = [
        ".tab.tab_lg:has-text('Активные')",
        ".tab.tab_lg.active-tab:has-text('Активные')",
        "button:has-text('Активные')",
        "a:has-text('Активные')",
        "[role='tab']:has-text('Активные')",
        ".tabs__item:has-text('Активные')",
        ".tab-link:has-text('Активные')",
    ]

    for selector in active_tab_selectors:
        locator = page.locator(selector)
        if locator.count() == 0:
            continue

        node = locator.first
        try:
            if not node.is_visible():
                continue
            node.click(timeout=3000)
            page.wait_for_timeout(700)
            return
        except Exception:  # noqa: BLE001
            continue


def parse_active_matches(page: Page, cfg: TargetConfig, source_url: str) -> list[ParsedMatch]:
    click_active_tab(page)

    row_selectors = [
        "#tab-forecast-active .js-tab-forecast-active-list .rTableLine",
        "#tab-forecast-active .rTableBody .rTableLine",
        cfg.parse_item_selector,
        DEFAULT_PARSE_ITEM_SELECTOR,
        ".dispatch-row",
    ]
    panel_selector = normalize_text(cfg.panel_container_selector)

    candidate_selectors: list[str] = []
    for row_selector in row_selectors:
        if not row_selector:
            continue
        if panel_selector:
            candidate_selectors.extend(
                [
                    f"{panel_selector}.active-tab {row_selector}",
                    f"{panel_selector}.active {row_selector}",
                    f"{panel_selector} {row_selector}",
                ]
            )
            continue
        candidate_selectors.append(row_selector)

    # Убираем дубли селекторов, сохраняя порядок.
    candidate_selectors = list(dict.fromkeys(candidate_selectors))
    seen_in_batch: set[str] = set()

    script = """
(rows, panelSelector) => {
  const isVisible = (element) => {
    if (!element) {
      return false;
    }
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };

  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
  return rows
    .map((row) => {
      if (!isVisible(row)) {
        return null;
      }

      if (panelSelector) {
        const panel = row.closest(panelSelector);
        if (panel && !isVisible(panel)) {
          return null;
        }
      }

      const teams = Array.from(row.querySelectorAll(".cell-team-command"))
        .map((item) => normalize(item.textContent))
        .filter(Boolean);

      if (teams.length < 2) {
        return null;
      }

      const tournament = normalize(row.querySelector(".cell-team-tnm")?.textContent);
      const rate = normalize(
        row.querySelector(".cell-coefficient__total")?.textContent ||
        row.querySelector(".rate")?.textContent
      );
      const rateDescription = normalize(
        row.querySelector(".rate-description")?.textContent ||
        row.querySelector(".cell-type .type-live")?.textContent ||
        row.querySelector(".cell-type")?.textContent
      );
      const href = row.querySelector("a")?.getAttribute("href") || "";
      const imageNode = row.querySelector(".rTableHead.cell-prognos img.img-light, .rTableHead.cell-prognos img, .cell-prognos img");
      const imageUrl = imageNode?.getAttribute("src") || imageNode?.getAttribute("data-src") || "";

      if (!tournament) {
        return null;
      }

      return {
        home_team: teams[0],
        away_team: teams[1],
        tournament,
        rate,
        rate_description: rateDescription,
        href,
        image_url: imageUrl,
      };
    })
    .filter(Boolean);
}
"""

    for selector in candidate_selectors:
        if not selector:
            continue

        try:
            raw_rows = page.eval_on_selector_all(selector, script, panel_selector or None)
        except Exception:  # noqa: BLE001
            continue

        parsed: list[ParsedMatch] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue

            home_team = normalize_text(str(row.get("home_team", "")))
            away_team = normalize_text(str(row.get("away_team", "")))
            tournament = normalize_text(str(row.get("tournament", "")))
            rate = normalize_text(str(row.get("rate", "")))
            rate_description = normalize_text(
                str(row.get("rate_description", "")))
            href = normalize_text(str(row.get("href", "")))
            image_url = normalize_text(str(row.get("image_url", "")))

            if not all([home_team, away_team, tournament]):
                continue

            full_href = urljoin(source_url, href) if href else source_url
            full_image_url = urljoin(source_url, image_url) if image_url else ""
            unique_key = "|".join(
                [home_team, away_team, tournament, rate, rate_description, full_href])

            if unique_key in seen_in_batch:
                continue
            seen_in_batch.add(unique_key)

            parsed.append(
                ParsedMatch(
                    home_team=home_team,
                    away_team=away_team,
                    tournament=tournament,
                    rate=rate,
                    rate_description=rate_description,
                    href=full_href,
                    image_url=full_image_url,
                    unique_key=unique_key,
                )
            )

        if parsed:
            return parsed

    return []


def fetch_active_matches(page: Page, cfg: TargetConfig, parse_url: str) -> list[ParsedMatch]:
    page.goto(parse_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)
    # Прокручиваем страницу вниз для lazy-load изображений прогноза.
    try:
        page.evaluate(
            """
() => {
  const step = Math.max(Math.floor(window.innerHeight * 0.8), 300);
  let prevHeight = -1;
  let sameHeightTicks = 0;

  for (let i = 0; i < 60; i += 1) {
    window.scrollBy(0, step);
    const currentHeight = Math.max(
      document.body.scrollHeight || 0,
      document.documentElement.scrollHeight || 0
    );
    if (currentHeight === prevHeight) {
      sameHeightTicks += 1;
      if (sameHeightTicks >= 3) {
        break;
      }
    } else {
      sameHeightTicks = 0;
      prevHeight = currentHeight;
    }
  }
}
"""
        )
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        pass
    return parse_active_matches(page, cfg, parse_url)


def humanize_parser_error(exc: Exception) -> str:
    raw = normalize_text(str(exc))
    lowered = raw.lower()

    if "cannot switch to a different thread" in lowered:
        return "Ошибка потоков браузера. Перезапусти парсер."
    if "target page, context or browser has been closed" in lowered or "has been closed" in lowered:
        return "Сессия браузера закрыта. Выполни вход заново."
    if "timeout" in lowered:
        return "Таймаут при загрузке страницы или получении данных."
    if not raw:
        return "Неизвестная ошибка парсера."
    return f"Техническая ошибка парсера: {raw}"


def parser_worker(
    cfg: TargetConfig,
    parse_url: str,
    stop_event: threading.Event,
    storage_state: dict[str, Any],
) -> None:
    logger.info("Запуск фонового парсера. url=%s", parse_url)
    try:
        vk_cfg: VkConfig = load_vk_config()
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.parser_error = f"Ошибка VK конфигурации: {exc}"
            state.parser_running = False
            state.parser_thread = None
            state.parser_stop_event = None
        return

    parser_playwright: Optional[Playwright] = None
    parser_page: Optional[Page] = None

    try:
        parser_playwright = sync_playwright().start()
        parser_browser = parser_playwright.chromium.launch(headless=cfg.headless)
        parser_context = parser_browser.new_context(storage_state=storage_state)
        parser_page = parser_context.new_page()
        logger.info("Браузер парсера успешно инициализирован")
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.parser_error = f"{now_label()} | {humanize_parser_error(exc)}"
            state.parser_running = False
            state.parser_thread = None
            state.parser_stop_event = None
        if parser_playwright is not None:
            try:
                parser_playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        return

    first_cycle = True

    while not stop_event.is_set():
        try:
            if parser_page is None:
                raise RuntimeError("Сессия парсера не инициализирована")

            matches = fetch_active_matches(parser_page, cfg, parse_url)
            logger.info(
                "Цикл парсера: найдено матчей=%s, first_cycle=%s",
                len(matches),
                first_cycle,
            )
            with state.lock:
                state.parser_last_check_at = now_label()
                state.parser_error = ""

            if first_cycle and not cfg.parser_send_existing_on_start:
                logger.info(
                    "Первый цикл: отправка существующих матчей отключена (PARSER_SEND_EXISTING_ON_START=0)"
                )
                with state.lock:
                    for match in matches:
                        state.seen_match_keys.add(match.unique_key)
                first_cycle = False
            else:
                for match in matches:
                    with state.lock:
                        already_seen = match.unique_key in state.seen_match_keys
                    if already_seen:
                        logger.debug("Матч уже отправлялся, пропускаю: %s", match.unique_key)
                        continue

                    message = build_active_match_message(match, parse_url)
                    attachment = ""
                    if match.image_url:
                        try:
                            attachment = upload_vk_message_photo_from_url(
                                vk_cfg, match.image_url)
                        except Exception as image_exc:  # noqa: BLE001
                            with state.lock:
                                state.parser_error = (
                                    f"{now_label()} | Картинку не удалось прикрепить, "
                                    f"отправляю только текст: {humanize_parser_error(image_exc)}"
                                )
                            logger.warning(
                                "Не удалось загрузить картинку в VK, отправляю текст. image_url=%s error=%s",
                                match.image_url,
                                image_exc,
                            )

                    message_id = send_vk_message(
                        vk_cfg, message, attachment=attachment)
                    logger.info(
                        "Сообщение отправлено в VK. message_id=%s match=%s - %s",
                        message_id,
                        match.home_team,
                        match.away_team,
                    )

                    with state.lock:
                        state.seen_match_keys.add(match.unique_key)
                        state.preview = message + (
                            f"\nВложение: {match.image_url}" if match.image_url else ""
                        )
                        state.last_message_id = message_id
                        state.parser_last_sent_at = now_label()
                        state.parser_last_match_title = f"{match.home_team} - {match.away_team}"
                first_cycle = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка в цикле парсера")
            with state.lock:
                state.parser_error = f"{now_label()} | {humanize_parser_error(exc)}"

        if stop_event.wait(cfg.parser_interval_seconds):
            break

    with state.lock:
        if state.parser_stop_event is stop_event:
            state.parser_stop_event = None
        if state.parser_thread is threading.current_thread():
            state.parser_thread = None
        state.parser_running = False
    logger.info("Фоновый парсер остановлен")

    if parser_page is not None:
        try:
            parser_page.context.browser.close()
        except Exception:  # noqa: BLE001
            pass

    if parser_playwright is not None:
        try:
            parser_playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def start_parser_thread(cfg: TargetConfig, parse_url: str, storage_state: dict[str, Any]) -> None:
    state.stop_parser()

    stop_event = threading.Event()
    worker = threading.Thread(
        target=parser_worker,
        args=(cfg, parse_url, stop_event, storage_state),
        name="alpinbet-parser",
        daemon=True,
    )

    with state.lock:
        state.parser_url = parse_url
        state.parser_interval_seconds = cfg.parser_interval_seconds
        state.seen_match_keys = set()
        state.parser_last_check_at = ""
        state.parser_last_sent_at = ""
        state.parser_last_match_title = ""
        state.parser_error = ""

        state.parser_stop_event = stop_event
        state.parser_thread = worker
        state.parser_running = True

    worker.start()


def describe_login_status(step: str) -> str:
    if step == "await_code":
        return "Ожидается код подтверждения из почты"
    if step == "ready":
        return "Вход в аккаунт выполнен успешно. Бот готов к парсингу данных"
    return "Вход не выполнен"


def describe_parser_status(step: str, running: bool, interval_seconds: int) -> str:
    if running:
        return f"Включен (интервал проверки: {interval_seconds} сек)"
    if step != "ready":
        return "Недоступен до успешного входа"
    return "Выключен"


TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Alpinbet Parser</title>
  <style>
    :root {
      --bg: #f3f5f7;
      --surface: #ffffff;
      --surface-soft: #f7fafc;
      --text: #0f172a;
      --muted: #506176;
      --accent: #0f766e;
      --accent-soft: #ccfbf1;
      --danger: #b91c1c;
      --danger-soft: #fee2e2;
      --line: #d8e0e8;
      --radius: 16px;
      --shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 20% 0%, #dbeafe 0%, transparent 34%),
        radial-gradient(circle at 90% 10%, #ccfbf1 0%, transparent 28%),
        var(--bg);
      color: var(--text);
      font-family: "Manrope", "Segoe UI Variable", "Trebuchet MS", sans-serif;
      line-height: 1.45;
    }

    .shell {
      max-width: 980px;
      margin: 0 auto;
      padding: 20px 14px 28px;
    }

    .hero {
      background: linear-gradient(130deg, #0f172a, #134e4a 64%);
      color: #ecfeff;
      border-radius: calc(var(--radius) + 2px);
      padding: 18px 20px;
      box-shadow: var(--shadow);
      margin-bottom: 14px;
    }

    .hero h1 {
      margin: 0;
      font-size: 24px;
      font-weight: 800;
      letter-spacing: 0.2px;
    }

    .hero p {
      margin: 8px 0 0;
      color: #bae6fd;
      font-size: 14px;
    }

    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px;
      box-shadow: var(--shadow);
    }

    .card h3 {
      margin: 0 0 10px;
      font-size: 16px;
      font-weight: 800;
      letter-spacing: 0.2px;
    }

    .status-wrap {
      grid-column: 1 / -1;
      background: var(--surface-soft);
    }

    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 10px;
      border-radius: 999px;
      font-size: 13px;
      border: 1px solid #99f6e4;
      background: var(--accent-soft);
      color: #0f766e;
    }

    .pill.meta {
      border-color: #bfdbfe;
      background: #eff6ff;
      color: #1d4ed8;
    }

    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 11px 12px;
      font-size: 14px;
      color: var(--text);
      background: #fff;
      margin-bottom: 10px;
    }

    input:focus {
      outline: none;
      border-color: #14b8a6;
      box-shadow: 0 0 0 3px rgba(20, 184, 166, 0.18);
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }

    .actions form {
      margin: 0;
      flex: 1 1 220px;
    }

    .actions form.full {
      flex-basis: 100%;
    }

    button {
      width: 100%;
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      color: #fff;
      background: linear-gradient(120deg, #0f766e, #14b8a6);
      transition: transform 0.16s ease, filter 0.16s ease;
    }

    button:hover { filter: brightness(1.04); transform: translateY(-1px); }
    button:active { transform: translateY(0); }
    button[disabled] { opacity: 0.55; cursor: not-allowed; transform: none; }

    .secondary {
      background: linear-gradient(120deg, #334155, #475569);
    }

    .danger {
      background: linear-gradient(120deg, #b91c1c, #ef4444);
    }

    .hint {
      color: var(--muted);
      font-size: 13px;
      margin: 4px 0 0;
    }

    .error,
    .ok {
      white-space: pre-wrap;
      border-radius: 12px;
      padding: 9px 10px;
      margin-top: 8px;
      font-size: 14px;
    }

    .error {
      color: #7f1d1d;
      background: var(--danger-soft);
      border: 1px solid #fecaca;
    }

    .ok {
      color: #14532d;
      background: #dcfce7;
      border: 1px solid #bbf7d0;
    }

    pre {
      margin: 8px 0 0;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fff;
      padding: 12px;
      white-space: pre-wrap;
      font-size: 13px;
    }

    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .hero h1 { font-size: 22px; }
    }

    @media (max-width: 560px) {
      .shell { padding: 14px 10px 22px; }
      .hero { padding: 14px; border-radius: 14px; }
      .hero h1 { font-size: 20px; }
      .card { padding: 13px; border-radius: 14px; }
      button { padding: 12px; }
      .actions form { flex: 1 1 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <h1>Панель управления ботом</h1>
      <p>Alpinbet -> VK: отслеживание активных матчей и отправка уведомлений.</p>
    </section>

    <div class="grid">
      <section class="card status-wrap">
        <div class="status-row">
          <span class="pill">Вход: {{ login_status }}</span>
          <span class="pill meta">Парсер: {{ parser_status }}</span>
        </div>
        {% if parser_last_check_at %}<div class="hint">Последняя проверка: {{ parser_last_check_at }}</div>{% endif %}
        {% if parser_last_sent_at %}<div class="hint">Последняя отправка: {{ parser_last_sent_at }}{% if parser_last_match_title %} ({{ parser_last_match_title }}){% endif %}</div>{% endif %}
        {% if info %}<div class="ok">{{ info }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        {% if parser_error %}<div class="error">Ошибка парсера: {{ parser_error }}</div>{% endif %}
        {% if message_id %}<div class="ok">Отправлено в VK. message_id={{ message_id }}</div>{% endif %}
      </section>

      <section class="card">
        <h3>Вход в аккаунт</h3>
        <form method="post" action="{{ url_for('start_login') }}">
          <input name="password" type="password" placeholder="Пароль" required />
          <button type="submit">Войти</button>
        </form>
      </section>

      <section class="card">
        <h3>Управление парсером</h3>
        <form method="post" action="{{ url_for('start_parser') }}">
          <input name="parse_url" type="url" placeholder="Ссылка для парсинга" value="{{ parser_url }}" required />
          <button type="submit" {% if not can_manage_parser %}disabled{% endif %}>Включить парсинг</button>
        </form>
        <div class="hint">Интервал проверки: {{ parser_interval_seconds }} сек.</div>
        <div class="actions">
          <form method="post" action="{{ url_for('send_test_message') }}" class="full">
            <button class="secondary" type="submit">Тест отправки в VK</button>
          </form>
          <form method="post" action="{{ url_for('stop_parser') }}">
            <button class="secondary" type="submit">Выключить парсинг</button>
          </form>
          <form method="post" action="{{ url_for('reset') }}">
            <button class="danger" type="submit">Сбросить вход</button>
          </form>
        </div>
      </section>

      <section class="card">
        <h3>Настройки VK</h3>
        <form method="post" action="{{ url_for('update_vk_token') }}">
          <input name="vk_user_token" type="password" placeholder="Новый VK_USER_TOKEN" required />
          <button class="secondary" type="submit">Сохранить токен</button>
        </form>
        <div class="hint">Текущий токен: {{ vk_token_masked }}</div>
      </section>

      {% if preview %}
      <section class="card" style="grid-column: 1 / -1;">
        <h3>Последнее отправленное сообщение</h3>
        <pre>{{ preview }}</pre>
      </section>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""


@app.get("/")
def index():
    config_error = ""
    default_parser_url = DEFAULT_PARSER_URL
    default_interval = DEFAULT_PARSER_INTERVAL_SECONDS

    try:
        cfg = load_target_config()
        default_parser_url = cfg.data_url
        default_interval = cfg.parser_interval_seconds
    except Exception as exc:  # noqa: BLE001
        config_error = str(exc)

    with state.lock:
        parser_interval_seconds = state.parser_interval_seconds or default_interval
        parser_url = state.parser_url or default_parser_url
        step = state.step

        current_error = state.error or config_error
        vk_token_masked = mask_token(os.getenv("VK_USER_TOKEN", ""))

        return render_template_string(
            TEMPLATE,
            login_status=describe_login_status(step),
            parser_status=describe_parser_status(
                step, state.parser_running, parser_interval_seconds),
            parser_last_check_at=state.parser_last_check_at,
            parser_last_sent_at=state.parser_last_sent_at,
            parser_last_match_title=state.parser_last_match_title,
            parser_error=state.parser_error,
            info=state.info,
            error=current_error,
            preview=state.preview,
            message_id=state.last_message_id,
            parser_url=parser_url,
            vk_token_masked=vk_token_masked,
            parser_interval_seconds=parser_interval_seconds,
            can_manage_parser=(step == "ready" and state.auth_storage_state is not None),
        )


@app.post("/start-login")
def start_login():
    password = request.form.get("password", "").strip()

    with state.lock:
        state.error = ""
        state.info = ""

    if not password:
        with state.lock:
            state.error = "Нужно передать пароль"
        return redirect(url_for("index"))

    try:
        cfg = load_target_config()

        state.stop_parser()
        state.clear_runtime()

        with state.lock:
            state.preview = ""
            state.last_message_id = None
            state.step = "idle"
            state.auth_storage_state = None

        state.playwright = sync_playwright().start()
        browser = state.playwright.chromium.launch(headless=cfg.headless)
        context = browser.new_context()
        state.page = context.new_page()

        state.page.goto(
            cfg.login_url, wait_until="domcontentloaded", timeout=30000)

        if cfg.open_login_selector:
            try:
                state.page.click(cfg.open_login_selector, timeout=10000)
            except Exception:  # noqa: BLE001
                pass

        username_input_visible = try_wait_visible(
            state.page, cfg.email_selector, timeout_ms=7000)
        if username_input_visible:
            if not cfg.login_username:
                raise ValueError(
                    "Не задан TARGET_LOGIN_USERNAME в .env (логин/почта для входа)")
            state.page.fill(cfg.email_selector,
                            cfg.login_username, timeout=10000)

        state.page.fill(cfg.password_selector, password, timeout=10000)
        state.page.click(cfg.submit_selector, timeout=10000)
        state.page.wait_for_timeout(1500)

        if try_wait_visible(state.page, cfg.code_selector, timeout_ms=3500):
            with state.lock:
                state.step = "await_code"
            return redirect(url_for("index"))

        login_errors = get_visible_texts(state.page, cfg.login_error_selector)
        joined_errors = " | ".join(login_errors).lower()
        has_invalid_password = any(
            marker in joined_errors
            for marker in ("неверный пароль", "неверный логин", "invalid", "error")
        )

        if has_invalid_password:
            raise ValueError(f"Ошибка входа: {'; '.join(login_errors)}")

        if is_login_form_visible(state.page, cfg.login_form_selector):
            raise ValueError(
                "Вход не подтвержден: форма логина все еще активна")

        auth_storage_state = state.page.context.storage_state()
        with state.lock:
            state.step = "ready"
            state.auth_storage_state = auth_storage_state
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Ошибка на шаге логина: {exc}"
            state.step = "idle"
            state.auth_storage_state = None
        state.clear_runtime()

    return redirect(url_for("index"))


@app.post("/submit-code")
def submit_code():
    code = request.form.get("code", "").strip()

    with state.lock:
        state.error = ""
        state.info = ""

    if not code:
        with state.lock:
            state.error = "Нужно передать код"
        return redirect(url_for("index"))

    with state.lock:
        if state.step != "await_code" or state.page is None:
            state.error = "Сначала выполни вход"
            return redirect(url_for("index"))

    try:
        cfg = load_target_config()

        with state.lock:
            page = state.page

        if page is None:
            raise RuntimeError("Сессия браузера недоступна")

        page.fill(cfg.code_selector, code, timeout=10000)
        page.click(cfg.code_submit_selector, timeout=10000)
        page.wait_for_timeout(2000)

        login_errors = get_visible_texts(page, cfg.login_error_selector)
        if login_errors:
            joined = "; ".join(login_errors)
            raise ValueError(f"Код не принят: {joined}")

        if is_login_form_visible(page, cfg.login_form_selector):
            raise ValueError("Код не подтвержден: форма логина снова активна")

        auth_storage_state = page.context.storage_state()
        with state.lock:
            state.step = "ready"
            state.auth_storage_state = auth_storage_state
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Ошибка на шаге кода: {exc}"

    return redirect(url_for("index"))


@app.post("/start-parser")
def start_parser():
    with state.lock:
        state.error = ""
        state.info = ""

    parse_url = request.form.get("parse_url", "").strip()

    try:
        cfg = load_target_config()
        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        if not parse_url:
            parse_url = cfg.data_url

        if not parse_url.startswith(("http://", "https://")):
            raise ValueError("Ссылка должна начинаться с http:// или https://")

        with state.lock:
            storage_state = state.auth_storage_state
        if storage_state is None:
            raise RuntimeError("Сессия авторизации недоступна. Выполни вход заново.")

        start_parser_thread(cfg, parse_url, storage_state)
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось запустить парсер: {exc}"

    return redirect(url_for("index"))


@app.post("/stop-parser")
def stop_parser():
    state.stop_parser()
    with state.lock:
        state.error = ""
        state.info = ""
    return redirect(url_for("index"))


@app.post("/send-test-message")
def send_test_message():
    with state.lock:
        state.error = ""
        state.info = ""

    try:
        vk_cfg = load_vk_config()
        message = (
            "Тестовое сообщение VK\n"
            "------------------------------\n"
            "Проверка отправки выполнена успешно.\n"
            f"Время: {now_label()}"
        )
        message_id = send_vk_message(vk_cfg, message)

        with state.lock:
            state.preview = message
            state.last_message_id = message_id
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Тестовая отправка в VK не удалась: {exc}"

    return redirect(url_for("index"))


@app.post("/update-vk-token")
def update_vk_token():
    new_token = request.form.get("vk_user_token", "").strip()

    with state.lock:
        state.error = ""
        state.info = ""

    if not new_token:
        with state.lock:
            state.error = "Нужно передать VK_USER_TOKEN"
        return redirect(url_for("index"))

    try:
        upsert_env_value("VK_USER_TOKEN", new_token)
        os.environ["VK_USER_TOKEN"] = new_token

        with state.lock:
            state.info = "VK_USER_TOKEN обновлен"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось обновить VK_USER_TOKEN: {exc}"

    return redirect(url_for("index"))


@app.post("/reset")
def reset():
    state.reset()
    return redirect(url_for("index"))


if __name__ == "__main__":
    load_dotenv()
    app.run(host="127.0.0.1", port=int(
        os.getenv("LOCAL_WEB_PORT", "5050")), debug=False)
