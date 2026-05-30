import os
import json
import logging
import asyncio
import feedparser
import httpx
import sqlite3
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ═══ РОССИЙСКИЕ БИРЖИ ═══
RU_RSS_FEEDS = [
    # FL.ru
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=2", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://www.fl.ru/rss/all.xml?category=19", "🇷🇺 FL.ru/Данные"),
    # Freelance.ru
    ("https://freelance.ru/rss/projects.xml", "🇷🇺 Freelance.ru"),
    # Weblancer
    ("https://www.weblancer.net/jobs/feed/", "🇷🇺 Weblancer"),
    ("https://www.weblancer.net/jobs/feed/?cat=13", "🇷🇺 Weblancer/Тексты"),
    ("https://www.weblancer.net/jobs/feed/?cat=22", "🇷🇺 Weblancer/Переводы"),
]

# ═══ МЕЖДУНАРОДНЫЕ БИРЖИ ═══
EN_RSS_FEEDS = [
    ("https://www.guru.com/jobs/rss/", "🟠 Guru.com"),
    ("https://www.guru.com/jobs/rss/?skill=data-entry", "🟠 Guru/Data"),
    ("https://www.guru.com/jobs/rss/?skill=translation", "🟠 Guru/Translation"),
    ("https://www.guru.com/jobs/rss/?skill=writing", "🟠 Guru/Writing"),
    ("https://www.peopleperhour.com/jobs/rss", "🔵 PeoplePerHour"),
    ("https://www.peopleperhour.com/jobs/rss?service=writing", "🔵 PPH/Writing"),
    ("https://www.peopleperhour.com/jobs/rss?service=translation", "🔵 PPH/Translation"),
]

# ═══ TELEGRAM КАНАЛЫ С ЗАКАЗАМИ ═══
TG_CHANNELS = [
    "freelance_ru",
    "freelancehunt_ru",
    "it_freelance_ru",
    "kopiraiting_ru",
    "freelance_project_ru",
]

# ═══ ФИЛЬТРЫ ═══
WHITELIST = [
    # Русские
    "ввод данных", "перевод", "транскрипция", "копирайтинг", "написать",
    "описание товара", "описание продукта", "статья", "текст", "контент",
    "редактура", "корректура", "сбор данных", "парсинг", "исследование",
    "таблица", "excel", "гугл таблиц", "заполнить", "обработка",
    "виртуальный помощник", "помощник", "администратор", "секретарь",
    "перевести", "транслит", "субтитры", "расшифровка",
    # Английские
    "data entry", "copy paste", "translation", "transcription",
    "article", "blog", "content writing", "copywriting", "rewrite",
    "proofread", "research", "spreadsheet", "excel", "csv",
    "product description", "virtual assistant", "admin", "summarize",
    "web research", "data collection", "categorize", "simple task",
]

BLACKLIST = [
    # Разработка
    "developer", "программист", "разработчик", "development", "coding",
    "react", "angular", "node", "python developer", "django", "flask",
    "mobile app", "android", "ios", "web app", "backend", "frontend",
    "wordpress developer", "shopify developer", "api integration",
    # Видео/Аудио
    "video edit", "видеомонтаж", "монтаж видео", "animation", "анимация",
    "after effects", "premiere", "motion", "3d", "render",
    "audio mixing", "звукозапись", "музыка", "music production",
    # Сложный дизайн
    "logo design", "логотип", "brand identity", "ui/ux", "ui design",
    "illustration", "иллюстрация", "3d model",
    # Реклама
    "google ads", "facebook ads", "таргет", "яндекс директ",
    "seo specialist", "smm manager",
]

MAX_BUDGET_RUB = 15000
MAX_BUDGET_USD = 200

def is_relevant(title: str, description: str, budget: str = "") -> bool:
    text = (title + " " + description).lower()
    
    for bad in BLACKLIST:
        if bad in text:
            return False
    
    # Проверка бюджета
    if budget:
        nums = re.findall(r'\d+', budget.replace(',', '').replace(' ', ''))
        if nums:
            max_num = max(int(n) for n in nums)
            if '₽' in budget or 'руб' in budget.lower():
                if max_num > MAX_BUDGET_RUB:
                    return False
            elif '$' in budget or 'usd' in budget.lower():
                if max_num > MAX_BUDGET_USD:
                    return False
    
    return any(kw in text for kw in WHITELIST)

# ═══ БАЗА ДАННЫХ ═══
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
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
        job_id TEXT, amount_usd REAL, amount_rub REAL,
        date TEXT, description TEXT
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

def save_earning(job_id, amount_usd, amount_rub, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id, amount_usd, amount_rub, date, description) VALUES (?, ?, ?, ?, ?)',
              (job_id, amount_usd, amount_rub, datetime.now().isoformat(), description))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    by_status = dict(c.fetchall())
    c.execute('SELECT source, COUNT(*) FROM jobs GROUP BY source ORDER BY COUNT(*) DESC LIMIT 5')
    by_source = c.fetchall()
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings')
    earn = c.fetchone()
    conn.close()
    return {'by_status': by_status, 'by_source': by_source,
            'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2]}

def clean_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

# ═══ ПАРСЕРЫ ═══
async def parse_rss_feeds(client, feeds) -> list:
    jobs = []
    for url, source in feeds:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                logger.info(f"❌ {source}: {r.status_code}")
                continue
            feed = feedparser.parse(r.text)
            count = 0
            for e in feed.entries[:8]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc = clean_html(e.get('summary', e.get('description', '')))
                
                # Ищем бюджет
                budget_m = re.search(r'[\$₽€£]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|EUR|\$|₽|р\.)', desc + title)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                
                if is_relevant(title, desc, budget):
                    jobs.append({
                        'id': make_id(link), 'title': title[:200],
                        'description': desc[:1200], 'budget': budget,
                        'url': link, 'source': source, 'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    })
                    count += 1
                mark_seen(link)
            logger.info(f"✅ {source}: {count} новых из {len(feed.entries)}")
        except Exception as e:
            logger.error(f"❌ {source}: {e}")
    return jobs

async def parse_freelancer_api(client) -> list:
    jobs = []
    try:
        r = await client.get(
            "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=20&compact=true&sort_field=time_updated",
            headers={**HEADERS, "Accept": "application/json"}
        )
        if r.status_code != 200:
            return jobs
        projects = r.json().get('result', {}).get('projects', [])
        for p in projects[:10]:
            url = f"https://www.freelancer.com/projects/{p.get('seo_url', p.get('id',''))}"
            if is_seen(url):
                continue
            title = p.get('title', '')
            desc = clean_html(p.get('description', ''))
            budget = p.get('budget', {})
            budget_str = f"${budget.get('minimum',0)}-${budget.get('maximum',0)}" if budget else "Договорная"
            if is_relevant(title, desc, budget_str):
                jobs.append({
                    'id': make_id(url), 'title': title[:200],
                    'description': desc[:1200], 'budget': budget_str,
                    'url': url, 'source': '🟢 Freelancer.com', 'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
            mark_seen(url)
        logger.info(f"✅ Freelancer.com: {len(jobs)} новых")
    except Exception as e:
        logger.error(f"❌ Freelancer.com: {e}")
    return jobs

async def parse_telegram_channels(client) -> list:
    jobs = []
    for channel in TG_CHANNELS:
        try:
            r = await client.get(f"https://t.me/s/{channel}", headers=HEADERS)
            if r.status_code != 200:
                continue
            posts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            for i, post_html in enumerate(posts[:5]):
                text = clean_html(post_html)
                if len(text) < 40:
                    continue
                post_url = f"https://t.me/{channel}/post_{abs(hash(text)) % 100000}"
                if is_seen(post_url):
                    continue
                if not is_relevant(text, ""):
                    continue
                budget_m = re.search(r'[\$₽€£]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', text)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                jobs.append({
                    'id': make_id(post_url),
                    'title': text[:60].strip() + "...",
                    'description': text[:1000], 'budget': budget,
                    'url': f"https://t.me/{channel}",
                    'source': f'📱 TG @{channel}', 'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(post_url)
        except Exception as e:
            logger.error(f"❌ TG {channel}: {e}")
    return jobs

# ═══ AI ═══
async def analyze_job(job: dict) -> dict:
    is_ru = bool(re.search(r'[а-яё]', job['title'] + job['description'][:100], re.I))
    lang = "русском" if is_ru else "English"
    
    prompt = f"""Фрилансер анализирует заказ. Ответь ТОЛЬКО JSON.

ЗАКАЗ:
Название: {job['title']}
Описание: {job['description'][:500]}
Бюджет: {job['budget']}

JSON:
{{
  "can_do": true,
  "difficulty": "ЛЁГКИЙ",
  "reason": "одно предложение на русском",
  "proposal": "proposal на {lang}, 3 предложения",
  "estimated_time": "1 час"
}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 400, "temperature": 0.7}
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        if "```" in text:
            text = text.split("```")[1].split("```")[0].replace("json","").strip()
        return json.loads(text)

async def execute_job(job: dict) -> str:
    is_ru = bool(re.search(r'[а-яё]', job['title'] + job['description'][:100], re.I))
    lang_note = "Отвечай на русском языке." if is_ru else "Reply in English."
    
    prompt = f"""Выполни фриланс задание профессионально и полностью. {lang_note}

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}

Результат должен быть готов к отправке клиенту."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ TELEGRAM UI ═══
async def send_job_card(bot, job: dict, analysis: dict):
    diff_emoji = {"ЛЁГКИЙ": "🟢", "СРЕДНИЙ": "🟡", "СЛОЖНЫЙ": "🔴"}.get(analysis.get('difficulty',''), "⚪")
    
    msg = f"""🎯 *НОВЫЙ ЗАКАЗ*
{job['source']}

📌 *{job['title'][:100]}*
💰 {job['budget']}
{diff_emoji} {analysis.get('difficulty','?')} · ⏱ {analysis.get('estimated_time','?')}

💬 _{analysis.get('reason','')}_

📝 *Proposal:*
{analysis.get('proposal','')[:400]}

🔗 [Открыть заказ]({job['url']})"""

    keyboard = [[
        InlineKeyboardButton("✅ Берём!", callback_data=f"take_{job['id']}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{job['id']}")
    ]]
    await bot.send_message(
        chat_id=YOUR_CHAT_ID, text=msg,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("take_"):
        job_id = data[5:]
        job = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден")
            return
        update_job(job_id, 'accepted')
        await query.edit_message_text(
            f"✅ *Берём!*\n{job['source']}\n📌 {job['title'][:80]}\n\n⏳ Выполняю...",
            parse_mode='Markdown'
        )
        try:
            result = await execute_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"✨ *ГОТОВО!*\n\n📌 *{job['title'][:80]}*\n\n━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data.startswith("skip_"):
        update_job(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job(job_id, 'done')
        # Считаем бюджет
        nums = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
        amount = float(nums[0]) if nums else 0
        is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
        if is_rub:
            save_earning(job_id, amount/90, amount, job['title'])
        else:
            save_earning(job_id, amount, amount*90, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n"
            f"✅ Выполнено всего: {stats['by_status'].get('done',0)}\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\n"
            f"Бухгалтер записал 📊",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        await query.edit_message_text("✏️ Напиши что исправить:")

# ═══ КОМАНДЫ ═══
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Полифан на связи!*\n\n"
        "Мониторю сразу 6 источников:\n"
        "🇷🇺 FL.ru\n"
        "🇷🇺 Freelance.ru\n"
        "🇷🇺 Weblancer.net\n"
        "🟠 Guru.com\n"
        "🔵 PeoplePerHour\n"
        "📱 Telegram каналы\n\n"
        "Проверка каждые 15 минут!\n\n"
        "/scan — проверить сейчас\n"
        "/stats — статистика",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    by_src = "\n".join([f"  {src}: {cnt}" for src, cnt in stats['by_source']])
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА ПОЛИФАНА*\n\n"
        f"🔍 Найдено: {stats['by_status'].get('found',0)}\n"
        f"✅ Принято: {stats['by_status'].get('accepted',0)}\n"
        f"🏁 Выполнено: {stats['by_status'].get('done',0)}\n"
        f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\n"
        f"📡 *По источникам:*\n{by_src if by_src else '  Пока нет данных'}",
        parse_mode='Markdown'
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Сканирую все источники...")
    count = await check_new_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Готово!\n\n"
        f"📨 Отправлено заказов: {count}\n"
        f"{'Заказы летят! 🚀' if count > 0 else 'Новых нет, жди следующей проверки ⏳'}"
    )

# ═══ ГЛАВНЫЙ ПАРСЕР ═══
async def check_new_jobs(bot) -> int:
    logger.info("🔍 Сканирую все источники...")
    all_jobs = []

    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        results = await asyncio.gather(
            parse_rss_feeds(client, RU_RSS_FEEDS),
            parse_rss_feeds(client, EN_RSS_FEEDS),
            parse_telegram_channels(client),
            return_exceptions=True
        )
        for r in results:
            if isinstance(r, list):
                all_jobs.extend(r)

    logger.info(f"📦 Всего найдено: {len(all_jobs)}")
    sent = 0
    for job in all_jobs[:4]:
        try:
            save_job(job)
            analysis = await analyze_job(job)
            if analysis.get('can_do', True):
                await send_job_card(bot, job, analysis)
                sent += 1
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    return sent

async def periodic_check(app):
    await asyncio.sleep(60)
    while True:
        try:
            await check_new_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка проверки: {e}")
        await asyncio.sleep(15 * 60)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    async def post_init(application):
        asyncio.create_task(periodic_check(application))
    app.post_init = post_init

    logger.info("🤖 Полифан запущен! Мониторю 7 источников!")
    app.run_polling()

if __name__ == "__main__":
    main()
