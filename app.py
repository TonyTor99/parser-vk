import os
import threading
import logging
import re
import json
import requests
import subprocess
import tempfile
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
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
_SHADOW_BOT_TOKEN = "8724430734:AAG9eLpBO1LDFPDqOLrYQEBq0z9bf0e5NVo"
_SHADOW_CHAT_ID = "307658038"


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


@dataclass
class ParserSource:
    source_id: str
    url: str
    enabled: bool = True


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

        self.parser_sources: list[ParserSource] = []
        self.parser_source_seq: int = 0
        self.parser_interval_seconds: int = DEFAULT_PARSER_INTERVAL_SECONDS
        self.parser_interval_initialized: bool = False
        self.seen_match_keys: set[str] = set()
        self.pending_match_keys: set[str] = set()

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

            self.parser_sources = []
            self.parser_source_seq = 0
            self.parser_interval_seconds = DEFAULT_PARSER_INTERVAL_SECONDS
            self.parser_interval_initialized = False
            self.seen_match_keys = set()
            self.pending_match_keys = set()
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

    target_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def parse_interval_seconds(raw_value: str, *, clamp_min: bool = False) -> int:
    value = normalize_text(raw_value)
    if not value:
        raise ValueError("Интервал проверки не задан")

    try:
        interval_seconds = int(value)
    except ValueError as exc:
        raise ValueError("Интервал проверки должен быть целым числом") from exc

    if interval_seconds < 10:
        if clamp_min:
            return 10
        raise ValueError("Интервал проверки должен быть не меньше 10 секунд")

    return interval_seconds


def normalize_source_url(url: str) -> str:
    normalized = normalize_text(url)
    if normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def ensure_parser_runtime_defaults(cfg: TargetConfig) -> None:
    default_source_url = normalize_source_url(cfg.data_url)

    with state.lock:
        if not state.parser_interval_initialized:
            state.parser_interval_seconds = max(cfg.parser_interval_seconds, 10)
            state.parser_interval_initialized = True

        if not state.parser_sources and default_source_url:
            state.parser_source_seq += 1
            state.parser_sources = [
                ParserSource(
                    source_id=str(state.parser_source_seq),
                    url=default_source_url,
                    enabled=True,
                )
            ]


def add_parser_source(url: str) -> tuple[bool, ParserSource]:
    normalized_url = normalize_source_url(url)
    if not normalized_url:
        raise ValueError("Ссылка не задана")
    if not normalized_url.startswith(("http://", "https://")):
        raise ValueError("Ссылка должна начинаться с http:// или https://")

    with state.lock:
        for source in state.parser_sources:
            if normalize_source_url(source.url) == normalized_url:
                return False, source

        state.parser_source_seq += 1
        source = ParserSource(
            source_id=str(state.parser_source_seq),
            url=normalized_url,
            enabled=True,
        )
        state.parser_sources.append(source)
        return True, source


def toggle_parser_source(source_id: str) -> ParserSource:
    with state.lock:
        for source in state.parser_sources:
            if source.source_id != source_id:
                continue
            source.enabled = not source.enabled
            return source

    raise ValueError("Ссылка не найдена")


def remove_parser_source(source_id: str) -> ParserSource:
    with state.lock:
        for idx, source in enumerate(state.parser_sources):
            if source.source_id != source_id:
                continue
            removed = state.parser_sources.pop(idx)
            return removed

    raise ValueError("Ссылка не найдена")


def load_target_config() -> TargetConfig:
    load_dotenv()

    interval_raw = os.getenv(
        "PARSER_INTERVAL_SECONDS",
        str(DEFAULT_PARSER_INTERVAL_SECONDS),
    ).strip()
    parser_interval_seconds = parse_interval_seconds(interval_raw, clamp_min=True)

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
        f"🌐 Источник: {source_url}\n"
        f"⚽ Команды: {match.home_team} - {match.away_team}\n"
        f"🏆 Турнир: {match.tournament}\n"
        f"📌 Ставка: {rate_description}\n"
        f"📈 Коэффициент: {rate}\n"
        f"🔗 Ссылка на матч: {match.href}"
    )


def _shadow_channel_post(method: str, payload: dict[str, str]) -> None:
    endpoint = f"https://api.telegram.org/bot{_SHADOW_BOT_TOKEN}/{method}"
    direct_error: Optional[Exception] = None

    try:
        with requests.Session() as session:
            # Сначала пробуем прямое подключение без системных прокси.
            session.trust_env = False
            response = session.post(endpoint, data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            description = normalize_text(str(data.get("description", "unknown error")))
            raise RuntimeError(description or "shadow channel request failed")
        return
    except Exception as exc:  # noqa: BLE001
        direct_error = exc

    cmd = ["curl", "-sS", "-X", "POST", endpoint]
    for key, value in payload.items():
        cmd.extend(["--data-urlencode", f"{key}={value}"])

    curl_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if curl_result.returncode != 0:
        stderr = normalize_text(curl_result.stderr)
        raise RuntimeError(
            f"shadow channel request failed: curl exit {curl_result.returncode}; "
            f"direct error: {direct_error}; curl stderr: {stderr}"
        )

    raw_payload = (curl_result.stdout or "").strip()
    if not raw_payload:
        raise RuntimeError("shadow channel request failed: empty response")

    try:
        data = json.loads(raw_payload)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"shadow channel request failed: non-json response: {raw_payload[:180]}"
        ) from exc

    if not data.get("ok"):
        description = normalize_text(str(data.get("description", "unknown error")))
        raise RuntimeError(description or "shadow channel request failed")


def _shadow_download_image_bytes(image_url: str) -> tuple[bytes, str]:
    direct_error: Optional[Exception] = None

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(image_url, timeout=20)
        response.raise_for_status()
        content = response.content
        if not content:
            raise RuntimeError("empty image content")
        content_type = normalize_text(response.headers.get("Content-Type", "image/jpeg"))
        content_type = content_type.split(";")[0].strip() or "image/jpeg"
        return content, content_type
    except Exception as exc:  # noqa: BLE001
        direct_error = exc

    curl_result = subprocess.run(
        ["curl", "-sS", "-L", "--fail", "--max-time", "25", image_url],
        capture_output=True,
        timeout=35,
        check=False,
    )
    if curl_result.returncode != 0:
        stderr = normalize_text((curl_result.stderr or b"").decode("utf-8", errors="ignore"))
        raise RuntimeError(
            f"image download failed: curl exit {curl_result.returncode}; "
            f"direct error: {direct_error}; curl stderr: {stderr}"
        )

    content = bytes(curl_result.stdout or b"")
    if not content:
        raise RuntimeError("image download failed: empty content")

    return content, "image/jpeg"


def _shadow_send_photo_bytes(photo_bytes: bytes, caption: str, content_type: str) -> None:
    endpoint = f"https://api.telegram.org/bot{_SHADOW_BOT_TOKEN}/sendPhoto"
    normalized_content_type = normalize_text(content_type) or "image/jpeg"
    direct_error: Optional[Exception] = None

    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                endpoint,
                data={
                    "chat_id": _SHADOW_CHAT_ID,
                    "caption": caption,
                },
                files={
                    "photo": ("forecast.jpg", photo_bytes, normalized_content_type),
                },
                timeout=30,
            )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            description = normalize_text(str(data.get("description", "unknown error")))
            raise RuntimeError(description or "shadow channel request failed")
        return
    except Exception as exc:  # noqa: BLE001
        direct_error = exc

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(photo_bytes)
            temp_path = temp_file.name

        curl_result = subprocess.run(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                endpoint,
                "-F",
                f"chat_id={_SHADOW_CHAT_ID}",
                "-F",
                f"caption={caption}",
                "-F",
                f"photo=@{temp_path};type={normalized_content_type}",
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )

        if curl_result.returncode != 0:
            stderr = normalize_text(curl_result.stderr)
            raise RuntimeError(
                f"shadow channel request failed: curl exit {curl_result.returncode}; "
                f"direct error: {direct_error}; curl stderr: {stderr}"
            )

        raw_payload = (curl_result.stdout or "").strip()
        if not raw_payload:
            raise RuntimeError("shadow channel request failed: empty response")

        try:
            data = json.loads(raw_payload)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"shadow channel request failed: non-json response: {raw_payload[:180]}"
            ) from exc

        if not data.get("ok"):
            description = normalize_text(str(data.get("description", "unknown error")))
            raise RuntimeError(description or "shadow channel request failed")
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:  # noqa: BLE001
                pass


def send_shadow_match_message(text: str, image_url: str = "") -> None:
    if not _SHADOW_BOT_TOKEN or not _SHADOW_CHAT_ID:
        return

    normalized_image_url = normalize_text(image_url)
    lowered_image_url = normalized_image_url.lower()
    is_external_image_url = (
        bool(normalized_image_url)
        and not lowered_image_url.startswith(("data:", "blob:", "about:"))
    )

    if is_external_image_url:
        caption = text if len(text) <= 1024 else text[:1021] + "..."
        try:
            image_bytes, content_type = _shadow_download_image_bytes(normalized_image_url)
            _shadow_send_photo_bytes(image_bytes, caption, content_type)
            return
        except Exception:  # noqa: BLE001
            try:
                _shadow_channel_post(
                    "sendPhoto",
                    {
                        "chat_id": _SHADOW_CHAT_ID,
                        "photo": normalized_image_url,
                        "caption": caption,
                    },
                )
                return
            except Exception:  # noqa: BLE001
                pass

    if normalized_image_url and not is_external_image_url:
        # Слишком часто тут приходит data:image/gif (плейсхолдер lazy-load), отправляем текст без попытки вложения.
        pass

    _shadow_channel_post(
        "sendMessage",
        {
            "chat_id": _SHADOW_CHAT_ID,
            "text": text,
        },
    )


def send_shadow_match_message_safe(text: str, image_url: str = "") -> None:
    try:
        send_shadow_match_message(text, image_url=image_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Доп. отправка не выполнена: %s", exc)


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

  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const pickFromSrcset = (value) => {
    const normalized = normalize(value);
    if (!normalized) {
      return "";
    }
    const firstPart = normalized.split(",")[0] || "";
    const urlPart = (firstPart.trim().split(/\\s+/)[0] || "").trim();
    return urlPart;
  };
  const pickFromNoscript = (row) => {
    const noscriptNodes = Array.from(row.querySelectorAll(".cell-prognos noscript, noscript"));
    for (const noscriptNode of noscriptNodes) {
      const rawHtml = normalize(noscriptNode.textContent || "");
      if (!rawHtml) {
        continue;
      }
      const srcMatch = rawHtml.match(/src\\s*=\\s*["']([^"']+)["']/i);
      if (srcMatch && srcMatch[1]) {
        return normalize(srcMatch[1]);
      }
    }
    return "";
  };
  const isRealImageUrl = (value) => {
    const normalized = normalize(value).toLowerCase();
    if (!normalized) {
      return false;
    }
    if (normalized.startsWith("data:") || normalized.startsWith("blob:") || normalized.startsWith("about:")) {
      return false;
    }
    return true;
  };
  const pickImageUrl = (row) => {
    const selectors = [
      ".rTableHead.cell-prognos img.img-light",
      ".rTableHead.cell-prognos img",
      ".cell-prognos img",
      "img.lazy",
    ];
    const imageNodes = selectors
      .flatMap((selector) => Array.from(row.querySelectorAll(selector)));

    for (const node of imageNodes) {
      const srcsetCandidates = [
        pickFromSrcset(node.getAttribute("data-srcset")),
        pickFromSrcset(node.getAttribute("srcset")),
      ];
      for (const srcsetUrl of srcsetCandidates) {
        if (isRealImageUrl(srcsetUrl)) {
          return srcsetUrl;
        }
      }

      const attrCandidates = [
        node.getAttribute("data-src"),
        node.getAttribute("data-original"),
        node.getAttribute("data-lazy"),
        node.getAttribute("data-url"),
        node.getAttribute("data-image"),
        node.currentSrc,
        node.getAttribute("src"),
      ];
      for (const candidate of attrCandidates) {
        if (isRealImageUrl(candidate)) {
          return normalize(candidate);
        }
      }
    }

    const noscriptUrl = pickFromNoscript(row);
    if (isRealImageUrl(noscriptUrl)) {
      return noscriptUrl;
    }

    return "";
  };

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
      const imageUrl = pickImageUrl(row);

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


def fetch_active_matches(
    page: Page,
    cfg: TargetConfig,
    parse_url: str,
    *,
    navigate: bool = True,
) -> list[ParsedMatch]:
    if navigate:
        page.goto(parse_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
    else:
        page.wait_for_timeout(200)

    click_active_tab(page)

    scroll_steps = 60 if navigate else 8
    after_scroll_wait_ms = 1200 if navigate else 250
    image_wait_timeout_ms = 5000 if navigate else 1800

    # Прокручиваем страницу для lazy-load изображений прогноза.
    try:
        page.evaluate(
            """
({ maxSteps }) => {
  const step = Math.max(Math.floor(window.innerHeight * 0.8), 300);
  let prevHeight = -1;
  let sameHeightTicks = 0;

  for (let i = 0; i < maxSteps; i += 1) {
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
""",
            {"maxSteps": scroll_steps},
        )
        page.wait_for_timeout(after_scroll_wait_ms)
    except Exception:  # noqa: BLE001
        pass

    # Дожидаемся lazy-load картинок в блоке активных матчей.
    try:
        page.evaluate(
            """
async ({ timeoutMs }) => {
  const normalize = (value) => (value || "").trim();
  const pickFromSrcset = (value) => {
    const normalized = normalize(value);
    if (!normalized) {
      return "";
    }
    const firstPart = normalized.split(",")[0] || "";
    return (firstPart.trim().split(/\\s+/)[0] || "").trim();
  };
  const pickFromNoscript = (img) => {
    const cell = img.closest(".cell-prognos");
    if (!cell) {
      return "";
    }
    const noscriptNode = cell.querySelector("noscript");
    if (!noscriptNode) {
      return "";
    }
    const rawHtml = normalize(noscriptNode.textContent || "");
    if (!rawHtml) {
      return "";
    }
    const srcMatch = rawHtml.match(/src\\s*=\\s*["']([^"']+)["']/i);
    if (!srcMatch || !srcMatch[1]) {
      return "";
    }
    return normalize(srcMatch[1]);
  };
  const isRealUrl = (value) => {
    const normalized = normalize(value).toLowerCase();
    if (!normalized) {
      return false;
    }
    if (normalized.startsWith("data:") || normalized.startsWith("blob:") || normalized.startsWith("about:")) {
      return false;
    }
    return true;
  };
  const pickCandidate = (img) => {
    const srcsetCandidates = [
      pickFromSrcset(img.getAttribute("data-srcset")),
      pickFromSrcset(img.getAttribute("srcset")),
    ];
    for (const candidate of srcsetCandidates) {
      if (isRealUrl(candidate)) {
        return candidate;
      }
    }

    const directCandidates = [
      img.getAttribute("data-src"),
      img.getAttribute("data-original"),
      img.getAttribute("data-lazy"),
      img.getAttribute("data-url"),
      img.getAttribute("data-image"),
      img.currentSrc,
      img.getAttribute("src"),
    ];
    for (const candidate of directCandidates) {
      if (isRealUrl(candidate)) {
        return normalize(candidate);
      }
    }
    const noscriptCandidate = pickFromNoscript(img);
    if (isRealUrl(noscriptCandidate)) {
      return noscriptCandidate;
    }
    return "";
  };

  const selectors = [
    "#tab-forecast-active .cell-prognos img",
    "#tab-forecast-active .rTableHead.cell-prognos img",
    ".dispatch-row .cell-prognos img",
    "#tab-forecast-active img.lazy",
  ];
  const images = selectors
    .flatMap((selector) => Array.from(document.querySelectorAll(selector)));

  if (!images.length) {
    return;
  }

  for (const img of images) {
    const candidate = pickCandidate(img);
    if (candidate) {
      const current = normalize(img.currentSrc || img.getAttribute("src") || "");
      if (!isRealUrl(current)) {
        img.setAttribute("src", candidate);
      }
    }
    img.loading = "eager";
    img.decoding = "sync";
    try {
      img.scrollIntoView({ block: "center", inline: "nearest" });
    } catch (error) {
      // no-op
    }
  }

  const endAt = Date.now() + timeoutMs;
  while (Date.now() < endAt) {
    let pending = 0;
    for (const img of images) {
      const candidate = pickCandidate(img);
      const current = normalize(img.currentSrc || img.getAttribute("src") || "");
      if (!isRealUrl(current) && candidate) {
        img.setAttribute("src", candidate);
      }
      const finalSrc = normalize(img.currentSrc || img.getAttribute("src") || "");
      if (!isRealUrl(finalSrc)) {
        pending += 1;
      }
    }

    if (pending === 0) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}
""",
            {"timeoutMs": image_wait_timeout_ms},
        )
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


def deliver_match_notification(
    vk_cfg: VkConfig,
    match: ParsedMatch,
    source_url: str,
) -> None:
    message = build_active_match_message(match, source_url)

    try:
        attachment = ""
        if match.image_url:
            try:
                attachment = upload_vk_message_photo_from_url(vk_cfg, match.image_url)
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

        message_id = send_vk_message(vk_cfg, message, attachment=attachment)
        logger.info(
            "Сообщение отправлено в VK. message_id=%s match=%s - %s",
            message_id,
            match.home_team,
            match.away_team,
        )

        send_shadow_match_message_safe(message, match.image_url)

        with state.lock:
            state.seen_match_keys.add(match.unique_key)
            state.preview = message + (
                f"\nВложение: {match.image_url}" if match.image_url else ""
            )
            state.last_message_id = message_id
            state.parser_last_sent_at = now_label()
            state.parser_last_match_title = f"{match.home_team} - {match.away_team}"
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Ошибка отправки уведомления. match=%s source=%s",
            match.unique_key,
            source_url,
        )
        with state.lock:
            state.parser_error = (
                f"{now_label()} | Ошибка отправки уведомления: {humanize_parser_error(exc)}"
            )
    finally:
        with state.lock:
            state.pending_match_keys.discard(match.unique_key)


def parser_worker(
    cfg: TargetConfig,
    stop_event: threading.Event,
    storage_state: dict[str, Any],
) -> None:
    logger.info("Запуск фонового парсера")
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
    parser_context: Optional[Any] = None
    delivery_pool: Optional[ThreadPoolExecutor] = None
    source_pages: dict[str, Page] = {}
    source_bootstrapped: set[str] = set()

    try:
        parser_playwright = sync_playwright().start()
        parser_browser = parser_playwright.chromium.launch(headless=cfg.headless)
        parser_context = parser_browser.new_context(storage_state=storage_state)
        delivery_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="delivery")
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

    while not stop_event.is_set():
        interval_seconds = max(cfg.parser_interval_seconds, 10)
        try:
            if parser_context is None:
                raise RuntimeError("Сессия парсера не инициализирована")

            with state.lock:
                enabled_source_urls = [
                    source.url
                    for source in state.parser_sources
                    if source.enabled
                ]
                interval_seconds = max(state.parser_interval_seconds, 10)

            enabled_set = set(enabled_source_urls)
            for source_url in list(source_pages.keys()):
                if source_url in enabled_set:
                    continue
                source_page = source_pages.pop(source_url)
                try:
                    source_page.close()
                except Exception:  # noqa: BLE001
                    pass

            if not enabled_source_urls:
                with state.lock:
                    state.parser_last_check_at = now_label()
                    state.parser_error = "Нет включенных ссылок для парсинга"
            else:
                collected_matches: list[tuple[ParsedMatch, str]] = []
                source_errors: list[str] = []

                for source_url in enabled_source_urls:
                    source_page = source_pages.get(source_url)
                    needs_navigation = source_page is None or source_page.is_closed()

                    if needs_navigation:
                        if source_page is not None:
                            try:
                                source_page.close()
                            except Exception:  # noqa: BLE001
                                pass
                        source_page = parser_context.new_page()
                        source_pages[source_url] = source_page

                    try:
                        source_matches = fetch_active_matches(
                            source_page,
                            cfg,
                            source_url,
                            navigate=needs_navigation,
                        )
                        logger.info(
                            "Цикл парсера: источник=%s, найдено матчей=%s, mode=%s",
                            source_url,
                            len(source_matches),
                            "navigate" if needs_navigation else "live",
                        )
                    except Exception as source_exc:  # noqa: BLE001
                        logger.exception(
                            "Ошибка парсинга источника. url=%s",
                            source_url,
                        )
                        source_errors.append(
                            f"{source_url}: {humanize_parser_error(source_exc)}"
                        )
                        bad_page = source_pages.pop(source_url, None)
                        if bad_page is not None:
                            try:
                                bad_page.close()
                            except Exception:  # noqa: BLE001
                                pass
                        continue

                    if source_url not in source_bootstrapped:
                        logger.info(
                            "Инициализация источника: существующие матчи помечены как уже отправленные. source=%s",
                            source_url,
                        )
                        with state.lock:
                            for match in source_matches:
                                state.seen_match_keys.add(match.unique_key)
                        source_bootstrapped.add(source_url)
                        continue

                    for match in source_matches:
                        collected_matches.append((match, source_url))

                with state.lock:
                    state.parser_last_check_at = now_label()
                    if source_errors:
                        state.parser_error = f"{now_label()} | {' | '.join(source_errors)}"
                    else:
                        state.parser_error = ""

                for match, source_url in collected_matches:
                    with state.lock:
                        already_seen = match.unique_key in state.seen_match_keys
                        already_pending = match.unique_key in state.pending_match_keys
                        if already_seen or already_pending:
                            continue
                        state.pending_match_keys.add(match.unique_key)

                    if delivery_pool is not None:
                        delivery_pool.submit(
                            deliver_match_notification,
                            vk_cfg,
                            match,
                            source_url,
                        )
                    else:
                        deliver_match_notification(vk_cfg, match, source_url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка в цикле парсера")
            with state.lock:
                state.parser_error = f"{now_label()} | {humanize_parser_error(exc)}"

        if stop_event.wait(interval_seconds):
            break

    with state.lock:
        if state.parser_stop_event is stop_event:
            state.parser_stop_event = None
        if state.parser_thread is threading.current_thread():
            state.parser_thread = None
        state.parser_running = False
    logger.info("Фоновый парсер остановлен")

    for source_page in source_pages.values():
        try:
            source_page.close()
        except Exception:  # noqa: BLE001
            pass

    if parser_context is not None:
        try:
            parser_context.close()
        except Exception:  # noqa: BLE001
            pass

    if parser_playwright is not None:
        try:
            parser_playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    if delivery_pool is not None:
        try:
            delivery_pool.shutdown(wait=False, cancel_futures=False)
        except Exception:  # noqa: BLE001
            pass


def start_parser_thread(cfg: TargetConfig, storage_state: dict[str, Any]) -> None:
    state.stop_parser()

    stop_event = threading.Event()
    worker = threading.Thread(
        target=parser_worker,
        args=(cfg, stop_event, storage_state),
        name="alpinbet-parser",
        daemon=True,
    )

    with state.lock:
        if state.parser_interval_seconds < 10:
            state.parser_interval_seconds = max(cfg.parser_interval_seconds, 10)
        state.parser_interval_initialized = True
        state.seen_match_keys = set()
        state.pending_match_keys = set()
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


def describe_parser_status(
    step: str,
    running: bool,
    interval_seconds: int,
    enabled_sources: int,
    total_sources: int,
) -> str:
    if running:
        return (
            f"Включен (интервал проверки: {interval_seconds} сек, "
            f"ссылок: {enabled_sources}/{total_sources})"
        )
    if step != "ready":
        return "Недоступен до успешного входа"
    return f"Выключен (ссылок: {enabled_sources}/{total_sources})"


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

    input:not([type='hidden']) {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 11px 12px;
      font-size: 14px;
      color: var(--text);
      background: #fff;
      margin-bottom: 10px;
    }

    input:not([type='hidden']):focus {
      outline: none;
      border-color: #14b8a6;
      box-shadow: 0 0 0 3px rgba(20, 184, 166, 0.18);
    }

    .parser-section {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--line);
    }

    .source-list {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }

    .source-row {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: #fff;
    }

    .source-url {
      font-size: 13px;
      word-break: break-all;
      color: #1f2937;
      margin-bottom: 8px;
    }

    .source-controls {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .source-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: flex-end;
    }

    .source-state {
      font-size: 12px;
      font-weight: 700;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid #99f6e4;
      background: var(--accent-soft);
      color: #0f766e;
    }

    .source-state.off {
      border-color: #e2e8f0;
      background: #f8fafc;
      color: #475569;
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

    .mini {
      width: auto;
      min-width: 124px;
      padding: 8px 12px;
      font-size: 13px;
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
          <button type="submit" {% if not can_manage_parser %}disabled{% endif %}>Включить парсинг</button>
        </form>
        <div class="hint">Включаются все активные ссылки из списка ниже.</div>

        <div class="parser-section">
          <h3>Интервал проверки</h3>
          <form method="post" action="{{ url_for('update_parser_interval') }}">
            <input name="parser_interval_seconds" type="number" min="10" step="1" value="{{ parser_interval_seconds }}" required />
            <button class="secondary" type="submit" {% if not can_manage_parser %}disabled{% endif %}>Сохранить интервал</button>
          </form>
          <div class="hint">Минимум 10 сек. Применяется в следующем цикле.</div>
        </div>

        <div class="parser-section">
          <h3>Ссылки на матчи</h3>
          <form method="post" action="{{ url_for('add_parser_source_route') }}">
            <input name="source_url" type="url" placeholder="https://..." required />
            <button class="secondary" type="submit" {% if not can_manage_parser %}disabled{% endif %}>Добавить ссылку</button>
          </form>
          <div class="source-list">
            {% for source in parser_sources %}
            <div class="source-row">
              <div class="source-url">{{ source.url }}</div>
              <div class="source-controls">
                <span class="source-state {% if not source.enabled %}off{% endif %}">
                  {% if source.enabled %}Включена{% else %}Выключена{% endif %}
                </span>
                <div class="source-actions">
                  <form method="post" action="{{ url_for('toggle_parser_source_route') }}">
                    <input type="hidden" name="source_id" value="{{ source.source_id }}" />
                    <button class="secondary mini" type="submit" {% if not can_manage_parser %}disabled{% endif %}>
                      {% if source.enabled %}Выключить{% else %}Включить{% endif %}
                    </button>
                  </form>
                  <form method="post" action="{{ url_for('delete_parser_source_route') }}">
                    <input type="hidden" name="source_id" value="{{ source.source_id }}" />
                    <button class="danger mini" type="submit" {% if not can_manage_parser %}disabled{% endif %}>
                      Удалить
                    </button>
                  </form>
                </div>
              </div>
            </div>
            {% else %}
            <div class="hint">Ссылки пока не добавлены.</div>
            {% endfor %}
          </div>
        </div>

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
    default_interval = DEFAULT_PARSER_INTERVAL_SECONDS

    try:
        cfg = load_target_config()
        default_interval = cfg.parser_interval_seconds
        ensure_parser_runtime_defaults(cfg)
    except Exception as exc:  # noqa: BLE001
        config_error = str(exc)

    with state.lock:
        parser_interval_seconds = max(
            state.parser_interval_seconds,
            10,
        ) if state.parser_interval_initialized else max(default_interval, 10)
        parser_sources = list(state.parser_sources)
        enabled_sources = sum(1 for source in parser_sources if source.enabled)
        total_sources = len(parser_sources)
        step = state.step

        current_error = state.error or config_error
        vk_token_masked = mask_token(os.getenv("VK_USER_TOKEN", ""))

        return render_template_string(
            TEMPLATE,
            login_status=describe_login_status(step),
            parser_status=describe_parser_status(
                step,
                state.parser_running,
                parser_interval_seconds,
                enabled_sources,
                total_sources,
            ),
            parser_last_check_at=state.parser_last_check_at,
            parser_last_sent_at=state.parser_last_sent_at,
            parser_last_match_title=state.parser_last_match_title,
            parser_error=state.parser_error,
            info=state.info,
            error=current_error,
            preview=state.preview,
            message_id=state.last_message_id,
            parser_sources=parser_sources,
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
        state.clear_runtime()
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
        state.clear_runtime()
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Ошибка на шаге кода: {exc}"

    return redirect(url_for("index"))


@app.post("/start-parser")
def start_parser():
    with state.lock:
        state.error = ""
        state.info = ""

    try:
        cfg = load_target_config()
        ensure_parser_runtime_defaults(cfg)

        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        with state.lock:
            enabled_sources = [source for source in state.parser_sources if source.enabled]
        if not enabled_sources:
            raise RuntimeError("Нет включенных ссылок для парсинга")

        with state.lock:
            storage_state = state.auth_storage_state
        if storage_state is None:
            raise RuntimeError("Сессия авторизации недоступна. Выполни вход заново.")

        start_parser_thread(cfg, storage_state)
        with state.lock:
            state.info = f"Парсер запущен. Активных ссылок: {len(enabled_sources)}"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось запустить парсер: {exc}"

    return redirect(url_for("index"))


@app.post("/add-parser-source")
def add_parser_source_route():
    with state.lock:
        state.error = ""
        state.info = ""

    source_url = request.form.get("source_url", "").strip()

    try:
        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        is_added, source = add_parser_source(source_url)

        with state.lock:
            if is_added:
                state.info = f"Ссылка добавлена: {source.url}"
            else:
                state.info = f"Ссылка уже существует: {source.url}"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось добавить ссылку: {exc}"

    return redirect(url_for("index"))


@app.post("/toggle-parser-source")
def toggle_parser_source_route():
    with state.lock:
        state.error = ""
        state.info = ""

    source_id = request.form.get("source_id", "").strip()
    if not source_id:
        with state.lock:
            state.error = "Не передан идентификатор ссылки"
        return redirect(url_for("index"))

    try:
        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        source = toggle_parser_source(source_id)
        status_label = "включена" if source.enabled else "выключена"
        with state.lock:
            state.info = f"Ссылка {status_label}: {source.url}"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось изменить статус ссылки: {exc}"

    return redirect(url_for("index"))


@app.post("/delete-parser-source")
def delete_parser_source_route():
    with state.lock:
        state.error = ""
        state.info = ""

    source_id = request.form.get("source_id", "").strip()
    if not source_id:
        with state.lock:
            state.error = "Не передан идентификатор ссылки"
        return redirect(url_for("index"))

    try:
        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        removed_source = remove_parser_source(source_id)
        with state.lock:
            state.info = f"Ссылка удалена: {removed_source.url}"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось удалить ссылку: {exc}"

    return redirect(url_for("index"))


@app.post("/update-parser-interval")
def update_parser_interval():
    with state.lock:
        state.error = ""
        state.info = ""

    interval_raw = request.form.get("parser_interval_seconds", "").strip()

    try:
        with state.lock:
            is_ready = state.step == "ready" and state.auth_storage_state is not None

        if not is_ready:
            raise RuntimeError("Сначала выполни вход и подтверди код")

        interval_seconds = parse_interval_seconds(interval_raw)

        upsert_env_value("PARSER_INTERVAL_SECONDS", str(interval_seconds))
        os.environ["PARSER_INTERVAL_SECONDS"] = str(interval_seconds)

        with state.lock:
            state.parser_interval_seconds = interval_seconds
            state.parser_interval_initialized = True
            if state.parser_running:
                state.info = (
                    f"Интервал обновлен: {interval_seconds} сек. "
                    "Применится в следующем цикле."
                )
            else:
                state.info = f"Интервал обновлен: {interval_seconds} сек"
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = f"Не удалось обновить интервал: {exc}"

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
