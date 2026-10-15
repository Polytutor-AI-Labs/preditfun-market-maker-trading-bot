from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from predict_mm.client import PredictClient
from predict_mm.config import Settings, load_config, update_dotenv_value
from predict_mm.engine import MarketMakerEngine
from predict_mm.logging import configure_logging
from predict_mm.risk import RiskManager
from predict_mm.setup_wizard import run_setup_wizard
from predict_mm.strategy import PassiveMakerStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predict.fun passive market maker bot")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    parser.add_argument("--setup", action="store_true", help="Run the interactive setup wizard")
    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="Fail instead of opening the setup wizard when config files are missing",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    env_path = Path(".env")

    if args.setup or (not args.no_setup and (not config_path.exists() or not env_path.exists())):
        run_setup_wizard(config_path=config_path, env_path=env_path)
        if args.setup:
            return

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    config = load_config(config_path)

    client = PredictClient(
        settings=settings,
        dry_run=config.dry_run,
        jwt_token_updated=lambda token: update_dotenv_value(
            env_path, "PREDICT_JWT_TOKEN", token
        ),
    )
    engine = MarketMakerEngine(
        config=config,
        client=client,
        strategy=PassiveMakerStrategy(config.strategy),
        risk=RiskManager(config.risk),
    )

    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(getattr(signal, signame), engine.request_stop)

    await engine.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
