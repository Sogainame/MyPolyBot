"""
Polymarket Copy-Trading Simulator
==================================
Симулирует копирование сделок BoneReader с начальным балансом $100.
Отслеживает реальные сделки, "покупает" пропорционально, и при резолюции
маркета считает P&L.

Запуск:
    pip install httpx rich
    python copy_sim.py --wallet 0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9
    python copy_sim.py --wallet 0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9 --duration 120
    python copy_sim.py --wallet 0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9 --balance 500

Аргументы:
    --wallet     Wallet address BoneReader (обязательно)
    --balance    Начальный баланс в USD (по умолчанию 100)
    --duration   Длительность симуляции в минутах (по умолчанию 60)
    --poll       Интервал проверки новых сделок в секундах (по умолчанию 15)
    --max-bet    Макс % баланса на одну сделку (по умолчанию 5)
"""

import httpx
import time
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ─── Config ─────────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    """Одна скопированная сделка."""
    timestamp: int
    market_title: str
    market_slug: str
    outcome: str
    side: str
    entry_price: float        # Цена покупки
    size_tokens: float        # Кол-во токенов
    cost_usdc: float          # Сколько потратили USDC
    current_price: float      # Текущая цена
    resolved: bool = False    # Маркет зарезолвился?
    resolution_price: float = 0.0  # 1.0 если выиграл, 0.0 если проиграл
    pnl: float = 0.0

    @property
    def current_value(self) -> float:
        if self.resolved:
            return self.size_tokens * self.resolution_price
        return self.size_tokens * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.current_value - self.cost_usdc

    @property
    def pnl_percent(self) -> float:
        if self.cost_usdc == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_usdc) * 100


@dataclass
class Portfolio:
    """Портфель симуляции."""
    initial_balance: float
    cash: float
    trades: list = field(default_factory=list)
    total_invested: float = 0.0
    total_returned: float = 0.0
    wins: int = 0
    losses: int = 0
    trades_copied: int = 0

    @property
    def open_value(self) -> float:
        return sum(t.current_value for t in self.trades if not t.resolved)

    @property
    def total_value(self) -> float:
        return self.cash + self.open_value

    @property
    def total_pnl(self) -> float:
        return self.total_value - self.initial_balance

    @property
    def total_pnl_percent(self) -> float:
        if self.initial_balance == 0:
            return 0.0
        return (self.total_pnl / self.initial_balance) * 100


# ─── API Client ─────────────────────────────────────────────────────────────

class PolymarketClient:
    def __init__(self, timeout: float = 30.0):
        self.http = httpx.Client(timeout=timeout)

    def close(self):
        self.http.close()

    def get_activity(self, wallet: str, limit: int = 50) -> list[dict]:
        """Последние сделки пользователя."""
        try:
            resp = self.http.get(
                f"{DATA_API}/activity",
                params={
                    "user": wallet,
                    "limit": limit,
                    "type": "TRADE",
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                },
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"  [!] Activity API error: {e}")
        return []

    def get_positions(self, wallet: str, limit: int = 100) -> list[dict]:
        """Текущие позиции пользователя (для проверки текущих цен)."""
        try:
            resp = self.http.get(
                f"{DATA_API}/positions",
                params={"user": wallet, "limit": limit},
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"  [!] Positions API error: {e}")
        return []

    def get_market_price(self, condition_id: str) -> Optional[float]:
        """Пробуем получить текущую цену маркета."""
        # Через позиции BoneReader — берём curPrice
        # Это fallback, основная цена берётся из его позиций
        return None

    def get_closed_positions(self, wallet: str, limit: int = 200) -> list[dict]:
        """Закрытые позиции — для определения резолюции маркетов."""
        try:
            resp = self.http.get(
                f"{DATA_API}/closed-positions",
                params={"user": wallet, "limit": limit},
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"  [!] Closed positions error: {e}")
        return []


# ─── Display ────────────────────────────────────────────────────────────────

def _try_rich():
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich import box
        return Console(), Table, Panel, box
    except ImportError:
        return None, None, None, None

CONSOLE, RichTable, Panel, box = _try_rich()

def cprint(msg: str, style: str = ""):
    if CONSOLE:
        CONSOLE.print(f"[{style}]{msg}[/]" if style else msg)
    else:
        print(msg)

def print_header(text: str):
    sep = "═" * 60
    cprint(f"\n{sep}", "bold cyan")
    cprint(f"  {text}", "bold white")
    cprint(sep, "bold cyan")
    print()

def print_portfolio_status(portfolio: Portfolio, elapsed_min: float, total_min: float):
    """Компактный статус портфеля."""
    remaining = max(0, total_min - elapsed_min)
    pnl_sign = "+" if portfolio.total_pnl >= 0 else ""
    pnl_color = "green" if portfolio.total_pnl >= 0 else "red"

    print()
    cprint("  ┌─────────────────────────────────────────────┐", "dim")
    cprint(f"  │  ⏱  {elapsed_min:.0f}мин / {total_min:.0f}мин  (осталось {remaining:.0f}мин)", "dim")
    cprint(f"  │  💰 Баланс:    ${portfolio.total_value:>10,.2f}", "bold")
    cprint(f"  │  📊 PnL:       {pnl_sign}${portfolio.total_pnl:>9,.2f} ({pnl_sign}{portfolio.total_pnl_percent:.2f}%)", f"bold {pnl_color}")
    cprint(f"  │  💵 Кэш:       ${portfolio.cash:>10,.2f}", "")
    cprint(f"  │  📈 В позициях: ${portfolio.open_value:>10,.2f}", "")
    cprint(f"  │  🔄 Сделок:    {portfolio.trades_copied}   ✅ {portfolio.wins}  ❌ {portfolio.losses}", "")
    cprint("  └─────────────────────────────────────────────┘", "dim")
    print()


def print_trade_event(trade: SimTrade, action: str):
    """Вывод события: новая сделка или резолюция."""
    dt = datetime.fromtimestamp(trade.timestamp, tz=timezone.utc)
    time_str = dt.strftime("%H:%M:%S")

    if action == "COPY":
        side_icon = "🟢" if trade.side == "BUY" else "🔴"
        cprint(
            f"  [{time_str}] {side_icon} COPY  {trade.outcome:5s} "
            f"${trade.cost_usdc:>7,.2f} @ {trade.entry_price:.4f}  "
            f"{trade.market_title[:45]}",
            "cyan"
        )
    elif action == "WIN":
        profit = trade.current_value - trade.cost_usdc
        cprint(
            f"  [{time_str}] ✅ WIN   {trade.outcome:5s} "
            f"+${profit:>7,.2f}  "
            f"{trade.market_title[:45]}",
            "green"
        )
    elif action == "LOSS":
        loss = trade.cost_usdc - trade.current_value
        cprint(
            f"  [{time_str}] ❌ LOSS  {trade.outcome:5s} "
            f"-${loss:>7,.2f}  "
            f"{trade.market_title[:45]}",
            "red"
        )


def print_final_report(portfolio: Portfolio, duration_min: float):
    """Финальный отчёт по симуляции."""
    print_header("ФИНАЛЬНЫЙ ОТЧЁТ СИМУЛЯЦИИ")

    pnl_sign = "+" if portfolio.total_pnl >= 0 else ""
    pnl_color = "bold green" if portfolio.total_pnl >= 0 else "bold red"

    cprint(f"  Длительность:        {duration_min:.0f} минут", "")
    cprint(f"  Начальный баланс:    ${portfolio.initial_balance:,.2f}", "")
    cprint(f"  Финальный баланс:    ${portfolio.total_value:,.2f}", "bold")
    cprint(f"  P&L:                 {pnl_sign}${portfolio.total_pnl:,.2f} ({pnl_sign}{portfolio.total_pnl_percent:.2f}%)", pnl_color)
    print()
    cprint(f"  Всего скопировано:   {portfolio.trades_copied} сделок", "")
    cprint(f"  Выигрышей:           {portfolio.wins}", "green")
    cprint(f"  Проигрышей:          {portfolio.losses}", "red")

    win_rate = portfolio.wins / max(portfolio.wins + portfolio.losses, 1) * 100
    cprint(f"  Win rate:            {win_rate:.1f}%", "")

    cprint(f"  Кэш:                ${portfolio.cash:,.2f}", "")
    cprint(f"  В открытых позициях: ${portfolio.open_value:,.2f}", "")

    if portfolio.total_pnl > 0:
        annual = portfolio.total_pnl_percent * (525600 / max(duration_min, 1))  # % в год
        cprint(f"\n  📈 Экстраполяция:    {annual:,.0f}% годовых (грубая оценка)", "yellow")
    print()

    # Таблица сделок
    if portfolio.trades:
        print_header("ДЕТАЛИ СДЕЛОК")
        if CONSOLE and RichTable:
            table = RichTable(title="Все сделки симуляции", box=box.ROUNDED, show_lines=True)
            table.add_column("Время", style="dim", width=10)
            table.add_column("Маркет", max_width=40)
            table.add_column("Side", width=6)
            table.add_column("Cost", justify="right", width=10)
            table.add_column("Entry", justify="right", width=8)
            table.add_column("Exit/Cur", justify="right", width=8)
            table.add_column("PnL $", justify="right", width=10)
            table.add_column("Status", width=8)

            for t in portfolio.trades:
                dt = datetime.fromtimestamp(t.timestamp, tz=timezone.utc)
                pnl = t.unrealized_pnl
                pnl_c = "green" if pnl >= 0 else "red"
                exit_p = t.resolution_price if t.resolved else t.current_price
                status = "✅ WIN" if t.resolved and pnl >= 0 else "❌ LOSS" if t.resolved else "⏳ OPEN"

                table.add_row(
                    dt.strftime("%H:%M:%S"),
                    t.market_title[:40],
                    t.outcome,
                    f"${t.cost_usdc:.2f}",
                    f"{t.entry_price:.4f}",
                    f"{exit_p:.4f}",
                    f"[{pnl_c}]{'+' if pnl >= 0 else ''}{pnl:.2f}[/]",
                    status,
                )
            CONSOLE.print(table)
        else:
            for t in portfolio.trades:
                dt = datetime.fromtimestamp(t.timestamp, tz=timezone.utc)
                pnl = t.unrealized_pnl
                status = "WIN" if t.resolved and pnl >= 0 else "LOSS" if t.resolved else "OPEN"
                print(
                    f"  {dt.strftime('%H:%M:%S')}  {status:5s}  "
                    f"${t.cost_usdc:>7.2f} → ${t.current_value:>7.2f}  "
                    f"PnL: {'+'if pnl>=0 else ''}{pnl:.2f}  "
                    f"{t.market_title[:40]}"
                )


# ─── Simulation Engine ─────────────────────────────────────────────────────

class CopySimulator:
    def __init__(
        self,
        client: PolymarketClient,
        wallet: str,
        initial_balance: float = 100.0,
        max_bet_pct: float = 5.0,
        duration_min: float = 60.0,
        poll_interval: int = 15,
    ):
        self.client = client
        self.wallet = wallet
        self.duration_min = duration_min
        self.poll_interval = poll_interval
        self.max_bet_pct = max_bet_pct

        self.portfolio = Portfolio(
            initial_balance=initial_balance,
            cash=initial_balance,
        )

        # Трекинг — чтобы не дублировать сделки
        self.seen_tx_hashes: set[str] = set()

        # Маппинг slug → текущая цена (обновляем из позиций BoneReader)
        self.price_cache: dict[str, float] = {}

        # Маппинг slug+outcome → наша SimTrade (для обновления цен)
        self.open_trades: dict[str, SimTrade] = {}

    def _calculate_bet_size(self, bone_usdc_size: float) -> float:
        """
        Рассчитываем размер нашей ставки.
        Пропорционально от баланса BoneReader (~$14K active) к нашему.
        Но не больше max_bet_pct% от текущего баланса.
        """
        max_bet = self.portfolio.cash * (self.max_bet_pct / 100)

        # Пропорция: BoneReader оперирует ~$14K, мы — наш баланс
        # Но проще: берём фиксированный % от кэша на каждую сделку
        # BoneReader делает ~10-50 сделок в час, поэтому 2-3% на сделку разумно
        bet = min(
            self.portfolio.cash * 0.02,  # 2% от кэша
            max_bet,
            self.portfolio.cash,          # Не больше чем есть
        )

        return max(bet, 0)

    def copy_trade(self, raw_trade: dict):
        """Копируем сделку BoneReader."""
        tx_hash = raw_trade.get("transactionHash", "")
        if tx_hash in self.seen_tx_hashes:
            return
        self.seen_tx_hashes.add(tx_hash)

        side = raw_trade.get("side", "BUY")
        price = float(raw_trade.get("price", 0))
        title = raw_trade.get("title", "Unknown")
        slug = raw_trade.get("slug", "")
        outcome = raw_trade.get("outcome", "")
        timestamp = int(raw_trade.get("timestamp", time.time()))

        if price <= 0 or price >= 1.0:
            return  # Некорректная цена

        # Считаем размер ставки
        bet_usdc = self._calculate_bet_size(float(raw_trade.get("usdcSize", 0)))
        if bet_usdc < 0.01:
            return  # Слишком маленькая ставка

        # Покупаем токены
        tokens = bet_usdc / price
        self.portfolio.cash -= bet_usdc

        trade = SimTrade(
            timestamp=timestamp,
            market_title=title,
            market_slug=slug,
            outcome=outcome,
            side=side,
            entry_price=price,
            size_tokens=tokens,
            cost_usdc=bet_usdc,
            current_price=price,
        )

        trade_key = f"{slug}:{outcome}"
        self.open_trades[trade_key] = trade
        self.portfolio.trades.append(trade)
        self.portfolio.trades_copied += 1
        self.portfolio.total_invested += bet_usdc

        print_trade_event(trade, "COPY")

    def update_prices(self):
        """Обновляем цены открытых позиций из позиций BoneReader."""
        positions = self.client.get_positions(self.wallet, limit=100)
        price_map: dict[str, float] = {}
        for p in positions:
            slug = p.get("slug", "")
            outcome = p.get("outcome", "")
            cur_price = float(p.get("curPrice", 0))
            key = f"{slug}:{outcome}"
            price_map[key] = cur_price

        for key, trade in self.open_trades.items():
            if not trade.resolved and key in price_map:
                trade.current_price = price_map[key]

    def check_resolutions(self):
        """
        Проверяем зарезолвились ли маркеты.
        Если curPrice ~1.0 — выигрыш. Если ~0.0 — проигрыш.
        Если маркет пропал из открытых позиций BoneReader — он зарезолвился.
        """
        # Получаем текущие открытые позиции BoneReader
        positions = self.client.get_positions(self.wallet, limit=200)
        open_slugs = set()
        for p in positions:
            slug = p.get("slug", "")
            outcome = p.get("outcome", "")
            open_slugs.add(f"{slug}:{outcome}")

        # Проверяем наши открытые сделки
        for key, trade in list(self.open_trades.items()):
            if trade.resolved:
                continue

            # Если маркет пропал из позиций BoneReader — зарезолвился
            if key not in open_slugs:
                # Определяем результат по последней цене
                if trade.current_price >= 0.95:
                    # Выигрыш — токен зарезолвился в $1
                    trade.resolved = True
                    trade.resolution_price = 1.0
                    returned = trade.size_tokens * 1.0
                    self.portfolio.cash += returned
                    self.portfolio.total_returned += returned
                    self.portfolio.wins += 1
                    trade.pnl = returned - trade.cost_usdc
                    print_trade_event(trade, "WIN")
                elif trade.current_price <= 0.05:
                    # Проигрыш — токен зарезолвился в $0
                    trade.resolved = True
                    trade.resolution_price = 0.0
                    self.portfolio.losses += 1
                    trade.pnl = -trade.cost_usdc
                    print_trade_event(trade, "LOSS")
                else:
                    # Непонятно — может BoneReader продал, а маркет ещё открыт
                    # Считаем по текущей цене и закрываем
                    trade.resolved = True
                    trade.resolution_price = trade.current_price
                    returned = trade.size_tokens * trade.current_price
                    self.portfolio.cash += returned
                    self.portfolio.total_returned += returned
                    if returned >= trade.cost_usdc:
                        self.portfolio.wins += 1
                        print_trade_event(trade, "WIN")
                    else:
                        self.portfolio.losses += 1
                        print_trade_event(trade, "LOSS")
                    trade.pnl = returned - trade.cost_usdc

    def run(self):
        """Основной цикл симуляции."""
        print_header("COPY-TRADING СИМУЛЯЦИЯ")
        cprint(f"  Цель:      @BoneReader", "bold")
        cprint(f"  Wallet:    {self.wallet}", "dim")
        cprint(f"  Баланс:    ${self.portfolio.initial_balance:,.2f}", "bold green")
        cprint(f"  Duration:  {self.duration_min:.0f} минут", "")
        cprint(f"  Max bet:   {self.max_bet_pct}% от баланса", "")
        cprint(f"  Poll:      каждые {self.poll_interval}с", "")
        print()
        cprint("  Ctrl+C для досрочной остановки\n", "dim")

        start_time = time.time()
        end_time = start_time + (self.duration_min * 60)

        # Инициализация — запоминаем уже существующие сделки
        initial = self.client.get_activity(self.wallet, limit=50)
        for t in initial:
            self.seen_tx_hashes.add(t.get("transactionHash", ""))
        cprint(f"  Инициализировано: {len(self.seen_tx_hashes)} existing trades\n", "dim")

        status_interval = 300  # Каждые 5 минут печатаем статус
        last_status = start_time

        try:
            while time.time() < end_time:
                now = time.time()
                elapsed_min = (now - start_time) / 60

                # 1. Проверяем новые сделки BoneReader
                trades = self.client.get_activity(self.wallet, limit=50)
                for raw in trades:
                    self.copy_trade(raw)

                # 2. Обновляем цены
                self.update_prices()

                # 3. Проверяем резолюции
                self.check_resolutions()

                # 4. Периодический статус
                if now - last_status >= status_interval:
                    print_portfolio_status(self.portfolio, elapsed_min, self.duration_min)
                    last_status = now

                # 5. Ждём
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            cprint("\n\n  ⚠️  Симуляция остановлена досрочно!", "yellow bold")

        # Финальное обновление цен
        self.update_prices()

        actual_duration = (time.time() - start_time) / 60
        print_final_report(self.portfolio, actual_duration)


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Copy-Trading Simulator — копируем BoneReader с $100"
    )
    parser.add_argument(
        "--wallet", type=str, required=True,
        help="Proxy wallet address BoneReader"
    )
    parser.add_argument(
        "--balance", type=float, default=100.0,
        help="Начальный баланс USD (по умолчанию 100)"
    )
    parser.add_argument(
        "--duration", type=float, default=60.0,
        help="Длительность симуляции в минутах (по умолчанию 60)"
    )
    parser.add_argument(
        "--poll", type=int, default=15,
        help="Интервал проверки сделок в секундах (по умолчанию 15)"
    )
    parser.add_argument(
        "--max-bet", type=float, default=5.0,
        help="Макс %% от баланса на одну сделку (по умолчанию 5)"
    )
    args = parser.parse_args()

    if not args.wallet.startswith("0x") or len(args.wallet) != 42:
        print("  [!] Некорректный wallet address. Должен быть 0x + 40 hex символов.")
        sys.exit(1)

    client = PolymarketClient()
    try:
        sim = CopySimulator(
            client=client,
            wallet=args.wallet,
            initial_balance=args.balance,
            max_bet_pct=args.max_bet,
            duration_min=args.duration,
            poll_interval=args.poll,
        )
        sim.run()
    finally:
        client.close()


if __name__ == "__main__":
    main()