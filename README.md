# AI Pulse Dashboard

Статический HTML-дашборд по данным из [community-brain](https://github.com/dmi3semenov/community-brain).

## Что делает

1. Читает данные из `../community-brain/data/` и `../community-brain/wiki/pages/`.
2. Собирает единый `out/dashboard.html` — открывается в браузере, без сервера.

## Запуск

```bash
uv run python main.py
```

После запуска смотри путь к файлу в выводе — открывай его в браузере.

## Требования

- Python ≥ 3.10
- [uv](https://docs.astral.sh/uv/)
- Соседний проект `community-brain/` в той же родительской папке.
