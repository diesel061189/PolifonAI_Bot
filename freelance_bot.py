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
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")

# Рабочие RSS фиды (проверенные)
UPWORK_RSS_FEEDS = [
    "https://www.upwork.com/ab/feed/jobs/rss?q=data+entry&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=translation+english+russian&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=copy+paste+research&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=write+article+blog&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=transcription+audio&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=excel+spreadsheet+data&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=product+description+writing&sort=recency",
]

# Заголовки браузера чтобы Upwork не блокировал
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        title TEXT,
        description TEXT,
        budget TEXT,
        url TEXT,
        status TEXT DEFAULT 'new',
        proposal TEXT,
        result TEXT,
        created_at TEXT,
        updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (
        url TEXT PRIMARY KEY,
        seen_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT,
        amount REAL,
        currency TEXT DEFAULT 'USD',
        date TEXT,
        description TEXT
    )''')
    conn.commit()
    conn.close()

def save_job(job: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO jobs 
        (id, title, description, budget, url, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (job['id'], job['title'], job['description'], job['budget'],
         job['url'], job['status'], job['created_at'], job['updated_at']))
    conn.commit()
    conn.close()

def update_job_status(job_id: str, status: str, result: str = None):
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

def get_job(job_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM jobs WHERE id=?', (job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        cols = ['id','title','description','budget','url','status','proposal','result','created_at','updated_at']
        return dict(zip(cols, row))
    return None

def is_seen(url: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE url=?', (url,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_seen(url: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (url, seen_at) VALUES (?, ?)',
              (url, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_earning(job_id: str, amount: float, description: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id, amount, date, description) VALUES (?, ?, ?, ?)',
              (job_id, amount, datetime.now().isoformat(), description))
    conn.commit()
    conn.close()

def get_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM jobs WHERE status="found"')
    found = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM jobs WHERE status="accepted"')
    accepted = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM jobs WHERE status="done"')
    done = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount), 0) FROM earnings')
    total = c.fetchone()[0]
    conn.close()
    return {'found': found, 'accepted': accepted, 'done': done, 'total': total}

async def analyze_and_generate_proposal(job: dict) -> dict:
    prompt = f"""Ты фрилансер. Проанализируй заказ и ответь строго в JSON без лишнего текста:

ЗАКАЗ:
Название: {job['title']}
Описание: {job['description'][:600]}
Бюджет: {job['budget']}

Верни ТОЛЬКО JSON:
{{
  "can_do": true,
  "difficulty": "ЛЁГКИЙ",
  "reason": "одно предложение почему можем сделать",
  "proposal": "короткий proposal на языке заказа 3-4 предложения",
  "estimated_time": "1-2 часа"
}}"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.7
            }
        )
        result = response.json()
        text = result["choices"][0]["message"]["content"].strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)

async def execute_job(job: dict) -> str:
    prompt = f"""Ты профессиональный фрилансер. Выполни это задание качественно и полностью.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:1000]}

Выполни задание. Результат должен быть готов к отправке клиенту."""

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000,
                "temperature": 0.8
            }
        )
        result = response.json()
        return result["choices"][0]["message"]["content"].strip()

async def parse_upwork_rss() -> list:
    new_jobs = []
    
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        for feed_url in UPWORK_RSS_FEEDS:
            try:
                response = await client.get(feed_url)
                logger.info(f"RSS статус: {response.status_code} для {feed_url[:60]}")
                
                if response.status_code != 200:
                    continue
                    
                feed = feedparser.parse(response.text)
                logger.info(f"Записей в фиде: {len(feed.entries)}")
                
                for entry in feed.entries[:5]:
                    url = entry.get('link', '')
                    if not url or is_seen(url):
                        continue
                    
                    title = entry.get('title', '').strip()
                    description = entry.get('summary', '').strip()
                    
                    # Чистим HTML теги
                    description = re.sub(r'<[^>]+>', ' ', description)
                    description = re.sub(r'\s+', ' ', description).strip()
                    
                    # Бюджет из описания
                    budget_match = re.search(r'Budget:\s*\$?([\d,]+)', description)
                    budget = f"${budget_match.group(1)}" if budget_match else "По договорённости"
                    
                    job = {
                        'id': abs(hash(url)) % (10**10),
                        'title': title[:200],
                        'description': description[:1500],
                        'budget': budget,
                        'url': url,
                        'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    }
                    job['id'] = str(job['id'])
                    new_jobs.append(job)
                    mark_seen(url)
                    
            except Exception as e:
                logger.error(f"Ошибка парсинга {feed_url[:50]}: {e}")
    
    return new_jobs

async def send_job_to_user(bot, job: dict, analysis: dict):
    difficulty_emoji = {"ЛЁГКИЙ": "🟢", "СРЕДНИЙ": "🟡", "СЛОЖНЫЙ": "🔴"}.get(
        analysis.get('difficulty', ''), "⚪"
    )
    
    msg = f"""🎯 *НОВЫЙ ЗАКАЗ*

📌 *{job['title'][:100]}*
💰 Бюджет: {job['budget']}
{difficulty_emoji} Сложность: {analysis.get('difficulty', '?')}
⏱ Время: {analysis.get('estimated_time', '?')}

💬 _{analysis.get('reason', '')}_

*Proposal:*
{analysis.get('proposal', '')[:500]}

🔗 [Открыть заказ]({job['url']})"""

    keyboard = [[
        InlineKeyboardButton("✅ Берём!", callback_data=f"take_{job['id']}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{job['id']}")
    ]]
    
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=msg,
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
            f"✅ *Берём!*\n\n📌 {job['title'][:80]}\n\n⏳ Выполняю работу...",
            parse_mode='Markdown'
        )
        
        try:
            result = await execute_job(job)
            update_job_status(job_id, 'completed', result)
            
            result_msg = f"""✨ *РАБОТА ВЫПОЛНЕНА*

📌 *{job['title'][:80]}*

━━━━━━━━━━━━━━━━
{result[:2500]}
━━━━━━━━━━━━━━━━

*Лила, проверь — отправляем клиенту?*"""

            keyboard = [[
                InlineKeyboardButton("👍 ОК, отправляем!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Нужна правка", callback_data=f"redo_{job_id}")
            ]]
            
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=result_msg,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"❌ Ошибка выполнения: {str(e)[:200]}"
            )

    elif data.startswith("skip_"):
        update_job_status(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job_status(job_id, 'done')
        nums = re.findall(r'\d+', job.get('budget', '0'))
        amount = float(nums[0]) if nums else 0
        save_earning(job_id, amount, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n✅ Выполнено: {stats['done']}\n💵 Заработано: ${stats['total']:.2f}",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        await query.edit_message_text(
            "✏️ Напиши что исправить — переделаю:",
            parse_mode='Markdown'
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА*\n\n"
        f"🔍 Найдено: {stats['found']}\n"
        f"✅ Принято: {stats['accepted']}\n"
        f"🏁 Выполнено: {stats['done']}\n"
        f"💰 Заработано: ${stats['total']:.2f}",
        parse_mode='Markdown'
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Freelance Bot запущен!*\n\n"
        "Мониторю Upwork каждые 15 минут.\n\n"
        "/scan — проверить сейчас\n"
        "/stats — статистика",
        parse_mode='Markdown'
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Сканирую Upwork...")
    count = await check_new_jobs(context.application.bot)
    await msg.edit_text(f"✅ Готово! Найдено новых заказов: {count}")

async def check_new_jobs(bot) -> int:
    logger.info("Проверяю новые заказы...")
    jobs = await parse_upwork_rss()
    logger.info(f"Найдено: {len(jobs)}")
    
    sent = 0
    for job in jobs[:3]:
        try:
            save_job(job)
            analysis = await analyze_and_generate_proposal(job)
            if analysis.get('can_do', True):
                await send_job_to_user(bot, job, analysis)
                sent += 1
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    return sent

async def periodic_check(app):
    while True:
        await asyncio.sleep(15 * 60)
        try:
            await check_new_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка проверки: {e}")

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

    logger.info("🤖 Freelance Bot запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
