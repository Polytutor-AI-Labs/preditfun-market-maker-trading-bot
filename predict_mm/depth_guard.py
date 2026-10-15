"""Bounded, in-memory depth observations; no networking, signing or disk I/O."""
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from predict_mm.config import DepthProtectionConfig
from predict_mm.models import ManagedOrder, OrderBook, OrderStatus, Quote, Side

Selection = tuple[str, str]
ZERO = Decimal("0")


def selection(quote: Quote) -> Selection:
    return quote.market_id, (quote.outcome_side or quote.outcome).strip().upper()


def depth_ahead(quote: Quote, book: OrderBook, owned: list[ManagedOrder]) -> Decimal:
    """Strictly better prices only. Remove our own equivalent BUY/SELL liquidity."""
    outcome = selection(quote)[1]
    if outcome not in {"YES", "NO"} or quote.side != Side.BUY:
        return ZERO
    own_by_price: dict[Decimal, Decimal] = {}
    for order in owned:
        if (order.quote.market_id != quote.market_id
                or order.status not in {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN}):
            continue
        other = selection(order.quote)[1]
        if other == outcome and order.quote.side == Side.BUY:
            price = order.quote.price
        elif other in {"YES", "NO"} and other != outcome and order.quote.side == Side.SELL:
            price = Decimal("1") - order.quote.price
        else:
            continue
        if price > quote.price:
            own_by_price[price] = own_by_price.get(price, ZERO) + max(
                ZERO, order.quote.size - order.filled_size,
            )
    total = ZERO
    levels = book.asks if outcome == "NO" else book.bids
    for level in levels:
        price = Decimal("1") - level.price if outcome == "NO" else level.price
        if price <= quote.price:  # OrderBook levels are best-first.
            break
        total += max(ZERO, level.size - own_by_price.get(price, ZERO))
    return total


@dataclass
class DepthState:
    price: Decimal
    size: Decimal
    # A monotonic maximum queue avoids scanning the whole two-second window.
    peaks: deque[tuple[float, Decimal]] = field(default_factory=deque)
    sample_at: float = -1
    good_since: float | None = None
    last_seen: float = 0
    last_reason: str | None = None


@dataclass(frozen=True)
class DepthObservation:
    depth: Decimal
    peak: Decimal
    cancel_threshold: Decimal
    resume_threshold: Decimal
    reason: str | None
    ready: bool


class DepthGuard:
    MAX_PEAKS = 512
    REST_MAX_AGE_SECONDS = 2.0

    def __init__(self, config: DepthProtectionConfig):
        self.config = config
        self.states: dict[Selection, DepthState] = {}
        # None means a depth-triggered cancellation has not been acknowledged yet.
        self.cooldowns: dict[Selection, float | None] = {}

    def observe(self, quote: Quote, book: OrderBook, owned: list[ManagedOrder], *,
                now: float, live: bool) -> DepthObservation:
        key = selection(quote)
        state = self.states.get(key)
        if state is None or (state.price, state.size) != (quote.price, quote.size):
            state = self.states[key] = DepthState(quote.price, quote.size)
        state.last_seen = now
        depth = depth_ahead(quote, book, owned)
        cancel = max(self.config.cancel_min_shares, self.config.cancel_size_multiplier * quote.size)
        resume = max(self.config.resume_min_shares, self.config.resume_size_multiplier * quote.size)
        # Reusing a stale REST snapshot cannot establish that liquidity recovered.
        fresh = live or 0 <= now - book.received_at <= self.REST_MAX_AGE_SECONDS
        while state.peaks and state.peaks[0][0] < now - self.config.drop_window_seconds:
            state.peaks.popleft()
        peak = max(depth, state.peaks[0][1] if state.peaks else ZERO)
        reason = None
        if depth < cancel:
            reason = "low_depth"
        elif (peak > ZERO and depth < resume
              and (peak - depth) * 100 >= peak * self.config.drop_percent):
            reason = "rapid_drop"
        if book.received_at > state.sample_at and fresh:
            while state.peaks and state.peaks[-1][1] <= depth:
                state.peaks.pop()
            # Fail closed on an extreme burst rather than silently losing its peak.
            if len(state.peaks) >= self.MAX_PEAKS:
                reason = "history_overflow"
            else:
                state.peaks.append((book.received_at, depth))
            state.sample_at = book.received_at
        if not fresh or depth < resume or reason is not None:
            state.good_since = None
        elif state.good_since is None:
            state.good_since = now
        cooldown = self.cooldowns.get(key, 0.0)
        ready = (fresh and cooldown is not None and now >= cooldown
                 and state.good_since is not None
                 and now - state.good_since >= self.config.stable_seconds)
        return DepthObservation(depth, peak, cancel, resume, reason, ready)

    def block(self, quote: Quote) -> None:
        key = selection(quote)
        self.cooldowns[key] = None
        if key in self.states:
            self.states[key].good_since = None

    def acknowledged(self, quote: Quote, now: float) -> None:
        self.cooldowns[selection(quote)] = now + self.config.cooldown_seconds

    def disconnected(self) -> None:
        for state in self.states.values():
            state.good_since = None
            state.peaks.clear()
            state.sample_at = -1

    def prune(self, keep: set[Selection], now: float) -> None:
        for key, state in list(self.states.items()):
            if key not in keep and now - state.last_seen > 60:
                del self.states[key]
                # Never forget a cancellation still pending acknowledgement.
        for key, deadline in list(self.cooldowns.items()):
            if key not in keep and deadline is not None and deadline <= now:
                self.cooldowns.pop(key, None)
