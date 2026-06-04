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

GROQ_URL        = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL      = "llama-3.3-70b-versatile"

# ═══ RSS ИСТОЧНИКИ — РАСШИРЕННЫЕ ═══
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

# ═══ КЛЮЧЕВЫЕ СЛОВА — ВСЕ ЯЗЫКИ ═══
KEYWORDS = [
    # Тексты и контент
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент", "рерайтинг",
    "блог", "blog post", "article", "content writing",
    "статья", "тексты для", "наполнение сайта",
    "продающий текст", "рекламный текст",
    # Переводы — ВСЕ ЯЗЫКИ МИРА
    "перевод", "перевести", "translation", "translate", "переводчик",
    "перевод текста", "перевод с английского", "перевод на английский",
    "перевод немецкий", "перевод французский", "перевод испанский",
    "перевод итальянский", "перевод китайский", "перевод японский",
    "перевод корейский", "перевод арабский", "перевод турецкий",
    "перевод португальский", "перевод польский", "перевод чешский",
    "перевод нидерландский", "перевод шведский", "перевод норвежский",
    "перевод финский", "перевод венгерский", "перевод румынский",
    "перевод греческий", "перевод иврит", "перевод хинди",
    "перевод индонезийский", "перевод вьетнамский", "перевод тайский",
    "перевод малайский", "перевод тагальский", "перевод суахили",
    "технический перевод", "деловой перевод", "юридический перевод",
    "медицинский перевод", "перевод договора", "перевод документа",
    "any language", "multilingual", "localization", "локализация",
    # Редактура
    "редактура", "корректура", "proofreading", "editing", "редактировать",
    # Email и рассылки
    "email рассылка", "email маркетинг", "письмо клиентам",
    "welcome письмо", "email копирайтинг", "newsletter",
    # Лендинги и сайты
    "текст для лендинга", "тексты для сайта", "landing page",
    "about us", "about page", "текст для страницы",
    # Соцсети
    "посты инстаграм", "контент telegram", "smm копирайтинг",
    "посты соцсетей", "контент план", "сценарий reels",
    "сценарий tiktok", "подписи к фото",
    # Маркетплейсы
    "карточка товара", "описание товара", "маркетплейс",
    "wildberries", "wb", "ozon", "озон", "яндекс маркет",
    "product description", "amazon listing", "etsy listing",
    # Презентации и документы
    "текст презентации", "написать презентацию", "текст для слайдов",
    "бизнес план текст", "коммерческое предложение",
    # Proposals и отклики
    "написать proposal", "сопроводительное письмо", "cover letter",
]

BLACKLIST = [
    "программирование", "разработка сайта", "верстка",
    "дизайн логотип с нуля", "видеомонтаж", "3d анимация",
    "мобильное приложение", "android", "ios",
    "чертёж", "autocad", "курсовая", "дипломная",
    "купить и доставить", "курьер", "доставить",
    "оформить ленту", "визуал аккаунта", "дизайн аккаунта",
]

AI_REJECTION_PHRASES = [
    "без нейросетей", "без ии", "без ai", "no ai",
    "только вручную", "исключительно вручную",
    "ai не принимается", "нейросети не принимаются",
    "без использования ии", "без использования нейросетей",
]

PRIORITY_BOOST = [
    "карточка товара", "wildberries", "wb", "ozon", "яндекс маркет",
    "срочно", "быстро", "копирайтер нужен", "ищу копирайтера",
    "перевод срочно", "нужен переводчик",
    "email копирайтер", "контент план",
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
        amount_usd REAL DEFAULT 0, amount_rub REAL DEFAULT 0,
        description TEXT, created_at TEXT
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

# ═══ GROQ ХЕЛПЕР ═══

async def groq_request(messages, system="", model=GROQ_MODEL, max_tokens=800):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": msgs, "max_tokens": max_tokens}
        )
        data = r.json()
        if "choices" not in data:
            raise Exception(f"Groq error: {data}")
        return data["choices"][0]["message"]["content"].strip()

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
            logger.error(f"❌ {source}: {e}")

    jobs.sort(key=lambda x: x.get('priority', 5), reverse=True)
    logger.info(f"📋 Полифан нашёл: {len(jobs)} (AI-запретов: {filtered_count})")
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

# ═══ ВЫПОЛНЕНИЕ ЗАКАЗА — GROQ ═══

async def execute_job(job: dict) -> str:
    title = job.get('title', '').lower()
    desc  = job.get('description', '').lower()
    text  = title + " " + desc

    if any(kw in text for kw in ['перевод','перевести','translation','translate']):
        instruction = ("Выполни профессиональный перевод. Сохрани структуру и тон оригинала. "
                       "Если русский — переведи на английский. Если английский — на русский. "
                       "Другой язык — переведи на русский.")
    elif any(kw in text for kw in ['email','рассылка','newsletter']):
        instruction = ("Напиши профессиональное email письмо. "
                       "Структура: тема, приветствие, текст, призыв, подпись. Деловой но живой тон.")
    elif any(kw in text for kw in ['пост','instagram','telegram','socseti','smm','reels','tiktok']):
        instruction = ("Напиши продающий пост для соцсетей. "
                       "Крючок внимания → ценность → призыв к действию. Добавь хэштеги.")
    elif any(kw in text for kw in ['лендинг','landing','сайт','about']):
        instruction = ("Напиши продающий текст для лендинга. "
                       "USP → проблема → решение → выгоды → призыв. Фокус на пользе.")
    elif any(kw in text for kw in ['презентация','слайды','presentation']):
        instruction = ("Напиши текст для презентации. "
                       "Структура: титул, введение, основные тезисы (3-5), выводы, призыв.")
    else:
        instruction = "Выполни задачу профессионально. Конкретно, без воды. Готово к использованию."

    prompt = (f"Задача: {instruction}\n\n"
              f"ЗАКАЗ: {job['title']}\nОПИСАНИЕ: {job['description'][:800]}\n\n"
              f"Напиши качественный результат на языке заказа.")

    return await groq_request(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000
    )

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Полифан v3.0*\n\n"
        "Ищу заказы, пишу тексты и переводы на ЛЮБОЙ язык!\n\n"
        "🌍 *Переводы:* русский, английский, немецкий,\n"
        "французский, китайский, японский, арабский и все остальные!\n\n"
        "📝 *Proposals:* генерирую кастомно под каждый заказ\n"
        "🚫 *Автофильтр:* запрет AI пропускаем\n\n"
        "/scan — найти заказы\n"
        "/proposal — написать отклик\n"
        "/translate — перевести текст\n"
        "/copywrite — написать текст\n"
        "/stats — статистика\n"
        "/clear — очистить кэш",
        parse_mode='Markdown',
        reply_markup=_main_keyboard()
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🔍 Ищу заказы...")
    count = await scan_and_send(context.application.bot)
    stats = get_stats()
    await msg.edit_text(
        f"✅ Нашёл и отправил Лиле: *{count}* заказов\n"
        f"🚫 Отфильтровано (AI запрет): *{stats.get('filtered_ai',0)}*\n"
        f"📝 Proposals сгенерированы автоматически!",
        parse_mode='Markdown'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    await update.message.reply_text(
        f"📊 *Статистика Полифана*\n\n"
        f"🔍 Найдено: {stats.get('found',0)}\n"
        f"✅ Принято: {stats.get('accepted',0)}\n"
        f"✨ Выполнено: {stats.get('completed',0)}\n"
        f"💰 Закрыто: {stats.get('done',0)}\n"
        f"⏭ Пропущено: {stats.get('skipped',0)}\n"
        f"🚫 Запрет AI: {stats.get('filtered_ai',0)}",
        parse_mode='Markdown'
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
        await update.message.reply_text(
            "📝 Отправь описание заказа — напишу кастомный отклик!",
            parse_mode='Markdown'
        )
        return
    job_desc = " ".join(context.args)
    await update.message.reply_text("📝 Пишу отклик...")
    fake_job = {"title": job_desc, "description": job_desc, "budget": "", "source": "FL.ru"}
    proposal = await generate_smart_proposal(fake_job)
    if proposal:
        await update.message.reply_text(f"📝 *ОТКЛИК:*\n\n{proposal}", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Ошибка генерации")

async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🌍 Использование:\n`/translate текст для перевода`\n\n"
            "Автоматически определю язык и переведу!\n"
            "RU→EN, EN→RU, любой→RU и т.д.",
            parse_mode='Markdown'
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
        await update.message.reply_text(f"🌍 *ПЕРЕВОД:*\n\n{translation}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

async def copywrite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "✍️ Использование:\n"
            "`/copywrite пост Instagram про кофе`\n"
            "`/copywrite email рассылка магазин одежды`\n"
            "`/copywrite текст лендинга курсы английского`",
            parse_mode='Markdown'
        )
        return
    task = " ".join(context.args)
    await update.message.reply_text("✍️ Пишу текст...")
    try:
        result = await groq_request(
            messages=[{"role": "user", "content":
                f"Напиши профессиональный текст.\nЗадание: {task}\n\n"
                f"Готовый текст без вступлений:"}],
            max_tokens=800
        )
        await update.message.reply_text(f"✍️ *ТЕКСТ:*\n\n{result}", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Полифан v3.0 — что умею:*\n\n"
        "✍️ Тексты и статьи (RU/EN/DE/FR)\n"
        "🌍 Переводы на ВСЕ языки мира\n"
        "📱 Посты соцсетей, сценарии Reels/TikTok\n"
        "📧 Email рассылки и письма\n"
        "🌐 Тексты лендингов и сайтов\n"
        "📊 Тексты презентаций\n"
        "🛍️ Описания товаров для маркетплейсов\n"
        "✅ Корректура и редактура\n"
        "📋 Proposals кастомно под каждый заказ\n\n"
        "🔍 Ищу на:\nFL.ru • Habr Freelance • RemoteOK • Jobicy • WWR",
        parse_mode='Markdown'
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
            "🤖 *Полифан v3.0*\n\n"
            "🌍 Переводы на ВСЕ языки\n"
            "✍️ Тексты, статьи, копирайтинг\n"
            "📱 Соцсети, email, лендинги\n"
            "📋 Кастомные proposals\n\n"
            "Источники: FL.ru + Habr + RemoteOK + WWR",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])
        )
    elif data == "kwork_menu":
        await query.edit_message_text(
            f"🛍️ *Наши кворки:*\n\n"
            f"✍️ Статья: от 500₽\n"
            f"🌍 Перевод: от 300₽\n"
            f"📧 Email: от 400₽\n"
            f"📦 Карточки WB/Ozon: от 400₽\n\n"
            f"[Kwork]({KWORK_URL})",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Открыть", url=KWORK_URL)],
                [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
            ])
        )
    elif data == "back_main":
        await query.edit_message_text(
            "🤖 *Полифан* — чем могу помочь?",
            parse_mode='Markdown',
            reply_markup=_main_keyboard()
        )
    elif data == "do_scan":
        await query.edit_message_text("🔍 Ищу заказы...")
        try:
            count = await scan_and_send(query.get_bot())
            stats = get_stats()
            await query.edit_message_text(
                f"✅ Нашёл: *{count}* заказов → отправил Лиле\n"
                f"🚫 AI запрет: *{stats.get('filtered_ai',0)}*",
                parse_mode='Markdown',
                reply_markup=_main_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}", reply_markup=_main_keyboard())
    elif data == "do_stats":
        stats = get_stats()
        await query.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"🔍 Найдено: {stats.get('found',0)}\n"
            f"✅ Принято: {stats.get('accepted',0)}\n"
            f"💰 Закрыто: {stats.get('done',0)}\n"
            f"🚫 AI запрет: {stats.get('filtered_ai',0)}",
            parse_mode='Markdown',
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
            await update.message.reply_text(f"📝 *ОТКЛИК:*\n\n{proposal}", parse_mode='Markdown')
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
            await update.message.reply_text(
                f"✨ *Исправлено!*\n\n{new_result[:2500]}",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await update.message.reply_text(f"❌ {str(e)[:100]}")
        return

    await update.message.reply_text(
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
                await bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔍 *Полифан нашёл {count} заказов* → отправил Лиле!\n"
                         f"📝 Proposals сгенерированы",
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"auto_scan: {e}")
        await asyncio.sleep(1800)

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
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        "🤖 *Полифан v3.0 запущен!*\n\n"
                        "✅ Groq — бесплатно\n"
                        "🌍 Переводы на ВСЕ языки мира\n"
                        "🖥 + Habr Freelance добавлен\n"
                        "📝 Proposals кастомные\n"
                        "🚫 Автофильтр AI запретов\n\n"
                        "/proposal /translate /copywrite"
                    ),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init
    logger.info("🤖 Полифан v3.0!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
