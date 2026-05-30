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
LILU_CHAT_ID = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")

# FL.ru авторизация
FL_PHPSESSID = os.getenv("FL_PHPSESSID", "")
FL_XSRF_TOKEN = os.getenv("FL_XSRF_TOKEN", "")

FL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-XSRF-TOKEN": FL_XSRF_TOKEN,
    "Referer": "https://www.fl.ru/",
    "Origin": "https://www.fl.ru",
}

FL_COOKIES = {
    "PHPSESSID": FL_PHPSESSID,
    "XSRF-TOKEN": FL_XSRF_TOKEN,
}

# ═══ FL.RU АВТООТКЛИК ═══
async def fl_apply_to_job(job_url: str, proposal_text: str) -> bool:
    """Подаёт отклик на заказ FL.ru"""
    if not FL_PHPSESSID:
        logger.info("FL.ru токены не настроены — пропускаем автоотклик")
        return False
    
    try:
        # Извлекаем ID заказа из URL
        # URL вида: https://www.fl.ru/projects/12345/...
        job_id_match = re.search(r'/projects/(\d+)/', job_url)
        if not job_id_match:
            logger.error(f"Не удалось извлечь ID заказа из {job_url}")
            return False
        
        job_id = job_id_match.group(1)
        
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Отправляем отклик через API FL.ru
            r = await client.post(
                f"https://www.fl.ru/api/project/{job_id}/bid/",
                headers=FL_HEADERS,
                cookies=FL_COOKIES,
                json={
                    "description": proposal_text,
                    "cost": "",
                    "term": "1",
                    "term_type": "day"
                }
            )
            
            logger.info(f"FL.ru отклик статус: {r.status_code}")
            
            if r.status_code in [200, 201]:
                logger.info(f"✅ Отклик успешно отправлен на заказ {job_id}")
                return True
            else:
                logger.error(f"❌ Ошибка отклика FL.ru: {r.status_code} {r.text[:200]}")
                return False
                
    except Exception as e:
        logger.error(f"Ошибка FL.ru автоотклика: {e}")
        return False

async def fl_send_result(job_url: str, result_text: str) -> bool:
    """Отправляет готовую работу клиенту через FL.ru"""
    if not FL_PHPSESSID:
        return False
    
    try:
        job_id_match = re.search(r'/projects/(\d+)/', job_url)
        if not job_id_match:
            return False
        
        job_id = job_id_match.group(1)
        
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Находим ID чата с заказчиком
            r = await client.get(
                f"https://www.fl.ru/api/project/{job_id}/",
                headers=FL_HEADERS,
                cookies=FL_COOKIES
            )
            
            if r.status_code == 200:
                data = r.json()
                # Отправляем сообщение в чат заказа
                chat_r = await client.post(
                    f"https://www.fl.ru/api/project/{job_id}/message/",
                    headers=FL_HEADERS,
                    cookies=FL_COOKIES,
                    json={"message": result_text}
                )
                logger.info(f"FL.ru отправка результата: {chat_r.status_code}")
                return chat_r.status_code in [200, 201]
    except Exception as e:
        logger.error(f"Ошибка отправки результата FL.ru: {e}")
        return False

async def fl_close_job(job_url: str) -> bool:
    """Закрывает заказ как выполненный"""
    if not FL_PHPSESSID:
        return False
    
    try:
        job_id_match = re.search(r'/projects/(\d+)/', job_url)
        if not job_id_match:
            return False
        
        job_id = job_id_match.group(1)
        
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.post(
                f"https://www.fl.ru/api/project/{job_id}/complete/",
                headers=FL_HEADERS,
                cookies=FL_COOKIES,
                json={"status": "complete"}
            )
            logger.info(f"FL.ru закрытие заказа: {r.status_code}")
            return r.status_code in [200, 201]
    except Exception as e:
        logger.error(f"Ошибка закрытия FL.ru: {e}")
        return False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ═══ РОССИЙСКИЕ БИРЖИ ═══
RU_RSS_FEEDS = [
    # FL.ru — работает ✅
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=2", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://www.fl.ru/rss/all.xml?category=19", "🇷🇺 FL.ru/Данные"),
]

# ═══ МЕЖДУНАРОДНЫЕ БИРЖИ ═══
EN_RSS_FEEDS = [
    ("https://www.peopleperhour.com/jobs/rss", "🔵 PeoplePerHour"),
    ("https://www.peopleperhour.com/jobs/rss?service=writing", "🔵 PPH/Writing"),
    ("https://www.peopleperhour.com/jobs/rss?service=translation", "🔵 PPH/Translation"),
]

async def parse_guru_direct(client) -> list:
    """Парсим Guru.com через правильные URL"""
    jobs = []
    urls = [
        "https://www.guru.com/d/jobs/?skill=data-entry",
        "https://www.guru.com/d/jobs/?skill=translation",
        "https://www.guru.com/d/jobs/?skill=writing",
        "https://www.guru.com/d/jobs/?skill=research",
        "https://www.guru.com/d/jobs/?skill=transcription",
        "https://www.guru.com/d/jobs/?skill=copywriting",
    ]
    for url in urls:
        try:
            r = await client.get(url, headers=HEADERS)
            logger.info(f"Guru URL {url[-30:]}: {r.status_code}")
            if r.status_code != 200:
                continue
            
            # Ищем заказы в HTML по разным паттернам
            # Паттерн 1: ссылки на заказы
            links = re.findall(r'href="(/d/jobs/[^"]+)"', r.text)
            titles_raw = re.findall(r'<h2[^>]*class="[^"]*jobRecord[^"]*"[^>]*>(.*?)</h2>', r.text, re.DOTALL)
            if not titles_raw:
                titles_raw = re.findall(r'class="jobTitle[^"]*"[^>]*>(.*?)</[^>]+>', r.text, re.DOTALL)
            
            for i, link in enumerate(links[:5]):
                full_url = f"https://www.guru.com{link}"
                if is_seen(full_url):
                    continue
                title = clean_html(titles_raw[i]) if i < len(titles_raw) else link.split('/')[-1].replace('-', ' ')
                if not title or len(title) < 5:
                    continue
                jobs.append({
                    'id': make_id(full_url), 'title': title[:200],
                    'description': title, 'budget': "По договорённости",
                    'url': full_url, 'source': '🟠 Guru.com',
                    'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(full_url)
        except Exception as e:
            logger.error(f"❌ Guru.com {url[-20:]}: {e}")
    
    logger.info(f"Guru.com итого: {len(jobs)}")
    return jobs

# ═══ TELEGRAM КАНАЛЫ С ЗАКАЗАМИ ═══
TG_CHANNELS = [
    "freelance_ru",
    "freelancehunt_ru",
    "it_freelance_ru",
    "kopiraiting_ru",
    "freelance_project_ru",
]

# ═══ ФИЛЬТРЫ ═══
# ═══ ФИЛЬТРЫ ═══
WHITELIST = [
    # 📝 Тексты и контент
    "написать статью", "написать текст", "написать пост", "написать описание",
    "статья", "текст", "контент", "копирайтинг", "копирайтер",
    "рерайт", "рерайтинг", "редактура", "корректура", "вычитка",
    "продающий текст", "лендинг текст", "описание товара", "описание продукта",
    "пост для", "посты для", "тексты для", "наполнение сайта",
    "сценарий", "скрипт продаж", "скрипт для", "faq", "ответы на вопросы",
    "коммерческое предложение", "кп ", "деловое письмо", "бизнес письмо",
    "резюме", "сопроводительное письмо", "пресс-релиз", "анонс",

    # 🌐 Переводы
    "перевод", "перевести", "перевести текст", "translation",
    "translate", "переводчик", "технический перевод", "субтитры",
    "локализация", "localization", "english to russian", "russian to english",

    # 📊 Данные и таблицы
    "ввод данных", "data entry", "copy paste", "копипаст",
    "заполнить таблицу", "заполнить excel", "заполнить google",
    "excel", "google sheets", "гугл таблицы", "spreadsheet",
    "csv", "база данных заполнить", "внести данные",
    "собрать данные", "сбор данных", "сбор информации",
    "парсинг", "парсить", "выгрузить данные", "обработать данные",
    "структурировать данные", "привести в порядок",

    # 🔍 Исследования
    "исследование", "анализ конкурентов", "анализ рынка",
    "подбор ключевых слов", "ключевые слова", "семантика",
    "сбор контактов", "найти контакты", "найти email",
    "мониторинг", "обзор рынка", "бенчмаркинг",
    "research", "internet research", "web research",
    "найти информацию", "собрать информацию",

    # 📋 Документы и деловые тексты
    "коммерческое предложение", "техническое задание", "тз ",
    "инструкция", "регламент", "договор написать", "оферта",
    "бриф", "заполнить бриф", "анкета", "опросник",
    "отчёт написать", "презентация текст", "питч",

    # 🤖 Автоматизация и AI задачи
    "чат-бот скрипт", "бот сценарий", "диалоги для бота",
    "промпт", "prompt", "нейросеть", "ai текст",
    "автоматизация", "шаблон письма", "email шаблон",

    # 📱 SMM контент
    "контент-план", "контентный план", "контент план",
    "посты instagram", "посты вконтакте", "посты telegram",
    "stories текст", "reels описание", "хэштеги",
    "smm текст", "caption", "подпись к фото",

    # 🛍️ Маркетплейсы
    "wildberries", "вайлдберриз", "wb ", "ozon", "озон",
    "яндекс маркет", "маркетплейс", "карточка товара",
    "seo описание", "rich контент", "характеристики товара",
    "amazon listing", "etsy listing", "product description",

    # 💻 Простой код
    "простой скрипт", "python скрипт", "автоматизация excel",
    "макрос excel", "google apps script", "формулы excel",
    "обработать файл", "конвертировать файл",
    "pdf в word", "word в pdf", "конвертация",

    # 🌍 Английские универсальные
    "article writing", "blog post", "blog writing",
    "content writing", "copywriting", "rewrite", "proofreading",
    "data collection", "data mining", "web scraping",
    "virtual assistant", "va task", "admin task",
    "summarize", "summary", "categorize", "simple task",
    "quick task", "easy task", "small task",
    "transcription", "document formatting",
]

BLACKLIST = [
    # Разработка
    "developer", "программист", "разработчик", "development", "coding",
    "react", "angular", "node", "python developer", "django", "flask",
    "mobile app", "android", "ios", "web app", "backend", "frontend",
    "wordpress developer", "shopify developer", "api integration",
    "верстка", "вёрстка", "верстальщик", "html", "css", "javascript",
    "сайт под ключ", "создание сайта", "разработка сайта",
    "чертёж", "чертеж", "autocad", "solidworks", "sketchup", "конструктор",
    "3д модел", "проектирование", "архитектур",
    # Отзывы и накрутка
    "написать отзыв", "публикация отзыва", "отзыв на картах",
    "отзыв google", "отзыв яндекс", "накрутка", "накрутить",
    "фейк отзыв", "review writing", "fake review",
    "лайки", "подписчики", "накрутка подписчиков",
    # Видео/Аудио
    "video edit", "видеомонтаж", "монтаж видео", "animation", "анимация",
    "after effects", "premiere", "motion", "3d", "render",
    "audio mixing", "звукозапись", "музыка", "music production",
    "короткие видео", "вертикальн", "reels", "tiktok video",
    "аниме", "мультфильм", "нарисовать",
    # Сложный дизайн
    "logo design", "логотип", "brand identity", "ui/ux", "ui design",
    "illustration", "иллюстрация", "3d model", "баннер",
    # Реклама
    "google ads", "facebook ads", "таргет", "яндекс директ",
    "seo specialist", "smm manager",
    # Крупные бюджеты в названии
    "50 000", "100 000", "150 000", "200 000",
]

MAX_BUDGET_RUB = 15000
MAX_BUDGET_USD = 200

def is_relevant(title: str, description: str, budget: str = "") -> bool:
    text = (title + " " + description).lower()
    
    # 🚫 Чёрный список — сначала
    for bad in BLACKLIST:
        if bad in text:
            return False
    
    # 💰 Проверка бюджета
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

    # ✅ Белый список
    if any(kw in text for kw in WHITELIST):
        return True
    
    # 🌍 Для английских заказов — более мягкая проверка
    # Если заголовок на английском и не в чёрном списке — берём
    is_english = bool(re.search(r'^[a-zA-Z\s\d\-\,\.]+$', title.strip()))
    if is_english and len(title) > 10:
        # Дополнительные стоп-слова для английских
        en_stop = ['developer', 'programmer', 'engineer', 'architect', 
                   'video', 'animation', 'design logo', 'brand identity',
                   'mobile app', 'web app', 'wordpress', 'shopify dev']
        if not any(stop in text for stop in en_stop):
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

Важно: в конце вместо "Ваше имя" или подписи пиши "Артём".
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
        
        # Автоотклик на FL.ru
        if "fl.ru" in job.get('source','').lower() and FL_PHPSESSID:
            proposal = job.get('proposal', f"Здравствуйте! Готов выполнить ваш заказ качественно и в срок. Артём")
            fl_ok = await fl_apply_to_job(job['url'], proposal)
            if fl_ok:
                await context.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text="📨 *Отклик автоматически отправлен на FL.ru!*",
                    parse_mode='Markdown'
                )
        
        try:
            result = await execute_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            
            result_msg = (
                f"✨ *ГОТОВО!*\n\n"
                f"📌 *{job['title'][:80]}*\n\n"
                f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n"
                f"*Лила, проверь — отправляем?*"
            )
            
            # Шлём тебе
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=result_msg,
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            # Шлём Лиле если настроено
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(
                    chat_id=LILU_CHAT_ID,
                    text=result_msg,
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
        nums = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
        amount = float(nums[0]) if nums else 0
        is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
        if is_rub:
            save_earning(job_id, amount/90, amount, job['title'])
        else:
            save_earning(job_id, amount, amount*90, job['title'])
        stats = get_stats()

        # Автоматически отправляем результат и закрываем на FL.ru
        if "fl.ru" in job.get('source','').lower() and FL_PHPSESSID and job.get('result'):
            await query.edit_message_text("📤 Отправляю результат клиенту на FL.ru...")
            
            sent = await fl_send_result(job['url'], job['result'])
            closed = await fl_close_job(job['url'])
            
            fl_status = ""
            if sent:
                fl_status += "✅ Результат отправлен клиенту\n"
            if closed:
                fl_status += "🏁 Заказ закрыт на FL.ru\n"
                fl_status += "💳 Деньги придут на ЮMoney после подтверждения"
            
            await query.edit_message_text(
                f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n"
                f"✅ Выполнено: {stats['by_status'].get('done',0)}\n"
                f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\n"
                f"{fl_status}\n"
                f"Бухгалтер записал 📊",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n"
                f"✅ Выполнено: {stats['by_status'].get('done',0)}\n"
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
        "Мониторю рабочие источники:\n"
        "🇷🇺 FL.ru (4 категории)\n"
        "🟠 Guru.com (прямой парсинг)\n"
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
            parse_guru_direct(client),
            parse_telegram_channels(client),
            return_exceptions=True
        )
        for r in results:
            if isinstance(r, list):
                all_jobs.extend(r)

    logger.info(f"📦 Всего найдено: {len(all_jobs)}")
    
    # Фильтруем релевантные
    relevant = [j for j in all_jobs if is_relevant(j['title'], j['description'], j.get('budget',''))]
    logger.info(f"✅ Релевантных: {len(relevant)}")
    
    sent = 0
    for job in relevant[:6]:  # До 6 заказов за раз
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
