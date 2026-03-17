"""
Maker Bot — BTC 15m Polymarket
================================
Стратегия: смотреть на Binance WebSocket, за 30 секунд до закрытия окна
вычислить направление BTC и поставить maker-ордер на правильную сторону
по цене 0.90–0.95. При исполнении — профит $0.05–0.10 за шару при resolution.

Почему maker:
- Makers не платят комиссию (taker fees до 1.56%)
- Makers получают rebate от Polymarket (~0.1–0.2%)
- Нет гонки за скоростью — ставим заранее

Запуск:
    python maker_bot.py              # DRY RUN (безопасно)
    python maker_bot.py --live       # LIVE (реальные деньги)
    python maker_bot.py --live --shares 5 --entry-time 45

Зависимости: pip install httpx websockets python-dotenv py-clob-client
"""

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from observer import MarketFinder
from notifier import send_telegram

load_dotenv()

# ── Константы ────────────────────────────────────────────────────────────────
CLOB_HOST       = "https://clob.polymarket.com"
BINANCE_WS_URL  = "wss://stream.binance.com:9443/ws/btcusdt@trade"
WINDOW_SECS     = 900           # 15 минут

# Стратегия
ENTRY_BEFORE_SECS = 30          # Входить за 30 сек до закрытия окна
MAKER_PRICE_HIGH  = 0.93        # Цена maker-ордера при уверенном сигнале
MAKER_PRICE_MED   = 0.90        # Цена при умеренном сигнале
MIN_DELTA_PCT     = 0.05        # Мин. движение BTC (%) для уверенного сигнала
MED_DELTA_PCT     = 0.02        # Движение для умеренного сигнала
SHARES            = 5           # Минимум Polymarket

# Binance дельта — пороги (% от цены открытия окна)
# delta >= MIN_DELTA_PCT  → уверенный сигнал, цена 0.93
# delta >= MED_DELTA_PCT  → умеренный сигнал, цена 0.90
# delta <  MED_DELTA_PCT  → слишком неопределённо, пропускаем


@dataclass
class WindowState:
    window_ts:      int   = 0
    window_open_price: Optional[float] = None   # Цена BTC при открытии окна
    last_btc_price: Optional[float] = None       # Последняя цена BTC
    order_placed:   bool  = False
    order_id:       Optional[str] = None
    side:           str   = ""    # "YES" или "NO"
    entry_price:    float = 0.0
    filled:         bool  = False
    token_id:       str   = ""


class MakerBot:
    def __init__(self, dry_run: bool = True, shares: int = SHARES,
                 entry_before_secs: int = ENTRY_BEFORE_SECS):
        self.dry_run           = dry_run
        self.shares            = shares
        self.entry_before_secs = entry_before_secs
        self.finder            = MarketFinder()
        self.http              = httpx.Client(timeout=10.0)
        self.state             = WindowState()
        self.running           = False

        # Статистика сессии
        self.total_orders  = 0
        self.total_filled  = 0
        self.total_profit  = 0.0

        # CLOB клиент
        self.clob = self._init_clob()

    def _init_clob(self) -> ClobClient:
        client = ClobClient(
            host=CLOB_HOST,
            key=os.getenv("POLY_PRIVATE_KEY"),
            chain_id=137,
            signature_type=1,
            funder=os.getenv("POLY_FUNDER_ADDRESS"),
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    # ── Время ────────────────────────────────────────────────────────────────

    def _current_window_ts(self) -> int:
        now = time.time()
        return int(math.floor(now / WINDOW_SECS) * WINDOW_SECS)

    def _secs_to_window_end(self) -> float:
        now = time.time()
        window_end = self._current_window_ts() + WINDOW_SECS
        return window_end - now

    # ── Binance цена ─────────────────────────────────────────────────────────

    async def _binance_listener(self):
        """Слушает Binance WebSocket и обновляет last_btc_price."""
        while self.running:
            try:
                async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                    print("[BTC] Подключён к Binance WebSocket")
                    async for msg in ws:
                        if not self.running:
                            break
                        data = json.loads(msg)
                        price = float(data.get("p", 0))
                        if price > 0:
                            self.state.last_btc_price = price

                            # Запоминаем цену открытия нового окна
                            cur_ts = self._current_window_ts()
                            if cur_ts != self.state.window_ts:
                                # Новое окно — сбрасываем состояние
                                self._on_new_window(cur_ts)

            except Exception as e:
                print(f"\n[BTC] Ошибка Binance WS: {e} — переподключение через 3с")
                await asyncio.sleep(3)

    def _on_new_window(self, new_ts: int):
        """Вызывается при переходе в новое окно."""
        if self.state.window_ts > 0:
            # Итог прошлого окна
            self._log_window_result()

        self.state = WindowState(
            window_ts=new_ts,
            window_open_price=self.state.last_btc_price,
            last_btc_price=self.state.last_btc_price,
        )
        ts_str = datetime.fromtimestamp(new_ts, tz=timezone.utc).strftime("%H:%M:%S")
        print(f"\n[NEW] Окно {new_ts} ({ts_str} UTC) | BTC open = {self.state.window_open_price}")

    # ── Сигнал ───────────────────────────────────────────────────────────────

    def _compute_signal(self) -> tuple[str, float, float]:
        """
        Вычисляет направление и уверенность.
        Возвращает: (side, delta_pct, maker_price)
          side = "YES" (BTC вырос) | "NO" (упал) | "" (неясно)
        """
        open_p  = self.state.window_open_price
        last_p  = self.state.last_btc_price

        if not open_p or not last_p or open_p <= 0:
            return "", 0.0, 0.0

        delta_pct = (last_p - open_p) / open_p * 100  # %

        if abs(delta_pct) >= MIN_DELTA_PCT:
            side  = "YES" if delta_pct > 0 else "NO"
            price = MAKER_PRICE_HIGH
        elif abs(delta_pct) >= MED_DELTA_PCT:
            side  = "YES" if delta_pct > 0 else "NO"
            price = MAKER_PRICE_MED
        else:
            side  = ""
            price = 0.0

        return side, delta_pct, price

    # ── Ордер ────────────────────────────────────────────────────────────────

    def _get_token_id(self, market: dict, side: str) -> Optional[str]:
        """YES = token_ids[0], NO = token_ids[1]."""
        ids = market.get("token_ids", [])
        if len(ids) < 2:
            return None
        return ids[0] if side == "YES" else ids[1]

    def _place_maker_order(self, token_id: str, price: float, side_label: str) -> Optional[str]:
        """Ставит maker GTC ордер. Возвращает order_id или None."""
        if self.dry_run:
            fake_id = f"DRY-{side_label}-{int(time.time())}"
            print(f"\n[DRY] Maker ордер: {side_label} @ {price} x{self.shares}sh → id={fake_id}")
            return fake_id

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=float(self.shares),
                side=BUY,
            )
            signed = self.clob.create_order(order_args)
            resp   = self.clob.post_order(signed, OrderType.GTC)
            oid    = resp.get("orderID") if isinstance(resp, dict) else None
            print(f"\n[ORDER] Maker {side_label} @ {price} x{self.shares}sh → id={oid or '?'}")
            return oid
        except Exception as e:
            print(f"\n[!] Ошибка ордера: {e}")
            return None

    def _check_fill(self, order_id: str) -> bool:
        """Проверяет заполнен ли ордер."""
        if self.dry_run:
            # Симуляция: заполняем если цена на Polymarket >= нашей цены
            if self.state.token_id:
                try:
                    r = self.http.get(f"{CLOB_HOST}/midpoint",
                                      params={"token_id": self.state.token_id})
                    if r.status_code == 200:
                        mid = float(r.json().get("mid", 0))
                        return mid >= self.state.entry_price
                except Exception:
                    pass
            return False

        try:
            resp   = self.clob.get_order(order_id)
            status = resp.get("status", "") if isinstance(resp, dict) else ""
            return status in ("MATCHED", "FILLED")
        except Exception:
            return False

    def _cancel_order(self, order_id: str):
        if self.dry_run:
            print(f"\n[DRY] Отменяю ордер {order_id}")
            return
        try:
            self.clob.cancel(order_id)
        except Exception as e:
            print(f"\n[!] Ошибка отмены: {e}")

    # ── Итог окна ────────────────────────────────────────────────────────────

    def _log_window_result(self):
        s = self.state
        if not s.order_placed:
            result = "⏭️  Нет сигнала — пропустили"
        elif s.filled:
            profit = (1.0 - s.entry_price) * self.shares
            self.total_profit += profit
            self.total_filled += 1
            result = f"✅ FILLED {s.side} @ {s.entry_price} → профит ${profit:.2f}"
        else:
            result = f"😴 Ордер не заполнен ({s.side} @ {s.entry_price})"

        mode   = "[DRY]" if self.dry_run else "[LIVE]"
        msg    = (f"{mode} Окно {s.window_ts}\n"
                  f"{result}\n"
                  f"BTC: {s.window_open_price:.2f} → {s.last_btc_price:.2f}\n"
                  f"Всего профит: ${self.total_profit:.2f} | "
                  f"filled: {self.total_filled}/{self.total_orders}")
        print(f"\n[SUMMARY] {msg}")

        if s.filled:
            send_telegram(f"🎯 MAKER BOT\n{msg}")

    # ── Главный цикл ─────────────────────────────────────────────────────────

    async def _trading_loop(self):
        """Торговый цикл — проверяет сигнал и ставит ордера."""
        last_fill_check = 0.0
        last_status_print = 0.0

        while self.running:
            now = time.time()
            secs_left = self._secs_to_window_end()

            # ── Статус каждые 10 сек ─────────────────────────────────────
            if now - last_status_print >= 10:
                last_status_print = now
                btc  = f"{self.state.last_btc_price:.2f}" if self.state.last_btc_price else "?"
                open_p = f"{self.state.window_open_price:.2f}" if self.state.window_open_price else "?"
                side, delta, price = self._compute_signal()
                sig_str = f"{side}@{price}" if side else "нет сигнала"
                mode = "[DRY]" if self.dry_run else "[LIVE]"
                print(
                    f"  {mode} BTC={btc} (открытие={open_p}) "
                    f"| delta={delta:+.3f}% | сигнал={sig_str} "
                    f"| до закрытия={secs_left:.0f}s",
                    end="\r"
                )

            # ── Вход: за ENTRY_BEFORE_SECS до конца окна ─────────────────
            if (secs_left <= self.entry_before_secs
                    and secs_left > 5
                    and not self.state.order_placed):

                side, delta_pct, maker_price = self._compute_signal()

                if not side:
                    print(f"\n[SKIP] Слабый сигнал (delta={delta_pct:+.3f}%) — пропускаем")
                    self.state.order_placed = True  # Не пробовать снова в этом окне
                else:
                    # Ищем рынок
                    market = self.finder.find_current_btc_15m()
                    if not market:
                        print("\n[!] Рынок не найден — пропускаем")
                        self.state.order_placed = True
                    else:
                        token_id = self._get_token_id(market, side)
                        if not token_id:
                            print("\n[!] Token ID не найден")
                            self.state.order_placed = True
                        else:
                            print(f"\n[SIGNAL] {side} | delta={delta_pct:+.3f}% | "
                                  f"BTC={self.state.last_btc_price:.2f} | "
                                  f"maker_price={maker_price} | {secs_left:.0f}s до конца")

                            order_id = self._place_maker_order(token_id, maker_price, side)
                            self.state.order_placed = True
                            self.state.order_id     = order_id
                            self.state.side         = side
                            self.state.entry_price  = maker_price
                            self.state.token_id     = token_id
                            self.total_orders      += 1

                            mode = "DRY RUN" if self.dry_run else "LIVE"
                            send_telegram(
                                f"📋 MAKER: {side} @ {maker_price} x{self.shares}sh\n"
                                f"BTC delta={delta_pct:+.3f}% | {secs_left:.0f}s до конца\n"
                                f"Mode: {mode}"
                            )

            # ── Проверка филла ────────────────────────────────────────────
            if (self.state.order_placed
                    and self.state.order_id
                    and not self.state.filled
                    and now - last_fill_check >= 2.0):
                last_fill_check = now
                if self._check_fill(self.state.order_id):
                    self.state.filled = True
                    profit = (1.0 - self.state.entry_price) * self.shares
                    print(f"\n[FILL] ✅ {self.state.side} @ {self.state.entry_price} "
                          f"— держим до resolution | профит ${profit:.2f}")
                    send_telegram(
                        f"✅ MAKER FILLED: {self.state.side} @ {self.state.entry_price}\n"
                        f"Профит при resolution: ${profit:.2f}"
                    )

            # ── Отмена незаполненного ордера после закрытия окна ──────────
            if (secs_left <= 0
                    and self.state.order_placed
                    and self.state.order_id
                    and not self.state.filled):
                print(f"\n[CANCEL] Окно закрылось — отменяю незаполненный ордер")
                self._cancel_order(self.state.order_id)

            await asyncio.sleep(0.5)

    async def run(self):
        mode = "LIVE 🔴" if not self.dry_run else "DRY RUN 🔸"
        bal  = self._get_balance_str()
        print("=" * 60)
        print(f"  🤖 Maker Bot — {mode}")
        print(f"  Shares       : {self.shares}")
        print(f"  Entry before : {self.entry_before_secs}s до закрытия окна")
        print(f"  Maker prices : уверенный={MAKER_PRICE_HIGH} | умеренный={MAKER_PRICE_MED}")
        print(f"  Min BTC delta: уверенный={MIN_DELTA_PCT}% | умеренный={MED_DELTA_PCT}%")
        print(f"  Balance      : {bal}")
        print("  Ctrl+C для остановки")
        print("=" * 60)

        send_telegram(
            f"🤖 Maker Bot запущен | {mode}\n"
            f"Balance: {bal} | Shares: {self.shares}"
        )

        self.running = True

        # Инициализируем текущее окно
        self.state.window_ts = self._current_window_ts()

        # Запускаем Binance listener и торговый цикл параллельно
        try:
            await asyncio.gather(
                self._binance_listener(),
                self._trading_loop(),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self.running = False
            self._log_window_result()
            print("\n\n⛔ Maker Bot остановлен")
            print(f"   Ордеров: {self.total_orders}")
            print(f"   Заполнено: {self.total_filled}")
            print(f"   Профит: ${self.total_profit:.2f}")

    def _get_balance_str(self) -> str:
        try:
            resp = self.clob.get_balance_allowance()
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                bal = raw / 1e6 if raw > 10_000 else raw
                return f"${bal:.2f}"
        except Exception:
            pass
        return "n/a"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Maker Bot — BTC 15m Polymarket")
    parser.add_argument("--live",         action="store_true",
                        help="Включить LIVE торговлю (реальные деньги)")
    parser.add_argument("--shares",       type=int,   default=SHARES,
                        help=f"Шаров на ордер (мин. {SHARES}, default: {SHARES})")
    parser.add_argument("--entry-time",   type=int,   default=ENTRY_BEFORE_SECS,
                        help=f"Секунд до закрытия окна для входа (default: {ENTRY_BEFORE_SECS})")
    args = parser.parse_args()

    if args.shares < 5:
        print("❌ Минимум 5 шаров на Polymarket")
        return

    dry_run = not args.live

    if not dry_run:
        print()
        print("!" * 60)
        print("  ⚠️  LIVE MODE — РЕАЛЬНЫЕ ДЕНЬГИ ⚠️")
        print(f"  {args.shares} шаров @ maker цене")
        print("  Ctrl+C для отмены (5 секунд)...")
        print("!" * 60)
        print()
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("⛔ Отменено")
            return

    bot = MakerBot(
        dry_run=dry_run,
        shares=args.shares,
        entry_before_secs=args.entry_time,
    )

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()