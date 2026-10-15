from __future__ import annotations

from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

from predict_mm.config import DepthProtectionConfig


@dataclass(frozen=True)
class WizardAnswers:
    api_base_url: str = "https://api.predict.fun"
    api_key: str = ""
    jwt_token: str = ""
    private_key: str = ""
    predict_account_address: str = ""
    log_level: str = "INFO"
    dry_run: bool = False
    emergency_exit_on_buy_fill: bool = True
    market_id: str = ""
    outcome: str = "YES"
    token_id: str = ""
    quote_size: str = "1.0"
    cancel_after_seconds: str = "60"
    run_duration_seconds: int = 0
    max_position_per_market: str = "10.0"
    max_total_position: str = "50.0"
    depth_protection: DepthProtectionConfig = field(default_factory=DepthProtectionConfig)


@dataclass(frozen=True)
class MarketAnswers:
    market_id: str
    market_title: str = ""
    outcome: str = "YES"
    outcome_index_set: int | None = None
    quote_size: str = "1.0"
    token_id: str = ""


def run_setup_wizard(config_path: str | Path = "config.toml", env_path: str | Path = ".env") -> None:
    config_file = Path(config_path)
    env_file = Path(env_path)

    print("\nPredict.fun 做市机器人初始配置向导")
    print("我会帮你生成 .env 和 config.toml。默认是实盘运行，请确认后再继续。\n")

    answers = WizardAnswers(
        api_base_url=_ask("API 地址", "https://api.predict.fun"),
        api_key=_ask_secret("API Key，可先留空"),
        jwt_token=_ask_secret("JWT Token，实盘/查询个人订单需要，可先留空"),
        private_key=_ask_secret("钱包 Private Key，实盘签单需要，可先留空"),
        predict_account_address=_ask("Predict Account Address，可先留空", ""),
        log_level=_ask("日志级别", "INFO").upper(),
        dry_run=_ask_bool("是否使用 dry-run 模拟运行？选择 no 会真实下单", False),
        emergency_exit_on_buy_fill=_ask_bool("买单被吃单后是否立即卖出？可能造成损失", True),
        market_id=_ask_required("要运行的 Market ID"),
        outcome=_ask_choice("交易 outcome", "YES", {"YES", "NO"}),
        token_id=_ask("Outcome token_id，实盘建议填写；留空则运行时尝试自动获取", ""),
        quote_size=_ask("单次挂单数量 quote_size", "1.0"),
        cancel_after_seconds=_ask("挂单存活时间（秒）", "60"),
        max_position_per_market=_ask("单市场最大仓位", "10.0"),
        max_total_position=_ask("总最大仓位", "50.0"),
    )

    _write_new_file(env_file, build_env_text(answers), "环境变量文件")
    _write_new_file(config_file, build_config_text(answers), "机器人配置文件")

    print("\n配置完成。")
    print(f"- 已生成 {env_file}")
    print(f"- 已生成 {config_file}")
    print("下一步可以运行：python -m predict_mm.main --config config.toml\n")


def build_env_text(answers: WizardAnswers) -> str:
    return "\n".join(
        [
            f"PREDICT_API_BASE_URL={answers.api_base_url}",
            f"PREDICT_API_KEY={answers.api_key}",
            f"PREDICT_JWT_TOKEN={answers.jwt_token}",
            f"PREDICT_PRIVATE_KEY={answers.private_key}",
            f"PREDICT_ACCOUNT_ADDRESS={answers.predict_account_address}",
            f"LOG_LEVEL={answers.log_level}",
            "",
        ]
    )


def build_config_text(answers: WizardAnswers, markets: list[MarketAnswers] | None = None) -> str:
    depth_lines = []
    for name in answers.depth_protection.__dataclass_fields__:
        value = getattr(answers.depth_protection, name)
        rendered = _toml_bool(value) if isinstance(value, bool) else f'"{value}"'
        depth_lines.append(f"{name} = {rendered}")
    depth_text = "\n".join(depth_lines)
    configured_markets = markets or [
        MarketAnswers(
            market_id=answers.market_id,
            outcome=answers.outcome,
            quote_size=answers.quote_size,
            token_id=answers.token_id,
        )
    ]
    markets_text = "\n".join(
        f'''[[markets]]
id = "{_toml_escape(market.market_id)}"
title = "{_toml_escape(market.market_title)}"
enabled = true
outcome = "{market.outcome}"
{f'outcome_index_set = {market.outcome_index_set}' if market.outcome_index_set is not None else ''}
quote_size = "{_toml_escape(market.quote_size)}"
token_id = "{_toml_escape(market.token_id)}"
'''
        for market in configured_markets
    )
    return f"""dry_run = {_toml_bool(answers.dry_run)}
poll_interval_seconds = 2.0
cancel_after_seconds = {answers.cancel_after_seconds}
run_duration_seconds = {answers.run_duration_seconds}
replace_on_orderbook_change = true
cancel_all_on_start = true
cancel_all_on_shutdown = true
emergency_exit_on_buy_fill = {_toml_bool(answers.emergency_exit_on_buy_fill)}

{markets_text}

[depth_protection]
{depth_text}

[strategy]
tick_size = "0.001"
quote_size = "{_toml_escape(answers.quote_size)}"
join_best_price = false
min_edge_ticks = 2
max_spread_to_quote = "0.20"
min_spread_to_quote = "0.006"

[risk]
max_order_size = "{_toml_escape(answers.quote_size)}"
max_position_per_market = "{_toml_escape(answers.max_position_per_market)}"
max_total_position = "{_toml_escape(answers.max_total_position)}"
max_open_orders_per_market = 2
pause_after_fill_seconds = 60
"""


def _write_new_file(path: Path, content: str, label: str) -> None:
    if path.exists():
        if not _ask_bool(f"{label} {path} 已存在，是否覆盖？", False):
            print(f"已跳过 {path}")
            return
    path.write_text(content, encoding="utf-8")


def _ask(label: str, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _ask_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("这个字段不能为空。")


def _ask_secret(label: str) -> str:
    value = getpass(f"{label}: ").strip()
    return value


def _ask_bool(label: str, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "是", "true", "1"}:
            return True
        if value in {"n", "no", "否", "false", "0"}:
            return False
        print("请输入 yes 或 no。")


def _ask_choice(label: str, default: str, choices: set[str]) -> str:
    while True:
        value = _ask(label, default).upper()
        if value in choices:
            return value
        print(f"可选值：{', '.join(sorted(choices))}")


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
