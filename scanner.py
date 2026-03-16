"""
Polymarket Logical Arbitrage Scanner

Сканирует рынки Polymarket и ищет арбитражные возможности:
1. Intra-event: сумма YES-цен в multi-outcome событии ≠ 1.00
2. Intra-market: YES + NO < 1.00 в бинарном рынке
3. Логический арбитраж: вложенные/связанные события с несовместимыми ценами
"""

import json
import time
import httpx
from datetime import datetime, timezone
from dataclasses import dataclass, field

import config


@dataclass
class ArbitrageOpportunity:
    """Найденная арбитражная возможность"""
    type: str                    # "intra_event" | "intra_market" | "logical"
    event_title: str
    description: str
    markets: list                # Список рынков, участвующих в арбитраже
    total_cost: float            # Сколько стоит купить все стороны
    guaranteed_payout: float     # Гарантированная выплата
    gross_profit_pct: float      # Прибыль до комиссий (%)
    net_profit_pct: float        # Прибыль после комиссий (%)
    net_profit_usd: float        # Прибыль в $ на $100 вложений
    liquidity: float             # Минимальная ликвидность среди рынков
    volume: float                # Суммарный объём
    url: str                     # Ссылка на событие
    found_at: str = ""           # Время обнаружения

    def __post_init__(self):
        if not self.found_at:
            self.found_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class PolymarketScanner:
    """Сканер арбитражных возможностей на Polymarket"""

    def __init__(self):
        self.client = httpx.Client(timeout=30.0)
        self.opportunities: list[ArbitrageOpportunity] = []

    # ── Получение данных ──

    def fetch_all_events(self) -> list[dict]:
        """
        Забирает все активные события с рынками через Gamma API.
        Использует пагинацию (offset/limit).
        """
        all_events = []
        offset = 0

        print(f"[*] Загружаю события с {config.GAMMA_API_URL}...")

        for page in range(config.MAX_PAGES):
            params = {
                "active": "true",
                "closed": "false",
                "limit": config.EVENTS_PER_PAGE,
                "offset": offset,
            }

            try:
                resp = self.client.get(
                    f"{config.GAMMA_API_URL}/events", params=params
                )
                resp.raise_for_status()
                events = resp.json()
            except httpx.HTTPError as e:
                print(f"[!] Ошибка API на странице {page}: {e}")
                break
            except json.JSONDecodeError:
                print(f"[!] Невалидный JSON на странице {page}")
                break

            if not events:
                break

            all_events.extend(events)
            offset += config.EVENTS_PER_PAGE

            print(f"    Страница {page + 1}: +{len(events)} событий (всего: {len(all_events)})")

            # Маленькая пауза чтобы не упереться в rate limit
            time.sleep(0.3)

        print(f"[+] Загружено {len(all_events)} активных событий\n")
        return all_events

    # ── Парсинг рынков ──

    @staticmethod
    def parse_market_prices(market: dict) -> dict | None:
        """
        Извлекает цены из рынка.
        outcomePrices — строка вида '["0.55","0.45"]'
        outcomes — строка вида '["Yes","No"]'
        """
        try:
            prices_raw = market.get("outcomePrices", "")
            outcomes_raw = market.get("outcomes", "")

            if not prices_raw or not outcomes_raw:
                return None

            # Парсим JSON-строки
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw

            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            prices = [float(p) for p in prices]

            if len(prices) != len(outcomes):
                return None

            return {
                "id": market.get("id", ""),
                "question": market.get("question", "N/A"),
                "slug": market.get("slug", ""),
                "outcomes": outcomes,
                "prices": prices,
                "yes_price": prices[0] if len(prices) > 0 else 0,
                "no_price": prices[1] if len(prices) > 1 else 0,
                "liquidity": float(market.get("liquidityNum", 0) or 0),
                "volume": float(market.get("volumeNum", 0) or 0),
                "active": market.get("active", False),
                "closed": market.get("closed", False),
                "accepting_orders": market.get("acceptingOrders", False),
                "best_bid": float(market.get("bestBid", 0) or 0),
                "best_ask": float(market.get("bestAsk", 0) or 0),
            }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            return None

    # ── Расчёт прибыли ──

    @staticmethod
    def calc_net_profit(total_cost: float, payout: float) -> tuple[float, float, float]:
        """
        Считает прибыль с учётом комиссий.
        Возвращает: (gross_pct, net_pct, net_usd_per_100)

        Polymarket берёт 2% с выигрыша (winner fee).
        """
        if total_cost <= 0:
            return (0, 0, 0)

        gross_profit = payout - total_cost
        gross_pct = (gross_profit / total_cost) * 100

        # Winner fee: 2% от выигрыша (payout - cost вложенных в выигравшую сторону)
        # Для арбитража: выигрывает ровно 1 сторона, fee = 2% от (1.00 - цена_выигрыша)
        # Упрощённо: fee ≈ 2% от gross_profit
        fee = gross_profit * (config.WINNER_FEE_PCT / 100)
        gas = config.GAS_COST_USD

        net_profit = gross_profit - fee - gas
        net_pct = (net_profit / total_cost) * 100
        net_usd = net_pct  # на $100 вложений

        return (gross_pct, net_pct, net_usd)

    # ── Поиск арбитража ──

    def scan_intra_market(self, events: list[dict]) -> list[ArbitrageOpportunity]:
        """
        Тип 1: Intra-market арбитраж.
        В бинарном рынке YES + NO < 1.00
        → покупаем обе стороны, гарантированная выплата $1.00
        """
        opps = []

        for event in events:
            markets = event.get("markets", [])
            if not markets:
                continue

            for market in markets:
                parsed = self.parse_market_prices(market)
                if not parsed or parsed["closed"] or not parsed["active"]:
                    continue

                # Пропускаем рынки которые не принимают ордера
                if not parsed["accepting_orders"]:
                    continue

                # Фильтр по ликвидности
                if parsed["liquidity"] < config.MIN_LIQUIDITY:
                    continue

                # Только бинарные рынки (Yes/No)
                if len(parsed["outcomes"]) != 2:
                    continue

                yes_price = parsed["yes_price"]
                no_price = parsed["no_price"]
                total_cost = yes_price + no_price

                # Пропускаем если цены не загрузились или нереальные
                if yes_price <= 0.001 or no_price <= 0.001:
                    continue

                if total_cost <= 0 or total_cost >= 1.0:
                    continue

                payout = 1.0
                gross_pct, net_pct, net_usd = self.calc_net_profit(total_cost, payout)

                if net_pct < config.MIN_PROFIT_PCT:
                    continue

                opps.append(ArbitrageOpportunity(
                    type="intra_market",
                    event_title=event.get("title", "N/A"),
                    description=(
                        f"YES={yes_price:.4f} + NO={no_price:.4f} = {total_cost:.4f} < 1.00\n"
                        f"Покупаем обе стороны → гарантированная выплата $1.00"
                    ),
                    markets=[parsed["question"]],
                    total_cost=total_cost,
                    guaranteed_payout=payout,
                    gross_profit_pct=gross_pct,
                    net_profit_pct=net_pct,
                    net_profit_usd=net_usd,
                    liquidity=parsed["liquidity"],
                    volume=parsed["volume"],
                    url=f"https://polymarket.com/event/{event.get('slug', '')}",
                ))

        return opps

    def scan_intra_event(self, events: list[dict]) -> list[ArbitrageOpportunity]:
        """
        Тип 2: Intra-event арбитраж (negRisk события).

        КЛЮЧЕВОЕ ПОНИМАНИЕ:
        negRisk = взаимоисключающие, но НЕ исчерпывающие.
        Сумма YES < 1.00 — это НОРМАЛЬНО (остаток = вероятность "Other"/не в списке).
        Это НЕ арбитраж!

        Настоящий арбитраж: сумма YES > 1.00
        → Невозможно чтобы больше 1 исхода выиграл
        → Значит рынок переоценён
        → Стратегия: Split $1 USDC → получаем YES во всех рынках → продаём все YES
        → Выручка = sum(YES) > $1.00 → профит = sum(YES) - $1.00
        """
        opps = []

        for event in events:
            if not event.get("negRisk"):
                continue

            markets = event.get("markets", [])
            if len(markets) < 2:
                continue

            parsed_markets = []
            for m in markets:
                parsed = self.parse_market_prices(m)
                if (parsed and parsed["active"] and not parsed["closed"]
                        and parsed["accepting_orders"]):
                    parsed_markets.append(parsed)

            if len(parsed_markets) < 2:
                continue

            yes_prices = [m["yes_price"] for m in parsed_markets]

            if any(p <= 0.001 for p in yes_prices):
                continue

            total_yes = sum(yes_prices)

            min_liq = min(m["liquidity"] for m in parsed_markets)
            total_vol = sum(m["volume"] for m in parsed_markets)

            if min_liq < config.MIN_LIQUIDITY:
                continue

            # ── АРБИТРАЖ: сумма YES > 1.00 (рынок перегрет) ──
            # Split $1 → sell all YES → profit = total_yes - 1.00
            if total_yes > 1.0:
                split_cost = 1.0
                revenue = total_yes
                gross_profit = revenue - split_cost
                gross_pct = (gross_profit / split_cost) * 100

                # Комиссии: при продаже YES мы — тейкер (до ~1.56% fee)
                # + газ за split + газ за N ордеров продажи
                # Консервативная оценка: 2% от прибыли + газ * количество рынков
                fee = gross_profit * 0.02
                gas = config.GAS_COST_USD * len(parsed_markets)
                net_profit = gross_profit - fee - gas

                if net_profit <= 0:
                    continue

                net_pct = (net_profit / split_cost) * 100

                if net_pct < config.MIN_PROFIT_PCT:
                    continue

                # Сортируем рынки по цене для наглядности
                sorted_markets = sorted(parsed_markets, key=lambda m: m["yes_price"], reverse=True)
                market_details = [
                    f"  • {m['question']}: YES={m['yes_price']:.4f}"
                    for m in sorted_markets[:15]  # Макс 15 для читаемости
                ]
                if len(sorted_markets) > 15:
                    market_details.append(f"  ... и ещё {len(sorted_markets) - 15}")

                opps.append(ArbitrageOpportunity(
                    type="intra_event",
                    event_title=event.get("title", "N/A"),
                    description=(
                        f"⚡ ПЕРЕГРЕТ: сумма YES = {total_yes:.4f} > 1.00 ({len(parsed_markets)} исходов)\n"
                        f"Split $1 → продать все YES → выручка ${total_yes:.4f}\n"
                        + "\n".join(market_details)
                    ),
                    markets=[m["question"] for m in parsed_markets],
                    total_cost=split_cost,
                    guaranteed_payout=revenue,
                    gross_profit_pct=gross_pct,
                    net_profit_pct=net_pct,
                    net_profit_usd=net_pct,
                    liquidity=min_liq,
                    volume=total_vol,
                    url=f"https://polymarket.com/event/{event.get('slug', '')}",
                ))

        return opps

    # scan_sum_mismatch УБРАН — давал ложные срабатывания.
    # Причина: не все multi-market события взаимоисключающие.
    # "Золото > $3000" и "Золото > $3100" — оба могут быть YES.
    # Арбитраж суммы работает ТОЛЬКО в negRisk событиях (scan_intra_event).

    # ── Основной цикл ──

    def scan_once(self) -> list[ArbitrageOpportunity]:
        """Выполняет один полный скан."""
        events = self.fetch_all_events()

        if not events:
            print("[!] Нет событий для анализа")
            return []

        # Считаем статистику
        total_markets = sum(len(e.get("markets", [])) for e in events)
        neg_risk_events = sum(1 for e in events if e.get("negRisk"))
        multi_market = sum(1 for e in events if len(e.get("markets", [])) >= 2)

        print(f"[*] Статистика:")
        print(f"    Событий: {len(events)}")
        print(f"    Рынков: {total_markets}")
        print(f"    negRisk событий: {neg_risk_events}")
        print(f"    Multi-market событий: {multi_market}")
        print()

        # Запускаем все типы сканирования
        all_opps = []

        print("[*] Сканирую intra-market (YES+NO < 1.00)...")
        opps1 = self.scan_intra_market(events)
        print(f"    Найдено: {len(opps1)}")
        all_opps.extend(opps1)

        print("[*] Сканирую intra-event negRisk (сумма YES < 1.00)...")
        opps2 = self.scan_intra_event(events)
        print(f"    Найдено: {len(opps2)}")
        all_opps.extend(opps2)

        # Сортируем по net profit
        all_opps.sort(key=lambda o: o.net_profit_pct, reverse=True)

        self.opportunities = all_opps
        return all_opps

    def format_opportunity(self, opp: ArbitrageOpportunity) -> str:
        """Форматирует возможность для вывода в консоль/Telegram."""
        emoji = {
            "intra_market": "💰",
            "intra_event": "🎯",
            "sum_anomaly": "⚠️",
            "logical": "🧠",
        }.get(opp.type, "📊")

        return (
            f"\n{'='*60}\n"
            f"{emoji} {opp.type.upper()} | {opp.event_title}\n"
            f"{'='*60}\n"
            f"{opp.description}\n\n"
            f"💵 Стоимость:  ${opp.total_cost:.4f}\n"
            f"💰 Выплата:    ${opp.guaranteed_payout:.2f}\n"
            f"📈 Прибыль:    {opp.gross_profit_pct:+.2f}% (до комиссий)\n"
            f"📉 Чистая:     {opp.net_profit_pct:+.2f}% (после 2% fee + газ)\n"
            f"💲 На $100:    ${opp.net_profit_usd:+.2f}\n"
            f"🌊 Ликвидность: ${opp.liquidity:,.0f}\n"
            f"📊 Объём:      ${opp.volume:,.0f}\n"
            f"🔗 {opp.url}\n"
            f"🕐 {opp.found_at}\n"
        )

    def run_loop(self):
        """Бесконечный цикл сканирования."""
        print("=" * 60)
        print("  Polymarket Logical Arbitrage Scanner v0.1")
        print(f"  Мин. прибыль: {config.MIN_PROFIT_PCT}%")
        print(f"  Мин. ликвидность: ${config.MIN_LIQUIDITY:,}")
        print(f"  Интервал: {config.SCAN_INTERVAL_SEC}с")
        print("=" * 60)
        print()

        scan_num = 0
        while True:
            scan_num += 1
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            print(f"\n{'─'*60}")
            print(f"[СКАН #{scan_num}] {now}")
            print(f"{'─'*60}")

            try:
                opps = self.scan_once()

                if opps:
                    print(f"\n🔔 НАЙДЕНО {len(opps)} ВОЗМОЖНОСТЕЙ:")
                    for opp in opps:
                        print(self.format_opportunity(opp))
                else:
                    print("\n😴 Арбитражных возможностей не найдено")

            except Exception as e:
                print(f"\n[!] Ошибка сканирования: {e}")

            print(f"\n⏳ Следующий скан через {config.SCAN_INTERVAL_SEC}с...")
            time.sleep(config.SCAN_INTERVAL_SEC)


def main():
    """Точка входа — одноразовый скан (для тестирования)."""
    scanner = PolymarketScanner()
    opps = scanner.scan_once()

    if opps:
        print(f"\n{'='*60}")
        print(f"🔔 НАЙДЕНО {len(opps)} ВОЗМОЖНОСТЕЙ")
        print(f"{'='*60}")
        for opp in opps:
            print(scanner.format_opportunity(opp))
    else:
        print("\n😴 Арбитражных возможностей не найдено (при текущих фильтрах)")
        print(f"   Попробуй снизить MIN_PROFIT_PCT (сейчас {config.MIN_PROFIT_PCT}%)")
        print(f"   или MIN_LIQUIDITY (сейчас ${config.MIN_LIQUIDITY:,})")

    return opps


if __name__ == "__main__":
    main()
