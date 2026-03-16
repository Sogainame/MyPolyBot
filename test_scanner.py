"""
Тест сканера с мок-данными.
Проверяет что логика поиска арбитража работает корректно.

Запуск: python test_scanner.py
"""
from scanner import PolymarketScanner
import config

# Снижаем пороги для теста
config.MIN_LIQUIDITY = 0
config.MIN_VOLUME = 0
config.MIN_PROFIT_PCT = 0.5


def make_market(question, yes_price, no_price, liquidity=10000, volume=5000):
    """Создаёт мок-рынок."""
    return {
        "id": "test-" + question[:10],
        "question": question,
        "slug": question.lower().replace(" ", "-"),
        "outcomePrices": f'["{yes_price}","{no_price}"]',
        "outcomes": '["Yes","No"]',
        "liquidityNum": liquidity,
        "volumeNum": volume,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "acceptingOrders": True,
        "bestBid": yes_price - 0.01,
        "bestAsk": yes_price + 0.01,
    }


def test_intra_market():
    """Тест 1: YES + NO < 1.00 — должен найти арбитраж."""
    print("=" * 50)
    print("ТЕСТ 1: Intra-market (YES + NO < 1.00)")
    print("=" * 50)

    events = [{
        "title": "Will BTC hit $200k?",
        "slug": "btc-200k",
        "negRisk": False,
        "markets": [
            make_market("BTC hits $200k by 2027", 0.35, 0.60),  # sum = 0.95 → 5% arb
        ]
    }]

    scanner = PolymarketScanner()
    opps = scanner.scan_intra_market(events)

    if opps:
        print(f"✅ Найдено {len(opps)} возможностей")
        for o in opps:
            print(f"   Cost={o.total_cost:.4f} Gross={o.gross_profit_pct:+.2f}% Net={o.net_profit_pct:+.2f}%")
    else:
        print("❌ Ничего не найдено (ошибка!)")

    print()
    return len(opps) > 0


def test_no_arb():
    """Тест 2: YES + NO >= 1.00 — НЕ должен найти."""
    print("=" * 50)
    print("ТЕСТ 2: Нет арбитража (YES + NO >= 1.00)")
    print("=" * 50)

    events = [{
        "title": "Normal market",
        "slug": "normal",
        "negRisk": False,
        "markets": [
            make_market("Normal event", 0.55, 0.47),  # sum = 1.02 — нет арбитража
        ]
    }]

    scanner = PolymarketScanner()
    opps = scanner.scan_intra_market(events)

    if not opps:
        print("✅ Корректно: арбитраж не найден")
    else:
        print("❌ Ошибочно нашёл арбитраж!")

    print()
    return len(opps) == 0


def test_intra_event_negrisk():
    """Тест 3: negRisk событие с суммой YES > 1.00 (перегрет)."""
    print("=" * 50)
    print("ТЕСТ 3: Intra-event negRisk (сумма YES > 1.00)")
    print("=" * 50)

    events = [{
        "title": "Who will win the election?",
        "slug": "election-winner",
        "negRisk": True,
        "markets": [
            make_market("Candidate A wins", 0.40, 0.60),
            make_market("Candidate B wins", 0.35, 0.65),
            make_market("Candidate C wins", 0.30, 0.70),
            # Сумма YES = 0.40 + 0.35 + 0.30 = 1.05 → перегрет!
            # Split $1 → sell all YES → $1.05 → profit $0.05
        ]
    }]

    scanner = PolymarketScanner()
    opps = scanner.scan_intra_event(events)

    if opps:
        print(f"✅ Найдено {len(opps)} возможностей")
        for o in opps:
            print(f"   Sum YES={o.guaranteed_payout:.4f} Net={o.net_profit_pct:+.2f}%")
    else:
        print("❌ Ничего не найдено (ошибка!)")

    print()
    return len(opps) > 0


def test_negrisk_no_arb():
    """Тест 4: negRisk с суммой YES < 1.00 — НЕ арбитраж (это нормально)."""
    print("=" * 50)
    print("ТЕСТ 4: negRisk сумма < 1.00 = НЕ арбитраж")
    print("=" * 50)

    events = [{
        "title": "Nobel Prize Winner",
        "slug": "nobel-prize",
        "negRisk": True,
        "markets": [
            make_market("Candidate A", 0.25, 0.75),
            make_market("Candidate B", 0.15, 0.85),
            make_market("Candidate C", 0.20, 0.80),
            # Сумма = 0.60 — остаток 0.40 = вероятность "Other"
            # Это НОРМАЛЬНО, НЕ арбитраж!
        ]
    }]

    scanner = PolymarketScanner()
    opps = scanner.scan_intra_event(events)

    if not opps:
        print("✅ Корректно: сумма < 1.00 не помечена как арбитраж")
    else:
        print("❌ Ложное срабатывание! Сумма < 1.00 — не арбитраж")

    print()
    return len(opps) == 0


def test_zero_prices_filtered():
    """Тест 5: Рынки с нулевыми ценами должны игнорироваться."""
    print("=" * 50)
    print("ТЕСТ 5: Фильтр нулевых цен")
    print("=" * 50)

    events = [{
        "title": "Broken market",
        "slug": "broken",
        "negRisk": False,
        "markets": [
            make_market("Zero price event", 0.0, 0.0),  # цены не загрузились
        ]
    }]

    scanner = PolymarketScanner()
    opps = scanner.scan_intra_market(events)

    if not opps:
        print("✅ Корректно: нулевые цены отфильтрованы")
    else:
        print("❌ Нулевые цены не отфильтрованы!")

    print()
    return len(opps) == 0


def test_profit_calc():
    """Тест 6: Проверка расчёта прибыли с комиссиями."""
    print("=" * 50)
    print("ТЕСТ 6: Расчёт прибыли")
    print("=" * 50)

    # Покупаем YES+NO за $0.95, получаем $1.00
    # Gross profit: ($1 - $0.95) / $0.95 = 5.26%
    # Fee: 2% от $0.05 = $0.001
    # Gas: $0.01
    # Net: $0.05 - $0.001 - $0.01 = $0.039
    gross, net, usd = PolymarketScanner.calc_net_profit(0.95, 1.0)

    print(f"  Cost: $0.95 → Payout: $1.00")
    print(f"  Gross: {gross:+.2f}%")
    print(f"  Net:   {net:+.2f}%")
    print(f"  На $100: ${usd:+.2f}")

    ok = gross > 5.0 and net > 3.0
    print(f"  {'✅' if ok else '❌'} Расчёт {'корректен' if ok else 'неверен'}")
    print()
    return ok


if __name__ == "__main__":
    results = {
        "Intra-market арбитраж": test_intra_market(),
        "Нет ложных срабатываний": test_no_arb(),
        "NegRisk арбитраж": test_intra_event_negrisk(),
        "NegRisk без ложных": test_negrisk_no_arb(),
        "Фильтр нулевых цен": test_zero_prices_filtered(),
        "Расчёт прибыли": test_profit_calc(),
    }

    print("=" * 50)
    print("РЕЗУЛЬТАТЫ")
    print("=" * 50)
    for name, passed in results.items():
        print(f"  {'✅' if passed else '❌'} {name}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {passed}/{total} тестов пройдено")
