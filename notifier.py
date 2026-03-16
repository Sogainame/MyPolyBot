"""
Telegram-уведомления об арбитражных возможностях.

Настройка:
1. Создай бота через @BotFather в Telegram → получишь токен
2. Напиши боту /start, затем открой:
   https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates
   → найди свой chat_id
3. Запиши в .env:
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=987654321
"""

import httpx
import config


def send_telegram(message: str) -> bool:
    """
    Отправляет сообщение в Telegram.
    Возвращает True если успешно, False если ошибка.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram лимит — 4096 символов
    if len(message) > 4000:
        message = message[:4000] + "\n... (обрезано)"

    try:
        resp = httpx.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        }, timeout=10.0)

        if resp.status_code == 200:
            return True
        else:
            print(f"[!] Telegram ошибка: {resp.status_code} {resp.text}")
            return False

    except Exception as e:
        print(f"[!] Telegram недоступен: {e}")
        return False


def format_opportunity_html(opp) -> str:
    """Форматирует возможность в HTML для Telegram."""
    emoji = {
        "intra_market": "💰",
        "intra_event": "🎯",
        "sum_anomaly": "⚠️",
        "logical": "🧠",
    }.get(opp.type, "📊")

    return (
        f"{emoji} <b>{opp.type.upper()}</b>\n"
        f"<b>{opp.event_title}</b>\n\n"
        f"💵 Стоимость: ${opp.total_cost:.4f}\n"
        f"📈 Прибыль: {opp.net_profit_pct:+.2f}%\n"
        f"💲 На $100: ${opp.net_profit_usd:+.2f}\n"
        f"🌊 Ликвидность: ${opp.liquidity:,.0f}\n\n"
        f"🔗 <a href=\"{opp.url}\">Открыть на Polymarket</a>\n"
        f"🕐 {opp.found_at}"
    )


def notify_opportunities(opportunities: list) -> int:
    """
    Отправляет все найденные возможности в Telegram.
    Возвращает количество успешно отправленных.
    """
    if not opportunities:
        return 0

    sent = 0
    for opp in opportunities:
        msg = format_opportunity_html(opp)
        if send_telegram(msg):
            sent += 1

    return sent
