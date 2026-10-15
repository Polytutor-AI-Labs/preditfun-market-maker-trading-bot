from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import StrEnum
from time import monotonic, time


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    market_id: str
    bids: list[Level]
    asks: list[Level]
    tick_size: Decimal | None = None
    update_timestamp_ms: int | None = field(default=None, compare=False)
    received_at: float = field(default_factory=monotonic, compare=False)
    received_timestamp_ms: int = field(default_factory=lambda: int(time() * 1000), compare=False)
    source: str = field(default="rest", compare=False)

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if not self.best_bid or not self.best_ask:
            return None
        return self.best_ask.price - self.best_bid.price


@dataclass(frozen=True)
class Quote:
    market_id: str
    side: Side
    price: Decimal
    size: Decimal
    outcome: str = "YES"
    token_id: str | None = None
    fee_rate_bps: int | None = None
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None
    outcome_side: str | None = None


@dataclass(frozen=True)
class ExitContext:
    group_id: str
    source_order_id: str
    target_size: Decimal
    sold_before: Decimal = Decimal("0")
    source_settlement_key: str | None = None


@dataclass
class ManagedOrder:
    order_id: str
    quote: Quote
    created_at: float
    status: OrderStatus = OrderStatus.OPEN
    order_hash: str | None = None
    filled_size: Decimal = Decimal("0")
    is_emergency_exit: bool = False
    exit_context: ExitContext | None = None
    expires_at: float | None = None
    # REST cumulative fills and delayed WS deltas are separate counters. Persist
    # both so a late notification after restart cannot sell the same fill again.
    wallet_filled_size: Decimal = Decimal("0")
    wallet_settlement_ids: set[str] = field(default_factory=set)
    # Matched is NOT settled. Keep speculative exit coverage separate from
    # factual fills, and persist plans before an asynchronous sell can start.
    matched_settlements: dict[str, Decimal] = field(default_factory=dict)
    failed_settlement_ids: set[str] = field(default_factory=set)
    exit_plans: list[ExitContext] = field(default_factory=list)
    completed_exit_groups: set[str] = field(default_factory=set)
    exit_baseline_size: Decimal | None = None

    @property
    def age_seconds(self) -> float:
        return monotonic() - self.created_at


@dataclass(frozen=True)
class WalletFillEvent:
    order_id: str
    filled_size: Decimal
    order_hash: str | None = None
    settlement_id: str | None = None
    event_type: str = "orderTransactionSuccess"
    market_id: str | None = None
    side: Side | None = None
    outcome: str | None = None
    price: Decimal | None = None
    order_size: Decimal | None = None
    received_at: float = field(default_factory=monotonic)
    event_timestamp_ms: int | None = None
    cumulative_filled_size: Decimal | None = None


@dataclass(frozen=True)
class WalletOrderStatusEvent:
    order_id: str
    event_type: str
    order_hash: str | None = None
    reason: str | None = None
    market_id: str | None = None
    side: Side | None = None
    outcome: str | None = None
    price: Decimal | None = None
    order_size: Decimal | None = None


def quantize_price(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    rounding = ROUND_DOWN if side == Side.BUY else ROUND_UP
    ticks = (price / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size
