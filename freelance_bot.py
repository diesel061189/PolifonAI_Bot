import os
import json
import logging
import asyncio
import feedparser
import httpx
import sqlite3
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("FREELANCE_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")
FL_PHPSESSID = os.getenv("FL_PHPSESSID", "")
FL_XSRF_TOKEN = os.getenv("FL_XSRF_TOKEN", "")

# Разные User-Agent для обхода блокировок
HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0"},
    {"User-Agent": "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)"},
    {"User-Agent": "FeedValidator/1.3"},
]

import random
def get_headers():
    return random.choice(HEADERS_LIST)

# ═══ ИСТОЧНИКИ — только те что реально работают с Railway ═══

# RSS фиды которые работают с облачных серверов
RSS_FEEDS = [
    # FL.ru — иногда работает с разными UA
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=2", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    # Хабр Фриланс — часто открыт
    ("https://freelance.habr.com/tasks.rss", "🟣 Хабр Фриланс"),
    ("https://freelance.habr.com/tasks.rss?q=перевод", "🟣 Хабр/Переводы"),
    ("https://freelance.habr.com/tasks.rss?q=копирайтинг", "🟣 Хабр/Копирайтинг"),
    ("https://freelance.habr.com/tasks.rss?q=текст", "🟣 Хабр/Тексты"),
    # Remote.co RSS
    ("https://remote.co/remote-jobs/writer/feed/", "🌍 Remote.co/Writer"),
    ("https://remote.co/remote-jobs/translator/feed/", "🌍 Remote.co/Translator"),
    # We Work Remotely RSS
    ("https://weworkremotely.com/categories/remote-writing-jobs.rss", "🌍 WWR/Writing"),
    ("https://weworkremotely.com/categories/remote-jobs.rss", "🌍 WWR/All"),
    # ProBlogger Jobs
    ("https://problogger.com/jobs/feed/", "🌍 ProBlogger"),
    # Freelance Writing Jobs
    ("https://www.freelancewritinggigs.com/feed/", "🌍 FreelanceWriting"),
]

# Белый список
WHITELIST = [
    "написать", "перевод", "перевести", "текст", "статья", "контент",
    "копирайтинг", "копирайтер", "редактура", "корректура",
    "ввод данных", "data entry", "copy paste", "таблица", "excel",
    "описание товара", "карточка", "wildberries", "ozon", "маркетплейс",
    "исследование", "research", "сбор данных", "парсинг",
    "translation", "translate", "writing", "writer", "content",
    "article", "blog", "copywriting", "proofreading", "editing",
    "transcription", "summarize", "virtual assistant",
    "product description", "amazon", "etsy", "shopify",
    "remote", "freelance", "работа удалённо",
]

BLACKLIST = [
    "developer", "программист", "разработчик", "coding", "react", "angular",
    "mobile app", "android", "ios", "backend", "frontend", "wordpress dev",
    "video edit", "монтаж", "animation", "after effects", "3d",
    "logo design", "логотип", "ui/ux", "illustration",
    "google ads", "facebook ads", "таргет",
    "устный перевод", "синхронный", "носитель языка",
    "казахский", "японский", "хинди", "арабский",
    "чертёж", "autocad", "solidworks",
    "написать отзыв", "публикация отзыва",
    "tax form", "irs form", "penalty relief",
    "допечатная", "indesign",
    "50 000", "100 000",
]

MAX_BUDGET_RUB = 15000
MAX_BUDGET_USD = 300

def is_relevant(title: str, description: str = "", budget: str = "") -> bool:
    text = (title + " " + description).lower()
    for bad in BLACKLIST:
        if bad in text:
            return False
    if budget:
        nums = re.findall(r'\d+', budget.replace(',','').replace(' ',''))
        if nums:
            mx = max(int(n) for n in nums)
            if ('₽' in budget or 'руб' in budget.lower()) and mx > MAX_BUDGET_RUB:
                return False
            if ('$' in budget or 'usd' in budget.lower()) and mx > MAX_BUDGET_USD:
                return False
    if any(kw in text for kw in WHITELIST):
        return True
    # Английские заголовки без стоп-слов — берём
    if re.match(r'^[a-zA-Z\s\d\-\,\.\/\(\)]+$', title.strip()) and len(title) > 10:
        return True
    return False

# ═══ БАЗА ДАННЫХ ═══
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, description TEXT,
        budget TEXT, url TEXT, source TEXT,
        status TEXT DEFAULT 'found', result TEXT, proposal TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (url TEXT PRIMARY KEY, seen_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT, amount_usd REAL, amount_rub REAL, date TEXT, description TEXT
    )''')
    conn.commit()
    conn.close()

def save_job(job):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO jobs
        (id,title,description,budget,url,source,status,proposal,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (job['id'],job['title'],job['description'],job['budget'],
         job['url'],job['source'],job['status'],job.get('proposal',''),
         job['created_at'],job['updated_at']))
    conn.commit()
    conn.close()

def update_job(job_id, status, result=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if result:
        c.execute('UPDATE jobs SET status=?,result=?,updated_at=? WHERE id=?',
                  (status,result,datetime.now().isoformat(),job_id))
    else:
        c.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=?',
                  (status,datetime.now().isoformat(),job_id))
    conn.commit()
    conn.close()

def get_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id,title,description,budget,url,source,status,result,proposal FROM jobs WHERE id=?',(job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(zip(['id','title','description','budget','url','source','status','result','proposal'],row))
    return None

def is_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE url=?',(url,))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (url,seen_at) VALUES (?,?)',
              (url,datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_earning(job_id, usd, rub, desc):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id,amount_usd,amount_rub,date,description) VALUES (?,?,?,?,?)',
              (job_id,usd,rub,datetime.now().isoformat(),desc))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')
    by_status = dict(c.fetchall())
    c.execute('SELECT COALESCE(SUM(amount_usd),0),COALESCE(SUM(amount_rub),0) FROM earnings')
    earn = c.fetchone()
    c.execute('SELECT source,COUNT(*) FROM jobs GROUP BY source ORDER BY COUNT(*) DESC LIMIT 5')
    by_src = c.fetchall()
    conn.close()
    return {'by_status':by_status,'earn_usd':earn[0],'earn_rub':earn[1],'by_src':by_src}

def clean_html(text):
    text = re.sub(r'<[^>]+>',' ',text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;|&#\d+;',' ',text)
    return re.sub(r'\s+',' ',text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

# ═══ ПАРСЕР RSS ═══
async def parse_rss(client, url, source) -> list:
    jobs = []
    try:
        headers = get_headers()
        headers['Accept'] = 'application/rss+xml,application/xml,text/xml,*/*'
        headers['Accept-Language'] = 'ru-RU,ru;q=0.9,en;q=0.8'
        
        r = await client.get(url, headers=headers, timeout=15)
        logger.info(f"{source}: {r.status_code}")
        
        if r.status_code != 200:
            return jobs
            
        feed = feedparser.parse(r.text)
        
        if not feed.entries:
            logger.info(f"{source}: пустой фид")
            return jobs
        
        logger.info(f"{source}: {len(feed.entries)} записей")
        
        for entry in feed.entries[:10]:
            link = entry.get('link','')
            if not link or is_seen(link):
                continue
            
            title = clean_html(entry.get('title',''))
            desc = clean_html(entry.get('summary', entry.get('description','')))
            
            # Бюджет
            budget_m = re.search(r'[\$₽£€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|GBP|\$|₽)', desc+title)
            budget = budget_m.group(0).strip() if budget_m else "Договорная"
            
            if is_relevant(title, desc[:200], budget):
                jobs.append({
                    'id': make_id(link),
                    'title': title[:200],
                    'description': desc[:1000],
                    'budget': budget,
                    'url': link,
                    'source': source,
                    'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
            mark_seen(link)
            
    except Exception as e:
        logger.error(f"❌ {source}: {e}")
    
    return jobs

# ═══ AI ═══
async def analyze_job(job: dict) -> dict:
    is_ru = bool(re.search(r'[а-яё]', (job['title']+job['description'][:50]).lower()))
    lang = "русском" if is_ru else "English"
    prompt = f"""Фрилансер анализирует заказ. Ответь ТОЛЬКО JSON без лишнего текста.
ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:400]}
БЮДЖЕТ: {job['budget']}
Верни JSON:
{{"can_do":true,"difficulty":"ЛЁГКИЙ","reason":"одно предложение на русском","proposal":"proposal на {lang} 3 предложения","estimated_time":"1 час"}}"""
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],"max_tokens":400}
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        if "```" in text:
            text = text.split("```")[1].split("```")[0].replace("json","").strip()
        start,end = text.find('{'),text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]
        return json.loads(text)

async def execute_job(job: dict) -> str:
    is_ru = bool(re.search(r'[а-яё]', (job['title']+job['description'][:50]).lower()))
    lang = "Отвечай на русском." if is_ru else "Reply in English."
    prompt = f"""Выполни фриланс задание профессионально. {lang}
ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}
Подпись в конце: Артём.
Результат готов к отправке клиенту."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt}],"max_tokens":2000}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ FL.RU АВТООТКЛИК ═══
async def fl_apply(job_url: str, proposal: str) -> bool:
    if not FL_PHPSESSID:
        return False
    try:
        job_id_m = re.search(r'/projects/(\d+)/', job_url)
        if not job_id_m:
            return False
        job_id = job_id_m.group(1)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"https://www.fl.ru/api/project/{job_id}/bid/",
                headers={"User-Agent":"Mozilla/5.0","X-XSRF-TOKEN":FL_XSRF_TOKEN,"Referer":"https://www.fl.ru/"},
                cookies={"PHPSESSID":FL_PHPSESSID,"XSRF-TOKEN":FL_XSRF_TOKEN},
                json={"description":proposal,"cost":"","term":"1","term_type":"day"}
            )
            return r.status_code in [200,201]
    except Exception as e:
        logger.error(f"FL apply error: {e}")
        return False

async def fl_send_result(job_url: str, result: str) -> bool:
    if not FL_PHPSESSID:
        return False
    try:
        job_id_m = re.search(r'/projects/(\d+)/', job_url)
        if not job_id_m:
            return False
        job_id = job_id_m.group(1)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"https://www.fl.ru/api/project/{job_id}/message/",
                headers={"User-Agent":"Mozilla/5.0","X-XSRF-TOKEN":FL_XSRF_TOKEN},
                cookies={"PHPSESSID":FL_PHPSESSID,"XSRF-TOKEN":FL_XSRF_TOKEN},
                json={"message":result}
            )
            return r.status_code in [200,201]
    except:
        return False

# ═══ UI ═══
async def send_job_card(bot, job: dict, analysis: dict):
    diff = {"ЛЁГКИЙ":"🟢","СРЕДНИЙ":"🟡","СЛОЖНЫЙ":"🔴"}.get(analysis.get('difficulty',''),"⚪")
    msg = (f"🎯 *НОВЫЙ ЗАКАЗ*\n{job['source']}\n\n"
           f"📌 *{job['title'][:100]}*\n"
           f"💰 {job['budget']}\n"
           f"{diff} {analysis.get('difficulty','?')} · ⏱ {analysis.get('estimated_time','?')}\n\n"
           f"💬 _{analysis.get('reason','')}_\n\n"
           f"📝 *Proposal:*\n{analysis.get('proposal','')[:400]}\n\n"
           f"🔗 [Открыть заказ]({job['url']})")
    keyboard = [[
        InlineKeyboardButton("✅ Берём!", callback_data=f"take_{job['id']}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{job['id']}")
    ],[
        InlineKeyboardButton("📋 Скопировать proposal", callback_data=f"copy_{job['id']}"),
        InlineKeyboardButton("🚫 Не наш заказ", callback_data=f"notours_{job['id']}")
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
        update_job(job_id,'accepted')
        await query.edit_message_text(
            f"✅ *Берём!*\n{job['source']}\n📌 {job['title'][:80]}\n\n⏳ Выполняю...",
            parse_mode='Markdown')
        # Автоотклик FL.ru
        if "fl.ru" in job.get('source','').lower() and FL_PHPSESSID:
            proposal = job.get('proposal','Здравствуйте! Готов выполнить заказ качественно. Артём')
            if await fl_apply(job['url'], proposal):
                await context.bot.send_message(chat_id=YOUR_CHAT_ID, text="📨 Отклик отправлен на FL.ru!")
        try:
            result = await execute_job(job)
            update_job(job_id,'completed',result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            msg = (f"✨ *ГОТОВО!*\n\n📌 *{job['title'][:80]}*\n\n"
                   f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n"
                   f"*Лила, проверь — отправляем?*")
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg,
                parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg,
                    parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data.startswith("skip_"):
        update_job(data[5:],'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data.startswith("copy_"):
        job_id = data[5:]
        job = get_job(job_id)
        if not job:
            await query.answer("❌ Не найден", show_alert=True)
            return
        await query.answer("⏳ Генерирую...")
        try:
            analysis = await analyze_job(job)
            proposal = analysis.get('proposal','')
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"📋 *PROPOSAL — нажми чтобы скопировать:*\n\n```\n{proposal}\n```\n\n🔗 [Открыть заказ]({job['url']})\n\n1️⃣ Нажми на текст → скопируется\n2️⃣ Открой заказ\n3️⃣ Вставь и отправь!",
                parse_mode='Markdown', disable_web_page_preview=True)
        except Exception as e:
            await query.answer(f"❌ {str(e)[:50]}", show_alert=True)

    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job(job_id,'done')
        nums = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
        amount = float(nums[0]) if nums else 0
        is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
        if is_rub:
            save_earning(job_id, amount/90, amount, job['title'])
        else:
            save_earning(job_id, amount, amount*90, job['title'])
        if "fl.ru" in job.get('source','').lower() and FL_PHPSESSID and job.get('result'):
            await fl_send_result(job['url'], job['result'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n✅ Выполнено: {stats['by_status'].get('done',0)}\n💵 ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\nБухгалтер записал 📊",
            parse_mode='Markdown')

    elif data.startswith("redo_"):
        job_id = data[5:]
        job = get_job(job_id)
        # Сохраняем job_id в контексте пользователя
        context.user_data['redo_job_id'] = job_id
        context.user_data['redo_result'] = job.get('result','') if job else ''
        await query.edit_message_text(
            "✏️ *Напиши что исправить:*\n\nНапример: _убери цены_, _сократи текст_, _переведи на русский_",
            parse_mode='Markdown'
        )

    elif data.startswith("notours_"):
        job_id = data[8:]
        update_job(job_id,'skipped')
        keyboard = [[
            InlineKeyboardButton("🎨 Спец. программы", callback_data=f"reason_soft_{job_id}"),
            InlineKeyboardButton("⚖️ Юридика/медицина", callback_data=f"reason_legal_{job_id}")
        ],[
            InlineKeyboardButton("💻 Сложная разработка", callback_data=f"reason_dev_{job_id}"),
            InlineKeyboardButton("🎬 Видео/дизайн", callback_data=f"reason_media_{job_id}")
        ]]
        await query.edit_message_text("🚫 Не наш!\n\nПочему?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("reason_"):
        await query.edit_message_text("✅ Записал! Фильтр будет улучшен 📊")

# ═══ КОМАНДЫ ═══
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения — правки к выполненной работе"""
    
    # Проверяем есть ли активная правка
    job_id = context.user_data.get('redo_job_id')
    if not job_id:
        return
    
    fix_instruction = update.message.text
    original_result = context.user_data.get('redo_result', '')
    job = get_job(job_id)
    
    if not job:
        await update.message.reply_text("❌ Заказ не найден")
        return
    
    await update.message.reply_text("⏳ Исправляю...")
    
    try:
        is_ru = bool(re.search(r'[а-яё]', (job['title']+job['description'][:50]).lower()))
        lang = "Отвечай на русском." if is_ru else "Reply in English."
        
        prompt = f"""Исправь текст согласно инструкции. {lang}

ОРИГИНАЛЬНЫЙ ТЕКСТ:
{original_result[:2000]}

ИНСТРУКЦИЯ ДЛЯ ИСПРАВЛЕНИЯ:
{fix_instruction}

Верни исправленный текст полностью. Подпись в конце: Артём."""

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
                json={"model":"llama-3.3-70b-versatile",
                      "messages":[{"role":"user","content":prompt}],
                      "max_tokens":2000}
            )
            new_result = r.json()["choices"][0]["message"]["content"].strip()
        
        # Сохраняем исправленный результат
        update_job(job_id, 'completed', new_result)
        context.user_data['redo_result'] = new_result
        
        keyboard = [[
            InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
            InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
        ]]
        
        await update.message.reply_text(
            f"✨ *ИСПРАВЛЕНО!*\n\n📌 *{job['title'][:80]}*\n\n"
            f"━━━━━━━━━━\n{new_result[:2500]}\n━━━━━━━━━━\n\n"
            f"*Лила, проверь — отправляем?*",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Уведомляем Лилу
        if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
            await context.bot.send_message(
                chat_id=LILU_CHAT_ID,
                text=f"✨ *ИСПРАВЛЕНО!*\n\n📌 *{job['title'][:80]}*\n\n"
                     f"━━━━━━━━━━\n{new_result[:2000]}\n━━━━━━━━━━\n\n"
                     f"*Лила, проверь — отправляем?*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Очищаем контекст
        context.user_data.pop('redo_job_id', None)
        context.user_data.pop('redo_result', None)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")


    await update.message.reply_text(
        "🤖 *Полифан на связи!*\n\n"
        "Мониторю источники:\n"
        "🇷🇺 FL.ru\n"
        "🟣 Хабр Фриланс\n"
        "🌍 We Work Remotely\n"
        "🌍 Remote.co\n"
        "🌍 ProBlogger Jobs\n"
        "📱 Telegram каналы\n\n"
        "Проверка каждые 15 минут!\n\n"
        "/scan — проверить сейчас\n"
        "/stats — статистика",
        parse_mode='Markdown')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Полифан на связи!*\n\n"
        "Мониторю источники:\n"
        "🇷🇺 FL.ru\n"
        "🟣 Хабр Фриланс\n"
        "🌍 We Work Remotely\n"
        "🌍 Remote.co\n"
        "🌍 ProBlogger Jobs\n\n"
        "Проверка каждые 15 минут!\n\n"
        "/scan — проверить сейчас\n"
        "/stats — статистика",
        parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    by_src = "\n".join([f"  {s}: {c}" for s,c in stats['by_src']])
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА ПОЛИФАНА*\n\n"
        f"🔍 Найдено: {stats['by_status'].get('found',0)}\n"
        f"✅ Принято: {stats['by_status'].get('accepted',0)}\n"
        f"🏁 Выполнено: {stats['by_status'].get('done',0)}\n"
        f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\n"
        f"📡 *По источникам:*\n{by_src if by_src else '  Пока нет данных'}",
        parse_mode='Markdown')

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Сканирую все источники...")
    count = await check_new_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Готово!\n📨 Найдено заказов: {count}\n"
        f"{'Заказы летят! 🚀' if count > 0 else 'Пока 0 — попробуй через 15 минут'}")

# ═══ ГЛАВНЫЙ ПАРСЕР ═══
async def check_new_jobs(bot) -> int:
    logger.info("🔍 Сканирую источники...")
    all_jobs = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        tasks = [parse_rss(client, url, source) for url, source in RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_jobs.extend(r)

    logger.info(f"📦 Всего: {len(all_jobs)}")
    relevant = [j for j in all_jobs if is_relevant(j['title'], j['description'], j.get('budget',''))]
    logger.info(f"✅ Релевантных: {len(relevant)}")

    sent = 0
    for job in relevant[:6]:
        try:
            save_job(job)
            analysis = await analyze_job(job)
            job['proposal'] = analysis.get('proposal','')
            save_job(job)
            if analysis.get('can_do', True):
                await send_job_card(bot, job, analysis)
                sent += 1
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    return sent

async def periodic_check(app):
    await asyncio.sleep(30)
    while True:
        try:
            await check_new_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
        await asyncio.sleep(15 * 60)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def post_init(application):
        asyncio.create_task(periodic_check(application))
        try:
            await application.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text="🤖 *Полифан запущен!*\nСканирую все источники...",
                parse_mode='Markdown')
        except:
            pass
    app.post_init = post_init

    logger.info("🤖 Полифан запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
