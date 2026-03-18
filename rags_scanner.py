"""
Polymarket Rags-to-Riches Scanner
==================================
Ищет кошельки, которые начали с маленького депозита
и значительно выросли. Именно у таких трейдеров
может быть реальный directional edge или инсайд.

Логика:
1. Берём лидерборд за всё время
2. Для каждого трейдера считаем ROI = PnL / Volume
3. Фильтруем по: высокий ROI + умеренный объём (не киты)
4. Анализируем на что ставили — крипто, политика, спорт

Запуск:
    python3 rags_scanner.py
    python3 rags_scanner.py --min-roi 20 --max-volume 100000
    python3 rags_scanner.py --category POLITICS
"""

import httpx
import json
import time
import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field

DATA_API = "https://data-api.polymarket.com"

# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class RagsTrader:
    wallet: str
    username: str
    pnl: float
    volume: float
    roi: float              # PnL / Volume в %
    positions_count: int
    avg_trade_size: float
    categories: list = field(default_factory=list)
    top_markets: list = field(default_factory=list)
    buys: int = 0
    sells: int = 0
    active_positions: int = 0
    profile_url: str = ""


# ─── API Client ─────────────────────────────────────────────────────────────

class Scanner:
    def __init__(self):
        self.http = httpx.Client(timeout=30.0)

    def close(self):
        self.http.close()

    def get_leaderboard(self, period: str = "ALL", category: str = "OVERALL",
                        limit: int = 50, offset: int = 0) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/v1/leaderboard",
                params={
                    "timePeriod": period,
                    "orderBy": "PNL",
                    "category": category,
                    "limit": min(limit, 50),
                    "offset": offset,
                }
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"  [!] Error: {e}")
        return []

    def get_activity(self, wallet: str, limit: int = 200) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/activity",
                params={
                    "user": wallet, "limit": limit,
                    "type": "TRADE", "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC"
                }
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []

    def get_positions(self, wallet: str, limit: int = 100) -> list[dict]:
        try:
            resp = self.http.get(
                f"{DATA_API}/positions",
                params={"user": wallet, "limit": limit}
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []


# ─── Analysis ───────────────────────────────────────────────────────────────

def analyze(scanner: Scanner, wallet: str, username: str,
            pnl: float, volume: float) -> RagsTrader:

    roi = (pnl / volume * 100) if volume > 0 else 0

    trades = scanner.get_activity(wallet, limit=200)
    positions = scanner.get_positions(wallet, limit=50)

    # Считаем метрики
    buys = sells = 0
    markets = defaultdict(lambda: {"volume": 0, "count": 0})
    categories = set()

    for t in trades:
        side = t.get("side", "BUY")
        if side == "BUY":
            buys += 1
        else:
            sells += 1

        title = t.get("title", "").lower()
        usdc = float(t.get("usdcSize", 0))
        markets[t.get("title", "Unknown")]["volume"] += usdc
        markets[t.get("title", "Unknown")]["count"] += 1

        # Категоризация
        if any(k in title for k in ["bitcoin", "btc", "ethereum", "eth",
                                     "solana", "sol", "crypto", "xrp"]):
            categories.add("crypto")
        elif any(k in title for k in ["trump", "biden", "election", "president",
                                       "republican", "democrat", "congress",
                                       "rubio", "vance", "governor"]):
            categories.add("politics")
        elif any(k in title for k in ["win on", "spread:", "o/u", "vs.",
                                       "lakers", "celtics", "nba", "nfl"]):
            categories.add("sports")
        else:
            categories.add("other")

    # Топ маркеты по объёму
    sorted_markets = sorted(markets.items(), key=lambda x: x[1]["volume"], reverse=True)
    top_markets = [
        {"title": title, "volume": stats["volume"], "count": stats["count"]}
        for title, stats in sorted_markets[:5]
    ]

    avg_trade = volume / max(len(trades), 1)

    return RagsTrader(
        wallet=wallet,
        username=username,
        pnl=pnl,
        volume=volume,
        roi=roi,
        positions_count=len(trades),
        avg_trade_size=avg_trade,
        categories=sorted(categories),
        top_markets=top_markets,
        buys=buys,
        sells=sells,
        active_positions=len(positions),
        profile_url=f"https://polymarket.com/@{username}",
    )


# ─── Display ────────────────────────────────────────────────────────────────

def print_results(traders: list[RagsTrader]):
    print(f"\n{'═' * 70}")
    print(f"  🚀 RAGS-TO-RICHES: ТОП-{len(traders)} ТРЕЙДЕРОВ")
    print(f"{'═' * 70}\n")

    for i, t in enumerate(traders, 1):
        sell_ratio = t.sells / max(t.buys + t.sells, 1) * 100

        print(f"  #{i:2d}  @{t.username}")
        print(f"      PnL: ${t.pnl:>12,.0f}   Volume: ${t.volume:>12,.0f}   ROI: {t.roi:>6.1f}%")
        print(f"      Trades: {t.positions_count}   Avg size: ${t.avg_trade_size:,.0f}   "
              f"Buy/Sell: {t.buys}/{t.sells} ({sell_ratio:.0f}% sells)")
        print(f"      Categories: {', '.join(t.categories)}")
        print(f"      Active positions: {t.active_positions}")

        if t.top_markets:
            print(f"      Top markets:")
            for m in t.top_markets[:3]:
                print(f"        ${m['volume']:>10,.0f}  ({m['count']} trades)  {m['title'][:50]}")

        print(f"      Profile: {t.profile_url}")
        print()


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Rags-to-Riches Scanner")
    parser.add_argument("--min-roi", type=float, default=10,
                        help="Минимальный ROI в %% (по умолчанию 10)")
    parser.add_argument("--max-volume", type=float, default=500000,
                        help="Максимальный объём (фильтруем китов)")
    parser.add_argument("--min-pnl", type=float, default=1000,
                        help="Минимальный PnL")
    parser.add_argument("--max-avg-trade", type=float, default=50000,
                        help="Макс средний размер сделки")
    parser.add_argument("--category", type=str, default="OVERALL",
                        choices=["OVERALL", "POLITICS", "SPORTS", "CRYPTO",
                                 "CULTURE", "MENTIONS"])
    parser.add_argument("--period", type=str, default="ALL",
                        choices=["DAY", "WEEK", "MONTH", "ALL"])
    parser.add_argument("--pages", type=int, default=4,
                        help="Сколько страниц лидерборда сканировать (по 50)")
    parser.add_argument("--export", type=str, default=None)
    args = parser.parse_args()

    scanner = Scanner()

    try:
        print(f"\n{'═' * 70}")
        print(f"  POLYMARKET RAGS-TO-RICHES SCANNER")
        print(f"{'═' * 70}")
        print(f"  Min ROI:       {args.min_roi}%")
        print(f"  Max Volume:    ${args.max_volume:,.0f}")
        print(f"  Min PnL:       ${args.min_pnl:,.0f}")
        print(f"  Category:      {args.category}")
        print(f"  Period:        {args.period}")
        print(f"  Pages:         {args.pages} (up to {args.pages * 50} traders)")
        print()

        # 1. Загружаем лидерборд (несколько страниц)
        print("  [1/3] Загружаю лидерборд...")
        all_leaders = []
        for page in range(args.pages):
            leaders = scanner.get_leaderboard(
                period=args.period,
                category=args.category,
                limit=50,
                offset=page * 50
            )
            if not leaders:
                break
            all_leaders.extend(leaders)
            time.sleep(0.3)

        print(f"  Загружено {len(all_leaders)} трейдеров")

        # 2. Первичный фильтр по ROI
        print("\n  [2/3] Фильтрую по ROI...")
        candidates = []
        for leader in all_leaders:
            pnl = float(leader.get("pnl", 0))
            vol = float(leader.get("vol", 0))

            if pnl < args.min_pnl:
                continue
            if vol > args.max_volume:
                continue
            if vol <= 0:
                continue

            roi = pnl / vol * 100
            if roi < args.min_roi:
                continue

            candidates.append(leader)

        print(f"  Кандидатов после фильтра: {len(candidates)}")

        if not candidates:
            print("\n  Никого не нашёл. Попробуй снизить --min-roi или увеличить --max-volume")
            return

        # 3. Глубокий анализ
        print(f"\n  [3/3] Анализирую {len(candidates)} кандидатов...")
        results = []

        for i, leader in enumerate(candidates):
            wallet = leader.get("proxyWallet", "")
            username = leader.get("userName", wallet[:12])
            pnl = float(leader.get("pnl", 0))
            vol = float(leader.get("vol", 0))

            if not wallet:
                continue

            sys.stdout.write(f"\r  Анализ: {i+1}/{len(candidates)}  @{username[:20]:20s}")
            sys.stdout.flush()

            trader = analyze(scanner, wallet, username, pnl, vol)

            # Доп фильтры
            if trader.avg_trade_size > args.max_avg_trade:
                continue
            # Пропускаем если только BUY по 0.99 (бондеры)
            if trader.sells == 0 and trader.roi < 20:
                continue

            results.append(trader)
            time.sleep(0.5)

        print(f"\n\n  Найдено {len(results)} трейдеров")

        # Сортируем по ROI
        results.sort(key=lambda t: t.roi, reverse=True)

        # Вывод
        if results:
            print_results(results[:20])
        else:
            print("\n  Никого не нашёл подходящего. Попробуй другие параметры:")
            print("    --min-roi 5 --max-volume 1000000")

        # Экспорт
        if args.export and results:
            export = []
            for t in results:
                export.append({
                    "username": t.username,
                    "wallet": t.wallet,
                    "pnl": t.pnl,
                    "volume": t.volume,
                    "roi_pct": round(t.roi, 2),
                    "trades": t.positions_count,
                    "avg_trade": round(t.avg_trade_size, 2),
                    "buys": t.buys,
                    "sells": t.sells,
                    "categories": t.categories,
                    "top_markets": t.top_markets,
                    "profile": t.profile_url,
                })
            with open(args.export, "w") as f:
                json.dump(export, f, indent=2)
            print(f"\n  Экспортировано в {args.export}")

    finally:
        scanner.close()


if __name__ == "__main__":
    main()
