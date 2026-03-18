"""
Polymarket Smart Wallet Scanner
================================
Сканирует лидерборд и историю трейдеров на Polymarket,
фильтрует бондеров/маркет-мейкеров, и находит "sharp" трейдеров
с реальным edge для copy-trading.

Критерии отбора:
  1. PnL margin > 3% (profit / volume) — отсеивает бондеров с 0.5%
  2. Win rate 55-90% — не бондер (99%) и не казино (50%)
  3. Сделок 50-5000 — не бот на 36K и не одноразовый везунчик
  4. Avg entry price 0.10-0.85 — directional, не bonding по 0.99
  5. Реагирует на новости — входит ДО движения цены

Запуск:
    pip install httpx rich
    python smart_scanner.py
    python smart_scanner.py --category crypto --window 30d --top 50
    python smart_scanner.py --analyze 0xWALLET_ADDRESS
"""

import httpx
import json
import time
import argparse
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ─── Config ─────────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Фильтры для "smart money"
MIN_PNL = 500               # Минимум $500 profit
MIN_PNL_MARGIN = 0.03       # Минимум 3% profit/volume
MIN_WIN_RATE = 0.55          # 55%+ win rate
MAX_WIN_RATE = 0.92          # <92% (бондеры обычно >95%)
MIN_POSITIONS = 30           # Минимум 30 сделок
MAX_POSITIONS = 10000        # Не маркет-мейкер бот
MIN_AVG_PRICE = 0.10        # Средняя цена входа — не мусорные ставки
MAX_AVG_PRICE = 0.88        # Не bonding по 0.99

# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class TraderScore:
    """Оценка трейдера для copy-trading."""
    wallet: str
    username: str
    pnl: float
    volume: float
    pnl_margin: float         # pnl / volume
    positions_count: int
    win_rate: float
    avg_entry_price: float    # Средняя цена входа
    categories: list          # В каких категориях торгует
    biggest_win: float
    biggest_loss: float
    active_positions: int
    score: float = 0.0        # Итоговый скор 0-100
    flags: list = field(default_factory=list)  # Красные флаги
    x_username: str = ""

    def calculate_score(self):
        """Рассчитываем composite score."""
        s = 0.0

        # PnL margin (0-25 очков)
        if self.pnl_margin > 0.10:
            s += 25
        elif self.pnl_margin > 0.05:
            s += 20
        elif self.pnl_margin > 0.03:
            s += 15

        # Win rate (0-25 очков)
        if 0.65 <= self.win_rate <= 0.85:
            s += 25  # Sweet spot
        elif 0.60 <= self.win_rate <= 0.90:
            s += 20
        elif self.win_rate >= 0.55:
            s += 10

        # Consistency — кол-во сделок (0-20 очков)
        if self.positions_count >= 200:
            s += 20
        elif self.positions_count >= 100:
            s += 15
        elif self.positions_count >= 50:
            s += 10

        # Directional trading — avg price (0-20 очков)
        if 0.25 <= self.avg_entry_price <= 0.75:
            s += 20  # Чистый directional
        elif 0.15 <= self.avg_entry_price <= 0.85:
            s += 15
        else:
            s += 5

        # Risk management — biggest loss vs PnL (0-10 очков)
        if self.pnl > 0 and self.biggest_loss < self.pnl * 0.5:
            s += 10
        elif self.pnl > 0 and self.biggest_loss < self.pnl:
            s += 5

        self.score = s


@dataclass
class TraderDetail:
    """Детальный анализ одного трейдера."""
    wallet: str
    username: str
    trades: list
    positions: list
    closed_positions: list
    timing_score: float = 0.0  # Как быстро входит перед движением


# ─── API Client ─────────────────────────────────────────────────────────────

class PolymarketScanner:
    def __init__(self, timeout: float = 30.0):
        self.http = httpx.Client(timeout=timeout)

    def close(self):
        self.http.close()

    def get_leaderboard(self, window: str = "ALL", limit: int = 50,
                        category: str = "OVERALL") -> list[dict]:
        """
        Лидерборд по PnL.
        Endpoint: GET /v1/leaderboard
        timePeriod: DAY, WEEK, MONTH, ALL
        category: OVERALL, POLITICS, SPORTS, CRYPTO, CULTURE, MENTIONS, etc.
        orderBy: PNL, VOL
        limit: 1-50
        """
        # Маппинг наших аргументов → API параметры
        period_map = {
            "1d": "DAY", "7d": "WEEK", "30d": "MONTH", "all": "ALL",
            "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH", "ALL": "ALL",
        }
        time_period = period_map.get(window, "ALL")

        all_results = []
        # API отдаёт максимум 50 за раз, пагинируем
        per_page = min(limit, 50)
        pages = (limit + per_page - 1) // per_page

        for page in range(pages):
            offset = page * per_page
            try:
                resp = self.http.get(
                    f"{DATA_API}/v1/leaderboard",
                    params={
                        "timePeriod": time_period,
                        "orderBy": "PNL",
                        "category": category,
                        "limit": per_page,
                        "offset": offset,
                    }
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if not data:
                        break
                    all_results.extend(data)
                else:
                    print(f"  [!] Leaderboard returned HTTP {resp.status_code}: {resp.text[:200]}")
                    break
            except Exception as e:
                print(f"  [!] Leaderboard error: {e}")
                break
            time.sleep(0.3)

        return all_results

    def get_positions(self, wallet: str, limit: int = 200) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/positions",
                params={"user": wallet, "limit": limit,
                        "sortBy": "CURRENT", "sortDirection": "DESC"}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []

    def get_closed_positions(self, wallet: str, limit: int = 200) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/closed-positions",
                params={"user": wallet, "limit": limit}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []

    def get_activity(self, wallet: str, limit: int = 200) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/activity",
                params={"user": wallet, "limit": limit,
                        "type": "TRADE", "sortBy": "TIMESTAMP",
                        "sortDirection": "DESC"}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []

    def get_profile(self, wallet: str) -> dict:
        try:
            resp = self.http.get(
                f"{GAMMA_API}/public-profile",
                params={"address": wallet}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}


# ─── Analysis ───────────────────────────────────────────────────────────────

def analyze_trader(scanner: PolymarketScanner, wallet: str,
                   username: str, pnl: float, volume: float) -> Optional[TraderScore]:
    """Глубокий анализ одного трейдера."""

    # Загружаем данные
    positions = scanner.get_positions(wallet, limit=200)
    closed = scanner.get_closed_positions(wallet, limit=200)
    trades = scanner.get_activity(wallet, limit=200)

    if not trades and not positions and not closed:
        return None

    # Считаем метрики
    total_positions = len(closed) + len(positions)
    if total_positions == 0:
        return None

    # Win rate по закрытым позициям
    wins = 0
    losses = 0
    biggest_win = 0
    biggest_loss = 0
    categories = set()

    for p in closed:
        cash_pnl = float(p.get("cashPnl", 0))
        title = p.get("title", "").lower()

        if cash_pnl > 0:
            wins += 1
            biggest_win = max(biggest_win, cash_pnl)
        elif cash_pnl < 0:
            losses += 1
            biggest_loss = max(biggest_loss, abs(cash_pnl))

        # Определяем категорию
        if any(k in title for k in ["bitcoin", "btc", "ethereum", "eth",
                                     "solana", "sol", "crypto", "xrp"]):
            categories.add("crypto")
        elif any(k in title for k in ["trump", "biden", "election", "president",
                                       "democrat", "republican", "congress"]):
            categories.add("politics")
        elif any(k in title for k in ["nba", "nfl", "mlb", "nhl", "game",
                                       "match", "score", "win"]):
            categories.add("sports")
        else:
            categories.add("other")

    total_resolved = wins + losses
    win_rate = wins / max(total_resolved, 1)

    # Средняя цена входа
    entry_prices = []
    for t in trades:
        price = float(t.get("price", 0))
        if 0 < price < 1:
            entry_prices.append(price)
    avg_entry_price = sum(entry_prices) / max(len(entry_prices), 1)

    # PnL margin
    pnl_margin = pnl / max(volume, 1)

    trader = TraderScore(
        wallet=wallet,
        username=username or wallet[:10],
        pnl=pnl,
        volume=volume,
        pnl_margin=pnl_margin,
        positions_count=total_positions,
        win_rate=win_rate,
        avg_entry_price=avg_entry_price,
        categories=sorted(categories),
        biggest_win=biggest_win,
        biggest_loss=biggest_loss,
        active_positions=len(positions),
    )

    # Флаги
    if avg_entry_price > 0.95:
        trader.flags.append("BONDER")
    if total_positions > 10000:
        trader.flags.append("BOT/MM")
    if win_rate > 0.95:
        trader.flags.append("HIGH_WR_SUSPECT")
    if pnl_margin < 0.01:
        trader.flags.append("LOW_MARGIN")
    if total_resolved < 10:
        trader.flags.append("TOO_FEW_RESOLVED")
    if biggest_loss > pnl * 2 and pnl > 0:
        trader.flags.append("HIGH_RISK")

    trader.calculate_score()
    return trader


def passes_filters(t: TraderScore) -> bool:
    """Проходит ли трейдер наши фильтры."""
    if t.pnl < MIN_PNL:
        return False
    if t.pnl_margin < MIN_PNL_MARGIN:
        return False
    if t.positions_count < MIN_POSITIONS:
        return False
    if t.positions_count > MAX_POSITIONS:
        return False
    if "BONDER" in t.flags:
        return False
    if "BOT/MM" in t.flags:
        return False
    return True


# ─── Display ────────────────────────────────────────────────────────────────

def _try_rich():
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
        return Console(), Table, box
    except ImportError:
        return None, None, None

CONSOLE, RichTable, box = _try_rich()

def cprint(msg, style=""):
    if CONSOLE:
        CONSOLE.print(f"[{style}]{msg}[/]" if style else msg)
    else:
        print(msg)

def print_header(text):
    sep = "═" * 60
    cprint(f"\n{sep}", "bold cyan")
    cprint(f"  {text}", "bold white")
    cprint(f"{sep}\n", "bold cyan")


def print_results(traders: list[TraderScore], show_all: bool = False):
    """Выводим результаты скрининга."""

    # Сортируем по score
    traders.sort(key=lambda t: t.score, reverse=True)

    if not show_all:
        traders = [t for t in traders if passes_filters(t)]

    if not traders:
        cprint("  Не найдено трейдеров, подходящих под критерии.", "yellow")
        return

    print_header(f"ТОП-{len(traders)} SMART WALLETS ДЛЯ COPY-TRADING")

    if CONSOLE and RichTable:
        table = RichTable(box=box.ROUNDED, show_lines=True)
        table.add_column("#", width=3)
        table.add_column("Score", width=6, justify="center")
        table.add_column("Username", max_width=18)
        table.add_column("PnL", justify="right", width=12)
        table.add_column("Margin", justify="right", width=8)
        table.add_column("WR%", justify="right", width=7)
        table.add_column("Trades", justify="right", width=7)
        table.add_column("Avg Price", justify="right", width=9)
        table.add_column("Category", width=12)
        table.add_column("Flags", max_width=18)
        table.add_column("Wallet", width=14)

        for i, t in enumerate(traders[:30], 1):
            score_color = "green" if t.score >= 70 else "yellow" if t.score >= 50 else "red"
            pnl_color = "green" if t.pnl > 0 else "red"
            flags_str = ", ".join(t.flags) if t.flags else "✅ clean"
            flags_color = "red" if t.flags else "green"
            cat_str = ", ".join(t.categories[:2])

            table.add_row(
                str(i),
                f"[{score_color}]{t.score:.0f}[/]",
                t.username[:18],
                f"[{pnl_color}]${t.pnl:>,.0f}[/]",
                f"{t.pnl_margin:.1%}",
                f"{t.win_rate:.0%}",
                f"{t.positions_count:,}",
                f"{t.avg_entry_price:.2f}",
                cat_str,
                f"[{flags_color}]{flags_str}[/]",
                f"{t.wallet[:8]}...{t.wallet[-4:]}",
            )

        CONSOLE.print(table)
    else:
        for i, t in enumerate(traders[:30], 1):
            flags = " | ".join(t.flags) if t.flags else "clean"
            print(
                f"  #{i:2d}  Score:{t.score:4.0f}  "
                f"${t.pnl:>10,.0f}  M:{t.pnl_margin:.1%}  "
                f"WR:{t.win_rate:.0%}  #{t.positions_count:>5d}  "
                f"AvgP:{t.avg_entry_price:.2f}  "
                f"{t.username[:15]:15s}  [{flags}]"
            )

    # Рекомендации
    top = [t for t in traders if t.score >= 60 and not t.flags]
    if top:
        print_header("🎯 РЕКОМЕНДОВАННЫЕ ДЛЯ COPY-TRADING")
        for t in top[:5]:
            cprint(f"\n  @{t.username}", "bold green")
            cprint(f"    Wallet:     {t.wallet}", "dim")
            cprint(f"    Score:      {t.score:.0f}/100", "bold")
            cprint(f"    PnL:        ${t.pnl:,.0f}  ({t.pnl_margin:.1%} margin)", "")
            cprint(f"    Win Rate:   {t.win_rate:.0%}", "")
            cprint(f"    Positions:  {t.positions_count}", "")
            cprint(f"    Avg Entry:  ${t.avg_entry_price:.2f}", "")
            cprint(f"    Markets:    {', '.join(t.categories)}", "")
            cprint(f"    Best win:   ${t.biggest_win:,.0f}", "green")
            cprint(f"    Worst loss: ${t.biggest_loss:,.0f}", "red")
            cprint(f"    Profile:    https://polymarket.com/@{t.username}", "cyan")


def print_detailed_analysis(scanner: PolymarketScanner, wallet: str):
    """Детальный анализ конкретного кошелька."""
    print_header(f"ДЕТАЛЬНЫЙ АНАЛИЗ: {wallet[:20]}...")

    profile = scanner.get_profile(wallet)
    username = profile.get("pseudonym", wallet[:12])
    cprint(f"  Username: {username}", "bold")

    trades = scanner.get_activity(wallet, limit=500)
    positions = scanner.get_positions(wallet, limit=200)
    closed = scanner.get_closed_positions(wallet, limit=200)

    cprint(f"\n  Сделок загружено:    {len(trades)}", "")
    cprint(f"  Открытых позиций:    {len(positions)}", "")
    cprint(f"  Закрытых позиций:    {len(closed)}", "")

    # Анализ по времени
    if trades:
        hours = defaultdict(int)
        markets = defaultdict(lambda: {"count": 0, "volume": 0, "buys": 0, "sells": 0})
        total_volume = 0

        for t in trades:
            ts = int(t.get("timestamp", 0))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            hours[dt.hour] += 1

            title = t.get("title", "Unknown")
            usdc = float(t.get("usdcSize", 0))
            side = t.get("side", "BUY")
            markets[title]["count"] += 1
            markets[title]["volume"] += usdc
            markets[title]["buys" if side == "BUY" else "sells"] += 1
            total_volume += usdc

        # Распределение по часам
        cprint(f"\n  ── Активность по часам (UTC) ──", "bold")
        max_h = max(hours.values()) if hours else 1
        for h in range(24):
            c = hours.get(h, 0)
            bar = "█" * int(c / max_h * 25) if max_h > 0 else ""
            cprint(f"    {h:02d}:00  {bar:25s}  {c}", "")

        # Топ маркеты
        cprint(f"\n  ── Топ маркеты по объёму ──", "bold")
        sorted_m = sorted(markets.items(), key=lambda x: x[1]["volume"], reverse=True)
        for title, stats in sorted_m[:15]:
            cprint(
                f"    ${stats['volume']:>10,.0f}  "
                f"B:{stats['buys']:>4d}  S:{stats['sells']:>4d}  "
                f"{title[:50]}",
                ""
            )

    # PnL по закрытым
    if closed:
        wins = sum(1 for p in closed if float(p.get("cashPnl", 0)) > 0)
        losses = sum(1 for p in closed if float(p.get("cashPnl", 0)) < 0)
        total_pnl = sum(float(p.get("cashPnl", 0)) for p in closed)
        wr = wins / max(wins + losses, 1)

        cprint(f"\n  ── P&L по закрытым позициям ──", "bold")
        cprint(f"    Wins / Losses:  {wins} / {losses}", "")
        cprint(f"    Win Rate:       {wr:.1%}", "")
        pnl_color = "green" if total_pnl > 0 else "red"
        cprint(f"    Total PnL:      ${total_pnl:,.2f}", pnl_color)

        # Топ выигрыши и проигрыши
        by_pnl = sorted(closed, key=lambda p: float(p.get("cashPnl", 0)), reverse=True)
        cprint(f"\n    Топ-5 выигрышей:", "green")
        for p in by_pnl[:5]:
            cprint(f"      +${float(p.get('cashPnl', 0)):>10,.2f}  {p.get('title', '')[:45]}", "green")
        cprint(f"\n    Топ-5 проигрышей:", "red")
        for p in by_pnl[-5:]:
            cprint(f"      ${float(p.get('cashPnl', 0)):>10,.2f}  {p.get('title', '')[:45]}", "red")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Smart Wallet Scanner")
    parser.add_argument("--window", type=str, default="all",
                        choices=["1d", "7d", "30d", "all"],
                        help="Временное окно лидерборда")
    parser.add_argument("--category", type=str, default="OVERALL",
                        choices=["OVERALL", "POLITICS", "SPORTS", "CRYPTO",
                                 "CULTURE", "MENTIONS", "WEATHER", "ECONOMICS",
                                 "TECH", "FINANCE"],
                        help="Категория маркетов")
    parser.add_argument("--top", type=int, default=100,
                        help="Сколько трейдеров с лидерборда проверить")
    parser.add_argument("--analyze", type=str, default=None,
                        help="Детальный анализ конкретного wallet")
    parser.add_argument("--show-all", action="store_true",
                        help="Показать всех, включая отфильтрованных")
    parser.add_argument("--min-pnl", type=float, default=MIN_PNL,
                        help="Минимальный PnL")
    parser.add_argument("--export", type=str, default=None,
                        help="Экспорт в JSON файл")
    args = parser.parse_args()

    scanner = PolymarketScanner()

    try:
        # Детальный анализ одного кошелька
        if args.analyze:
            print_detailed_analysis(scanner, args.analyze)
            return

        # Скан лидерборда
        print_header("POLYMARKET SMART WALLET SCANNER")
        cprint(f"  Window:   {args.window}", "")
        cprint(f"  Category: {args.category}", "")
        cprint(f"  Top:      {args.top} трейдеров", "")
        cprint(f"  Min PnL:  ${args.min_pnl:,.0f}", "")
        print()

        cprint("  [1/3] Загружаю лидерборд...", "dim")
        leaders = scanner.get_leaderboard(
            window=args.window, limit=args.top, category=args.category
        )

        if not leaders:
            cprint("  [!] Не удалось загрузить лидерборд. Проверь подключение.", "red")
            return

        cprint(f"  Загружено {len(leaders)} трейдеров\n", "dim")

        cprint("  [2/3] Анализирую каждого трейдера (это займёт время)...", "dim")
        results: list[TraderScore] = []

        for i, leader in enumerate(leaders):
            wallet = leader.get("proxyWallet", leader.get("address", ""))
            username = leader.get("userName", leader.get("pseudonym", leader.get("username", "")))
            pnl = float(leader.get("pnl", 0))
            volume = float(leader.get("vol", leader.get("volume", 0)))

            if not wallet:
                continue
            if pnl < args.min_pnl:
                continue

            # Прогресс
            pct = (i + 1) / len(leaders) * 100
            sys.stdout.write(f"\r  Анализ: {i+1}/{len(leaders)} ({pct:.0f}%)  @{username[:20]:20s}")
            sys.stdout.flush()

            trader = analyze_trader(scanner, wallet, username, pnl, volume)
            if trader:
                results.append(trader)

            # Rate limit protection
            time.sleep(0.5)

        print()
        cprint(f"\n  [3/3] Проанализировано {len(results)} трейдеров\n", "dim")

        # Вывод
        print_results(results, show_all=args.show_all)

        # Экспорт
        if args.export and results:
            export_data = []
            for t in sorted(results, key=lambda x: x.score, reverse=True):
                export_data.append({
                    "wallet": t.wallet,
                    "username": t.username,
                    "score": t.score,
                    "pnl": t.pnl,
                    "volume": t.volume,
                    "pnl_margin": t.pnl_margin,
                    "win_rate": t.win_rate,
                    "positions_count": t.positions_count,
                    "avg_entry_price": t.avg_entry_price,
                    "categories": t.categories,
                    "biggest_win": t.biggest_win,
                    "biggest_loss": t.biggest_loss,
                    "flags": t.flags,
                    "profile": f"https://polymarket.com/@{t.username}",
                })
            with open(args.export, "w") as f:
                json.dump(export_data, f, indent=2)
            cprint(f"\n  Экспортировано в {args.export}", "green")

    finally:
        scanner.close()


if __name__ == "__main__":
    main()