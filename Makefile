.PHONY: install run lint format test build package-install update-rates demo

install:
	uv sync

run:
	uv run main.py

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

test:
	uv run python tests/test_valutatrade.py
	uv run python -m unittest discover -s tests

build:
	uv build

package-install:
	uv tool install --force $(wildcard dist/*.whl)

update-rates:
	uv run main.py update-rates

demo:
	uv run python -m http.server 8000 --bind 127.0.0.1 --directory demo
