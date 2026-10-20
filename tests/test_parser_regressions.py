"""Проверки валидации API, истории и параллельных обновлений."""

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from valutatrade_hub.core.exceptions import ApiRequestError, CurrencyNotFoundError
from valutatrade_hub.core.utils import to_iso
from valutatrade_hub.parser_service import scheduler
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import (
    RatesUpdater,
    UpdateReport,
    build_updater,
)

REQUESTS_GET = "valutatrade_hub.parser_service.api_clients.requests.get"
STAMP = "2026-10-20T12:00:00Z"


def history_record(pair, stamp, rate):
    """Полная запись измерения для временного хранилища."""
    from_code, to_code = pair.split("_")
    return {
        "id": f"{pair}_{stamp}",
        "from_currency": from_code,
        "to_currency": to_code,
        "rate": rate,
        "timestamp": stamp,
        "source": "test",
    }


class ParserRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="parser-regression-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = ParserConfig(
            EXCHANGERATE_API_KEY="secret-test-key",
            CRYPTO_CURRENCIES=("BTC",),
            FIAT_CURRENCIES=("EUR",),
            RATES_FILE_PATH=str(self.root / "rates.json"),
            HISTORY_FILE_PATH=str(self.root / "history.json"),
        )
        self.storage = RatesStorage(
            self.config.RATES_FILE_PATH, self.config.HISTORY_FILE_PATH
        )
        logger = logging.getLogger("valutatrade.parser")
        handler = logging.NullHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

    def response(self, payload):
        response = Mock(status_code=200, headers={})
        response.json.return_value = payload
        return response

    def test_clients_reject_invalid_rates(self):
        values = (True, False, float("nan"), float("inf"), -1, 0, "1.2", None)
        for value in values:
            with self.subTest(value=value):
                with patch(
                    REQUESTS_GET,
                    return_value=self.response({"bitcoin": {"usd": value}}),
                ):
                    with self.assertRaises(ApiRequestError):
                        CoinGeckoClient(self.config).fetch_rates()
                with patch(
                    REQUESTS_GET,
                    return_value=self.response(
                        {"result": "success", "conversion_rates": {"EUR": value}}
                    ),
                ):
                    with self.assertRaises(ApiRequestError):
                        ExchangeRateApiClient(self.config).fetch_rates()

    def test_inverse_rate_must_remain_positive_and_finite(self):
        with patch(
            REQUESTS_GET,
            return_value=self.response(
                {"result": "success", "conversion_rates": {"EUR": 1e-320}}
            ),
        ):
            with self.assertRaises(ApiRequestError):
                ExchangeRateApiClient(self.config).fetch_rates()
        for value in (1e10, 1e300):
            with (
                self.subTest(value=value),
                patch(
                    REQUESTS_GET,
                    return_value=self.response(
                        {"result": "success", "conversion_rates": {"EUR": value}}
                    ),
                ),
            ):
                rate = ExchangeRateApiClient(self.config).fetch_rates()["EUR_USD"]
                self.assertGreater(rate, 0)
                self.assertEqual(rate, 1 / value)

    def test_malformed_response_raises_api_error(self):
        payloads = (
            [],
            {"result": "success", "conversion_rates": ["EUR"]},
            {"result": "success", "rates": "EUR"},
            {"result": "error", "error-type": []},
        )
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                patch(REQUESTS_GET, return_value=self.response(payload)),
            ):
                with self.assertRaises(ApiRequestError):
                    ExchangeRateApiClient(self.config).fetch_rates()

    def test_api_key_is_not_in_debug_logs_or_errors(self):
        secret = self.config.EXCHANGERATE_API_KEY
        with (
            patch(
                REQUESTS_GET,
                side_effect=requests.ConnectionError(f"https://example.test/{secret}"),
            ),
            self.assertLogs("valutatrade.parser", level="DEBUG") as captured,
        ):
            with self.assertRaises(ApiRequestError) as raised:
                ExchangeRateApiClient(self.config).fetch_rates()
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_snapshot_ignores_old_equal_and_invalid_measurements(self):
        first = {"rate": 50000.0, "updated_at": STAMP, "source": "test"}
        self.storage.save_snapshot({"BTC_USD": first}, STAMP)
        for stamp in ("2020-01-01T00:00:00Z", STAMP):
            self.storage.save_snapshot(
                {"BTC_USD": {"rate": 1.0, "updated_at": stamp}}, stamp
            )
        for rate in (True, float("inf"), float("nan")):
            self.storage.save_snapshot(
                {"ETH_USD": {"rate": rate, "updated_at": STAMP}}, STAMP
            )
        snapshot = self.storage.load_snapshot()
        self.assertEqual(snapshot["pairs"], {"BTC_USD": first})
        self.assertEqual(snapshot["last_refresh"], STAMP)

    def test_history_deduplicates_existing_and_same_batch_ids(self):
        record = history_record("BTC_USD", STAMP, 50000.0)
        invalid = history_record("ETH_USD", STAMP, True)
        self.assertEqual(self.storage.append_history([record, record, invalid]), 1)
        self.assertEqual(self.storage.append_history([record]), 0)
        self.assertEqual(self.storage.load_history(), [record])

    def test_invalid_source_does_not_stop_valid_source(self):
        bad = Mock(config=self.config, last_meta={})
        good = Mock(config=self.config, last_meta={})
        bad.name = "bad"
        good.name = "good"
        bad.fetch_rates.return_value = {"BTC_USD": True}
        good.fetch_rates.return_value = {"EUR_USD": 1.1}
        report = RatesUpdater([bad, good], self.storage).run_update()
        self.assertEqual(report.total, 1)
        self.assertIn("bad", report.errors)
        self.assertEqual(set(self.storage.load_snapshot()["pairs"]), {"EUR_USD"})
        self.assertEqual(len(self.storage.load_history()), 1)

    def test_non_usd_base_still_produces_usd_pairs(self):
        config = replace(
            self.config, BASE_CURRENCY="eur", FIAT_CURRENCIES=("EUR", "GBP")
        )
        self.assertEqual(config.BASE_CURRENCY, "EUR")
        responses = [
            self.response({"bitcoin": {"usd": 60000.0}}),
            self.response(
                {
                    "result": "success",
                    "base_code": "EUR",
                    "conversion_rates": {"USD": 1.25, "GBP": 0.8},
                }
            ),
        ]
        with patch(REQUESTS_GET, side_effect=responses) as get:
            report = build_updater(config=config).run_update()
        self.assertEqual(report.total, 3)
        self.assertEqual(report.errors, {})
        self.assertEqual(get.call_args_list[0].kwargs["params"]["vs_currencies"], "usd")
        self.assertTrue(get.call_args_list[1].args[0].endswith("/latest/EUR"))
        rates = {
            pair: record["rate"]
            for pair, record in self.storage.load_snapshot()["pairs"].items()
        }
        self.assertEqual(
            rates, {"BTC_USD": 60000.0, "EUR_USD": 1.25, "GBP_USD": 1.5625}
        )

    def test_non_usd_base_works_without_api_key(self):
        config = replace(self.config, BASE_CURRENCY="EUR", EXCHANGERATE_API_KEY=None)
        payload = {"result": "success", "rates": {"USD": 1.25}}
        with patch(REQUESTS_GET, return_value=self.response(payload)) as get:
            rates = ExchangeRateApiClient(config).fetch_rates()
        self.assertEqual(rates, {"EUR_USD": 1.25})
        self.assertEqual(get.call_args.args[0], f"{config.EXCHANGERATE_OPEN_URL}/EUR")

    def test_non_usd_base_requires_valid_usd_bridge(self):
        config = replace(self.config, BASE_CURRENCY="EUR")
        for bridge in (None, True, 0, -1, float("nan"), float("inf")):
            payload = {"result": "success", "conversion_rates": {"USD": bridge}}
            with (
                self.subTest(bridge=bridge),
                patch(REQUESTS_GET, return_value=self.response(payload)),
            ):
                with self.assertRaisesRegex(ApiRequestError, "EUR→USD"):
                    ExchangeRateApiClient(config).fetch_rates()

    def test_parser_base_must_be_a_registered_fiat_currency(self):
        for base in ("BTC", "ABC", "", None):
            with self.subTest(base=base):
                with self.assertRaises((ValueError, CurrencyNotFoundError)):
                    replace(self.config, BASE_CURRENCY=base)

    def test_measurement_times_follow_each_response(self):
        start = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        times = [start + timedelta(seconds=index) for index in (1, 2, 3)]
        clock = iter(times)
        events = []
        clients = []
        for name, pair, rate in (
            ("CoinGecko", "BTC_USD", 60000.0),
            ("ExchangeRate-API", "EUR_USD", 1.25),
        ):
            client = Mock(config=self.config, last_meta={"status_code": 200})
            client.name = name

            def fetch(name=name, pair=pair, rate=rate):
                events.append(name)
                return {pair: rate}

            client.fetch_rates.side_effect = fetch
            clients.append(client)

        def now():
            events.append("clock")
            return next(clock)

        with patch("valutatrade_hub.parser_service.updater.utc_now", side_effect=now):
            report = RatesUpdater(clients, self.storage).run_update()
        self.assertEqual(
            events, ["CoinGecko", "clock", "ExchangeRate-API", "clock", "clock"]
        )
        snapshot = self.storage.load_snapshot()
        history = self.storage.load_history()
        self.assertEqual(report.total, 2)
        self.assertEqual(len(history), 2)
        self.assertEqual(report.last_refresh, to_iso(times[2]))
        self.assertEqual(snapshot["last_refresh"], to_iso(times[2]))
        for record, pair, moment, source in zip(
            history,
            ("BTC_USD", "EUR_USD"),
            times[:2],
            ("CoinGecko", "ExchangeRate-API"),
        ):
            stamp = to_iso(moment)
            self.assertEqual(record["id"], f"{pair}_{stamp}")
            self.assertEqual(record["timestamp"], stamp)
            self.assertEqual(snapshot["pairs"][pair]["updated_at"], stamp)
            self.assertEqual(record["source"], source)
            self.assertEqual(record["meta"]["status_code"], 200)

    def test_once_reports_failure_with_nonzero_exit_code(self):
        with (
            patch.object(scheduler, "setup_logging"),
            patch.object(scheduler, "build_updater") as build,
        ):
            build.return_value.run_update.return_value = UpdateReport(
                errors={"API": "offline"}
            )
            self.assertEqual(scheduler.main(["--once"]), 1)
            build.return_value.run_update.return_value = UpdateReport(total=1)
            self.assertEqual(scheduler.main(["--once"]), 0)

    def test_concurrent_processes_preserve_snapshot_and_history(self):
        worker = textwrap.dedent(
            """\
            import sys
            import time
            from pathlib import Path
            from valutatrade_hub.parser_service.storage import RatesStorage

            root, pair = Path(sys.argv[1]), sys.argv[2]
            storage = RatesStorage(root / "rates.json", root / "history.json")
            original = storage.load_snapshot
            def delayed_load():
                snapshot = original()
                time.sleep(0.01)
                return snapshot
            storage.load_snapshot = delayed_load
            (root / (pair + ".ready")).touch()
            deadline = time.monotonic() + 10
            while not (root / "start").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("start barrier")
                time.sleep(0.01)
            for index in range(12):
                stamp = f"2026-10-20T12:00:{index:02d}Z"
                source, target = pair.split("_")
                record = {
                    "id": f"{pair}_{stamp}", "from_currency": source,
                    "to_currency": target, "timestamp": stamp,
                    "rate": index + 1, "source": "test",
                }
                storage.save_update(
                    {pair: {"rate": index + 1, "updated_at": stamp}},
                    stamp, [record],
                )
            """
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        processes = []
        try:
            for pair in ("BTC_USD", "ETH_USD"):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", worker, str(self.root), pair],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 10
            while len(list(self.root.glob("*.ready"))) != 2:
                if time.monotonic() > deadline:
                    self.fail("workers did not reach start barrier")
                time.sleep(0.01)
            (self.root / "start").touch()
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        snapshot = self.storage.load_snapshot()
        self.assertEqual(set(snapshot["pairs"]), {"BTC_USD", "ETH_USD"})
        self.assertEqual(
            {pair: record["rate"] for pair, record in snapshot["pairs"].items()},
            {"BTC_USD": 12, "ETH_USD": 12},
        )
        history = self.storage.load_history()
        self.assertEqual(len(history), 24)
        self.assertEqual(len({record["id"] for record in history}), 24)


if __name__ == "__main__":
    unittest.main()
