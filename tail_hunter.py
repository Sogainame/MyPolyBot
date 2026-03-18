"""
Tail Hunter — Paper Trading Bot for BTC 5-Min Extreme Positions

Стратегия: покупаем позицию когда цена <= порога (x33-x100 payout).
Мониторинг цен в РЕАЛЬНОМ ВРЕМЕНИ через Polymarket WebSocket.

Два режима получения цен:
1. WebSocket (основной) — мгновенные обновления при каждом изменении
2. REST polling (фолбэк) — каждые 5 сек если WS не подключён

Запуск:
    python tail_hunter.py                    # стандартный режим
    python tail_hunter.py --balance 50       # стартовый баланс $50
    python tail_hunter.py --threshold 0.03   # покупать при цене <= $0.03
    python tail_hunter.py --bet-size 1       # ставка $1 за сделку

Зависимости: pip install httpx websockets python-dotenv
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import websockets

from notifier import send_telegram

# ── API endpoints ────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_BALANCE = 20.0
DEFAULT_BET_SIZE = 1.0
DEFAULT_THRESHOLD = 0.02
WINDOW_SECONDS = 300
WAIT_AFTER_WINDOW = 30
POLYMARKET_FEE = 0.02
REST_FALLBACK_INTERVAL = 5

LOG_DIR = Path("data/tail_hunter")
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── State ────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    timestamp: str
    window_slug: str
    side: str
    entry_price: float
    shares: float
    bet_cost: float
    result: str = ""
    payout: float = 0.0
    pnl: float = 0.0


@dataclass
class LivePrices:
    up_mid: float = 0.0
    down_mid: float = 0.0
    up_best_bid: float = 0.0
    up_best_ask: float = 0.0
    down_best_bid: float = 0.0
    down_best_ask: float = 0.0
    last_update: float = 0.0
    ws_connected: bool = False
    update_count: int = 0


@dataclass
class BotState:
    balance: float = DEFAULT_BALANCE
    initial_balance: float = DEFAULT_BALANCE
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    trades: list = field(default_factory=list)


# ── Market Finder (5-min) ────────────────────────────────────────────────────

class MarketFinder5m:
    def __init__(self):
        self.client = httpx.Client(timeout=15.0)

    def _get_server_time(self) -> float:
        try:
            resp = self.client.get(f"{CLOB_API}/time", timeout=5.0)
            if resp.status_code == 200:
                return float(resp.text.strip().strip('"'))
        except Exception:
            pass
        return datetime.now(timezone.utc).timestamp()

    def _window_start_ts(self, base_ts: float, offset_min: int = 0) -> int:
        t = datetime.fromtimestamp(base_ts, tz=timezone.utc) + timedelta(minutes=offset_min)
        rounded_min = (t.minute // 5) * 5
        interval_start = t.replace(minute=rounded_min, second=0, microsecond=0)
        return int(interval_start.timestamp())

    def _parse_market(self, m: dict) -> dict | None:
        clob_token_ids = m.get("clobTokenIds", "")
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except Exception:
                clob_token_ids = []

        outcomes = m.get("outcomes", "")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        prices = m.get("outcomePrices", "")
        if isinstance(prices, str):
            try:
                prices = [float(p) for p in json.loads(prices)]
            except Exception:
                prices = []

        return {
            "slug": m.get("slug", ""),
            "question": m.get("question", ""),
            "condition_id": m.get("conditionId", ""),
            "token_ids": clob_token_ids,
            "outcomes": outcomes,
            "prices": prices,
            "end_date": m.get("endDate", ""),
        }

    def find_current_btc_5m(self, quiet: bool = False) -> dict | None:
        now_ts = self._get_server_time()
        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        if not quiet:
            print(f"  Серверное время: {now_dt.strftime('%H:%M:%S')} UTC")

        for offset in [0, -5, 5]:
            ts = self._window_start_ts(now_ts, offset)
            slug = f"btc-updown-5m-{ts}"
            try:
                resp = self.client.get(f"{GAMMA_API}/markets", params={"slug": slug})
                if resp.status_code != 200:
                    continue
                data = resp.json()
                markets = data if isinstance(data, list) else [data]
                for m in markets:
                    if not m or m.get("slug") != slug:
                        continue
                    if m.get("closed") or not m.get("acceptingOrders", False):
                        continue
                    if not quiet:
                        print(f"  ✓ Найден: {slug}")
                    return self._parse_market(m)
            except Exception:
                continue

        # Фолбэк
        try:
            resp = self.client.get(f"{GAMMA_API}/markets", params={
                "active": "true", "closed": "false",
                "limit": 50, "order": "startDate", "ascending": "false",
            })
            resp.raise_for_status()
            for m in resp.json():
                s = m.get("slug", "")
                q = m.get("question", "").lower()
                if ("btc-updown-5m" in s or
                    ("bitcoin" in q and "5" in q and ("up" in q or "down" in q))):
                    if not m.get("closed") and m.get("acceptingOrders", False):
                        if not quiet:
                            print(f"  ✓ Фолбэк: {s}")
                        return self._parse_market(m)
        except Exception:
            pass
        return None

    def check_resolution(self, slug: str) -> str | None:
        try:
            resp = self.client.get(f"{GAMMA_API}/markets", params={"slug": slug})
            if resp.status_code != 200:
                return None
            data = resp.json()
            markets = data if isinstance(data, list) else [data]
            for m in markets:
                if m.get("slug") != slug:
                    continue
                prices = m.get("outcomePrices", "")
                if isinstance(prices, str):
                    try:
                        prices = [float(p) for p in json.loads(prices)]
                    except Exception:
                        prices = []
                outcomes = m.get("outcomes", "")
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = []
                if (m.get("closed") or m.get("resolved")) and prices:
                    for outcome, price in zip(outcomes, prices):
                        if price >= 0.95:
                            return outcome
        except Exception:
            pass
        return None


# ── Tail Hunter Bot ──────────────────────────────────────────────────────────

class TailHunterBot:

    def __init__(self, balance: float, bet_size: float, threshold: float):
        self.finder = MarketFinder5m()
        self.http = httpx.Client(timeout=10.0)

        self.state = BotState(balance=balance, initial_balance=balance)
        self.bet_size = bet_size
        self.threshold = threshold
        self.running = False

        self.prices = LivePrices()
        self.current_slug = ""
        self.current_market: dict | None = None
        self.pending_trades: list[Trade] = []
        self.bought_this_window = False

        self.log_file = LOG_DIR / f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self._init_log()

    def _init_log(self):
        with open(self.log_file, "w") as f:
            f.write("timestamp,window_slug,side,entry_price,shares,"
                    "bet_cost,result,payout,pnl,balance\n")

    def _log_trade(self, trade: Trade):
        with open(self.log_file, "a") as f:
            f.write(f"{trade.timestamp},{trade.window_slug},{trade.side},"
                    f"{trade.entry_price:.4f},{trade.shares:.2f},"
                    f"{trade.bet_cost:.4f},{trade.result},"
                    f"{trade.payout:.4f},{trade.pnl:.4f},"
                    f"{self.state.balance:.4f}\n")

    @staticmethod
    def _time_remaining() -> float:
        now = datetime.now(timezone.utc)
        rounded_min = (now.minute // 5) * 5
        window_end = now.replace(minute=rounded_min, second=0, microsecond=0) + timedelta(minutes=5)
        return max((window_end - now).total_seconds(), 0)

    @staticmethod
    def _current_window_ts() -> int:
        now = datetime.now(timezone.utc)
        rounded_min = (now.minute // 5) * 5
        ws = now.replace(minute=rounded_min, second=0, microsecond=0)
        return int(ws.timestamp())

    def _get_buy_price(self, side: str) -> float:
        if side == "UP":
            mid, ask = self.prices.up_mid, self.prices.up_best_ask
        else:
            mid, ask = self.prices.down_mid, self.prices.down_best_ask

        if mid <= 0:
            return 0.0
        # ask реалистичен только если не сильно больше midpoint
        if 0 < ask <= mid * 3 and ask <= 0.5:
            return ask
        return mid

    # ── WebSocket listener ───────────────────────────────────────────────

    async def _ws_listener(self):
        subscribed_tokens: set[str] = set()

        while self.running:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    self.prices.ws_connected = True
                    print("  📡 WebSocket подключён")

                    while self.running:
                        # Подписка на новые токены при смене рынка
                        if self.current_market:
                            tokens = set(self.current_market.get("token_ids", []))
                            if tokens != subscribed_tokens and tokens:
                                if subscribed_tokens:
                                    await ws.send(json.dumps({
                                        "type": "unsubscribe",
                                        "channel": "market",
                                        "assets_ids": list(subscribed_tokens),
                                    }))
                                await ws.send(json.dumps({
                                    "type": "subscribe",
                                    "channel": "market",
                                    "assets_ids": list(tokens),
                                }))
                                subscribed_tokens = tokens

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue

                        self._process_ws_message(raw)

            except websockets.exceptions.ConnectionClosed:
                self.prices.ws_connected = False
                print("\n  📡 WS отключён, переподключение...")
                subscribed_tokens.clear()
                await asyncio.sleep(2)
            except Exception as e:
                self.prices.ws_connected = False
                print(f"\n  [!] WS: {e}")
                subscribed_tokens.clear()
                await asyncio.sleep(3)

    def _process_ws_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        messages = data if isinstance(data, list) else [data]

        for msg in messages:
            asset_id = msg.get("asset_id", "")
            if not asset_id or not self.current_market:
                continue

            token_ids = self.current_market.get("token_ids", [])
            if len(token_ids) < 2:
                continue

            # Определяем какой токен обновился
            if asset_id == token_ids[0]:
                prefix = "up"
            elif asset_id == token_ids[1]:
                prefix = "down"
            else:
                continue

            # Обновляем цены из любых доступных полей
            if msg.get("price"):
                val = float(msg["price"])
                if prefix == "up":
                    self.prices.up_mid = val
                else:
                    self.prices.down_mid = val

            if msg.get("best_bid"):
                val = float(msg["best_bid"])
                if prefix == "up":
                    self.prices.up_best_bid = val
                else:
                    self.prices.down_best_bid = val

            if msg.get("best_ask"):
                val = float(msg["best_ask"])
                if prefix == "up":
                    self.prices.up_best_ask = val
                else:
                    self.prices.down_best_ask = val

            # Вычисляем mid из bid/ask если price не пришёл
            if not msg.get("price") and msg.get("best_bid") and msg.get("best_ask"):
                bid = float(msg["best_bid"])
                ask = float(msg["best_ask"])
                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    if prefix == "up":
                        self.prices.up_mid = mid
                    else:
                        self.prices.down_mid = mid

            self.prices.last_update = time.time()
            self.prices.update_count += 1

    # ── REST fallback ────────────────────────────────────────────────────

    def _fetch_prices_rest(self):
        if not self.current_market:
            return
        token_ids = self.current_market.get("token_ids", [])
        if len(token_ids) < 2:
            return

        for prefix, token_id in zip(["up", "down"], token_ids[:2]):
            try:
                resp = self.http.get(f"{CLOB_API}/midpoint", params={"token_id": token_id})
                if resp.status_code == 200:
                    mid = float(resp.json().get("mid", 0))
                    if prefix == "up":
                        self.prices.up_mid = mid
                    else:
                        self.prices.down_mid = mid

                resp = self.http.get(f"{CLOB_API}/book", params={"token_id": token_id})
                if resp.status_code == 200:
                    book = resp.json()
                    bids, asks = book.get("bids", []), book.get("asks", [])
                    if prefix == "up":
                        if bids: self.prices.up_best_bid = float(bids[0]["price"])
                        if asks: self.prices.up_best_ask = float(asks[0]["price"])
                    else:
                        if bids: self.prices.down_best_bid = float(bids[0]["price"])
                        if asks: self.prices.down_best_ask = float(asks[0]["price"])
            except Exception:
                pass

        self.prices.last_update = time.time()
        self.prices.update_count += 1

    # ── Trading logic ────────────────────────────────────────────────────

    def _try_buy(self) -> list[Trade]:
        if not self.current_market:
            return []
        trades = []
        for side in ["UP", "DOWN"]:
            price = self._get_buy_price(side)
            if price <= 0 or price > self.threshold:
                continue
            if self.state.balance < self.bet_size:
                break

            shares = self.bet_size / price
            trade = Trade(
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                window_slug=self.current_slug,
                side=side,
                entry_price=price,
                shares=shares,
                bet_cost=self.bet_size,
                result="PENDING",
            )
            self.state.balance -= self.bet_size
            trades.append(trade)

            potential = shares * (1 - POLYMARKET_FEE)
            mult = potential / self.bet_size

            print(f"\n\n  🎯 КУПИЛ {side} @ ${price:.4f}  |  "
                  f"${self.bet_size:.2f} → {shares:.1f} шар  |  "
                  f"x{mult:.0f} потенциал")

            send_telegram(
                f"🎯 TAIL HUNTER [PAPER]\n"
                f"{side} @ ${price:.4f} | ${self.bet_size:.2f} → {shares:.1f} шар\n"
                f"x{mult:.0f} | Окно: ...{self.current_slug[-10:]}\n"
                f"Баланс: ${self.state.balance:.2f}"
            )
        return trades

    def _resolve_trades(self):
        if not self.pending_trades:
            return
        slug = self.pending_trades[0].window_slug
        print(f"\n  ⏳ Resolution ...{slug[-10:]}...")

        resolution = None
        for attempt in range(15):
            time.sleep(4)
            resolution = self.finder.check_resolution(slug)
            if resolution:
                break
            if attempt % 3 == 0:
                print(f"  ... попытка {attempt + 1}/15")

        if not resolution:
            print(f"  ⚠️ Resolution timeout → LOSS")
            resolution = "__UNKNOWN__"

        print(f"  📢 Результат: {resolution}")

        for trade in self.pending_trades:
            won = ((trade.side == "UP" and resolution == "Up") or
                   (trade.side == "DOWN" and resolution == "Down"))

            if won:
                payout = trade.shares * (1 - POLYMARKET_FEE)
                trade.result = "WIN"
                trade.payout = payout
                trade.pnl = payout - trade.bet_cost
                self.state.balance += payout
                self.state.wins += 1
                print(f"  🏆 WIN {trade.side} → +${trade.pnl:.2f}")
                send_telegram(f"🏆 WIN {trade.side} +${trade.pnl:.2f} | ${self.state.balance:.2f}")
            else:
                trade.result = "LOSS"
                trade.pnl = -trade.bet_cost
                self.state.losses += 1
                print(f"  ❌ LOSS {trade.side} → -${trade.bet_cost:.2f}")

            self.state.total_trades += 1
            self.state.total_pnl += trade.pnl
            self.state.trades.append(trade)
            self._log_trade(trade)

        self.pending_trades = []

    def _print_status(self):
        s = self.state
        wr = (s.wins / s.total_trades * 100) if s.total_trades else 0
        roi = ((s.balance - s.initial_balance) / s.initial_balance * 100)
        print(f"\n{'─' * 60}")
        print(f"  💰 ${s.balance:.2f} (start ${s.initial_balance:.2f}, ROI {roi:+.1f}%)")
        print(f"  📊 {s.total_trades} trades  W:{s.wins} L:{s.losses}  WR:{wr:.0f}%")
        print(f"  📈 PnL: ${s.total_pnl:+.2f}")
        print(f"{'─' * 60}")

    # ── Main loop ────────────────────────────────────────────────────────

    async def _main_loop(self):
        last_rest_fetch = 0
        prev_window_ts = 0

        while self.running:
            try:
                now = time.time()
                remaining = self._time_remaining()
                window_ts = self._current_window_ts()

                # ── Новое окно ───────────────────────────────────────
                if window_ts != prev_window_ts:
                    if self.pending_trades:
                        self._resolve_trades()
                        self._print_status()
                        if self.state.balance < self.bet_size:
                            print(f"\n  ⛔ Баланс исчерпан!")
                            self.running = False
                            break

                    prev_window_ts = window_ts
                    self.bought_this_window = False
                    self.prices = LivePrices()

                    print(f"\n\n  🔍 Новое окно...")
                    market = self.finder.find_current_btc_5m()
                    if market:
                        self.current_market = market
                        self.current_slug = market["slug"]
                        short = market["slug"].split("btc-updown-5m-")[-1]
                        print(f"  📌 {short} | {market['question']}")
                    else:
                        self.current_market = None
                        print(f"  ⚠️ Рынок не найден")

                # ── REST фолбэк ─────────────────────────────────────
                ws_stale = (now - self.prices.last_update) > 5
                if (not self.prices.ws_connected or ws_stale) and \
                   (now - last_rest_fetch) >= REST_FALLBACK_INTERVAL:
                    self._fetch_prices_rest()
                    last_rest_fetch = now

                # ── Display + buy logic ──────────────────────────────
                if self.current_market and self.prices.last_update > 0:
                    buy_up = self._get_buy_price("UP")
                    buy_down = self._get_buy_price("DOWN")

                    min_p = min(
                        (p for p in [buy_up, buy_down] if p > 0), default=1.0
                    )

                    if min_p <= self.threshold:
                        ind = "🔴"
                    elif min_p <= self.threshold * 2:
                        ind = "🟡"
                    else:
                        ind = "🟢"

                    ws_tag = "WS" if self.prices.ws_connected else "REST"
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

                    if self.bought_this_window:
                        print(f"\r  ⏳ [{ts}] HOLDING... "
                              f"{remaining:.0f}с | ${self.state.balance:.2f}"
                              "        ", end="", flush=True)
                    else:
                        print(f"\r  {ind} [{ts}] "
                              f"UP={buy_up:.4f} DN={buy_down:.4f} "
                              f"| {remaining:.0f}с "
                              f"| ${self.state.balance:.2f} "
                              f"| {ws_tag} #{self.prices.update_count}"
                              "        ", end="", flush=True)

                        # ПОКУПКА
                        if min_p <= self.threshold and remaining > 10:
                            trades = self._try_buy()
                            if trades:
                                self.pending_trades = trades
                                self.bought_this_window = True

                await asyncio.sleep(0.5)

            except Exception as e:
                print(f"\n  [!] Loop: {e}")
                await asyncio.sleep(2)

    async def _run_async(self):
        self.running = True

        print("=" * 60)
        print("  🎯 TAIL HUNTER — Real-Time Paper Trading")
        print(f"  Баланс: ${self.state.balance:.2f}")
        print(f"  Ставка: ${self.bet_size:.2f}")
        print(f"  Порог: <= ${self.threshold:.4f} (x{1/self.threshold:.0f})")
        print(f"  Цены: WebSocket (real-time) + REST (fallback)")
        print(f"  Лог: {self.log_file}")
        print(f"  Ctrl+C для остановки")
        print("=" * 60)

        ws_task = asyncio.create_task(self._ws_listener())
        main_task = asyncio.create_task(self._main_loop())

        try:
            await asyncio.gather(ws_task, main_task)
        except asyncio.CancelledError:
            pass

    def run(self):
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            print("\n\n⛔ Остановлено")
            self.running = False
            if self.pending_trades:
                print("  Resolving open positions...")
                remaining = self._time_remaining()
                if remaining > 0:
                    time.sleep(remaining + WAIT_AFTER_WINDOW)
                self._resolve_trades()
        self._final_report()

    def _final_report(self):
        s = self.state
        if s.total_trades == 0:
            print(f"\n  Сделок не было. Баланс: ${s.balance:.2f}")
            return
        wr = s.wins / s.total_trades * 100
        roi = (s.balance - s.initial_balance) / s.initial_balance * 100
        print(f"\n{'═' * 60}")
        print(f"  📊 TAIL HUNTER ИТОГ")
        print(f"{'═' * 60}")
        print(f"  ${s.initial_balance:.2f} → ${s.balance:.2f} ({roi:+.1f}%)")
        print(f"  Trades: {s.total_trades}  W:{s.wins} L:{s.losses}  WR:{wr:.0f}%")
        print(f"  PnL: ${s.total_pnl:+.2f}")
        print(f"  Лог: {self.log_file}")
        print(f"{'═' * 60}")
        send_telegram(
            f"📊 TAIL HUNTER ИТОГ\n"
            f"${s.initial_balance:.2f} → ${s.balance:.2f} ({roi:+.1f}%)\n"
            f"Trades: {s.total_trades} W:{s.wins} L:{s.losses}\n"
            f"PnL: ${s.total_pnl:+.2f}"
        )


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tail Hunter — real-time paper trading")
    parser.add_argument("--balance", type=float, default=DEFAULT_BALANCE)
    parser.add_argument("--bet-size", type=float, default=DEFAULT_BET_SIZE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    bot = TailHunterBot(
        balance=args.balance,
        bet_size=args.bet_size,
        threshold=args.threshold,
    )
    bot.run()


if __name__ == "__main__":
    main()