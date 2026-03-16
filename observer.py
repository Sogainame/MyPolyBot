"""
BTC 5-Minute Market Observer (Шаг 1 стратегии Gabagool)

Подключается к Polymarket, находит текущий 5-мин BTC Up/Down рынок,
слушает цены через WebSocket и логирует в CSV.

Цель: собрать данные чтобы понять паттерны цен YES/NO внутри 5-мин окна.

Запуск:
  pip install httpx websocket-client
  python observer.py

Без API ключей — только наблюдение, без торговли.
"""

import json
import csv
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

# ── Настройки ──
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

CSV_DIR = Path("data")
CSV_DIR.mkdir(exist_ok=True)

POLL_INTERVAL = 3  # Секунды между REST-опросами (фолбэк если WebSocket не работает)


class MarketFinder:
    """Находит текущий активный 5-мин BTC Up/Down рынок."""

    def __init__(self):
        self.client = httpx.Client(timeout=15.0)

    def _get_server_time(self) -> float:
        """Получает серверное время из CLOB API. Фолбэк — локальное UTC."""
        try:
            resp = self.client.get(f"{CLOB_API}/time", timeout=5.0)
            if resp.status_code == 200:
                raw = resp.text.strip().strip('"')
                return float(raw)
        except Exception:
            pass
        return datetime.now(timezone.utc).timestamp()

    def _window_start_ts(self, base_ts: float, offset_min: int = 0) -> int:
        """Возвращает unix timestamp начала 15-мин окна для base_ts + offset_min."""
        t = datetime.fromtimestamp(base_ts, tz=timezone.utc) + timedelta(minutes=offset_min)
        rounded_min = (t.minute // 15) * 15
        interval_start = t.replace(minute=rounded_min, second=0, microsecond=0)
        return int(interval_start.timestamp())

    def _midpoint_is_live(self, token_id: str) -> bool:
        """
        Возвращает True если midpoint отличается от 0.5
        (означает, что рынок живой и торгуется).
        """
        try:
            resp = self.client.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=5.0)
            if resp.status_code == 200:
                mid = float(resp.json().get("mid", 0.5))
                return mid != 0.5
        except Exception:
            pass
        return False

    def _market_is_valid(self, m: dict) -> bool:
        """Проверяет что рынок открыт и принимает заявки."""
        if m.get("closed") is True:
            return False
        if not m.get("acceptingOrders", False):
            return False
        return True

    def find_current_btc_15m(self) -> dict | None:
        """
        Ищет активный 15-мин BTC рынок.
        Основной метод — генерация slug по серверному времени.
        Фолбэк — поиск по /markets endpoint.
        """
        # ── Шаг 1: получаем серверное время ──────────────────────────────────
        now_ts = self._get_server_time()
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        print(f"    Серверное время: {now_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        # ── Шаг 2-4: пробуем slugs по окнам ──────────────────────────────────
        for offset_min in [0, -15, 15]:
            ts = self._window_start_ts(now_ts, offset_min)
            slug = f"btc-updown-15m-{ts}"
            label = {0: "текущее", -5: "предыдущее", 5: "следующее"}[offset_min]
            print(f"    Trying slug ({label}): {slug}")

            try:
                resp = self.client.get(f"{GAMMA_API}/markets", params={"slug": slug})
                if resp.status_code != 200:
                    continue

                data = resp.json()
                markets = data if isinstance(data, list) else [data]

                for m in markets:
                    if not m or m.get("slug") != slug:
                        continue
                    if not self._market_is_valid(m):
                        print(f"    [~] {slug} — рынок закрыт или не принимает заявки")
                        continue

                    # Проверяем что midpoint не заморожен на 0.5
                    clob_ids = m.get("clobTokenIds", "")
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except Exception:
                            clob_ids = []
                    first_token = clob_ids[0] if clob_ids else None

                    if first_token and not self._midpoint_is_live(first_token):
                        print(f"    [~] {slug} — midpoint застрял на 0.5, пропускаем")
                        continue

                    print(f"    [+] Найден живой рынок: {slug}")
                    return self._parse_market(m)

            except Exception as e:
                print(f"    [!] Ошибка при проверке {slug}: {e}")
                continue

        # ── Шаг 5: фолбэк — поиск по /markets endpoint ───────────────────────
        print("    [~] Slug-метод не дал результата, пробую поиск по /markets...")
        try:
            resp = self.client.get(f"{GAMMA_API}/markets", params={
                "active": "true",
                "closed": "false",
                "limit": 50,
                "order": "startDate",
                "ascending": "false",
            })
            resp.raise_for_status()
            for m in resp.json():
                slug = m.get("slug", "")
                question = m.get("question", "").lower()
                is_btc_15m = (
                    "btc-updown-15m" in slug or
                    ("btc" in slug and "15m" in slug) or
                    ("bitcoin" in question and "15" in question and ("up" in question or "down" in question))
                )
                if is_btc_15m and self._market_is_valid(m):
                    print(f"    [+] Фолбэк нашёл: {slug}")
                    return self._parse_market(m)
        except Exception as e:
            print(f"[!] Ошибка фолбэк-поиска: {e}")

        return None

    def _parse_market(self, m: dict) -> dict:
        """Парсит рынок в удобный формат."""
        clob_token_ids = m.get("clobTokenIds", "")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except:
                clob_token_ids = []

        outcomes = m.get("outcomes", "")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = []

        prices = m.get("outcomePrices", "")
        if isinstance(prices, str):
            try:
                prices = [float(p) for p in json.loads(prices)]
            except:
                prices = []

        return {
            "slug": m.get("slug", ""),
            "question": m.get("question", ""),
            "condition_id": m.get("conditionId", ""),
            "token_ids": clob_token_ids,  # [YES_token_id, NO_token_id]
            "outcomes": outcomes,
            "prices": prices,
            "end_date": m.get("endDate", ""),
            "game_start_time": m.get("gameStartTime", ""),
        }


class PriceLogger:
    """Логирует цены в CSV файл."""

    def __init__(self):
        self.current_file = None
        self.writer = None
        self.file_handle = None
        self.row_count = 0

    def start_new_session(self, slug: str):
        """Начинает новую CSV-сессию для рынка."""
        self.close()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = CSV_DIR / f"{slug}_{timestamp}.csv"
        self.file_handle = open(filename, "w", newline="")
        self.writer = csv.writer(self.file_handle)
        self.writer.writerow([
            "timestamp_utc",      # Время записи
            "elapsed_sec",        # Секунд от начала наблюдения
            "yes_price",          # Цена YES
            "no_price",           # Цена NO
            "sum_price",          # YES + NO
            "spread_to_1",        # 1.00 - (YES + NO) — потенциальный профит
            "yes_best_bid",       # Лучший бид на YES
            "yes_best_ask",       # Лучший аск на YES
            "no_best_bid",        # Лучший бид на NO
            "no_best_ask",        # Лучший аск на NO
            "source",             # "ws" или "rest"
        ])
        self.current_file = filename
        self.row_count = 0
        self.start_time = time.time()
        print(f"[+] CSV: {filename}")

    def log(self, yes_price, no_price, yes_bid=0, yes_ask=0, no_bid=0, no_ask=0, source="rest"):
        """Записывает строку в CSV."""
        if not self.writer:
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        elapsed = round(time.time() - self.start_time, 1)
        sum_price = round(yes_price + no_price, 6)
        spread = round(1.0 - sum_price, 6)

        self.writer.writerow([
            now, elapsed,
            yes_price, no_price, sum_price, spread,
            yes_bid, yes_ask, no_bid, no_ask,
            source,
        ])
        self.file_handle.flush()
        self.row_count += 1

    def close(self):
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None
            self.writer = None


class BTCObserver:
    """Основной наблюдатель — находит рынки и логирует цены."""

    def __init__(self):
        self.finder = MarketFinder()
        self.logger = PriceLogger()
        self.http = httpx.Client(timeout=10.0)
        self.current_market = None
        self.running = False

        # Последние известные цены (для терминала)
        self.last_yes = 0
        self.last_no = 0
        self.last_sum = 0
        self.min_sum = 999
        self.max_sum = 0
        self.tick_count = 0

    def find_market(self) -> bool:
        """Ищет текущий активный 5-мин BTC рынок."""
        print("\n[*] Ищу активный BTC 5-мин рынок...")
        market = self.finder.find_current_btc_15m()

        if not market:
            print("[!] Активный BTC 5-мин рынок не найден")
            print("    Возможно, сейчас нет активного окна или формат slug изменился")
            return False

        self.current_market = market
        print(f"[+] Найден: {market['question']}")
        print(f"    Slug: {market['slug']}")
        print(f"    Outcomes: {market['outcomes']}")
        if market['prices']:
            print(f"    Цены: YES={market['prices'][0]:.4f} NO={market['prices'][1]:.4f}")
        print(f"    Token IDs: {len(market['token_ids'])} шт.")

        self.logger.start_new_session(market['slug'])
        self.min_sum = 999
        self.max_sum = 0
        self.tick_count = 0

        return True

    def poll_prices_rest(self):
        """Получает цены через REST API (CLOB midpoint)."""
        if not self.current_market or not self.current_market['token_ids']:
            return

        token_ids = self.current_market['token_ids']

        try:
            yes_data = {"mid": 0, "best_bid": 0, "best_ask": 0}
            no_data = {"mid": 0, "best_bid": 0, "best_ask": 0}

            # Получаем midpoint и book для YES
            if len(token_ids) > 0:
                resp = self.http.get(f"{CLOB_API}/midpoint", params={"token_id": token_ids[0]})
                if resp.status_code == 200:
                    yes_data["mid"] = float(resp.json().get("mid", 0))

                resp_book = self.http.get(f"{CLOB_API}/book", params={"token_id": token_ids[0]})
                if resp_book.status_code == 200:
                    book = resp_book.json()
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids:
                        yes_data["best_bid"] = float(bids[0].get("price", 0))
                    if asks:
                        yes_data["best_ask"] = float(asks[0].get("price", 0))

            # Получаем midpoint и book для NO
            if len(token_ids) > 1:
                resp = self.http.get(f"{CLOB_API}/midpoint", params={"token_id": token_ids[1]})
                if resp.status_code == 200:
                    no_data["mid"] = float(resp.json().get("mid", 0))

                resp_book = self.http.get(f"{CLOB_API}/book", params={"token_id": token_ids[1]})
                if resp_book.status_code == 200:
                    book = resp_book.json()
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids:
                        no_data["best_bid"] = float(bids[0].get("price", 0))
                    if asks:
                        no_data["best_ask"] = float(asks[0].get("price", 0))

            yes_price = yes_data["mid"]
            no_price = no_data["mid"]

            if yes_price > 0 or no_price > 0:
                self._update_prices(
                    yes_price, no_price,
                    yes_data["best_bid"], yes_data["best_ask"],
                    no_data["best_bid"], no_data["best_ask"],
                    source="rest"
                )

        except Exception as e:
            print(f"[!] REST ошибка: {e}")

    def _update_prices(self, yes_price, no_price, yes_bid, yes_ask, no_bid, no_ask, source):
        """Обновляет цены и логирует."""
        self.last_yes = yes_price
        self.last_no = no_price
        self.last_sum = yes_price + no_price
        self.tick_count += 1

        if self.last_sum > 0:
            self.min_sum = min(self.min_sum, self.last_sum)
            self.max_sum = max(self.max_sum, self.last_sum)

        # Логируем в CSV
        self.logger.log(
            yes_price, no_price,
            yes_bid, yes_ask,
            no_bid, no_ask,
            source=source
        )

        # Печатаем в терминал
        spread = 1.0 - self.last_sum
        indicator = "🟢" if spread > 0.02 else "🟡" if spread > 0 else "🔴"

        print(
            f"  {indicator} YES={yes_price:.4f} NO={no_price:.4f} "
            f"SUM={self.last_sum:.4f} SPREAD={spread:+.4f} "
            f"[min={self.min_sum:.4f} max={self.max_sum:.4f}] "
            f"tick#{self.tick_count} ({source})",
            end="\r"
        )

    def run(self):
        """Основной цикл: найти рынок → логировать цены → повторить."""
        print("=" * 60)
        print("  🔭 BTC 5-Min Observer (Gabagool Strategy)")
        print("  Собираем данные о ценах YES/NO")
        print("  Ctrl+C для остановки")
        print("=" * 60)

        self.running = True

        while self.running:
            # Ищем рынок
            if not self.find_market():
                print("[*] Жду 30 секунд и пробую снова...")
                time.sleep(30)
                continue

            print(f"\n[*] Наблюдаю (REST каждые {POLL_INTERVAL}с)...\n")

            # Опрашиваем REST пока рынок активен
            market_start = time.time()
            while self.running:
                self.poll_prices_rest()

                # Проверяем не истёк ли рынок (5 мин = 300 сек + запас)
                elapsed = time.time() - market_start
                if elapsed > 360:  # 6 минут
                    print(f"\n\n[*] 5-мин окно завершено. Записей: {self.logger.row_count}")
                    print(f"    MIN sum = {self.min_sum:.4f}")
                    print(f"    MAX sum = {self.max_sum:.4f}")
                    print(f"    Файл: {self.logger.current_file}")
                    break

                try:
                    time.sleep(POLL_INTERVAL)
                except KeyboardInterrupt:
                    self.running = False
                    break

            # Переходим к следующему рынку
            if self.running:
                print("\n[*] Ищу следующий 5-мин рынок...")
                time.sleep(5)

        self.logger.close()
        print("\n\n⛔ Остановлено")


if __name__ == "__main__":
    observer = BTCObserver()
    try:
        observer.run()
    except KeyboardInterrupt:
        observer.logger.close()
        print("\n⛔ Остановлено")
