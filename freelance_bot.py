import os
import json
import logging
import asyncio
import httpx
import sqlite3
import feedparser
import re
import random
import pytz
import html
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID    = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID    = int(os.getenv("LILU_CHAT_ID", str(YOUR_CHAT_ID)))
DB_PATH         = os.getenv("DB_PATH", "/tmp/freelance.db")
KWORK_URL       = os.getenv("KWORK_URL", "https://kwork.ru/user/artem_sh")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise EnvironmentError("❌ Проверь FREELANCE_BOT_TOKEN и GROQ_API_KEY в переменных окружения!")

# ═══ GROQ — ТОЛЬКО РАБОЧИЕ МОДЕЛИ (проверено 05.06.2026) ═══
GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]
_groq_model_index = 0

# ═══ ВРЕМЯ МСК ═══
def msk_now() -> datetime:
    return datetime.now(pytz.timezone('Europe/Moscow'))

def msk_time_str() -> str:
    return msk_now().strftime("%d.%m.%Y %H:%M МСК")

# ═══ RSS И JSON ИСТОЧНИКИ ═══
RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://www.fl.ru/rss/all.xml?subcategory=172", "🇷🇺 FL.ru/Копирайтинг"),
    ("https://www.fl.ru/rss/all.xml?subcategory=106", "🇷🇺 FL.ru/Соцсети"),
    ("https://www.fl.ru/rss/all.xml?subcategory=113", "🇷🇺 FL.ru/Маркетплейс"),
    ("https://freelance.habr.com/tasks.rss", "🖥 Habr Freelance"),
    ("https://remoteok.com/remote-writing-jobs.json", "🌍 RemoteOK"),
    ("https://jobicy.com/?feed=job_feed&job_categories=writing", "🌍 Jobicy"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

KEYWORDS = [
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент", "рерайтинг",
    "блог", "blog post", "article", "content writing",
    "статья", "тексты для", "наполнение сайта",
    "продающий текст", "рекламный текст",
    "перевод", "перевести", "translation", "translate", "переводчик",
    "перевод текста", "перевод с английского", "перевод на английский",
    "технический перевод", "деловой перевод", "юридический перевод",
    "медицинский перевод", "перевод договора", "перевод документа",
    "any language", "multilingual", "localization", "локализация",
    "редактура", "корректура", "proofreading", "editing", "редактировать",
    "email рассылка", "email маркетинг", "письмо клиентам",
    "welcome письмо", "email копирайтинг", "newsletter",
    "текст для лендинга", "тексты для сайта", "landing page",
    "посты инстаграм", "контент telegram", "smm копирайтинг",
    "посты соцсетей", "контент план", "сценарий reels",
    "карточка товара", "описание товара", "маркетплейс",
    "wildberries", "wb", "ozon", "озон", "яндекс маркет",
    "product description", "amazon listing", "сео текст", "seo текст",
    "seo writing", "ключевые слова", "транскрибация", "резюме", "resume", "cv"
]

BLACKLIST = [
    "программирование", "разработка сайта", "верстка",
    "дизайн логотип с нуля", "видеомонтаж", "3d анимация",
    "мобильное приложение", "android", "ios",
    "чертёж", "autocad", "курсовая", "дипломная",
    "купить и доставить", "курьер", "доставить",
]

AI_REJECTION_PHRASES = [
    "без нейросетей", "без ии", "без ai", "no ai",
    "только вручную", "исключительно вручную",
    "ai не принимается", "нейросети не принимаются",
]

PRIORITY_BOOST = [
    "карточка товара", "wildberries", "wb", "ozon", "яндекс маркет",
    "срочно", "быстро", "копирайтер нужен", "ищу копирайтера",
    "перевод срочно", "нужен переводчик",
]

def is_ai_rejection(title, desc):
    text = (title + " " + desc).lower()
    for phrase in AI_REJECTION_PHRASES:
        if phrase.lower() in text:
            return True, phrase
    return False, ""

def get_priority_score(title, desc):
    text = (title + " " + desc).lower()
    score = 5
    for phrase in PRIORITY_BOOST:
        if phrase.lower() in text:
            score += 1
    return min(10, score)

# ═══ БАЗА ДАННЫХ ═══

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, description TEXT,
        budget TEXT, url TEXT, source TEXT,
        status TEXT DEFAULT 'found', result TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (url TEXT PRIMARY KEY, seen_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount_usd REAL DEFAULT 0, amount_rub REAL DEFAULT 0,
        description TEXT, created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS filtered_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, url TEXT, reason TEXT, date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT UNIQUE,
        title TEXT, url TEXT, status TEXT,
        lila_decision TEXT, lila_reason TEXT,
        found_at TEXT, source TEXT
    )''')
    conn.commit()
    conn.close()

def save_job(job):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO jobs
        (id, title, description, budget, url, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (job['id'], job['title'], job['description'], job['budget'],
         job['url'], job['source'], job['status'],
         job['created_at'], job['updated_at']))
    c.execute('''INSERT OR IGNORE INTO jobs_log (project_id, title, url, status, found_at, source)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (job['id'], job['title'][:200], job['url'], job['status'], job['created_at'], job['source']))
    conn.commit()
    conn.close()

def save_filtered_job(title, url, reason):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO filtered_jobs (title, url, reason, date) VALUES (?, ?, ?, ?)',
              (title[:200], url, reason, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_job(job_id, status, result=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if result:
        c.execute('UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?',
                  (status, result, datetime.now().isoformat(), job_id))
    else:
        c.execute('UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                  (status, datetime.now().isoformat(), job_id))
    conn.commit()
    conn.close()

def get_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id,title,description,budget,url,source,status,result FROM jobs WHERE id=?', (job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(zip(['id','title','description','budget','url','source','status','result'], row))
    return None

def is_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE url=?', (url,))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (url, seen_at) VALUES (?, ?)',
              (url, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    by_status = dict(c.fetchall())
    try:
        c.execute('SELECT COUNT(*) FROM filtered_jobs')
        filtered_count = c.fetchone()[0]
    except:
        filtered_count = 0
    conn.close()
    by_status['filtered_ai'] = filtered_count
    return by_status

def clean_html(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;|&quot;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

def is_relevant(title, desc):
    text = (title + " " + desc).lower()
    for bad in BLACKLIST:
        if bad in text:
            return False
    return any(kw in text for kw in KEYWORDS)

# ═══ GROQ ═══

async def groq_request(messages, system="", model=None, max_tokens=800):
    global _groq_model_index
    for attempt in range(len(GROQ_MODELS)):
        current_model = GROQ_MODELS[_groq_model_index] if model is None else model
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": current_model, "messages": msgs, "max_tokens": max_tokens}
                )
                if r.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"⚠️ Rate limit [{current_model}] → переключаю, жду {wait:.1f}с")
                    _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
                    await asyncio.sleep(wait)
                    continue
                data = r.json()
                if "choices" not in data:
                    raise Exception(f"Groq error: {data}")
                logger.info(f"✅ Groq [{current_model}]")
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
                await asyncio.sleep(2)
                continue
            raise
    return "⚠️ Все модели временно недоступны."

# ═══ УМНЫЙ PROPOSAL ═══

async def generate_smart_proposal(job: dict) -> str:
    title  = job.get('title', '')
    desc   = job.get('description', '')[:500]
    budget = job.get('budget', 'не указан')
    source = job.get('source', '')
    is_english = any(w in (title+desc).lower() for w in
                     ['the ','and ','for ','writing','content','article','translation'])
    prompt = (
        f"Напиши профессиональный отклик на фриланс-заказ.\n\n"
        f"ЗАКАЗ: {title}\nОПИСАНИЕ: {desc}\nБЮДЖЕТ: {budget}\nПЛАТФОРМА: {source}\n\n"
        f"Требования:\n"
        f"1. Начни с понимания задачи — 1 предложение о сути\n"
        f"2. Почему мы подходим — конкретно\n"
        f"3. Мини-план или уточняющий вопрос\n"
        f"4. Сроки и условия\n"
        f"5. Призыв к действию\n\n"
        f"Тон: профессиональный, живой. Длина: 80-120 слов.\n"
        f"Язык: {'английский' if is_english else 'русский'}.\n"
        f"НЕ начинай с 'Здравствуйте' или 'Hello'."
    )
    try:
        return await groq_request(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
    except Exception as e:
        logger.error(f"proposal: {e}")
        return ""

# ═══ ПАРСЕРЫ ═══

async def parse_rss(client) -> list:
    jobs = []
    filtered_count = 0
    for url, source in RSS_FEEDS:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0",
                "Accept": "application/rss+xml,application/xml,text/xml,application/json,*/*"
            }
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue

            entries = []
            if ".json" in url or "json" in r.headers.get("Content-Type", ""):
                try:
                    data = r.json()
                    if isinstance(data, list):
                        for item in data[:15]:
                            if "legal" in item or not item.get("url"): continue
                            entries.append({
                                'link': item.get('url', ''),
                                'title': item.get('position', ''),
                                'description': item.get('description', '')
                            })
                except Exception as je:
                    logger.error(f"Ошибка JSON парсинга для {source}: {je}")
            else:
                feed = feedparser.parse(r.text)
                for e in feed.entries[:15]:
                    entries.append({
                        'link': e.get('link', ''),
                        'title': e.get('title', ''),
                        'description': e.get('summary', e.get('description', ''))
                    })

            if not entries:
                continue

            logger.info(f"{source}: {len(entries)} записей получено")

            for item in entries:
                link = item['link']
                if not link or is_seen(link):
                    continue
                title = clean_html(item['title'])
                desc  = clean_html(item['description'])

                if not is_relevant(title, desc):
                    continue

                ai_rejected, ai_phrase = is_ai_rejection(title, desc)
                if ai_rejected:
                    logger.info(f"🚫 Запрет AI: {title[:50]}")
                    save_filtered_job(title, link, f"запрет AI: {ai_phrase}")
                    mark_seen(link)
                    filtered_count += 1
                    continue

                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc+title)
                budget   = budget_m.group(0).strip() if budget_m else "Договорная"
                score    = get_priority_score(title, desc)

                jobs.append({
                    'id': make_id(link), 'title': title[:200],
                    'description': desc[:1200], 'budget': budget,
                    'url': link, 'source': source,
                    'status': 'found', 'priority': score,
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(link)
        except Exception as e:
            logger.error(f"❌ Ошибка в источнике {source}: {e}")

    jobs.sort(key=lambda x: x.get('priority', 5), reverse=True)
    logger.info(f"📋 Полифан нашёл: {len(jobs)} целевых заказов (AI-запретов: {filtered_count})")
    return jobs

# ═══ ОТПРАВКА ЛИЛЕ ═══

async def send_to_lilu(bot, job: dict):
    try:
        proposal = await generate_smart_proposal(job)
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        source = f"Полифан | {job.get('source', '')}"
        c.execute(
            "UPDATE jobs SET status='pending_lilu', source=?, result=?, updated_at=? WHERE id=?",
            (source[:200], proposal[:2000] if proposal else "", datetime.now().isoformat(), job['id'])
        )
        conn.commit()
        conn.close()
        logger.info(f"📨 → Лила (pending_lilu + proposal): {job.get('title','')[:50]}")
    except Exception as e:
        logger.error(f"send_to_lilu: {e}")

async def scan_and_send(bot) -> int:
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_rss(client)
    for job in jobs:
        save_job(job)
        await send_to_lilu(bot, job)
        count += 1
        await asyncio.sleep(1)
    return count

# ═══ КОМАНДЫ (HTML МОД) ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    safe_time = html.escape(msk_time_str())
    await update.message.reply_html(
        f"🤖 <b>Полифан v3.2</b>\n\n"
        f"🕐 {safe_time}\n\n"
        f"Ищу заказы, пишу тексты и переводы на ЛЮБОЙ язык!\n\n"
        f"🌍 <b>Переводы:</b> русский, английский, немецкий,\n"
        f"французский, китайский, японский, арабский и все остальные!\n\n"
        f"📝 <b>Proposals:</b> генерирую кастомно под каждый заказ\n"
        f"🚫 <b>Автофильтр:</b> запрет AI пропускаем\n"
        f"⚡ <b>Groq:</b> ротация 2 рабочих моделей\n\n"
        f"/scan — найти заказы\n"
        f"/proposal — написать отклик\n"
        f"/translate — перевести текст\n"
        f"/copywrite — написать текст\n"
        f"/stats — статистика\n"
        f"/clear — очистить кэш",
        reply_markup=_main_keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Ищу заказы...")
    count = await scan_and_send(context.application.bot)
    stats = get_stats()
    safe_time = html.escape(msk_time_str())
    await msg.edit_text(
        f"✅ Нашёл и отправил Лиле: <b>{count}</b> заказов\n"
        f"🚫 Отфильтровано (AI запрет): <b>{stats.get('filtered_ai',0)}</b>\n"
        f"📝 Proposals сгенерированы автоматически!\n"
        f"🕐 {safe_time}",
        parse_mode='HTML'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    safe_time = html.escape(msk_time_str())
    await update.message.reply_html(
        f"📊 <b>Статистика Полифана</b>\n\n"
        f"🕐 {safe_time}\n\n"
        f"🔍 Найдено: {stats.get('found',0)}\n"
        f"✅ Принято: {stats.get('accepted',0)}\n"
        f"✨ Выполнено: {stats.get('completed',0)}\n"
        f"💰 Закрыто: {stats.get('done',0)}\n"
        f"⏭ Пропущено: {stats.get('skipped',0)}\n"
        f"🚫 Запрет AI: {stats.get('filtered_ai',0)}"
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute('DELETE FROM seen_jobs')
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Кэш очищен! /scan найдёт заново.")

async def proposal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        context.user_data['making_proposal'] = True
        await update.message.reply_html("📝 Отправь описание заказа — напишу кастомный отклик!")
        return
    job_desc = " ".join(context.args)
    await update.message.reply_text("📝 Пишу отклик...")
    fake_job = {"title": job_desc, "description": job_desc, "budget": "", "source": "FL.ru"}
    proposal = await generate_smart_proposal(fake_job)
    if proposal:
        await update.message.reply_html(f"📝 <b>ОТКЛИК:</b>\n\n<code>{html.escape(proposal)}</code>")
    else:
        await update.message.reply_text("❌ Ошибка генерации")

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "🌍 Использование:\n<code>/translate текст для перевода</code>\n\n"
            "Автоматически определю язык и переведу!\n"
            "RU→EN, EN→RU, любой→RU и т.д."
        )
        return
    text_to_translate = " ".join(context.args)
    await update.message.reply_text("🌍 Перевожу...")
    try:
        translation = await groq_request(
            messages=[{"role": "user", "content":
                f"Определи язык текста и переведи:\n"
                f"- русский → английский\n"
                f"- английский → русский\n"
                f"- любой другой → русский\n\n"
                f"Текст: {text_to_translate}\n\n"
                f"Верни ТОЛЬКО перевод без пояснений."}],
            max_tokens=500
        )
        await update.message.reply_html(f"🌍 <b>ПЕРЕВОД:</b>\n\n<code>{html.escape(translation)}</code>")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

async def copywrite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_html(
            "✍️ Использование:\n"
            "<code>/copywrite пост Instagram про кофе</code>\n"
            "<code>/copywrite email рассылка магазин одежды</code>\n"
            "<code>/copywrite текст лендинга курсы английского</code>"
        )
        return
    task = " ".join(context.args)
    await update.message.reply_text("✍️ Пишу текст...")
    try:
        result = await groq_request(
            messages=[{"role": "user", "content":
                f"Напиши professional текст.\nЗадание: {task}\n\n"
                f"Готовый текст без вступлений:"}],
            max_tokens=800
        )
        await update.message.reply_html(f"✍️ <b>ТЕКСТ:</b>\n\n<code>{html.escape(result)}</code>")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🤖 <b>Полифан v3.2 — что умею:</b>\n\n"
        "✍️ Тексты и статьи (RU/EN/DE/FR)\n"
        "🌍 Переводы на ВСЕ языки мира\n"
        "📱 Посты соцсетей, сценарии Reels/TikTok\n"
        "📧 Email рассылки и письма\n"
        "🌐 Тексты лендингов и сайтов\n"
        "🛍️ Описания товаров для маркетплейсов\n"
        "📄 Резюме и документы\n"
        "📋 Proposals кастомно под каждый заказ\n\n"
        "⚡ Groq: 2 проверенные модели — работаю без остановок\n\n"
        "🔍 Ищу на:\nFL.ru • Habr Freelance • RemoteOK • Jobicy • WWR"
    )

# ═══ КНОПКИ ═══

def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Найти заказы", callback_data="do_scan"),
         InlineKeyboardButton("📊 Статистика",   callback_data="do_stats")],
        [InlineKeyboardButton("🧠 Что умею",     callback_data="team_skills"),
         InlineKeyboardButton("🛍️ Кворки",       callback_data="kwork_menu")],
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "team_skills":
        await query.edit_message_text(
            "🤖 <b>Полифан v3.2</b>\n\n"
            "🌍 Переводы на ВСЕ языки\n"
            "✍️ Тексты, статьи, копирайтинг\n"
            "📱 Соцсети, email, лендинги\n"
            "📄 Резюме и документы\n"
            "📋 Кастомные proposals\n"
            "⚡ Groq 2 рабочие модели\n\n"
            "Источники: FL.ru + Habr + RemoteOK + WWR",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
        )
    elif data == "kwork_menu":
        await query.edit_message_text(
            f"🛍️ <b>Наши кворки:</b>\n\n"
            f"✍️ Статья: от 500₽\n"
            f"🌍 Перевод: от 300₽\n"
            f"📧 Email: от 400₽\n"
            f"📦 Карточки WB/Ozon: от 400₽\n\n"
            f"<a href='{KWORK_URL}'>Открыть профиль Kwork</a>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Открыть", url=KWORK_URL)],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
            ])
        )
    elif data == "back_main":
        await query.edit_message_text(
            "🤖 <b>Полифан</b> — чем могу помочь?",
            parse_mode='HTML',
            reply_markup=_main_keyboard()
        )
    elif data == "do_scan":
        await query.edit_message_text("🔍 Ищу заказы...")
        try:
            count = await scan_and_send(context.application.bot)
            stats = get_stats()
            safe_time = html.escape(msk_time_str())
            await query.edit_message_text(
                f"✅ Нашёл: <b>{count}</b> заказов → отправил Лиле\n"
                f"🚫 AI запрет: <b>{stats.get('filtered_ai',0)}</b>\n"
                f"🕐 {safe_time}",
                parse_mode='HTML',
                reply_markup=_main_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {html.escape(str(e)[:100])}", reply_markup=_main_keyboard())
    elif data == "do_stats":
        stats = get_stats()
        safe_time = html.escape(msk_time_str())
        await query.edit_message_text(
            f"📊 <b>Статистика</b>\n\n"
            f"🕐 {safe_time}\n\n"
            f"🔍 Найдено: {stats.get('found',0)}\n"
            f"✅ Принято: {stats.get('accepted',0)}\n"
            f"💰 Закрыто: {stats.get('done',0)}\n"
            f"🚫 AI запрет: {stats.get('filtered_ai',0)}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
        )
    elif data.startswith("done_"):
        update_job(data[5:], 'done')
        await query.edit_message_text("💰 Заказ закрыт! 🎉")
    elif data.startswith("redo_"):
        job_id = data[5:]
        job    = get_job(job_id)
        context.user_data['redo_job_id']  = job_id
        context.user_data['redo_result']  = job.get('result','') if job else ''
        await query.edit_message_text("✏️ Напиши что исправить:")

# ═══ СООБЩЕНИЯ ═══

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('making_proposal'):
        context.user_data.pop('making_proposal', None)
        await update.message.reply_text("📝 Пишу отклик...")
        fake_job = {"title": update.message.text, "description": update.message.text,
                    "budget": "", "source": "FL.ru"}
        proposal = await generate_smart_proposal(fake_job)
        if proposal:
            await update.message.reply_html(f"📝 <b>ОТКЛИК:</b>\n\n<code>{html.escape(proposal)}</code>")
        else:
            await update.message.reply_text("❌ Ошибка")
        return

    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            new_result = await groq_request(
                messages=[{"role": "user", "content":
                    f"Исправь текст.\nОРИГИНАЛ:\n{original[:2000]}\n\n"
                    f"ИНСТРУКЦИЯ: {fix}\n\nВерни исправленный текст полностью."}],
                max_tokens=2000
            )
            update_job(job_id, 'completed', new_result)
            context.user_data.pop('redo_job_id', None)
            keyboard = [[
                InlineKeyboardButton("👍 Готово!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
            ]]
            await update.message.reply_html(
                f"✨ <b>Исправлено!</b>\n\n<code>{html.escape(new_result[:2500])}</code>",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await update.message.reply_text(f"❌ {str(e)[:100]}")
        return

    await update.message.reply_html(
        "Используй команды:\n"
        "/scan — найти заказы\n"
        "/proposal — написать отклик\n"
        "/translate — перевести текст\n"
        "/copywrite — написать текст",
        reply_markup=_main_keyboard()
    )

# ═══ АВТОСКАНИРОВАНИЕ ═══

async def auto_scan_loop(bot):
    await asyncio.sleep(90)
    while True:
        logger.info("🔄 Полифан: автосканирование...")
        try:
            count = await scan_and_send(bot)
            stats = get_stats()
            logger.info(f"✅ → Лила: {count} заказов (AI запрет: {stats.get('filtered_ai',0)})")
            if count > 0 and YOUR_CHAT_ID:
                safe_time = html.escape(msk_time_str())
                await bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔍 <b>Полифан нашёл {count} заказов</b> → отправил Лиле!\n"
                         f"📝 Proposals сгенерированы\n"
                         f"🕐 {safe_time}",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"auto_scan: {e}")
        await asyncio.sleep(900)  # 15 минут

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("scan",      scan_command))
    app.add_handler(CommandHandler("stats",     stats_command))
    app.add_handler(CommandHandler("clear",     clear_command))
    app.add_handler(CommandHandler("skills",    skills_command))
    app.add_handler(CommandHandler("proposal",  proposal_command))
    app.add_handler(CommandHandler("translate", translate_command))
    app.add_handler(CommandHandler("copywrite", copywrite_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(auto_scan_loop(application.bot))
        try:
            if YOUR_CHAT_ID:
                safe_time = html.escape(msk_time_str())
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        f"🤖 <b>Полифан v3.2 запущен!</b>\n\n"
                        f"🕐 {safe_time}\n\n"
                        f"✅ HTML режим — бронебойный\n"
                        f"✅ Groq 2 рабочие модели\n"
                        f"✅ Время МСК везде\n"
                        f"✅ Скан каждые 15 мин\n"
                        f"✅ JSON парсинг RemoteOK\n"
                        f"✅ Резюме в категориях\n\n"
                        f"/proposal /translate /copywrite"
                    ),
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init
    logger.info("🤖 Полифан v3.2!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
