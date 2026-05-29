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
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
FREELANCER_CLIENT_ID = os.getenv("FREELANCER_CLIENT_ID", "")
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")

# ═══ ИСТОЧНИКИ ЗАКАЗОВ ═══

# 1. Guru.com RSS
GURU_RSS_FEEDS = [
    "https://www.guru.com/jobs/rss/",
    "https://www.guru.com/jobs/rss/?skill=data-entry",
    "https://www.guru.com/jobs/rss/?skill=translation",
    "https://www.guru.com/jobs/rss/?skill=writing",
    "https://www.guru.com/jobs/rss/?skill=research",
]

# 2. PeoplePerHour RSS
PPH_RSS_FEEDS = [
    "https://www.peopleperhour.com/jobs/rss",
    "https://www.peopleperhour.com/jobs/rss?service=writing",
    "https://www.peopleperhour.com/jobs/rss?service=translation",
    "https://www.peopleperhour.com/jobs/rss?service=data-entry",
]

# 3. Freelancer.com API (бесплатный, без ключа)
FREELANCER_API = "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=20&job_details=true&compact=true"

# 4. Telegram каналы с заказами (парсим через публичный превью)
TELEGRAM_JOB_CHANNELS = [
    "freelance_ru",
    "freelancehunt_ru", 
    "freelance_work_ru",
    "it_freelance_ru",
    "kopiraiting_ru",
    "designjobs_ru",
]

# Заголовки браузера
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

# Ключевые слова мелких задач
KEYWORDS = [
    "data entry", "copy paste", "translation", "translate", "transcription",
    "research", "spreadsheet", "excel", "csv", "write", "description",
    "article", "blog", "rewrite", "proofread", "edit", "summarize",
    "list", "categorize", "simple", "easy", "quick", "short",
    "ввод данных", "перевод", "написать", "описание", "статья",
    "таблица", "исследование", "редактура", "транскрипция", "простая",
    "быстрая", "небольшая", "текст", "перевести"
]

# ═══ БАЗА ДАННЫХ ═══
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, description TEXT,
        budget TEXT, url TEXT, source TEXT,
        status TEXT DEFAULT 'new', result TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (url TEXT PRIMARY KEY, seen_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT, amount REAL, date TEXT, description TEXT
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
         job['url'], job.get('source','?'), job['status'],
         job['created_at'], job['updated_at']))
    conn.commit()
    conn.close()

def update_job_status(job_id, status, result=None):
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
    c.execute('SELECT id,title,description,budget,url,source,status,result,created_at,updated_at FROM jobs WHERE id=?', (job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(zip(['id','title','description','budget','url','source','status','result','created_at','updated_at'], row))
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

def save_earning(job_id, amount, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id, amount, date, description) VALUES (?, ?, ?, ?)',
              (job_id, amount, datetime.now().isoformat(), description))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    stats = {}
    for key, status in [('found','found'),('accepted','accepted'),('done','done')]:
        c.execute(f'SELECT COUNT(*) FROM jobs WHERE status=?', (status,))
        stats[key] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM jobs')
    stats['total_found'] = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount), 0) FROM earnings')
    stats['earned'] = c.fetchone()[0]
    # По источникам
    c.execute('SELECT source, COUNT(*) FROM jobs GROUP BY source')
    stats['by_source'] = dict(c.fetchall())
    conn.close()
    return stats

def clean_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def make_job_id(url):
    return str(abs(hash(url)) % (10**12))

def is_relevant(title, description):
    text = (title + " " + description).lower()
    return any(kw in text for kw in KEYWORDS)

# ═══ ПАРСЕРЫ ═══

async def parse_guru(client) -> list:
    jobs = []
    for url in GURU_RSS_FEEDS:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            for e in feed.entries[:8]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc = clean_html(e.get('summary', ''))
                budget_m = re.search(r'\$[\d,]+', desc)
                budget = budget_m.group(0) if budget_m else "По договорённости"
                jobs.append({
                    'id': make_job_id(link), 'title': title[:200],
                    'description': desc[:1200], 'budget': budget,
                    'url': link, 'source': '🟠 Guru.com',
                    'status': 'found', 'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(link)
            logger.info(f"Guru: {len(feed.entries)} записей из {url[:50]}")
        except Exception as e:
            logger.error(f"Guru ошибка: {e}")
    return jobs

async def parse_pph(client) -> list:
    jobs = []
    for url in PPH_RSS_FEEDS:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            for e in feed.entries[:8]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc = clean_html(e.get('summary', ''))
                budget_m = re.search(r'[\$£€][\d,]+', desc)
                budget = budget_m.group(0) if budget_m else "По договорённости"
                jobs.append({
                    'id': make_job_id(link), 'title': title[:200],
                    'description': desc[:1200], 'budget': budget,
                    'url': link, 'source': '🔵 PeoplePerHour',
                    'status': 'found', 'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(link)
        except Exception as e:
            logger.error(f"PPH ошибка: {e}")
    return jobs

async def parse_freelancer(client) -> list:
    jobs = []
    try:
        # Публичный endpoint без авторизации
        url = "https://www.freelancer.com/api/projects/0.1/projects/active/?limit=20&compact=true&sort_field=time_updated"
        r = await client.get(url, headers={**HEADERS, "Accept": "application/json"})
        if r.status_code != 200:
            logger.info(f"Freelancer API статус: {r.status_code}")
            return jobs
        data = r.json()
        projects = data.get('result', {}).get('projects', [])
        for p in projects[:10]:
            job_id = str(p.get('id', ''))
            url_job = f"https://www.freelancer.com/projects/{p.get('seo_url', job_id)}"
            if is_seen(url_job):
                continue
            title = p.get('title', '')
            desc = clean_html(p.get('description', ''))
            budget = p.get('budget', {})
            budget_str = f"${budget.get('minimum',0)}-${budget.get('maximum',0)}" if budget else "По договорённости"
            jobs.append({
                'id': make_job_id(url_job), 'title': title[:200],
                'description': desc[:1200], 'budget': budget_str,
                'url': url_job, 'source': '🟢 Freelancer.com',
                'status': 'found', 'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat()
            })
            mark_seen(url_job)
        logger.info(f"Freelancer: найдено {len(projects)} проектов")
    except Exception as e:
        logger.error(f"Freelancer ошибка: {e}")
    return jobs

async def parse_telegram_channels(client) -> list:
    """Парсим публичные Telegram каналы через t.me/s/"""
    jobs = []
    for channel in TELEGRAM_JOB_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            r = await client.get(url, headers=HEADERS)
            if r.status_code != 200:
                continue
            
            # Ищем посты
            posts = re.findall(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                r.text, re.DOTALL
            )
            
            for i, post_html in enumerate(posts[:5]):
                post_text = clean_html(post_html)
                if len(post_text) < 30:
                    continue
                
                # Уникальный ID по тексту
                post_url = f"https://t.me/{channel}/{i}_{hash(post_text) % 10000}"
                if is_seen(post_url):
                    continue
                
                # Проверяем релевантность
                if not is_relevant(post_text, ""):
                    continue
                
                # Бюджет
                budget_m = re.search(r'[\$₽€£]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|EUR|\$|₽)', post_text)
                budget = budget_m.group(0).strip() if budget_m else "По договорённости"
                
                # Заголовок — первые 60 символов
                title = post_text[:60].strip() + "..."
                
                jobs.append({
                    'id': make_job_id(post_url), 'title': title,
                    'description': post_text[:1000], 'budget': budget,
                    'url': f"https://t.me/{channel}",
                    'source': f'📱 TG @{channel}',
                    'status': 'found', 'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(post_url)
                
        except Exception as e:
            logger.error(f"Telegram {channel} ошибка: {e}")
    return jobs

# ═══ AI ФУНКЦИИ ═══
async def analyze_job(job: dict) -> dict:
    prompt = f"""Фрилансер анализирует заказ. Ответь ТОЛЬКО JSON без лишнего текста.

ЗАКАЗ:
Название: {job['title']}
Описание: {job['description'][:500]}
Бюджет: {job['budget']}
Источник: {job['source']}

Верни JSON:
{{
  "can_do": true,
  "difficulty": "ЛЁГКИЙ",
  "reason": "одно предложение",
  "proposal": "proposal на языке заказа, 3 предложения",
  "estimated_time": "1 час"
}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role":"user","content":prompt}], "max_tokens": 400}
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        if "```" in text:
            text = text.split("```")[1].split("```")[0].replace("json","").strip()
        return json.loads(text)

async def execute_job(job: dict) -> str:
    prompt = f"""Выполни это фриланс-задание профессионально и полностью.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}

Результат должен быть готов к отправке клиенту."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role":"user","content":prompt}], "max_tokens": 2000}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ ГЛАВНЫЙ ПАРСЕР ═══
async def check_new_jobs(bot) -> int:
    logger.info("🔍 Проверяю все источники...")
    all_jobs = []
    
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        # Запускаем все парсеры параллельно
        results = await asyncio.gather(
            parse_guru(client),
            parse_pph(client),
            parse_freelancer(client),
            parse_telegram_channels(client),
            return_exceptions=True
        )
        
        for r in results:
            if isinstance(r, list):
                all_jobs.extend(r)
            else:
                logger.error(f"Парсер ошибка: {r}")
    
    logger.info(f"📦 Всего найдено: {len(all_jobs)} заказов")
    
    # Фильтруем релевантные
    relevant = [j for j in all_jobs if is_relevant(j['title'], j['description'])]
    logger.info(f"✅ Релевантных: {len(relevant)}")
    
    sent = 0
    for job in relevant[:4]:  # Максимум 4 за раз
        try:
            save_job(job)
            analysis = await analyze_job(job)
            if analysis.get('can_do', True):
                await send_job_card(bot, job, analysis)
                sent += 1
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
    
    return sent

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
        update_job_status(job_id, 'accepted')
        await query.edit_message_text(
            f"✅ *Берём!*\n{job['source']}\n📌 {job['title'][:80]}\n\n⏳ Выполняю...",
            parse_mode='Markdown'
        )
        try:
            result = await execute_job(job)
            update_job_status(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"✨ *ГОТОВО — Лила, проверь!*\n\n📌 *{job['title'][:80]}*\n\n━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\nОтправляем клиенту?",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data.startswith("skip_"):
        update_job_status(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job_status(job_id, 'done')
        nums = re.findall(r'\d+', job.get('budget','0'))
        amount = float(nums[0]) if nums else 0
        save_earning(job_id, amount, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n✅ Выполнено: {stats['done']}\n💵 Заработано: ${stats['earned']:.2f}\n\nБухгалтер записал 📊",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        await query.edit_message_text("✏️ Напиши что исправить:")

# ═══ КОМАНДЫ ═══
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Полифан на связи!*\n\n"
        "Мониторю сразу 4 источника:\n"
        "🟠 Guru.com\n"
        "🔵 PeoplePerHour\n"
        "🟢 Freelancer.com\n"
        "📱 Telegram каналы\n\n"
        "Проверка каждые 15 минут!\n\n"
        "/scan — проверить сейчас\n"
        "/stats — статистика",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    by_source = "\n".join([f"  {src}: {cnt}" for src, cnt in stats.get('by_source', {}).items()])
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА ПОЛИФАНА*\n\n"
        f"🔍 Найдено всего: {stats['total_found']}\n"
        f"✅ Принято: {stats['accepted']}\n"
        f"🏁 Выполнено: {stats['done']}\n"
        f"💰 Заработано: ${stats['earned']:.2f}\n\n"
        f"📡 *По источникам:*\n{by_source if by_source else '  Пока пусто'}",
        parse_mode='Markdown'
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Сканирую все источники...")
    count = await check_new_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Готово!\n\n"
        f"📦 Отправлено заказов: {count}\n"
        f"{'Заказы уже летят к тебе! 🚀' if count > 0 else 'Пока новых нет, жди следующей проверки'}",
        parse_mode='Markdown'
    )

async def periodic_check(app):
    await asyncio.sleep(60)  # Первая проверка через минуту после старта
    while True:
        try:
            await check_new_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка проверки: {e}")
        await asyncio.sleep(15 * 60)  # Каждые 15 минут

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

    logger.info("🤖 Полифан запущен! Мониторю 4 источника!")
    app.run_polling()

if __name__ == "__main__":
    main()
