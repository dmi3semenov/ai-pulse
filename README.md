# AI Pulse

Еженедельный trend-radar для русскоязычного AI-коммьюнити в Telegram.
Агрегирует сущности, извлечённые соседним проектом **community-brain**, и
собирает из них self-contained HTML-дашборд в редакционном стиле «Weekly
Briefing».

## Что он показывает

Дашборд отвечает на вопрос «что обсуждали в этот период?» через:

- **Pulse meter** — 4 KPI: новинок · растут · падают · главный всплеск
- **Lede** — одно предложение с вшитыми цифрами за неделю
- **§ 01 Всплески** — таблица со sparkbar'ами (прошлая vs текущая неделя)
- **§ 02 Траектории** — bump-chart движения ранга тем по неделям/месяцам
- **§ 03 Рекорды** — абсолютный топ-16 за всё окно данных
- **§ 04 Новинки** — хронология сущностей, появившихся впервые

Разбивка по категориям: 🚀 Проекты · 🛠 Инструменты · 💡 Концепции · 👤 Люди.

## Как оно работает

```
community-brain/data/messages_*.json     (raw telegram dumps)
community-brain/wiki/pages/{cat}/*.md    (LLM-извлечённые сущности)
                    │
                    ▼
            [ python main.py ]
                    │
                    │  1. load messages  → {msg_id → date}
                    │  2. load wiki      → {title, category, source_ids}
                    │  3. normalize (aliases.yml) + filter (excluded.yml)
                    │  4. aggregate by week / month
                    │  5. build hero-stats, novelty, rank-stacked, surges
                    │  6. render HTML c inline-DATA + inline-app.js
                    ▼
            out/dashboard_YYYY-MM-DD_HHMM.html
```

LLM-пайплайн здесь **не запускается** — мы потребляем уже готовые сущности
из community-brain. Наша работа — агрегация по времени, категоризация и
рендеринг. Сырой текст сообщений не читаем, из `messages_*.json` берём
только карту `msg_id → date`.

## Запуск

```bash
uv run python main.py
```

Требует, чтобы `community-brain` лежал рядом:

```
Pet projects/
├── ai-pulse/           ← вы здесь
└── community-brain/
    ├── data/           ← messages_YYYY-MM-DD.json
    └── wiki/pages/     ← concepts/*.md, entities/*.md, people/*.md, projects/*.md
```

Результат — self-contained HTML в `out/dashboard_YYYY-MM-DD_HHMM.html`.
Старые версии не перезатираются (удобно сравнивать и откатываться).

## Конфиги

### `aliases.yml` — канонические имена

Словарь `canonical → [variants]`. Матчинг регистронезависимый; wiki-страницы
с разными `title`, сводящимися к одному каноническому, сливаются (их
`source_ids` объединяются без дубликатов).

```yaml
"Andrej Karpathy":
  - "Карпатый"
  - "Карпаты"
  - "Карпати"
  - "Andrej Karpathy"
```

### `excluded.yml` — чёрный список

Сущности, которые не попадают в дашборд вовсе. Там администраторы парсимых
чатов и постоянные участники — их много упоминают внутри их же сообществ,
это зашумляет топ-людей:

```yaml
people:
  - "Валерий Ковальский"
```

Дополняй по мере появления дубликатов / шума.

## Стек

- Python 3.10+ (через `uv`), `pyyaml` для frontmatter
- Весь HTML / CSS / JS встроен в `generate.py` как строковый шаблон
- Шрифты: Fraunces (serif display) · Geist (sans UI) · JetBrains Mono (числа)
  — подгружаются с Google Fonts CDN
- Bump-chart рисуется вручную как inline-SVG, без зависимостей
- Никакого Chart.js, никаких build-тулов — просто один HTML-файл на выходе

## История дизайна

Изначально был «тёмно-фиолетовый SaaS-дашборд» со стеками Chart.js и облаком
слов. Текущий вид — редакционное еженедельное досье — получен через
[Claude Design](https://claude.ai/design): он предложил bump-chart вместо
стекового бара, таблицу со sparkbars вместо цветных чипов и бумажную
oklch-палитру вместо тёмного фона. Handoff-бандл с исходным прототипом
находится в `.design-bundle/` (не коммитится).

## Что дальше

- [ ] Источники помимо Telegram (Reddit / YouTube / Instagram)
- [ ] Фильтры-фасеты на вкладках: «только новое» · «только выросло»
- [ ] Интерактив на bump-chart (hover → подсветка линии + полное имя)
- [ ] Mini-sparkline рядом с каждой строкой лидерборда
- [ ] Автозапуск раз в неделю + публикация на GitHub Pages
