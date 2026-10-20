"""Функциональные проверки: uv run python tests/test_valutatrade.py."""

import io
import json
import logging
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import requests

from valutatrade_hub.cli import interface
from valutatrade_hub.core import usecases, utils
from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    FiatCurrency,
    get_currency,
)
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    AuthError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    RateNotFoundError,
    WalletNotFoundError,
)
from valutatrade_hub.core.models import Portfolio, User, Wallet
from valutatrade_hub.decorators import cached, log_action
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import JsonFormatter
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig, load_env_file
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import RatesUpdater, UpdateReport

REQUESTS_GET = "valutatrade_hub.parser_service.api_clients.requests.get"
PARSER_CONFIG = ParserConfig(
    EXCHANGERATE_API_KEY="key", RATES_FILE_PATH="r.json", HISTORY_FILE_PATH="h.json"
)
OFFLINE = UpdateReport(errors={"CoinGecko": "Network error"})

for name in ("valutatrade.actions", "valutatrade.parser"):
    logging.getLogger(name).addHandler(logging.NullHandler())


@contextmanager
def temporary_workdir():
    """Отдельная рабочая папка, чтобы не трогать данные проекта."""
    previous = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="valutatrade-test-") as directory:
        os.chdir(directory)
        usecases.reset_rates_cache()
        try:
            yield Path(directory)
        finally:
            os.chdir(previous)


def assert_raises(exception_type, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except exception_type as error:
        return error
    raise AssertionError(f"{exception_type.__name__} не выброшено")


def write_rates(pairs, age_seconds=0):
    """Снимок курсов заданного возраста в data/rates.json."""
    stamp = utils.to_iso(utils.utc_now() - timedelta(seconds=age_seconds))
    snapshot = {
        "pairs": {
            pair: {"rate": rate, "updated_at": stamp, "source": "test"}
            for pair, rate in pairs.items()
        },
        "last_refresh": stamp,
    }
    utils.write_json_atomic(Path("data") / "rates.json", snapshot)
    usecases.load_rates.cache_clear()


def offline_updater():
    """Подменить парсер отчётом о недоступной сети."""
    patcher = patch.object(usecases, "build_updater")
    build = patcher.start()
    build.return_value.run_update.return_value = OFFLINE
    return patcher, build


def run_cli(*argv):
    output = io.StringIO()
    with redirect_stdout(output):
        interface.run_command(list(argv))
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"ETag": "abc"}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    name = "Fake"

    def __init__(self, rates=None, error=None):
        self.rates = rates
        self.error = error
        self.config = PARSER_CONFIG
        self.last_meta = {"status_code": 200}

    def fetch_rates(self):
        if isinstance(self.error, Exception):
            raise self.error
        if self.error:
            raise ApiRequestError(self.error)
        return self.rates


def test_user_password_hashing():
    user = User.create(1, "alice", "1234")
    assert user.verify_password("1234")
    assert not user.verify_password("0000")
    assert user.hashed_password != "1234" and len(user.hashed_password) == 64
    assert user.hashed_password == User.hash_password("1234", user.salt)
    old_hash = user.hashed_password
    user.change_password("5678")
    assert user.verify_password("5678") and user.hashed_password != old_hash
    info = user.get_user_info()
    assert "hashed_password" not in info and "salt" not in info
    restored = User.from_dict(user.to_dict())
    assert restored.verify_password("5678")
    assert restored.registration_date == user.registration_date
    assert "T" in user.to_dict()["registration_date"]
    assert_raises(ValueError, User.from_dict, {"user_id": 1})
    assert_raises(ValueError, User.from_dict, "garbage")


def test_user_validation():
    assert_raises(ValueError, User.create, 1, "", "1234")
    assert_raises(ValueError, User.create, 1, "bob", "123")
    user = User.create(1, "bob", "1234")
    assert_raises(ValueError, user.change_password, "12")
    assert_raises(ValueError, setattr, user, "username", "  ")
    assert_raises(AttributeError, setattr, user, "user_id", 5)


def test_wallet_rules():
    wallet = Wallet("btc")
    assert wallet.currency_code == "BTC" and wallet.balance == 0.0
    wallet.deposit(0.05)
    wallet.withdraw(0.01)
    assert wallet.balance == 0.04
    assert_raises(ValueError, wallet.deposit, 0)
    assert_raises(ValueError, wallet.deposit, -1)
    small = Wallet("BTC")
    small.deposit(1e-9)
    assert small.balance == 1e-9
    assert_raises(ValueError, wallet.withdraw, "1")
    error = assert_raises(InsufficientFundsError, wallet.withdraw, 1)
    expected = "Недостаточно средств: доступно 0.0400 BTC, требуется 1.0000 BTC"
    assert str(error) == expected
    assert_raises(ValueError, setattr, wallet, "balance", -1)
    assert_raises(ValueError, setattr, wallet, "balance", float("inf"))
    assert_raises(TypeError, setattr, wallet, "balance", "10")
    assert_raises(TypeError, setattr, wallet, "balance", True)
    assert wallet.get_balance_info() == "BTC: 0.0400"
    wallet.withdraw(0.04)
    assert wallet.balance == 0.0
    fiat = Wallet("USD", 100)
    error = assert_raises(InsufficientFundsError, fiat.withdraw, 250.5)
    expected = "Недостаточно средств: доступно 100.00 USD, требуется 250.50 USD"
    assert str(error) == expected
    precise = Wallet("EUR", 0.3)
    precise.withdraw(0.1)
    precise.withdraw(0.2)
    assert precise.balance == 0.0


def test_portfolio_rules():
    user = User.create(7, "carol", "1234")
    portfolio = Portfolio(user)
    wallet = portfolio.add_currency("btc")
    assert wallet is portfolio.get_wallet("BTC")
    assert_raises(ValueError, portfolio.add_currency, "BTC")
    assert_raises(WalletNotFoundError, portfolio.get_wallet, "ETH")
    copy = portfolio.wallets
    copy["ETH"] = Wallet("ETH")
    assert "ETH" not in portfolio.wallets
    assert portfolio.user is user and portfolio.user_id == 7
    assert_raises(AttributeError, setattr, portfolio, "user", None)
    portfolio.get_wallet("BTC").deposit(0.5)
    portfolio.add_currency("EUR").deposit(100)
    rates = {"BTC_USD": 60000.0, "EUR_USD": 1.1}
    assert abs(portfolio.get_total_value("USD", rates) - 30110.0) < 1e-6
    expected_eur = 30000 / 1.1 + 100
    assert abs(portfolio.get_total_value("EUR", rates) - expected_eur) < 1e-6
    assert portfolio.get_total_value() > 0
    assert_raises(RateNotFoundError, portfolio.get_total_value, "USD", {})
    restored = Portfolio.from_dict(portfolio.to_dict(), user)
    assert restored.get_wallet("EUR").balance == 100
    legacy = {"user_id": 7, "wallets": {"USD": {"balance": 5}}}
    assert Portfolio.from_dict(legacy, user).get_wallet("USD").balance == 5
    assert_raises(ValueError, Portfolio.from_dict, {"wallets": {"USD": 5}}, user)


def test_currencies():
    usd = get_currency("usd").get_display_info()
    assert usd == "[FIAT] USD — US Dollar (Issuing: United States)"
    btc = get_currency("BTC").get_display_info()
    assert btc == "[CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.60e12)"
    assert isinstance(get_currency("ETH"), CryptoCurrency)
    assert isinstance(get_currency("EUR"), FiatCurrency)
    assert_raises(CurrencyNotFoundError, get_currency, "ABC")
    assert_raises(ValueError, FiatCurrency, "", "USD", "x")
    assert_raises(ValueError, FiatCurrency, "Dollar", "usd", "x")
    assert_raises(ValueError, CryptoCurrency, "X", "TOOLONG", "a", 1)


def test_utils_formats():
    assert utils.format_rate(59337.21) == "59337.21"
    assert utils.format_rate(1.0786) == "1.0786"
    assert utils.format_rate(0.00001685) == "0.00001685"
    assert utils.format_rate(0.01016) == "0.01016"
    assert utils.format_rate(1.2e-7) == "0.00000012"
    assert utils.format_money(3329) == "3,329.00"
    assert utils.format_money(0.5, "BTC") == "0.5000"
    assert utils.format_amount(0.05, "BTC") == "0.0500"
    assert utils.format_amount(200, "EUR") == "200.00"
    assert utils.format_amount(0.00000001, "BTC") == "0.00000001"
    assert utils.format_amount(0.00000001, "USD") == "0.00000001"
    assert utils.format_money(0.0006, "USD") == "0.0006"
    assert utils.format_money(1e-12, "USD") == "1e-12"
    assert_raises(ValueError, utils.parse_amount, "abc")
    assert_raises(ValueError, utils.parse_amount, "-1")
    assert utils.parse_amount("1e-9") == 1e-9
    assert_raises(ValueError, utils.validate_amount, float("nan"))
    assert utils.parse_amount("0.5") == 0.5
    rates = {"BTC_USD": 50000.0, "EUR_USD": 1.25}
    assert utils.find_rate(rates, "USD", "BTC") == 1 / 50000
    assert utils.find_rate(rates, "BTC", "EUR") == 40000.0
    assert utils.find_rate({}, "BTC", "USD") is None
    assert utils.is_fresh(utils.to_iso(utils.utc_now()), 60)
    assert not utils.is_fresh("2020-01-01T00:00:00Z", 60)
    assert not utils.is_fresh(None, 60)
    assert utils.parse_iso("2025-10-09T12:00:00").tzinfo is not None
    assert utils.format_moment(None) == "нет данных"
    stamp = utils.format_moment("2025-10-09T10:30:00Z", "T")
    assert len(stamp) == 19 and stamp[10] == "T"
    with temporary_workdir():
        Path("empty.json").write_text("", encoding="utf-8")
        assert utils.read_json("empty.json", []) == []
        Path("broken.json").write_text("{", encoding="utf-8")
        assert_raises(ValueError, utils.read_json, "broken.json")
        utils.write_json_atomic("data/x.json", {"a": 1})
        assert utils.read_json("data/x.json") == {"a": 1}
        assert not list(Path("data").glob("*.tmp"))


def test_singletons_and_settings():
    assert SettingsLoader() is SettingsLoader()
    assert DatabaseManager() is DatabaseManager()
    settings = SettingsLoader()
    assert settings.get("rates_ttl_seconds") == 300
    assert settings.get("missing", "x") == "x"
    assert str(settings.data_path("users")).endswith("users.json")
    with temporary_workdir():
        Path("pyproject.toml").write_text(
            "[tool.valutatrade]\nrates_ttl_seconds = 7\n", encoding="utf-8"
        )
        settings.reload()
        assert settings.get("rates_ttl_seconds") == 7
        settings.reload("missing.toml")
        assert settings.get("rates_ttl_seconds") == 300
    settings.reload()


def test_env_file():
    with temporary_workdir():
        Path(".env").write_bytes(
            b"\xef\xbb\xbfexport TEST_VALUTATRADE_KEY='abc'\n# comment\nBROKEN\n"
        )
        os.environ.pop("TEST_VALUTATRADE_KEY", None)
        try:
            load_env_file()
            assert os.environ["TEST_VALUTATRADE_KEY"] == "abc"
        finally:
            os.environ.pop("TEST_VALUTATRADE_KEY", None)
        Path(".env").write_bytes("KEY=1\n".encode("utf-16"))
        load_env_file()
    assert "EXCHANGERATE_API_KEY" not in repr(PARSER_CONFIG)


def test_decorators():
    calls = []

    @cached(60)
    def compute(x):
        calls.append(x)
        return x * 2

    assert compute(2) == 4 and compute(2) == 4 and calls == [2]
    compute.cache_clear()
    compute(2)
    assert calls == [2, 2]

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("valutatrade.actions")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:

        @log_action("BUY", verbose=True)
        def operation(user_id, currency_code, amount):
            if amount < 0:
                raise ValueError("bad amount")
            return usecases.TradeResult(
                "alice", currency_code, amount, 2.5, "USD", 0.0, amount, False
            )

        operation(1, "BTC", 0.5)
        assert_raises(ValueError, operation, 1, "BTC", -1.0)
        operation(1, "BTC", 1e-8)
        operation(1, "BTC", 0.12345678)
        operation(1, "BTC", 1e-12)
    finally:
        logger.removeHandler(handler)
    messages = [record.getMessage() for record in records]
    assert messages[0] == (
        "BUY user='alice' currency='BTC' amount=0.5000 rate=2.5000 base='USD' "
        "balance=0.0000->0.5000 result=OK"
    )
    assert messages[1].startswith(
        "BUY user=1 currency='BTC' amount=-1.0000 result=ERROR error_type=ValueError"
    )
    assert "amount=0.00000001 " in messages[2]
    assert "balance=0.0000->0.00000001 " in messages[2]
    assert "amount=0.12345678 " in messages[3]
    assert "balance=0.0000->0.12345678 " in messages[3]
    assert "amount=0.000000000001 " in messages[4]
    assert "balance=0.0000->0.000000000001 " in messages[4]
    assert all(record.levelno == logging.INFO for record in records)
    payload = json.loads(JsonFormatter().format(records[0]))
    assert payload["action"] == "BUY" and payload["result"] == "OK"
    assert payload["level"] == "INFO" and "timestamp" in payload


def test_register_login_and_session():
    with temporary_workdir():
        user = usecases.register("alice", "1234")
        assert user.user_id == 1
        stored = json.loads(Path("data/users.json").read_text(encoding="utf-8"))
        assert stored[0]["username"] == "alice"
        assert stored[0]["hashed_password"] != "1234" and stored[0]["salt"]
        assert "T" in stored[0]["registration_date"]
        portfolios = json.loads(
            Path("data/portfolios.json").read_text(encoding="utf-8")
        )
        assert portfolios == [{"user_id": 1, "wallets": {}}]
        assert_raises(AuthError, usecases.register, "alice", "1234")
        assert_raises(ValueError, usecases.register, "bob", "123")
        assert usecases.register("bob", "abcd").user_id == 2
        assert_raises(AuthError, usecases.login, "carol", "1234")
        assert_raises(AuthError, usecases.login, "alice", "0000")
        assert_raises(AuthError, usecases.require_login)
        assert usecases.login(" alice ", "1234").user_id == 1
        assert usecases.require_login().username == "alice"
        usecases.logout()
        assert usecases.current_user() is None
        usecases.login("bob", "abcd")
        Path("data/users.json").write_text("[]", encoding="utf-8")
        assert usecases.current_user() is None
        assert not Path("data/session.json").exists()


def test_buy_sell_and_portfolio():
    with temporary_workdir():
        user = usecases.register("alice", "1234")
        write_rates({"BTC_USD": 60000.0, "EUR_USD": 1.1})
        patcher, build = offline_updater()
        try:
            assert_raises(
                InsufficientFundsError, usecases.buy, user.user_id, "BTC", 0.05
            )
            usecases.deposit(user.user_id, 3370)
            result = usecases.buy(user.user_id, "btc", 0.05)
            assert (result.old_balance, result.new_balance) == (0.0, 0.05)
            assert result.rate == 60000.0 and result.value == 3000.0
            assert (result.old_usd_balance, result.new_usd_balance) == (3370, 370)
            assert not result.is_stale
            usecases.buy(user.user_id, "EUR", 200)
            usd = usecases.buy(user.user_id, "USD", 150)
            assert (usd.old_balance, usd.new_balance) == (150, 150)
            assert_raises(ValueError, usecases.buy, user.user_id, "BTC", 0)
            assert_raises(CurrencyNotFoundError, usecases.buy, user.user_id, "ABC", 1)
            assert_raises(ApiRequestError, usecases.buy, user.user_id, "SOL", 1)
            assert build.call_count == 1
            sold = usecases.sell(user.user_id, "BTC", 0.01)
            assert sold.new_balance == 0.04 and sold.value == 600.0
            assert (sold.old_usd_balance, sold.new_usd_balance) == (150, 750)
            assert_raises(InsufficientFundsError, usecases.sell, user.user_id, "BTC", 1)
            assert_raises(WalletNotFoundError, usecases.sell, user.user_id, "ETH", 1)
            view = usecases.show_portfolio(user.user_id)
            assert {row.code for row in view.rows} == {"BTC", "EUR", "USD"}
            assert abs(view.total - 3370) < 1e-6
            assert not view.is_stale and view.updated_at
            eur = usecases.show_portfolio(user.user_id, "eur")
            assert eur.base == "EUR" and abs(eur.total - view.total / 1.1) < 1e-6
            assert_raises(
                CurrencyNotFoundError, usecases.show_portfolio, user.user_id, "ABC"
            )
            assert build.call_count == 1
        finally:
            patcher.stop()
        saved = json.loads(Path("data/portfolios.json").read_text(encoding="utf-8"))
        assert saved[0]["wallets"]["BTC"]["balance"] == 0.04
        assert "SOL" not in saved[0]["wallets"]


def test_get_rate_ttl_and_refresh():
    with temporary_workdir():
        write_rates({"BTC_USD": 50000.0, "EUR_USD": 1.25})
        info = usecases.get_rate("usd", "btc")
        assert info.rate == 1 / 50000 and abs(info.reverse_rate - 50000.0) < 1e-6
        assert not info.is_stale and info.updated_at
        assert usecases.get_rate("USD", "USD").rate == 1.0
        assert_raises(CurrencyNotFoundError, usecases.get_rate, "USD", "XYZ")

        notices = []
        usecases.on_refresh = lambda: notices.append(1)
        write_rates({"BTC_USD": 50000.0, "EUR_USD": 1.25}, age_seconds=3600)
        patcher, build = offline_updater()
        try:
            assert_raises(ApiRequestError, usecases.get_rate, "BTC", "USD")
            assert build.call_count == 1 and notices == [1]
            error = assert_raises(ApiRequestError, usecases.get_rate, "SOL", "USD")
            assert "CoinGecko: Network error" in str(error)
            assert_raises(ApiRequestError, usecases.get_rate, "BTC", "USD")
            assert build.call_count == 1
        finally:
            patcher.stop()
            usecases.on_refresh = None

        usecases.reset_rates_cache()

        def fake_update():
            write_rates({"BTC_USD": 70000.0, "EUR_USD": 1.2})
            return UpdateReport(total=2, last_refresh="now")

        with patch.object(usecases, "build_updater") as build:
            build.return_value.run_update.side_effect = fake_update
            fresh = usecases.get_rate("BTC", "USD")
            assert fresh.rate == 70000.0 and not fresh.is_stale

        write_rates({"EUR_USD": 1.2})
        snapshot = utils.read_json("data/rates.json")
        snapshot["pairs"]["BTC_USD"] = {
            "rate": 1.0,
            "updated_at": "2020-01-01T00:00:00Z",
            "source": "old",
        }
        utils.write_json_atomic("data/rates.json", snapshot)
        usecases.reset_rates_cache()
        patcher, build = offline_updater()
        try:
            assert not usecases.get_rate("EUR", "USD").is_stale
            assert build.call_count == 0
            assert_raises(ApiRequestError, usecases.get_rate, "BTC", "USD")
            assert build.call_count == 1
        finally:
            patcher.stop()


def test_list_rates_filters():
    with temporary_workdir():
        assert usecases.list_rates() == ([], None)
        write_rates(
            {"BTC_USD": 60000.0, "ETH_USD": 3000.0, "SOL_USD": 100.0, "EUR_USD": 1.1}
        )
        rows, _ = usecases.list_rates()
        assert [row.pair for row in rows] == [
            "BTC_USD",
            "ETH_USD",
            "EUR_USD",
            "SOL_USD",
        ]
        rows, _ = usecases.list_rates(top=2)
        assert [row.pair for row in rows] == ["BTC_USD", "ETH_USD"]
        rows, _ = usecases.list_rates(currency="eur")
        assert [row.pair for row in rows] == ["EUR_USD"]
        assert_raises(ValueError, usecases.list_rates, currency="ABC")
        assert_raises(ValueError, usecases.list_rates, currency="EUR", top=1)
        assert_raises(ValueError, usecases.list_rates, top=0)
        rows, _ = usecases.list_rates(base="EUR")
        pairs = {row.pair: row.rate for row in rows}
        assert abs(pairs["USD_EUR"] - 1 / 1.1) < 1e-9
        assert abs(pairs["BTC_EUR"] - 60000 / 1.1) < 1e-6
        assert "EUR_EUR" not in pairs


def test_api_clients():
    payload = {
        "bitcoin": {"usd": 59337.21},
        "ethereum": {"usd": 3720.0},
        "solana": {"usd": 145.12},
        "litecoin": {"usd": 70},
        "dogecoin": {"usd": 0.1},
    }
    client = CoinGeckoClient(PARSER_CONFIG)
    with patch(REQUESTS_GET, return_value=FakeResponse(payload)) as get:
        rates = client.fetch_rates()
        assert rates["BTC_USD"] == 59337.21 and len(rates) == 5
        assert get.call_args.kwargs["params"] == {
            "ids": "bitcoin,ethereum,solana,litecoin,dogecoin",
            "vs_currencies": "usd",
        }
        assert get.call_args.kwargs["timeout"] == 10
    with patch(REQUESTS_GET, return_value=FakeResponse({}, status_code=429)):
        error = assert_raises(ApiRequestError, client.fetch_rates)
        assert "429" in error.reason
    with patch(REQUESTS_GET, side_effect=requests.exceptions.ConnectionError("x")):
        error = assert_raises(ApiRequestError, client.fetch_rates)
        assert error.reason == "сетевая ошибка (ConnectionError)"
    with patch(REQUESTS_GET, side_effect=requests.exceptions.Timeout()):
        error = assert_raises(ApiRequestError, client.fetch_rates)
        assert "ожидания" in error.reason
    with patch(REQUESTS_GET, return_value=FakeResponse(ValueError("not json"))):
        assert_raises(ApiRequestError, client.fetch_rates)
    with patch(REQUESTS_GET, return_value=FakeResponse({"bitcoin": {}})):
        assert_raises(ApiRequestError, client.fetch_rates)

    fiat = {
        "result": "success",
        "conversion_rates": {
            "EUR": 0.927,
            "GBP": 0.809,
            "RUB": 98.45,
            "JPY": 150.0,
            "CNY": 7.2,
        },
    }
    with patch(REQUESTS_GET, return_value=FakeResponse(fiat)) as get:
        rates = ExchangeRateApiClient(PARSER_CONFIG).fetch_rates()
        assert abs(rates["EUR_USD"] - 1 / 0.927) < 1e-8 and len(rates) == 5
        assert get.call_args.args[0] == (
            "https://v6.exchangerate-api.com/v6/key/latest/USD"
        )
    bad_key = {"result": "error", "error-type": "invalid-key"}
    with patch(REQUESTS_GET, return_value=FakeResponse(bad_key)):
        error = assert_raises(
            ApiRequestError, ExchangeRateApiClient(PARSER_CONFIG).fetch_rates
        )
        assert "ключ" in error.reason
    open_config = ParserConfig(
        EXCHANGERATE_API_KEY=None, RATES_FILE_PATH="r.json", HISTORY_FILE_PATH="h.json"
    )
    open_payload = {"result": "success", "rates": {"EUR": 0.9}}
    with patch(REQUESTS_GET, return_value=FakeResponse(open_payload)) as get:
        rates = ExchangeRateApiClient(open_config).fetch_rates()
        assert list(rates) == ["EUR_USD"]
        assert get.call_args.args[0] == "https://open.er-api.com/v6/latest/USD"


def test_updater_and_storage():
    with temporary_workdir():
        storage = RatesStorage("data/rates.json", "data/exchange_rates.json")
        good = FakeClient({"BTC_USD": 60000.0, "ETH_USD": 3000.0})
        bad = FakeClient(error="Network error")
        crashed = FakeClient(error=KeyError("DOGE"))
        report = RatesUpdater([bad, crashed, good], storage).run_update()
        assert report.total == 2
        assert report.errors["Fake"] in ("Network error", "'DOGE'")
        snapshot = storage.load_snapshot()
        assert snapshot["pairs"]["BTC_USD"]["rate"] == 60000.0
        assert snapshot["last_refresh"] == report.last_refresh
        history = storage.load_history()
        assert len(history) == 2
        stamp = history[0]["timestamp"]
        assert history[0]["id"] == f"BTC_USD_{stamp}"
        assert stamp == snapshot["pairs"]["BTC_USD"]["updated_at"]
        assert utils.parse_iso(report.last_refresh) >= utils.parse_iso(stamp)
        assert history[0]["meta"]["status_code"] == 200
        assert storage.append_history(history) == 0
        old = {"rate": 1.0, "updated_at": "2020-01-01T00:00:00Z", "source": "old"}
        storage.save_snapshot({"BTC_USD": old}, "2020-01-01T00:00:00Z")
        assert storage.load_snapshot()["pairs"]["BTC_USD"]["rate"] == 60000.0
        report = RatesUpdater([bad], storage).run_update()
        assert report.total == 0
        assert storage.load_snapshot()["pairs"]["BTC_USD"]["rate"] == 60000.0
        assert not list(Path("data").glob("*.tmp"))
        Path("data/exchange_rates.json").write_text("{broken", encoding="utf-8")
        assert RatesUpdater([good], storage).run_update().total == 2
        assert len(storage.load_history()) == 2
        legacy = {
            "EUR_USD": {"rate": 1.07, "updated_at": "2025-10-09T10:30:00"},
            "last_refresh": "2025-10-09T10:35:00",
        }
        utils.write_json_atomic("data/rates.json", legacy)
        assert storage.load_snapshot()["pairs"] == {"EUR_USD": legacy["EUR_USD"]}
        utils.write_json_atomic("data/rates.json", {"pairs": {"BTC_USD": 5}})
        assert storage.load_snapshot()["pairs"] == {}


def test_cli_session():
    with temporary_workdir():
        write_rates({"BTC_USD": 59300.0, "EUR_USD": 1.07})
        patcher, build = offline_updater()
        try:
            assert "Сначала выполните login" in run_cli("show-portfolio")
            out = run_cli("register", "--username", "alice", "--password", "1234")
            assert out.strip() == (
                "Пользователь 'alice' зарегистрирован (id=1). "
                "Войдите: login --username alice --password ****"
            )
            out = run_cli("register", "--username", "alice", "--password", "1234")
            assert "уже занято" in out
            out = run_cli("register", "--username", "bob", "--password", "1")
            assert "не короче 4" in out
            out = run_cli("login", "--username", "alice", "--password", "0")
            assert "Неверный пароль" in out
            out = run_cli("login", "--username", "zed", "--password", "1234")
            assert "не найден" in out
            out = run_cli("login", "--username", "alice", "--password", "1234")
            assert out.strip() == "Вы вошли как 'alice'"
            assert "пока нет кошельков" in run_cli("show-portfolio")
            out = run_cli("buy", "--currency", "BTC", "--amount", "0.05")
            assert "доступно 0.00 USD, требуется 2965.00 USD" in out
            out = run_cli("deposit", "--amount", "3329")
            assert "Счёт пополнен: 3329.00 USD" in out
            out = run_cli("buy", "--currency", "BTC", "--amount", "0.05")
            assert out.splitlines() == [
                "Покупка выполнена: 0.0500 BTC по курсу 59300.00 USD/BTC",
                "Изменения в портфеле:",
                "- BTC: было 0.0000 → стало 0.0500",
                "- USD: было 3329.00 → стало 364.00",
                "Стоимость покупки: 2,965.00 USD",
            ]
            run_cli("buy", "--currency", "EUR", "--amount", "200")
            assert run_cli("show-portfolio").splitlines() == [
                "Портфель пользователя 'alice' (база: USD):",
                "- USD: 150.00  → 150.00 USD",
                "- BTC: 0.0500  → 2965.00 USD",
                "- EUR: 200.00  → 214.00 USD",
                "---------------------------------",
                "ИТОГО: 3,329.00 USD",
            ]
            out = run_cli("sell", "--currency", "BTC", "--amount", "0.01")
            assert "Продажа выполнена: 0.0100 BTC по курсу 59300.00 USD/BTC" in out
            assert "- BTC: было 0.0500 → стало 0.0400" in out
            assert "Выручка: 593.00 USD" in out
            assert "- USD: было 150.00 → стало 743.00" in out
            for command in ("buy", "sell"):
                out = run_cli(command, "--currency", "USD", "--amount", "1")
                assert out.count("- USD:") == 1
                assert "- USD: было 743.00 → стало 743.00" in out
            out = run_cli("sell", "--currency", "BTC", "--amount", "0.05")
            assert "доступно 0.0400 BTC, требуется 0.0500 BTC" in out
            out = run_cli("sell", "--currency", "ETH", "--amount", "1")
            assert "У вас нет кошелька 'ETH'" in out
            out = run_cli("buy", "--currency", "BTC", "--amount", "-1")
            assert "'amount' должен быть положительным числом" in out
            out = run_cli("buy", "--currency", "ABC", "--amount", "1")
            assert "Неизвестная валюта 'ABC'" in out and "Поддерживаемые коды" in out
            out = run_cli("show-portfolio", "--base", "ABC")
            assert "Неизвестная базовая валюта 'ABC'" in out
            out = run_cli("get-rate", "--from", "USD", "--to", "BTC")
            assert out.startswith("Курс USD→BTC: 0.00001686 (обновлено: ")
            assert "Обратный курс BTC→USD: 59300.00" in out
            out = run_cli("show-rates", "--top", "1")
            assert "BTC_USD" in out and "EUR_USD" not in out
            out = run_cli("show-rates", "--currency", "ABC")
            assert "Курс для 'ABC' не найден в кеше." in out
            out = run_cli("show-rates", "--currency", "EUR", "--top", "1")
            assert "нет курсов криптовалют" in out
            out = run_cli("buy", "--currency", "BTC")
            assert "Ошибка: не указаны обязательные аргументы: --amount" in out
            assert "usage" in run_cli("help", "buy")
            assert build.call_count == 0
        finally:
            patcher.stop()

        Path("data/rates.json").unlink()
        usecases.reset_rates_cache()
        patcher, build = offline_updater()
        try:
            out = run_cli("get-rate", "--from", "USD", "--to", "BTC")
            assert "Курс USD→BTC недоступен. Повторите попытку позже." in out
            assert "CoinGecko: Network error" in out
            assert "Локальный кеш курсов пуст" in run_cli("show-rates")
            assert "Update completed with errors" in run_cli("update-rates")
            out = run_cli("buy", "--currency", "BTC", "--amount", "1")
            assert "Не удалось получить курс для BTC→USD" in out
            assert build.call_count == 2
        finally:
            patcher.stop()


def test_recovery_and_identity_rates():
    with temporary_workdir():
        write_rates({"BTC_USD": 60000.0})
        assert_raises(ValueError, usecases.list_rates, base="JPY")
        out = run_cli("show-rates", "--base", "JPY")
        assert "Курс для 'JPY' не найден в кеше." in out
        Path("data/rates.json").write_text("{broken", encoding="utf-8")
        usecases.reset_rates_cache()
        assert usecases.load_rates() == {"pairs": {}, "last_refresh": None}
        storage = RatesStorage("data/rates.json", "data/exchange_rates.json")
        good = FakeClient({"BTC_USD": 60000.0})
        assert RatesUpdater([good], storage).run_update().total == 1
        assert storage.load_snapshot()["pairs"]["BTC_USD"]["rate"] == 60000.0
        usecases.reset_rates_cache()
        with patch.object(usecases, "build_updater") as build:
            build.return_value.run_update.return_value = UpdateReport(
                total=1, last_refresh="now"
            )
            usecases.refresh_rates("coingecko")
            assert_raises(ApiRequestError, usecases.get_rate, "EUR", "USD")
            assert build.call_count == 2
        Path(".env").write_text("=x\nVALID_TEST_KEY=1\n", encoding="utf-8")
        os.environ.pop("VALID_TEST_KEY", None)
        try:
            load_env_file()
            assert os.environ["VALID_TEST_KEY"] == "1"
        finally:
            os.environ.pop("VALID_TEST_KEY", None)
        assert usecases.get_rate("USD", "USD").updated_at is None
        out = run_cli("get-rate", "--from", "USD", "--to", "USD")
        assert out.splitlines()[0] == "Курс USD→USD: 1.0000"
    assert utils.validate_amount(1e13) == 1e13


def main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
