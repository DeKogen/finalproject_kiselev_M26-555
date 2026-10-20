"""Проверки расчётов в USD, точности сумм и свежести кросс-курсов."""

import logging
import os
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from valutatrade_hub.core import usecases, utils
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    InsufficientFundsError,
    WalletNotFoundError,
)
from valutatrade_hub.core.models import Portfolio, Wallet
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.updater import UpdateReport


class CoreRegressions(unittest.TestCase):
    def setUp(self):
        self.previous_directory = Path.cwd()
        self.directory = tempfile.TemporaryDirectory(prefix="valutatrade-core-")
        os.chdir(self.directory.name)
        self.settings = SettingsLoader()
        self.settings.reload()
        usecases.reset_rates_cache()
        for name in ("valutatrade.actions", "valutatrade.parser"):
            logger = logging.getLogger(name)
            handler = logging.NullHandler()
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
        updater_patch = patch.object(usecases, "build_updater")
        self.updater = updater_patch.start()
        self.updater.return_value.run_update.return_value = UpdateReport(
            errors={"test": "offline"}
        )
        self.addCleanup(updater_patch.stop)
        self.user = usecases.register("alice", "1234")
        self.write_rates({"BTC_USD": 60000.0, "EUR_USD": 1.2, "DOGE_USD": 0.11})

    def tearDown(self):
        os.chdir(self.previous_directory)
        self.directory.cleanup()
        self.settings.reload()
        usecases.reset_rates_cache()

    def write_rates(self, rates):
        now = utils.to_iso(utils.utc_now())
        utils.write_json_atomic(
            Path("data/rates.json"),
            {
                "pairs": {
                    pair: {"rate": rate, "updated_at": now, "source": "test"}
                    for pair, rate in rates.items()
                },
                "last_refresh": now,
            },
        )
        usecases.reset_rates_cache()

    def balances(self):
        return {
            code: wallet.balance
            for code, wallet in usecases.load_portfolio(self.user).wallets.items()
        }

    def test_register_keeps_portfolio_empty(self):
        self.assertEqual(self.balances(), {})

    def test_buy_and_sell_preserve_portfolio_value(self):
        deposit = usecases.deposit(self.user.user_id, 1000)
        self.assertEqual((deposit.old_balance, deposit.new_balance), (0.0, 1000.0))
        bought = usecases.buy(self.user.user_id, "BTC", 0.01)
        self.assertEqual(self.balances(), {"USD": 400.0, "BTC": 0.01})
        self.assertEqual((bought.old_usd_balance, bought.new_usd_balance), (1000, 400))
        self.assertEqual(bought.value, 600)
        self.assertEqual(usecases.show_portfolio(self.user.user_id).total, 1000)
        sold = usecases.sell(self.user.user_id, "BTC", 0.004)
        self.assertEqual(self.balances(), {"USD": 640.0, "BTC": 0.006})
        self.assertEqual((sold.old_usd_balance, sold.new_usd_balance), (400, 640))
        self.assertEqual(sold.value, 240)
        self.assertEqual(usecases.show_portfolio(self.user.user_id).total, 1000)

    def test_trade_settles_in_usd_when_display_base_is_eur(self):
        Path("pyproject.toml").write_text(
            '[tool.valutatrade]\ndefault_base_currency = "EUR"\n',
            encoding="utf-8",
        )
        self.settings.reload()
        usecases.deposit(self.user.user_id, 1000)
        result = usecases.buy(self.user.user_id, "BTC", 0.01)
        self.assertEqual(result.base, "USD")
        self.assertEqual(self.balances()["USD"], 400)
        self.assertEqual(usecases.show_portfolio(self.user.user_id).base, "EUR")

    def test_portfolio_without_conversion_does_not_need_rates(self):
        cases = (
            ({"BTC": 1.0, "USD": 0.0}, "BTC", 1.0),
            ({"BTC": 0.0, "USD": 0.0}, "EUR", 0.0),
        )
        with patch.object(usecases, "ensure_rates") as ensure_rates:
            for balances, base, total in cases:
                with self.subTest(balances=balances, base=base):
                    portfolio = Portfolio(
                        self.user,
                        {code: Wallet(code, value) for code, value in balances.items()},
                    )
                    usecases.save_portfolio(portfolio)
                    result = usecases.show_portfolio(self.user.user_id, base)
                    self.assertEqual(result.base, base)
                    self.assertEqual(result.total, total)
                    self.assertEqual(
                        {row.code: row.balance for row in result.rows}, balances
                    )
                    self.assertIsNone(result.updated_at)
                    self.assertFalse(result.is_stale)
                    ensure_rates.assert_not_called()

    def test_insufficient_buy_preserves_file_and_does_not_create_wallet(self):
        usecases.deposit(self.user.user_id, 1)
        previous = Path("data/portfolios.json").read_bytes()
        with self.assertRaises(InsufficientFundsError):
            usecases.buy(self.user.user_id, "BTC", 0.01)
        self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)
        self.assertEqual(self.balances(), {"USD": 1.0})

    def test_buy_without_usd_fails_without_creating_wallets(self):
        previous = Path("data/portfolios.json").read_bytes()
        with self.assertRaises(InsufficientFundsError):
            usecases.buy(self.user.user_id, "BTC", 0.01)
        self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)
        self.assertEqual(self.balances(), {})

    def test_insufficient_sell_preserves_both_wallets(self):
        usecases.deposit(self.user.user_id, 600)
        usecases.buy(self.user.user_id, "BTC", 0.01)
        previous = Path("data/portfolios.json").read_bytes()
        with self.assertRaises(InsufficientFundsError):
            usecases.sell(self.user.user_id, "BTC", 0.02)
        self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)

    def test_missing_sell_wallet_fails_before_network(self):
        Path("data/rates.json").unlink()
        usecases.reset_rates_cache()
        with self.assertRaises(WalletNotFoundError):
            usecases.sell(self.user.user_id, "BTC", 1)
        self.updater.assert_not_called()

    def test_usd_buy_and_sell_settle_at_one_without_changing_balance(self):
        usecases.deposit(self.user.user_id, 100)
        previous = Path("data/portfolios.json").read_bytes()
        Path("data/rates.json").unlink()
        usecases.reset_rates_cache()
        for operation in (usecases.buy, usecases.sell):
            for amount in (1, 100, 1e-12):
                with self.subTest(operation=operation.__name__, amount=amount):
                    result = operation(self.user.user_id, "USD", amount)
                    self.assertEqual(result.rate, 1)
                    self.assertEqual(result.value, amount)
                    self.assertEqual(result.base, "USD")
                    self.assertEqual(
                        (result.old_balance, result.new_balance), (100, 100)
                    )
                    self.assertEqual(
                        (result.old_usd_balance, result.new_usd_balance), (100, 100)
                    )
                    self.assertFalse(result.is_stale)
                    self.assertEqual(
                        Path("data/portfolios.json").read_bytes(), previous
                    )
            with self.subTest(operation=operation.__name__, insufficient=True):
                with self.assertRaises(InsufficientFundsError):
                    operation(self.user.user_id, "USD", 101)
                self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)
        self.assertEqual(self.balances(), {"USD": 100.0})
        self.updater.assert_not_called()

    def test_fractional_usd_settlement_matches_reported_value(self):
        self.write_rates({"DOGE_USD": 0.123456789})
        usecases.deposit(self.user.user_id, 1)
        amount = 0.012345678901
        result = usecases.buy(self.user.user_id, "DOGE", amount)
        self.assertEqual(
            result.value, float(Decimal(str(amount)) * Decimal("0.123456789"))
        )
        self.assertNotEqual(result.value, round(result.value, 8))
        self.assertAlmostEqual(self.balances()["USD"], 1 - result.value, delta=1e-16)
        self.assertEqual(self.balances()["DOGE"], amount)
        usecases.sell(self.user.user_id, "DOGE", amount)
        self.assertEqual(self.balances(), {"USD": 1.0, "DOGE": 0.0})

    def test_decimal_settlement_keeps_exact_remaining_usd_available(self):
        self.write_rates({"EUR_USD": 1.1})
        usecases.deposit(self.user.user_id, 370)
        result = usecases.buy(self.user.user_id, "EUR", 200)
        self.assertEqual(result.value, 220)
        self.assertEqual(self.balances(), {"USD": 150.0, "EUR": 200.0})
        result = usecases.buy(self.user.user_id, "USD", 150)
        self.assertEqual(result.value, 150)
        self.assertEqual((result.old_balance, result.new_balance), (150, 150))
        self.assertEqual(self.balances(), {"USD": 150.0, "EUR": 200.0})

    def test_tiny_trade_preserves_amount_and_settlement(self):
        self.write_rates({"DOGE_USD": 0.5})
        usecases.deposit(self.user.user_id, 1e-9)
        result = usecases.buy(self.user.user_id, "DOGE", 1e-10)
        self.assertEqual(result.value, 5e-11)
        self.assertEqual(self.balances(), {"USD": 9.5e-10, "DOGE": 1e-10})
        usecases.sell(self.user.user_id, "DOGE", 1e-10)
        self.assertEqual(self.balances(), {"USD": 1e-9, "DOGE": 0.0})

    def test_smallest_positive_float_can_be_traded_at_one(self):
        amount = float.fromhex("0x0.0000000000001p-1022")
        self.write_rates({"EUR_USD": 1.0})
        usecases.deposit(self.user.user_id, amount)
        result = usecases.buy(self.user.user_id, "EUR", amount)
        self.assertEqual(result.value, amount)
        self.assertEqual(self.balances(), {"USD": 0.0, "EUR": amount})
        usecases.sell(self.user.user_id, "EUR", amount)
        self.assertEqual(self.balances(), {"USD": amount, "EUR": 0.0})

    def test_amount_and_settlement_above_one_trillion_are_allowed(self):
        self.write_rates({"EUR_USD": 2.0})
        usecases.deposit(self.user.user_id, 2e13)
        result = usecases.buy(self.user.user_id, "EUR", 5e12)
        self.assertEqual(result.value, 1e13)
        self.assertEqual(self.balances(), {"USD": 1e13, "EUR": 5e12})
        usecases.sell(self.user.user_id, "EUR", 5e12)
        self.assertEqual(self.balances(), {"USD": 2e13, "EUR": 0.0})

    def test_unrepresentable_settlement_does_not_change_portfolio(self):
        usecases.deposit(self.user.user_id, 1)
        previous = Path("data/portfolios.json").read_bytes()
        for amount, rate in ((5e-324, 0.1), (1e308, 1e308)):
            with self.subTest(amount=amount, rate=rate):
                self.write_rates({"DOGE_USD": rate})
                with self.assertRaisesRegex(ValueError, "Стоимость сделки"):
                    usecases.buy(self.user.user_id, "DOGE", amount)
                self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)

    def test_unrepresentable_second_balance_preserves_both_wallets(self):
        self.write_rates({"DOGE_USD": 1.0})
        cases = (
            (usecases.buy, {"USD": 10.0, "DOGE": 1e308}, 1.0),
            (usecases.sell, {"USD": 1e308, "DOGE": 1e-10}, 1e-10),
        )
        for operation, balances, amount in cases:
            with self.subTest(operation=operation.__name__):
                portfolio = Portfolio(
                    self.user,
                    {code: Wallet(code, value) for code, value in balances.items()},
                )
                usecases.save_portfolio(portfolio)
                previous = Path("data/portfolios.json").read_bytes()
                with self.assertRaisesRegex(ValueError, "точности"):
                    operation(self.user.user_id, "DOGE", amount)
                self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)
                self.assertEqual(self.balances(), balances)

    def test_wallet_strictly_rejects_overdraw_and_keeps_extra_precision(self):
        wallet = Wallet("BTC", 0.00000001)
        with self.assertRaises(InsufficientFundsError):
            wallet.withdraw(0.0000000105)
        self.assertEqual(wallet.balance, 0.00000001)
        wallet.deposit(0.0000000105)
        self.assertEqual(wallet.balance, 0.0000000205)

    def test_wallet_and_validator_accept_all_representable_positive_scales(self):
        for amount in (5e-324, 1e-12, 0.123456789123456, 1e13, 1e308):
            with self.subTest(amount=amount):
                self.assertEqual(utils.validate_amount(amount), amount)
                wallet = Wallet("USD", amount)
                self.assertEqual(wallet.balance, amount)
                wallet.withdraw(amount)
                self.assertEqual(wallet.balance, 0)
                wallet.deposit(amount)
                self.assertEqual(wallet.balance, amount)

    def test_invalid_amounts_are_rejected_without_changing_balance(self):
        for amount in (True, False, float("nan"), float("inf"), 0, -1, 10**400):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    utils.validate_amount(amount)
                wallet = Wallet("USD", 10)
                for operation in (wallet.deposit, wallet.withdraw):
                    with self.assertRaises(ValueError):
                        operation(amount)
                    self.assertEqual(wallet.balance, 10)

    def test_insufficient_funds_message_distinguishes_close_amounts(self):
        cases = (
            ("BTC", 1.00000001, 1.00000002, "1.00000001", "1.00000002"),
            ("BTC", 1.00000000001, 1.00000000002, "1.00000000001", "1.00000000002"),
            ("USD", 100.001, 100.002, "100.001", "100.002"),
        )
        for code, available, required, available_text, required_text in cases:
            with self.subTest(code=code):
                wallet = Wallet(code, available)
                with self.assertRaises(InsufficientFundsError) as caught:
                    wallet.withdraw(required)
                self.assertEqual(
                    str(caught.exception),
                    f"Недостаточно средств: доступно {available_text} {code}, "
                    f"требуется {required_text} {code}",
                )
                self.assertEqual(wallet.balance, available)

    def test_wallet_decimal_arithmetic_and_unrepresentable_deposit(self):
        wallet = Wallet("USD", 0.3)
        wallet.withdraw(0.1)
        wallet.withdraw(0.2)
        self.assertEqual(wallet.balance, 0)
        wallet.balance = 1e12
        with self.assertRaisesRegex(ValueError, "точности"):
            wallet.deposit(0.00000001)
        self.assertEqual(wallet.balance, 1e12)
        with self.assertRaisesRegex(ValueError, "точности"):
            wallet.withdraw(0.00000001)
        self.assertEqual(wallet.balance, 1e12)
        wallet.balance = 1e308
        with self.assertRaisesRegex(ValueError, "бесконечным"):
            wallet.deposit(1e308)
        self.assertEqual(wallet.balance, 1e308)

    def test_stale_rates_reject_operations_without_changing_portfolio(self):
        usecases.deposit(self.user.user_id, 1000)
        usecases.buy(self.user.user_id, "BTC", 0.01)
        previous = Path("data/portfolios.json").read_bytes()
        snapshot = usecases.load_rates()
        oldest = utils.to_iso(utils.utc_now() - timedelta(days=1))
        snapshot["pairs"]["BTC_USD"]["updated_at"] = oldest
        utils.write_json_atomic(Path("data/rates.json"), snapshot)
        operations = (
            lambda: usecases.get_rate("BTC", "USD"),
            lambda: usecases.buy(self.user.user_id, "BTC", 0.001),
            lambda: usecases.sell(self.user.user_id, "BTC", 0.001),
            lambda: usecases.show_portfolio(self.user.user_id),
        )
        for index, operation in enumerate(operations):
            with self.subTest(operation=index):
                usecases.reset_rates_cache()
                with self.assertRaisesRegex(ApiRequestError, "offline"):
                    operation()
                self.assertEqual(Path("data/portfolios.json").read_bytes(), previous)
        self.updater.reset_mock()
        rows, _ = usecases.list_rates(currency="BTC")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_stale)
        self.assertEqual(rows[0].updated_at, oldest)
        self.updater.assert_not_called()

    def test_rate_is_usable_after_successful_refresh(self):
        self.write_rates({})

        def refresh():
            self.write_rates({"BTC_USD": 61000.0})
            return UpdateReport()

        self.updater.return_value.run_update.side_effect = refresh
        result = usecases.get_rate("BTC", "USD")
        self.assertEqual(result.rate, 61000)
        self.assertFalse(result.is_stale)
        self.updater.return_value.run_update.assert_called_once()

    def test_cross_rate_uses_both_sources_and_oldest_timestamp(self):
        snapshot = usecases.load_rates()
        oldest = utils.to_iso(utils.utc_now() - timedelta(days=1))
        snapshot["pairs"]["EUR_USD"]["updated_at"] = oldest
        snapshot["pairs"]["EUR_USD"]["source"] = "ExchangeRate-API"
        snapshot["pairs"]["BTC_USD"]["source"] = "CoinGecko"
        with patch.object(usecases, "load_rates", return_value=snapshot):
            rows, _ = usecases.list_rates(base="EUR", currency="BTC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].updated_at, oldest)
        self.assertTrue(rows[0].is_stale)
        self.assertEqual(rows[0].source, "CoinGecko + ExchangeRate-API")
        self.assertEqual(rows[0].rate, 50000)
        self.updater.assert_not_called()

    def test_invalid_numeric_rates_never_count_as_fresh(self):
        now = utils.to_iso(utils.utc_now())
        for invalid in (True, False, float("inf"), float("nan"), 0, -1):
            with self.subTest(rate=invalid):
                usecases.reset_rates_cache()
                snapshot = {
                    "pairs": {"BTC_USD": {"rate": invalid, "updated_at": now}},
                    "last_refresh": now,
                }
                self.assertEqual(usecases.rates_table(snapshot), {})
                self.assertFalse(usecases.pairs_are_fresh(snapshot, ("BTC", "USD")))
                with patch.object(usecases, "load_rates", return_value=snapshot):
                    with self.assertRaises(ApiRequestError):
                        usecases.get_rate("USD", "BTC")


if __name__ == "__main__":
    unittest.main()
