"""
Gabagool Strategy — Step 2: Signal Bot

Monitors active BTC 15-min markets on Polymarket.
Sends Telegram alerts when YES or NO prices dip below thresholds,
and when the virtual pair cost drops low enough to be profitable.

Usage:
    python signaler.py                    # default thresholds
    python signaler.py --threshold 0.45  # custom buy threshold
"""

import argparse
import time
from datetime import datetime, timezone

import httpx

from observer import MarketFinder, PriceLogger
from notifier import send_telegram

# ── Config ────────────────────────────────────────────────────────────────────
BUY_THRESHOLD = 0.47    # Alert when YES or NO midpoint drops below this
PAIR_THRESHOLD = 0.95   # Alert when virtual pair cost (min_yes + min_no) drops below this
POLL_INTERVAL = 3       # Seconds between REST polls
WINDOW_DURATION = 960   # Seconds before we consider a window expired (16 min)

CLOB_API = "https://clob.polymarket.com"


class WindowState:
    """Per-window tracking state. Reset at the start of each 15-min window."""

    def __init__(self):
        self.min_yes = 999.0
        self.min_no = 999.0
        self.best_yes_bid = 0.0
        self.best_no_bid = 0.0
        self.tick_count = 0

        # Alert flags — each fires at most once per window
        self.alerted_yes_buy = False
        self.alerted_no_buy = False
        self.alerted_pair = False

    @property
    def virtual_pair_cost(self) -> float:
        """Lowest possible pair cost seen this window."""
        if self.min_yes >= 999 or self.min_no >= 999:
            return 999.0
        return self.min_yes + self.min_no

    def update(self, yes_price: float, no_price: float,
               yes_bid: float, no_bid: float):
        self.tick_count += 1
        if yes_price > 0:
            self.min_yes = min(self.min_yes, yes_price)
        if no_price > 0:
            self.min_no = min(self.min_no, no_price)
        if yes_bid > 0:
            self.best_yes_bid = max(self.best_yes_bid, yes_bid)
        if no_bid > 0:
            self.best_no_bid = max(self.best_no_bid, no_bid)


class GabagoolSignaler:

    def __init__(self, buy_threshold: float = BUY_THRESHOLD,
                 pair_threshold: float = PAIR_THRESHOLD):
        self.buy_threshold = buy_threshold
        self.pair_threshold = pair_threshold

        self.finder = MarketFinder()
        self.logger = PriceLogger()
        self.http = httpx.Client(timeout=10.0)

        self.current_market: dict | None = None
        self.state = WindowState()
        self.running = False

    # ── Price fetching ────────────────────────────────────────────────────────

    def _fetch_token_data(self, token_id: str) -> dict:
        """Returns mid, best_bid, best_ask for a single token."""
        result = {"mid": 0.0, "best_bid": 0.0, "best_ask": 0.0}
        try:
            r = self.http.get(f"{CLOB_API}/midpoint", params={"token_id": token_id})
            if r.status_code == 200:
                result["mid"] = float(r.json().get("mid", 0))

            r2 = self.http.get(f"{CLOB_API}/book", params={"token_id": token_id})
            if r2.status_code == 200:
                book = r2.json()
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids:
                    result["best_bid"] = float(bids[0].get("price", 0))
                if asks:
                    result["best_ask"] = float(asks[0].get("price", 0))
        except Exception as e:
            print(f"\n[!] Fetch error ({token_id[:8]}...): {e}")
        return result

    def poll_prices(self):
        """Fetches YES/NO prices, updates state, checks alert triggers."""
        token_ids = self.current_market.get("token_ids", [])
        if not token_ids:
            return

        yes = self._fetch_token_data(token_ids[0]) if len(token_ids) > 0 else {}
        no  = self._fetch_token_data(token_ids[1]) if len(token_ids) > 1 else {}

        yes_mid = yes.get("mid", 0.0)
        no_mid  = no.get("mid", 0.0)

        if yes_mid == 0 and no_mid == 0:
            return  # No data yet

        self.state.update(yes_mid, no_mid, yes.get("best_bid", 0), no.get("best_bid", 0))

        self.logger.log(
            yes_mid, no_mid,
            yes.get("best_bid", 0), yes.get("best_ask", 0),
            no.get("best_bid", 0), no.get("best_ask", 0),
            source="rest",
        )

        self._check_alerts(yes_mid, no_mid)
        self._print_status(yes_mid, no_mid)

    # ── Alert logic ───────────────────────────────────────────────────────────

    def _check_alerts(self, yes_price: float, no_price: float):
        slug = self.current_market.get("slug", "?")
        pair = self.state.virtual_pair_cost
        profit_pct = round((1.0 - pair) * 100, 2)

        if yes_price > 0 and yes_price < self.buy_threshold and not self.state.alerted_yes_buy:
            msg = f"🟢 BUY YES signal @ {yes_price:.4f} | Market: {slug}"
            print(f"\n[ALERT] {msg}")
            send_telegram(msg)
            self.state.alerted_yes_buy = True

        if no_price > 0 and no_price < self.buy_threshold and not self.state.alerted_no_buy:
            msg = f"🟢 BUY NO signal @ {no_price:.4f} | Market: {slug}"
            print(f"\n[ALERT] {msg}")
            send_telegram(msg)
            self.state.alerted_no_buy = True

        if pair < self.pair_threshold and not self.state.alerted_pair:
            msg = (
                f"⚡ PAIR signal: YES@{self.state.min_yes:.4f} + NO@{self.state.min_no:.4f} "
                f"= {pair:.4f} | Potential profit: {profit_pct}%"
            )
            print(f"\n[ALERT] {msg}")
            send_telegram(msg)
            self.state.alerted_pair = True

    def _send_window_summary(self):
        slug = self.current_market.get("slug", "?")
        pair = self.state.virtual_pair_cost
        profit_pct = round((1.0 - pair) * 100, 2) if pair < 999 else 0.0

        min_yes_str = f"{self.state.min_yes:.4f}" if self.state.min_yes < 999 else "n/a"
        min_no_str  = f"{self.state.min_no:.4f}"  if self.state.min_no  < 999 else "n/a"
        pair_str    = f"{pair:.4f}"                if pair < 999         else "n/a"

        msg = (
            f"📊 Window closed: {slug}\n"
            f"Best YES: {min_yes_str}\n"
            f"Best NO: {min_no_str}\n"
            f"Best pair: {pair_str} → {profit_pct}% potential\n"
            f"Ticks: {self.state.tick_count}"
        )
        print(f"\n\n[SUMMARY]\n{msg}")
        send_telegram(msg)

    # ── Terminal output ───────────────────────────────────────────────────────

    def _print_status(self, yes_price: float, no_price: float):
        current_sum = yes_price + no_price
        spread = 1.0 - current_sum
        pair = self.state.virtual_pair_cost

        if spread > 0.02:
            dot = "🟢"
        elif spread > 0:
            dot = "🟡"
        else:
            dot = "🔴"

        pair_flag = " ⚡" if pair < self.pair_threshold else ""

        min_yes_str = f"{self.state.min_yes:.4f}" if self.state.min_yes < 999 else "-.----"
        min_no_str  = f"{self.state.min_no:.4f}"  if self.state.min_no  < 999 else "-.----"
        pair_str    = f"{pair:.4f}"                if pair < 999         else "-.----"

        print(
            f"  {dot} YES={yes_price:.4f} NO={no_price:.4f} SUM={current_sum:.4f} | "
            f"minYES={min_yes_str} minNO={min_no_str} PAIR={pair_str}{pair_flag} | "
            f"tick#{self.state.tick_count}",
            end="\r",
        )

    # ── Market lifecycle ──────────────────────────────────────────────────────

    def _find_market(self) -> bool:
        print("\n[*] Searching for active BTC 15-min market...")
        market = self.finder.find_current_btc_15m()
        if not market:
            print("[!] No active market found. Will retry in 30s.")
            return False

        self.current_market = market
        self.state = WindowState()

        print(f"[+] Found: {market['question']}")
        print(f"    Slug:  {market['slug']}")
        if market.get("prices"):
            print(f"    Prices: YES={market['prices'][0]:.4f}  NO={market['prices'][1]:.4f}")
        print(f"    Thresholds: BUY<{self.buy_threshold}  PAIR<{self.pair_threshold}")

        self.logger.start_new_session(market["slug"])
        return True

    def run(self):
        print("=" * 60)
        print("  ⚡ Gabagool Signaler — BTC 15-Min Signal Bot")
        print(f"  BUY threshold : {self.buy_threshold}")
        print(f"  PAIR threshold: {self.pair_threshold}")
        print("  Ctrl+C to stop")
        print("=" * 60)

        self.running = True

        while self.running:
            if not self._find_market():
                time.sleep(30)
                continue

            print(f"\n[*] Monitoring (REST every {POLL_INTERVAL}s)...\n")
            window_start = time.time()

            while self.running:
                self.poll_prices()

                elapsed = time.time() - window_start
                if elapsed > WINDOW_DURATION:
                    self._send_window_summary()
                    print(f"\n[*] Window expired after {elapsed:.0f}s. "
                          f"Rows logged: {self.logger.row_count}")
                    break

                try:
                    time.sleep(POLL_INTERVAL)
                except KeyboardInterrupt:
                    self.running = False
                    break

            if self.running:
                print("\n[*] Looking for next window...")
                time.sleep(5)

        self.logger.close()
        print("\n\n⛔ Stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gabagool BTC 15-min signal bot")
    parser.add_argument(
        "--threshold", type=float, default=BUY_THRESHOLD,
        help=f"Buy threshold for YES/NO price alerts (default: {BUY_THRESHOLD})",
    )
    parser.add_argument(
        "--pair-threshold", type=float, default=PAIR_THRESHOLD,
        help=f"Pair cost threshold for pair alerts (default: {PAIR_THRESHOLD})",
    )
    args = parser.parse_args()

    signaler = GabagoolSignaler(
        buy_threshold=args.threshold,
        pair_threshold=args.pair_threshold,
    )
    try:
        signaler.run()
    except KeyboardInterrupt:
        signaler.logger.close()
        print("\n⛔ Stopped")


if __name__ == "__main__":
    main()
