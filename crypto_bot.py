import os
import logging
import asyncio
import aiohttp
import feedparser
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
)

# Конфигурация
TELEGRAM_TOKEN = "tg_bot"
DEEPSEEK_API_KEY = "ds_api"
CRYPTO_PANIC_API_KEY = "cp_api"

# Состояния для ConversationHandler
GENERATING_ANALYSIS, GENERATING_FORECAST, SELECTING_COIN = range(3)

# Надежные источники
SOURCES = {
    "CryptoPanic": f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_API_KEY}&filter=hot",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/rss",
    "Binance": "https://www.binance.com/en/rss/news",
    "Coinbase": "https://blog.coinbase.com/feed"
}

# Поддерживаемые валюты
SUPPORTED_COINS = ["BTC", "ETH", "SOL", "TON", "XLM", "BNB", "LTC"]

# --- Вспомогательные функции ---
async def get_current_prices():
    """Получает актуальные цены криптовалют с CoinGecko"""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "bitcoin,ethereum,solana,toncoin,stellar,binancecoin,litecoin",
        "vs_currencies": "usd",
        "include_market_cap": "false",
        "include_24hr_vol": "false",
        "include_24hr_change": "true",
        "include_last_updated_at": "true"
    }
    
    coin_mapping = {
        "bitcoin": "BTC",
        "ethereum": "ETH",
        "solana": "SOL",
        "toncoin": "TON",
        "stellar": "XLM",
        "binancecoin": "BNB",
        "litecoin": "LTC"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    result = {}
                    for coin_id, coin_data in data.items():
                        symbol = coin_mapping.get(coin_id)
                        if symbol:
                            result[symbol] = {
                                "price": coin_data["usd"],
                                "change": coin_data.get("usd_24h_change", 0)
                            }
                    return result
    except Exception as e:
        logging.error(f"CoinGecko API error: {str(e)[:200]}")
    
    # Fallback в случае ошибки
    return {
        coin: {"price": "N/A", "change": 0} for coin in SUPPORTED_COINS
    }

def format_price_change(change):
    """Форматирует изменение цены с иконкой"""
    if isinstance(change, (int, float)):
        icon = "📈" if change >= 0 else "📉"
        return f"{icon} {abs(change):.2f}%"
    return ""

async def fetch_news() -> list:
    """Собирает свежие новости (не старше 24 часов)"""
    news = []
    now = datetime.utcnow()
    time_threshold = now - timedelta(hours=24)
    
    # Для CryptoPanic (API)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(SOURCES["CryptoPanic"], timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    for post in data.get("results", []):
                        published = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
                        if published >= time_threshold:
                            news.append({
                                "title": post["title"],
                                "link": post["url"],
                                "source": "CryptoPanic",
                                "published": published,
                                "votes": post.get("votes", {})
                            })
    except Exception as e:
        logging.error(f"CryptoPanic error: {str(e)[:100]}")
    
    # Для RSS источников
    for name, url in [(k, v) for k, v in SOURCES.items() if k != "CryptoPanic"]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        text = await response.text()
                        feed = feedparser.parse(text)
                        for entry in feed.entries:
                            if "published_parsed" in entry:
                                published = datetime(*entry.published_parsed[:6])
                                if published >= time_threshold:
                                    news.append({
                                        "title": entry.title,
                                        "link": entry.link,
                                        "source": name,
                                        "published": published,
                                    })
        except Exception as e:
            logging.error(f"Error fetching {name} RSS: {str(e)[:100]}")
    
    # Сортируем по дате (свежие первыми)
    news.sort(key=lambda x: x["published"], reverse=True)
    return news[:20]  # Ограничиваем 20 самыми свежими новостями

async def analyze_with_deepseek(prompt: str) -> str:
    """Анализ контента с помощью DeepSeek-R1"""
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=60) as response:
                if response.status == 200:
                    data = await response.json()
                    if "choices" in data and len(data["choices"]) > 0:
                        return data["choices"][0]["message"]["content"]
                    else:
                        logging.error(f"DeepSeek response missing choices: {data}")
                else:
                    error = await response.text()
                    logging.error(f"DeepSeek API error {response.status}: {error[:200]}")
    except Exception as e:
        logging.error(f"DeepSeek API exception: {str(e)[:100]}")
    
    return "⚠️ Ошибка генерации анализа. Попробуйте позже."

# --- Основная логика бота ---
async def generate_full_analysis(news: list) -> str:
    """Генерирует актуальный анализ на основе свежих новостей"""
    # Получаем актуальные цены
    prices = await get_current_prices()
    current_date = datetime.now().strftime("%d %B %Y")
    
    # Форматируем блок с ценами
    price_block = "💰 *Актуальные цены:*\n"
    for asset, data in prices.items():
        price = f"${data['price']:,}" if isinstance(data['price'], (int, float)) else data['price']
        change = format_price_change(data.get('change', 0))
        price_block += f"- {asset}: {price} {change}\n"
    
    # Формируем контекст для анализа
    context = "📰 *Свежие новости за последние 24 часа:*\n\n"
    for i, item in enumerate(news[:10], 1):
        time_diff = datetime.utcnow() - item['published']
        hours_ago = time_diff.total_seconds() // 3600
        time_info = f"{int(hours_ago)} ч. назад" if hours_ago < 24 else item['published'].strftime('%d.%m.%Y')
        
        context += f"{i}. [{item['title']}]({item['link']}) ({item['source']} - {time_info})\n"
    
    # Создаем промт для DeepSeek с явным указанием даты и цен
    prompt = (
        f"Ты профессиональный криптоаналитик. Сегодня {current_date}. "
        f"Актуальные цены на основные криптоактивы:\n"
        f"- BTC: ${prices['BTC']['price']} (изменение: {prices['BTC'].get('change', 0):.2f}%)\n"
        f"- ETH: ${prices['ETH']['price']} (изменение: {prices['ETH'].get('change', 0):.2f}%)\n"
        f"- SOL: ${prices['SOL']['price']} (изменение: {prices['SOL'].get('change', 0):.2f}%)\n\n"
        f"Проанализируй САМЫЕ СВЕЖИЕ новости и дай краткий отчет ТОЛЬКО на основе последних 24 часов:\n\n"
        f"{context}\n\n"
        "Структура отчета (максимум 300 слов):\n"
        "1. Основные рыночные тренды (на основе последних новостей)\n"
        "2. Влияние на BTC, ETH, SOL (с использованием реальных цен)\n"
        "3. Прогноз на ближайшие 24 часа (с учетом текущей ситуации)\n"
        "4. Торговые рекомендации (практические советы)\n\n"
        "Используй профессиональную лексику. Будь кратким и информативным. "
        "НЕ ИСПОЛЬЗУЙ устаревшие данные или исторические примеры старше 2024 года. "
        "Все цены и прогнозы должны соответствовать текущей рыночной ситуации."
    )
    
    return price_block + "\n" + await analyze_with_deepseek(prompt)

async def generate_weekly_forecast(news: list) -> str:
    """Генерирует актуальный прогноз на неделю"""
    # Получаем актуальные цены
    prices = await get_current_prices()
    current_date = datetime.now().strftime("%d %B %Y")
    next_week = (datetime.now() + timedelta(days=7)).strftime("%d %B %Y")
    
    # Форматируем блок с ценами
    price_block = "💰 *Актуальные цены:*\n"
    for asset, data in prices.items():
        price = f"${data['price']:,}" if isinstance(data['price'], (int, float)) else data['price']
        change = format_price_change(data.get('change', 0))
        price_block += f"- {asset}: {price} {change}\n"
    
    # Создаем промт с явным указанием дат и цен
    prompt = (
        f"Ты профессиональный криптоаналитик. Сегодня {current_date}. "
        f"Актуальные цены на основные криптоактивы:\n"
        f"- BTC: ${prices['BTC']['price']}\n"
        f"- ETH: ${prices['ETH']['price']}\n"
        f"- SOL: ${prices['SOL']['price']}\n\n"
        f"Сделай прогноз крипторынка на ближайшую неделю ({current_date} - {next_week}) "
        f"ТОЛЬКО на основе последних новостей и текущих рыночных условий.\n\n"
        "Структура прогноза (максимум 400 слов):\n"
        "1. Обзор текущих рыночных тенденций (на основе СВЕЖИХ данных)\n"
        "2. Прогноз по BTC, ETH, SOL (с указанием ценовых диапазонов)\n"
        "3. Ключевые уровни поддержки/сопротивления (актуальные)\n"
        "4. Практические рекомендации для трейдеров\n\n"
        "Будь конкретным и информативным. Учитывай ТОЛЬКО события последних 48 часов. "
        "НЕ ИСПОЛЬЗУЙ устаревшие данные или исторические примеры старше 2024 года. "
        "Все прогнозы должны основываться на текущих ценах и рыночных условиях."
    )
    
    return price_block + "\n" + await analyze_with_deepseek(prompt)

async def generate_coin_analysis(coin: str, news: list) -> str:
    """Генерирует анализ и прогноз для конкретной валюты"""
    # Получаем актуальные цены
    prices = await get_current_prices()
    coin_price = prices.get(coin, {"price": "N/A", "change": 0})
    current_date = datetime.now().strftime("%d %B %Y")
    next_week = (datetime.now() + timedelta(days=7)).strftime("%d %B %Y")
    
    # Форматируем блок с ценой
    price = f"${coin_price['price']:,}" if isinstance(coin_price['price'], (int, float)) else coin_price['price']
    change = format_price_change(coin_price.get('change', 0))
    price_block = f"💰 *Текущая цена {coin}:* {price} {change}\n"
    
    # Фильтруем новости, относящиеся к выбранной валюте
    coin_news = [item for item in news if coin.lower() in item['title'].lower()]
    
    # Формируем контекст новостей
    context = f"📰 *Свежие новости по {coin}:*\n\n"
    news_links = []  # Сохраняем ссылки для добавления в конце
    
    if coin_news:
        for i, item in enumerate(coin_news[:5], 1):
            time_diff = datetime.utcnow() - item['published']
            hours_ago = time_diff.total_seconds() // 3600
            time_info = f"{int(hours_ago)} ч. назад" if hours_ago < 24 else item['published'].strftime('%d.%m.%Y')
            
            # Форматируем как ссылку
            context += f"{i}. [{item['title']}]({item['link']}) ({item['source']} - {time_info})\n"
            news_links.append(item['link'])
    else:
        context += "Новостей по этой валюте за последние 24 часа не найдено.\n"
    
    # Создаем промт для DeepSeek
    prompt = (
        f"Ты профессиональный криптоаналитик. Сегодня {current_date}. "
        f"Текущая цена {coin}: {price} (изменение за 24ч: {coin_price.get('change', 0):.2f}%)\n\n"
        f"Проанализируй новости, связанные с {coin}, и сделай прогноз на ближайшую неделю ({current_date} - {next_week}).\n\n"
        f"{context}\n\n"
        "Структура отчета (максимум 300 слов):\n"
        "1. Ключевые события, влияющие на цену\n"
        "2. Технический анализ (тренды, уровни поддержки/сопротивления)\n"
        "3. Прогноз цены на неделю\n"
        "4. Рекомендации для трейдеров\n\n"
        "Будь конкретным и информативным. Используй только актуальные данные."
    )
    
    analysis = await analyze_with_deepseek(prompt)
    
    # Добавляем ссылки на статьи в конце
    if news_links:
        analysis += "\n\n🔍 *Источники новостей:*\n"
        for i, link in enumerate(news_links[:5], 1):
            analysis += f"{i}. [Статья {i}]({link})\n"
    
    return price_block + "\n" + analysis

# --- Обработчики Telegram ---
def create_main_menu():
    """Создает клавиатуру главного меню"""
    keyboard = [
        [InlineKeyboardButton("📈 Анализ за 24ч", callback_data="daily")],
        [InlineKeyboardButton("🔮 Прогноз на неделю", callback_data="weekly")],
        [InlineKeyboardButton("📰 Топ-5 новостей", callback_data="news")],
        [InlineKeyboardButton("💰 Конкретная валюта", callback_data="specific_coin")]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_coin_menu():
    """Создает меню выбора валюты"""
    keyboard = []
    # Разбиваем на группы по 3 монеты
    for i in range(0, len(SUPPORTED_COINS), 3):
        row = [
            InlineKeyboardButton(coin, callback_data=f"coin_{coin}")
            for coin in SUPPORTED_COINS[i:i+3]
        ]
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def create_cancel_button():
    """Создает клавиатуру с кнопкой отмены"""
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start"""
    await update.message.reply_text(
        "💰 *Ваш персональный криптоаналитик готов к работе!*\n"
        f"Актуальные данные на {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        "Выберите действие:",
        reply_markup=create_main_menu(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Операция отменена. Выберите действие:",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

async def specific_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора конкретной валюты"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Выберите криптовалюту для анализа:",
        reply_markup=create_coin_menu()
    )
    return SELECTING_COIN

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Выберите действие:",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

async def coin_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализ конкретной валюты"""
    query = update.callback_query
    await query.answer()
    coin = query.data.split('_')[1]  # Извлекаем название валюты
    
    # Сообщение о начале генерации
    await query.edit_message_text(
        text=f"⏳ Анализирую {coin}...\nСобираю новости и формирую прогноз на неделю.",
        reply_markup=create_cancel_button()
    )
    
    # Сбор новостей и генерация анализа
    news = await fetch_news()
    analysis = await generate_coin_analysis(coin, news)
    
    # Добавляем отметку времени
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    analysis = f"🔍 *АНАЛИЗ И ПРОГНОЗ ДЛЯ {coin}:*\n\n🔄 Обновлено: {timestamp}\n\n{analysis}"
    
    # Отправка результата
    await query.edit_message_text(
        text=analysis,
        parse_mode="Markdown",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

async def daily_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск анализа за 24 часа"""
    query = update.callback_query
    await query.answer()
    
    # Сообщение о начале генерации
    await query.edit_message_text(
        text="⏳ *Собираю СВЕЖИЕ новости и анализирую рынок...*\n"
             "Используются данные только за последние 24 часа...",
        parse_mode="Markdown",
        reply_markup=create_cancel_button()
    )
    
    # Сбор новостей и генерация анализа
    news = await fetch_news()
    analysis = await generate_full_analysis(news)
    
    # Добавляем отметку времени
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    analysis = f"🔄 Обновлено: {timestamp}\n\n{analysis}"
    
    # Отправка результата
    await query.edit_message_text(
        text=f"📊 *КРИПТОАНАЛИТИКА ЗА 24 ЧАСА:*\n\n{analysis}",
        parse_mode="Markdown",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

async def weekly_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск прогноза на неделю"""
    query = update.callback_query
    await query.answer()
    
    # Сообщение о начале генерации
    await query.edit_message_text(
        text="⏳ *Анализирую текущие тренды и составляю прогноз...*\n"
             "Используются самые свежие данные...",
        parse_mode="Markdown",
        reply_markup=create_cancel_button()
    )
    
    # Сбор новостей и генерация прогноза
    news = await fetch_news()
    forecast = await generate_weekly_forecast(news)
    
    # Добавляем даты прогноза
    today = datetime.now().strftime("%d.%m.%Y")
    next_week = (datetime.now() + timedelta(days=7)).strftime("%d.%m.%Y")
    forecast = f"📅 Период прогноза: {today} - {next_week}\n\n{forecast}"
    
    # Отправка результата
    await query.edit_message_text(
        text=f"🔮 *ПРОГНОЗ НА НЕДЕЛЮ:*\n\n{forecast}",
        parse_mode="Markdown",
        reply_markup=create_main_menu()
    )
    return ConversationHandler.END

async def show_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 самых свежих новостей"""
    query = update.callback_query
    await query.answer()
    
    # Получаем новости
    news = await fetch_news()
    top_news = news[:5]
    
    # Формируем сообщение с временными метками
    message = "📰 *САМЫЕ СВЕЖИЕ НОВОСТИ:*\n\n"
    for i, item in enumerate(top_news, 1):
        time_diff = (datetime.utcnow() - item['published']).total_seconds()
        hours_ago = time_diff // 3600
        minutes_ago = (time_diff % 3600) // 60
        
        if hours_ago > 0:
            time_info = f"{int(hours_ago)} ч. {int(minutes_ago)} мин. назад"
        else:
            time_info = f"{int(minutes_ago)} мин. назад"
        
        message += f"{i}. [{item['title']}]({item['link']}) \n⌚ {time_info} | {item['source']}\n\n"
    
    await query.edit_message_text(
        text=message,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=create_main_menu()
    )

# --- Регулярные задачи ---
async def send_scheduled_news(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет важные новости одним сообщением"""
    try:
        # Проверяем временное окно (7:00-21:00 GMT+1)
        now = datetime.utcnow() + timedelta(hours=1)  # GMT+1
        if not (7 <= now.hour <= 21):
            return
        
        # Получаем горячие новости не старше 4 часов
        news = await fetch_news()
        hot_news = [n for n in news if (datetime.utcnow() - n['published']).total_seconds() < 14400]
        
        if not hot_news:
            return
        
        # Получаем актуальные цены
        prices = await get_current_prices()
        price_block = "💰 *Актуальные цены:*\n"
        for asset, data in prices.items():
            price = f"${data['price']:,}" if isinstance(data['price'], (int, float)) else data['price']
            change = format_price_change(data.get('change', 0))
            price_block += f"- {asset}: {price} {change}\n"
        
        # Формируем сообщение
        message = "🔥 *СВЕЖИЕ НОВОСТИ ЗА ПОСЛЕДНИЕ 4 ЧАСА:*\n\n"
        for i, item in enumerate(hot_news[:3], 1):
            time_diff = (datetime.utcnow() - item['published']).total_seconds()
            hours_ago = int(time_diff // 3600)
            minutes_ago = int((time_diff % 3600) // 60)
            
            if hours_ago > 0:
                time_info = f"{hours_ago} ч. {minutes_ago} мин. назад"
            else:
                time_info = f"{minutes_ago} мин. назад"
            
            message += f"{i}. [{item['title']}]({item['link']}) \n⌚ {time_info} | {item['source']}\n\n"
        
        # Добавляем цены
        message += "\n" + price_block
        
        # Отправляем одним сообщением
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Error in send_scheduled_news: {str(e)[:100]}")

# --- Запуск бота ---
def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    # Создаем Application с JobQueue
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем обработчики
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(daily_analysis, pattern="^daily$"),
            CallbackQueryHandler(weekly_forecast, pattern="^weekly$"),
            CallbackQueryHandler(specific_coin, pattern="^specific_coin$"),
        ],
        states={
            GENERATING_ANALYSIS: [CallbackQueryHandler(cancel, pattern="^cancel$")],
            GENERATING_FORECAST: [CallbackQueryHandler(cancel, pattern="^cancel$")],
            SELECTING_COIN: [
                CallbackQueryHandler(coin_analysis, pattern="^coin_"),
                CallbackQueryHandler(back_to_main, pattern="^back_to_main$"),
                CallbackQueryHandler(cancel, pattern="^cancel$")
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(show_news, pattern="^news$"))
    application.add_handler(CallbackQueryHandler(cancel, pattern="^cancel$"))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))
    
    # Планировщик для регулярных новостей
    job_queue = application.job_queue
    if job_queue:
        # Запускаем задачу каждые 4 часа (14400 секунд)
        job_queue.run_repeating(
            send_scheduled_news,
            interval=14400,
            first=10
        )
    
    application.run_polling()

if __name__ == "__main__":
    main()
