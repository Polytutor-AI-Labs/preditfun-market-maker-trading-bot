from __future__ import annotations

from decimal import Decimal

from predict_mm.config import MarketConfig, StrategyConfig
from predict_mm.models import OrderBook, Quote, Side, quantize_price


class PassiveMakerStrategy:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def build_quotes(
        self,
        market: MarketConfig,
        orderbook: OrderBook,
        *,
        outcome_side: str | None = None,
    ) -> list[Quote]:
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask
        spread = orderbook.spread
        if not best_bid or not best_ask or spread is None:
            return []

        tick_size = orderbook.tick_size or self.config.tick_size
        # A fixed minimum spread incorrectly rejects liquid 0.001-tick markets
        # whose best bid and ask are one tick apart. Quotes are placed away from
        # the touch below, so the narrowest valid spread is the market tick.
        effective_min_spread = min(self.config.min_spread_to_quote, tick_size)
        if spread < effective_min_spread:
            return []
        if spread > self.config.max_spread_to_quote:
            return []

        edge = tick_size * Decimal(self.config.min_edge_ticks)
        if self.config.join_best_price:
            raw_bid = best_bid.price
            raw_ask = best_ask.price
        else:
            raw_bid = best_bid.price - edge
            raw_ask = best_ask.price + edge

        # At the bottom of a 0.001-tick book there may be no room to move the
        # configured two ticks away from the touch.  Keep a valid maker quote
        # at the lowest market tick instead of dropping the market entirely.
        # This is the 0.1-cent price shown by Predict.fun.
        if raw_bid < tick_size and best_bid.price >= tick_size:
            raw_bid = tick_size

        bid = quantize_price(raw_bid, tick_size, Side.BUY)
        ask = quantize_price(raw_ask, tick_size, Side.SELL)
        # Predict's orderbook is always expressed in YES prices.  A NO bid is
        # therefore the complement of the YES ask, not the YES ask itself.
        # Quantize again at the market precision to avoid Decimal values that
        # the backend cannot represent.
        raw_no_bid = Decimal("1") - ask
        current_no_bid = Decimal("1") - best_ask.price
        if raw_no_bid < tick_size and current_no_bid >= tick_size:
            raw_no_bid = tick_size
        no_bid = quantize_price(raw_no_bid, tick_size, Side.BUY)

        quote_size = market.quote_size or self.config.quote_size

        def buy(outcome: str, price: Decimal, canonical_side: str) -> Quote:
            return Quote(
                market_id=market.id,
                side=Side.BUY,
                price=price,
                size=quote_size,
                outcome=outcome,
                token_id=market.token_id,
                fee_rate_bps=market.fee_rate_bps,
                is_neg_risk=market.is_neg_risk,
                is_yield_bearing=market.is_yield_bearing,
                outcome_side=canonical_side,
            )

        selected = (outcome_side or market.outcome).strip().upper()
        yes_quote_is_safe = (
            bid > Decimal("0")
            and ask < Decimal("1")
            and bid < ask
        )
        no_quote_is_safe = Decimal("0") < no_bid < Decimal("1")
        if selected in {"YES_NO", "YES&NO", "YES AND NO"}:
            if not yes_quote_is_safe or not no_quote_is_safe:
                return []
            return [buy("Yes", bid, "YES"), buy("No", no_bid, "NO")]
        if selected == "NO":
            if not no_quote_is_safe:
                return []
            return [buy(market.outcome, no_bid, "NO")]
        if selected == "YES":
            if not yes_quote_is_safe:
                return []
            return [buy(market.outcome, bid, "YES")]
        return []
