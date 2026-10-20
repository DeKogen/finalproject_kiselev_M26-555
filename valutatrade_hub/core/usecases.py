"""Бизнес-логика: пользователи, сессия, портфель, сделки и курсы."""

import time
from dataclasses import dataclass
from decimal import Decimal, localcontext

from valutatrade_hub.core import utils
from valutatrade_hub.core.currencies import is_crypto
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    AuthError,
    InsufficientFundsError,
    RateNotFoundError,
)
from valutatrade_hub.core.models import DECIMAL_PRECISION, Portfolio, User, Wallet
from valutatrade_hub.decorators import cached, log_action
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.storage import RatesStorage
from valutatrade_hub.parser_service.updater import SOURCES, build_updater

RATES_READ_CACHE_SECONDS = 5
REFRESH_RETRY_SECONDS = 60
RATE_SOURCES = tuple(SOURCES)

# CLI задаёт функцию для сообщения перед автообновлением курсов.
on_refresh = None
_last_attempt = {"at": None, "errors": {}}


@dataclass
class RateInfo:
    """Курс пары валют и сведения о его свежести."""

    from_code: str
    to_code: str
    rate: float
    updated_at: str | None
    is_stale: bool

    @property
    def reverse_rate(self):
        return 1 / self.rate


@dataclass
class TradeResult:
    """Итог покупки или продажи для вывода и журнала."""

    username: str
    currency_code: str
    amount: float
    rate: float
    base: str
    old_balance: float
    new_balance: float
    is_stale: bool
    old_usd_balance: float | None = None
    new_usd_balance: float | None = None
    settlement_value: float | None = None

    @property
    def value(self):
        """Сумма расчёта по сделке в базовой валюте."""
        if self.settlement_value is not None:
            return self.settlement_value
        return self.amount * self.rate


@dataclass
class PortfolioRow:
    """Баланс кошелька и его стоимость в базовой валюте."""

    code: str
    balance: float
    value: float


@dataclass
class PortfolioView:
    """Портфель в пересчёте на базовую валюту."""

    username: str
    base: str
    rows: list
    total: float
    updated_at: str | None
    is_stale: bool


@dataclass
class RateRow:
    """Курс из кеша с источниками и сроком актуальности."""

    pair: str
    rate: float
    updated_at: str | None
    source: str | None
    is_stale: bool = False


def _settings():
    return SettingsLoader()


def _db():
    return DatabaseManager()


def base_currency():
    """Базовая валюта для оценок из настроек."""
    return _settings().get("default_base_currency")


def rates_ttl():
    """Срок годности курсов в секундах из настроек."""
    return int(_settings().get("rates_ttl_seconds"))


def load_users():
    """Все пользователи из users.json."""
    return [User.from_dict(item) for item in _db().read("users", [])]


def save_users(users):
    """Перезаписать users.json."""
    _db().write("users", [user.to_dict() for user in users])


def find_user(username):
    """Пользователь по имени или None."""
    for user in load_users():
        if user.username == username:
            return user
    return None


def get_user(user_id):
    """Пользователь по id или AuthError."""
    for user in load_users():
        if user.user_id == user_id:
            return user
    raise AuthError(f"Пользователь с id={user_id} не найден")


@log_action("REGISTER")
def register(username, password):
    """Создать пользователя и пустой портфель."""
    username = utils.validate_username(username)
    utils.validate_password(password)
    with _db().transaction():
        users = load_users()
        if any(user.username == username for user in users):
            raise AuthError(f"Имя пользователя '{username}' уже занято")
        portfolios = _db().read("portfolios", [])
        if not isinstance(portfolios, list):
            raise ValueError("Повреждён файл portfolios.json: ожидается список")
        user_id = max((user.user_id for user in users), default=0) + 1
        user = User.create(user_id, username, password)
        users.append(user)
        portfolios.append(Portfolio(user).to_dict())
        save_users(users)
        _db().write("portfolios", portfolios)
    return user


@log_action("LOGIN")
def login(username, password):
    """Проверить пароль и запомнить сессию в data/session.json."""
    username = utils.validate_username(username)
    user = find_user(username)
    if user is None:
        raise AuthError(f"Пользователь '{username}' не найден")
    if not user.verify_password(password):
        raise AuthError("Неверный пароль")
    _db().write(
        "session",
        {
            "user_id": user.user_id,
            "username": user.username,
            "logged_in_at": utils.to_iso(utils.utc_now()),
        },
    )
    return user


def logout():
    """Удалить файл сессии."""
    _db().delete("session")


def current_user():
    """Пользователь из сессии; сессия удалённого пользователя сбрасывается."""
    session = _db().read("session")
    if not isinstance(session, dict):
        return None
    try:
        return get_user(session.get("user_id"))
    except AuthError:
        logout()
        return None


def require_login():
    """Текущий пользователь или AuthError, если сессии нет."""
    user = current_user()
    if user is None:
        raise AuthError("Сначала выполните login")
    return user


def load_portfolio(user):
    """Портфель пользователя; без записи в файле — пустой."""
    for item in _db().read("portfolios", []):
        if isinstance(item, dict) and item.get("user_id") == user.user_id:
            return Portfolio.from_dict(item, user)
    return Portfolio(user)


def save_portfolio(portfolio):
    """Заменить или добавить запись портфеля в portfolios.json."""
    with _db().transaction():
        portfolios = _db().read("portfolios", [])
        for index, item in enumerate(portfolios):
            if isinstance(item, dict) and item.get("user_id") == portfolio.user_id:
                portfolios[index] = portfolio.to_dict()
                break
        else:
            portfolios.append(portfolio.to_dict())
        _db().write("portfolios", portfolios)


@cached(RATES_READ_CACHE_SECONDS)
def load_rates():
    """Снимок курсов из rates.json: {"pairs": {...}, "last_refresh": ...}."""
    return RatesStorage().load_snapshot()


def reset_rates_cache():
    """Сбросить кэш чтения и память о последней попытке обновления."""
    load_rates.cache_clear()
    _last_attempt.update(at=None, errors={})


def rates_table(snapshot):
    """Только числовые курсы: {"BTC_USD": 59337.21, ...}."""
    table = {}
    for pair, record in snapshot.get("pairs", {}).items():
        rate = record.get("rate") if isinstance(record, dict) else None
        if utils.is_valid_rate(rate):
            table[pair] = float(rate)
    return table


def _pair_record(snapshot, code):
    record = snapshot["pairs"].get(utils.pair_key(code, utils.BASE_CODE), {})
    return record if isinstance(record, dict) else {}


def _needed(codes):
    return {code for code in codes if code != utils.BASE_CODE}


def pairs_are_fresh(snapshot, codes):
    """Есть ли свежие курсы для всех валют из codes (для USD курс не нужен)."""
    ttl = rates_ttl()
    return all(
        utils.is_valid_rate(_pair_record(snapshot, code).get("rate"))
        and utils.is_fresh(_pair_record(snapshot, code).get("updated_at"), ttl)
        for code in _needed(codes)
    )


def oldest_update(snapshot, codes):
    """Самая старая метка updated_at среди нужных пар; None, если пары нет."""
    moments = [
        utils.parse_iso(_pair_record(snapshot, code).get("updated_at"))
        for code in _needed(codes)
    ]
    if not moments or None in moments:
        return None
    return utils.to_iso(min(moments))


def refresh_rates(source=None):
    """Запустить Parser Service и сбросить кэш чтения (команда update-rates)."""
    report = build_updater(source).run_update()
    load_rates.cache_clear()
    at = time.monotonic() if report.errors else None
    _last_attempt.update(at=at, errors=dict(report.errors))
    return report


def _failure_reason():
    errors = _last_attempt["errors"]
    return "; ".join(f"{name}: {reason}" for name, reason in errors.items()) or None


def ensure_rates(codes):
    """Получить свежие курсы, при необходимости обновив кеш.

    После неудачи повторяем запрос не раньше чем через минуту.
    Возвращает снимок и None; без свежих данных бросает ApiRequestError.
    """
    snapshot = load_rates()
    if pairs_are_fresh(snapshot, codes):
        return snapshot, None
    attempted_at = _last_attempt["at"]
    if attempted_at is None or time.monotonic() - attempted_at >= REFRESH_RETRY_SECONDS:
        if on_refresh:
            on_refresh()
        refresh_rates()
        snapshot = load_rates()
        if pairs_are_fresh(snapshot, codes):
            return snapshot, None
    raise ApiRequestError(
        _failure_reason() or "Курсы отсутствуют или устарели. Выполните update-rates."
    )


def get_rate(from_code, to_code):
    """Курс from→to из кеша с учётом TTL."""
    from_code = utils.validate_currency_code(from_code)
    to_code = utils.validate_currency_code(to_code)
    if from_code == to_code:
        return RateInfo(from_code, to_code, 1.0, None, False)
    codes = (from_code, to_code)
    snapshot, _ = ensure_rates(codes)
    rate = utils.find_rate(rates_table(snapshot), from_code, to_code)
    if rate is None:
        raise RateNotFoundError(from_code, to_code)
    return RateInfo(
        from_code,
        to_code,
        rate,
        oldest_update(snapshot, codes),
        not pairs_are_fresh(snapshot, codes),
    )


def list_rates(currency=None, top=None, base=None):
    """Курсы из кеша с фильтрами show-rates; возвращает (строки, last_refresh)."""
    snapshot = load_rates()
    if not snapshot["pairs"]:
        return [], None
    if top is not None and top <= 0:
        raise ValueError("'top' должен быть положительным числом")
    base_code = utils.validate_currency_code(base) if base else utils.BASE_CODE
    table = rates_table(snapshot)
    if utils.find_rate(table, base_code, utils.BASE_CODE) is None:
        raise ValueError(f"Курс для '{base_code}' не найден в кеше.")
    codes = dict.fromkeys(
        [pair.partition("_")[0] for pair in table] + [utils.BASE_CODE]
    )
    rows = []
    for from_code in codes:
        rate = utils.find_rate(table, from_code, base_code)
        if from_code == base_code or rate is None:
            continue
        needed_codes = (from_code, base_code)
        sources = dict.fromkeys(
            _pair_record(snapshot, code).get("source")
            for code in needed_codes
            if code != utils.BASE_CODE
        )
        source = " + ".join(str(value) for value in sources if value) or None
        rows.append(
            RateRow(
                utils.pair_key(from_code, base_code),
                rate,
                oldest_update(snapshot, needed_codes),
                source,
                not pairs_are_fresh(snapshot, needed_codes),
            )
        )
    if currency:
        code = utils.normalize_code(currency)
        rows = [row for row in rows if row.pair.startswith(code + "_")]
        if not rows:
            raise ValueError(f"Курс для '{code}' не найден в кеше.")
    if top:
        rows = [row for row in rows if is_crypto(row.pair.partition("_")[0])]
        rows.sort(key=lambda row: row.rate, reverse=True)
        rows = rows[:top]
        if not rows:
            raise ValueError("В кеше нет курсов криптовалют для вывода.")
    else:
        rows.sort(key=lambda row: row.pair)
    return rows, snapshot.get("last_refresh")


def _prepare_trade(user_id, currency_code, amount, selling=False):
    """Проверить вход и получить курс до блокировки хранилища."""
    user = get_user(user_id)
    code = utils.validate_currency_code(currency_code)
    amount = Wallet.validate_amount(amount)
    if selling:
        wallet = load_portfolio(user).get_wallet(code)
        if amount > wallet.balance:
            raise InsufficientFundsError(
                wallet.balance, amount, code, utils.amount_digits(code)
            )
    rate_info = get_rate(code, utils.BASE_CODE)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        value = Decimal(str(amount)) * Decimal(str(rate_info.rate))
    try:
        value = utils.validate_amount(float(value))
    except ValueError:
        raise ValueError(
            "Стоимость сделки не представима положительным конечным числом"
        ) from None
    return code, amount, rate_info, value


def _trade_result(
    user, wallet, rate_info, amount, old_balance, usd_wallet, old_usd_balance, value
):
    return TradeResult(
        user.username,
        wallet.currency_code,
        amount,
        rate_info.rate,
        rate_info.to_code,
        old_balance,
        wallet.balance,
        rate_info.is_stale,
        old_usd_balance,
        usd_wallet.balance,
        value,
    )


@log_action("DEPOSIT", verbose=True)
def deposit(user_id, amount):
    """Пополнить виртуальный USD-кошелёк."""
    amount = Wallet.validate_amount(amount)
    with _db().transaction():
        user = get_user(user_id)
        portfolio = load_portfolio(user)
        if utils.BASE_CODE not in portfolio.wallets:
            portfolio.add_currency(utils.BASE_CODE)
        wallet = portfolio.get_wallet(utils.BASE_CODE)
        old_balance = wallet.balance
        wallet.deposit(amount)
        save_portfolio(portfolio)
    return TradeResult(
        user.username,
        utils.BASE_CODE,
        amount,
        1.0,
        utils.BASE_CODE,
        old_balance,
        wallet.balance,
        False,
        settlement_value=amount,
    )


@log_action("BUY", verbose=True)
def buy(user_id, currency_code, amount):
    """Купить валюту за USD; новый кошелёк создаётся автоматически."""
    code, amount, rate_info, value = _prepare_trade(user_id, currency_code, amount)
    with _db().transaction():
        user = get_user(user_id)
        portfolio = load_portfolio(user)
        if utils.BASE_CODE not in portfolio.wallets:
            raise InsufficientFundsError(0.0, value, utils.BASE_CODE, 2)
        usd_wallet = portfolio.get_wallet(utils.BASE_CODE)
        old_usd_balance = usd_wallet.balance
        if code == utils.BASE_CODE:
            if value > old_usd_balance:
                raise InsufficientFundsError(old_usd_balance, value, code, 2)
            wallet = usd_wallet
            old_balance = old_usd_balance
        else:
            usd_wallet.withdraw(value)
            if code not in portfolio.wallets:
                portfolio.add_currency(code)
            wallet = portfolio.get_wallet(code)
            old_balance = wallet.balance
            wallet.deposit(amount)
            save_portfolio(portfolio)
    return _trade_result(
        user, wallet, rate_info, amount, old_balance, usd_wallet, old_usd_balance, value
    )


@log_action("SELL", verbose=True)
def sell(user_id, currency_code, amount):
    """Продать валюту и зачислить выручку в USD."""
    code, amount, rate_info, value = _prepare_trade(
        user_id, currency_code, amount, selling=True
    )
    with _db().transaction():
        user = get_user(user_id)
        portfolio = load_portfolio(user)
        wallet = portfolio.get_wallet(code)
        old_balance = wallet.balance
        if code == utils.BASE_CODE:
            if amount > old_balance:
                raise InsufficientFundsError(old_balance, amount, code, 2)
            usd_wallet = wallet
            old_usd_balance = old_balance
        else:
            wallet.withdraw(amount)
            if utils.BASE_CODE not in portfolio.wallets:
                portfolio.add_currency(utils.BASE_CODE)
            usd_wallet = portfolio.get_wallet(utils.BASE_CODE)
            old_usd_balance = usd_wallet.balance
            usd_wallet.deposit(value)
            save_portfolio(portfolio)
    return _trade_result(
        user, wallet, rate_info, amount, old_balance, usd_wallet, old_usd_balance, value
    )


def show_portfolio(user_id, base=None):
    """Кошельки пользователя и их стоимость в базовой валюте."""
    user = get_user(user_id)
    base_code = utils.validate_currency_code(base or base_currency())
    portfolio = load_portfolio(user)
    wallets = portfolio.wallets
    if not wallets:
        return PortfolioView(user.username, base_code, [], 0.0, None, False)
    codes = [
        code for code, wallet in wallets.items() if wallet.balance and code != base_code
    ]
    snapshot = {"pairs": {}}
    if codes:
        codes.append(base_code)
        snapshot, _ = ensure_rates(codes)
    table = rates_table(snapshot)
    rows = []
    for code, wallet in wallets.items():
        value = 0.0
        if wallet.balance:
            value = utils.convert(wallet.balance, code, base_code, table)
        rows.append(PortfolioRow(code, wallet.balance, value))
    total = portfolio.get_total_value(base_code, table)
    return PortfolioView(
        user.username,
        base_code,
        rows,
        total,
        oldest_update(snapshot, codes),
        not pairs_are_fresh(snapshot, codes),
    )
