#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot — Главный скрипт.

Запуск:
  python main.py              — однократный скан (тест)
  python main.py --loop       — бесконечный цикл
  python main.py --loop --tg  — цикл + уведомления в Telegram

Перед запуском:
  pip install httpx python-telegram-bot python-dotenv
  cp .env.example .env        — и заполни свои данные
"""

import sys
import time
from datetime import datetime, timezone

from scanner import PolymarketScanner
from notifier import notify_opportunities, send_telegram
import config


def run_once(use_telegram: bool = False):
    """Одноразовый скан — для тестирования."""
    scanner = PolymarketScanner()
    opps = scanner.scan_once()

    if opps:
        print(f"\n🔔 НАЙДЕНО {len(opps)} ВОЗМОЖНОСТЕЙ\n")
        for opp in opps:
            print(scanner.format_opportunity(opp))

        if use_telegram:
            sent = notify_opportunities(opps)
            print(f"\n📱 Отправлено в Telegram: {sent}/{len(opps)}")
    else:
        print("\n😴 Арбитражных возможностей не найдено")
        print(f"   MIN_PROFIT_PCT = {config.MIN_PROFIT_PCT}%")
        print(f"   MIN_LIQUIDITY  = ${config.MIN_LIQUIDITY:,}")
        print("   Попробуй снизить пороги в config.py")


def run_loop(use_telegram: bool = False):
    """Бесконечный цикл сканирования."""
    scanner = PolymarketScanner()

    print("=" * 60)
    print("  🤖 Polymarket Arbitrage Scanner v0.1")
    print(f"  Мин. прибыль:    {config.MIN_PROFIT_PCT}%")
    print(f"  Мин. ликвидность: ${config.MIN_LIQUIDITY:,}")
    print(f"  Интервал:        {config.SCAN_INTERVAL_SEC}с")
    print(f"  Telegram:        {'✅' if use_telegram else '❌'}")
    print("=" * 60)

    if use_telegram:
        send_telegram("🤖 Polymarket Arbitrage Scanner запущен!")

    scan_num = 0
    total_found = 0

    while True:
        scan_num += 1
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        print(f"\n{'─'*60}")
        print(f"[СКАН #{scan_num}] {now}")
        print(f"{'─'*60}")

        try:
            opps = scanner.scan_once()

            if opps:
                total_found += len(opps)
                print(f"\n🔔 НАЙДЕНО {len(opps)} ВОЗМОЖНОСТЕЙ:")
                for opp in opps:
                    print(scanner.format_opportunity(opp))

                if use_telegram:
                    sent = notify_opportunities(opps)
                    print(f"📱 Telegram: {sent}/{len(opps)} отправлено")
            else:
                print("😴 Ничего не найдено")

            print(f"📊 Всего найдено за сессию: {total_found}")

        except KeyboardInterrupt:
            print("\n\n⛔ Остановлено пользователем")
            if use_telegram:
                send_telegram(
                    f"⛔ Scanner остановлен\n"
                    f"Сканов: {scan_num}, найдено: {total_found}"
                )
            break

        except Exception as e:
            print(f"\n[!] Ошибка: {e}")
            if use_telegram:
                send_telegram(f"⚠️ Ошибка сканера: {e}")

        print(f"⏳ Следующий скан через {config.SCAN_INTERVAL_SEC}с...")

        try:
            time.sleep(config.SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n\n⛔ Остановлено")
            break


def main():
    args = sys.argv[1:]

    use_loop = "--loop" in args
    use_tg = "--tg" in args

    if use_loop:
        run_loop(use_telegram=use_tg)
    else:
        run_once(use_telegram=use_tg)


if __name__ == "__main__":
    main()
