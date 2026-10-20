# ValutaTrade Hub

Учебный консольный кошелёк на Python 3.12 для фиатных и криптовалют.
Можно зарегистрироваться, пополнить виртуальный баланс, купить и продать
валюту, посмотреть портфель и курсы. Автор: Киселёв, группа М26-555.
Проект: `finalproject_kiselev_M26-555`, пакет: `valutatrade_hub`.

## Установка и запуск

Нужны Python 3.12+ и uv. Из корня проекта:

```sh
uv sync
uv run main.py
```

Также доступны `uv run valutatrade` и `make run`. Без аргументов открывается
интерактивный режим. Одну команду можно выполнить так:

```sh
uv run main.py get-rate --from USD --to BTC
```

Данные хранятся в `data/`, журналы — в `logs/` относительно текущей папки.
Сессия сохраняется в `data/session.json` до `logout`. Запускайте команды
из корня проекта, чтобы использовать одни и те же данные.

## Команды

| Команда | Назначение |
| --- | --- |
| `register --username <имя> --password <пароль>` | Регистрация, пароль от 4 символов |
| `login --username <имя> --password <пароль>` | Вход |
| `logout`, `whoami` | Выйти, показать текущего пользователя |
| `deposit --amount 10000` | Пополнить виртуальный USD-кошелёк |
| `show-portfolio [--base USD]` | Портфель и его общая стоимость |
| `buy --currency BTC --amount 0.05` | Купить валюту за USD |
| `sell --currency BTC --amount 0.01` | Продать валюту за USD |
| `get-rate --from USD --to BTC` | Курс пары и время обновления |
| `update-rates [--source coingecko\|exchangerate]` | Обновить курсы |
| `show-rates [--currency BTC] [--top 3] [--base EUR]` | Посмотреть кеш курсов |
| `help [команда]`, `exit` | Справка, выход из приложения |

Пример сеанса:

```text
> register --username alice --password 1234
> login --username alice --password 1234
> deposit --amount 100000
> buy --currency BTC --amount 0.01
> sell --currency BTC --amount 0.005
> show-portfolio --base USD
> get-rate --from USD --to BTC
```

После регистрации портфель пуст. Покупка списывает `amount × курс` с
USD-кошелька, продажа зачисляет выручку обратно. Сумма должна быть конечным
числом больше нуля и задаётся в единицах выбранной валюты. Коды приводятся
к верхнему регистру. При нехватке средств или неизвестной валюте команда
выводит ошибку, балансы не меняются.

## Курсы и Parser Service

`data/rates.json` хранит последние курсы к USD, а `data/exchange_rates.json` —
историю измерений. Кросс-курсы считаются через USD. TTL кеша — 300 секунд;
его можно изменить в `[tool.valutatrade]` в `pyproject.toml`.

Если нужный курс устарел или отсутствует, `buy`, `sell`, `get-rate` и
`show-portfolio` пытаются обновить его. Если получить свежий курс не удалось,
команда завершается с ошибкой. Старый кеш доступен через `show-rates`
с предупреждением об устаревших данных.

Parser Service получает криптовалютные курсы из CoinGecko, фиатные — из
ExchangeRate-API. Ключ задаётся переменной окружения `EXCHANGERATE_API_KEY`
или в `.env` в корне проекта (см. [.env.example](.env.example)):

```dotenv
EXCHANGERATE_API_KEY=ваш_ключ
```

`.env` не попадает в Git. Без ключа используется открытый API `open.er-api.com`.
Параметры источников и списки валют находятся в
`valutatrade_hub/parser_service/config.py`.

```text
> update-rates
> update-rates --source coingecko
> show-rates --top 3
> show-rates --currency RUB
> show-rates --base EUR
```

Если один источник недоступен, курсы остальных сохраняются, ошибка попадает
в `logs/parser.log`. Обновление по таймеру и отдельный разовый запуск:

```sh
uv run valutatrade-parser --interval 300
uv run valutatrade-parser --once
```

Остановка таймера — `Ctrl+C`.

## Проверки и сборка

```sh
uv run ruff check .
uv run ruff format --check .
uv run python tests/test_valutatrade.py
uv run python -m unittest discover -s tests
uv build
```

Или `make lint`, `make test`, `make build`. Сборка появится в `dist/`.
GitHub Actions запускает эти проверки при push и pull request.

## Демо

[Запись сеанса](demo/full-session.cast): регистрация, вход, пополнение,
покупка и продажа, портфель, обновление и просмотр курсов, обработка ошибок.
[Превью](demo/preview.svg).

Просмотр с установленной asciinema:

```sh
asciinema play demo/full-session.cast
```

Или запустите плеер через HTTP:

```sh
uv run python -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Откройте [127.0.0.1:8000](http://127.0.0.1:8000/) и нажмите ▶.
Для загрузки плеера с CDN нужен интернет. Та же команда — `make demo`;
остановка сервера — `Ctrl+C`.

## Структура

```text
valutatrade_hub/
    core/                # модели, валюты, операции, исключения
    infra/               # настройки, JSON-хранилище, блокировки
    parser_service/      # API-клиенты, обновление, история, таймер
    cli/                 # командный интерфейс
    decorators.py        # логирование и кеширование
    logging_config.py    # настройка журналов
data/                    # users.json, portfolios.json, rates.json, exchange_rates.json
tests/                   # проверки
demo/                    # запись и плеер
main.py                  # точка входа
pyproject.toml, uv.lock   # пакет, зависимости и настройки
```
