from __future__ import annotations

from collections import Counter
from decimal import Decimal
import logging

from predict_mm.config import RiskConfig
from predict_mm.models import ManagedOrder, Quote, Side

logger = logging.getLogger("predict-mm")


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def filter_quotes(
        self,
        quotes: list[Quote],
        open_orders: list[ManagedOrder],
        positions: dict[str, Decimal],
    ) -> list[Quote]:
        counts = Counter(order.quote.market_id for order in open_orders)
        total_position = sum(abs(size) for size in positions.values())
        accepted: list[Quote] = []

        for quote in quotes:
            if quote.size > self.config.max_order_size:
                logger.warning("Skip quote above max order size: %s", quote)
                continue

            if counts[quote.market_id] >= self.config.max_open_orders_per_market:
                continue

            current_position = positions.get(quote.market_id, Decimal("0"))
            projected = self._project_position(current_position, quote)
            if abs(projected) > self.config.max_position_per_market:
                logger.warning("Skip quote above market position limit: %s", quote)
                continue

            projected_total = total_position + quote.size
            if projected_total > self.config.max_total_position:
                logger.warning("Skip quote above total exposure limit: %s", quote)
                continue

            accepted.append(quote)
            counts[quote.market_id] += 1

        return accepted

    def _project_position(self, position: Decimal, quote: Quote) -> Decimal:
        if quote.side == Side.BUY:
            return position + quote.size
        return position - quote.size
