"""
Следит за скидками на пижамы Victoria's Secret и шлёт уведомление в Telegram,
когда появляется что-то НОВОЕ. Ловит:
  1) пижамы с зачёркнутой (старой) ценой — реальные уценки;
  2) сонные офферы из меню (Buy 1 Get 1, up to 60% off sleep и т.п.);
  3) баннеры-плашки прямо в разделе (Save $X when you spend $Y и т.п.).
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

# Баннеры-акции (шаблоны), которые ищем в тексте раздела
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

# Для офферов из меню: признак скидки + признак "это про сон"
MENU_PROMO_RE = re.compile(
    r"%|\bbuy\s*1\b|\bbuy\s*2\b|\bbogo\b|save\s*\$|\d\s*/\s*\$|\bfree\b", re.IGNORECASE
)
MENU_SLEEP_RE = re.compile(
    r"sleep|pajama|pyjama|sleepshirt|nightgown|loungewear|robe|cami", re.IGNORECASE
)

# JS, который находит товары с зачёркнутой ценой прямо в отрисованной странице
FIND_SALES_JS = r"""
() => {
  const priceRe = /\$\s?\d[\d.,]*/;
  function getPrice(s){ const m=(s||'').match(priceRe); return m?m[0].replace(/\s/g,''):null; }
  function isStruck(node){
    let n = node;
    for (let i=0;i<5 && n;i++){
      const tag = n.tagName ? n.tagName.toLowerCase() : '';
      if (tag==='s'||tag==='del'||tag==='strike') return true;
      try {
        const cs = getComputedStyle(n);
        const d = (cs.textDecorationLine||cs.textDecoration||'');
        if (d.indexOf('line-through')>=0) return true;
      } catch(e){}
      n = n.parentElement;
    }
    return false;
  }
  const root = document.querySelector('main') || document.getElementById('main') || document.body;
  const els = Array.from(root.querySelectorAll('*'));
  const sales = [];
  for (const el of els){
    const t=(el.textContent||'').trim();
    if (t.length>25) continue;
    if (!priceRe.test(t)) continue;
    if (Array.from(el.children).some(c => (c.textContent||'').trim()===t)) continue;
    if (!isStruck(el)) continue;
    const oldPrice = getPrice(t); if(!oldPrice) continue;
    let node=el, name=null, newPrice=null;
    for (let i=0;i<10 && node;i++){
      node=node.parentElement; if(!node) break;
      if(!name){
        const h=node.querySelector('h1,h2,h3,h4');
        if(h){ const ht=(h.textContent||'').trim(); if(ht && !priceRe.test(ht)) name=ht; }
      }
      if(!newPrice){
        const cand=Array.from(node.querySelectorAll('*')).filter(x=>{
          const xt=(x.textContent||'').trim();
          return xt.length<=25 && priceRe.test(xt) &&
                 !Array.from(x.children).some(c=>(c.textContent||'').trim()===xt);
        });
        for(const c of cand){ if(isStruck(c)) continue; const p=getPrice(c.textContent); if(p && p!==oldPrice){newPrice=p;break;} }
      }
      if(name&&newPrice) break;
    }
    sales.push({name: name||'Пижама (см. сайт)', oldPrice: oldPrice, newPrice: newPrice||'?'});
  }
  const seen=new Set(), out=[];
  for(const s of sales){ const k=s.name+'|'+s.oldPrice+'|'+s.newPrice; if(!seen.has(k)){seen.add(k);out.push(s);} }
  return out;
}
"""


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("Нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID — пропускаю отправку.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text[:4000], "disable_web_page_preview": False},
            timeout=30,
        )
        print("Telegram статус:", r.status_code)
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


def scrape():
    """Возвращает (текст_раздела, список_ссылок, список_уценённых_товаров)."""
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
        page.wait_for_timeout(4000)

        # Прокручиваем вниз, чтобы подгрузились товары и их цены
        prev_h = 0
        for _ in range(25):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            h = page.evaluate("document.body.scrollHeight")
            if h == prev_h:
                break
            prev_h = h
        page.wait_for_timeout(1500)

        node = page.query_selector("main") or page.query_selector("#main")
        main_text = node.inner_text() if node else page.inner_text("body")

        links = page.evaluate(
            "() => Array.from(document.querySelectorAll('a'))"
            ".map(a => ({text:(a.textContent||'').trim(), href:a.href}))"
        )

        try:
            sales = page.evaluate(FIND_SALES_JS)
        except Exception as e:
            print("Не смог разобрать цены:", e)
            sales = []

        browser.close()
    return main_text, links, sales


def collect_signals(main_text, links, sales):
    signals = set()

    # 1) Уценённые товары (зачёркнутая цена)
    for s in sales:
        name = (s.get("name") or "Пижама").strip()
        old = s.get("oldPrice") or "?"
        new = s.get("newPrice") or "?"
        signals.add(f"🔻 {name}: {old} → {new}")

    # 2) Сонные офферы из меню
    for l in links:
        text = (l.get("text") or "").strip()
        href = (l.get("href") or "")
        if not text or len(text) > 80:
            continue
        if MENU_PROMO_RE.search(text) and (MENU_SLEEP_RE.search(text) or MENU_SLEEP_RE.search(href)):
            clean = re.sub(r"\s+", " ", text)
            signals.add(f"🛏 {clean}")

    # 3) Баннеры-плашки в тексте раздела
    flat = re.sub(r"\s+", " ", main_text)
    for pat in PROMO_PATTERNS:
        for m in re.finditer(pat, flat, flags=re.IGNORECASE):
            t = m.group(0).strip(" .,-")
            if len(t) > 3:
                signals.add(f"🏷 {t}")

    return signals


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if "signals" not in st:          # старый формат -> начинаем заново
            return set(), False
        return set(st.get("signals", [])), bool(st.get("initialized", False))
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), False


def save_state(signals):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "signals": sorted(signals),
                "initialized": True,
                "last_checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            f, ensure_ascii=False, indent=2,
        )


def shorten(items, limit=20):
    items = sorted(items)
    if len(items) <= limit:
        return "\n".join(items)
    return "\n".join(items[:limit]) + f"\n…и ещё {len(items) - limit}"


def main():
    try:
        main_text, links, sales = scrape()
    except Exception as e:
        print("Не удалось загрузить страницу:", e)
        sys.exit(0)

    if "pajama" not in main_text.lower():
        print("Похоже, страница не загрузилась или нас заблокировали. Пропускаю запуск.")
        sys.exit(0)

    current = collect_signals(main_text, links, sales)
    previous, initialized = load_state()

    if not initialized:
        msg = "✅ Бот обновлён и следит за скидками на пижамы Victoria's Secret.\n\n"
        if current:
            msg += f"Сейчас активно ({len(current)}):\n" + shorten(current)
        else:
            msg += "Сейчас скидок и офферов не вижу. Напишу, как только появятся."
        msg += f"\n\n{URL}"
        send_telegram(msg)
    else:
        new = current - previous
        if new:
            msg = f"🔥 Новое в пижамах Victoria's Secret ({len(new)}):\n\n" + shorten(new)
            msg += f"\n\n{URL}"
            send_telegram(msg)
        else:
            print("Нового нет.")

    save_state(current)
    print(f"Всего сигналов: {len(current)} (уценок: {len(sales)})")


if __name__ == "__main__":
    main()
