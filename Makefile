.PHONY: install dev test lint fmt typecheck run status docker clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	python -m pytest

lint:
	ruff check src tests

fmt:
	ruff format src tests

typecheck:
	mypy src

run:
	itlg-keeper run

status:
	itlg-keeper status

docker:
	docker compose up -d --build

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
