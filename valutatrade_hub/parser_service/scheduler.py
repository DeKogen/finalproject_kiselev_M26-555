"""Периодический запуск обновления курсов."""

import argparse
import logging
import time

from valutatrade_hub.logging_config import PARSER_LOGGER, parser_console, setup_logging
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.updater import build_updater

logger = logging.getLogger(PARSER_LOGGER)


class UpdateScheduler:
    """Вызывает run_update() каждые interval_seconds секунд."""

    def __init__(self, updater, interval_seconds):
        self.updater = updater
        self.interval_seconds = interval_seconds

    def run_forever(self):
        logger.info("Scheduler started, interval %s s", self.interval_seconds)
        while True:
            self.updater.run_update()
            time.sleep(self.interval_seconds)


def main(argv=None):
    """Точка входа valutatrade-parser: --once или цикл с интервалом."""
    parser = argparse.ArgumentParser(description="Обновление курсов по расписанию")
    parser.add_argument(
        "--interval",
        type=int,
        default=ParserConfig().UPDATE_INTERVAL_SECONDS,
        help="пауза между обновлениями в секундах",
    )
    parser.add_argument("--once", action="store_true", help="обновить один раз")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval должен быть положительным")
    setup_logging()
    updater = build_updater()
    with parser_console():
        if args.once:
            updater.run_update()
            return
        try:
            UpdateScheduler(updater, args.interval).run_forever()
        except KeyboardInterrupt:
            logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
