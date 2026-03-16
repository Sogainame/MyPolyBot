"""
Gabagool Strategy — Pre-Order 15-Minute Markets

Places limit buy orders on BOTH sides 2 minutes BEFORE the next window
starts, when the market is still ~50/50. Monitors fills during the window.
If both fill → guaranteed profit from the complete set (YES+NO = $1.00).

Based on crellOS/polymarket-arbitrage-bot-pre-order-15m-markets.

Usage:
    python trader.py                           # DRY RUN (safe)
    python trader.py --live                    # LIVE (real money!)
    python trader.py --live --shares 5 --price-limit 0.45
"""

import argparse
import json
import math
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
PRICE_LIMIT = 0.45             # Limit buy price for both sides
SHARES = 5                     # Shares per order
PLACE_ORDER_BEFORE_SECS = 120  # Place orders 2 min before next window
CHECK_INTERVAL = 0.5           # 500ms main loop
FILL_CHECK_INTERVAL = 1.0      # Check fills every 1s
STABLE_MIN = 0.30              # Good signal: prices in this range
STABLE_MAX = 0.70
CLEAR_THRESHOLD = 0.90         # Bad signal: one side above this
DANGER_PRICE = 0.28            # Sell one-sided fill if price drops below
DANGER_TIME_SECS = 900         # Sell one-sided fill after 15 min
WINDOW_SECS = 900              # 15 minutes

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slug_short(slug: str) -> str:
    """'btc-updown-15m-1773573300' → '15m-1773573300'"""
    parts = slug.split("btc-updown-")
    return parts[-1] if len(parts) > 1 else slug


# ── Per-cycle state ──────────────────────────────────────────────────────────

@dataclass
class WindowState:
    # Which window we placed orders for
    window_ts: int = 0
    next_market_slug: str = ""

    # Orders
    yes_order_id: str | None = None
    no_order_id:  str | None = None
    yes_order_placed_at: float = 0.0
    no_order_placed_at:  float = 0.0

    # Fills
    yes_filled:     bool  = False
    no_filled:      bool  = False
    yes_fill_price: float = 0.0
    no_fill_price:  float = 0.0
    yes_fill_time:  float = 0.0
    no_fill_time:   float = 0.0

    # Control
    orders_placed:    bool = False
    signal_checked:   bool = False
    signal_attempted: bool = False
    pair_alerted:     bool = False
    danger_sold:      bool = False

    # First fill timestamp (for danger timeout)
    fill_time: float = 0.0

    # Token IDs for the target market
    yes_token_id: str = ""
    no_token_id:  str = ""

    @property
    def both_filled(self) -> bool:
        return self.yes_filled and self.no_filled

    @property
    def one_filled(self) -> bool:
        return (self.yes_filled or self.no_filled) and not self.both_filled

    @property
    def pair_cost(self) -> float:
        return self.yes_fill_price + self.no_fill_price

    @property
    def edge(self) -> float:
        """Profit per $1 payout after 2% winner fee."""
        return round((1.0 - self.pair_cost) * 0.98, 4)


# ── Main trader class ─────────────────────────────────────────────────────────

class GabagoolTrader:

    def __init__(
        self,
        dry_run: bool = True,
        shares: int = SHARES,
        price_limit: float = PRICE_LIMIT,
    ):
        self.dry_run     = dry_run
        self.shares      = shares
        self.price_limit = price_limit

        self.finder = MarketFinder()
        self.logger = PriceLogger()
        self.http   = httpx.Client(timeout=10.0)

        self.state = WindowState()
        self.running = False

        # Session stats
        self.total_pairs:      int   = 0
        self.total_incomplete: int   = 0
        self.total_skipped:    int   = 0
        self.total_no_fills:   int   = 0
        self.total_profit:     float = 0.0
        self.window_count:     int   = 0

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

    # ── Price fetching ────────────────────────────────────────────────────────

    def _fetch_midpoint(self, token_id: str) -> float:
        try:
            r = self.http.get(
                f"{CLOB_HOST}/midpoint",
                params={"token_id": token_id},
                timeout=5.0,
            )
            if r.status_code == 200:
                mid = float(r.json().get("mid", 0))
                if 0.02 < mid < 0.98:
                    return mid
        except Exception as e:
            print(f"\n[!] Price fetch error: {e}")
        return 0.0

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
                    alert = f"⚠️ Low balance: {bal_s}"
                    print(f"\n  [!] {alert}")
                    send_telegram(alert)
                    return None, err_str

                if "invalid" in err_low and attempt == 1:
                    time.sleep(2)
                    continue

                return None, err_str

        return None, "max retries exceeded"

    # ── Window timing ─────────────────────────────────────────────────────────

    def _get_next_window_ts(self) -> int:
        """Returns unix timestamp of the next 15-min window start."""
        now = time.time()
        return int(math.ceil(now / WINDOW_SECS) * WINDOW_SECS)

    def _get_current_window_ts(self) -> int:
        """Returns unix timestamp of the current 15-min window start."""
        now = time.time()
        return int(math.floor(now / WINDOW_SECS) * WINDOW_SECS)

    # ── Market lookup by timestamp ────────────────────────────────────────────

    def _find_market_by_ts(self, ts: int) -> dict | None:
        """Look up a specific 15-min market by its window timestamp."""
        slug = f"btc-updown-15m-{ts}"
        try:
            resp = self.http.get(
                f"{GAMMA_API}/markets",
                params={"slug": slug},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            markets = data if isinstance(data, list) else [data]

            for m in markets:
                if not m or m.get("slug") != slug:
                    continue
                # Parse token IDs
                clob_ids = m.get("clobTokenIds", "")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                if len(clob_ids) < 2:
                    continue

                prices = m.get("outcomePrices", "")
                if isinstance(prices, str):
                    try:
                        prices = [float(p) for p in json.loads(prices)]
                    except Exception:
                        prices = []

                return {
                    "slug": slug,
                    "question": m.get("question", ""),
                    "condition_id": m.get("conditionId", ""),
                    "token_ids": clob_ids,
                    "prices": prices,
                }

        except Exception as e:
            print(f"\n[!] Market lookup error for {slug}: {e}")
        return None

    # ── Signal check (on current market) ──────────────────────────────────────

    def _check_signal(self) -> tuple[bool, float, float]:
        """
        Check signal on CURRENT market. Returns (good, yes_mid, no_mid).
        Uses MarketFinder which correctly handles server time.
        """
        market = self.finder.find_current_btc_15m()
        if not market or len(market.get("token_ids", [])) < 2:
            return False, 0.0, 0.0

        token_ids = market["token_ids"]
        yes_mid = self._fetch_midpoint(token_ids[0])
        no_mid  = self._fetch_midpoint(token_ids[1])

        if yes_mid <= 0.01 or no_mid <= 0.01:
            return False, yes_mid, no_mid

        good = (STABLE_MIN <= yes_mid <= STABLE_MAX
                and yes_mid < CLEAR_THRESHOLD
                and no_mid < CLEAR_THRESHOLD)
        return good, yes_mid, no_mid

    # ── Pre-order placement ───────────────────────────────────────────────────

    def _place_orders_for_ts(self, target_ts: int, label: str = "PRE-ORDER"):
        """Place YES and NO limit buys on the market at target_ts."""
        slug = f"btc-updown-15m-{target_ts}"
        slug_s = _slug_short(slug)

        market = self._find_market_by_ts(target_ts)
        if not market:
            print(f"\n  [!] Market not found: {slug}")
            return

        token_ids = market["token_ids"]
        self.state.window_ts = target_ts
        self.state.next_market_slug = slug
        self.state.yes_token_id = token_ids[0]
        self.state.no_token_id  = token_ids[1]

        now = time.time()
        price = self.price_limit
        shares = self.shares

        if self.dry_run:
            self.state.yes_order_id = f"DRY-YES-{target_ts}"
            self.state.no_order_id  = f"DRY-NO-{target_ts}"
            print(f"\n  [DRY] Would place YES@{price} + NO@{price} x{shares}sh on {slug_s}")
        else:
            yes_id, yes_err = self._submit_order(token_ids[0], price, shares, "YES")
            no_id,  no_err  = self._submit_order(token_ids[1], price, shares, "NO")
            self.state.yes_order_id = yes_id
            self.state.no_order_id  = no_id

            errors = []
            if not yes_id:
                errors.append(f"YES: {yes_err[:200]}")
            if not no_id:
                errors.append(f"NO: {no_err[:200]}")
            if errors:
                msg = f"⚠️ Order error: {'; '.join(errors)}"
                print(f"\n  [!] {msg}")
                send_telegram(msg)

        self.state.yes_order_placed_at = now
        self.state.no_order_placed_at  = now
        self.state.orders_placed = True

        combined = price * 2
        edge_pct = round((1.0 - combined) * 100, 1)
        msg = (f"📋 {label}: YES@{price} + NO@{price} = ${combined:.2f}"
               f" ({edge_pct}% edge) x{shares}sh | {slug_s}")
        print(f"\n  [{label}] {msg}")
        send_telegram(msg)

        self.logger.start_new_session(slug)

    # ── Fill checking ─────────────────────────────────────────────────────────

    def _check_fills(self):
        """Check fill status for active orders."""
        if self.dry_run:
            self._check_fills_dry()
        else:
            self._check_fills_live()

        # Pair complete alert (once)
        if self.state.both_filled and not self.state.pair_alerted:
            self._on_pair_complete()

    def _check_fills_live(self):
        """LIVE: poll exchange for fill status."""
        if self.state.yes_order_id and not self.state.yes_filled:
            if self.state.yes_order_id.startswith("DRY-"):
                return
            try:
                resp = self.clob.get_order(self.state.yes_order_id)
                status = resp.get("status", "") if isinstance(resp, dict) else ""
                if status in ("MATCHED", "FILLED"):
                    fill_price = float(resp.get("price", self.price_limit))
                    self._record_fill("YES", fill_price)
            except Exception as e:
                print(f"\n  [!] Fill check failed (YES): {e}")

        if self.state.no_order_id and not self.state.no_filled:
            if self.state.no_order_id.startswith("DRY-"):
                return
            try:
                resp = self.clob.get_order(self.state.no_order_id)
                status = resp.get("status", "") if isinstance(resp, dict) else ""
                if status in ("MATCHED", "FILLED"):
                    fill_price = float(resp.get("price", self.price_limit))
                    self._record_fill("NO", fill_price)
            except Exception as e:
                print(f"\n  [!] Fill check failed (NO): {e}")

    def _check_fills_dry(self):
        """DRY RUN: simulate fill when midpoint drops to or below limit price."""
        if not self.state.yes_token_id or not self.state.no_token_id:
            return

        if not self.state.yes_filled and self.state.yes_order_id:
            yes_mid = self._fetch_midpoint(self.state.yes_token_id)
            if yes_mid > 0.02 and yes_mid <= self.price_limit:
                self._record_fill("YES", self.price_limit)

        if not self.state.no_filled and self.state.no_order_id:
            no_mid = self._fetch_midpoint(self.state.no_token_id)
            if no_mid > 0.02 and no_mid <= self.price_limit:
                self._record_fill("NO", self.price_limit)

    def _record_fill(self, side: str, fill_price: float):
        ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        now = time.time()
        sim = " (sim)" if self.dry_run else ""

        if side == "YES" and not self.state.yes_filled:
            self.state.yes_filled = True
            self.state.yes_fill_price = fill_price
            self.state.yes_fill_time = now
            if self.state.fill_time == 0.0:
                self.state.fill_time = now
            print(f"\n  [FILL] YES @ {fill_price}{sim} at {ts_str}")
        elif side == "NO" and not self.state.no_filled:
            self.state.no_filled = True
            self.state.no_fill_price = fill_price
            self.state.no_fill_time = now
            if self.state.fill_time == 0.0:
                self.state.fill_time = now
            print(f"\n  [FILL] NO @ {fill_price}{sim} at {ts_str}")

    def _on_pair_complete(self):
        """Alert when both sides fill. Called once."""
        self.state.pair_alerted = True
        self.total_pairs += 1

        s = self.state
        slug_s = _slug_short(s.next_market_slug)
        profit = round(s.edge * self.shares, 2)
        pct    = round((1.0 - s.pair_cost) * 100, 1)
        self.total_profit += profit

        msg = (
            f"✅ PAIR: YES@{s.yes_fill_price} + NO@{s.no_fill_price}"
            f" = ${s.pair_cost:.2f} -> $1.00 = +${profit:.2f} ({pct}% edge)\n"
            f"Window: {slug_s}"
        )
        print(f"\n  [PAIR] {msg}")
        send_telegram(msg)

    # ── Cancel open orders ────────────────────────────────────────────────────

    def _cancel_all_open_orders(self):
        if self.dry_run:
            self.state.yes_order_id = None
            self.state.no_order_id = None
            return
        try:
            self.clob.cancel_all()
        except Exception as e:
            print(f"\n[!] cancel_all failed: {e}")

    # ── Window summary ────────────────────────────────────────────────────────

    def _send_window_summary(self):
        s      = self.state
        slug_s = _slug_short(s.next_market_slug) if s.next_market_slug else "?"

        if s.danger_sold:
            side  = "YES" if s.yes_filled else "NO"
            price = s.yes_fill_price if s.yes_filled else s.no_fill_price
            result_line = f"🚨 DANGER EXIT: {side} sold @ market (was filled @ {price})"
            self.total_incomplete += 1
        elif s.both_filled:
            profit = round(s.edge * self.shares, 2)
            pct    = round((1.0 - s.pair_cost) * 100, 1)
            result_line = (
                f"✅ PAIR: YES@{s.yes_fill_price} + NO@{s.no_fill_price}"
                f" = ${s.pair_cost:.2f} -> +${profit:.2f} ({pct}% edge)"
            )
        elif s.yes_filled:
            result_line = f"⚠️ INCOMPLETE: only YES filled @ {s.yes_fill_price} — DIRECTIONAL BET"
            self.total_incomplete += 1
        elif s.no_filled:
            result_line = f"⚠️ INCOMPLETE: only NO filled @ {s.no_fill_price} — DIRECTIONAL BET"
            self.total_incomplete += 1
        elif s.orders_placed:
            result_line = "😴 NO FILLS: orders placed but neither filled"
            self.total_no_fills += 1
        else:
            result_line = f"⏭️ SKIP: signal check failed"
            self.total_skipped += 1

        bal_str = self._balance_str()
        bal_line = f"💰 Balance: {bal_str}"

        pair_rate = (self.total_pairs / self.window_count * 100) if self.window_count > 0 else 0.0
        stats_line = (
            f"📊 pairs: {self.total_pairs}, incomplete: {self.total_incomplete},"
            f" skipped: {self.total_skipped}, empty: {self.total_no_fills}"
            f" / total: {self.window_count} windows"
            f" | {pair_rate:.1f}% pair rate | profit: ${self.total_profit:.2f}"
        )

        console_msg = (
            f"\n\n[SUMMARY] {slug_s}\n"
            f"{result_line}\n"
            f"{bal_line}\n"
            f"{stats_line}"
        )
        print(console_msg)

        # Telegram: pairs already sent live; incomplete always
        if not s.both_filled and (s.yes_filled or s.no_filled):
            side  = "YES" if s.yes_filled else "NO"
            price = s.yes_fill_price if s.yes_filled else s.no_fill_price
            send_telegram(
                f"⚠️ INCOMPLETE: {side} only @ {price} — DIRECTIONAL BET\n"
                f"Window: {slug_s}"
            )

        if self.window_count % 5 == 0 and self.window_count > 0:
            send_telegram(f"{bal_line}\n{stats_line}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        mode_label = "LIVE" if not self.dry_run else "DRY RUN"
        bal_str = self._balance_str()

        print("=" * 60)
        print(f"  🤖 Gabagool Pre-Order — {mode_label}")
        print(f"  Price limit     : {self.price_limit}")
        print(f"  Shares/order    : {self.shares}")
        print(f"  Pre-order       : {PLACE_ORDER_BEFORE_SECS}s before window")
        print(f"  Signal range    : [{STABLE_MIN}, {STABLE_MAX}]")
        print(f"  Balance         : {bal_str}")
        print("  Ctrl+C to stop")
        print("=" * 60)

        send_telegram(
            f"🤖 Gabagool Pre-Order | {mode_label} | Balance: {bal_str}\n"
            f"Price: {self.price_limit} | Shares: {self.shares}"
        )

        self.running = True
        last_fill_check = 0.0

        while self.running:
            now = time.time()
            next_ts = self._get_next_window_ts()
            secs_to_next = next_ts - now

            # ── Phase 1: Pre-order (2 min before next window) ─────
            if not self.state.orders_placed and not self.state.signal_attempted:
                if secs_to_next <= PLACE_ORDER_BEFORE_SECS:
                    # Check signal exactly ONCE
                    self.state.signal_attempted = True
                    good, yes_mid, no_mid = self._check_signal()

                    if good:
                        print(f"\n  [SIGNAL] GOOD: YES={yes_mid:.3f} NO={no_mid:.3f}"
                              f" | placing orders {secs_to_next:.0f}s before window")
                        self._place_orders_for_ts(self._get_next_window_ts(), "PRE-ORDER")
                    else:
                        if yes_mid >= CLEAR_THRESHOLD or no_mid >= CLEAR_THRESHOLD:
                            reason = f"market decided (YES={yes_mid:.3f} NO={no_mid:.3f})"
                        elif yes_mid <= 0.01 or no_mid <= 0.01:
                            reason = f"bad price data (YES={yes_mid:.3f} NO={no_mid:.3f})"
                        else:
                            reason = f"outside range (YES={yes_mid:.3f} NO={no_mid:.3f})"

                        print(f"\n  [SKIP] Bad signal: {reason}")
                        self.state.signal_checked = True
                        self.state.window_ts = next_ts

            # ── Phase 1b: Mid-market orders (if no active orders) ─
            if (not self.state.orders_placed
                    and not self.state.signal_checked
                    and not self.state.signal_attempted
                    and secs_to_next > PLACE_ORDER_BEFORE_SECS):
                current_ts = self._get_current_window_ts()
                current_window_end = current_ts + WINDOW_SECS
                time_remaining = current_window_end - now

                if time_remaining > DANGER_TIME_SECS + PLACE_ORDER_BEFORE_SECS:
                    good, yes_mid, no_mid = self._check_signal()
                    self.state.signal_attempted = True

                    if good:
                        print(f"\n  [MID-MARKET] GOOD signal: YES={yes_mid:.3f} NO={no_mid:.3f}"
                              f" | placing orders on current window ({time_remaining:.0f}s left)")
                        self._place_orders_for_ts(current_ts, "MID-MARKET")
                    else:
                        # Bad signal for mid-market, will retry for pre-order later
                        self.state.signal_attempted = False
                        print(
                            f"  [WAITING] secs_to_next={secs_to_next:.0f}"
                            f" | pre-order in {secs_to_next - PLACE_ORDER_BEFORE_SECS:.0f}s"
                            f" {('[DRY]' if self.dry_run else '[LIVE]')}",
                            end="\r",
                        )
                else:
                    print(
                        f"  [WAITING] secs_to_next={secs_to_next:.0f}"
                        f" | pre-order in {secs_to_next - PLACE_ORDER_BEFORE_SECS:.0f}s"
                        f" {('[DRY]' if self.dry_run else '[LIVE]')}",
                        end="\r",
                    )

            # ── Phase 2: Monitoring fills ─────────────────────────
            if self.state.orders_placed:
                # Check fills
                if now - last_fill_check >= FILL_CHECK_INTERVAL:
                    self._check_fills()
                    last_fill_check = now

                # ── Danger logic for one-sided fills ──────────────
                if self.state.one_filled and not self.state.danger_sold:
                    filled_side = "YES" if self.state.yes_filled else "NO"
                    filled_token = self.state.yes_token_id if self.state.yes_filled else self.state.no_token_id
                    time_since_fill = now - self.state.fill_time

                    current_price = self._fetch_midpoint(filled_token)
                    danger = False

                    if current_price > 0.02 and current_price <= DANGER_PRICE:
                        danger = True
                        reason = f"price={current_price:.3f} <= {DANGER_PRICE}"
                    elif time_since_fill >= DANGER_TIME_SECS:
                        danger = True
                        reason = f"time={time_since_fill:.0f}s >= {DANGER_TIME_SECS}s"

                    if danger:
                        self.state.danger_sold = True
                        slug_s = _slug_short(self.state.next_market_slug)
                        if self.dry_run:
                            print(f"\n  [DANGER] Would sell {filled_side} @ {current_price:.3f} ({reason})")
                        else:
                            self._cancel_all_open_orders()
                            print(f"\n  [DANGER] Selling {filled_side} @ market ({reason})")
                        msg = f"⚠️ DANGER EXIT: sold {filled_side} @ {current_price:.3f} ({reason}) | {slug_s}"
                        send_telegram(msg)

                # Status line
                slug_s = _slug_short(self.state.next_market_slug)
                mode_tag = "[DRY]" if self.dry_run else "[LIVE]"
                yes_s = f"FILLED@{self.state.yes_fill_price}" if self.state.yes_filled else "open"
                no_s  = f"FILLED@{self.state.no_fill_price}"  if self.state.no_filled  else "open"
                pair  = " PAIR!" if self.state.both_filled else ""

                secs_into = WINDOW_SECS - secs_to_next if secs_to_next < WINDOW_SECS else 0
                print(
                    f"  YES@{self.price_limit}({yes_s}) NO@{self.price_limit}({no_s})"
                    f"{pair} | {slug_s} {secs_into:.0f}s {mode_tag}",
                    end="\r",
                )

            # ── Window expiry: reset when next window starts ──────
            if self.state.window_ts > 0 and now >= self.state.window_ts + WINDOW_SECS:
                self._cancel_all_open_orders()
                self.window_count += 1
                self._send_window_summary()
                window_slug = _slug_short(self.state.next_market_slug)
                print(f"\n[*] Window {window_slug} ended.")
                self.state = WindowState()
                last_fill_check = 0.0
                continue

            # Also reset if signal was bad and we've passed the window start
            if (self.state.signal_checked and not self.state.orders_placed
                    and self.state.window_ts > 0 and now >= self.state.window_ts):
                self.window_count += 1
                self._send_window_summary()
                self.state = WindowState()
                last_fill_check = 0.0
                continue

            try:
                time.sleep(CHECK_INTERVAL)
            except KeyboardInterrupt:
                self.running = False
                break

        self._cancel_all_open_orders()
        self.logger.close()
        print("\n\n⛔ Stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gabagool BTC 15-min pre-order trader")
    parser.add_argument(
        "--live", action="store_true",
        help="Enable LIVE trading (real money). Default is DRY RUN.",
    )
    parser.add_argument(
        "--shares", type=int, default=SHARES,
        help=f"Shares per order (default: {SHARES})",
    )
    parser.add_argument(
        "--price-limit", type=float, default=PRICE_LIMIT,
        help=f"Limit buy price for both sides (default: {PRICE_LIMIT})",
    )
    args = parser.parse_args()

    dry_run = not args.live

    if not dry_run:
        print()
        print("!" * 60)
        print("  ⚠️  LIVE MODE — REAL MONEY WILL BE SPENT ⚠️")
        print(f"  {args.shares}sh/order @ ${args.price_limit}/side")
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
        shares=args.shares,
        price_limit=args.price_limit,
    )
    try:
        trader.run()
    except KeyboardInterrupt:
        trader._cancel_all_open_orders()
        trader.logger.close()
        print("\n⛔ Stopped")


if __name__ == "__main__":
    main()
