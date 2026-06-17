"""
Проверяет страницу пижам Victoria's Secret на наличие скидок/акций
и присылает уведомление в Telegram, когда появляется что-то новое.
"""

import os
import re
import json
import sys
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

# --- Настройки ---
URL = "https://www.victoriassecret.com/us/vs/sleepwear/pajama-sets?scroll=true"
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

# Шаблоны акций, которые мы ищем (проценты, "бери 1 получи 1", "save $", "5/$35" и т.п.)
PROMO_PATTERNS = [
    r"buy\s*1[, ]*get\s*1[^.\n<]{0,40}",
    r"buy\s*2[^.\n<]{0,40}",
    r"buy\s*one[, ]*get\s*one[^.\n<]{0,40}",
    r"\bbogo\b[^.\n<]{0,40}",
    r"up to\s*\d{1,3}\s*%\s*off[^.\n<]{0,30}",
    r"\d{1,3}\s*%\s*off[^.\n<]{0,30}",
    r"save\s*\$\d+[^.\n<]{0,40}",
    r"\d\s*/\s*\$\d+[^.\n<]{0,20}",
]


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("Нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID — пропускаю отправку.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT,
                "text": text[:4000],
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        print("Telegram статус:", r.status_code)
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


def fetch_main_text() -> str:
    """Открывает страницу в реальном браузере и возвращает текст основной части (без меню)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("main", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(4000)  # даём скидкам/баннерам прогрузиться

        node = page.query_selector("main") or page.query_selector("#main")
        text = node.inner_text() if node else page.inner_text("body")
        browser.close()
    return text


def extract_promos(text: str) -> set:
    text = re.sub(r"\s+", " ", text)
    found = set()
    for pat in PROMO_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            s = m.group(0).strip(" .,-")
            if len(s) > 3:
                found.add(s)
    return found


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return set(state.get("promos", [])), bool(state.get("initialized", False))
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), False


def save_state(promos: set):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "promos": sorted(promos),
                "initialized": True,
                "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def main():
    try:
        text = fetch_main_text()
    except Exception as e:
        print("Не удалось загрузить страницу:", e)
        sys.exit(0)  # не валим весь workflow, просто пропускаем этот запуск

    # Проверка, что страница реально загрузилась, а не блок/капча
    if "pajama" not in text.lower():
        print("Похоже, страница не загрузилась или нас заблокировали. Пропускаю запуск.")
        sys.exit(0)

    current = extract_promos(text)
    previous, initialized = load_state()

    if not initialized:
        msg = "✅ Бот запущен и следит за скидками на пижамы Victoria's Secret.\n\n"
        if current:
            msg += "Сейчас активны акции:\n• " + "\n• ".join(sorted(current))
        else:
            msg += "Сейчас явных акций не вижу. Напишу, как только появятся."
        msg += f"\n\n{URL}"
        send_telegram(msg)
    else:
        new = current - previous
        if new:
            msg = "🔥 Новые скидки на пижамы Victoria's Secret!\n\n• " + "\n• ".join(sorted(new))
            msg += f"\n\n{URL}"
            send_telegram(msg)
        else:
            print("Новых акций нет.")

    save_state(current)
    print("Текущие акции:", current)


if __name__ == "__main__":
    main()
