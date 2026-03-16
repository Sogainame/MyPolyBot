"""
Конфигурация Polymarket Arbitrage Scanner
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket API ──
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

# ── Параметры сканирования ──
SCAN_INTERVAL_SEC = 30          # Как часто сканировать (секунды)
MIN_PROFIT_PCT = 1.0            # Минимальный % прибыли для оповещения
MIN_LIQUIDITY = 5000            # Минимальная ликвидность рынка ($)
MIN_VOLUME = 1000               # Минимальный объём рынка ($)
WINNER_FEE_PCT = 2.0            # Комиссия Polymarket на выигрыш (%)
GAS_COST_USD = 0.01             # Примерная стоимость газа на Polygon ($)

# ── Telegram ──
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Пагинация API ──
EVENTS_PER_PAGE = 100           # Сколько событий за один запрос
MAX_PAGES = 20                  # Максимум страниц (до 2000 событий)
