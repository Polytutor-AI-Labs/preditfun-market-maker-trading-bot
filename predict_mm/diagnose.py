from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from predict_mm.client import PredictClient
from predict_mm.config import Settings, load_config
from predict_mm.strategy import PassiveMakerStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Predict.fun order diagnostics")
    parser.add_argument("market_id", help="Numeric Predict.fun market ID")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    return parser.parse_args()


async def diagnose(market_id: str, config_path: str) -> dict[str, object]:
    settings = Settings.from_env()
    config = load_config(Path(config_path))
    market_config = next(
        (market for market in config.enabled_markets if market.id == market_id),
        None,
    )
    if market_config is None:
        raise RuntimeError(f"Market {market_id} is not enabled in {config_path}.")

    client = PredictClient(settings=settings, dry_run=False)
    metadata = await client._get_market_metadata(market_id)
    book = await client.get_orderbook(market_id)
    quotes = PassiveMakerStrategy(config.strategy).build_quotes(market_config, book)

    is_neg_risk = bool(client._first_present(metadata, "isNegRisk", "is_neg_risk"))
    is_yield_bearing = bool(
        client._first_present(metadata, "isYieldBearing", "is_yield_bearing")
    )
    result: dict[str, object] = {
        "market": {
            "id": market_id,
            "configured_outcome": market_config.outcome,
            "fee_rate_bps": client._first_present(metadata, "feeRateBps", "fee_rate_bps"),
            "is_neg_risk": is_neg_risk,
            "is_yield_bearing": is_yield_bearing,
            "status": client._first_present(metadata, "tradingStatus", "status"),
            "tick_size": str(book.tick_size) if book.tick_size is not None else None,
            "best_bid": str(book.best_bid.price) if book.best_bid else None,
            "best_ask": str(book.best_ask.price) if book.best_ask else None,
        },
        "quotes": [],
        "account": {},
        "approvals": [],
    }

    if quotes:
        quote = await client._complete_quote_with_market_metadata(quotes[0])
        payload = await asyncio.to_thread(client._build_signed_limit_order_payload, quote, True)
        order = payload["data"]["order"]
        result["quotes"] = [
            {
                "side": quote.side.value,
                "outcome": quote.outcome,
                "price": str(quote.price),
                "size": str(quote.size),
                "payload_side": order.get("side"),
                "signature_type": order.get("signatureType"),
                "fee_rate_bps": order.get("feeRateBps"),
                "null_fields": [key for key, value in order.items() if value is None],
            }
        ]

    if not settings.private_key:
        raise RuntimeError("PREDICT_PRIVATE_KEY is not configured.")

    from predict_sdk import (  # type: ignore[import-not-found]
        ApprovalScope,
        ChainId,
        OrderBuilder,
        OrderBuilderOptions,
        Side as PredictSide,
    )

    options = (
        OrderBuilderOptions(predict_account=settings.predict_account_address)
        if settings.predict_account_address
        else None
    )
    builder = await asyncio.to_thread(
        OrderBuilder.make,
        ChainId(settings.chain_id),
        settings.private_key,
        options,
    )
    balance, _ = await client.get_usdt_balance()
    result["account"] = {"usdt_balance": str(balance)}

    steps = builder.get_approval_steps(
        ApprovalScope(
            operation="TRADE",
            is_neg_risk=is_neg_risk,
            is_yield_bearing=is_yield_bearing,
            side=PredictSide.BUY,
        )
    )
    checks = await builder.check_approvals_async(steps)
    result["approvals"] = [
        {"type": check.step.type, "satisfied": check.satisfied} for check in checks
    ]
    return result


def main() -> None:
    args = parse_args()
    result = asyncio.run(diagnose(args.market_id, args.config))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
