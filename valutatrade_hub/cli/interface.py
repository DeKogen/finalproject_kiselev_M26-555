"""Консольный интерфейс: разбор команд и вывод результатов."""

import argparse
import logging
import shlex
import sys

from prettytable import PrettyTable

from valutatrade_hub.core import usecases, utils
from valutatrade_hub.core.currencies import supported_codes
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    RateNotFoundError,
    ValutaTradeError,
)
from valutatrade_hub.logging_config import (
    ACTIONS_LOGGER,
    parser_console,
    setup_logging,
)

PROMPT = "> "
BANNER = "ValutaTrade Hub. Введите help для списка команд, exit — для выхода."
SEPARATOR = "-" * 33
STALE_NOTE = "Внимание: курс устарел, выполните update-rates."
RATE_HINT = "Повторите попытку позже или проверьте сеть."
REFRESH_NOTE = "Кеш курсов устарел, обновляю..."
INTERRUPTED = "Команда прервана."
ARGPARSE_MESSAGES = {
    "the following arguments are required:": "не указаны обязательные аргументы:",
    "unrecognized arguments:": "лишние аргументы:",
    "invalid choice:": "недопустимое значение:",
    "invalid int value:": "ожидается целое число:",
    "expected one argument": "нужно указать значение",
    "(choose from ": "(варианты: ",
    "argument ": "аргумент ",
}
logger = logging.getLogger(ACTIONS_LOGGER)


class CommandError(Exception):
    """Ошибка разбора аргументов команды."""


class CommandParser(argparse.ArgumentParser):
    """Ошибки аргументов на русском без завершения приложения."""

    def error(self, message):
        """Передать ошибку аргументов в обработчик CLI."""
        for english, russian in ARGPARSE_MESSAGES.items():
            message = message.replace(english, russian)
        raise CommandError(message)


def cmd_register(args):
    """Зарегистрировать пользователя."""
    user = usecases.register(args.username, args.password)
    name = shlex.quote(user.username)
    print(
        f"Пользователь '{user.username}' зарегистрирован (id={user.user_id}). "
        f"Войдите: login --username {name} --password ****"
    )


def cmd_login(args):
    """Открыть пользовательскую сессию."""
    user = usecases.login(args.username, args.password)
    print(f"Вы вошли как '{user.username}'")


def cmd_logout(args):
    """Завершить сессию."""
    usecases.logout()
    print("Вы вышли из системы")


def cmd_whoami(args):
    """Показать пользователя текущей сессии."""
    info = usecases.require_login().get_user_info()
    print(
        f"Вы вошли как '{info['username']}' (id={info['user_id']}, "
        f"зарегистрирован {info['registration_date']})"
    )


def cmd_show_portfolio(args):
    """Показать кошельки и общую стоимость."""
    user = usecases.require_login()
    try:
        view = usecases.show_portfolio(user.user_id, args.base)
    except CurrencyNotFoundError as error:
        print(f"Неизвестная базовая валюта '{error.code}'")
        return 1
    if not view.rows:
        print(
            f"У пользователя '{view.username}' пока нет кошельков. "
            "Пополните USD командой deposit --amount 10000."
        )
        return
    print(f"Портфель пользователя '{view.username}' (база: {view.base}):")
    for row in view.rows:
        balance = utils.format_amount(row.balance, row.code)
        value = utils.format_amount(row.value, view.base)
        print(f"- {row.code}: {balance}  → {value} {view.base}")
    print(SEPARATOR)
    print(f"ИТОГО: {utils.format_money(view.total, view.base)} {view.base}")
    if view.is_stale:
        print(
            f"Курсы устарели (обновлено: {utils.format_moment(view.updated_at)}). "
            "Выполните update-rates."
        )


def _print_trade(result, title, value_title):
    code = result.currency_code
    print(
        f"{title}: {utils.format_amount(result.amount, code)} {code} "
        f"по курсу {utils.format_rate(result.rate)} {result.base}/{code}"
    )
    print("Изменения в портфеле:")
    print(
        f"- {code}: было {utils.format_amount(result.old_balance, code)} "
        f"→ стало {utils.format_amount(result.new_balance, code)}"
    )
    if result.old_usd_balance is not None and code != "USD":
        print(
            f"- USD: было {utils.format_amount(result.old_usd_balance, 'USD')} "
            f"→ стало {utils.format_amount(result.new_usd_balance, 'USD')}"
        )
    value = utils.format_money(result.value, result.base)
    print(f"{value_title}: {value} {result.base}")
    if result.is_stale:
        print(STALE_NOTE)


def _trade(args, operation, title, value_title):
    user = usecases.require_login()
    amount = utils.parse_amount(args.amount)
    try:
        result = operation(user.user_id, args.currency, amount)
    except (ApiRequestError, RateNotFoundError) as error:
        code = utils.normalize_code(args.currency)
        print(f"Не удалось получить курс для {code}→USD")
        print(error)
        print(RATE_HINT)
        return 1
    _print_trade(result, title, value_title)
    return 0


def cmd_buy(args):
    """Купить валюту за USD."""
    return _trade(args, usecases.buy, "Покупка выполнена", "Стоимость покупки")


def cmd_sell(args):
    """Продать валюту за USD."""
    return _trade(args, usecases.sell, "Продажа выполнена", "Выручка")


def cmd_deposit(args):
    """Пополнить виртуальный счёт USD."""
    user = usecases.require_login()
    result = usecases.deposit(user.user_id, utils.parse_amount(args.amount))
    print(f"Счёт пополнен: {utils.format_amount(result.amount, 'USD')} USD")
    print("Изменения в портфеле:")
    print(
        f"- USD: было {utils.format_amount(result.old_balance, 'USD')} "
        f"→ стало {utils.format_amount(result.new_balance, 'USD')}"
    )


def _rate_unavailable(args):
    pair = f"{args.from_code.upper()}→{args.to_code.upper()}"
    print(f"Курс {pair} недоступен. Повторите попытку позже.")


def cmd_get_rate(args):
    """Показать прямой и обратный курс."""
    try:
        info = usecases.get_rate(args.from_code, args.to_code)
    except ApiRequestError as error:
        print(error)
        _rate_unavailable(args)
        return 1
    except RateNotFoundError:
        _rate_unavailable(args)
        return 1
    line = f"Курс {info.from_code}→{info.to_code}: {utils.format_rate(info.rate)}"
    if info.updated_at:
        line += f" (обновлено: {utils.format_moment(info.updated_at)})"
    print(line)
    reverse = utils.format_rate(info.reverse_rate)
    print(f"Обратный курс {info.to_code}→{info.from_code}: {reverse}")
    if info.is_stale:
        print(STALE_NOTE)


def cmd_update_rates(args):
    """Обновить курсы и показать результат по источникам."""
    with parser_console():
        report = usecases.refresh_rates(args.source)
    refreshed = utils.format_moment(report.last_refresh, "T")
    if report.errors:
        print("Update completed with errors. Check logs/parser.log for details.")
        if report.total:
            print(f"Rates updated: {report.total}. Last refresh: {refreshed}")
        return 1
    print(
        f"Update successful. Total rates updated: {report.total}. "
        f"Last refresh: {refreshed}"
    )


def cmd_show_rates(args):
    """Показать таблицу курсов из кеша."""
    rows, last_refresh = usecases.list_rates(args.currency, args.top, args.base)
    if not rows:
        print(
            "Локальный кеш курсов пуст. Выполните 'update-rates', "
            "чтобы загрузить данные."
        )
        return 1
    print(f"Rates from cache (updated at {utils.format_moment(last_refresh, 'T')}):")
    table = PrettyTable(["Pair", "Rate", "Updated at", "Source"])
    table.align["Pair"] = "l"
    table.align["Rate"] = "r"
    for row in rows:
        table.add_row(
            [
                row.pair,
                utils.format_rate(row.rate),
                utils.format_moment(row.updated_at, "T"),
                row.source or "-",
            ]
        )
    print(table)
    if any(row.is_stale for row in rows):
        print(STALE_NOTE)


def cmd_help(args):
    """Показать справку по команде."""
    if args.topic in COMMANDS:
        COMMANDS[args.topic].print_help()
    elif args.topic:
        print(f"Неизвестная команда '{args.topic}'. Доступные: {', '.join(COMMANDS)}")
        return 1
    else:
        PARSER.print_help()


def build_parser():
    """Главный парсер и словарь подпарсеров по имени команды."""
    parser = CommandParser(
        prog="valutatrade",
        description="ValutaTrade Hub — виртуальный валютный кошелёк.",
        epilog="В интерактивном режиме: help <команда> — справка, exit — выход.",
    )
    commands = parser.add_subparsers(
        dest="command", title="Команды", metavar="<команда>"
    )

    def command(name, help_text):
        return commands.add_parser(name, help=help_text, description=help_text)

    sub = command("register", "Зарегистрировать пользователя и создать портфель.")
    sub.add_argument("--username", required=True, help="имя пользователя")
    sub.add_argument("--password", required=True, help="пароль не короче 4 символов")
    sub.set_defaults(handler=cmd_register)

    sub = command("login", "Войти в систему и запомнить сессию.")
    sub.add_argument("--username", required=True, help="имя пользователя")
    sub.add_argument("--password", required=True, help="пароль")
    sub.set_defaults(handler=cmd_login)

    sub = command("logout", "Завершить сессию.")
    sub.set_defaults(handler=cmd_logout)

    sub = command("whoami", "Показать текущего пользователя.")
    sub.set_defaults(handler=cmd_whoami)

    sub = command("show-portfolio", "Показать кошельки и их стоимость.")
    sub.add_argument("--base", help="базовая валюта (по умолчанию USD)")
    sub.set_defaults(handler=cmd_show_portfolio)

    sub = command("deposit", "Пополнить виртуальный счёт USD.")
    sub.add_argument("--amount", required=True, help="сумма в USD")
    sub.set_defaults(handler=cmd_deposit)

    sub = command("buy", "Купить валюту; кошелёк создаётся при первой покупке.")
    sub.add_argument("--currency", required=True, help="код валюты, например BTC")
    sub.add_argument("--amount", required=True, help="количество в единицах валюты")
    sub.set_defaults(handler=cmd_buy)

    sub = command("sell", "Продать валюту с существующего кошелька.")
    sub.add_argument("--currency", required=True, help="код валюты")
    sub.add_argument("--amount", required=True, help="количество в единицах валюты")
    sub.set_defaults(handler=cmd_sell)

    sub = command("get-rate", "Показать курс одной валюты к другой.")
    sub.add_argument("--from", dest="from_code", required=True, help="исходная")
    sub.add_argument("--to", dest="to_code", required=True, help="целевая")
    sub.set_defaults(handler=cmd_get_rate)

    sub = command("update-rates", "Обновить курсы из внешних API.")
    sub.add_argument(
        "--source", choices=usecases.RATE_SOURCES, help="только один источник"
    )
    sub.set_defaults(handler=cmd_update_rates)

    sub = command("show-rates", "Показать курсы из локального кеша.")
    sub.add_argument("--currency", help="только указанная валюта")
    sub.add_argument("--top", type=int, help="N самых дорогих криптовалют")
    sub.add_argument("--base", help="пересчитать относительно другой базы")
    sub.set_defaults(handler=cmd_show_rates)

    sub = command("help", "Справка по командам.")
    sub.add_argument("topic", nargs="?", help="имя команды")
    sub.set_defaults(handler=cmd_help)
    return parser, commands.choices


PARSER, COMMANDS = build_parser()


def run_command(argv):
    """Выполнить команду; вернуть 0 при успехе, 1 при ошибке."""
    try:
        args = PARSER.parse_args(argv)
    except CommandError as error:
        print(f"Ошибка: {error}")
        print("Введите help для списка команд.")
        return 1
    except SystemExit as error:
        return error.code
    if getattr(args, "handler", None) is None:
        PARSER.print_help()
        return 0
    try:
        return args.handler(args) or 0
    except InsufficientFundsError as error:
        print(error)
    except CurrencyNotFoundError as error:
        print(error)
        print(f"Поддерживаемые коды: {', '.join(supported_codes())}")
    except ApiRequestError as error:
        print(error)
        print(RATE_HINT)
    except RateNotFoundError as error:
        print(error)
        print("Выполните update-rates, чтобы загрузить курсы.")
    except ValutaTradeError as error:
        print(error)
    except (ValueError, TypeError) as error:
        print(error)
    except OSError as error:
        logger.error("Storage error in command %s: %s", args.command, error)
        print(f"Не удалось сохранить или прочитать данные: {error}")
    except Exception as error:
        logger.exception("Unexpected error in command %s", args.command)
        print(f"Непредвиденная ошибка: {error}")
    return 1


def repl():
    """Интерактивный режим: одна команда на строку."""
    print(BANNER)
    while True:
        try:
            line = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            tokens = shlex.split(line)
        except ValueError:
            print("Ошибка разбора команды: проверьте кавычки.")
            continue
        if not tokens:
            continue
        if tokens[0] in ("exit", "quit"):
            break
        try:
            run_command(tokens)
        except KeyboardInterrupt:
            print(f"\n{INTERRUPTED}")
    print("До свидания!")


def main(argv=None):
    """Точка входа: команда из аргументов или интерактивный режим."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if (
            stream is not None
            and hasattr(stream, "reconfigure")
            and not stream.isatty()
        ):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging()
    usecases.on_refresh = lambda: print(REFRESH_NOTE)
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        if argv:
            return run_command(argv)
        else:
            repl()
    except KeyboardInterrupt:
        print(f"\n{INTERRUPTED}")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
