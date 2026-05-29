import os
import json
import logging
import asyncio
import feedparser
import httpx
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══ НАСТРОЙКИ ═══
TELEGRAM_TOKEN = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")          # тот же что у Лилы
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0")) # твой Telegram ID
DB_PATH = os.getenv("DB_PATH", "/data/freelance.db")

# RSS фиды Upwork (мелкие задачи, любые категории)
UPWORK_RSS_FEEDS = [
    "https://www.upwork.com/ab/feed/jobs/rss?q=&sort=recency&budget=50&max_budget=200&job_type=fixed",
    "https://www.upwork.com/ab/feed/jobs/rss?q=translation+writing+data&sort=recency&budget=10&max_budget=100",
    "https://www.upwork.com/ab/feed/jobs/rss?q=copy+paste+data+entry&sort=recency",
    "https://www.upwork.com/ab/feed/jobs/rss?q=research+excel+spreadsheet&sort=recency",
]

# Ключевые слова мелких задач
SMALL_TASK_KEYWORDS = [
    "data entry", "copy paste", "translation", "translate", "transcription",
    "research", "spreadsheet", "excel", "csv", "write description",
    "product description", "short article", "blog post", "rewrite",
    "proofread", "edit text", "summarize", "list", "categorize",
    "ввод данных", "перевод", "написать", "описание", "статья",
    "таблица", "исследование", "редактура", "транскрипция"
]

# Максимальный бюджет для "мелких" задач
MAX_BUDGET = 200

# ═══ БАЗА ДАННЫХ ═══
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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

# ═══ AI ФУНКЦИИ ═══
async def analyze_and_generate_proposal(job: dict) -> dict:
    """Анализирует заказ и генерирует proposal на нужном языке"""
    
    # Определяем язык описания
    is_english = any(c.isascii() and c.isalpha() for c in job['description'][:100])
    lang = "English" if is_english else "Russian"
    
    prompt = f"""Ты фрилансер. Проанализируй этот заказ и:
1. Скажи можно ли его выполнить с помощью AI (тексты, данные, перевод, исследование)
2. Оцени сложность: ЛЁГКИЙ / СРЕДНИЙ / СЛОЖНЫЙ
3. Напиши короткий proposal на языке заказа ({lang}), 3-4 предложения, живой и конкретный

ЗАКАЗ:
Название: {job['title']}
Описание: {job['description'][:800]}
Бюджет: {job['budget']}

Ответь строго в JSON:
{{
  "can_do": true/false,
  "difficulty": "ЛЁГКИЙ/СРЕДНИЙ/СЛОЖНЫЙ", 
  "reason": "почему можно/нельзя (1 предложение)",
  "proposal": "текст proposal",
  "estimated_time": "примерное время выполнения"
}}"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.7
            }
        )
        result = response.json()
        text = result["choices"][0]["message"]["content"].strip()
        
        # Чистим JSON
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        
        return json.loads(text)

async def execute_job(job: dict) -> str:
    """Выполняет работу по заказу с помощью AI"""
    
    prompt = f"""Ты профессиональный фрилансер. Выполни это задание качественно.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:1000]}

Выполни задание полностью. Если нужен текст — напиши его.
Если нужен перевод — переведи. Если нужно исследование — проведи.
Результат должен быть готов к отправке клиенту."""

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

# ═══ ПАРСЕР ЗАКАЗОВ ═══
def is_small_task(title: str, description: str, budget_str: str) -> bool:
    """Проверяет подходит ли заказ"""
    text = (title + " " + description).lower()
    
    # Проверка ключевых слов
    has_keyword = any(kw in text for kw in SMALL_TASK_KEYWORDS)
    
    # Проверка бюджета
    budget_ok = True
    if budget_str:
        import re
        numbers = re.findall(r'\d+', budget_str.replace(',', ''))
        if numbers:
            max_num = max(int(n) for n in numbers)
            budget_ok = max_num <= MAX_BUDGET
    
    return has_keyword or budget_ok

async def parse_upwork_rss() -> list:
    """Парсит RSS Upwork и возвращает новые заказы"""
    new_jobs = []
    
    for feed_url in UPWORK_RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                url = entry.get('link', '')
                if not url or is_seen(url):
                    continue
                
                title = entry.get('title', '')
                description = entry.get('summary', '')
                
                # Извлекаем бюджет из описания
                import re
                budget_match = re.search(r'Budget:\s*\$?([\d,]+)', description)
                budget = budget_match.group(0) if budget_match else "Не указан"
                
                if is_small_task(title, description, budget):
                    job = {
                        'id': url[-40:].replace('/', '_'),
                        'title': title,
                        'description': description[:1500],
                        'budget': budget,
                        'url': url,
                        'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    }
                    new_jobs.append(job)
                
                mark_seen(url)
        except Exception as e:
            logger.error(f"Ошибка парсинга RSS: {e}")
    
    return new_jobs

# ═══ TELEGRAM ХЕНДЛЕРЫ ═══
async def send_job_to_user(bot, job: dict, analysis: dict):
    """Отправляет карточку заказа пользователю"""
    
    difficulty_emoji = {"ЛЁГКИЙ": "🟢", "СРЕДНИЙ": "🟡", "СЛОЖНЫЙ": "🔴"}.get(
        analysis.get('difficulty', ''), "⚪"
    )
    
    msg = f"""🎯 *НОВЫЙ ЗАКАЗ*

📌 *{job['title']}*
💰 Бюджет: {job['budget']}
{difficulty_emoji} Сложность: {analysis.get('difficulty', '?')}
⏱ Время: {analysis.get('estimated_time', '?')}

📝 *Моё резюме:* {analysis.get('reason', '')}

*Proposal готов:*
_{analysis.get('proposal', '')}_

🔗 [Смотреть заказ]({job['url']})"""

    keyboard = [
        [
            InlineKeyboardButton("✅ Берём!", callback_data=f"take_{job['id']}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{job['id']}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=msg,
        parse_mode='Markdown',
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("take_"):
        job_id = data[5:]
        job = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден в базе")
            return
        
        update_job_status(job_id, 'accepted')
        await query.edit_message_text(
            f"✅ *Берём заказ!*\n\n📌 {job['title']}\n\n⏳ Выполняю работу...",
            parse_mode='Markdown'
        )
        
        # Выполняем работу
        try:
            result = await execute_job(job)
            update_job_status(job_id, 'completed', result)
            
            # Отправляем результат через Лилу
            result_msg = f"""✨ *РАБОТА ВЫПОЛНЕНА*

📌 *{job['title']}*

━━━━━━━━━━━━━━━━
{result[:2000]}
━━━━━━━━━━━━━━━━

*Лила, проверь пожалуйста и скажи — отправляем клиенту?*"""

            keyboard = [
                [
                    InlineKeyboardButton("👍 ОК, отправляем!", callback_data=f"done_{job_id}"),
                    InlineKeyboardButton("✏️ Нужна правка", callback_data=f"redo_{job_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=result_msg,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"❌ Ошибка при выполнении: {e}"
            )
    
    elif data.startswith("skip_"):
        job_id = data[5:]
        update_job_status(job_id, 'skipped')
        await query.edit_message_text("⏭ Пропустили заказ")
    
    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job_status(job_id, 'done')
        
        # Записываем в бухгалтерию
        import re
        budget_nums = re.findall(r'\d+', job.get('budget', '0'))
        amount = float(budget_nums[0]) if budget_nums else 0
        save_earning(job_id, amount, job['title'])
        
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n"
            f"✅ Выполнено заказов: {stats['done']}\n"
            f"💵 Общий заработок: ${stats['total']:.2f}\n\n"
            f"Бухгалтер уже записал 📊",
            parse_mode='Markdown'
        )
    
    elif data.startswith("redo_"):
        job_id = data[5:]
        await query.edit_message_text(
            "✏️ *Нужна правка*\n\nНапиши что именно исправить, и я переделаю:",
            parse_mode='Markdown'
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats — статистика"""
    stats = get_stats()
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА*\n\n"
        f"🔍 Найдено заказов: {stats['found']}\n"
        f"✅ Принято: {stats['accepted']}\n"
        f"🏁 Выполнено: {stats['done']}\n"
        f"💰 Заработано: ${stats['total']:.2f}",
        parse_mode='Markdown'
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Freelance Bot запущен!*\n\n"
        "Я мониторю Upwork каждые 15 минут и нахожу мелкие заказы.\n\n"
        "Команды:\n"
        "/stats — статистика\n"
        "/scan — проверить прямо сейчас",
        parse_mode='Markdown'
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск сканирования"""
    await update.message.reply_text("🔍 Сканирую Upwork...")
    await check_new_jobs(context.application.bot)

# ═══ ОСНОВНОЙ ЦИКЛ ═══
async def check_new_jobs(bot):
    """Главная функция — ищет и обрабатывает новые заказы"""
    logger.info("Проверяю новые заказы...")
    
    jobs = await parse_upwork_rss()
    logger.info(f"Найдено новых заказов: {len(jobs)}")
    
    for job in jobs[:3]:  # Не более 3 за раз
        try:
            save_job(job)
            analysis = await analyze_and_generate_proposal(job)
            
            if analysis.get('can_do', False):
                await send_job_to_user(bot, job, analysis)
                await asyncio.sleep(2)  # Пауза между сообщениями
        except Exception as e:
            logger.error(f"Ошибка обработки заказа: {e}")

async def periodic_check(app):
    """Периодическая проверка каждые 15 минут"""
    while True:
        await asyncio.sleep(15 * 60)  # 15 минут
        try:
            await check_new_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка периодической проверки: {e}")

def main():
    init_db()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Запускаем периодическую проверку
    async def post_init(application):
        asyncio.create_task(periodic_check(application))
    
    app.post_init = post_init
    
    logger.info("🤖 Freelance Bot запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
