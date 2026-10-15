from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "https://api.predict.fun"
    api_key: str | None = None
    jwt_token: str | None = None
    private_key: str | None = None
    predict_account_address: str | None = None
    chain_id: int = 56
    log_level: str = "INFO"
    order_journal_path: str = ".predict-mm-orders.json"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(".env"))
        return cls(
            api_base_url=os.getenv("PREDICT_API_BASE_URL", cls.api_base_url),
            api_key=os.getenv("PREDICT_API_KEY") or None,
            jwt_token=os.getenv("PREDICT_JWT_TOKEN") or None,
            private_key=os.getenv("PREDICT_PRIVATE_KEY") or None,
            predict_account_address=os.getenv("PREDICT_ACCOUNT_ADDRESS") or None,
            chain_id=int(os.getenv("PREDICT_CHAIN_ID", str(cls.chain_id))),
            log_level=os.getenv("LOG_LEVEL", cls.log_level),
            order_journal_path=os.getenv(
                "PREDICT_ORDER_JOURNAL_PATH", cls.order_journal_path
            ),
        )


@dataclass(frozen=True)
class StrategyConfig:
    tick_size: Decimal = Decimal("0.001")
    quote_size: Decimal = Decimal("1")
    join_best_price: bool = False
    min_edge_ticks: int = 2
    max_spread_to_quote: Decimal = Decimal("0.20")
    min_spread_to_quote: Decimal = Decimal("0.006")


@dataclass(frozen=True)
class RiskConfig:
    max_order_size: Decimal = Decimal("1")
    max_position_per_market: Decimal = Decimal("10")
    max_total_position: Decimal = Decimal("50")
    max_open_orders_per_market: int = 2
    pause_after_fill_seconds: float = 60


@dataclass(frozen=True)
class DepthProtectionConfig:
    enabled: bool = True
    cancel_min_shares: Decimal = Decimal("200")
    cancel_size_multiplier: Decimal = Decimal("2")
    resume_min_shares: Decimal = Decimal("400")
    resume_size_multiplier: Decimal = Decimal("4")
    drop_window_seconds: float = 2.0
    drop_percent: Decimal = Decimal("50")
    stable_seconds: float = 3.0
    cooldown_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in ("cancel_min_shares", "cancel_size_multiplier", "resume_min_shares",
                     "resume_size_multiplier", "drop_percent"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError("深度保护阈值必须是有限的正数")
        if (self.resume_min_shares <= self.cancel_min_shares
                or self.resume_size_multiplier <= self.cancel_size_multiplier):
            raise ValueError("恢复深度的最低份额和数量倍数必须高于撤单阈值")
        if self.drop_percent > 100:
            raise ValueError("深度下降百分比不能超过 100")
        for name in ("drop_window_seconds", "stable_seconds", "cooldown_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError("深度保护时间必须大于 0 且不超过 300 秒")


@dataclass(frozen=True)
class MarketConfig:
    id: str
    enabled: bool = True
    outcome: str = "YES"
    outcome_index_set: int | None = None
    quote_size: Decimal | None = None
    token_id: str | None = None
    fee_rate_bps: int | None = None
    is_neg_risk: bool | None = None
    is_yield_bearing: bool | None = None
    title: str | None = None


@dataclass(frozen=True)
class BotConfig:
    dry_run: bool = False
    poll_interval_seconds: float = 2
    cancel_after_seconds: float = 60
    run_duration_seconds: int = 0
    replace_on_orderbook_change: bool = True
    cancel_all_on_start: bool = True
    cancel_all_on_shutdown: bool = True
    emergency_exit_on_buy_fill: bool = True
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    depth_protection: DepthProtectionConfig = field(default_factory=DepthProtectionConfig)
    markets: list[MarketConfig] = field(default_factory=list)

    @property
    def enabled_markets(self) -> list[MarketConfig]:
        return [market for market in self.markets if market.enabled]


def load_config(path: str | Path) -> BotConfig:
    with Path(path).open("rb") as file:
        raw = tomllib.load(file)

    markets = [_market(market) for market in raw.get("markets", [])]
    config = BotConfig(
        dry_run=bool(raw.get("dry_run", False)),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 2)),
        cancel_after_seconds=float(raw.get("cancel_after_seconds", 60)),
        run_duration_seconds=max(0, int(raw.get("run_duration_seconds", 0))),
        replace_on_orderbook_change=bool(raw.get("replace_on_orderbook_change", True)),
        cancel_all_on_start=bool(raw.get("cancel_all_on_start", True)),
        cancel_all_on_shutdown=bool(raw.get("cancel_all_on_shutdown", True)),
        emergency_exit_on_buy_fill=bool(raw.get("emergency_exit_on_buy_fill", True)),
        strategy=_strategy(raw.get("strategy", {})),
        risk=_risk(raw.get("risk", {})),
        depth_protection=depth_protection_config(raw.get("depth_protection", {})),
        markets=markets,
    )
    if not config.enabled_markets:
        raise ValueError("at least one enabled market is required")
    return config


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def update_dotenv_value(path: str | Path, key: str, value: str) -> None:
    """Update one dotenv value without rewriting unrelated account settings."""
    if "\n" in value or "\r" in value:
        raise ValueError("dotenv values must not contain newlines")
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current_key, _ = stripped.split("=", 1)
        if current_key.strip() != key:
            continue
        lines[index] = f"{key}={value}"
        updated = True
        break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _decimal(value: object, default: str) -> Decimal:
    return Decimal(str(value if value is not None else default))


def depth_protection_config(raw: dict) -> DepthProtectionConfig:
    defaults = DepthProtectionConfig()
    values = {}
    for name in defaults.__dataclass_fields__:
        default = getattr(defaults, name)
        value = raw.get(name, default)
        if isinstance(default, Decimal):
            values[name] = Decimal(str(value))
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError("深度保护开关必须是布尔值")
            values[name] = value
        else:
            values[name] = float(value)
    return DepthProtectionConfig(**values)


def _strategy(raw: dict) -> StrategyConfig:
    return StrategyConfig(
        tick_size=_decimal(raw.get("tick_size"), "0.001"),
        quote_size=_decimal(raw.get("quote_size"), "1"),
        join_best_price=bool(raw.get("join_best_price", False)),
        min_edge_ticks=int(raw.get("min_edge_ticks", 2)),
        max_spread_to_quote=_decimal(raw.get("max_spread_to_quote"), "0.20"),
        min_spread_to_quote=_decimal(raw.get("min_spread_to_quote"), "0.006"),
    )


def _market(raw: dict) -> MarketConfig:
    market = dict(raw)
    if market.get("quote_size") is not None:
        market["quote_size"] = _decimal(market["quote_size"], "1")
    return MarketConfig(**market)


def _risk(raw: dict) -> RiskConfig:
    return RiskConfig(
        max_order_size=_decimal(raw.get("max_order_size"), "1"),
        max_position_per_market=_decimal(raw.get("max_position_per_market"), "10"),
        max_total_position=_decimal(raw.get("max_total_position"), "50"),
        max_open_orders_per_market=int(raw.get("max_open_orders_per_market", 2)),
        pause_after_fill_seconds=float(raw.get("pause_after_fill_seconds", 60)),
    )
