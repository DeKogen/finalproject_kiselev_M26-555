# ValutaTrade Hub

Учебный проект на Python 3.12: консольный кошелёк для фиатных и криптовалют
с курсами из CoinGecko и ExchangeRate-API. Автор: Киселёв, группа М26-555.
Репозиторий: `finalproject_kiselev_M26-555`, пакет `valutatrade_hub`.

Что умеет:

- регистрация и вход, пароль хранится как SHA-256 от пароля и соли;
- виртуальный портфель: покупка и продажа валют, оценка в базовой валюте;
- курсы из локального кеша `data/rates.json` с TTL и автообновлением;
- Parser Service: два внешних API, история измерений, планировщик;
- журнал операций с ротацией, доменные исключения, синглтоны настроек
  и хранилища.

## Установка и запуск

Нужны Python 3.12+ и [uv](https://docs.astral.sh/uv/). Из корня проекта:

```sh
uv sync
uv run main.py
```

Или через Make: `make install`, `make run`. После `uv sync` доступны команды
`uv run valutatrade` (то же приложение) и `uv run valutatrade-parser`
(обновление курсов по таймеру).

Без аргументов запускается интерактивный режим с приглашением `>`. Команду
можно передать и аргументами: `uv run main.py get-rate --from USD --to BTC`.
Строка разбирается модулем `shlex`, значения с пробелами берутся в кавычки.

Данные лежат в `data/` относительно **текущей рабочей папки**, журналы —
в `logs/`, оттуда же читается `.env`. Запускайте приложение из корня проекта,
чтобы работать с одной и той же базой. Сессия после `login` хранится
в `data/session.json` (в Git не попадает), поэтому команды работают
и по одной, и в интерактивном режиме. При перенаправлении ввода или вывода
в файл используется UTF-8.

## Команды

| Команда | Назначение |
| --- | --- |
| `register --username <имя> --password <пароль>` | Регистрация, пароль не короче 4 символов |
| `login --username <имя> --password <пароль>` | Вход и запись сессии |
| `logout`, `whoami` | Завершить сессию, показать текущего пользователя |
| `show-portfolio [--base USD]` | Кошельки и их стоимость в базовой валюте |
| `buy --currency BTC --amount 0.05` | Купить валюту, кошелёк создаётся сам |
| `sell --currency BTC --amount 0.01` | Продать валюту с существующего кошелька |
| `get-rate --from USD --to BTC` | Курс пары с учётом TTL кеша |
| `update-rates [--source coingecko\|exchangerate]` | Запустить Parser Service |
| `show-rates [--currency BTC] [--top 3] [--base EUR]` | Таблица курсов из кеша |
| `help [команда]`, `exit` | Справка, выход |

Пример сеанса:

```text
> register --username alice --password 1234
Пользователь 'alice' зарегистрирован (id=1). Войдите: login --username alice --password ****
> login --username alice --password 1234
Вы вошли как 'alice'
> buy --currency BTC --amount 0.05
Покупка выполнена: 0.0500 BTC по курсу 81368.00 USD/BTC
Изменения в портфеле:
- BTC: было 0.0000 → стало 0.0500
Оценочная стоимость покупки: 4,068.40 USD
> buy --currency EUR --amount 200
Покупка выполнена: 200.00 EUR по курсу 1.1217 USD/EUR
Изменения в портфеле:
- EUR: было 0.00 → стало 200.00
Оценочная стоимость покупки: 224.35 USD
> buy --currency USD --amount 150
Покупка выполнена: 150.00 USD по курсу 1.0000 USD/USD
Изменения в портфеле:
- USD: было 0.00 → стало 150.00
Оценочная стоимость покупки: 150.00 USD
> show-portfolio
Портфель пользователя 'alice' (база: USD):
- BTC: 0.0500  → 4068.40 USD
- EUR: 200.00  → 224.35 USD
- USD: 150.00  → 150.00 USD
---------------------------------
ИТОГО: 4,442.75 USD
> sell --currency BTC --amount 0.01
Продажа выполнена: 0.0100 BTC по курсу 81368.00 USD/BTC
Изменения в портфеле:
- BTC: было 0.0500 → стало 0.0400
Оценочная выручка: 813.68 USD
> get-rate --from USD --to BTC
Курс USD→BTC: 0.00001229 (обновлено: 2026-10-20 14:18:37)
Обратный курс BTC→USD: 81368.00
```

`amount` — положительное число в единицах указанной валюты, коды валют
приводятся к верхнему регистру. Покупка и продажа меняют только кошелёк
указанной валюты, стоимость в USD показывается как оценка по текущему курсу.
Поддерживаемые коды: USD, EUR, GBP, RUB, JPY, CNY, BTC, ETH, SOL, LTC, DOGE
(реестр в `core/currencies.py`, фиат и крипта — наследники `Currency`).

Ошибки печатаются понятными сообщениями: `Недостаточно средств: доступно
0.0400 BTC, требуется 1.0000 BTC`, `Неизвестная валюта 'ABC'` со списком
кодов, `У вас нет кошелька 'ETH'...`, `Сначала выполните login`,
`Ошибка при обращении к внешнему API: ...` с советом повторить позже.
Ошибки разбора аргументов тоже не прерывают интерактивный режим.

## Курсы, кеш и TTL

`data/rates.json` — снимок последних курсов относительно USD:

```json
{
  "pairs": {
    "BTC_USD": {"rate": 81368.0, "updated_at": "2026-10-20T09:18:37Z", "source": "CoinGecko"},
    "EUR_USD": {"rate": 1.1217, "updated_at": "2026-10-20T09:18:37Z", "source": "ExchangeRate-API"}
  },
  "last_refresh": "2026-10-20T09:18:37Z"
}
```

Кросс-курсы считаются через USD: `EUR→RUB = EUR_USD / RUB_USD`. Срок годности
задаёт ключ `rates_ttl_seconds` (300 секунд) и проверяется для каждой пары,
участвующей в расчёте. Если нужная пара устарела или отсутствует, `get-rate`,
`buy`, `sell` и `show-portfolio` печатают `Кеш курсов устарел, обновляю...`
и запускают Parser Service. Когда обновить не удалось, показывается старое
значение с предупреждением, а при пустом кеше —
`Курс USD→BTC недоступен. Повторите попытку позже.` После неудачной попытки
автообновление в рамках одного запуска повторяется не раньше чем через минуту.
Все метки времени выводятся в локальном времени.

## Parser Service

Источники: CoinGecko для криптовалют (без ключа) и ExchangeRate-API для фиата.
Ключ ExchangeRate-API берётся из переменной окружения `EXCHANGERATE_API_KEY`
или из файла `.env` в рабочей папке (образец в `.env.example`, сам `.env`
в Git не входит, кодировка UTF-8):

```sh
EXCHANGERATE_API_KEY=ваш_ключ
```

Без ключа клиент идёт на открытый эндпоинт того же сервиса (`open.er-api.com`)
и пишет предупреждение в журнал. Списки валют, адреса, таймаут и пути
к файлам собраны в `parser_service/config.py` (`ParserConfig`).

```text
> update-rates
INFO: Starting rates update...
INFO: Fetching from CoinGecko... OK (5 rates)
WARNING: EXCHANGERATE_API_KEY не задан, используется открытый эндпоинт
INFO: Fetching from ExchangeRate-API... OK (5 rates)
INFO: Writing 10 rates to data/rates.json...
INFO: Update finished: 10 rates saved, failed sources: none
Update successful. Total rates updated: 10. Last refresh: 2026-10-20T14:18:37
```

Падение одного источника не останавливает обновление: ошибка уходит
в `logs/parser.log`, курсы остальных сохраняются, в консоли —
`Update completed with errors. Check logs/parser.log for details.`
Файлы пишутся атомарно: сначала временный файл, потом замена. Каждое измерение
дописывается в `data/exchange_rates.json` с id вида
`BTC_USD_2026-10-20T09:18:37Z`, повторы по id пропускаются. В снимке пара
заменяется только записью с более свежим `updated_at`. Повреждённый кеш
считается пустым и перезаписывается следующим обновлением.

Обновление по таймеру: `uv run valutatrade-parser --interval 300`
(остановка по Ctrl+C), разовое — `uv run valutatrade-parser --once`.

## Настройки, журналы и исключения

Конфигурация — секция `[tool.valutatrade]` в `pyproject.toml`: папка `data/`
и имена JSON-файлов, `rates_ttl_seconds`, базовая валюта, папка журналов,
уровень, формат (`text` или `json`), размер и число файлов ротации. Её читает
синглтон `SettingsLoader`, доступ к JSON-файлам идёт через синглтон
`DatabaseManager`; оба реализованы через `__new__`.

Журналы: `logs/actions.log` — операции REGISTER, LOGIN, BUY и SELL через
декоратор `@log_action`, `logs/parser.log` — работа парсера. Ротация
по размеру: 1 МБ, три резервных файла. Полный текст сетевых ошибок пишется
только на уровне `DEBUG`, в сообщениях остаётся класс ошибки. Пример строки:

```text
INFO 2026-10-20T14:18:37 BUY user='alice' currency='BTC' amount=0.0500 rate=81368.00 base='USD' balance=0.0000->0.0500 result=OK
```

Исключения в `core/exceptions.py`: `InsufficientFundsError`,
`CurrencyNotFoundError`, `ApiRequestError`, а также `RateNotFoundError`,
`WalletNotFoundError` и `AuthError`. Ядро их бросает, CLI перехватывает
и печатает сообщение. Кэш чтения `rates.json` сделан декоратором `cached`
с хранилищем в замыкании.

## Проверки и сборка

```sh
make lint    # ruff check + ruff format --check
make test    # uv run python tests/test_valutatrade.py
make build   # uv build, результат в dist/
```

Тесты работают во временных папках и подменяют сетевые запросы, данные
проекта не трогают. GitHub Actions выполняет те же шаги при push
и pull request. Собранный wheel ставится как утилита командой
`make package-install` (`uv tool install`), после чего `valutatrade`
и `valutatrade-parser` доступны из любой папки.

## Демонстрация

[Запись полного сеанса](demo/full-session.cast) в формате asciicast v2:
обновление курсов, регистрация, вход, покупка и продажа, портфель, курсы
из кеша и ошибки (нехватка средств, неизвестная валюта, нет кошелька).
Фрагмент: [demo/preview.svg](demo/preview.svg).

Просмотр с установленной asciinema:

```sh
asciinema play demo/full-session.cast
```

Или локальный плеер (нужен интернет для загрузки скрипта с CDN):

```sh
uv run python -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Запись делается командой `make demo`: приложение запускается во временной
папке, вывод сохраняется как есть.

## Структура

```text
valutatrade_hub/
    __main__.py            # python -m valutatrade_hub
    logging_config.py      # формат, уровень и ротация журналов
    decorators.py          # @log_action и кэш в замыкании
    core/
        models.py          # User, Wallet, Portfolio
        currencies.py      # Currency, FiatCurrency, CryptoCurrency, реестр
        exceptions.py      # доменные исключения
        utils.py           # валидация, форматирование, JSON, даты
        usecases.py        # регистрация, вход, сделки, курсы
    infra/
        settings.py        # SettingsLoader (singleton)
        database.py        # DatabaseManager (singleton)
    parser_service/
        config.py          # ParserConfig
        api_clients.py     # BaseApiClient, CoinGeckoClient, ExchangeRateApiClient
        updater.py         # RatesUpdater
        storage.py         # rates.json и exchange_rates.json
        scheduler.py       # обновление по таймеру
    cli/
        interface.py       # команды
data/                      # users.json, portfolios.json, rates.json, exchange_rates.json
tests/                     # функциональные проверки
demo/                      # запись сеанса, превью и плеер
.github/workflows/         # проверки в GitHub Actions
main.py                    # точка входа
pyproject.toml, uv.lock    # пакет, зависимости и настройки [tool.valutatrade]
Makefile, .env.example     # цели сборки и образец файла с ключом
```

Разработка велась в ветках `feature/*` с объединением в `main`.
