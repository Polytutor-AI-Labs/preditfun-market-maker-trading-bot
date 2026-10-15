from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field as dataclass_field, is_dataclass, replace
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic, time
from uuid import uuid4

import requests

from predict_mm.config import Settings
from predict_mm.models import (
    ExitContext,
    Level,
    ManagedOrder,
    OrderBook,
    OrderStatus,
    Quote,
    Side,
    WalletFillEvent,
    WalletOrderStatusEvent,
)

logger = logging.getLogger("predict-mm")


class PredictAuthorizationError(RuntimeError):
    """A protected Predict REST request was rejected with HTTP 401."""


class PredictTransientError(RuntimeError):
    """Predict temporarily rejected a request that is safe to retry."""


class PredictInsufficientSharesError(RuntimeError):
    """An explicit HTTP 400 rejected this sell before acceptance."""


class PredictSubmissionAborted(RuntimeError):
    """The source match failed or shutdown began before the POST was sent."""


class PredictRateLimitError(RuntimeError):
    """An explicit HTTP 429; respect the server's cooldown."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class PreparedOrder:
    """Local-only signature; never persisted or submitted during preparation."""

    quote: Quote
    post_only: bool
    payload: dict = dataclass_field(repr=False)
    used: bool = False


class PredictOrderSubmissionUnknown(RuntimeError):
    """The POST may have succeeded. Reconcile this hash before creating another order."""

    def __init__(self, order: ManagedOrder) -> None:
        self.order = order
        super().__init__(f"Emergency order submission uncertain; reconcile hash {order.order_hash}")


class PredictClient:
    """Predict.fun REST + SDK adapter.

    REST endpoints follow the current Predict API beta docs. Real order creation uses the official
    ``predict-sdk`` package to build and sign orders before POSTing them to the order API.
    """

    def __init__(
        self,
        settings: Settings,
        dry_run: bool,
        jwt_token_updated: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.base_url = settings.api_base_url.rstrip("/")
        self._jwt_token_updated = jwt_token_updated
        self._jwt_refresh_lock = asyncio.Lock()
        self._dry_orders: dict[str, ManagedOrder] = {}
        self._market_metadata: dict[str, dict] = {}
        self._order_journal_path = Path(settings.order_journal_path)
        self._request_times: deque[float] = deque()
        self._next_early_probe_at = 0.0
        self._rate_limit_until = 0.0
        self._emergency_submit_lock = asyncio.Lock()
        self._next_emergency_submit_at = 0.0
        self.wallet_stream_connected = False
        self.orderbook_stream_connected = False

    async def close(self) -> None:
        return None

    def allow_early_fill_probe(self) -> bool:
        """Budget ONLY the additional fast-path reads, not existing risk controls.

        At most one extra probe/second across all markets; leave 60 of the
        documented default 240 requests/minute for normal/critical traffic.
        An exhausted budget falls back to the existing WS/REST reconciliation.
        """
        now = monotonic()
        while self._request_times and self._request_times[0] <= now - 60:
            self._request_times.popleft()
        if (now < max(self._next_early_probe_at, self._rate_limit_until)
                or len(self._request_times) >= 180):
            return False
        self._next_early_probe_at = now + 1.0
        return True

    async def _pace_emergency_submission(self) -> None:
        # Sequential starts, including across simultaneous fills. Never queue a
        # second POST merely because the first one's response has not arrived.
        async with self._emergency_submit_lock:
            delay = max(self._next_emergency_submit_at, self._rate_limit_until) - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_emergency_submit_at = monotonic() + 0.5

    async def get_usdt_balance(self) -> tuple[Decimal, str]:
        """Read the configured trading account's on-chain USDT balance.

        The official SDK needs the local signer to initialise its BNB-chain contract
        client.  This only performs a contract ``balanceOf`` call: it does not create,
        sign, or submit an order or transaction.
        """
        if not self.settings.private_key:
            raise RuntimeError("请先在账户设置中保存钱包 Private Key，才能读取余额。")
        try:
            from eth_account import Account
            from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("读取余额需要安装 Predict.fun 官方 SDK。") from error

        account_address = self.settings.predict_account_address or Account.from_key(
            self.settings.private_key
        ).address
        options = (
            OrderBuilderOptions(predict_account=self.settings.predict_account_address)
            if self.settings.predict_account_address
            else None
        )
        try:
            builder = await asyncio.to_thread(
                OrderBuilder.make,
                ChainId(self.settings.chain_id),
                self.settings.private_key,
                options,
            )
            balance_wei = await builder.balance_of_async("USDT", account_address)
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("无法读取链上 USDT 余额，请检查钱包配置和服务器网络。") from error
        return Decimal(str(balance_wei)) / Decimal(10**18), account_address

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.api_key:
            headers["x-api-key"] = self.settings.api_key
        if self.settings.jwt_token:
            headers["Authorization"] = f"Bearer {self.settings.jwt_token}"
        return headers

    async def get_orderbook(self, market_id: str) -> OrderBook:
        if self.dry_run and self.settings.api_base_url == "https://api.predict.fun":
            return self._sample_orderbook(market_id)

        market = await self._get_market_metadata(market_id)
        tick_size = self._tick_size_from_market(market, market_id)
        response = await self._request("GET", f"/v1/markets/{market_id}/orderbook")
        return self._parse_orderbook(market_id, self._data(response), tick_size=tick_size)

    async def search_markets(self, query: str, *, limit: int = 10) -> list[dict]:
        """Search public markets and markets nested under matching categories."""
        self._require_api_key()
        response = await self._request(
            "GET",
            "/v1/search",
            query={"query": query, "limit": max(1, min(limit, 20))},
        )
        data = self._data(response)
        results: list[dict] = []
        seen_ids: set[str] = set()

        def add_market(market: object, category: dict | None = None) -> None:
            if not isinstance(market, dict):
                return
            market_id = str(market.get("id") or "")
            if not market_id or market_id in seen_ids:
                return
            item = dict(market)
            if category is not None:
                item["categoryTitle"] = category.get("title") or ""
                item["categorySlug"] = category.get("slug") or ""
            results.append(item)
            seen_ids.add(market_id)

        for market in self._market_items(data.get("markets")):
            add_market(market)
        for category in data.get("categories") or []:
            if not isinstance(category, dict):
                continue
            for market in self._market_items(category.get("markets")):
                add_market(market, category)
        return results

    async def get_category_markets(self, slug: str, *, locale: str = "") -> list[dict]:
        """Get every market in a category and apply official localized titles."""
        self._require_api_key()
        encoded_slug = urllib.parse.quote(slug.strip(), safe="")
        response = await self._request("GET", f"/v1/categories/{encoded_slug}")
        category = self._data(response)
        title = str(category.get("title") or "")
        category_slug = str(category.get("slug") or slug)
        markets = [
            {
                **market,
                "categoryTitle": title,
                "categorySlug": category_slug,
            }
            for market in self._market_items(category.get("markets"))
        ]
        normalized_locale = locale.strip().replace("_", "-").lower()
        if not normalized_locale:
            return markets

        try:
            translations_response = await self._request(
                "GET",
                f"/v1/categories/{encoded_slug}/translations",
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Predict.fun 市场翻译接口不可用，保留英文名称: %s", error)
            return markets

        translations_data = self._data(translations_response)
        translations = translations_data.get("translations")
        if not isinstance(translations, dict):
            return markets
        localized = next(
            (
                value
                for language, value in translations.items()
                if str(language).replace("_", "-").lower() == normalized_locale
                and isinstance(value, dict)
            ),
            None,
        )
        if not localized:
            return markets

        translated_category = localized.get("category")
        if isinstance(translated_category, dict):
            translated_title = str(translated_category.get("title") or "").strip()
            if translated_title:
                title = translated_title

        translated_markets = {
            str(item.get("id")): item
            for item in localized.get("markets") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        for market in markets:
            market["categoryTitle"] = title
            translation = translated_markets.get(str(market.get("id")))
            if not translation:
                continue
            for field in ("title", "question"):
                translated_value = str(translation.get(field) or "").strip()
                if translated_value:
                    market[field] = translated_value
            market["translationLocale"] = normalized_locale
        return markets

    async def markets_from_public_page(self, market_url: str, slug: str) -> list[dict]:
        """Read the rendered public category page when API search is unavailable."""
        return await asyncio.to_thread(self._markets_from_public_page_sync, market_url, slug)

    async def create_eoa_jwt(self, private_key: str) -> str:
        """Create a wallet JWT by signing Predict's current auth message locally."""
        self._require_api_key()
        auth_message_response = await self._request("GET", "/v1/auth/message")
        message = str(self._data(auth_message_response).get("message") or "")
        if not message:
            raise RuntimeError("Predict.fun did not return an authentication message.")
        try:
            signer, signature = await asyncio.to_thread(self._sign_eoa_auth_message, private_key, message)
        except (TypeError, ValueError) as error:
            raise RuntimeError("钱包 Private Key 无效，无法生成 JWT。") from error
        response = await self._request(
            "POST",
            "/v1/auth",
            {"signer": signer, "signature": signature, "message": message},
        )
        token = self._data(response).get("token")
        if not token:
            raise RuntimeError("Predict.fun 未返回 JWT Token。")
        return str(token)

    async def create_predict_account_jwt(
        self, private_key: str, predict_account_address: str
    ) -> str:
        """Create a JWT for a Predict Account (Kernel smart wallet)."""
        self._require_api_key()
        auth_message_response = await self._request("GET", "/v1/auth/message")
        message = str(self._data(auth_message_response).get("message") or "")
        if not message:
            raise RuntimeError("Predict.fun 未返回用于签名的认证消息。")
        try:
            signature = await asyncio.to_thread(
                self._sign_predict_account_auth_message,
                private_key,
                predict_account_address,
                self.settings.chain_id,
                message,
            )
        except ImportError as error:
            raise RuntimeError("生成 Predict Account JWT 需要安装 Predict.fun 官方 SDK。") from error
        except (TypeError, ValueError) as error:
            raise RuntimeError("Privy Wallet Private Key 或 Predict Account Address 无效。") from error
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(
                "无法使用该 Privy Wallet Private Key 验证 Predict Account Address。"
            ) from error
        response = await self._request(
            "POST",
            "/v1/auth",
            {
                "signer": predict_account_address,
                "signature": signature,
                "message": message,
            },
        )
        token = self._data(response).get("token")
        if not token:
            raise RuntimeError("Predict.fun 未返回 JWT Token。")
        return str(token)

    async def get_positions(self) -> dict[str, Decimal]:
        if self.dry_run:
            return {}

        self._require_api_key()
        self._require_jwt()
        data = await self._request("GET", "/v1/positions", query={"first": 100})
        positions: dict[str, Decimal] = {}
        for row in self._rows(data):
            market = row.get("market") if isinstance(row, dict) else {}
            market_id = str(
                row.get("marketId")
                or row.get("market_id")
                or (market or {}).get("id")
                or ""
            )
            if not market_id:
                continue
            # The positions endpoint returns share quantities in 18-decimal wei.
            # Convert before applying risk limits; otherwise 100 shares is treated
            # as 100000000000000000000 shares.
            amount_wei = Decimal(
                str(row.get("amount") or row.get("size") or row.get("quantity") or "0")
            )
            amount = amount_wei / Decimal(10**18)
            outcome = self._outcome_name(row.get("outcome"))
            signed_amount = -amount if outcome.upper() == "NO" else amount
            positions[market_id] = positions.get(market_id, Decimal("0")) + signed_amount
        return positions

    async def get_order_filled_amounts(self) -> dict[str, Decimal]:
        """Return cumulative filled share amounts keyed by both order ID and hash.

        Wallet WebSocket topics intentionally have no snapshot. Polling the authenticated
        orders endpoint gives the engine a recovery path if a fill happens while that stream
        is reconnecting.
        """
        if self.dry_run:
            return {}

        self._require_api_key()
        self._require_jwt()
        filled_amounts: dict[str, Decimal] = {}
        for row in await self._all_order_rows():
            raw_amount = row.get("amountFilled") or row.get("amount_filled") or "0"
            amount = Decimal(str(raw_amount)) / Decimal(10**18)
            order = row.get("order") if isinstance(row.get("order"), dict) else {}
            keys = {
                str(row.get("id") or row.get("orderId") or row.get("order_id") or ""),
                str(
                    row.get("orderHash")
                    or row.get("order_hash")
                    or order.get("hash")
                    or ""
                ),
            }
            for key in keys - {""}:
                filled_amounts[key] = amount
        return filled_amounts

    async def get_order_by_hash(self, order_hash: str) -> ManagedOrder | None:
        """Recover an authenticated order that arrived before its create response."""
        if self.dry_run or not order_hash:
            return None

        self._require_api_key()
        self._require_jwt()
        response = await self._request("GET", f"/v1/orders/{order_hash}")
        data = self._data(response)
        order_data = data.get("order") if isinstance(data.get("order"), dict) else {}
        market_id = str(data.get("marketId") or data.get("market_id") or "")
        order_id = str(data.get("id") or data.get("orderId") or data.get("order_id") or "")
        token_id = str(order_data.get("tokenId") or order_data.get("token_id") or "")
        if not market_id or not order_id:
            return None

        side = self._order_side(order_data.get("side"))
        if side is None:
            return None
        amount = self._from_wei(data.get("amount"))
        filled_size = self._from_wei(
            data.get("amountFilled") or data.get("amount_filled")
        )
        maker_amount = self._from_wei(
            order_data.get("makerAmount") or order_data.get("maker_amount")
        )
        taker_amount = self._from_wei(
            order_data.get("takerAmount") or order_data.get("taker_amount")
        )
        price = self._order_price(side, maker_amount, taker_amount)
        market = await self._get_market_metadata(market_id)
        outcome = self._find_outcome_name(market, token_id)
        if not outcome:
            logger.critical(
                "Unable to map token %s while recovering order %s on market %s",
                token_id,
                order_id,
                market_id,
            )
            return None
        raw_status = str(data.get("status") or "UNKNOWN").strip().lower()
        raw_status = {"cancelled": "canceled"}.get(raw_status, raw_status)
        try:
            status = OrderStatus(raw_status)
        except ValueError:
            status = OrderStatus.UNKNOWN

        return ManagedOrder(
            order_id=order_id,
            order_hash=str(order_data.get("hash") or order_hash),
            quote=Quote(
                market_id=market_id,
                side=side,
                price=price,
                size=amount or filled_size,
                outcome=outcome,
                token_id=token_id or None,
                fee_rate_bps=self._optional_int(
                    self._first_present(data, "feeRateBps", "fee_rate_bps")
                ),
                is_neg_risk=self._optional_bool(
                    self._first_present(data, "isNegRisk", "is_neg_risk")
                ),
                is_yield_bearing=self._optional_bool(
                    self._first_present(data, "isYieldBearing", "is_yield_bearing")
                ),
            ),
            created_at=monotonic(),
            status=status,
            filled_size=filled_size,
            expires_at=float(order_data["expiration"]) if order_data.get("expiration") else None,
        )

    async def create_order(
        self, quote: Quote, *, post_only: bool = True,
        exit_context: ExitContext | None = None,
        should_submit: Callable[[], bool] | None = None,
    ) -> ManagedOrder:
        if should_submit is not None and not should_submit():
            raise PredictSubmissionAborted("Emergency submission no longer needed")
        if self.dry_run:
            order = ManagedOrder(order_id=f"dry-{uuid4().hex[:12]}", quote=quote, created_at=monotonic())
            self._dry_orders[order.order_id] = order
            display_side, display_outcome = self._display_order_intent(quote, post_only=post_only)
            logger.info(
                "DRY-RUN create%s %s %s %s @ %s on %s",
                " emergency" if not post_only else "",
                display_side,
                quote.size,
                display_outcome,
                quote.price,
                quote.market_id,
            )
            return order

        prepared = await self.prepare_order(quote, post_only=post_only)
        return await self.submit_prepared_order(
            prepared, exit_context=exit_context, should_submit=should_submit,
        )

    async def prepare_order(self, quote: Quote, *, post_only: bool = False) -> PreparedOrder:
        """Resolve metadata and sign without sending an order or changing holdings."""
        self._require_api_key()
        self._require_jwt()
        quote = await self._complete_quote_with_market_metadata(quote)
        payload = await asyncio.to_thread(self._build_signed_limit_order_payload, quote, post_only)
        return PreparedOrder(quote, post_only, payload)

    async def submit_prepared_order(
        self, prepared: PreparedOrder, *, exit_context: ExitContext | None = None,
        should_submit: Callable[[], bool] | None = None,
    ) -> ManagedOrder:
        if prepared.used:
            raise RuntimeError("Prepared order already submitted; reconcile its hash, do not resubmit")
        quote, post_only = prepared.quote, prepared.post_only
        if not post_only:
            await self._pace_emergency_submission()
        # The await above can race with another caller using the same object.
        if prepared.used:
            raise RuntimeError("Prepared order already submitted; reconcile its hash, do not resubmit")
        prepared.used = True
        signed_order_payload = prepared.payload
        if float(signed_order_payload["data"]["order"]["expiration"]) - time() < 60:
            # This signature has never been sent. It is safe to refresh it here.
            signed_order_payload = (await self.prepare_order(quote, post_only=post_only)).payload
        signed_order = signed_order_payload["data"]["order"]
        # Recheck after every preparation/pacing await. Do not cancel a POST
        # once sent: an uncertain response must still be reconciled by hash.
        if should_submit is not None and not should_submit():
            raise PredictSubmissionAborted("Source match failed before emergency POST")
        order_hash = str(signed_order.get("hash") or "")
        intent = ManagedOrder(
            order_id=order_hash, order_hash=order_hash or None, quote=quote,
            created_at=monotonic(), status=OrderStatus.UNKNOWN,
            is_emergency_exit=not post_only, exit_context=exit_context,
            expires_at=float(signed_order["expiration"]),
        )
        if not post_only:
            if not order_hash:
                raise RuntimeError("Emergency order must have a hash before submission")
            # Persist BEFORE the POST: a timeout or restart must not lose the
            # signed order identity and accidentally produce a second sell.
            self.persist_tracked_order(intent)
        try:
            response = await self._request("POST", "/v1/orders", signed_order_payload)
        except (PredictInsufficientSharesError, PredictRateLimitError):
            if not post_only:
                intent.status = OrderStatus.REJECTED
                self.persist_tracked_order(intent)
            # Only an explicit rejection permits reuse. Never do this on a
            # timeout/5xx/malformed success response.
            prepared.payload = signed_order_payload
            prepared.used = False
            raise
        except (requests.RequestException, PredictTransientError) as error:
            if not post_only:
                raise PredictOrderSubmissionUnknown(intent) from error
            raise
        except RuntimeError:
            if not post_only:
                intent.status = OrderStatus.REJECTED
                self.persist_tracked_order(intent)
            raise
        data = self._data(response)
        order_id = str(
            data.get("orderId")
            or data.get("order_id")
            or data.get("id")
            or data.get("orderHash")
            or data.get("order_hash")
        )
        if not post_only and order_id == "None":
            raise PredictOrderSubmissionUnknown(intent)
        managed_order = ManagedOrder(
            order_id=order_id,
            quote=quote,
            created_at=monotonic(),
            # POST /v1/orders only confirms submission. Predict's wallet stream
            # or GET /v1/orders must confirm that matching accepted it.
            status=OrderStatus.PENDING,
            order_hash=str(
                data.get("orderHash")
                or data.get("order_hash")
                or signed_order.get("hash")
                or ""
            )
            or None,
            is_emergency_exit=not post_only,
            exit_context=exit_context,
            expires_at=intent.expires_at,
        )
        self.persist_tracked_order(managed_order)
        display_side, display_outcome = self._display_order_intent(
            quote, post_only=post_only
        )
        logger.info(
            "Submit%s %s %s %s @ %s on %s (order %s; awaiting Predict.fun OPEN confirmation)",
            " emergency" if not post_only else "",
            display_side,
            quote.size,
            display_outcome,
            quote.price,
            quote.market_id,
            managed_order.order_id,
        )
        return managed_order

    async def stream_wallet_fill_events(self):
        """Yield confirmed wallet fills from Predict's account WebSocket."""
        if self.dry_run:
            return

        self._require_api_key()
        self._require_jwt()
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:
            raise RuntimeError("WebSocket support requires the websockets package.") from error

        subscribed_jwt = self.settings.jwt_token
        self.wallet_stream_connected = False
        try:
            async with connect(
                "wss://ws.predict.fun/ws",
                additional_headers={"x-api-key": self.settings.api_key},
            ) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "method": "subscribe",
                            "requestId": 1,
                            "params": [f"predictWalletEvents/{subscribed_jwt}"],
                        }
                    )
                )
                async for raw_message in websocket:
                    message = json.loads(raw_message)
                    if self.settings.jwt_token != subscribed_jwt:
                        logger.info("JWT 已更新，正在重新连接钱包事件流。")
                        return
                    error = message.get("error") if isinstance(message, dict) else None
                    if (
                        isinstance(error, dict)
                        and error.get("code") == "invalid_credentials"
                    ):
                        logger.warning("钱包事件流拒绝了旧 JWT，正在自动重新生成。")
                        await self._refresh_jwt(subscribed_jwt)
                        return
                    if isinstance(error, dict):
                        raise RuntimeError(
                            "Predict.fun 钱包事件订阅失败："
                            + str(error.get("message") or error.get("code") or "unknown error")
                        )

                    # Official WebSocket pushes use an envelope:
                    # {"type":"M", "topic":"heartbeat|predictWalletEvents/...", "data": ...}
                    if message.get("type") == "R":
                        if message.get("requestId") == 1 and message.get("success") is True:
                            self.wallet_stream_connected = True
                            logger.info("Predict.fun 钱包成交监听已连接。")
                        continue
                    if message.get("type") == "M" and message.get("topic") == "heartbeat":
                        await websocket.send(
                            json.dumps({"method": "heartbeat", "data": message.get("data")})
                        )
                        continue

                    payload = self._wallet_event_payload(message)
                    event = self._wallet_event(payload) if payload is not None else None
                    if event is not None:
                        yield event
        finally:
            self.wallet_stream_connected = False

    async def stream_orderbook_updates(
        self,
        market_ids: Callable[[], set[str]],
    ):
        """Yield live orderbooks while keeping subscriptions aligned with open orders."""
        if self.dry_run:
            return

        self._require_api_key()
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:
            raise RuntimeError("WebSocket support requires the websockets package.") from error

        self.orderbook_stream_connected = False
        subscribed: set[str] = set()
        request_id = 0
        try:
            async with connect(
                "wss://ws.predict.fun/ws",
                additional_headers={"x-api-key": self.settings.api_key},
            ) as websocket:
                self.orderbook_stream_connected = True
                logger.info("Predict.fun 挂单盘口监听已连接。")
                while True:
                    desired = {str(market_id) for market_id in market_ids() if market_id}
                    for market_id in sorted(desired - subscribed):
                        request_id += 1
                        await websocket.send(
                            json.dumps(
                                {
                                    "method": "subscribe",
                                    "requestId": request_id,
                                    "params": [f"predictOrderbook/{market_id}"],
                                }
                            )
                        )
                        subscribed.add(market_id)
                    for market_id in sorted(subscribed - desired):
                        request_id += 1
                        await websocket.send(
                            json.dumps(
                                {
                                    "method": "unsubscribe",
                                    "requestId": request_id,
                                    "params": [f"predictOrderbook/{market_id}"],
                                }
                            )
                        )
                        subscribed.remove(market_id)

                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    received_at = monotonic()
                    received_timestamp_ms = int(time() * 1000)
                    message = json.loads(raw_message)
                    error = message.get("error") if isinstance(message, dict) else None
                    if isinstance(error, dict):
                        raise RuntimeError(
                            "Predict.fun 盘口订阅失败："
                            + str(error.get("message") or error.get("code") or "unknown error")
                        )
                    if message.get("type") == "R":
                        if message.get("success") is False:
                            raise RuntimeError("Predict.fun 盘口订阅请求被拒绝。")
                        continue
                    if message.get("type") != "M":
                        continue
                    if message.get("topic") == "heartbeat":
                        await websocket.send(
                            json.dumps({"method": "heartbeat", "data": message.get("data")})
                        )
                        continue

                    topic = str(message.get("topic") or "")
                    if not topic.startswith("predictOrderbook/"):
                        continue
                    market_id = topic.removeprefix("predictOrderbook/")
                    payload = message.get("data")
                    if not isinstance(payload, dict):
                        continue
                    market = self._market_metadata.get(market_id) or {}
                    tick_size = (
                        self._tick_size_from_market(market, market_id)
                        if market
                        else None
                    )
                    yield replace(
                        self._parse_orderbook(
                            market_id, payload, tick_size=tick_size, source="websocket",
                        ),
                        received_at=received_at,
                        received_timestamp_ms=received_timestamp_ms,
                    )
        finally:
            self.orderbook_stream_connected = False

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            if order_id in self._dry_orders:
                self._dry_orders[order_id].status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel order %s", order_id)
            return

        self._require_api_key()
        self._require_jwt()
        await self._request("POST", "/v1/orders/remove", {"data": {"ids": [order_id]}})

    async def cancel_all_orders(self, market_id: str | None = None) -> None:
        if self.dry_run:
            for order in self._dry_orders.values():
                if market_id is None or order.quote.market_id == market_id:
                    order.status = OrderStatus.CANCELED
            logger.info("DRY-RUN cancel all open orders market=%s", market_id or "*")
            return

        self._require_api_key()
        self._require_jwt()
        open_order_ids = await self._get_open_order_ids(market_id)
        if not open_order_ids:
            return
        # Predict.fun validates ``ids`` with maxItems(100). The orders reader is
        # deliberately paginated, so an account with more than 100 open orders
        # must remove them in API-sized batches instead of sending one oversized
        # payload that stops the engine during startup or shutdown cleanup.
        for start in range(0, len(open_order_ids), 100):
            batch = open_order_ids[start : start + 100]
            await self._request("POST", "/v1/orders/remove", {"data": {"ids": batch}})

    async def cancel_market_buy_orders(self, market_id: str) -> None:
        """Emergency cleanup must never cancel an exit sell submitted concurrently."""
        if self.dry_run:
            for order in self._dry_orders.values():
                if order.quote.market_id == market_id and order.quote.side == Side.BUY:
                    order.status = OrderStatus.CANCELED
            return
        rows = await self._all_order_rows({"status": "OPEN"})
        ids = [str(row["id"]) for row in rows
               if str(row.get("marketId")) == market_id
               and self._order_side((row.get("order") or {}).get("side")) == Side.BUY]
        for start in range(0, len(ids), 100):
            await self._request("POST", "/v1/orders/remove", {"data": {"ids": ids[start:start + 100]}})

    async def _get_open_order_ids(self, market_id: str | None = None) -> list[str]:
        query: dict[str, object] = {"first": 100, "status": "OPEN"}
        if market_id is not None:
            query["marketId"] = market_id
        ids: list[str] = []
        for row in await self._all_order_rows(query):
            order_id = row.get("id") or row.get("orderId") or row.get("order_id")
            if order_id:
                ids.append(str(order_id))
        return ids

    async def get_open_order_ids(self, market_id: str | None = None) -> set[str]:
        """Return order IDs that Predict currently reports as OPEN."""
        return set(await self._get_open_order_ids(market_id))

    async def _all_order_rows(
        self, query: dict[str, object] | None = None
    ) -> list[dict]:
        """Read every orders page so fills and stale orders cannot hide after row 100."""
        page_query = {"first": 100, **(query or {})}
        rows: list[dict] = []
        seen_cursors: set[str] = set()
        while True:
            response = await self._request("GET", "/v1/orders", query=page_query)
            rows.extend(self._rows(response))
            cursor = self._response_cursor(response)
            if not cursor or cursor in seen_cursors:
                return rows
            seen_cursors.add(cursor)
            page_query["after"] = cursor

    @staticmethod
    def _response_cursor(response: dict) -> str | None:
        containers = [response]
        data = response.get("data")
        if isinstance(data, dict):
            containers.append(data)
        for container in containers:
            cursor = container.get("cursor")
            if cursor not in (None, ""):
                return str(cursor)
        return None

    @staticmethod
    def _read_exit_context(context: dict) -> ExitContext:
        return ExitContext(
            group_id=context["group_id"], source_order_id=context["source_order_id"],
            target_size=Decimal(context["target_size"]),
            sold_before=Decimal(context.get("sold_before", "0")),
            source_settlement_key=context.get("source_settlement_key"),
        )

    @staticmethod
    def _write_exit_context(context: ExitContext) -> dict:
        return {
            "group_id": context.group_id, "source_order_id": context.source_order_id,
            "target_size": str(context.target_size), "sold_before": str(context.sold_before),
            "source_settlement_key": context.source_settlement_key,
        }

    def load_tracked_orders(self) -> list[ManagedOrder]:
        """Restore bot-created orders, including orders removed from the public book."""
        if self.dry_run or not self._order_journal_path.exists():
            return []
        try:
            raw = json.loads(self._order_journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.error("Unable to read the local order safety journal: %s", error)
            return []
        records = raw.get("orders", []) if isinstance(raw, dict) else []
        restored: list[ManagedOrder] = []
        for record in records:
            try:
                quote_data = record["quote"]
                context = record.get("exit_context")
                restored.append(
                    ManagedOrder(
                        order_id=str(record["order_id"]),
                        order_hash=record.get("order_hash") or None,
                        quote=Quote(
                            market_id=str(quote_data["market_id"]),
                            side=Side(str(quote_data["side"])),
                            price=Decimal(str(quote_data["price"])),
                            size=Decimal(str(quote_data["size"])),
                            outcome=str(quote_data.get("outcome") or "YES"),
                            token_id=quote_data.get("token_id") or None,
                            fee_rate_bps=quote_data.get("fee_rate_bps"),
                            is_neg_risk=quote_data.get("is_neg_risk"),
                            is_yield_bearing=quote_data.get("is_yield_bearing"),
                            outcome_side=quote_data.get("outcome_side") or None,
                        ),
                        created_at=monotonic(),
                        status=OrderStatus(str(record.get("status") or "unknown")),
                        filled_size=Decimal(str(record.get("filled_size") or "0")),
                        wallet_filled_size=Decimal(str(record.get("wallet_filled_size") or "0")),
                        wallet_settlement_ids=set(record.get("wallet_settlement_ids") or []),
                        matched_settlements={
                            key: Decimal(str(size)) for key, size in
                            (record.get("matched_settlements") or {}).items()
                        },
                        failed_settlement_ids=set(record.get("failed_settlement_ids") or []),
                        exit_plans=[self._read_exit_context(plan)
                                    for plan in record.get("exit_plans", [])],
                        completed_exit_groups=set(record.get("completed_exit_groups") or []),
                        exit_baseline_size=Decimal(str(record["exit_baseline_size"]))
                        if record.get("exit_baseline_size") is not None else None,
                        is_emergency_exit=bool(record.get("is_emergency_exit", False)),
                        expires_at=record.get("expires_at"),
                        exit_context=self._read_exit_context(context) if context else None,
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("Ignoring a malformed order journal record: %s", error)
        return restored

    def persist_tracked_order(self, order: ManagedOrder) -> None:
        """Atomically retain enough metadata to react to a late fill after restart."""
        if self.dry_run:
            return
        existing: dict[str, dict] = {}
        if self._order_journal_path.exists():
            try:
                raw = json.loads(self._order_journal_path.read_text(encoding="utf-8"))
                for record in raw.get("orders", []):
                    if isinstance(record, dict) and record.get("order_id"):
                        existing[str(record["order_id"])] = record
            except (OSError, ValueError):
                existing = {}
        # Replace the pre-POST hash-keyed intent when the numeric ID is known.
        if order.order_hash and order.order_hash != order.order_id:
            existing.pop(order.order_hash, None)
        existing[order.order_id] = {
            "order_id": order.order_id,
            "order_hash": order.order_hash,
            "status": order.status.value,
            "filled_size": str(order.filled_size),
            "wallet_filled_size": str(order.wallet_filled_size),
            "wallet_settlement_ids": sorted(order.wallet_settlement_ids),
            "matched_settlements": {key: str(size) for key, size in order.matched_settlements.items()},
            "failed_settlement_ids": sorted(order.failed_settlement_ids),
            "exit_plans": [self._write_exit_context(plan) for plan in order.exit_plans],
            "completed_exit_groups": sorted(order.completed_exit_groups),
            "exit_baseline_size": str(order.exit_baseline_size)
            if order.exit_baseline_size is not None else None,
            "is_emergency_exit": order.is_emergency_exit,
            "expires_at": order.expires_at,
            "exit_context": self._write_exit_context(order.exit_context) if order.exit_context else None,
            "quote": {
                "market_id": order.quote.market_id,
                "side": order.quote.side.value,
                "price": str(order.quote.price),
                "size": str(order.quote.size),
                "outcome": order.quote.outcome,
                "token_id": order.quote.token_id,
                "fee_rate_bps": order.quote.fee_rate_bps,
                "is_neg_risk": order.quote.is_neg_risk,
                "is_yield_bearing": order.quote.is_yield_bearing,
                "outcome_side": order.quote.outcome_side,
            },
        }
        # Retain the latest attempt for each exit even during large quote batches.
        exits: dict[str, dict] = {}
        for record in existing.values():
            context = record.get("exit_context")
            if context:
                exits[context["group_id"]] = record
        # Drop superseded attempts before pruning. Otherwise an old expired leg
        # could reappear as the latest exit after its completed replacement ages out.
        records = [record for record in existing.values()
                   if not record.get("exit_context")
                   or exits[record["exit_context"]["group_id"]] is record][-500:]
        retained = {record["order_id"]: record for record in records}
        for record in exits.values():
            if record.get("status") != OrderStatus.FILLED.value:
                retained[record["order_id"]] = record
        # A durable source plan may exist before its first POST. Keep it and
        # its latest sell together until completion is recorded on the source.
        for record in existing.values():
            pending = {plan["group_id"] for plan in record.get("exit_plans", [])}
            pending.difference_update(record.get("completed_exit_groups", []))
            if pending:
                retained[record["order_id"]] = record
                for group in pending:
                    if group in exits:
                        retained[exits[group]["order_id"]] = exits[group]
        records = list(retained.values())
        self._order_journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._order_journal_path.with_suffix(
            self._order_journal_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps({"version": 1, "orders": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self._order_journal_path)

    async def _complete_quote_with_market_metadata(self, quote: Quote) -> Quote:
        if (
            quote.token_id
            and quote.fee_rate_bps is not None
            and quote.is_neg_risk is not None
            and quote.is_yield_bearing is not None
        ):
            return quote

        market = await self._get_market_metadata(quote.market_id)
        return replace(
            quote,
            token_id=quote.token_id
            or self._find_outcome_token_id(
                market,
                quote.outcome,
                outcome_side=quote.outcome_side,
            ),
            fee_rate_bps=quote.fee_rate_bps
            if quote.fee_rate_bps is not None
            else self._optional_int(self._first_present(market, "feeRateBps", "fee_rate_bps")),
            is_neg_risk=quote.is_neg_risk
            if quote.is_neg_risk is not None
            else self._optional_bool(self._first_present(market, "isNegRisk", "is_neg_risk")),
            is_yield_bearing=quote.is_yield_bearing
            if quote.is_yield_bearing is not None
            else self._optional_bool(self._first_present(market, "isYieldBearing", "is_yield_bearing")),
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, object] | None = None,
    ) -> dict:
        authorization_retried = False
        while True:
            failed_token = self.settings.jwt_token
            try:
                for attempt in range(3):
                    try:
                        now = monotonic()
                        while self._request_times and self._request_times[0] <= now - 60:
                            self._request_times.popleft()
                        self._request_times.append(now)
                        return await asyncio.to_thread(
                            self._request_sync, method, path, payload, query
                        )
                    except requests.RequestException:
                        if method.upper() != "GET" or attempt == 2:
                            raise
                        await asyncio.sleep(0.25 * (2**attempt))
                    except PredictTransientError:
                        # Retrying a read is safe. Retrying order creation after a
                        # server-side 5xx could duplicate an order that was already
                        # accepted before the response failed.
                        if method.upper() != "GET" or attempt == 2:
                            raise
                        await asyncio.sleep(0.25 * (2**attempt))
                    except PredictRateLimitError as error:
                        self._rate_limit_until = max(
                            self._rate_limit_until, monotonic() + error.retry_after
                        )
                        raise
            except PredictAuthorizationError:
                if (
                    authorization_retried
                    or not self._uses_wallet_jwt(path)
                    or not self._can_refresh_jwt()
                ):
                    raise
                logger.warning("Predict.fun JWT 已失效，正在自动重新生成并重试请求。")
                await self._refresh_jwt(failed_token)
                authorization_retried = True

    def _can_refresh_jwt(self) -> bool:
        return bool(self.settings.api_key and self.settings.private_key)

    @staticmethod
    def _uses_wallet_jwt(path: str) -> bool:
        return path == "/v1/orders" or path.startswith(
            ("/v1/orders/", "/v1/positions", "/v1/account")
        )

    async def _refresh_jwt(self, failed_token: str | None) -> None:
        async with self._jwt_refresh_lock:
            if self.settings.jwt_token and self.settings.jwt_token != failed_token:
                return
            if not self.settings.private_key:
                raise RuntimeError("钱包 Private Key 未保存，无法自动重新生成 JWT。")

            if self.settings.predict_account_address:
                token = await self.create_predict_account_jwt(
                    self.settings.private_key,
                    self.settings.predict_account_address,
                )
            else:
                token = await self.create_eoa_jwt(self.settings.private_key)

            self.settings = replace(self.settings, jwt_token=token)
            os.environ["PREDICT_JWT_TOKEN"] = token
            if self._jwt_token_updated is not None:
                try:
                    self._jwt_token_updated(token)
                except Exception as error:  # noqa: BLE001
                    logger.warning("新 JWT 已生效，但保存到配置文件失败：%s", error)
            logger.info("Predict.fun JWT 已自动更新。")

    def _request_sync(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        query: dict[str, object] | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        clean_query = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and value != ""
        }
        request_options: dict[str, object] = {
            "headers": self._headers(),
            "params": clean_query or None,
            "timeout": 10,
        }
        if payload is not None:
            request_options["json"] = payload

        # Predict.fun 的官方 Python 文档使用 requests 访问认证和交易接口。
        # 采用相同客户端也可避免 urllib 的请求指纹被站点防护误判。
        try:
            response = requests.request(method, url, **request_options)
        except requests.RequestException:
            raise

        if response.status_code >= 400:
            try:
                parsed_error = response.json()
            except ValueError:
                parsed_error = {}
            error_data = self._data(parsed_error) if isinstance(parsed_error, dict) else {}
            detail = next(
                (
                    str(error_data.get(key)).strip()
                    for key in ("message", "error", "detail", "code")
                    if error_data.get(key) not in (None, "")
                ),
                "",
            )
            detail = re.sub(r"\s+", " ", detail)[:300]
            suffix = f"：{detail}" if detail else ""
            message = f"Predict.fun 拒绝了 {method} {path} 请求（HTTP {response.status_code}）{suffix}"
            if response.status_code == 429:
                try:
                    retry_after = max(1.0, float(response.headers.get("Retry-After", "5")))
                except (TypeError, ValueError):
                    retry_after = 5.0
                raise PredictRateLimitError(message, retry_after)
            if (response.status_code == 400 and method.upper() == "POST"
                    and path == "/v1/orders" and "insufficient shares" in detail.casefold()):
                raise PredictInsufficientSharesError(message)
            error_type = (
                PredictAuthorizationError
                if response.status_code == 401
                else PredictTransientError
                if response.status_code >= 500
                else RuntimeError
            )
            raise error_type(
                f"Predict.fun 拒绝了 {method} {path} 请求（HTTP {response.status_code}）{suffix}"
            )

        if not response.content:
            return {}
        try:
            result = response.json()
        except ValueError as error:
            raise PredictTransientError(f"Predict.fun 的 {method} {path} 响应不是有效 JSON。") from error
        if not isinstance(result, dict):
            raise PredictTransientError(f"Predict.fun 的 {method} {path} 响应格式不正确。")
        return result

    def _markets_from_public_page_sync(self, market_url: str, slug: str) -> list[dict]:
        request = urllib.request.Request(
            market_url,
            headers={
                "Accept": "text/html",
                "User-Agent": "Mozilla/5.0 (compatible; PredictMMBot/1.0)",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            markup = response.read().decode("utf-8", errors="replace")
        return self._public_category_markets_from_html(markup, slug)

    def _public_category_markets_from_html(self, markup: str, slug: str) -> list[dict]:
        """Extract the category payload embedded in Predict.fun Next.js page data."""
        slug_pattern = re.escape(slug)
        category_start = re.compile(
            rf'\{{"id":"[^"]+","slug":"{slug_pattern}"(?:,|\}})'
        )
        script_string = re.compile(r'self\.__next_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)')
        decoder = json.JSONDecoder()
        for script in script_string.findall(html.unescape(markup)):
            try:
                payload = json.loads(script)
            except json.JSONDecodeError:
                continue
            query_markets = self._markets_from_next_query_payload(payload, slug, decoder)
            if query_markets:
                return query_markets
            for match in category_start.finditer(payload):
                try:
                    category, _ = decoder.raw_decode(payload[match.start() :])
                except json.JSONDecodeError:
                    continue
                if not isinstance(category, dict) or category.get("slug") != slug:
                    continue
                markets = self._market_items(category.get("markets"))
                if not markets:
                    continue
                title = str(category.get("title") or "")
                return [
                    {**market, "categoryTitle": title, "categorySlug": slug}
                    for market in markets
                ]
        return []

    def _markets_from_next_query_payload(
        self, payload: str, slug: str, decoder: json.JSONDecoder
    ) -> list[dict]:
        """Read individual market query results hydrated by a category page."""
        results: list[dict] = []
        seen_ids: set[str] = set()
        for match in re.finditer(r'\{\s*"id"\s*:\s*"[^"]+"', payload):
            try:
                market, _ = decoder.raw_decode(payload[match.start() :])
            except json.JSONDecodeError:
                continue
            if not isinstance(market, dict) or not market.get("outcomes"):
                continue
            category = market.get("category")
            if not isinstance(category, dict):
                continue
            if str(category.get("id") or category.get("slug") or "") != slug:
                continue
            market_id = str(market.get("id") or "")
            if not market_id or market_id in seen_ids:
                continue
            results.append(
                {
                    **market,
                    "categoryTitle": str(category.get("title") or ""),
                    "categorySlug": slug,
                }
            )
            seen_ids.add(market_id)
        return results

    @staticmethod
    def _sign_eoa_auth_message(private_key: str, message: str) -> tuple[str, str]:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        account = Account.from_key(private_key)
        signed_message = account.sign_message(encode_defunct(text=message))
        return account.address, f"0x{signed_message.signature.hex()}"

    @staticmethod
    def _sign_predict_account_auth_message(
        private_key: str,
        predict_account_address: str,
        chain_id: int,
        message: str,
    ) -> str:
        from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions  # type: ignore[import-not-found]

        builder = OrderBuilder.make(
            ChainId(chain_id),
            private_key,
            OrderBuilderOptions(predict_account=predict_account_address),
        )
        return builder.sign_predict_account_message(message)

    def _parse_orderbook(
        self,
        market_id: str,
        data: dict,
        *,
        tick_size: Decimal | None = None,
        source: str = "rest",
    ) -> OrderBook:
        bids_raw = data.get("bids") or data.get("buy") or []
        asks_raw = data.get("asks") or data.get("sell") or []
        bids = sorted(
            [self._parse_level(row) for row in bids_raw],
            key=lambda level: level.price,
            reverse=True,
        )
        asks = sorted([self._parse_level(row) for row in asks_raw], key=lambda level: level.price)
        timestamp = data.get("updateTimestampMs")
        try:
            timestamp = int(timestamp) if timestamp is not None else None
        except (ValueError, TypeError, OverflowError):
            timestamp = None
        return OrderBook(
            market_id=market_id, bids=bids, asks=asks, tick_size=tick_size,
            update_timestamp_ms=timestamp, source=source,
        )

    async def _get_market_metadata(self, market_id: str) -> dict:
        if market_id not in self._market_metadata:
            response = await self._request("GET", f"/v1/markets/{market_id}")
            self._market_metadata[market_id] = self._data(response)
        return self._market_metadata[market_id]

    async def enrich_market_outcome_indices(self, market: dict) -> dict:
        """Attach official indexSet values to localized public-page outcomes."""
        market_id = str(market.get("id") or "")
        if not market_id:
            return market
        await self._get_market_metadata(market_id)
        raw_outcomes = market.get("outcomes") or []
        if isinstance(raw_outcomes, dict):
            raw_outcomes = raw_outcomes.get("edges") or []
        for raw_outcome in raw_outcomes:
            outcome = raw_outcome
            if isinstance(outcome, dict) and isinstance(outcome.get("node"), dict):
                outcome = outcome["node"]
            if not isinstance(outcome, dict):
                continue
            if self._first_present(outcome, "indexSet", "index_set") not in (None, ""):
                continue
            label = self._outcome_name(outcome).strip()
            side = self.cached_outcome_side(market_id, label)
            index_set = {"YES": 1, "NO": 2}.get(side or "")
            if index_set is not None:
                outcome["indexSet"] = index_set
        return market

    def cached_market_title(self, market_id: str) -> str:
        """Return a user-facing title for market metadata already fetched by the engine."""
        market = self._market_metadata.get(market_id) or {}
        for key in ("question", "title", "name"):
            value = str(market.get(key) or "").strip()
            if value:
                return value
        return ""

    def cached_outcome_side(self, market_id: str, outcome_name: str) -> str | None:
        """Map a user-facing outcome label to Predict's canonical YES/NO book side.

        Sports markets often replace the visible labels with team abbreviations.
        Predict still exposes one YES-denominated orderbook: CTF indexSet 1 is
        the YES outcome and indexSet 2 is the complementary NO outcome.
        """
        wanted_names = self._outcome_label_aliases(outcome_name)
        canonical = outcome_name.strip().upper()
        if canonical in {"YES", "NO"}:
            return canonical
        if canonical in {"YES_NO", "YES&NO", "YES AND NO"}:
            return "YES_NO"

        market = self._market_metadata.get(market_id) or {}
        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            names = self._outcome_metadata_aliases(outcome)
            if not wanted_names or wanted_names.isdisjoint(names):
                continue

            explicit_side = str(
                outcome.get("side") or outcome.get("outcome") or ""
            ).strip().upper()
            if explicit_side in {"YES", "NO"}:
                return explicit_side

            raw_index_set = self._first_present(outcome, "indexSet", "index_set")
            try:
                index_set = int(str(raw_index_set))
            except (TypeError, ValueError):
                return None
            if index_set == 1:
                return "YES"
            if index_set == 2:
                return "NO"
            return None
        return None

    @staticmethod
    def _outcome_label_aliases(value: str) -> set[str]:
        """Return exact labels plus safe aliases for localized outcome text."""
        normalized = " ".join(value.strip().casefold().split())
        if not normalized:
            return set()
        aliases = {normalized}

        direction_aliases = {
            "涨": "up",
            "上涨": "up",
            "上升": "up",
            "跌": "down",
            "下跌": "down",
            "下降": "down",
        }
        direction = direction_aliases.get(normalized)
        if direction:
            aliases.add(direction)

        localized_prefixes = {
            "大": "over",
            "小": "under",
            "超过": "over",
            "低于": "under",
        }
        for localized, english in localized_prefixes.items():
            if normalized == localized:
                aliases.add(english)
            elif normalized.startswith(f"{localized} "):
                aliases.add(f"{english}{normalized[len(localized):]}")

        usd_value = PredictClient._normalized_usd_outcome_value(normalized)
        if usd_value is not None:
            aliases.add(f"usd:{usd_value}")
        return aliases

    @staticmethod
    def _normalized_usd_outcome_value(value: str) -> str | None:
        """Normalize equivalent USD labels without guessing across currencies.

        Predict's localized pages can render ``$3,000`` as ``3,000美元`` and
        occasionally include a duplicate dollar sign.  Only labels that
        explicitly identify USD are normalized so unrelated numeric outcomes
        cannot collide.
        """
        compact = "".join(value.strip().casefold().split())
        number = ""
        if compact.startswith("us$"):
            number = compact[3:]
        elif compact.startswith("$"):
            number = compact.lstrip("$")
        else:
            for suffix in ("美元", "usd", "usdollars", "usdollar", "dollars", "dollar"):
                if compact.endswith(suffix):
                    number = compact[: -len(suffix)]
                    break
        if not number or not re.fullmatch(r"[0-9][0-9,]*(?:\.[0-9]+)?", number):
            return None
        try:
            amount = Decimal(number.replace(",", ""))
        except ArithmeticError:
            return None
        if not amount.is_finite():
            return None
        return format(amount.normalize(), "f")

    def _outcome_metadata_aliases(self, outcome: dict) -> set[str]:
        aliases = {
            alias
            for key in ("name", "outcome", "side", "title")
            if outcome.get(key) not in (None, "")
            for alias in self._outcome_label_aliases(str(outcome.get(key, "")))
        }
        for container_key in (
            "team",
            "participant",
            "competitor",
            "variantData",
            "variantDetails",
        ):
            container = outcome.get(container_key)
            if isinstance(container, dict):
                aliases.update(
                    alias
                    for key in (
                        "name",
                        "title",
                        "shortName",
                        "short_name",
                        "displayName",
                        "display_name",
                        "teamName",
                        "team_name",
                        "abbreviation",
                        "code",
                        "label",
                        "value",
                    )
                    if container.get(key) not in (None, "")
                    for alias in self._outcome_label_aliases(str(container.get(key, "")))
                )
            elif container not in (None, ""):
                aliases.update(self._outcome_label_aliases(str(container)))
        return aliases

    def _tick_size_from_market(self, market: dict, market_id: str) -> Decimal:
        raw_precision = self._first_present(market, "decimalPrecision", "decimal_precision")
        try:
            precision_decimal = Decimal(str(raw_precision))
            if precision_decimal != precision_decimal.to_integral_value():
                raise ValueError
            precision = int(precision_decimal)
        except (ArithmeticError, ValueError) as error:
            raise RuntimeError(
                f"Market {market_id} is missing a valid decimalPrecision; refusing to quote with an unknown tick size."
            ) from error
        if not 0 <= precision <= 18:
            raise RuntimeError(
                f"Market {market_id} has unsupported decimalPrecision={precision}; refusing to quote."
            )
        return Decimal(1).scaleb(-precision)

    def _parse_level(self, row: dict | list) -> Level:
        if isinstance(row, (list, tuple)):
            if len(row) < 2:
                raise ValueError(f"invalid orderbook price level: {row!r}")
            return Level(price=Decimal(str(row[0])), size=Decimal(str(row[1])))
        return Level(
            price=Decimal(str(row.get("price"))),
            size=Decimal(str(row.get("size") or row.get("quantity"))),
        )

    def _build_signed_limit_order_payload(self, quote: Quote, post_only: bool = True) -> dict:
        self._require_real_order_inputs(quote)
        try:
            from predict_sdk import (  # type: ignore[import-not-found]
                BuildOrderInput,
                ChainId,
                LimitHelperInput,
                OrderBuilder,
                OrderBuilderOptions,
                Side as PredictSide,
            )
        except ImportError as error:
            raise RuntimeError(
                "Real order creation requires the official Predict SDK. "
                "Install project dependencies with `pip install -e .`."
            ) from error

        sdk_side = PredictSide.BUY if quote.side.value == "buy" else PredictSide.SELL
        options = None
        if self.settings.predict_account_address:
            options = OrderBuilderOptions(predict_account=self.settings.predict_account_address)

        chain_id = ChainId(self.settings.chain_id)
        builder = OrderBuilder.make(chain_id, self.settings.private_key, options)
        amounts = builder.get_limit_order_amounts(
            LimitHelperInput(
                side=sdk_side,
                price_per_share_wei=self._to_wei(quote.price),
                quantity_wei=self._to_wei(quote.size),
            )
        )
        order = builder.build_order(
            "LIMIT",
            BuildOrderInput(
                side=sdk_side,
                token_id=quote.token_id,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=int(quote.fee_rate_bps or 0),
                # Removing an order from Predict's public orderbook does not
                # invalidate its signature on-chain. A short hard expiry bounds
                # that residual risk even if the process or network disappears.
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
            ),
        )
        typed_data = builder.build_typed_data(
            order,
            is_neg_risk=bool(quote.is_neg_risk),
            is_yield_bearing=bool(quote.is_yield_bearing),
        )
        order_hash = builder.build_typed_data_hash(typed_data)
        signed_order = builder.sign_typed_data_order(typed_data)
        signed_order_dict = self._signed_order_to_api_dict(signed_order, order_hash)
        data = {
            # Predict's REST API expects this field in 18-decimal integer units,
            # matching the SDK's ``price_per_share`` output. Sending a display
            # decimal such as "0.249" passes JSON validation but causes order
            # creation to fail inside the API.
            "pricePerShare": str(amounts.price_per_share),
            "strategy": "LIMIT",
            "isPostOnly": post_only,
            "selfTradePrevention": "CANCEL_MAKER",
            "order": signed_order_dict,
        }
        return {"data": data}

    def _wallet_fill_event(self, message: dict) -> WalletFillEvent | None:
        event_type = str(message.get("type") or "")
        # Submitted starts a speculative exit; Success records the factual fill.
        # Failed may omit a fill quantity but must still stop the named retry.
        if event_type not in {"orderTransactionSubmitted", "orderTransactionSuccess", "orderTransactionFailed"}:
            return None
        fill = message.get("fill") or (message.get("details") or {}).get("fill") or {}
        size_wei = fill.get("executedSizeWei")
        order_id = message.get("orderId")
        if not order_id or (size_wei in (None, "") and event_type != "orderTransactionFailed"):
            return None
        context = self._wallet_order_context(message)
        return WalletFillEvent(
            order_id=str(order_id),
            order_hash=str(message.get("orderHash") or "") or None,
            filled_size=Decimal(str(size_wei or "0")) / Decimal(10**18),
            settlement_id=str(message.get("settlementId")
                              or (message.get("details") or {}).get("settlementId") or "") or None,
            event_type=event_type,
            event_timestamp_ms=message.get("timestamp"),
            **context,
        )

    def _wallet_event(
        self, message: dict
    ) -> WalletFillEvent | WalletOrderStatusEvent | None:
        fill_event = self._wallet_fill_event(message)
        if fill_event is not None:
            return fill_event
        event_type = str(message.get("type") or "")
        if event_type not in {
            "orderAccepted",
            "orderNotAccepted",
            "orderExpired",
            "orderCancelled",
        }:
            return None
        order_id = message.get("orderId")
        if order_id in (None, ""):
            return None
        context = self._wallet_order_context(message)
        return WalletOrderStatusEvent(
            order_id=str(order_id),
            order_hash=str(message.get("orderHash") or "") or None,
            event_type=event_type,
            reason=str(message.get("reason") or "") or None,
            **context,
        )

    def _wallet_order_context(self, message: dict) -> dict[str, object]:
        details = message.get("details")
        if not isinstance(details, dict):
            details = {}
        market_id = details.get("marketId") or details.get("market_id")
        return {
            "market_id": str(market_id) if market_id not in (None, "") else None,
            "side": self._order_side(details.get("quoteType") or details.get("quote_type")),
            "outcome": str(details.get("outcome") or "") or None,
            "price": self._optional_decimal(details.get("price")),
            "order_size": self._optional_decimal(
                details.get("quantity") or details.get("size")
            ),
        }

    @staticmethod
    def _wallet_event_payload(message: dict) -> dict | None:
        """Unwrap an official wallet-topic message while accepting legacy direct payloads."""
        if message.get("type") == "M":
            topic = str(message.get("topic") or "")
            data = message.get("data")
            if topic.startswith("predictWalletEvents/") and isinstance(data, dict):
                return data
            return None
        if str(message.get("type") or "").startswith("order"):
            return message
        return None

    def _signed_order_to_api_dict(
        self,
        signed_order: object,
        order_hash: str,
    ) -> dict[str, object]:
        if is_dataclass(signed_order):
            raw = asdict(signed_order)
        elif hasattr(signed_order, "model_dump"):
            raw = signed_order.model_dump()
        elif isinstance(signed_order, dict):
            raw = signed_order
        else:
            raw = vars(signed_order)
        return {
            "hash": raw.get("hash") or order_hash,
            "salt": raw["salt"],
            "maker": raw["maker"],
            "signer": raw["signer"],
            "taker": raw["taker"],
            "tokenId": self._first_present(raw, "token_id", "tokenId"),
            "makerAmount": self._first_present(raw, "maker_amount", "makerAmount"),
            "takerAmount": self._first_present(raw, "taker_amount", "takerAmount"),
            "expiration": raw["expiration"],
            "nonce": raw["nonce"],
            "feeRateBps": self._first_present(raw, "fee_rate_bps", "feeRateBps"),
            "side": self._api_value(raw["side"]),
            # SignatureType.EOA is IntEnum(0). Using ``a or b`` here turns that
            # valid zero into None, which Predict's integer_int32 parser rejects.
            "signatureType": self._api_value(
                self._first_present(raw, "signature_type", "signatureType")
            ),
            "signature": raw["signature"],
        }

    def _require_real_order_inputs(self, quote: Quote) -> None:
        self._require_api_key()
        self._require_jwt()
        if not self.settings.private_key:
            raise RuntimeError("PREDICT_PRIVATE_KEY is required for real signed orders.")
        missing = [
            name
            for name, value in {
                "token_id": quote.token_id,
                "fee_rate_bps": quote.fee_rate_bps,
                "is_neg_risk": quote.is_neg_risk,
                "is_yield_bearing": quote.is_yield_bearing,
            }.items()
            if value is None or value == ""
        ]
        if missing:
            raise RuntimeError(
                "Market config is missing fields required by Predict SDK order signing: "
                + ", ".join(missing)
            )

    def _require_api_key(self) -> None:
        if not self.settings.api_key:
            raise RuntimeError("PREDICT_API_KEY is required for Predict.fun mainnet API requests.")

    def _require_jwt(self) -> None:
        if not self.settings.jwt_token:
            raise RuntimeError("PREDICT_JWT_TOKEN is required for wallet-specific Predict.fun requests.")

    def _data(self, response: dict) -> dict:
        data = response.get("data", response)
        return data if isinstance(data, dict) else {"data": data}

    def _rows(self, response: dict) -> list[dict]:
        data = response.get("data", response)
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("positions", "orders", "items"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
        return []

    def _market_items(self, value: object) -> list[dict]:
        """Accept both REST arrays and GraphQL-style market edge collections."""
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, dict):
            return []
        edges = value.get("edges")
        if isinstance(edges, list):
            return [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]
        return []

    def _outcome_name(self, outcome: object) -> str:
        if isinstance(outcome, dict):
            return str(
                outcome.get("name")
                or outcome.get("side")
                or outcome.get("outcome")
                or outcome.get("title")
                or ""
            )
        return str(outcome or "")

    @staticmethod
    def _display_order_intent(quote: Quote, *, post_only: bool) -> tuple[Side, str]:
        """Express passive binary asks as bids on the complementary outcome in logs.

        Predict's API submits a sell order for the selected token.  For the normal
        two-sided maker quote, users more naturally read that as buying the other
        choice.  This is a display-only conversion; it never changes the submitted
        order or its price.  Emergency exits remain explicit sell orders.
        """
        complementary_outcomes = {"YES": "NO", "NO": "YES"}
        outcome = quote.outcome.strip().upper()
        if post_only and quote.side == Side.SELL and outcome in complementary_outcomes:
            return Side.BUY, complementary_outcomes[outcome].title()
        return quote.side, quote.outcome

    def _to_wei(self, value: Decimal) -> int:
        return int(value * Decimal(10**18))

    def _api_value(self, value: object) -> object:
        return getattr(value, "value", value)

    def _optional_int(self, value: object) -> int | None:
        return None if value is None or value == "" else int(value)

    def _optional_bool(self, value: object) -> bool | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            return None

    @staticmethod
    def _from_wei(value: object) -> Decimal:
        if value in (None, ""):
            return Decimal("0")
        return Decimal(str(value)) / Decimal(10**18)

    @staticmethod
    def _order_side(value: object) -> Side | None:
        raw_value = getattr(value, "value", value)
        normalized = "" if raw_value is None else str(raw_value).strip().upper()
        if normalized in {"0", "BUY", "BID"}:
            return Side.BUY
        if normalized in {"1", "SELL", "ASK"}:
            return Side.SELL
        return None

    @staticmethod
    def _order_price(
        side: Side, maker_amount: Decimal, taker_amount: Decimal
    ) -> Decimal:
        if side == Side.BUY:
            return maker_amount / taker_amount if taker_amount else Decimal("0")
        return taker_amount / maker_amount if maker_amount else Decimal("0")

    def _first_present(self, data: dict, *keys: str) -> object:
        for key in keys:
            if key in data:
                return data[key]
        return None

    def _find_outcome_token_id(
        self,
        market: dict,
        outcome_name: str,
        *,
        outcome_side: str | None = None,
    ) -> str | None:
        wanted_names = self._outcome_label_aliases(outcome_name)
        canonical_side = (outcome_side or outcome_name).strip().upper()
        canonical_index_set = {"YES": 1, "NO": 2}.get(canonical_side)
        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            try:
                index_set = int(
                    str(self._first_present(outcome, "indexSet", "index_set"))
                )
            except (TypeError, ValueError):
                index_set = None
            if canonical_index_set is not None:
                if index_set is not None and index_set != canonical_index_set:
                    continue
                if index_set is None:
                    explicit_side = str(
                        outcome.get("side") or outcome.get("outcome") or outcome.get("name") or ""
                    ).strip().upper()
                    if explicit_side != canonical_side:
                        continue
            else:
                names = self._outcome_metadata_aliases(outcome)
                if not wanted_names or wanted_names.isdisjoint(names):
                    continue
            token_id = self._extract_token_id(outcome)
            if token_id:
                return token_id
        return None

    def _find_outcome_name(self, market: dict, token_id: str) -> str | None:
        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            if self._extract_token_id(outcome) != token_id:
                continue
            name = self._outcome_name(outcome).strip()
            if name:
                return name
            try:
                index_set = int(
                    str(self._first_present(outcome, "indexSet", "index_set"))
                )
            except (TypeError, ValueError):
                continue
            return {1: "YES", 2: "NO"}.get(index_set)
        return None

    def _extract_token_id(self, data: object) -> str | None:
        if isinstance(data, dict):
            for key in ("tokenId", "token_id", "onChainId", "on_chain_id"):
                value = data.get(key)
                if value not in (None, ""):
                    return str(value)
            for value in data.values():
                token_id = self._extract_token_id(value)
                if token_id:
                    return token_id
        elif isinstance(data, list):
            for value in data:
                token_id = self._extract_token_id(value)
                if token_id:
                    return token_id
        return None

    def _sample_orderbook(self, market_id: str) -> OrderBook:
        return OrderBook(
            market_id=market_id,
            bids=[Level(Decimal("0.493"), Decimal("100")), Level(Decimal("0.490"), Decimal("80"))],
            asks=[Level(Decimal("0.507"), Decimal("120")), Level(Decimal("0.510"), Decimal("90"))],
            tick_size=Decimal("0.001"),
        )
