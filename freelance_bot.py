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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
YOUR_CHAT_ID    = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID    = int(os.getenv("LILU_CHAT_ID", str(YOUR_CHAT_ID)))
LILU_BOT_TOKEN  = os.getenv("LILU_BOT_TOKEN", "")
DB_PATH         = os.getenv("DB_PATH", "/tmp/freelance.db")
FL_PHPSESSID    = os.getenv("FL_PHPSESSID", "")
FL_XSRF_TOKEN   = os.getenv("FL_XSRF_TOKEN", "")
KWORK_URL       = os.getenv("KWORK_URL", "https://kwork.ru/user/artem_sh")

ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_HAIKU = "claude-haiku-4-5-20251001"

POLYFAN_SKILLS = """Полифан умеет делать:
- Тексты, статьи, блог-посты (EN/RU/DE)
- Копирайтинг и рерайтинг
- Описания товаров для сайтов и маркетплейсов
- Переводы EN↔RU, DE↔RU, FR↔RU и другие языки
- Технические и деловые переводы
- Посты для соцсетей (Instagram, Telegram, VK)
- Email-рассылки и письма клиентам
- Тексты для лендингов и сайтов
- Корректура и редактура
- Карточки товаров WB/Ozon/ЯМ (через Карточника)
- Кастомные proposals под каждый заказ

НЕ умеем: программирование, дизайн вручную, видео, SEO-технический аудит"""

RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://remoteok.com/remote-writing-jobs.json", "🌍 RemoteOK"),
    ("https://jobicy.com/?feed=job_feed&job_categories=writing", "🌍 Jobicy"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

KEYWORDS = [
    # Тексты и статьи
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент", "рерайтинг",
    "блог", "blog post", "article", "content writing",
    "статья", "тексты для", "наполнение сайта",
    # Переводы
    "перевод", "перевести", "translation", "translate",
    "перевод текста", "перевести текст", "переводчик",
    "перевод с английского", "перевод на английский",
    "перевод немецкий", "перевод французский",
    "технический перевод", "деловой перевод",
    "перевод договора", "перевод инструкции",
    # Редактура
    "редактура", "корректура", "proofreading", "editing",
    # Email и рассылки
    "email рассылка", "email маркетинг", "письмо клиентам",
    "welcome письмо", "триггерное письмо", "email копирайтинг",
    # Лендинги и сайты
    "текст для лендинга", "тексты для сайта",
    "about us", "about page", "landing page text",
    "текст для страницы", "продающий текст",
    # Соцсети
    "посты в инстаграм", "контент для telegram", "контент телеграм",
    "smm копирайтинг", "ведение соцсетей тексты",
    "посты для соцсетей", "контент план",
    # Маркетплейсы
    "карточка товара", "описание товара", "маркетплейс",
    "wildberries", "wb ", " вб ", "ozon", "озон",
    "яндекс маркет",
    # EN
    "product description", "copywriting", "proofreading",
    "content writer", "article writer", "blog writer",
    # Proposals и документы
    "написать proposal", "написать отклик", "написать питч",
    "резюме", "сопроводительное письмо",
]

BLACKLIST = [
    "программирование", "разработка", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "мобильное приложение",
    "android", "ios", "чертёж", "курсовая", "дипломная",
    "купить и отправить", "курьер", "доставить",
]

AI_REJECTION_PHRASES = [
    "без нейросетей", "без ии", "без ai", "no ai", "not ai",
    "кроме ии", "кроме ai", "кроме нейросетей",
    "не используя ии", "не используя ai",
    "только вручную", "исключительно вручную",
    "ai не принимается", "нейросети не принимаются",
    "нейросеть не подходит", "нейросети не подходят",
    "кроме инструментов ии", "без использования ии",
    "портфолио без нейросетей", "работы без ai",
    "только реальные работы", "без использования нейросетей",
]

PRIORITY_BOOST = [
    "карточка товара", "карточки товаров",
    "инфографика", "wildberries", "wb", "ozon", "озон", "яндекс маркет",
    "описание товара", "seo описание", "быстро", "срочно",
    "копирайтер нужен", "ищу копирайтера",
    "перевод срочно", "нужен переводчик", "email копирайтер",
]

def is_ai_rejection(title: str, desc: str) -> tuple:
    text = (title + " " + desc).lower()
    for phrase in AI_REJECTION_PHRASES:
        if phrase.lower() in text:
            return True, phrase
    return False, ""

def get_priority_score(title: str, desc: str) -> int:
    text = (title + " " + desc).lower()
    score = 5
    for phrase in PRIORITY_BOOST:
        if phrase.lower() in text:
            score += 1
    return min(10, score)

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
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount_usd REAL DEFAULT 0,
        amount_rub REAL DEFAULT 0,
        description TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS filtered_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, url TEXT, reason TEXT, date TEXT
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

# ═══ УМНЫЙ PROPOSAL ЧЕРЕЗ ANTHROPIC ═══

async def generate_smart_proposal(job: dict) -> str:
    """Генерирует кастомный отклик под каждый заказ через Anthropic Haiku"""
    if not ANTHROPIC_API_KEY:
        return ""
    
    title  = job.get('title', '')
    desc   = job.get('description', '')[:500]
    budget = job.get('budget', 'не указан')
    source = job.get('source', '')
    
    # Определяем язык заказа
    is_english = any(w in (title + desc).lower() for w in 
                     ['the ', 'and ', 'for ', 'with ', 'writing', 'content', 'article'])
    
    prompt = (
        f"Напиши профессиональный отклик на фриланс-заказ.\n\n"
        f"ЗАКАЗ: {title}\n"
        f"ОПИСАНИЕ: {desc}\n"
        f"БЮДЖЕТ: {budget}\n"
        f"ПЛАТФОРМА: {source}\n\n"
        f"Требования:\n"
        f"1. Начни с понимания задачи — 1 предложение о сути заказа\n"
        f"2. Почему мы подходим — конкретно, 1-2 предложения\n"
        f"3. Мини-план или уточняющий вопрос\n"
        f"4. Сроки и условия\n"
        f"5. Призыв к действию\n\n"
        f"Тон: профессиональный, живой, НЕ шаблонный.\n"
        f"Длина: 80-120 слов.\n"
        f"Язык: {'английский' if is_english else 'русский'}.\n"
        f"НЕ начинай с 'Здравствуйте' или 'Добрый день' или 'Hello'."
    )
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": ANTHROPIC_HAIKU,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            return r.json()["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"generate_smart_proposal ошибка: {e}")
        return ""

# ═══ ПАРСЕРЫ ═══

async def parse_rss(client) -> list:
    jobs = []
    filtered_count = 0
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

                if not is_relevant(title, desc):
                    continue

                ai_rejected, ai_phrase = is_ai_rejection(title, desc)
                if ai_rejected:
                    logger.info(f"🚫 Пропускаем (запрет AI): {title[:50]}")
                    save_filtered_job(title, link, f"запрет AI: {ai_phrase}")
                    mark_seen(link)
                    filtered_count += 1
                    continue

                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc + title)
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
            logger.error(f"❌ {source}: {e}")

    jobs.sort(key=lambda x: x.get('priority', 5), reverse=True)
    logger.info(f"📋 Полифан нашёл: {len(jobs)} заказов (отфильтровано AI-запретов: {filtered_count})")
    return jobs

# ═══ ОТПРАВКА ЛИЛЕ ═══

async def send_to_lilu(bot, job: dict):
    """Генерирует proposal и записывает заказ в БД как pending_lilu"""
    try:
        # Генерируем умный proposal
        proposal = await generate_smart_proposal(job)
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        source = f"Полифан | {job.get('source', '')}"
        c.execute(
            "UPDATE jobs SET status='pending_lilu', source=?, result=?, updated_at=? WHERE id=?",
            (source[:200], proposal[:2000] if proposal else "", datetime.now().isoformat(), job['id'])
        )
        conn.commit()
        conn.close()
        logger.info(f"📨 Полифан → БД (pending_lilu + proposal): {job.get('title','')[:50]}")
    except Exception as e:
        logger.error(f"❌ Ошибка send_to_lilu: {e}")

async def scan_and_send(bot) -> int:
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_rss(client)
    for job in jobs:
        save_job(job)
        await send_to_lilu(bot, job)
        count += 1
        await asyncio.sleep(2)
    return count

# ═══ ВЫПОЛНЕНИЕ ЗАКАЗА ═══

async def execute_job(job: dict) -> str:
    """Выполняет заказ с учётом типа задачи"""
    title = job.get('title', '').lower()
    desc  = job.get('description', '').lower()
    text  = title + " " + desc

    # Определяем тип
    if any(kw in text for kw in ['перевод', 'перевести', 'translation', 'translate']):
        instruction = (
            "Выполни перевод текста профессионально. "
            "Сохрани структуру, тон и смысл оригинала. "
            "Если оригинал на русском — переведи на английский. Если на английском — на русский."
        )
    elif any(kw in text for kw in ['email', 'рассылка', 'письмо клиент']):
        instruction = (
            "Напиши профессиональное email-письмо. "
            "Структура: тема, приветствие, основной текст, призыв к действию, подпись. "
            "Тон: деловой но живой."
        )
    elif any(kw in text for kw in ['пост', 'instagram', 'telegram', 'соцсет', 'smm']):
        instruction = (
            "Напиши продающий пост для соцсетей. "
            "Структура: крючок внимания → ценность → призыв к действию. "
            "Добавь релевантные хэштеги."
        )
    elif any(kw in text for kw in ['лендинг', 'сайт', 'about', 'landing']):
        instruction = (
            "Напиши продающий текст для лендинга. "
            "Структура: заголовок (USP) → проблема → решение → выгоды → призыв. "
            "Фокус на пользе для клиента."
        )
    else:
        instruction = (
            "Выполни задачу профессионально. "
            "Пиши конкретно, без воды. Результат должен быть готов к использованию."
        )

    prompt = (
        f"Задача: {instruction}\n\n"
        f"ЗАКАЗ: {job['title']}\n"
        f"ОПИСАНИЕ: {job['description'][:800]}\n\n"
        f"Напиши качественный результат на языке заказа."
    )

    if ANTHROPIC_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": ANTHROPIC_HAIKU,
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                )
                return r.json()["content"][0]["text"].strip()
        except Exception as e:
            logger.error(f"Anthropic execute_job: {e}")

    # Fallback на Groq
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

# ═══ АВТООТКЛИК FL.RU ═══

async def fl_apply(job_url: str, proposal: str) -> bool:
    if not FL_PHPSESSID or not FL_XSRF_TOKEN:
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
        logger.error(f"FL автоотклик: {e}")
        return False

# ═══ НОВЫЕ КОМАНДЫ ═══

async def proposal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует кастомный отклик на заказ"""
    if not context.args:
        context.user_data['making_proposal'] = True
        await update.message.reply_text(
            "📝 *Режим написания отклика*\n\n"
            "Отправь описание заказа — напишу кастомный proposal!\n\n"
            "Или: `/proposal Нужен копирайтер для описаний WB`",
            parse_mode='Markdown'
        )
        return
    job_desc = " ".join(context.args)
    await update.message.reply_text("📝 Пишу отклик...")
    fake_job = {"title": job_desc, "description": job_desc, "budget": "", "source": "FL.ru"}
    proposal = await generate_smart_proposal(fake_job)
    if proposal:
        await update.message.reply_text(f"📝 *ОТКЛИК ГОТОВ:*\n\n{proposal}", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Ошибка генерации. Проверь ANTHROPIC_API_KEY")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переводит текст"""
    if not context.args:
        await update.message.reply_text(
            "🌍 Использование:\n"
            "`/translate Hello, I need help with my project`\n\n"
            "Автоматически определю язык и переведу!\n"
            "RU→EN, EN→RU, DE→RU и т.д.",
            parse_mode='Markdown'
        )
        return
    text_to_translate = " ".join(context.args)
    await update.message.reply_text("🌍 Перевожу...")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": ANTHROPIC_HAIKU,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content":
                        f"Определи язык текста и переведи: если русский — на английский, "
                        f"если английский — на русский, иначе на русский.\n\n"
                        f"Текст: {text_to_translate}\n\nВерни ТОЛЬКО перевод без пояснений."}]
                }
            )
            translation = r.json()["content"][0]["text"].strip()
        await update.message.reply_text(f"🌍 *ПЕРЕВОД:*\n\n{translation}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")


async def copywrite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пишет текст по заданию"""
    if not context.args:
        await update.message.reply_text(
            "✍️ Использование:\n"
            "`/copywrite пост для Instagram про кофе, молодёжная аудитория`\n"
            "`/copywrite email рассылка для магазина одежды`\n"
            "`/copywrite текст для лендинга — курсы английского`\n\n"
            "Укажи тему и формат — напишу сразу!",
            parse_mode='Markdown'
        )
        return
    task = " ".join(context.args)
    await update.message.reply_text("✍️ Пишу текст...")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": ANTHROPIC_HAIKU,
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content":
                        f"Напиши профессиональный текст. "
                        f"Задание: {task}\n\n"
                        f"Готовый текст без пояснений и вступлений:"}]
                }
            )
            result = r.json()["content"][0]["text"].strip()
        await update.message.reply_text(f"✍️ *ТЕКСТ ГОТОВ:*\n\n{result}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

# ═══ КНОПКИ ═══

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "team_skills":
        await query.edit_message_text(
            "🤖 *ПОЛИФАН — ЧТО УМЕЮ*\n\n"
            "✍️ Тексты и статьи (EN/RU/DE)\n"
            "📝 Копирайтинг и рерайтинг\n"
            "🌍 Переводы EN↔RU, DE, FR\n"
            "🛍️ Описания товаров для маркетплейсов\n"
            "📱 Посты для соцсетей (Instagram, TG, VK)\n"
            "📧 Email-рассылки и письма клиентам\n"
            "🌐 Тексты для лендингов и сайтов\n"
            "✅ Корректура и редактура\n"
            "📋 Кастомные proposals под каждый заказ\n\n"
            "🚫 *Автофильтр:* заказы с запретом AI пропускаем!\n\n"
            "🔍 Источники:\n"
            "• FL.ru (RSS) • RemoteOK\n"
            "• Jobicy • WWR\n\n"
            "📤 Все заказы идут через *Лилу* — она фильтрует!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )

    elif data == "kwork_menu":
        await query.edit_message_text(
            "🛍️ *НАШИ КВОРКИ НА KWORK*\n\n"
            "✍️ *Тексты и копирайтинг:*\n"
            " • Статья/блог-пост: от 500₽\n"
            " • Описание для сайта: от 400₽\n"
            " • Email-рассылка: от 400₽\n"
            " • Пост для соцсетей: от 300₽\n\n"
            "🌍 *Переводы:*\n"
            " • Перевод EN↔RU: от 300₽\n"
            " • Деловой перевод: от 500₽\n\n"
            "📦 *Карточки WB/Ozon/ЯМ:*\n"
            " • Эконом: 400₽\n • Стандарт: 1200₽\n • Бизнес: 2000₽\n\n"
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

    elif data == "do_scan":
        await query.edit_message_text("🔍 Ищу заказы и отправляю Лиле...")
        try:
            count = await scan_and_send(query.get_bot())
            stats = get_stats()
            filtered = stats.get('filtered_ai', 0)
            await query.edit_message_text(
                f"✅ Нашёл и отправил Лиле: *{count}* заказов\n"
                f"🚫 Отфильтровано (запрет AI): *{filtered}*\n\n"
                f"📝 Proposals сгенерированы автоматически!\n"
                f"Лила анализирует — лучшие придут тебе!",
                parse_mode='Markdown',
                reply_markup=_main_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:100]}", reply_markup=_main_keyboard())

    elif data == "do_stats":
        stats = get_stats()
        await query.edit_message_text(
            f"📊 *Статистика Полифана*\n\n"
            f"🔍 Найдено: {stats.get('found', 0)}\n"
            f"✅ Принято: {stats.get('accepted', 0)}\n"
            f"✨ Выполнено: {stats.get('completed', 0)}\n"
            f"💰 Закрыто: {stats.get('done', 0)}\n"
            f"⏭ Пропущено: {stats.get('skipped', 0)}\n"
            f"🚫 Запрет AI: {stats.get('filtered_ai', 0)}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back_main")
            ]])
        )

    elif data.startswith("done_"):
        update_job(data[5:], 'done')
        await query.edit_message_text("💰 Заказ закрыт! Молодцы 🎉")

    elif data.startswith("redo_"):
        job_id = data[5:]
        context.user_data['redo_job_id'] = job_id
        job = get_job(job_id)
        context.user_data['redo_result'] = job.get('result', '') if job else ''
        await query.edit_message_text("✏️ *Напиши что исправить:*", parse_mode='Markdown')

# ═══ ГЛАВНОЕ МЕНЮ ═══

def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Что умею",      callback_data="team_skills"),
         InlineKeyboardButton("🛍️ Наши кворки",  callback_data="kwork_menu")],
        [InlineKeyboardButton("🔍 Найти заказы", callback_data="do_scan"),
         InlineKeyboardButton("📊 Статистика",   callback_data="do_stats")],
    ])

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Привет! Я Полифан — фриланс-помощник!*\n\n"
        "Ищу заказы на текст/контент/переводы.\n"
        "Все заказы сначала проверяет *Лила*.\n\n"
        "🚫 *Автофильтр:* заказы с запретом AI пропускаем!\n"
        "📝 *Proposals:* генерирую кастомно под каждый заказ!\n\n"
        "Команды:\n"
        "/scan — найти заказы\n"
        "/proposal — написать отклик на заказ\n"
        "/translate — перевести текст\n"
        "/copywrite — написать текст\n"
        "/stats — статистика\n"
        "/skills — что умею\n"
        "/clear — очистить кэш",
        parse_mode='Markdown',
        reply_markup=_main_keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Ищу заказы и отправляю Лиле...")
    count = await scan_and_send(context.application.bot)
    stats = get_stats()
    filtered = stats.get('filtered_ai', 0)
    await msg.edit_text(
        f"✅ Нашёл и отправил Лиле: *{count}* заказов\n"
        f"🚫 Отфильтровано (запрет AI): *{filtered}* всего\n"
        f"📝 Proposals сгенерированы автоматически!\n\n"
        f"Лила анализирует — лучшие придут тебе!",
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
        f"⏭ Пропущено: {stats.get('skipped', 0)}\n"
        f"🚫 Запрет AI (пропущено): {stats.get('filtered_ai', 0)}",
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
    # Режим proposal
    if context.user_data.get('making_proposal'):
        context.user_data.pop('making_proposal', None)
        job_desc = update.message.text
        await update.message.reply_text("📝 Пишу отклик...")
        fake_job = {"title": job_desc, "description": job_desc, "budget": "", "source": "FL.ru"}
        proposal = await generate_smart_proposal(fake_job)
        if proposal:
            await update.message.reply_text(f"📝 *ОТКЛИК ГОТОВ:*\n\n{proposal}", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Ошибка генерации")
        return

    # Режим правки
    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            if ANTHROPIC_API_KEY:
                async with httpx.AsyncClient(timeout=60) as client:
                    r = await client.post(
                        ANTHROPIC_URL,
                        headers={
                            "x-api-key": ANTHROPIC_API_KEY,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json"
                        },
                        json={
                            "model": ANTHROPIC_HAIKU,
                            "max_tokens": 2000,
                            "messages": [{"role": "user", "content":
                                f"Исправь текст.\n\nОРИГИНАЛ:\n{original[:2000]}\n\n"
                                f"ИНСТРУКЦИЯ: {fix}\n\nВерни исправленный текст полностью."}]
                        }
                    )
                    new_result = r.json()["content"][0]["text"].strip()
            else:
                async with httpx.AsyncClient(timeout=60) as client:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "llama-3.3-70b-versatile",
                              "messages": [{"role": "user", "content":
                                  f"Исправь текст.\n\nОРИГИНАЛ:\n{original[:2000]}\n\nИНСТРУКЦИЯ: {fix}"}],
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
        "Используй команды или кнопки меню 👇\n\n"
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
            filtered = stats.get('filtered_ai', 0)
            logger.info(f"✅ Полифан → Лила: {count} заказов (AI-запретов: {filtered})")
            if count > 0 and YOUR_CHAT_ID:
                await bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔍 *Полифан нашёл {count} заказов* — отправил Лиле!\n"
                         f"📝 Proposals сгенерированы автоматически",
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"❌ Автосканирование: {e}")
        await asyncio.sleep(1800)

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",      start_command))
    app.add_handler(CommandHandler("scan",       scan_command))
    app.add_handler(CommandHandler("stats",      stats_command))
    app.add_handler(CommandHandler("clear",      clear_command))
    app.add_handler(CommandHandler("skills",     skills_command))
    app.add_handler(CommandHandler("kwork",      kwork_command))
    app.add_handler(CommandHandler("proposal",   proposal_command))
    app.add_handler(CommandHandler("translate",  translate_command))
    app.add_handler(CommandHandler("copywrite",  copywrite_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(application):
        asyncio.create_task(auto_scan_loop(application.bot))
        logger.info("✅ Автосканирование запущено")
        try:
            if YOUR_CHAT_ID:
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        "🤖 *Полифан v2.0 запущен!*\n\n"
                        "✅ Автосканирование каждые 30 мин\n"
                        "🚫 Автофильтр: запрет AI пропускаем\n"
                        "📝 Proposals: генерирую кастомно!\n"
                        "📨 Заказы идут через Лилу\n\n"
                        "Новые команды:\n"
                        "/proposal — написать отклик\n"
                        "/translate — перевести текст\n"
                        "/copywrite — написать текст"
                    ),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init
    logger.info("🤖 Полифан v2.0 запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
