"""
Gabagool Strategy — Step 3: Automated Trader

Monitors each 15-min window, placing limit buys for YES and NO only when
their midpoint drops below the configured threshold.  Polymarket auto-resolves
15-minute markets — no manual redeem/merge is needed.

Usage:
    python trader.py                                           # DRY RUN (safe)
    python trader.py --live                                    # LIVE (real money!)
    python trader.py --live --yes-price 0.49 --no-price 0.49 --order-size 4
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
BUY_PRICE_YES = 0.49        # Threshold / limit price for YES leg
BUY_PRICE_NO  = 0.49        # Threshold / limit price for NO leg
ORDER_SIZE = 4.0            # USDC per side
MAX_SPEND_PER_WINDOW = 10.0 # Max USDC per window (both sides combined)
POLL_INTERVAL = 2.0         # Seconds between REST price polls
WINDOW_DURATION = 960       # Seconds per window (16 min)
FILL_CHECK_INTERVAL = 5.0   # Seconds between order status polls (LIVE only)

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
    # Whether a limit order has been placed this window (one per side max)
    yes_order_placed: bool = False
    no_order_placed:  bool = False

    # Order IDs returned by the exchange
    yes_order_id: str | None = None
    no_order_id:  str | None = None

    # Share quantities submitted
    yes_shares: float = 0.0
    no_shares:  float = 0.0

    # Fill tracking
    yes_filled:     bool  = False
    no_filled:      bool  = False
    yes_fill_price: float = 0.0
    no_fill_price:  float = 0.0

    # Price extremes seen this window (for CSV / summary)
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
        """Guaranteed profit fraction after 2 % winner fee."""
        return round((1.0 - self.pair_cost) * 0.98, 4)


# ── Main trader class ─────────────────────────────────────────────────────────

class GabagoolTrader:

    def __init__(
        self,
        dry_run: bool = True,
        yes_price: float = BUY_PRICE_YES,
        no_price:  float = BUY_PRICE_NO,
        order_size: float = ORDER_SIZE,
        max_spend: float = MAX_SPEND_PER_WINDOW,
    ):
        self.dry_run    = dry_run
        self.yes_price  = yes_price
        self.no_price   = no_price
        self.order_size = order_size
        self.max_spend  = max_spend

        self.finder = MarketFinder()
        self.logger = PriceLogger()
        self.http   = httpx.Client(timeout=10.0)

        self.current_market: dict | None = None
        self.state = WindowState()
        self.running = False

        # Session stats
        self.total_pairs:      int = 0
        self.total_incomplete: int = 0
        self.total_empty:      int = 0
        self.window_count:     int = 0

        # Track directional (incomplete) bets separately
        self.incomplete_bets: list[dict] = []

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
        # Attempt 1: CLOB client
        try:
            resp = self.clob.get_balance_allowance()
            if isinstance(resp, dict):
                raw = float(resp.get("balance", 0) or 0)
                return raw / 1e6 if raw > 10_000 else raw
        except Exception:
            pass

        # Attempt 2: Polymarket data API
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
        """Human-readable balance string. Shows 'n/a' on failure."""
        bal = self._get_balance()
        return f"${bal:.2f}" if bal is not None else "n/a"

    # ── Price fetching (REST) ─────────────────────────────────────────────────

    def _fetch_midpoint(self, token_id: str) -> float:
        """GET /midpoint → returns mid price as float, or 0.0 on failure."""
        try:
            r = self.http.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id})
            if r.status_code == 200:
                return float(r.json().get("mid", 0))
        except Exception as e:
            print(f"\n[!] Fetch error ({token_id[:8]}...): {e}")
        return 0.0

    def _fetch_prices(self) -> tuple[float, float] | None:
        """Fetch YES and NO midpoints via REST. Returns (yes_mid, no_mid) or None."""
        token_ids = self.current_market.get("token_ids", [])
        if len(token_ids) < 2:
            return None
        yes_mid = self._fetch_midpoint(token_ids[0])
        no_mid  = self._fetch_midpoint(token_ids[1])
        return yes_mid, no_mid

    # ── Order placement ───────────────────────────────────────────────────────

    def _calc_shares(self, price: float) -> float:
        shares = round(self.order_size / price, 2)
        if shares < 5.0:
            shares = 5.5
        return shares

    def _maybe_place_order(self, side: str, mid_price: float):
        """
        Gabagool logic: place a limit buy for a side only when its midpoint
        drops below the threshold, and only once per window.
        """
        if side == "YES":
            if self.state.yes_order_placed:
                return
            threshold = self.yes_price
        else:
            if self.state.no_order_placed:
                return
            threshold = self.no_price

        if mid_price <= 0.01 or mid_price > threshold:
            return

        token_ids = self.current_market.get("token_ids", [])
        token_id = token_ids[0] if side == "YES" else (token_ids[1] if len(token_ids) > 1 else None)
        if not token_id:
            return

        shares = self._calc_shares(threshold)
        slug_s = _slug_short(self.current_market.get("slug", "?"))

        if self.dry_run:
            print(f"\n  [DRY RUN] BUY {side}@{threshold} ({shares}sh) | {slug_s} (mid={mid_price:.4f})")
            order_id = f"DRY-{side}"
            # mid < threshold triggered this order, so it fills immediately
            self._record_fill(side, threshold)
        else:
            order_id, err = self._submit_order(token_id, threshold, shares, side)
            if not order_id:
                msg = f"⚠️ {side} order failed: {err[:200]}"
                print(f"\n  [!] {msg}")
                send_telegram(msg)
                return

        if side == "YES":
            self.state.yes_order_placed = True
            self.state.yes_order_id = order_id
            self.state.yes_shares = shares
        else:
            self.state.no_order_placed = True
            self.state.no_order_id = order_id
            self.state.no_shares = shares

        msg = f"📋 {side}@{threshold} placed (mid={mid_price:.4f}) | {slug_s}"
        print(f"\n  [ORDER] {msg}")
        send_telegram(msg)

    def _submit_order(
        self, token_id: str, price: float, shares: float, side_label: str
    ) -> tuple[str | None, str]:
        """
        Submits a single GTC limit buy.
        Returns (order_id, error_str). order_id is None on failure.
        Retries once on 'invalid signature'; pauses on balance errors.
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

                # Balance / allowance — alert and abort (no sleep)
                if any(kw in err_low for kw in ("not enough", "balance", "allowance", "insufficient")):
                    bal = self._get_balance()
                    bal_s = f"${bal:.2f}" if bal is not None else "n/a"
                    if bal is not None and bal < self.order_size:
                        alert = f"⚠️ Low balance: {bal_s} (need ${self.order_size})"
                        print(f"\n  [!] {alert}")
                        send_telegram(alert)
                    return None, err_str

                # Invalid signature — retry once
                if "invalid" in err_low and attempt == 1:
                    print(f"\n  [*] Retrying {side_label} in 2s...")
                    time.sleep(2)
                    continue

                return None, err_str

        return None, "max retries exceeded"

    # ── Fill checking (LIVE only) ─────────────────────────────────────────────

    def _check_live_fills(self):
        """Poll exchange for order fill status. Only used in LIVE mode."""
        if self.state.yes_order_id and not self.state.yes_filled:
            self._poll_order_fill("YES", self.state.yes_order_id, self.yes_price)

        if self.state.no_order_id and not self.state.no_filled:
            self._poll_order_fill("NO", self.state.no_order_id, self.no_price)

    def _poll_order_fill(self, side_label: str, order_id: str, limit_price: float):
        try:
            resp   = self.clob.get_order(order_id)
            status = resp.get("status", "") if isinstance(resp, dict) else ""
            if status in ("MATCHED", "FILLED"):
                fill_price = float(resp.get("price", limit_price))
                self._record_fill(side_label, fill_price)
        except Exception as e:
            print(f"\n  [!] Fill check failed ({side_label}): {e}")

    def _record_fill(self, side_label: str, fill_price: float):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if side_label == "YES" and not self.state.yes_filled:
            self.state.yes_filled     = True
            self.state.yes_fill_price = fill_price
            print(f"\n  [FILL] YES @ {fill_price} at {ts}")
        elif side_label == "NO" and not self.state.no_filled:
            self.state.no_filled     = True
            self.state.no_fill_price = fill_price
            print(f"\n  [FILL] NO @ {fill_price} at {ts}")

        if self.state.both_filled:
            self.total_pairs += 1
            slug_s  = _slug_short(self.current_market.get("slug", "?"))
            s       = self.state
            shares  = min(s.yes_shares, s.no_shares)
            gross   = round((1.0 - s.pair_cost) * shares, 2)
            net     = round(gross * 0.98, 2)            # after 2% fee
            spent   = round(s.pair_cost * shares, 2)
            pct     = round(net / spent * 100, 1) if spent > 0 else 0.0

            msg = (
                f"✅ PAIR COMPLETE: ${s.pair_cost:.2f} → $1.00 = +${net:.2f} profit ({pct:.1f}%)\n"
                f"Window: {slug_s}"
            )
            print(f"\n  [PAIR] {msg}")
            send_telegram(msg)

    # ── Price processing ──────────────────────────────────────────────────────

    def _process_tick(self, yes_mid: float, no_mid: float, tick_num: int):
        """
        Core per-tick logic: log prices, track mins, maybe place orders.
        Called every POLL_INTERVAL for the entire window duration.
        """
        # Skip entire tick if either side reports zero/stale data
        if yes_mid <= 0.01 or no_mid <= 0.01:
            return

        self.logger.log(yes_mid, no_mid, 0, 0, 0, 0, source="rest")

        self.state.seen_min_yes = min(self.state.seen_min_yes, yes_mid)
        self.state.seen_min_no  = min(self.state.seen_min_no, no_mid)

        # Gabagool: check if we should place orders based on current prices
        self._maybe_place_order("YES", yes_mid)
        self._maybe_place_order("NO", no_mid)

        self._print_status(yes_mid, no_mid, tick_num)

    # ── Terminal display ──────────────────────────────────────────────────────

    def _print_status(self, yes_mid: float, no_mid: float, tick_num: int):
        mode_tag = "[DRY]" if self.dry_run else "[LIVE]"

        if self.state.yes_filled:
            yes_status = "FILLED"
        elif self.state.yes_order_placed:
            yes_status = f"ORDER@{self.yes_price}"
        else:
            yes_status = "waiting"

        if self.state.no_filled:
            no_status = "FILLED"
        elif self.state.no_order_placed:
            no_status = f"ORDER@{self.no_price}"
        else:
            no_status = "waiting"

        pair_tag = " PAIR!" if self.state.both_filled else ""

        print(
            f"  YES={yes_mid:.4f}({yes_status}) NO={no_mid:.4f}({no_status})"
            f"{pair_tag} | tick#{tick_num} {mode_tag}",
            end="\r",
        )

    # ── Window summary ────────────────────────────────────────────────────────

    def _send_window_summary(self):
        """
        Prints a full summary to the console every window.
        Sends Telegram only every 5 windows (or on an incomplete fill).
        Never sends Telegram for empty windows.
        """
        slug  = self.current_market.get("slug", "?")
        slug_s = _slug_short(slug)
        s     = self.state

        min_yes_str = f"{s.seen_min_yes:.4f}" if s.seen_min_yes != float("inf") else "n/a"
        min_no_str  = f"{s.seen_min_no:.4f}"  if s.seen_min_no  != float("inf") else "n/a"

        # Console result line
        if s.both_filled:
            result_line = (
                f"✅ PAIR COMPLETE | Pair cost: {s.pair_cost:.4f}"
                f" | Edge after fee: {s.edge:.2%}"
            )
        elif s.yes_filled:
            result_line = f"⚠️ INCOMPLETE — only YES filled @ {s.yes_fill_price}"
        elif s.no_filled:
            result_line = f"⚠️ INCOMPLETE — only NO filled @ {s.no_fill_price}"
        else:
            result_line = "😴 No fills"

        # Balance from API only
        bal_str = self._balance_str()
        bal_line = f"💰 Balance: {bal_str}"

        win_rate = (self.total_pairs / self.window_count * 100) if self.window_count > 0 else 0.0
        stats_line = (
            f"📊 Session: {self.total_pairs} pairs, {self.total_incomplete} incomplete"
            f" / {self.window_count} windows | {win_rate:.1f}% pair rate"
        )

        # Full console print every window
        console_msg = (
            f"\n\n[SUMMARY] {slug_s}\n"
            f"{result_line}\n"
            f"Lowest: YES={min_yes_str} NO={min_no_str}\n"
            f"{bal_line}\n"
            f"{stats_line}"
        )
        print(console_msg)

        # Telegram: incomplete → always; empty → never; pair → already sent live
        if s.both_filled:
            pass
        elif s.yes_filled or s.no_filled:
            side  = "YES" if s.yes_filled else "NO"
            price = s.yes_fill_price if s.yes_filled else s.no_fill_price
            self.total_incomplete += 1
            self.incomplete_bets.append({
                "window": slug_s,
                "side": side,
                "price": price,
                "time": datetime.now(timezone.utc).isoformat(),
            })
            send_telegram(
                f"⚠️ INCOMPLETE — {side} only @ {price} (directional bet)\n"
                f"Window: {slug_s}"
            )
        else:
            self.total_empty += 1

        # Every-5-windows stats summary
        if self.window_count % 5 == 0 and self.window_count > 0:
            tg_stats = f"{bal_line}\n{stats_line}"
            if self.incomplete_bets:
                tg_stats += f"\n⚠️ {len(self.incomplete_bets)} directional bet(s) this session"
            send_telegram(tg_stats)

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
        print(f"    Thresholds: YES<={self.yes_price} + NO<={self.no_price} | ${self.order_size}/side")

        self.logger.start_new_session(market["slug"])
        return True

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        mode_label = "LIVE" if not self.dry_run else "DRY RUN"
        bal_str = self._balance_str()

        print("=" * 60)
        print(f"  🤖 Gabagool Trader — {mode_label}")
        print(f"  YES threshold   : {self.yes_price}")
        print(f"  NO  threshold   : {self.no_price}")
        print(f"  Order size      : ${self.order_size}/side")
        print(f"  Max spend       : ${self.max_spend}/window")
        print(f"  Balance         : {bal_str}")
        print("  Ctrl+C to stop")
        print("=" * 60)

        send_telegram(
            f"🤖 Gabagool | {mode_label} | Balance: {bal_str}\n"
            f"Thresholds: YES<={self.yes_price} / NO<={self.no_price} | ${self.order_size}/side"
        )

        self.running = True

        while self.running:
            if not self._find_market():
                time.sleep(30)
                continue

            # No orders at window start — Gabagool waits for price to drop

            print(f"\n[*] Monitoring window (tick every {POLL_INTERVAL}s, waiting for price < threshold)...\n")
            window_start    = time.time()
            last_fill_check = 0.0
            tick_num        = 0

            while self.running:
                now = time.time()
                tick_num += 1

                # ── REST price poll ───────────────────────────────────
                result = self._fetch_prices()
                if result is not None:
                    yes_mid, no_mid = result
                    self._process_tick(yes_mid, no_mid, tick_num)

                # ── LIVE fill check (every FILL_CHECK_INTERVAL) ───────
                if not self.dry_run and (now - last_fill_check >= FILL_CHECK_INTERVAL):
                    self._check_live_fills()
                    last_fill_check = now

                # ── Window expiry ─────────────────────────────────────
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
        "--yes-price", type=float, default=BUY_PRICE_YES,
        help=f"GTC limit price for YES leg (default: {BUY_PRICE_YES})",
    )
    parser.add_argument(
        "--no-price", type=float, default=BUY_PRICE_NO,
        help=f"GTC limit price for NO leg (default: {BUY_PRICE_NO})",
    )
    parser.add_argument(
        "--order-size", type=float, default=ORDER_SIZE,
        help=f"USDC per side (default: {ORDER_SIZE})",
    )
    parser.add_argument(
        "--max-spend", type=float, default=MAX_SPEND_PER_WINDOW,
        help=f"Max USDC per window (default: {MAX_SPEND_PER_WINDOW})",
    )
    args = parser.parse_args()

    dry_run = not args.live

    if not dry_run:
        print()
        print("!" * 60)
        print("  ⚠️  LIVE MODE — REAL MONEY WILL BE SPENT ⚠️")
        print(f"  YES <= {args.yes_price}  +  NO <= {args.no_price}")
        print(f"  ${args.order_size}/side  —  up to ${args.max_spend}/window")
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
        yes_price=args.yes_price,
        no_price=args.no_price,
        order_size=args.order_size,
        max_spend=args.max_spend,
    )
    try:
        trader.run()
    except KeyboardInterrupt:
        trader._cancel_all_open_orders()
        trader.logger.close()
        print("\n⛔ Stopped")


if __name__ == "__main__":
    main()
