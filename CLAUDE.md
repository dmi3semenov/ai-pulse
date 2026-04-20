# CLAUDE.md — AI Pulse

Памятка для будущих сессий Claude Code. Коротко: что за проект, как запускать,
чего НЕ делать.

## Что это

Еженедельный trend-radar русскоязычного AI-коммьюнити в Telegram. Читает
готовые сущности из соседнего проекта **community-brain** и рендерит
self-contained HTML-дашборд в редакционном стиле («Weekly Briefing»).

Никакого backend'а, никакого LLM-пайплайна здесь нет. Один Python-скрипт
собирает один HTML-файл.

## Источники данных

Парсятся 4 Telegram-канала (см. шапку дашборда):

- `neuraldeepchat` — Чат Kovalskii (Варианты?)
- `prompt_chat` — Промптинг: Изучай, создавай и зарабатывай с ChatGPT
- `Поляков считает и болтает`
- `Чатик мыслителей`

Сами сообщения лежат в `community-brain/data/messages_YYYY-MM-DD.json`,
извлечённые сущности — в `community-brain/wiki/pages/{concepts,entities,people,projects}/*.md`.

## Структура

```
ai-pulse/
├── main.py          # точка входа: uv run python main.py
├── generate.py      # вся логика: загрузка, агрегация, HTML/CSS/JS шаблон
├── aliases.yml      # canonical → [variants] для слияния дублей
├── excluded.yml     # чёрный список (админы чатов и т.п.)
└── out/             # dashboard_YYYY-MM-DD_HHMM.html (таймстампованные сборки)
```

`generate.py` — один большой файл ~1750 строк с inline HTML/CSS/JS как
строковый шаблон. Намеренно. Build-тулов нет.

## Запуск

```bash
uv run python main.py
```

Требует, чтобы `community-brain` лежал рядом (`../community-brain/`).
На выходе — новый файл `out/dashboard_<timestamp>.html`, старые не трогаем.

## Что показывает дашборд

- **Pulse meter** (4 KPI): новинок · растут · падают · главный всплеск
- **Lede** — одно предложение с вшитыми цифрами
- **§ 01 Всплески** — sparkbar'ы прошлая vs текущая неделя
- **§ 02 Траектории** — bump-chart движения ранга тем по неделям/месяцам
- **§ 03 Рекорды** — абсолютный топ-16 за всё окно
- **§ 04 Новинки** — хронология впервые появившихся сущностей

Категории (6, с апр 2026): 🚀 Проекты · 🛠 Инструменты · 🌐 Платформы ·
🧠 Модели · 💡 Концепции · 👤 Люди. Раньше было 4 (entities вместо
tools/platforms/models) — community-brain расщепил «entities».

## Таксономия категорий — источник истины

Все правила project / tool / model / platform / concept / person и поле
`affiliation` лежат в **`community-brain/docs/entity_taxonomy.md`**. Когда
сомневаешься, куда должна попасть сущность, — смотри туда, а не гадай по
контексту. Категоризация чинится **на стороне community-brain** (через
`canonicalize.py` / `migrate_types.py`), а не пластырями в нашем
`aliases.yml`. Наш aliases держит только display-переименования («Андрей
Карпаты» → «Andrej Karpathy») и не должен перемещать сущности между
категориями — это баг: `aliases.yml` применяется без учёта категории.

## Типичный воркфлоу «ревью категорий»

Повторяющийся цикл последних сессий:
1. Юзер правит категории/дубли в community-brain (экстракция / canonicalize).
2. Здесь — `uv run python main.py`, пересобрать дашборд.
3. Пробежаться по вкладкам и сверить с `entity_taxonomy.md`: что в
   неправильной категории, есть ли unicode/translit-дубли, есть ли сущности
   в нескольких категориях одновременно.
4. Вернуть юзеру список проблем для правки в community-brain.
   **НЕ** чинить пластырями в `aliases.yml`, если только community-brain
   не сопротивляется.

## Класс багов: Unicode/translit-дубли

Community-brain-LLM регулярно создаёт два файла для одной сущности:
- **Lookalike-символы**: `CodeDash` (латинская C, U+0043) vs `СodeDash`
  (кириллическая С, U+0421). Глазом не отличить.
- **Транслит**: `Нейропакет` (кириллица) vs `Neyropaket 2025` (латиница).
- **Регистр/пунктуация**: `gpt oss` vs `GPT OSS`, редко.

Как диагностировать: если на bump-chart'е видно одно и то же имя дважды
в одной колонке — вытащи title'ы через `build_data` и сравни repr()'ами.
Чинится в community-brain `canonicalize.py` (нормализация confusables +
транслит). Пластыри в `aliases.yml` — только как временное решение.

## Debug-скрипт для проверки данных

Быстро вытащить состояние для конкретной сущности / недели / категории
(минимально работает без правки файлов):

```bash
cd ai-pulse && uv run python -c "
import sys; sys.path.insert(0, '.')
from generate import build_data
d = build_data('../community-brain/data', '../community-brain/wiki/pages')
# топы по категории
for t, c in d['total']['projects'][:10]: print(f'{t}: {c}')
# конкретная сущность в bump-chart
sw = d['stacked_week']['projects']
for pi, p in enumerate(sw['periods']):
    for r in sw['ranks']:
        t = r['titles'][pi]
        if t and 'agents week' in t.lower():
            print(p, t, r['counts'][pi], r['tags'][pi])
"
```

## Нюансы, о которых легко забыть

- **Top-15 per period с фильтром min_count ≥ 2** (`generate.py:_build_rank_series`,
  `make_stacked`). В редких категориях (проекты) колонка может показать <15
  точек — это не баг, это значит столько сущностей с ≥2 упоминаниями за период.
- **Подписи в bump-chart**: рисуются только если у трека totalCount ≥ 3 ИЛИ
  он заканчивается в последней колонке (`generate.py:renderBump`). Сделано
  намеренно, чтобы не перегружать левую часть графика.
- **aliases.yml слияние** — регистронезависимое; `source_ids` объединяются
  без дубликатов. Дополняй при появлении новых вариантов написания.
- **excluded.yml** — сюда идут админы парсимых чатов (их постоянно упоминают
  внутри их же сообществ, это зашумляет топ-людей).
- **Люди = только external.** В `load_wiki_entities` отфильтровываются все
  `people` с `affiliation != "external"`. Внутренние участники чатов нам
  на дашборде не интересны — они обсуждают тему, а не являются темой.
  Источник истины — поле `affiliation` во frontmatter community-brain.
- **Cырой текст сообщений НЕ читаем**. Из `messages_*.json` берём только
  `msg_id → date`. Вся семантика — уже в wiki community-brain.

## Git-правила

- Коммит-сообщения — на русском (глобальное правило юзера).
- Без co-authored-by строк.
- Любое нетривиальное изменение — ветка + PR, мерджит юзер вручную.
- Ветки: `feature/…`, `fix/…`, `chore/…`, `docs/…`, `experiment/…`.
- Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`.

## Чего НЕ делать

- Не трогать старые сборки в `out/` — это история дизайна, удобно сравнивать.
- Не добавлять build-тулы (webpack/vite/npm). Всё inline в `generate.py` — это
  фича, а не баг.
- Не подключать Chart.js / D3 / другие библиотеки графиков. Bump-chart
  отрисовывается вручную через SVG, стекбар — тоже. Никакого runtime-fetch.
- Не переписывать `messages_*.json` — это вотчина community-brain.
- Не коммитить `.design-bundle/` и `.claude/` — уже в `.gitignore`.
