from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from predict_mm.client import (
    PredictClient,
    PredictInsufficientSharesError,
    PredictOrderSubmissionUnknown,
    PredictRateLimitError,
    PredictSubmissionAborted,
)
from predict_mm.config import BotConfig, MarketConfig
from predict_mm.depth_guard import DepthGuard, selection
from predict_mm.models import (
    ExitContext,
    ManagedOrder,
    OrderBook,
    OrderStatus,
    Quote,
    Side,
    WalletFillEvent,
    WalletOrderStatusEvent,
)
from predict_mm.risk import RiskManager
from predict_mm.strategy import PassiveMakerStrategy

logger = logging.getLogger("predict-mm")

MarketTaskKey = tuple[str, str]


@dataclass(frozen=True)
class QuoteReference:
    best_bid: Decimal | None
    best_ask: Decimal | None
    target_price: Decimal


class MarketMakerEngine:
    MARKET_BATCH_SIZE = 20
    MARKET_BATCH_INTERVAL_SECONDS = 1.0
    MARKET_FETCH_CONCURRENCY = 5
    ORDER_SUBMIT_CONCURRENCY = 5
    CANCEL_CONCURRENCY = 3
    NO_SAFE_QUOTE_BACKOFF_SECONDS = 15.0
    DEPTH_WATCH_LIMIT = 64
    DEPTH_WATCH_SECONDS = 60.0

    def __init__(
        self,
        config: BotConfig,
        client: PredictClient,
        strategy: PassiveMakerStrategy,
        risk: RiskManager,
    ) -> None:
        self.config = config
        self.client = client
        self.strategy = strategy
        self.risk = risk
        self.open_orders: dict[str, ManagedOrder] = {}
        self._stop = asyncio.Event()
        self._fill_events: asyncio.Queue[
            WalletFillEvent | WalletOrderStatusEvent | OrderBook
        ] = asyncio.Queue()
        self._wallet_task: asyncio.Task[None] | None = None
        self._wallet_events: asyncio.PriorityQueue[
            tuple[int, int, WalletFillEvent | WalletOrderStatusEvent]
        ] = asyncio.PriorityQueue()
        self._wallet_event_sequence = 0
        self._wallet_processor_task: asyncio.Task[None] | None = None
        self._orderbook_task: asyncio.Task[None] | None = None
        self._cancel_slots = asyncio.Semaphore(self.CANCEL_CONCURRENCY)
        self._cancel_tasks: dict[str, asyncio.Task[bool]] = {}
        self._guard_cancel_tasks: dict[str, asyncio.Task[bool]] = {}
        self._guard_first_attempts: dict[str, asyncio.Future[bool]] = {}
        self._depth_guard = DepthGuard(config.depth_protection)
        self._depth_candidates: dict[tuple[str, str], Quote] = {}
        self._depth_watch_until: dict[str, float] = {}
        self._depth_cancelling: dict[str, Quote] = {}
        self._depth_stream_epoch = 0.0
        self._cancel_not_before = 0.0
        self._orderbook_history: dict[str, deque[OrderBook]] = {}
        self._emergency_tasks: set[asyncio.Task[None]] = set()
        self._emergency_cancel_tasks: dict[str, asyncio.Task[None]] = {}
        self._fill_locks: dict[str, asyncio.Lock] = {}
        self._exit_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._wallet_fill_totals: dict[str, Decimal] = {}
        self._pending_sell_settlements: dict[str, set[str]] = {}
        self._exit_wakeups: dict[str, asyncio.Event] = {}
        self._match_received_at: dict[str, float] = {}
        self._active_exit_groups: set[str] = set()
        self._exit_poll_seconds = 0.5
        self._halted_markets: set[str] = set()
        self._prepared_emergency_markets: set[str] = set()
        self._handled_fill_settlements: set[str] = set()
        self._market_tick_sizes: dict[str, Decimal] = {}
        self._latest_orderbooks: dict[str, OrderBook] = {}
        self._order_quote_references: dict[str, QuoteReference] = {}
        self._extended_lifetime_orders: set[str] = set()
        self._active_market_queue: deque[MarketTaskKey] = deque()
        self._normal_market_queue: deque[MarketTaskKey] = deque(
            self._market_task_key(market) for market in self.config.enabled_markets
        )
        self._no_safe_quote_until: dict[MarketTaskKey, float] = {}
        self._degraded_fill_reconcile_interval_seconds = 0.5
        self._healthy_fill_reconcile_interval_seconds = max(
            2.0, self.config.poll_interval_seconds
        )
        self._emergency_retry_base_seconds = 0.5
        self._shutdown_cancel_retry_base_seconds = 0.5
        self._order_acceptance_timeout_seconds = max(
            5.0, self.config.poll_interval_seconds * 2
        )
        self._run_deadline: float | None = (
            monotonic() + self.config.run_duration_seconds
            if self.config.run_duration_seconds > 0
            else None
        )
        self._run_expires_at: datetime | None = (
            datetime.now(timezone.utc) + timedelta(seconds=self.config.run_duration_seconds)
            if self.config.run_duration_seconds > 0
            else None
        )

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def run_expires_at(self) -> str | None:
        return self._run_expires_at.isoformat() if self._run_expires_at else None

    @property
    def run_remaining_seconds(self) -> int | None:
        if self._run_deadline is None:
            return None
        return max(0, math.ceil(self._run_deadline - monotonic()))

    def market_title(self, market_id: str) -> str:
        configured = next(
            (market.title for market in self.config.markets if market.id == market_id and market.title),
            None,
        )
        cached_title = getattr(self.client, "cached_market_title", None)
        return configured or (cached_title(market_id) if cached_title else "")

    def _outcome_side(self, market: MarketConfig) -> str | None:
        """Resolve the selected label without depending on its display language."""
        if market.outcome_index_set is not None:
            return {1: "YES", 2: "NO"}.get(market.outcome_index_set)
        selected = market.outcome.strip().upper()
        if selected in {"YES", "NO"}:
            return selected
        if selected in {"YES_NO", "YES&NO", "YES AND NO"}:
            return "YES_NO"
        resolve_outcome_side = getattr(self.client, "cached_outcome_side", None)
        if callable(resolve_outcome_side):
            return resolve_outcome_side(market.id, market.outcome)
        return None

    def active_orders(self) -> list[dict[str, object]]:
        orders: list[dict[str, object]] = []
        for order in self.open_orders.values():
            if order.status != OrderStatus.OPEN:
                continue
            orders.append(
                {
                    "order_id": order.order_id,
                    "market_id": order.quote.market_id,
                    "market_title": self.market_title(order.quote.market_id),
                    "side": order.quote.side.value,
                    "outcome": order.quote.outcome,
                    "price": str(order.quote.price),
                    "size": str(order.quote.size),
                    "is_emergency_exit": order.is_emergency_exit,
                    "age_seconds": max(0, math.floor(order.age_seconds)),
                }
            )
        return orders

    async def cancel_all_orders(self) -> None:
        await self._cancel_all_known_markets()

    async def run(self) -> None:
        logger.info(
            "Starting market maker: dry_run=%s, markets=%s",
            self.config.dry_run,
            [market.id for market in self.config.enabled_markets],
        )

        self._restore_tracked_orders()
        started = False
        try:
            if self.config.cancel_all_on_start:
                await self._cancel_all_known_markets()

            if not self.config.dry_run:
                self._wallet_task = asyncio.create_task(self._watch_wallet_fills())
                self._wallet_processor_task = asyncio.create_task(self._process_wallet_events())
                self._orderbook_task = asyncio.create_task(self._watch_active_orderbooks())
                self._resume_emergency_exits()

            if self._run_deadline is not None:
                logger.info(
                    "Run duration enabled: orders will be cancelled and the market maker "
                    "will stop in %s seconds",
                    self.config.run_duration_seconds,
                )

            started = True
            next_quote_at = monotonic()
            next_fill_reconcile_at = (
                monotonic() + self._degraded_fill_reconcile_interval_seconds
            )
            next_lifetime_check_at = monotonic() + 1.0
            while not self._stop.is_set():
                now = monotonic()
                if self._run_deadline is not None and now >= self._run_deadline:
                    logger.info("Run duration reached; cancelling orders and stopping market maker")
                    self._stop.set()
                    break
                if not self._wallet_stream_connected():
                    next_fill_reconcile_at = min(
                        next_fill_reconcile_at,
                        now + self._degraded_fill_reconcile_interval_seconds,
                    )
                if now >= next_fill_reconcile_at:
                    await self._reconcile_buy_fills()
                    next_fill_reconcile_at = (
                        monotonic() + self._fill_reconcile_interval()
                    )
                if now >= next_lifetime_check_at:
                    await self._manage_active_order_lifetimes_from_cache()
                    next_lifetime_check_at = monotonic() + 1.0
                if monotonic() >= next_quote_at:
                    await self._tick()
                    next_quote_at = monotonic() + self.MARKET_BATCH_INTERVAL_SECONDS
                next_deadline = min(
                    next_quote_at,
                    next_fill_reconcile_at,
                    next_lifetime_check_at,
                )
                if self._run_deadline is not None:
                    next_deadline = min(next_deadline, self._run_deadline)
                await self._wait_for_fill_or_deadline(next_deadline)
        finally:
            if self._wallet_task is not None:
                self._wallet_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._wallet_task
            if self._wallet_processor_task is not None:
                self._wallet_processor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._wallet_processor_task
            if self._orderbook_task is not None:
                self._orderbook_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._orderbook_task
            # Stop retry controllers before closing the client or account-wide
            # shutdown cleanup. In-flight HTTP removal may still complete; the
            # shutdown removal and durable fill tracking remain authoritative.
            cancel_tasks = list(self._guard_cancel_tasks.values()) + list(self._cancel_tasks.values())
            for task in cancel_tasks:
                task.cancel()
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
            for task in list(self._emergency_tasks):
                task.cancel()
            if self._emergency_tasks:
                await asyncio.gather(*self._emergency_tasks, return_exceptions=True)
            for task in self._emergency_cancel_tasks.values():
                task.cancel()
            await asyncio.gather(*self._emergency_cancel_tasks.values(), return_exceptions=True)
            if started and self.config.cancel_all_on_shutdown:
                await self._cancel_all_known_markets_safely()
            await self.client.close()
            logger.info("Market maker stopped")

    def _wallet_stream_connected(self) -> bool:
        return bool(getattr(self.client, "wallet_stream_connected", False))

    def _orderbook_stream_connected(self) -> bool:
        return bool(getattr(self.client, "orderbook_stream_connected", False))

    def _fill_reconcile_interval(self) -> float:
        if self._wallet_stream_connected():
            return self._healthy_fill_reconcile_interval_seconds
        return self._degraded_fill_reconcile_interval_seconds

    async def _watch_wallet_fills(self) -> None:
        while not self._stop.is_set():
            try:
                async for event in self.client.stream_wallet_fill_events():
                    if isinstance(event, WalletFillEvent):
                        logger.info(
                            "Wallet fill received: order=%s event=%s settlement=%s event_timestamp_ms=%s",
                            event.order_id, event.event_type, event.settlement_id, event.event_timestamp_ms,
                        )
                    self._wallet_event_sequence += 1
                    await self._wallet_events.put((
                        0 if isinstance(event, WalletFillEvent) else 1,
                        self._wallet_event_sequence, event,
                    ))
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning("Wallet event stream disconnected: %s; retrying shortly", error)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=1)

    async def _process_wallet_events(self) -> None:
        """Consume wallet events independently of quote batches and orderbook traffic."""
        while not self._stop.is_set():
            _, _, event = await self._wallet_events.get()
            try:
                if isinstance(event, WalletOrderStatusEvent):
                    self._handle_wallet_order_status(event)
                else:
                    logger.info(
                        "Wallet fill processing: order=%s event=%s queue_ms=%.1f",
                        event.order_id, event.event_type, (monotonic() - event.received_at) * 1000,
                    )
                    await self._handle_wallet_fill(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Wallet event processing failed; REST reconciliation remains active")
            finally:
                self._wallet_events.task_done()

    def _active_order_market_ids(self) -> set[str]:
        active = {
            order.quote.market_id
            for order in self._working_orders()
            if not order.is_emergency_exit
        }
        now = monotonic()
        return active | {
            market_id for market_id, until in self._depth_watch_until.items()
            if until > now and market_id not in self._halted_markets
        }

    async def _watch_active_orderbooks(self) -> None:
        while not self._stop.is_set():
            try:
                async for orderbook in self.client.stream_orderbook_updates(
                    self._active_order_market_ids
                ):
                    # Check every update here, independently of slow quote
                    # batches. Never await removal HTTP calls in the receiver.
                    await self._handle_orderbook_update(orderbook, wait_for_cancels=False)
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Active-order WebSocket disconnected: %s; using REST fallback until reconnect",
                    error,
                )
            self._depth_guard.disconnected()
            self._depth_stream_epoch = monotonic()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1)

    async def _wait_for_fill_or_deadline(self, deadline: float) -> None:
        while not self._stop.is_set():
            timeout = max(0, min(0.2, deadline - monotonic()))
            if timeout == 0:
                return
            try:
                event = await asyncio.wait_for(self._fill_events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            if isinstance(event, OrderBook):
                await self._handle_orderbook_update(event)
            elif isinstance(event, WalletOrderStatusEvent):
                self._handle_wallet_order_status(event)
            else:
                await self._handle_wallet_fill(event)

    def _cache_orderbook(self, orderbook: OrderBook) -> OrderBook | None:
        previous = self._latest_orderbooks.get(orderbook.market_id)
        if (previous is not None
                and previous.update_timestamp_ms is not None
                and orderbook.update_timestamp_ms is not None
                and orderbook.update_timestamp_ms < previous.update_timestamp_ms):
            return None
        tick_size = orderbook.tick_size or self._market_tick_sizes.get(orderbook.market_id)
        if tick_size is not None and orderbook.tick_size is None:
            orderbook = replace(orderbook, tick_size=tick_size)
        self._latest_orderbooks[orderbook.market_id] = orderbook
        if tick_size is not None:
            self._market_tick_sizes[orderbook.market_id] = tick_size
        history = self._orderbook_history.setdefault(orderbook.market_id, deque(maxlen=32))
        if (not history or history[-1].best_bid != orderbook.best_bid
                or history[-1].best_ask != orderbook.best_ask
                or orderbook.received_at - history[-1].received_at >= 1):
            history.append(orderbook)
        return orderbook

    async def _handle_orderbook_update(
        self, orderbook: OrderBook, *, wait_for_cancels: bool = True,
    ) -> None:
        orderbook = self._cache_orderbook(orderbook)
        if orderbook is None:
            return
        if self.config.replace_on_orderbook_change or self.config.depth_protection.enabled:
            await self._cancel_orders_approached_by_market(
                orderbook.market_id, orderbook, wait=wait_for_cancels,
            )
        if self.config.depth_protection.enabled:
            # Only warm candidates belonging to this update, never all markets.
            owned = [o for o in self.open_orders.values() if o.quote.market_id == orderbook.market_id]
            active = {selection(o.quote) for o in owned
                      if o.status in {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN}
                      and not o.is_emergency_exit}
            for side in ("YES", "NO"):
                key = (orderbook.market_id, side)
                quote = self._depth_candidates.get(key)
                if quote is not None and key not in active:
                    self._depth_observation(quote, orderbook, owned)

    def _depth_observation(self, quote: Quote, book: OrderBook,
                           owned: list[ManagedOrder] | None = None):
        return self._depth_guard.observe(
            quote, book, owned if owned is not None else [
                o for o in self.open_orders.values() if o.quote.market_id == quote.market_id
            ], now=monotonic(),
            live=(book.source == "websocket" and self._orderbook_stream_connected()
                  and book.received_at >= max(
                      self._depth_stream_epoch,
                      self._depth_watch_until.get(quote.market_id, 0) - self.DEPTH_WATCH_SECONDS,
                  )),
        )

    def _watch_depth_candidate(self, quote: Quote) -> None:
        self._depth_candidates[selection(quote)] = quote
        if (quote.market_id not in self._depth_watch_until
                and len(self._depth_watch_until) < self.DEPTH_WATCH_LIMIT):
            self._depth_watch_until[quote.market_id] = monotonic() + self.DEPTH_WATCH_SECONDS

    def _depth_allows_submission(self, quote: Quote, book: OrderBook | None) -> bool:
        if not self.config.depth_protection.enabled or quote.side != Side.BUY:
            return True
        self._watch_depth_candidate(quote)
        if book is None:
            return False
        observation = self._depth_observation(quote, book)
        state = self._depth_guard.states[selection(quote)]
        reason = None if observation.ready else observation.reason or "recovering"
        if state.last_reason != reason:
            logger.info(
                "Depth guard %s: market=%s outcome=%s quote=%s depth=%s cancel_below=%s "
                "resume_at=%s reason=%s; new orders use normal batch scheduling",
                "ready" if observation.ready else "waiting", quote.market_id, quote.outcome,
                quote.price, observation.depth, observation.cancel_threshold,
                observation.resume_threshold, reason,
            )
            state.last_reason = reason
        return observation.ready

    def _prune_depth_watches(self) -> None:
        now = monotonic()
        expired = set()
        for mid, until in list(self._depth_watch_until.items()):
            if until <= now or mid in self._halted_markets:
                del self._depth_watch_until[mid]
                expired.add(mid)
        active = {selection(o.quote) for o in self._working_orders() if not o.is_emergency_exit}
        for key in list(self._depth_candidates):
            state = self._depth_guard.states.get(key)
            if key in active:
                del self._depth_candidates[key]
            elif key[0] in expired or state is None or now - state.last_seen > 60:
                del self._depth_candidates[key]
                self._depth_guard.states.pop(key, None)
        self._depth_guard.prune(active | set(self._depth_candidates), now)

    async def _manage_active_order_lifetimes_from_cache(self) -> None:
        for market in self.config.enabled_markets:
            if not any(
                self._order_matches_market_config(order, market)
                for order in self._working_orders()
                if not order.is_emergency_exit
            ):
                continue
            orderbook = self._latest_orderbooks.get(market.id)
            if orderbook is None:
                continue
            outcome_side = self._outcome_side(market)
            if outcome_side is None:
                continue
            quotes = self.strategy.build_quotes(
                market,
                orderbook,
                outcome_side=outcome_side,
            )
            await self._manage_order_lifetimes(market, orderbook, quotes)

    async def _tick(self) -> None:
        self._prune_depth_watches()
        await self._reconcile_order_statuses()
        markets = self._next_market_batch()
        if not markets:
            return
        try:
            positions = await self.client.get_positions()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Unable to read positions from Predict.fun; pausing new quotes for this cycle: %s",
                error,
            )
            return

        quotes_to_submit: list[Quote] = []
        quote_references: dict[tuple[str, Side, str, str, Decimal], QuoteReference] = {}
        reserved_orders: list[ManagedOrder] = []
        for market, orderbook in await self._fetch_orderbooks(markets):
            if orderbook is None:
                continue
            orderbook = self._cache_orderbook(orderbook) or self._latest_orderbooks[market.id]
            if self.config.replace_on_orderbook_change or self.config.depth_protection.enabled:
                await self._cancel_orders_approached_by_market(market.id, orderbook)
            outcome_side = self._outcome_side(market)
            if outcome_side is None:
                logger.warning(
                    "Unable to map outcome %s on %s to Predict's YES/NO orderbook; "
                    "skipping this market for safety",
                    market.outcome,
                    market.id,
                )
                continue
            quotes = self.strategy.build_quotes(
                market,
                orderbook,
                outcome_side=outcome_side,
            )
            await self._manage_order_lifetimes(market, orderbook, quotes)
            task_key = self._market_task_key(market)
            if not quotes:
                logger.info("No safe quote for %s", market.id)
                self._no_safe_quote_until[task_key] = (
                    monotonic() + self.NO_SAFE_QUOTE_BACKOFF_SECONDS
                )
                continue
            self._no_safe_quote_until.pop(task_key, None)

            active = self._working_orders() + reserved_orders
            missing_quotes = [
                quote
                for quote in quotes
                if not any(
                    order.quote.market_id == quote.market_id
                    and order.quote.side == quote.side
                    and order.quote.outcome.strip().casefold() == quote.outcome.strip().casefold()
                    and not order.is_emergency_exit
                    for order in active
                )
            ]
            missing_quotes = [q for q in missing_quotes if self._depth_allows_submission(q, orderbook)]
            approved = self.risk.filter_quotes(missing_quotes, active, positions)
            for quote in approved:
                quotes_to_submit.append(quote)
                quote_references[self._quote_key(quote)] = self._quote_reference(
                    orderbook,
                    quote,
                )
                reserved_orders.append(
                    ManagedOrder(
                        order_id=f"reserved:{len(reserved_orders)}",
                        quote=quote,
                        created_at=monotonic(),
                        status=OrderStatus.PENDING,
                    )
                )
        await self._submit_quotes(quotes_to_submit, quote_references)
        await self._reconcile_order_statuses()

    def _next_market_batch(self, *, now: float | None = None) -> list[MarketConfig]:
        now = monotonic() if now is None else now
        enabled = [
            market
            for market in self.config.enabled_markets
            if market.id not in self._halted_markets
        ]
        configured_by_key = {
            self._market_task_key(market): market
            for market in enabled
        }
        working_orders = [
            order for order in self._working_orders() if not order.is_emergency_exit
        ]
        active_keys = {
            task_key
            for task_key, market in configured_by_key.items()
            if any(
                self._order_matches_market_config(order, market)
                for order in working_orders
            )
        }

        normal_order = [
            self._market_task_key(market)
            for market in enabled
            if self._market_task_key(market) not in active_keys
        ]
        active_order = [
            self._market_task_key(market)
            for market in enabled
            if self._market_task_key(market) in active_keys
        ]
        self._sync_market_queue(self._normal_market_queue, normal_order)
        self._sync_market_queue(self._active_market_queue, active_order)

        selected_keys: list[MarketTaskKey] = []
        if not self._orderbook_stream_connected():
            selected_keys.extend(
                self._take_market_keys(
                    self._active_market_queue,
                    self.MARKET_BATCH_SIZE,
                )
            )

        remaining = self.MARKET_BATCH_SIZE - len(selected_keys)
        if remaining:
            selected_keys.extend(
                self._take_market_keys(
                    self._normal_market_queue,
                    remaining,
                    eligible=lambda task_key: (
                        self._no_safe_quote_until.get(task_key, 0) <= now
                    ),
                )
            )

        return [configured_by_key[task_key] for task_key in selected_keys]

    @staticmethod
    def _sync_market_queue(
        queue: deque[MarketTaskKey],
        ordered_keys: list[MarketTaskKey],
    ) -> None:
        allowed = set(ordered_keys)
        retained = [task_key for task_key in queue if task_key in allowed]
        seen = set(retained)
        retained.extend(task_key for task_key in ordered_keys if task_key not in seen)
        queue.clear()
        queue.extend(retained)

    @staticmethod
    def _take_market_keys(
        queue: deque[MarketTaskKey],
        limit: int,
        *,
        eligible: Callable[[MarketTaskKey], bool] | None = None,
    ) -> list[MarketTaskKey]:
        if limit <= 0 or not queue:
            return []
        is_eligible = eligible or (lambda _task_key: True)
        selected: list[MarketTaskKey] = []
        for _ in range(len(queue)):
            task_key = queue.popleft()
            queue.append(task_key)
            if is_eligible(task_key):
                selected.append(task_key)
                if len(selected) >= limit:
                    break
        return selected

    @staticmethod
    def _market_task_key(market: MarketConfig) -> MarketTaskKey:
        return market.id, market.outcome.strip().casefold()

    @staticmethod
    def _order_matches_market_config(
        order: ManagedOrder,
        market: MarketConfig,
    ) -> bool:
        if order.quote.market_id != market.id:
            return False
        selected = market.outcome.strip().upper()
        if selected in {"YES_NO", "YES&NO", "YES AND NO"}:
            canonical_outcome = (
                order.quote.outcome_side or order.quote.outcome
            ).strip().upper()
            return canonical_outcome in {"YES", "NO"}
        return (
            order.quote.outcome.strip().casefold()
            == market.outcome.strip().casefold()
        )

    async def _fetch_orderbooks(
        self,
        markets: list[MarketConfig],
    ) -> list[tuple[MarketConfig, OrderBook | None]]:
        semaphore = asyncio.Semaphore(self.MARKET_FETCH_CONCURRENCY)

        async def fetch(market_id: str) -> tuple[str, OrderBook | None]:
            async with semaphore:
                try:
                    return market_id, await self.client.get_orderbook(market_id)
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        "Unable to read orderbook for %s; skipping this market for this cycle: %s",
                        market_id,
                        error,
                    )
                    return market_id, None

        market_ids = list(dict.fromkeys(market.id for market in markets))
        books = dict(await asyncio.gather(*(fetch(market_id) for market_id in market_ids)))
        return [(market, books[market.id]) for market in markets]

    async def _submit_quotes(
        self,
        quotes: list[Quote],
        quote_references: dict[
            tuple[str, Side, str, str, Decimal],
            QuoteReference,
        ] | None = None,
    ) -> None:
        semaphore = asyncio.Semaphore(self.ORDER_SUBMIT_CONCURRENCY)

        async def submit(quote: Quote) -> None:
            async with semaphore:
                if self._stop.is_set() or quote.market_id in self._halted_markets:
                    return
                latest = self._latest_orderbooks.get(quote.market_id)
                if (latest is not None and self.config.replace_on_orderbook_change
                        and self._approached_touch(quote, latest) is not None):
                    logger.info(
                        "Skip stale quote before submission: market=%s outcome=%s price=%s; "
                        "latest book is already within one tick",
                        quote.market_id, quote.outcome, quote.price,
                    )
                    return
                if not self._depth_allows_submission(quote, latest):
                    return
                try:
                    order = await self.client.create_order(quote)
                except Exception as error:  # noqa: BLE001
                    # A rejected passive quote must not bring down the wallet
                    # event stream. In particular, available collateral can
                    # change between risk evaluation and API submission while a
                    # different order is settling. Keeping the engine alive is
                    # essential so a later settlement-success event can still
                    # trigger the emergency exit.
                    logger.warning(
                        "Create quote failed on %s (%s %s %s @ %s); "
                        "skipping this quote and keeping the bot running: %s",
                        quote.market_id,
                        quote.side.value,
                        quote.size,
                        quote.outcome,
                        quote.price,
                        error,
                    )
                    return

                # Register each successful response immediately. Waiting for the
                # entire concurrent batch lets a fast wallet fill overtake local
                # registration when another POST in the batch is still pending.
                reference = (quote_references or {}).get(self._quote_key(order.quote))
                self._register_order(order, reference)
                latest = self._latest_orderbooks.get(quote.market_id)
                if latest is not None and (self.config.replace_on_orderbook_change
                                           or self.config.depth_protection.enabled):
                    # A price update can precede the POST response/registration.
                    # Recheck now, even if no further WS message arrives.
                    await self._cancel_orders_approached_by_market(
                        quote.market_id, latest, wait=False,
                    )
                if quote.market_id in self._halted_markets:
                    # A fill can halt the market while this POST is in flight.
                    await self._cancel_order_safely(order)

        await asyncio.gather(*(submit(quote) for quote in quotes))

    def _register_order(
        self,
        order: ManagedOrder,
        reference: QuoteReference | None = None,
    ) -> ManagedOrder:
        existing = self.open_orders.get(order.order_id)
        if existing is None and order.order_hash:
            existing = next((candidate for candidate in self.open_orders.values()
                             if candidate.order_hash == order.order_hash), None)
            if existing is not None and existing.order_id != order.order_id:
                self.open_orders.pop(existing.order_id, None)
                existing.order_id = order.order_id
                self.open_orders[existing.order_id] = existing
        if existing is None:
            registered = order
            self.open_orders[order.order_id] = registered
        else:
            # A wallet event can reconstruct the order before POST /v1/orders
            # returns. Preserve fill/status progress while enriching its quote
            # with the complete metadata from the eventual response.
            existing.quote = order.quote
            existing.order_hash = existing.order_hash or order.order_hash
            existing.is_emergency_exit = existing.is_emergency_exit or order.is_emergency_exit
            existing.exit_context = order.exit_context or existing.exit_context
            existing.expires_at = order.expires_at or existing.expires_at
            existing.filled_size = max(existing.filled_size, order.filled_size)
            existing.wallet_filled_size = max(existing.wallet_filled_size, order.wallet_filled_size)
            existing.wallet_settlement_ids.update(order.wallet_settlement_ids)
            existing.matched_settlements.update(order.matched_settlements)
            existing.failed_settlement_ids.update(order.failed_settlement_ids)
            existing.completed_exit_groups.update(order.completed_exit_groups)
            if existing.exit_baseline_size is None:
                existing.exit_baseline_size = order.exit_baseline_size
            groups = {plan.group_id for plan in existing.exit_plans}
            existing.exit_plans.extend(plan for plan in order.exit_plans if plan.group_id not in groups)
            registered = existing
        wallet_key = registered.order_hash or registered.order_id
        self._wallet_fill_totals[wallet_key] = max(
            self._wallet_fill_totals.get(wallet_key, Decimal("0")), registered.wallet_filled_size
        )
        if reference is not None:
            self._order_quote_references[registered.order_id] = reference
        self._remember_order(registered)
        return registered

    def _working_orders(self) -> list[ManagedOrder]:
        return [
            order
            for order in self.open_orders.values()
            if order.status in {OrderStatus.PENDING, OrderStatus.OPEN}
        ]

    async def _reconcile_order_statuses(self) -> None:
        """Treat Predict's OPEN orders response as the dashboard source of truth."""
        if self.config.dry_run:
            return
        candidates = [order for order in self._working_orders() if not order.is_emergency_exit]
        if not candidates:
            return
        try:
            official_open_ids = await self.client.get_open_order_ids()
        except Exception as error:  # noqa: BLE001
            logger.warning("Unable to confirm OPEN orders from Predict.fun: %s", error)
            return

        for order in candidates:
            if order.order_id in official_open_ids:
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.OPEN
                    self._remember_order(order)
                    logger.info(
                        "Order accepted and OPEN on Predict.fun: %s (%s %s %s @ %s on %s)",
                        order.order_id,
                        order.quote.side.value,
                        order.quote.size,
                        order.quote.outcome,
                        order.quote.price,
                        order.quote.market_id,
                    )
                continue

            if (
                order.status == OrderStatus.OPEN
                and order.age_seconds < self._order_acceptance_timeout_seconds
            ):
                # The wallet stream can confirm acceptance before the REST list
                # catches up. Give that authoritative event a short grace period.
                continue
            if order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELED
                self._remember_order(order)
                logger.warning(
                    "Order %s is no longer OPEN according to Predict.fun; removed from dashboard",
                    order.order_id,
                )
            elif order.age_seconds >= self._order_acceptance_timeout_seconds:
                logger.warning(
                    "Order submission %s was not confirmed OPEN by Predict.fun within %.1f seconds; "
                    "removing it defensively and keeping it off the dashboard",
                    order.order_id,
                    self._order_acceptance_timeout_seconds,
                )
                await self._cancel_order_safely(order)

    def _handle_wallet_order_status(self, event: WalletOrderStatusEvent) -> None:
        order = self.open_orders.get(event.order_id)
        if order is None and event.order_hash:
            order = next(
                (
                    candidate
                    for candidate in self.open_orders.values()
                    if candidate.order_hash == event.order_hash
                ),
                None,
            )
        if order is None and event.order_hash:
            loader = getattr(self.client, "load_tracked_orders", None)
            if loader:
                order = next((candidate for candidate in loader()
                              if candidate.order_hash == event.order_hash), None)
                if order is not None:
                    order.order_id = event.order_id
                    order = self._register_order(order)
        if order is None:
            order = self._order_from_wallet_context(event)
            if order is not None:
                order.status = (
                    OrderStatus.OPEN
                    if event.event_type == "orderAccepted"
                    else OrderStatus.CANCELED
                )
                self._register_order(order)
                logger.warning(
                    "Recovered wallet %s for order %s from Predict.fun event details",
                    event.event_type,
                    event.order_id,
                )
            else:
                logger.warning(
                    "Predict.fun wallet event %s for unresolved order %s%s",
                    event.event_type,
                    event.order_id,
                    f" ({event.reason})" if event.reason else "",
                )
                return

        if event.event_type == "orderAccepted":
            if order.status != OrderStatus.FILLED:
                order.status = OrderStatus.OPEN
            logger.info("Predict.fun accepted order %s into the orderbook", order.order_id)
        else:
            order.status = OrderStatus.CANCELED
            if order.is_emergency_exit:
                if event.event_type == "orderExpired":
                    order.status = OrderStatus.EXPIRED
                elif event.event_type == "orderNotAccepted":
                    order.status = (OrderStatus.UNKNOWN if event.reason == "rejectedDuplicate"
                                    else OrderStatus.REJECTED)
            logger.warning(
                "Predict.fun %s order %s%s",
                {
                    "orderNotAccepted": "rejected",
                    "orderExpired": "expired",
                    "orderCancelled": "cancelled",
                }.get(event.event_type, event.event_type),
                order.order_id,
                f": {event.reason}" if event.reason else "",
            )
        self._remember_order(order)
        self._wake_exit(order)

    async def _cancel_orders_approached_by_market(
        self, market_id: str, orderbook: OrderBook, *, wait: bool = True,
    ) -> None:
        """Run independent price/depth guards through the same deduplicated cancel path."""
        tick_size = orderbook.tick_size or self.config.strategy.tick_size
        pending: list[asyncio.Future[bool]] = []
        owned = [o for o in self.open_orders.values() if o.quote.market_id == market_id]
        for order in owned:
            if (
                order.status not in {OrderStatus.PENDING, OrderStatus.OPEN}
                or order.is_emergency_exit
                or order.quote.market_id != market_id
            ):
                continue

            touch_price = (self._approached_touch(order.quote, orderbook)
                           if self.config.replace_on_orderbook_change else None)
            depth = (self._depth_observation(order.quote, orderbook, owned)
                     if self.config.depth_protection.enabled and order.quote.side == Side.BUY else None)
            if depth is not None and depth.reason and order.order_id not in self._depth_cancelling:
                self._depth_cancelling[order.order_id] = order.quote
                self._depth_guard.block(order.quote)
                self._watch_depth_candidate(order.quote)
            if touch_price is None and not (depth and depth.reason):
                continue

            task = self._guard_cancel_tasks.get(order.order_id)
            if task is None or task.done():
                triggered_at = monotonic()
                if touch_price is not None:
                    logger.info(
                        "Price guard triggered: order=%s market=%s outcome=%s quote=%s touch=%s "
                        "tick=%s source=%s book_ts_ms=%s received_ts_ms=%s receive_to_trigger_ms=%.1f",
                        order.order_id, market_id, order.quote.outcome, order.quote.price,
                        touch_price, tick_size, orderbook.source, orderbook.update_timestamp_ms,
                        orderbook.received_timestamp_ms, (triggered_at - orderbook.received_at) * 1000,
                    )
                if depth and depth.reason:
                    logger.info(
                        "Depth guard triggered: order=%s market=%s outcome=%s quote=%s "
                        "depth=%s peak=%s cancel_below=%s resume_at=%s reason=%s "
                        "source=%s receive_to_trigger_ms=%.1f",
                        order.order_id, market_id, order.quote.outcome, order.quote.price,
                        depth.depth, depth.peak, depth.cancel_threshold, depth.resume_threshold,
                        depth.reason, orderbook.source,
                        (triggered_at - orderbook.received_at) * 1000,
                    )
                first_attempt = asyncio.get_running_loop().create_future()
                self._guard_first_attempts[order.order_id] = first_attempt
                task = asyncio.create_task(self._run_guard_cancel(order, triggered_at, first_attempt))
                self._guard_cancel_tasks[order.order_id] = task
                task.add_done_callback(
                    lambda done, oid=order.order_id: self._forget_guard_task(oid, done)
                )
            pending.append(self._guard_first_attempts[order.order_id])
        if wait and pending:
            # REST quote construction waits for the first attempts, not an
            # unbounded retry controller. Working orders reserve risk meanwhile.
            await asyncio.gather(*(asyncio.shield(attempt) for attempt in pending))

    def _forget_guard_task(self, order_id: str, task: asyncio.Task) -> None:
        if self._guard_cancel_tasks.get(order_id) is task:
            first_attempt = self._guard_first_attempts.pop(order_id, None)
            if first_attempt is not None and not first_attempt.done():
                first_attempt.set_result(False)
        self._forget_task(self._guard_cancel_tasks, order_id, task)

    @staticmethod
    def _forget_task(tasks: dict, order_id: str, task: asyncio.Task) -> None:
        if tasks.get(order_id) is task:
            tasks.pop(order_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Order cancellation worker failed: order=%s error=%s", order_id, task.exception())

    async def _run_guard_cancel(
        self, order: ManagedOrder, triggered_at: float, first_attempt: asyncio.Future[bool],
    ) -> bool:
        # One controller per order. Retry failures without requiring another
        # price update; a static dangerous book must not leave a failed cancel idle.
        delay = 0.5
        while not self._stop.is_set() and order.status in {OrderStatus.PENDING, OrderStatus.OPEN}:
            result = await self._cancel_order_safely(order, triggered_at=triggered_at)
            if not first_attempt.done():
                first_attempt.set_result(result)
            if result:
                self._depth_cancel_finished(order)
                return True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return False
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 4.0)
        self._depth_cancel_finished(order)
        return False

    def _depth_cancel_finished(self, order: ManagedOrder) -> None:
        quote = self._depth_cancelling.pop(order.order_id, None)
        if quote is not None and not any(
            selection(other) == selection(quote) for other in self._depth_cancelling.values()
        ):
            self._depth_guard.acknowledged(quote, monotonic())

    def _approached_touch(self, quote: Quote, book: OrderBook) -> Decimal | None:
        tick = book.tick_size or self.config.strategy.tick_size
        canonical = (quote.outcome_side or quote.outcome).strip().upper()
        if quote.side == Side.BUY and canonical == "NO":
            touch = Decimal("1") - book.best_ask.price if book.best_ask else None
        else:
            level = book.best_bid if quote.side == Side.BUY else book.best_ask
            touch = level.price if level else None
        if touch is None or self._minimum_tick_quote_is_pinned(quote, book, tick):
            return None
        approached = (touch <= quote.price + tick if quote.side == Side.BUY
                      else touch >= quote.price - tick)
        return touch if approached else None

    @staticmethod
    def _minimum_tick_buy_is_pinned(
        order: ManagedOrder,
        orderbook: OrderBook,
        tick_size: Decimal,
    ) -> bool:
        """Keep an unmovable floor quote until its one-tick spread widens."""
        return MarketMakerEngine._minimum_tick_quote_is_pinned(order.quote, orderbook, tick_size)

    @staticmethod
    def _minimum_tick_quote_is_pinned(
        quote: Quote, orderbook: OrderBook, tick_size: Decimal,
    ) -> bool:
        if (
            quote.side != Side.BUY
            or quote.price != tick_size
            or orderbook.spread is None
            or orderbook.spread > tick_size
        ):
            return False
        canonical_outcome = (
            quote.outcome_side or quote.outcome
        ).strip().upper()
        if canonical_outcome == "NO":
            touch_price = (
                Decimal("1") - orderbook.best_ask.price
                if orderbook.best_ask is not None
                else None
            )
        else:
            touch_price = orderbook.best_bid.price if orderbook.best_bid is not None else None
        return touch_price == tick_size

    async def _manage_order_lifetimes(
        self,
        market: MarketConfig,
        orderbook: OrderBook,
        target_quotes: list[Quote],
    ) -> None:
        """Refresh quotes after one lifetime, with at most one unchanged extension."""
        lifetime = self.config.cancel_after_seconds
        targets = {
            self._quote_selection_key(quote): quote
            for quote in target_quotes
        }
        for order in list(self.open_orders.values()):
            if (
                order.status not in {OrderStatus.PENDING, OrderStatus.OPEN}
                or order.is_emergency_exit
                or not self._order_matches_market_config(order, market)
            ):
                continue
            tick_size = orderbook.tick_size or self.config.strategy.tick_size
            if self._minimum_tick_buy_is_pinned(order, orderbook, tick_size):
                continue
            if order.age_seconds < lifetime:
                continue

            target_quote = targets.get(self._quote_selection_key(order.quote))
            if target_quote is None:
                logger.info(
                    "Refreshing order %s after %.1f seconds: no safe current target quote",
                    order.order_id,
                    order.age_seconds,
                )
                await self._cancel_order_safely(order)
                continue

            current_reference = self._quote_reference(orderbook, target_quote)
            original_reference = self._order_quote_references.setdefault(
                order.order_id,
                current_reference,
            )
            if order.age_seconds >= lifetime * 2:
                logger.info(
                    "Refreshing order %s after maximum %.1f-second lifetime",
                    order.order_id,
                    lifetime * 2,
                )
                await self._cancel_order_safely(order)
                continue

            if current_reference != original_reference:
                logger.info(
                    "Refreshing order %s after %.1f seconds: orderbook or target price changed",
                    order.order_id,
                    order.age_seconds,
                )
                await self._cancel_order_safely(order)
                continue

            if order.order_id not in self._extended_lifetime_orders:
                self._extended_lifetime_orders.add(order.order_id)
                logger.info(
                    "Keeping order %s for one extra %.1f-second lifetime: "
                    "orderbook and target price are unchanged",
                    order.order_id,
                    lifetime,
                )

    @staticmethod
    def _quote_selection_key(quote: Quote) -> tuple[Side, str]:
        canonical_outcome = (quote.outcome_side or quote.outcome).strip().upper()
        return quote.side, canonical_outcome

    @classmethod
    def _quote_key(cls, quote: Quote) -> tuple[str, Side, str, str, Decimal]:
        return (
            quote.market_id,
            quote.side,
            quote.outcome.strip().casefold(),
            (quote.outcome_side or "").strip().upper(),
            quote.price,
        )

    @staticmethod
    def _quote_reference(orderbook: OrderBook, quote: Quote) -> QuoteReference:
        return QuoteReference(
            best_bid=orderbook.best_bid.price if orderbook.best_bid else None,
            best_ask=orderbook.best_ask.price if orderbook.best_ask else None,
            target_price=quote.price,
        )

    async def _cancel_order_safely(
        self, order: ManagedOrder, *, triggered_at: float | None = None,
    ) -> bool:
        """Keep a temporary cancel API failure from stopping the entire engine."""
        task = self._cancel_tasks.get(order.order_id)
        if task is None or task.done():
            task = asyncio.create_task(self._remove_order(order, triggered_at))
            self._cancel_tasks[order.order_id] = task
            task.add_done_callback(
                lambda done: self._forget_task(self._cancel_tasks, order.order_id, done)
            )
        return await asyncio.shield(task)

    async def _remove_order(self, order: ManagedOrder, triggered_at: float | None) -> bool:
        async with self._cancel_slots:
            while monotonic() < self._cancel_not_before:
                await asyncio.sleep(self._cancel_not_before - monotonic())
            if order.status not in {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN}:
                return True
            return await self._remove_order_request(order, triggered_at)

    async def _remove_order_request(self, order: ManagedOrder, triggered_at: float | None) -> bool:
        started_at = monotonic()
        logger.info(
            "Cancel request started: order=%s market=%s trigger_to_request_ms=%s",
            order.order_id, order.quote.market_id,
            round((started_at - triggered_at) * 1000, 1) if triggered_at is not None else None,
        )
        try:
            await self.client.cancel_order(order.order_id)
        except Exception as error:  # noqa: BLE001
            if isinstance(error, PredictRateLimitError):
                self._cancel_not_before = max(
                    self._cancel_not_before, monotonic() + max(0.5, error.retry_after),
                )
            logger.warning(
                "Cancel failed for order %s; keeping it active and retrying next cycle: %s",
                order.order_id,
                error,
            )
            return False
        if order.status != OrderStatus.FILLED:
            order.status = OrderStatus.CANCELED
        self._order_quote_references.pop(order.order_id, None)
        self._extended_lifetime_orders.discard(order.order_id)
        self._remember_order(order)
        logger.info(
            "Cancel removal acknowledged: order=%s market=%s request_ms=%.1f trigger_to_ack_ms=%s",
            order.order_id, order.quote.market_id, (monotonic() - started_at) * 1000,
            round((monotonic() - triggered_at) * 1000, 1) if triggered_at is not None else None,
        )
        return True

    def _log_prefill_orderbooks(self, order: ManagedOrder) -> None:
        for book in list(self._orderbook_history.get(order.quote.market_id, []))[-8:]:
            logger.info(
                "Pre-fill orderbook: order=%s market=%s source=%s book_ts_ms=%s "
                "received_ts_ms=%s bid=%s bid_size=%s ask=%s ask_size=%s",
                order.order_id, order.quote.market_id, book.source, book.update_timestamp_ms,
                book.received_timestamp_ms,
                book.best_bid.price if book.best_bid else None,
                book.best_bid.size if book.best_bid else None,
                book.best_ask.price if book.best_ask else None,
                book.best_ask.size if book.best_ask else None,
            )

    async def _reconcile_buy_fills(self) -> None:
        """Recover fills missed while the no-snapshot wallet stream was disconnected."""
        if self.config.dry_run or not self.config.emergency_exit_on_buy_fill:
            return
        candidates = [
            order
            for order in self.open_orders.values()
            if not order.is_emergency_exit
            and order.quote.side == Side.BUY
            and order.filled_size < order.quote.size
        ]
        if not candidates:
            return
        try:
            filled_amounts = await self.client.get_order_filled_amounts()
        except Exception as error:  # noqa: BLE001
            logger.warning("Unable to reconcile order fills from REST: %s", error)
            return

        for order in candidates:
            cumulative = max(
                filled_amounts.get(order.order_id, Decimal("0")),
                filled_amounts.get(order.order_hash or "", Decimal("0")),
            )
            cumulative = min(cumulative, order.quote.size)
            delta = cumulative - order.filled_size
            if delta <= Decimal("0"):
                continue
            await self._handle_wallet_fill(
                WalletFillEvent(
                    order_id=order.order_id,
                    order_hash=order.order_hash,
                    filled_size=delta,
                    settlement_id=f"rest:{order.order_id}:{cumulative}",
                    event_type="REST order reconciliation",
                    cumulative_filled_size=cumulative,
                )
            )

    async def _handle_wallet_fill(self, event: WalletFillEvent) -> None:
        known = self.open_orders.get(event.order_id)
        key = (known.order_hash if known else None) or event.order_hash or event.order_id
        async with self._fill_locks.setdefault(key, asyncio.Lock()):
            await self._apply_wallet_fill(event)

    async def _apply_wallet_fill(self, event: WalletFillEvent) -> None:
        order = self.open_orders.get(event.order_id)
        if order is None and event.order_hash:
            order = next(
                (candidate for candidate in self.open_orders.values() if candidate.order_hash == event.order_hash),
                None,
            )
        if order is None:
            order = await self._recover_wallet_fill_order(event)
            if order is None:
                logger.critical(
                    "Received a wallet fill that could not be recovered from memory, "
                    "the safety journal, event details, or the order hash: "
                    "order_id=%s order_hash=%s event=%s. Manual review is required.",
                    event.order_id,
                    event.order_hash,
                    event.event_type,
                )
                return
        if order.quote.side != Side.BUY and not order.is_emergency_exit:
            return
        if order.quote.side == Side.BUY and not self.config.emergency_exit_on_buy_fill:
            return

        fill_size = event.filled_size
        if fill_size <= Decimal("0") and event.event_type != "orderTransactionFailed":
            return

        order_key = order.order_hash or order.order_id
        if order.quote.side == Side.BUY and order.exit_baseline_size is None:
            # Existing journals used filled_size as their exit dedup counter.
            # Do not re-sell those older fills when upgrading to durable plans.
            order.exit_baseline_size = order.filled_size
        self._wallet_fill_totals.setdefault(order_key, order.wallet_filled_size)
        settlement_key = order_key + ":" + (event.settlement_id or (
            f"{event.order_id}:{event.order_hash or ''}:{fill_size}"
        ))
        if order.is_emergency_exit:
            pending = self._pending_sell_settlements.setdefault(order_key, set())
            if event.event_type == "orderTransactionSubmitted":
                pending.add(settlement_key)
                logger.info("Emergency sell matched: order=%s settlement=%s", order.order_id, settlement_key)
                self._wake_exit(order)
                return
            pending.discard(settlement_key)
            if event.event_type == "orderTransactionFailed":
                logger.error("Emergency sell settlement failed: order=%s", order.order_id)
                self._wake_exit(order)
                return
        elif event.event_type == "orderTransactionFailed":
            if settlement_key in order.wallet_settlement_ids:
                return
            order.failed_settlement_ids.add(settlement_key)
            self._remember_order(order)
            logger.warning("Buy settlement failed: order=%s settlement=%s; stop unsent retries, "
                           "continue reconciling any sell already submitted",
                           order.order_id, settlement_key)
            return

        # User-authorized speculative exit: a match starts an immediate POST,
        # without treating that match as a settled buy. Existing holdings may
        # satisfy this sell. Durable plans prevent later WS/REST duplication.
        if event.event_type == "orderTransactionSubmitted":
            if (settlement_key in order.matched_settlements
                    or settlement_key in order.wallet_settlement_ids
                    or settlement_key in order.failed_settlement_ids):
                return
            if not order.matched_settlements:
                self._log_prefill_orderbooks(order)
            order.matched_settlements[settlement_key] = min(fill_size, order.quote.size)
            self._match_received_at.setdefault(order_key, event.received_at)
            logger.critical(
                "Buy order %s matched; attempting emergency sell BEFORE buy settlement success",
                order.order_id,
            )
            self._ensure_emergency_cancel(order)
            self._schedule_uncovered_exit(order, settlement_key)
            return

        # A success event can race with a local cancellation. The order's local
        # CANCELED state therefore must not make us discard the confirmed fill.
        if (settlement_key in self._handled_fill_settlements
                or settlement_key in order.wallet_settlement_ids):
            return
        self._handled_fill_settlements.add(settlement_key)

        if event.cumulative_filled_size is not None:
            cumulative = event.cumulative_filled_size
        else:
            self._wallet_fill_totals[order_key] += fill_size
            cumulative = self._wallet_fill_totals[order_key]
            order.wallet_filled_size = cumulative
            order.wallet_settlement_ids.add(settlement_key)
            if not order.is_emergency_exit:
                order.matched_settlements[settlement_key] = min(fill_size, order.quote.size)
        fill_size = max(Decimal("0"), min(cumulative, order.quote.size) - order.filled_size)
        if fill_size <= Decimal("0"):
            self._remember_order(order)
            return
        order.filled_size += fill_size
        if order.filled_size >= order.quote.size:
            order.status = OrderStatus.FILLED
        self._remember_order(order)
        if order.is_emergency_exit:
            logger.critical(
                "Emergency sell settlement confirmed: order=%s filled=%s/%s event_timestamp_ms=%s",
                order.order_id, order.filled_size, order.quote.size, event.event_timestamp_ms,
            )
            self._wake_exit(order)
            return
        logger.critical(
            "Detected %s for buy order %s; recording settled quantity and checking exit coverage",
            event.event_type,
            order.order_id,
        )
        matched_at = self._match_received_at.get(order_key)
        logger.info(
            "Buy settlement timing: order=%s queue_ms=%.1f match_to_success_ms=%s",
            order.order_id, (monotonic() - event.received_at) * 1000,
            round((event.received_at - matched_at) * 1000, 1) if matched_at is not None else "unknown",
        )
        self._ensure_emergency_cancel(order)
        self._schedule_uncovered_exit(order)

    def _schedule_uncovered_exit(self, order: ManagedOrder, settlement_key: str | None = None) -> None:
        matched = sum((size for key, size in order.matched_settlements.items()
                       if key not in order.failed_settlement_ids), Decimal("0"))
        target = min(order.quote.size, max(order.filled_size, matched))
        covered = (order.exit_baseline_size or Decimal("0")) + sum(
            (plan.target_size for plan in order.exit_plans
             if plan.source_settlement_key not in order.failed_settlement_ids), Decimal("0"),
        )
        size = target - covered
        if size > 0:
            plan = ExitContext(uuid4().hex, order.order_id, size,
                               source_settlement_key=settlement_key)
            order.exit_plans.append(plan)
            # Persist allocation BEFORE scheduling the task / signing / POST.
            self._remember_order(order)
            self._start_exit_plan(order, plan)
        else:
            self._remember_order(order)

    def _start_exit_plan(self, source: ManagedOrder, plan: ExitContext,
                         resume: ManagedOrder | None = None) -> None:
        if plan.group_id in self._active_exit_groups:
            return
        self._active_exit_groups.add(plan.group_id)
        task = asyncio.create_task(self._emergency_exit(
            source, plan.target_size, resume=resume, plan=plan,
        ))
        self._emergency_tasks.add(task)
        def finished(done: asyncio.Task[None]) -> None:
            self._active_exit_groups.discard(plan.group_id)
            self._emergency_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.critical("Emergency exit worker failed; durable plan %s retained: %s",
                                plan.group_id, done.exception())
        task.add_done_callback(finished)

    def _exit_source(self, context: ExitContext) -> ManagedOrder | None:
        return self.open_orders.get(context.source_order_id)

    def _exit_source_failed(self, context: ExitContext) -> bool:
        source = self._exit_source(context)
        return bool(source and context.source_settlement_key in source.failed_settlement_ids)

    def _finish_exit_plan(self, context: ExitContext) -> None:
        source = self._exit_source(context)
        if source is not None:
            source.completed_exit_groups.add(context.group_id)
            self._remember_order(source)

    def _order_from_wallet_context(
        self, event: WalletFillEvent | WalletOrderStatusEvent
    ) -> ManagedOrder | None:
        if not event.market_id or event.side is None or not event.outcome:
            return None
        event_fill_size = (
            event.filled_size if isinstance(event, WalletFillEvent) else Decimal("0")
        )
        size = event.order_size or event_fill_size
        if size <= Decimal("0"):
            return None
        return ManagedOrder(
            order_id=event.order_id,
            order_hash=event.order_hash,
            quote=Quote(
                market_id=event.market_id,
                side=event.side,
                price=event.price or Decimal("0"),
                size=size,
                outcome=event.outcome,
                outcome_side=event.outcome.strip().upper()
                if event.outcome.strip().upper() in {"YES", "NO"}
                else None,
            ),
            created_at=monotonic(),
            status=OrderStatus.PENDING,
        )

    async def _recover_wallet_fill_order(
        self, event: WalletFillEvent
    ) -> ManagedOrder | None:
        # An exit can fill before its POST response. Recover the persisted
        # pre-POST intent so its sell event is not mistaken for an unrelated sell.
        loader = getattr(self.client, "load_tracked_orders", None)
        order = next((candidate for candidate in loader()
                      if event.order_hash and candidate.order_hash == event.order_hash), None) if loader else None
        from_journal = order is not None
        order = order or self._order_from_wallet_context(event)
        if order is None and event.order_hash:
            recover = getattr(self.client, "get_order_by_hash", None)
            if callable(recover):
                try:
                    order = await recover(event.order_hash)
                except Exception as error:  # noqa: BLE001
                    logger.critical(
                        "Unable to recover wallet fill %s by order hash: %s",
                        event.order_id,
                        error,
                    )
        if order is None:
            return None

        order.order_id = event.order_id
        order.order_hash = order.order_hash or event.order_hash
        # The event being handled is the source of truth for this delta. A hash
        # lookup may already report the cumulative fill, which would otherwise
        # make the emergency-exit calculation incorrectly discard this event.
        if not order.is_emergency_exit and not from_journal:
            order.filled_size = Decimal("0")
        registered = self._register_order(order)
        logger.critical(
            "Recovered previously unregistered %s order %s on market %s; "
            "continuing wallet fill handling",
            registered.quote.side.value,
            registered.order_id,
            registered.quote.market_id,
        )
        return registered

    def _ensure_emergency_cancel(self, filled_order: ManagedOrder) -> None:
        market_id = filled_order.quote.market_id
        self._halted_markets.add(market_id)
        if market_id in self._prepared_emergency_markets:
            return
        task = self._emergency_cancel_tasks.get(market_id)
        if task is None or task.done():
            self._emergency_cancel_tasks[market_id] = asyncio.create_task(
                self._prepare_emergency_exit(filled_order)
            )

    async def _prepare_emergency_exit(self, filled_order: ManagedOrder) -> None:
        market_id = filled_order.quote.market_id
        try:
            await self.client.cancel_market_buy_orders(market_id)
        except Exception as error:  # noqa: BLE001
            logger.critical(
                "Could not cancel all market quotes before emergency exit; "
                "the market remains halted and the sell will still be attempted: %s",
                error,
            )
            return
        for order in self.open_orders.values():
            if (order.quote.market_id == market_id and order.quote.side == Side.BUY
                    and order.status in {
                OrderStatus.PENDING,
                OrderStatus.OPEN,
            }):
                order.status = OrderStatus.CANCELED
        self._prepared_emergency_markets.add(market_id)

    def _wake_exit(self, order: ManagedOrder) -> None:
        self._exit_wakeups.setdefault(order.order_hash or order.order_id, asyncio.Event()).set()

    async def _monitor_emergency_sell(self, order: ManagedOrder) -> Decimal:
        """Return sold quantity only when filled or definitively safe to replace.

        A removal/404/timeout is NOT proof the signed order cannot still fill.
        Such orders stay under observation and never cause an additional sell.
        """
        key = order.order_hash or order.order_id
        wake = self._exit_wakeups.setdefault(key, asyncio.Event())
        last_warning = float("-inf")
        while not self._stop.is_set():
            wake.clear()
            if order.filled_size >= order.quote.size:
                order.status = OrderStatus.FILLED
                self._remember_order(order)
                return order.filled_size
            confirmed_terminal = False
            try:
                remote = await self.client.get_order_by_hash(order.order_hash or "")
                if remote is not None:
                    # A hash lookup can reveal the numeric ID after an uncertain POST.
                    remote.is_emergency_exit = True
                    remote.exit_context = order.exit_context
                    order = self._register_order(remote)
                    order.filled_size = max(order.filled_size, remote.filled_size)
                    if order.filled_size >= order.quote.size:
                        order.status = OrderStatus.FILLED
                    elif order.status != OrderStatus.FILLED:
                        order.status = remote.status
                    self._remember_order(order)
                    confirmed_terminal = remote.status in {OrderStatus.REJECTED, OrderStatus.EXPIRED}
                # An explicit orderNotAccepted can be absent from the hash endpoint.
                confirmed_terminal = confirmed_terminal or order.status == OrderStatus.REJECTED
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                if monotonic() - last_warning >= 30:
                    logger.error("Cannot confirm emergency sell %s; NOT submitting a duplicate: %s",
                                 order.order_id, error)
                    last_warning = monotonic()
                confirmed_terminal = order.status == OrderStatus.REJECTED

            if order.filled_size >= order.quote.size:
                logger.critical("Emergency sell fully filled: order=%s shares=%s (REST confirmed)",
                                order.order_id, order.filled_size)
                return order.filled_size
            if confirmed_terminal and not self._pending_sell_settlements.get(key):
                # Do not infer expiration from the local clock: require the
                # server's terminal status, plus no known settlement in flight.
                logger.warning("Emergency sell %s is %s; sold=%s remaining=%s",
                               order.order_id, order.status, order.filled_size,
                               order.quote.size - order.filled_size)
                return order.filled_size
            if monotonic() - last_warning >= 30:
                logger.warning("Emergency sell pending confirmation: order=%s status=%s sold=%s/%s; no duplicate sell",
                               order.order_id, order.status, order.filled_size, order.quote.size)
                last_warning = monotonic()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self._exit_poll_seconds)
        raise asyncio.CancelledError

    def _resume_emergency_exits(self) -> None:
        latest: dict[str, ManagedOrder] = {}
        for order in self.open_orders.values():
            if order.exit_context:
                latest[order.exit_context.group_id] = order
        for order in latest.values():
            context = order.exit_context
            assert context is not None
            if context.sold_before + order.filled_size >= context.target_size:
                self._finish_exit_plan(context)
                continue
            self._halted_markets.add(order.quote.market_id)
            logger.warning("Resuming emergency sell confirmation for order %s", order.order_id)
            self._start_exit_plan(self._exit_source(context) or order, context, resume=order)
        # Recover the crash window between persisting a source plan and the
        # client's durable pre-POST intent. A completed plan is never replayed.
        for source in list(self.open_orders.values()):
            for plan in source.exit_plans:
                if plan.group_id in latest or plan.group_id in source.completed_exit_groups:
                    continue
                self._halted_markets.add(source.quote.market_id)
                self._start_exit_plan(source, plan)
            if source.quote.side == Side.BUY and source.exit_baseline_size is not None:
                # A crash may occur after persisting a confirmed fill but
                # before its exit allocation is written.
                self._schedule_uncovered_exit(source)

    async def _emergency_exit(
        self, filled_order: ManagedOrder, fill_size: Decimal, *, resume: ManagedOrder | None = None,
        plan: ExitContext | None = None,
    ) -> None:
        key = (filled_order.quote.market_id, filled_order.quote.token_id or filled_order.quote.outcome)
        async with self._exit_locks.setdefault(key, asyncio.Lock()):
            await self._run_emergency_exit(filled_order, fill_size, resume=resume, plan=plan)

    async def _run_emergency_exit(
        self, filled_order: ManagedOrder, fill_size: Decimal, *, resume: ManagedOrder | None = None,
        plan: ExitContext | None = None,
    ) -> None:
        started_at = monotonic()
        market_id = filled_order.quote.market_id
        context = resume.exit_context if resume else plan or ExitContext(
            group_id=uuid4().hex, source_order_id=filled_order.order_id, target_size=fill_size,
        )
        assert context is not None
        sold = context.sold_before
        current = resume
        self._ensure_emergency_cancel(filled_order)
        exit_price = await self._emergency_exit_price(market_id)
        prepared = None
        logger.critical(
            "Emergency exit started on %s; canceling market quotes and attempting to sell %s "
            "at emergency limit %s",
            market_id,
            fill_size,
            exit_price,
        )
        attempt = 0
        while not self._stop.is_set() and sold < context.target_size:
            if current is not None:
                sold += await self._monitor_emergency_sell(current)
                current = None
                if sold >= context.target_size:
                    self._finish_exit_plan(context)
                    logger.critical(
                        "Emergency exit complete: market=%s source_order=%s sold=%s monitor_elapsed_ms=%.1f",
                        market_id, context.source_order_id, sold, (monotonic() - started_at) * 1000,
                    )
                    return
                context = replace(context, sold_before=sold)
                prepared = None
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self._emergency_retry_base_seconds)
                if self._stop.is_set():
                    return
            if self._exit_source_failed(context):
                self._finish_exit_plan(context)
                logger.warning("Stop unsent emergency retries: source settlement failed; group=%s",
                               context.group_id)
                return
            attempt += 1
            self._ensure_emergency_cancel(filled_order)
            try:
                # Sign as part of the immediate submission, keeping the same
                # signature for explicit balance/429 rejections only.
                prepare = getattr(self.client, "prepare_order", None)
                submit = getattr(self.client, "submit_prepared_order", None)
                if prepared is None and callable(prepare) and callable(submit) and not self.config.dry_run:
                    prepared = await prepare(
                        replace(filled_order.quote, side=Side.SELL, price=exit_price,
                                size=context.target_size - sold), post_only=False,
                    )
                kwargs = {"exit_context": context}
                if context.source_settlement_key is not None:
                    kwargs["should_submit"] = lambda: (
                        not self._stop.is_set() and not self._exit_source_failed(context)
                    )
                if prepared is not None:
                    exit_order = await self.client.submit_prepared_order(prepared, **kwargs)
                else:
                    exit_order = await self.client.create_order(
                        replace(filled_order.quote, side=Side.SELL, price=exit_price,
                                size=context.target_size - sold),
                        post_only=False, **kwargs,
                    )
            except PredictSubmissionAborted:
                if self._exit_source_failed(context):
                    self._finish_exit_plan(context)
                return
            except PredictOrderSubmissionUnknown as error:
                prepared = None
                current = self._register_order(error.order)
                logger.critical("Emergency POST outcome unknown: order_hash=%s; checking original order, not resubmitting",
                                current.order_hash)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                retry_delay = min(
                    self._emergency_retry_base_seconds * (2 ** min(attempt - 1, 3)),
                    5.0,
                )
                if isinstance(error, PredictInsufficientSharesError):
                    # Short bounded retries for balance-indexing lag, then backoff.
                    retry_delay = 0.5 if attempt <= 4 else retry_delay
                elif isinstance(error, PredictRateLimitError):
                    retry_delay = max(retry_delay, error.retry_after)
                else:
                    prepared = None
                if "insufficient shares" in str(error).casefold():
                    logger.critical(
                        "Emergency sell is waiting for %s shares to become available on %s; "
                        "retrying in %.1f seconds (attempt %s)",
                        fill_size,
                        market_id,
                        retry_delay,
                        attempt,
                    )
                else:
                    logger.critical(
                        "Emergency sell attempt %s failed on %s; retrying in %.1f seconds: %s",
                        attempt,
                        market_id,
                        retry_delay,
                        error,
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=retry_delay)
                except asyncio.TimeoutError:
                    continue
                return

            exit_order.is_emergency_exit = True
            exit_order.exit_context = context
            current = self._register_order(exit_order)
            logger.critical(
                "Emergency %s sell order submitted for market %s: %s; exit_to_submit_ms=%.1f; awaiting fill",
                exit_price,
                market_id,
                exit_order.order_id,
                (monotonic() - started_at) * 1000,
            )

    async def _emergency_exit_price(self, market_id: str) -> Decimal:
        """Use the lowest aggressive limit supported by the current market tick."""
        fallback = Decimal("0.01")
        tick_size = self._market_tick_sizes.get(market_id)
        if tick_size is not None:
            return Decimal("0.001") if tick_size <= Decimal("0.001") else fallback
        try:
            orderbook = await self.client.get_orderbook(market_id)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Unable to read tick size for emergency exit on %s; using %s: %s",
                market_id,
                fallback,
                error,
            )
            return fallback
        tick_size = orderbook.tick_size
        if tick_size is not None:
            self._market_tick_sizes[market_id] = tick_size
        if tick_size is not None and tick_size <= Decimal("0.001"):
            return Decimal("0.001")
        return fallback

    async def _cancel_all_known_markets(self) -> None:
        # Predict's remove endpoint only hides orders from the public book. Use
        # one account-wide removal so stale orders from deleted market configs
        # cannot remain visible. New signatures also expire after 300 seconds.
        await self.client.cancel_all_orders(None)
        for order in self.open_orders.values():
            order.status = OrderStatus.CANCELED
            self._remember_order(order)

    async def _cancel_all_known_markets_safely(self) -> bool:
        """Retry shutdown cancellation without masking the error that stopped the engine."""
        for attempt in range(1, 6):
            try:
                await self._cancel_all_known_markets()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                if attempt == 5:
                    logger.critical(
                        "Unable to cancel all orders during shutdown after %s attempts; "
                        "orders still expire after 300 seconds: %s",
                        attempt,
                        error,
                    )
                    return False
                retry_delay = min(
                    self._shutdown_cancel_retry_base_seconds * (2 ** (attempt - 1)),
                    4.0,
                )
                logger.warning(
                    "Shutdown cancellation attempt %s failed; retrying in %.1f seconds: %s",
                    attempt,
                    retry_delay,
                    error,
                )
                await asyncio.sleep(retry_delay)
        return False

    def _restore_tracked_orders(self) -> None:
        loader = getattr(self.client, "load_tracked_orders", None)
        if loader is None:
            return
        restored = loader()
        for order in restored:
            self.open_orders[order.order_id] = order
        if restored:
            logger.warning(
                "Restored %s bot-created orders from the local safety journal",
                len(restored),
            )

    def _remember_order(self, order: ManagedOrder) -> None:
        persist = getattr(self.client, "persist_tracked_order", None)
        if persist is not None:
            persist(order)
