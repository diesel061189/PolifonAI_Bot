#!/usr/bin/env python3
"""
🤖 FL.ru Автооткликатор v1.0
Playwright — входит на FL.ru как живой человек и пишет отклик.

УСТАНОВКА на VPS:
  pip install playwright --break-system-packages
  python3 -m playwright install chromium
  python3 -m playwright install-deps chromium

ЗАПУСК:
  python3 fl_autoreply.py --url "https://www.fl.ru/projects/..." --message "Текст отклика"

Или автоматически из Полифана через subprocess.
"""

import os
import sys
import asyncio
import random
import logging
import argparse
import json
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger("FL_AUTO")

# ─── КОНФИГ ──────────────────────────────────────────────────────────────────
FL_LOGIN    = os.getenv("FL_LOGIN", "")       # email или логин FL.ru
FL_PASSWORD = os.getenv("FL_PASSWORD", "")    # пароль FL.ru
SENT_LOG    = "/opt/jarvis/fl_sent.json"      # лог отправленных откликов

# ─── ЧЕЛОВЕЧЕСКИЕ ЗАДЕРЖКИ ───────────────────────────────────────────────────
async def human_delay(min_sec=1.0, max_sec=3.0):
    """Случайная задержка как у живого человека."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))

async def human_type(page, selector, text):
    """Печатает текст как человек — по одному символу."""
    await page.click(selector)
    await human_delay(0.3, 0.8)
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.03, 0.12))

# ─── ЛОГ ОТПРАВЛЕННЫХ ────────────────────────────────────────────────────────
def load_sent_log() -> dict:
    try:
        with open(SENT_LOG, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_sent_log(data: dict):
    Path(SENT_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_LOG, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_already_sent(project_url: str) -> bool:
    log_data = load_sent_log()
    return project_url in log_data

def mark_as_sent(project_url: str, message: str):
    log_data = load_sent_log()
    log_data[project_url] = {
        "sent_at": datetime.now().isoformat(),
        "message_preview": message[:100]
    }
    save_sent_log(log_data)

# ─── ОСНОВНАЯ ФУНКЦИЯ ────────────────────────────────────────────────────────
async def send_fl_reply(project_url: str, message: str) -> bool:
    """
    Заходит на FL.ru, авторизуется если нужно,
    открывает заказ и отправляет отклик.
    Возвращает True если успешно.
    """
    from playwright.async_api import async_playwright

    if not FL_LOGIN or not FL_PASSWORD:
        log.error("❌ FL_LOGIN и FL_PASSWORD не заданы в .env!")
        return False

    if is_already_sent(project_url):
        log.info(f"⏭ Уже откликались на {project_url}")
        return True

    log.info(f"🚀 Открываю браузер...")

    async with async_playwright() as p:
        # Запускаем Chromium в headless режиме
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )

        page = await context.new_page()

        try:
            # ШАГ 1 — Проверяем авторизацию
            log.info("🔐 Проверяю авторизацию...")
            await page.goto("https://www.fl.ru/", wait_until="domcontentloaded")
            await human_delay(2, 4)

            # Проверяем залогинены ли мы
            is_logged_in = await page.locator(".user-menu, .b-header-user").count() > 0

            if not is_logged_in:
                log.info("🔑 Авторизуюсь...")
                await page.goto(
                    "https://www.fl.ru/login/",
                    wait_until="domcontentloaded"
                )
                await human_delay(1, 2)

                # Вводим логин
                await human_type(page, "input[name='login']", FL_LOGIN)
                await human_delay(0.5, 1.5)

                # Вводим пароль
                await human_type(page, "input[name='passwd']", FL_PASSWORD)
                await human_delay(0.5, 1.0)

                # Нажимаем войти
                await page.click("button[type='submit'], input[type='submit']")
                await page.wait_for_load_state("domcontentloaded")
                await human_delay(2, 4)

                # Проверяем что вошли
                if "login" in page.url:
                    log.error("❌ Авторизация не удалась! Проверь логин/пароль.")
                    return False

                log.info("✅ Авторизован!")

            # ШАГ 2 — Открываем страницу заказа
            log.info(f"📋 Открываю заказ: {project_url}")
            await page.goto(project_url, wait_until="domcontentloaded")
            await human_delay(2, 4)

            # ШАГ 3 — Ищем кнопку "Откликнуться"
            # FL.ru использует разные кнопки в разных версиях
            reply_btn_selectors = [
                "a.btn-reply",
                "a[href*='reply']",
                "button.b-layout__btn_reply",
                ".b-layout__btn-reply",
                "a:has-text('Откликнуться')",
                "button:has-text('Откликнуться')",
                ".reply-btn",
            ]

            reply_btn = None
            for selector in reply_btn_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        reply_btn = btn
                        log.info(f"✅ Нашёл кнопку: {selector}")
                        break
                except Exception:
                    continue

            if not reply_btn:
                # Делаем скриншот для диагностики
                await page.screenshot(path="/opt/jarvis/fl_debug.png")
                log.error("❌ Не нашёл кнопку 'Откликнуться'. Скриншот: /opt/jarvis/fl_debug.png")
                return False

            # Скроллим к кнопке
            await reply_btn.scroll_into_view_if_needed()
            await human_delay(0.5, 1.5)
            await reply_btn.click()
            await human_delay(1.5, 3)

            # ШАГ 4 — Вводим текст отклика
            # Ищем textarea для отклика
            textarea_selectors = [
                "textarea[name='reply']",
                "textarea.b-reply__text",
                "textarea[placeholder*='отклик']",
                "textarea[placeholder*='сообщение']",
                ".b-reply textarea",
                "textarea",
            ]

            textarea = None
            for selector in textarea_selectors:
                try:
                    ta = page.locator(selector).first
                    if await ta.count() > 0:
                        textarea = ta
                        log.info(f"✅ Нашёл textarea: {selector}")
                        break
                except Exception:
                    continue

            if not textarea:
                await page.screenshot(path="/opt/jarvis/fl_debug2.png")
                log.error("❌ Не нашёл поле для отклика. Скриншот: /opt/jarvis/fl_debug2.png")
                return False

            # Печатаем как человек
            await textarea.click()
            await human_delay(0.5, 1.0)
            for char in message:
                await page.keyboard.type(char)
                await asyncio.sleep(random.uniform(0.02, 0.08))

            await human_delay(1, 2)

            # ШАГ 5 — Отправляем
            send_selectors = [
                "button[type='submit']:has-text('Отправить')",
                "input[type='submit'][value*='Отправить']",
                "button.b-reply__submit",
                ".b-reply__btn-submit",
                "button:has-text('Отправить отклик')",
                "button:has-text('Откликнуться')",
            ]

            sent = False
            for selector in send_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.scroll_into_view_if_needed()
                        await human_delay(0.5, 1.5)
                        await btn.click()
                        await human_delay(2, 4)
                        sent = True
                        log.info(f"✅ Отклик отправлен через: {selector}")
                        break
                except Exception:
                    continue

            if not sent:
                await page.screenshot(path="/opt/jarvis/fl_debug3.png")
                log.error("❌ Не нашёл кнопку отправки. Скриншот: /opt/jarvis/fl_debug3.png")
                return False

            # Делаем скриншот подтверждения
            await page.screenshot(path="/opt/jarvis/fl_success.png")
            log.info(f"🎉 Успешно! Скриншот: /opt/jarvis/fl_success.png")

            # Записываем в лог
            mark_as_sent(project_url, message)
            return True

        except Exception as e:
            log.error(f"❌ Ошибка: {e}")
            try:
                await page.screenshot(path="/opt/jarvis/fl_error.png")
            except Exception:
                pass
            return False

        finally:
            await browser.close()


# ─── ГЕНЕРАЦИЯ ОТКЛИКА ЧЕРЕЗ GROQ ────────────────────────────────────────────
async def generate_reply(project_title: str, project_desc: str, executor: str = "Полифан") -> str:
    """Генерирует персональный отклик через Groq."""
    import httpx

    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
    if not GROQ_API_KEY:
        return (
            f"Здравствуйте! Готов взяться за задачу '{project_title}'. "
            f"Опыт в этой области есть, выполню качественно и в срок. "
            f"Готов обсудить детали и стоимость. Напишите!"
        )

    prompt = f"""Напиши короткий профессиональный отклик на заказ с FL.ru.

Заказ: {project_title}
Описание: {project_desc[:500]}
Исполнитель: {executor}

Требования к отклику:
- 3-5 предложений максимум
- Конкретно про задачу, не шаблонно
- Упомяни что есть опыт именно в этом
- Предложи обсудить детали
- Без лишних слов и восклицательных знаков через слово
- На русском языке
- НЕ начинай с "Здравствуйте, я готов" — это шаблон
- Пиши живо и конкретно"""

    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.7
                }
            )
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"Groq: {e}")

    return (
        f"Изучил задачу по {project_title}. "
        f"Выполню качественно — есть опыт именно в этом направлении. "
        f"Готов обсудить детали и сроки. Напишите!"
    )


# ─── ДИАГНОСТИКА ─────────────────────────────────────────────────────────────
async def test_connection():
    """Тест — просто открываем FL.ru и делаем скриншот."""
    from playwright.async_api import async_playwright

    log.info("🔍 Тест подключения к FL.ru...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        await page.goto("https://www.fl.ru/", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.screenshot(path="/opt/jarvis/fl_test.png")
        title = await page.title()
        log.info(f"✅ Страница: {title}")
        log.info(f"📸 Скриншот: /opt/jarvis/fl_test.png")
        await browser.close()
    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="FL.ru Автооткликатор")
    parser.add_argument("--url", help="URL заказа на FL.ru")
    parser.add_argument("--message", help="Текст отклика")
    parser.add_argument("--title", help="Заголовок заказа (для генерации отклика)")
    parser.add_argument("--desc", help="Описание заказа (для генерации отклика)", default="")
    parser.add_argument("--test", action="store_true", help="Тест подключения")
    parser.add_argument("--executor", default="Полифан", help="Имя исполнителя")
    args = parser.parse_args()

    if args.test:
        await test_connection()
        return

    if not args.url:
        print("❌ Укажи --url заказа")
        print("Пример: python3 fl_autoreply.py --url 'https://www.fl.ru/projects/...' --title 'Название'")
        sys.exit(1)

    # Генерируем или используем готовый текст
    if args.message:
        message = args.message
    elif args.title:
        log.info("✍️ Генерирую отклик через Groq...")
        message = await generate_reply(args.title, args.desc, args.executor)
        log.info(f"📝 Отклик:\n{message}")
    else:
        print("❌ Укажи --message или --title для автогенерации")
        sys.exit(1)

    result = await send_fl_reply(args.url, message)

    if result:
        print(f"✅ УСПЕХ — отклик отправлен!")
        print(f"📝 Текст: {message}")
    else:
        print("❌ ОШИБКА — проверь скриншоты в /opt/jarvis/")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
