import os
import json
import logging
import asyncio
import httpx
import sqlite3
import feedparser
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID    = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID    = int(os.getenv("LILU_CHAT_ID", str(YOUR_CHAT_ID)))
DB_PATH         = os.getenv("DB_PATH", "/tmp/freelance.db")
FL_PHPSESSID    = os.getenv("FL_PHPSESSID", "")
FL_XSRF_TOKEN   = os.getenv("FL_XSRF_TOKEN", "")
KWORK_URL       = os.getenv("KWORK_URL", "https://kwork.ru/user/artem_sh")

# ═══ НАВЫКИ ПОЛИФАНА ═══

POLYFAN_SKILLS = """Полифан умеет делать:
- Тексты, статьи, блог-посты (EN/RU)
- Копирайтинг и рерайтинг
- Описания товаров для сайтов
- Переводы EN↔RU и другие языки
- Посты для соцсетей
- Корректура и редактура
- Описания для маркетплейсов

НЕ умеет: программирование, дизайн, видео, SEO-технический аудит"""

RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://remoteok.com/remote-writing-jobs.json", "🌍 RemoteOK"),
    ("https://jobicy.com/?feed=job_feed&job_categories=writing", "🌍 Jobicy"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

KEYWORDS = [
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент", "рерайтинг",
    "перевод", "перевести", "translation", "translate",
    "редактура", "корректура", "proofreading", "editing",
    "блог", "blog post", "article", "content writing",
    "product description", "copywriting", "статья",
    "тексты для", "наполнение сайта", "карточка товара",
    "описание товара", "маркетплейс",
]

BLACKLIST = [
    "программирование", "разработка", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "мобильное приложение",
    "android", "ios", "чертёж", "курсовая", "дипломная",
    "купить и отправить", "курьер", "доставить",
]

# ═══ БД ═══

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
    conn.close()
    return by_status

def clean_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

def is_relevant(title, desc):
    text = (title + " " + desc).lower()
    for bad in BLACKLIST:
        if bad in text:
            return False
    return any(kw in text for kw in KEYWORDS)

# ═══ ПАРСЕРЫ ═══

async def parse_rss(client) -> list:
    jobs = []
    for url, source in RSS_FEEDS:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0",
                "Accept": "application/rss+xml,application/xml,text/xml,*/*"
            }
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                continue
            logger.info(f"{source}: {len(feed.entries)} записей")
            for e in feed.entries[:15]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc  = clean_html(e.get('summary', e.get('description', '')))
                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc + title)
                budget   = budget_m.group(0).strip() if budget_m else "Договорная"
                if is_relevant(title, desc):
                    jobs.append({
                        'id': make_id(link), 'title': title[:200],
                        'description': desc[:1200], 'budget': budget,
                        'url': link, 'source': source,
                        'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    })
                    mark_seen(link)
        except Exception as e:
            logger.error(f"❌ {source}: {e}")
    logger.info(f"📋 Полифан нашёл: {len(jobs)} заказов")
    return jobs

# ═══ ОТПРАВКА ЛИЛЕ ═══

async def send_to_lilu(bot, job: dict):
    """
    Полифан отправляет заказ Лиле в её чат.
    Лила его анализирует и решает — пропустить или нет.
    """
    job_with_source = dict(job)
    job_with_source['source_bot'] = 'Полифан'

    payload = json.dumps(job_with_source, ensure_ascii=False)
    msg     = f"🤖JOB:{payload}"

    try:
        await bot.send_message(
            chat_id=LILU_CHAT_ID,
            text=msg[:4000]
        )
        logger.info(f"📨 Полифан → Лила: {job.get('title','')[:50]}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки Лиле: {e}")

async def scan_and_send(bot) -> int:
    """Сканируем заказы и шлём Лиле"""
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_rss(client)

    for job in jobs:
        save_job(job)
        await send_to_lilu(bot, job)
        count += 1
        await asyncio.sleep(2)  # пауза между отправками

    return count

# ═══ ВЫПОЛНЕНИЕ ЗАКАЗА ═══

async def execute_job(job: dict) -> str:
    prompt = f"""Выполни фриланс-заказ профессионально.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}

Напиши качественный результат на языке заказа.
Если заказ на английском — отвечай по-английски.
Если на русском — по-русски."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000
            }
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ АВТООТКЛИК FL.RU ═══

async def fl_apply(job_url: str, proposal: str) -> bool:
    if not FL_PHPSESSID or not FL_XSRF_TOKEN:
        logger.warning("FL cookies не заданы — автоотклик недоступен")
        return False
    try:
        project_id_m = re.search(r'/projects/(\d+)/', job_url)
        if not project_id_m:
            return False
        project_id = project_id_m.group(1)
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://www.fl.ru/projects/ajax/bid/",
                headers={
                    "Cookie": f"PHPSESSID={FL_PHPSESSID}; XSRF-TOKEN={FL_XSRF_TOKEN}",
                    "X-XSRF-TOKEN": FL_XSRF_TOKEN,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"project_id": project_id, "text": proposal, "cost": ""}
            )
            return r.status_code == 200
    except Exception as e:
        logger.error(f"FL автоотклик ошибка: {e}")
        return False

# ═══ КНОПКИ ═══

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "team_skills":
        await query.edit_message_text(
            "🤖 *ПОЛИФАН — ЧТО УМЕЮ*\n\n"
            "✍️ Тексты и статьи (EN/RU)\n"
            "📝 Копирайтинг и рерайтинг\n"
            "🌍 Переводы EN↔RU\n"
            "🛍️ Описания товаров\n"
            "📱 Посты для соцсетей\n"
            "✅ Корректура и редактура\n\n"
            "🔍 Ищу заказы на:\n"
            "• FL.ru (RSS)\n"
            "• RemoteOK\n"
            "• Jobicy\n"
            "• WWR (We Work Remotely)\n\n"
            "📤 Все заказы сначала идут через *Лилу* — она переводит и фильтрует!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )

    elif data == "kwork_menu":
        await query.edit_message_text(
            "🛍️ *НАШИ КВОРКИ НА KWORK*\n\n"
            "✍️ *Тексты и копирайтинг*\n"
            " • Статья/блог-пост: от 500₽\n"
            " • Описание для сайта: от 400₽\n"
            " • Перевод EN↔RU: от 300₽\n\n"
            "📦 *Карточки WB/Ozon/ЯМ*\n"
            " • Эконом: 400₽\n"
            " • Стандарт: 1200₽\n"
            " • Бизнес: 2000₽\n\n"
            f"🔗 [Все кворки на Kwork]({KWORK_URL})",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Открыть Kwork", url=KWORK_URL),
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )

    elif data == "back_main":
        await query.edit_message_text(
            "🤖 *Полифан* — твой фриланс-помощник!\n\nЧем могу помочь?",
            parse_mode='Markdown',
            reply_markup=_main_keyboard()
        )

    elif data.startswith("done_"):
        job_id = data[5:]
        update_job(job_id, 'done')
        await query.edit_message_text("💰 Заказ закрыт! Молодцы 🎉")

    elif data.startswith("redo_"):
        job_id = data[5:]
        context.user_data['redo_job_id'] = job_id
        job = get_job(job_id)
        context.user_data['redo_result'] = job.get('result','') if job else ''
        await query.edit_message_text(
            "✏️ *Напиши что исправить:*",
            parse_mode='Markdown'
        )

# ═══ ГЛАВНОЕ МЕНЮ ═══

def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Что умею",    callback_data="team_skills"),
         InlineKeyboardButton("🛍️ Наши кворки", callback_data="kwork_menu")],
        [InlineKeyboardButton("🔍 Найти заказы", callback_data="do_scan"),
         InlineKeyboardButton("📊 Статистика",   callback_data="do_stats")],
    ])

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Привет! Я Полифан — фриланс-помощник!*\n\n"
        "Ищу заказы на текст/контент/переводы.\n"
        "Все заказы сначала проверяет *Лила* —\n"
        "она переводит и решает, берём или нет.\n\n"
        "Только лучшие заказы доходят до тебя! 🎯",
        parse_mode='Markdown',
        reply_markup=_main_keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🔍 Ищу заказы и отправляю Лиле...")
    count = await scan_and_send(context.application.bot)
    await msg.edit_text(
        f"✅ Нашёл и отправил Лиле: *{count}* заказов\n\n"
        f"Лила сейчас анализирует — лучшие придут тебе!",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    await update.message.reply_text(
        f"📊 *Статистика Полифана*\n\n"
        f"🔍 Найдено: {stats.get('found', 0)}\n"
        f"✅ Принято: {stats.get('accepted', 0)}\n"
        f"✨ Выполнено: {stats.get('completed', 0)}\n"
        f"💰 Закрыто: {stats.get('done', 0)}\n"
        f"⏭ Пропущено: {stats.get('skipped', 0)}",
        parse_mode='Markdown'
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM seen_jobs')
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Кэш очищен! Теперь /scan найдёт заново.")

async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Что умеет Полифан:*\n\n" + POLYFAN_SKILLS,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛍️ Наши кворки", callback_data="kwork_menu")
        ]])
    )

async def kwork_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🛍️ Наши кворки на Kwork:\n{KWORK_URL}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Открыть", url=KWORK_URL)
        ]])
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile",
                          "messages": [{"role": "user", "content":
                              f"Исправь текст.\n\nОРИГИНАЛ:\n{original[:2000]}\n\nИНСТРУКЦИЯ: {fix}\n\nВерни исправленный текст полностью."}],
                          "max_tokens": 2000}
                )
                new_result = r.json()["choices"][0]["message"]["content"].strip()
            update_job(job_id, 'completed', new_result)
            context.user_data.pop('redo_job_id', None)
            keyboard = [[
                InlineKeyboardButton("👍 Готово!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
            ]]
            await update.message.reply_text(
                f"✨ *Исправлено!*\n\n{new_result[:2500]}",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        return

    await update.message.reply_text(
        "Используй команды или кнопки меню 👇",
        reply_markup=_main_keyboard()
    )

# ═══ АВТОСКАНИРОВАНИЕ ═══

async def auto_scan(context):
    logger.info("🔄 Полифан: автосканирование...")
    try:
        count = await scan_and_send(context.bot)
        logger.info(f"✅ Полифан → Лила: {count} заказов")
    except Exception as e:
        logger.error(f"❌ Автосканирование: {e}")

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start_command))
    app.add_handler(CommandHandler("scan",   scan_command))
    app.add_handler(CommandHandler("stats",  stats_command))
    app.add_handler(CommandHandler("clear",  clear_command))
    app.add_handler(CommandHandler("skills", skills_command))
    app.add_handler(CommandHandler("kwork",  kwork_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(auto_scan, interval=1800, first=90)

    logger.info("🤖 Полифан запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
