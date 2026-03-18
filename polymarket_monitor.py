"""
Polymarket BoneReader Monitor
=============================
Мониторинг сделок трейдера BoneReader на Polymarket.

Функции:
1. Находит wallet address по username
2. Загружает текущие позиции и историю активности
3. Анализирует паттерны: win rate, размеры позиций, направления ставок
4. Непрерывный мониторинг новых сделок с уведомлениями в консоль

Требования:
    pip install httpx rich

Запуск:
    python polymarket_monitor.py              # Полный анализ + мониторинг
    python polymarket_monitor.py --analyze    # Только анализ без мониторинга
    python polymarket_monitor.py --monitor    # Только мониторинг новых сделок
"""

import httpx
import json
import time
import argparse
import sys
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ─── Config ─────────────────────────────────────────────────────────────────

TARGET_USERNAME = "BoneReader"
POLL_INTERVAL_SEC = 30  # Как часто проверять новые сделки

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ─── Data classes ───────────────────────────────────────────────────────────

@dataclass
class TraderProfile:
    username: str
    proxy_wallet: str
    joined: str
    bio: str = ""

@dataclass
class Position:
    title: str
    slug: str
    outcome: str
    size: float
    avg_price: float
    current_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float

@dataclass
class Trade:
    timestamp: int
    title: str
    side: str  # BUY / SELL
    outcome: str
    size: float
    price: float
    usdc_size: float
    tx_hash: str

@dataclass
class AnalysisResult:
    total_trades: int = 0
    total_positions: int = 0
    active_positions: int = 0
    total_value: float = 0.0
    total_pnl: float = 0.0
    markets_traded: dict = field(default_factory=dict)
    buy_count: int = 0
    sell_count: int = 0
    avg_trade_size: float = 0.0
    trades_by_hour: dict = field(default_factory=lambda: defaultdict(int))
    crypto_markets: list = field(default_factory=list)

# ─── API Client ─────────────────────────────────────────────────────────────

class PolymarketClient:
    """Обёртка над Polymarket REST API (без аутентификации — только чтение)."""

    def __init__(self, timeout: float = 30.0):
        self.http = httpx.Client(timeout=timeout)

    def close(self):
        self.http.close()

    # --- Profile resolution ---

    def resolve_wallet(self, username: str) -> Optional[TraderProfile]:
        """
        Polymarket не имеет прямого эндпоинта поиска по username.
        Стратегия: ищем через search endpoint, затем через leaderboard.
        """
        # Способ 1: Search API
        profile = self._try_search(username)
        if profile:
            return profile

        # Способ 2: Profile page scraping через Gamma
        profile = self._try_gamma_profiles(username)
        if profile:
            return profile

        return None

    def _try_search(self, username: str) -> Optional[TraderProfile]:
        """Пробуем найти через Gamma search."""
        try:
            resp = self.http.get(
                f"{GAMMA_API}/search",
                params={"query": username, "limit": 10}
            )
            if resp.status_code == 200:
                data = resp.json()
                # Search может вернуть profiles в результатах
                profiles = data if isinstance(data, list) else data.get("profiles", [])
                for p in profiles:
                    pseudo = p.get("pseudonym", "") or p.get("name", "")
                    if pseudo.lower() == username.lower():
                        return TraderProfile(
                            username=pseudo,
                            proxy_wallet=p.get("proxyWallet", ""),
                            joined=p.get("createdAt", ""),
                            bio=p.get("bio", ""),
                        )
        except Exception as e:
            print(f"  [!] Search API error: {e}")
        return None

    def _try_gamma_profiles(self, username: str) -> Optional[TraderProfile]:
        """Пробуем Gamma profiles endpoint."""
        try:
            resp = self.http.get(
                f"{GAMMA_API}/profiles",
                params={"pseudonym": username}
            )
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                items = data if isinstance(data, list) else [data]
                for p in items:
                    return TraderProfile(
                        username=p.get("pseudonym", username),
                        proxy_wallet=p.get("proxyWallet", ""),
                        joined=p.get("createdAt", ""),
                        bio=p.get("bio", ""),
                    )
        except Exception as e:
            print(f"  [!] Gamma profiles error: {e}")
        return None

    # --- Positions ---

    def get_positions(self, wallet: str, limit: int = 100) -> list[Position]:
        """Текущие открытые позиции."""
        try:
            resp = self.http.get(
                f"{DATA_API}/positions",
                params={"user": wallet, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC"}
            )
            if resp.status_code != 200:
                print(f"  [!] Positions API returned {resp.status_code}")
                return []
            data = resp.json()
            return [
                Position(
                    title=p.get("title", ""),
                    slug=p.get("slug", ""),
                    outcome=p.get("outcome", ""),
                    size=float(p.get("size", 0)),
                    avg_price=float(p.get("avgPrice", 0)),
                    current_price=float(p.get("curPrice", 0)),
                    initial_value=float(p.get("initialValue", 0)),
                    current_value=float(p.get("currentValue", 0)),
                    cash_pnl=float(p.get("cashPnl", 0)),
                    percent_pnl=float(p.get("percentPnl", 0)),
                )
                for p in data
            ]
        except Exception as e:
            print(f"  [!] Error fetching positions: {e}")
            return []

    def get_closed_positions(self, wallet: str, limit: int = 200) -> list[dict]:
        """Закрытые позиции."""
        try:
            resp = self.http.get(
                f"{DATA_API}/closed-positions",
                params={"user": wallet, "limit": limit}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"  [!] Error fetching closed positions: {e}")
        return []

    # --- Activity / Trades ---

    def get_activity(
        self,
        wallet: str,
        limit: int = 500,
        activity_type: str = "TRADE",
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> list[Trade]:
        """История сделок пользователя."""
        params = {
            "user": wallet,
            "limit": limit,
            "type": activity_type,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        try:
            resp = self.http.get(f"{DATA_API}/activity", params=params)
            if resp.status_code != 200:
                print(f"  [!] Activity API returned {resp.status_code}")
                return []
            data = resp.json()
            return [
                Trade(
                    timestamp=int(t.get("timestamp", 0)),
                    title=t.get("title", ""),
                    side=t.get("side", ""),
                    outcome=t.get("outcome", ""),
                    size=float(t.get("size", 0)),
                    price=float(t.get("price", 0)),
                    usdc_size=float(t.get("usdcSize", 0)),
                    tx_hash=t.get("transactionHash", ""),
                )
                for t in data
            ]
        except Exception as e:
            print(f"  [!] Error fetching activity: {e}")
            return []

    # --- Value ---

    def get_total_value(self, wallet: str) -> float:
        try:
            resp = self.http.get(f"{DATA_API}/value", params={"user": wallet})
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return float(data[0].get("value", 0))
                return float(data.get("value", 0))
        except Exception:
            pass
        return 0.0


# ─── Analysis ───────────────────────────────────────────────────────────────

def analyze_trades(trades: list[Trade], positions: list[Position]) -> AnalysisResult:
    """Анализ паттернов торговли."""
    result = AnalysisResult()
    result.total_trades = len(trades)
    result.active_positions = len(positions)

    if not trades:
        return result

    # Buy/Sell ratio
    for t in trades:
        if t.side == "BUY":
            result.buy_count += 1
        else:
            result.sell_count += 1

        # Группируем по маркетам
        if t.title not in result.markets_traded:
            result.markets_traded[t.title] = {"buys": 0, "sells": 0, "volume": 0.0}
        result.markets_traded[t.title]["buys" if t.side == "BUY" else "sells"] += 1
        result.markets_traded[t.title]["volume"] += t.usdc_size

        # Время торговли (час UTC)
        dt = datetime.fromtimestamp(t.timestamp, tz=timezone.utc)
        result.trades_by_hour[dt.hour] += 1

    # Средний размер сделки
    total_volume = sum(t.usdc_size for t in trades)
    result.avg_trade_size = total_volume / len(trades) if trades else 0

    # PnL по позициям
    result.total_pnl = sum(p.cash_pnl for p in positions)
    result.total_value = sum(p.current_value for p in positions)

    # Крипто-маркеты
    crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"]
    for title, stats in result.markets_traded.items():
        title_lower = title.lower()
        if any(kw in title_lower for kw in crypto_keywords):
            result.crypto_markets.append({"title": title, **stats})

    return result


# ─── Display (uses rich if available, fallback to plain) ────────────────────

def _try_import_rich():
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich import box
        return Console(), Table, Panel, box
    except ImportError:
        return None, None, None, None


CONSOLE, Table, Panel, box = _try_import_rich()


def print_header(text: str):
    if CONSOLE:
        CONSOLE.print(f"\n[bold cyan]{'═' * 60}[/]")
        CONSOLE.print(f"[bold white]  {text}[/]")
        CONSOLE.print(f"[bold cyan]{'═' * 60}[/]\n")
    else:
        print(f"\n{'═' * 60}")
        print(f"  {text}")
        print(f"{'═' * 60}\n")


def print_profile(profile: TraderProfile):
    print_header(f"Профиль: {profile.username}")
    print(f"  Wallet:  {profile.proxy_wallet}")
    print(f"  Joined:  {profile.joined}")
    if profile.bio:
        print(f"  Bio:     {profile.bio}")


def print_positions(positions: list[Position]):
    print_header("Текущие открытые позиции")
    if not positions:
        print("  Нет открытых позиций")
        return

    if CONSOLE and Table:
        table = Table(title="Active Positions", box=box.ROUNDED, show_lines=True)
        table.add_column("Market", style="white", max_width=40)
        table.add_column("Side", style="cyan")
        table.add_column("Size", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("Cur Price", justify="right")
        table.add_column("PnL $", justify="right")
        table.add_column("PnL %", justify="right")

        for p in positions[:30]:
            pnl_color = "green" if p.cash_pnl >= 0 else "red"
            table.add_row(
                p.title[:40],
                p.outcome,
                f"{p.size:,.0f}",
                f"${p.avg_price:.4f}",
                f"${p.current_price:.4f}",
                f"[{pnl_color}]${p.cash_pnl:,.2f}[/]",
                f"[{pnl_color}]{p.percent_pnl:,.1f}%[/]",
            )
        CONSOLE.print(table)
    else:
        for p in positions[:30]:
            sign = "+" if p.cash_pnl >= 0 else ""
            print(f"  {p.title[:50]:50s} | {p.outcome:4s} | {sign}${p.cash_pnl:>10,.2f} ({p.percent_pnl:>6.1f}%)")


def print_analysis(result: AnalysisResult):
    print_header("Анализ торговли")

    print(f"  Всего сделок:          {result.total_trades}")
    print(f"  BUY / SELL:            {result.buy_count} / {result.sell_count}")
    ratio = result.buy_count / max(result.sell_count, 1)
    print(f"  Buy/Sell ratio:        {ratio:.2f}")
    print(f"  Средний размер:        ${result.avg_trade_size:,.2f}")
    print(f"  Активных позиций:      {result.active_positions}")
    print(f"  Уникальных маркетов:   {len(result.markets_traded)}")

    # Топ маркеты по объёму
    print("\n  ── Топ-10 маркетов по объёму ──")
    sorted_markets = sorted(
        result.markets_traded.items(), key=lambda x: x[1]["volume"], reverse=True
    )
    for title, stats in sorted_markets[:10]:
        print(f"    ${stats['volume']:>12,.0f}  B:{stats['buys']:>5d}  S:{stats['sells']:>5d}  {title[:55]}")

    # Крипто-маркеты
    if result.crypto_markets:
        print("\n  ── Крипто-маркеты ──")
        for m in sorted(result.crypto_markets, key=lambda x: x["volume"], reverse=True)[:15]:
            print(f"    ${m['volume']:>12,.0f}  B:{m['buys']:>5d}  S:{m['sells']:>5d}  {m['title'][:55]}")

    # Активность по часам UTC
    if result.trades_by_hour:
        print("\n  ── Активность по часам (UTC) ──")
        max_count = max(result.trades_by_hour.values())
        for hour in range(24):
            count = result.trades_by_hour.get(hour, 0)
            bar_len = int(count / max(max_count, 1) * 30)
            bar = "█" * bar_len
            print(f"    {hour:02d}:00  {bar:30s}  {count}")


def print_new_trade(trade: Trade):
    """Вывод новой сделки при мониторинге."""
    dt = datetime.fromtimestamp(trade.timestamp, tz=timezone.utc)
    time_str = dt.strftime("%H:%M:%S")
    side_icon = "🟢 BUY " if trade.side == "BUY" else "🔴 SELL"

    msg = (
        f"  [{time_str}] {side_icon}  "
        f"{trade.outcome:4s}  "
        f"${trade.usdc_size:>10,.2f}  "
        f"@{trade.price:.4f}  "
        f"{trade.title[:50]}"
    )

    if CONSOLE:
        color = "green" if trade.side == "BUY" else "red"
        CONSOLE.print(f"[{color}]{msg}[/]")
    else:
        print(msg)


# ─── Monitor loop ──────────────────────────────────────────────────────────

def monitor_loop(client: PolymarketClient, wallet: str, poll_interval: int = 30):
    """Непрерывный мониторинг новых сделок."""
    print_header(f"Мониторинг сделок (каждые {poll_interval}с)")
    print("  Ctrl+C для остановки\n")

    seen_hashes: set[str] = set()

    # Загружаем последние сделки чтобы не дублировать
    initial_trades = client.get_activity(wallet, limit=50)
    for t in initial_trades:
        seen_hashes.add(t.tx_hash)
    print(f"  Инициализировано: {len(seen_hashes)} known trades\n")

    while True:
        try:
            trades = client.get_activity(wallet, limit=50)
            new_trades = [t for t in trades if t.tx_hash not in seen_hashes]

            if new_trades:
                # Выводим от старых к новым
                for t in reversed(new_trades):
                    print_new_trade(t)
                    seen_hashes.add(t.tx_hash)

            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n\n  Мониторинг остановлен.")
            break
        except Exception as e:
            print(f"  [!] Error: {e}. Retrying in {poll_interval}s...")
            time.sleep(poll_interval)


# ─── Manual wallet input fallback ──────────────────────────────────────────

def prompt_wallet() -> str:
    """Если API не нашёл кошелёк — просим ввести вручную."""
    print("\n  Не удалось автоматически найти wallet через API.")
    print("  Как найти вручную:")
    print("    1. Открой https://polymarket.com/@BoneReader")
    print("    2. DevTools → Network → ищи запросы к data-api.polymarket.com")
    print("    3. Параметр 'user' в URL = proxy wallet address")
    print("    Или: polymarketanalytics.com → Traders → найди BoneReader → wallet в URL")
    print()
    wallet = input("  Введи wallet address (0x...): ").strip()
    if not wallet.startswith("0x") or len(wallet) != 42:
        print("  [!] Некорректный адрес. Должен быть 0x + 40 hex символов.")
        sys.exit(1)
    return wallet


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket BoneReader Monitor")
    parser.add_argument("--analyze", action="store_true", help="Только анализ")
    parser.add_argument("--monitor", action="store_true", help="Только мониторинг")
    parser.add_argument("--wallet", type=str, help="Wallet address (если известен)")
    parser.add_argument("--user", type=str, default=TARGET_USERNAME, help="Username")
    parser.add_argument("--trades-limit", type=int, default=500, help="Сколько сделок загрузить для анализа")
    parser.add_argument("--poll", type=int, default=POLL_INTERVAL_SEC, help="Интервал мониторинга (сек)")
    args = parser.parse_args()

    poll_interval = args.poll

    client = PolymarketClient()

    try:
        # 1. Resolve wallet
        wallet = args.wallet
        if not wallet:
            print(f"\n  Ищу wallet для @{args.user}...")
            profile = client.resolve_wallet(args.user)
            if profile and profile.proxy_wallet:
                wallet = profile.proxy_wallet
                print_profile(profile)
            else:
                wallet = prompt_wallet()
        else:
            print(f"\n  Используем wallet: {wallet}")

        # 2. Анализ
        if not args.monitor:
            # Позиции
            print("\n  Загружаю позиции...")
            positions = client.get_positions(wallet, limit=200)
            print_positions(positions)

            # Сделки
            print(f"\n  Загружаю последние {args.trades_limit} сделок...")
            trades = client.get_activity(wallet, limit=args.trades_limit)
            print(f"  Загружено {len(trades)} сделок")

            # Анализ
            analysis = analyze_trades(trades, positions)
            print_analysis(analysis)

            # Total value
            total_val = client.get_total_value(wallet)
            if total_val:
                print(f"\n  Общая стоимость позиций: ${total_val:,.2f}")

        # 3. Мониторинг
        if not args.analyze:
            monitor_loop(client, wallet, poll_interval)

    finally:
        client.close()


if __name__ == "__main__":
    main()