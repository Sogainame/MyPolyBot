"""
Gabagool Strategy — Step 3: Automated Trader

Limit-order market-making on BTC 15-min markets.  At window start, places
two GTC limit buys (YES + NO) below current midpoint.  Monitors fills for
the duration of the window.  If both fill → guaranteed profit.

Usage:
    python trader.py                                           # DRY RUN (safe)
    python trader.py --live                                    # LIVE (real money!)
    python trader.py --live --order-size 2 --spread 0.03
"""

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from observer import MarketFinder, PriceLogger
from notifier import send_telegram

load_dotenv()

# ── Trading constants ─────────────────────────────────────────────────────────
SPREAD = 0.03               # Place order this far below midpoint
MIN_YES_MID = 0.35          # Skip window if either side below this
MAX_YES_MID = 0.65          # Skip window if either side above this
ORDER_SIZE = 2.0            # USDC per side
MAX_PAIR_COST = 0.96        # Only trade if combined order prices below this
POLL_INTERVAL = 2.0         # Seconds between price checks
FILL_CHECK_INTERVAL = 5.0   # Seconds between fill status polls
WINDOW_DURATION = 960       # Seconds per window (16 min)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug_short(slug: str) -> str:
    """'btc-updown-15m-1773573300' → '15m-1773573300'"""
    parts = slug.split("btc-updown-")
    return parts[-1] if len(parts) > 1 else slug


# ── Per-window state ──────────────────────────────────────────────────────────

@dataclass
class WindowState:
    yes_order_id: str | None = None
    no_order_id:  str | None = None
    yes_order_price: float = 0.0
    no_order_price:  float = 0.0
    yes_shares: float = 0.0
    no_shares:  float = 0.0

    yes_filled:     bool  = False
    no_filled:      bool  = False
    yes_fill_price: float = 0.0
    no_fill_price:  float = 0.0

    skipped:     bool = False
    skip_reason: str  = ""

    # Set True after pair alert sent (prevent duplicate alerts)
    pair_alerted: bool = False

    # Price extremes seen during window
    seen_min_yes: float = float("inf")
    seen_min_no:  float = float("inf")

    @property
    def both_filled(self) -> bool:
        return self.yes_filled and self.no_filled

    @property
    def pair_cost(self) -> float:
        return self.yes_fill_price + self.no_fill_price

    @property
    def edge(self) -> float:
        """Profit per $1 payout after 2% winner fee."""
        return round((1.0 - self.pair_cost) * 0.98, 4)

    @property
    def orders_placed(self) -> bool:
        return self.yes_order_id is not None or self.no_order_id is not None


# ── Main trader class ─────────────────────────────────────────────────────────

class GabagoolTrader:

    def __init__(
        self,
        dry_run: bool = True,
        order_size: float = ORDER_SIZE,
        spread: float = SPREAD,
    ):
        self.dry_run    = dry_run
        self.order_size = order_size
        self.spread     = spread

        self.finder = MarketFinder()
        self.logger = PriceLogger()
        self.http   = httpx.Client(timeout=10.0)

        self.current_market: dict | None = None
        self.state = WindowState()
        self.running = False

        # Session stats
        self.total_pairs:      int = 0
        self.total_incomplete: int = 0
        self.total_skipped:    int = 0
        self.total_no_fills:   int = 0
        self.window_count:     int = 0

        self.clob = self._init_clob_client()

    # ── CLOB client initialisation ────────────────────────────────────────────

    def _init_clob_client(self) -> ClobClient:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLY_PRIVATE_KEY"),
            chain_id=137,
            signature_type=1,
            funder=os.getenv("POLY_FUNDER_ADDRESS"),
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    # ── Balance fetching ──────────────────────────────────────────────────────

    def _get_balance(self) -> float | None:
        """Returns balance in USDC, or None if fetch fails."""
        try:
            resp = self.clob.get_balance_allowance()
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                return raw / 1e6 if raw > 10_000 else raw
        except Exception:
            pass

        funder = os.getenv("POLY_FUNDER_ADDRESS", "")
        if funder:
            try:
                r = self.http.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": funder.lower()},
                )
                if r.status_code == 200:
                    data = r.json()
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        for key in ("portfolioValue", "value", "cashBalance", "balance"):
                            if key in entry:
                                return float(entry[key])
            except Exception:
                pass

        return None

    def _balance_str(self) -> str:
        bal = self._get_balance()
        return f"${bal:.2f}" if bal is not None else "n/a"

    # ── Price fetching (REST) ─────────────────────────────────────────────────

    def _fetch_midpoint(self, token_id: str) -> float:
        try:
            r = self.http.get(
                "https://clob.polymarket.com/midpoint",
                params={"token_id": token_id},
                timeout=5.0
            )
            if r.status_code == 200:
                mid = float(r.json().get("mid", 0))
                if 0.02 < mid < 0.98:
                    return mid
        except Exception as e:
            print(f"\n[!] Price fetch error: {e}")
        return 0.0

    def _fetch_prices(self) -> tuple[float, float] | None:
        """Fetch YES and NO midpoints via REST. Returns (yes_mid, no_mid) or None."""
        token_ids = self.current_market.get("token_ids", [])
        if len(token_ids) < 2:
            return None
        yes_mid = self._fetch_midpoint(token_ids[0])
        no_mid  = self._fetch_midpoint(token_ids[1])
        return yes_mid, no_mid

    # ── Order submission ──────────────────────────────────────────────────────

    def _submit_order(
        self, token_id: str, price: float, shares: float, side_label: str
    ) -> tuple[str | None, str]:
        """
        Submits a single GTC limit buy.
        Returns (order_id, error_str). order_id is None on failure.
        """
        for attempt in range(1, 3):
            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 2),
                    size=shares,
                    side=BUY,
                )
                signed   = self.clob.create_order(order_args)
                resp     = self.clob.post_order(signed, OrderType.GTC)
                order_id = resp.get("orderID") if isinstance(resp, dict) else None
                print(f"\n  [ORDER] {side_label} @ {price} | {shares}sh | ID: {order_id or '?'}")
                return order_id, ""

            except Exception as e:
                err_str = str(e)
                err_low = err_str.lower()
                print(f"\n  [!] Order failed ({side_label} @ {price}) attempt {attempt}: {err_str}")

                if any(kw in err_low for kw in ("not enough", "balance", "allowance", "insufficient")):
                    bal = self._get_balance()
                    bal_s = f"${bal:.2f}" if bal is not None else "n/a"
                    if bal is not None and bal < self.order_size:
                        alert = f"⚠️ Low balance: {bal_s} (need ${self.order_size})"
                        print(f"\n  [!] {alert}")
                        send_telegram(alert)
                    return None, err_str

                if "invalid" in err_low and attempt == 1:
                    print(f"\n  [*] Retrying {side_label} in 2s...")
                    time.sleep(2)
                    continue

                return None, err_str

        return None, "max retries exceeded"

    # ── Step 1-3: Evaluate window and place orders ────────────────────────────

    def _place_window_orders(self, yes_mid: float, no_mid: float) -> bool:
        """
        Evaluate whether to trade this window and place both limit orders.
        Returns True if orders placed, False if window skipped.
        """
        slug_s = _slug_short(self.current_market.get("slug", "?"))

        # Step 1: Check if market is uncertain enough (close to 50/50)
        if yes_mid < MIN_YES_MID or yes_mid > MAX_YES_MID:
            self.state.skipped = True
            self.state.skip_reason = f"not 50/50 (YES_mid={yes_mid:.3f})"
            print(f"\n  [SKIP] {self.state.skip_reason}")
            return False

        # Step 2: Calculate order prices and check edge
        yes_price = round(yes_mid - self.spread, 2)
        no_price  = round(no_mid - self.spread, 2)
        combined  = yes_price + no_price

        if combined >= MAX_PAIR_COST:
            self.state.skipped = True
            self.state.skip_reason = f"edge too small ({combined:.3f} >= {MAX_PAIR_COST})"
            print(f"\n  [SKIP] {self.state.skip_reason}")
            return False

        # Step 3: Place both orders
        self.state.yes_order_price = yes_price
        self.state.no_order_price  = no_price

        yes_shares = round(self.order_size / yes_price, 2)
        no_shares  = round(self.order_size / no_price, 2)
        self.state.yes_shares = yes_shares
        self.state.no_shares  = no_shares

        edge_pct = round((1.0 - combined) * 100, 1)
        print(f"\n  Midpoints: YES={yes_mid:.4f} NO={no_mid:.4f}")
        print(f"  Orders:    YES@{yes_price} ({yes_shares}sh) + NO@{no_price} ({no_shares}sh)")
        print(f"  Combined:  ${combined:.2f} | edge {edge_pct}%")

        if self.dry_run:
            self.state.yes_order_id = "DRY-YES"
            self.state.no_order_id  = "DRY-NO"
            print(f"\n  [DRY] Would place YES@{yes_price} + NO@{no_price} | {slug_s}")
        else:
            token_ids = self.current_market.get("token_ids", [])
            yes_id, yes_err = self._submit_order(token_ids[0], yes_price, yes_shares, "YES")
            no_id,  no_err  = self._submit_order(token_ids[1], no_price,  no_shares,  "NO")
            self.state.yes_order_id = yes_id
            self.state.no_order_id  = no_id

            errors = []
            if not yes_id:
                errors.append(f"YES: {yes_err[:200]}")
            if not no_id:
                errors.append(f"NO: {no_err[:200]}")
            if errors:
                msg = f"⚠️ Order error: {'; '.join(errors)} | {slug_s}"
                print(f"\n  [!] {msg}")
                send_telegram(msg)
                if not yes_id and not no_id:
                    return False

        msg = (f"📋 Orders placed: YES@{yes_price} + NO@{no_price}"
               f" = ${combined:.2f} ({edge_pct}% edge) | {slug_s}")
        print(f"\n  [ORDERS] {msg}")
        send_telegram(msg)
        return True

    # ── Step 4: Fill checking ─────────────────────────────────────────────────

    def _check_fills(self, yes_mid: float, no_mid: float):
        """Check fill status. LIVE polls exchange, DRY RUN simulates."""
        if self.dry_run:
            self._simulate_fills(yes_mid, no_mid)
        else:
            self._poll_live_fills()

    def _poll_live_fills(self):
        """Poll exchange for order fill status (LIVE mode)."""
        if self.state.yes_order_id and not self.state.yes_filled:
            self._poll_order_fill("YES", self.state.yes_order_id, self.state.yes_order_price)

        if self.state.no_order_id and not self.state.no_filled:
            self._poll_order_fill("NO", self.state.no_order_id, self.state.no_order_price)

        if self.state.both_filled and not self.state.pair_alerted:
            self._on_pair_complete()

    def _poll_order_fill(self, side: str, order_id: str, order_price: float):
        try:
            resp   = self.clob.get_order(order_id)
            status = resp.get("status", "") if isinstance(resp, dict) else ""
            if status in ("MATCHED", "FILLED"):
                fill_price = float(resp.get("price", order_price))
                self._record_fill(side, fill_price)
        except Exception as e:
            print(f"\n  [!] Fill check failed ({side}): {e}")

    def _simulate_fills(self, yes_mid: float, no_mid: float):
        """DRY RUN: simulate fill when midpoint drops to or below order price."""
        if not self.state.yes_filled and yes_mid > 0.02 and yes_mid <= self.state.yes_order_price:
            self._record_fill("YES", self.state.yes_order_price)

        if not self.state.no_filled and no_mid > 0.02 and no_mid <= self.state.no_order_price:
            self._record_fill("NO", self.state.no_order_price)

        if self.state.both_filled and not self.state.pair_alerted:
            self._on_pair_complete()

    def _record_fill(self, side: str, fill_price: float):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if side == "YES" and not self.state.yes_filled:
            self.state.yes_filled     = True
            self.state.yes_fill_price = fill_price
            sim = " (sim)" if self.dry_run else ""
            print(f"\n  [FILL] YES @ {fill_price}{sim} at {ts}")
        elif side == "NO" and not self.state.no_filled:
            self.state.no_filled     = True
            self.state.no_fill_price = fill_price
            sim = " (sim)" if self.dry_run else ""
            print(f"\n  [FILL] NO @ {fill_price}{sim} at {ts}")

    def _on_pair_complete(self):
        """Send alert when both sides fill. Called exactly once per window."""
        self.state.pair_alerted = True
        self.total_pairs += 1

        slug_s = _slug_short(self.current_market.get("slug", "?"))
        s      = self.state
        shares = min(s.yes_shares, s.no_shares)
        net    = round(s.edge * shares, 2)
        pct    = round((1.0 - s.pair_cost) * 100, 1)

        msg = (
            f"✅ PAIR: YES@{s.yes_fill_price} + NO@{s.no_fill_price}"
            f" = ${s.pair_cost:.2f} -> $1.00 = +${net:.2f} ({pct}% edge)\n"
            f"Window: {slug_s}"
        )
        print(f"\n  [PAIR] {msg}")
        send_telegram(msg)

    # ── Terminal display ──────────────────────────────────────────────────────

    def _print_status(self, yes_mid: float, no_mid: float, tick_num: int):
        mode_tag = "[DRY]" if self.dry_run else "[LIVE]"
        s = self.state

        yes_status = "FILLED" if s.yes_filled else "open"
        no_status  = "FILLED" if s.no_filled  else "open"
        pair_tag   = " PAIR!" if s.both_filled else ""

        print(
            f"  YES_mid={yes_mid:.4f} NO_mid={no_mid:.4f}"
            f" | YES@{s.yes_order_price}({yes_status})"
            f" NO@{s.no_order_price}({no_status})"
            f"{pair_tag} | tick#{tick_num} {mode_tag}",
            end="\r",
        )

    # ── Step 5: Window summary ────────────────────────────────────────────────

    def _send_window_summary(self):
        slug   = self.current_market.get("slug", "?")
        slug_s = _slug_short(slug)
        s      = self.state

        min_yes_str = f"{s.seen_min_yes:.4f}" if s.seen_min_yes != float("inf") else "n/a"
        min_no_str  = f"{s.seen_min_no:.4f}"  if s.seen_min_no  != float("inf") else "n/a"

        if s.skipped:
            result_line = f"⏭️ SKIP: {s.skip_reason}"
            self.total_skipped += 1
        elif s.both_filled:
            shares = min(s.yes_shares, s.no_shares)
            net    = round(s.edge * shares, 2)
            pct    = round((1.0 - s.pair_cost) * 100, 1)
            result_line = (
                f"✅ PAIR: YES@{s.yes_fill_price} + NO@{s.no_fill_price}"
                f" = ${s.pair_cost:.2f} -> $1.00 = +${net:.2f} profit ({pct}% edge)"
            )
        elif s.yes_filled:
            result_line = f"⚠️ INCOMPLETE: only YES filled @ {s.yes_fill_price} — DIRECTIONAL BET"
            self.total_incomplete += 1
        elif s.no_filled:
            result_line = f"⚠️ INCOMPLETE: only NO filled @ {s.no_fill_price} — DIRECTIONAL BET"
            self.total_incomplete += 1
        elif s.orders_placed:
            result_line = "😴 NO FILLS: orders placed but expired"
            self.total_no_fills += 1
        else:
            result_line = "😴 No orders placed"
            self.total_no_fills += 1

        bal_str = self._balance_str()
        bal_line = f"💰 Balance: {bal_str}"

        pair_rate = (self.total_pairs / self.window_count * 100) if self.window_count > 0 else 0.0
        stats_line = (
            f"📊 pairs: {self.total_pairs}, incomplete: {self.total_incomplete},"
            f" skipped: {self.total_skipped}, empty: {self.total_no_fills}"
            f" / total: {self.window_count} windows | {pair_rate:.1f}% pair rate"
        )

        console_msg = (
            f"\n\n[SUMMARY] {slug_s}\n"
            f"{result_line}\n"
            f"Lowest: YES={min_yes_str} NO={min_no_str}\n"
            f"{bal_line}\n"
            f"{stats_line}"
        )
        print(console_msg)

        # Telegram: pairs already sent live; incomplete always; skips/no-fills never
        if s.both_filled:
            pass
        elif s.yes_filled or s.no_filled:
            side  = "YES" if s.yes_filled else "NO"
            price = s.yes_fill_price if s.yes_filled else s.no_fill_price
            send_telegram(
                f"⚠️ INCOMPLETE: {side} only @ {price} — DIRECTIONAL BET\n"
                f"Window: {slug_s}"
            )

        if self.window_count % 5 == 0 and self.window_count > 0:
            send_telegram(f"{bal_line}\n{stats_line}")

    # ── Cancel open orders ────────────────────────────────────────────────────

    def _cancel_all_open_orders(self):
        if self.dry_run:
            print("\n  [DRY RUN] Would cancel all open orders")
            return
        try:
            self.clob.cancel_all()
            print("\n[*] Cancelled all open orders")
        except Exception as e:
            print(f"\n[!] cancel_all failed: {e}")

    # ── Market lifecycle ──────────────────────────────────────────────────────

    def _find_market(self) -> bool:
        print("\n[*] Searching for active BTC 15-min market...")
        market = self.finder.find_current_btc_15m()
        if not market:
            print("[!] No active market found. Will retry in 30s.")
            return False

        self.current_market = market
        self.state          = WindowState()

        print(f"[+] Found: {market['question']}")
        print(f"    Slug:  {market['slug']}")
        if market.get("prices"):
            print(f"    Prices: YES={market['prices'][0]:.4f}  NO={market['prices'][1]:.4f}")
        print(f"    Order size: ${self.order_size}/side | Spread: {self.spread}")

        self.logger.start_new_session(market["slug"])
        return True

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        mode_label = "LIVE" if not self.dry_run else "DRY RUN"
        bal_str = self._balance_str()

        print("=" * 60)
        print(f"  🤖 Gabagool Trader — {mode_label}")
        print(f"  Order size      : ${self.order_size}/side")
        print(f"  Spread          : {self.spread}")
        print(f"  Max pair cost   : {MAX_PAIR_COST}")
        print(f"  Mid range       : [{MIN_YES_MID}, {MAX_YES_MID}]")
        print(f"  Balance         : {bal_str}")
        print("  Ctrl+C to stop")
        print("=" * 60)

        send_telegram(
            f"🤖 Gabagool | {mode_label} | Balance: {bal_str}\n"
            f"Size: ${self.order_size}/side | Spread: {self.spread}"
        )

        self.running = True

        while self.running:
            if not self._find_market():
                time.sleep(30)
                continue

            # Fetch opening prices
            result = self._fetch_prices()
            if result is None or result[0] <= 0.01 or result[1] <= 0.01:
                print("[!] Bad price data. Retrying in 30s.")
                time.sleep(30)
                continue

            yes_mid, no_mid = result

            # Evaluate window and place orders
            orders_placed = self._place_window_orders(yes_mid, no_mid)

            # Monitor window for fills
            print(f"\n[*] Monitoring window ({WINDOW_DURATION}s, poll every {POLL_INTERVAL}s)...\n")
            window_start    = time.time()
            last_fill_check = 0.0
            tick_num        = 0

            while self.running:
                now = time.time()
                tick_num += 1

                # Fetch current prices every POLL_INTERVAL
                result = self._fetch_prices()
                if result is not None:
                    yes_mid, no_mid = result

                    # Track price mins
                    if yes_mid > 0.02:
                        self.state.seen_min_yes = min(self.state.seen_min_yes, yes_mid)
                    if no_mid > 0.02:
                        self.state.seen_min_no = min(self.state.seen_min_no, no_mid)

                    # CSV log
                    self.logger.log(yes_mid, no_mid, 0, 0, 0, 0, source="rest")

                    # Check fills every FILL_CHECK_INTERVAL
                    if orders_placed and (now - last_fill_check >= FILL_CHECK_INTERVAL):
                        self._check_fills(yes_mid, no_mid)
                        last_fill_check = now

                    # Print status line
                    if orders_placed:
                        self._print_status(yes_mid, no_mid, tick_num)

                # Window expiry
                elapsed = now - window_start
                if elapsed > WINDOW_DURATION:
                    self._cancel_all_open_orders()
                    self.window_count += 1
                    self._send_window_summary()
                    print(f"\n[*] Window expired after {elapsed:.0f}s.")
                    break

                try:
                    time.sleep(POLL_INTERVAL)
                except KeyboardInterrupt:
                    self.running = False
                    break

            if self.running:
                self._cancel_all_open_orders()
                print("\n[*] Looking for next window...")
                time.sleep(5)

        self.logger.close()
        print("\n\n⛔ Stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gabagool BTC 15-min automated trader")
    parser.add_argument(
        "--live", action="store_true",
        help="Enable LIVE trading (real money). Default is DRY RUN.",
    )
    parser.add_argument(
        "--order-size", type=float, default=ORDER_SIZE,
        help=f"USDC per side (default: {ORDER_SIZE})",
    )
    parser.add_argument(
        "--spread", type=float, default=SPREAD,
        help=f"Spread below midpoint for limit orders (default: {SPREAD})",
    )
    args = parser.parse_args()

    dry_run = not args.live

    if not dry_run:
        print()
        print("!" * 60)
        print("  ⚠️  LIVE MODE — REAL MONEY WILL BE SPENT ⚠️")
        print(f"  ${args.order_size}/side  —  spread: {args.spread}")
        print("  Press Ctrl+C NOW to abort, or wait 5 seconds to continue...")
        print("!" * 60)
        print()
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("⛔ Aborted before live trading started.")
            return

    trader = GabagoolTrader(
        dry_run=dry_run,
        order_size=args.order_size,
        spread=args.spread,
    )
    try:
        trader.run()
    except KeyboardInterrupt:
        trader._cancel_all_open_orders()
        trader.logger.close()
        print("\n⛔ Stopped")


if __name__ == "__main__":
    main()
