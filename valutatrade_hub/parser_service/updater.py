"""Опрос всех клиентов, объединение курсов и сохранение результата."""

import logging
from dataclasses import dataclass, field

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import to_iso, utc_now
from valutatrade_hub.logging_config import PARSER_LOGGER
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RatesStorage

logger = logging.getLogger(PARSER_LOGGER)
SOURCES = {"coingecko": CoinGeckoClient, "exchangerate": ExchangeRateApiClient}


@dataclass
class UpdateReport:
    """Итог обновления: число записанных курсов и ошибки по источникам."""

    total: int = 0
    last_refresh: str | None = None
    errors: dict = field(default_factory=dict)


class RatesUpdater:
    """Точка входа Parser Service: run_update() опрашивает все источники."""

    def __init__(self, clients, storage):
        self.clients = list(clients)
        self.storage = storage

    def run_update(self):
        """Собрать курсы; падение одного источника не мешает остальным."""
        logger.info("Starting rates update...")
        report = UpdateReport()
        now = to_iso(utc_now())
        pairs = {}
        history = []
        for client in self.clients:
            try:
                rates = client.fetch_rates()
            except Exception as error:
                reason = str(error)
                if isinstance(error, ApiRequestError):
                    reason = error.reason
                logger.error("Failed to fetch from %s: %s", client.name, reason)
                report.errors[client.name] = reason
                continue
            logger.info("Fetching from %s... OK (%d rates)", client.name, len(rates))
            for pair, rate in rates.items():
                from_code, _, to_code = pair.partition("_")
                pairs[pair] = {"rate": rate, "updated_at": now, "source": client.name}
                history.append(
                    {
                        "id": f"{pair}_{now}",
                        "from_currency": from_code,
                        "to_currency": to_code,
                        "rate": rate,
                        "timestamp": now,
                        "source": client.name,
                        "meta": {
                            "raw_id": client.config.CRYPTO_ID_MAP.get(
                                from_code, from_code
                            ),
                            **client.last_meta,
                        },
                    }
                )
        if pairs:
            path = str(self.storage.rates_path).replace("\\", "/")
            logger.info("Writing %d rates to %s...", len(pairs), path)
            try:
                self.storage.save_snapshot(pairs, now)
                self.storage.append_history(history)
            except OSError as error:
                logger.error("Failed to write rates: %s", error)
                report.errors["storage"] = str(error)
            else:
                report.total = len(pairs)
                report.last_refresh = now
        logger.info(
            "Update finished: %d rates saved, failed sources: %s",
            report.total,
            ", ".join(report.errors) or "none",
        )
        return report


def build_updater(source=None, config=None):
    """Собрать RatesUpdater со всеми клиентами или только с одним источником."""
    config = config or ParserConfig()
    names = [source] if source else list(SOURCES)
    for name in names:
        if name not in SOURCES:
            raise ValueError(
                f"Неизвестный источник '{name}'. Доступны: {', '.join(SOURCES)}"
            )
    clients = [SOURCES[name](config) for name in names]
    storage = RatesStorage(config.RATES_FILE_PATH, config.HISTORY_FILE_PATH)
    return RatesUpdater(clients, storage)
