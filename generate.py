"""
generate.py — модуль генерации AI Pulse Dashboard (v2: Trend Radar).

Читает данные из двух источников:
  1. community-brain/data/messages_YYYY-MM-DD.json  — сырые сообщения
  2. community-brain/wiki/pages/{category}/*.md     — wiki-сущности с source_ids

И третий вспомогательный файл:
  3. aliases.yml                                     — словарь канонических имён

Строит:
  - агрегированные счётчики по неделям / месяцам
  - heatmap-данные (топ-15 × последние N недель) по каждой категории
  - stacked-данные для графика «горячие темы» (топ-6 с подписями)
  - unified-surges (всплески по всем 4 категориям в одном блоке)
  - топ-листы по каждой категории

Затем рендерит самодостаточный HTML с Chart.js + wordcloud2.js (CDN).
"""

import json
import re
import glob
import os
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml  # pyyaml — для чтения YAML frontmatter wiki-страниц и aliases.yml

# ── Категории сущностей (совпадают с папками в wiki/pages/) ─────────────────
# Порядок здесь определяет порядок табов и дефолтную категорию (первая = by default).
# С апреля 2026 community-brain разделил старый «entities» на три: tools,
# platforms, models. Старая папка entities теперь пустая (держать в списке
# смысла нет — иначе колонка табов пустая).
CATEGORIES = ["tools", "models", "platforms", "concepts", "people", "projects"]

# Иконки и цвета категорий — используются в UI и передаются в JS.
CATEGORY_META = {
    "projects":  {"icon": "🚀", "label": "Инициативы",   "color": "#fb923c"},
    "tools":     {"icon": "🛠", "label": "Инструменты", "color": "#60a5fa"},
    "platforms": {"icon": "🌐", "label": "Платформы",   "color": "#22d3ee"},
    "models":    {"icon": "🧠", "label": "Модели",      "color": "#eab308"},
    "concepts":  {"icon": "💡", "label": "Концепции",   "color": "#a78bfa"},
    "people":    {"icon": "👤", "label": "Люди",        "color": "#34d399"},
}

# Минимальное число упоминаний за период, чтобы сущность попала в графики
# (hot-topics, novelty, hero-KPI). В плотных категориях (инструменты, модели,
# проекты) порог 2 отсекает одноразовый шум. В «Люди» внешних фигур мало и
# упоминаются они редко — при пороге 2 график пустеет, поэтому 1.
CATEGORY_MIN_COUNT = {
    "projects":  2,
    "tools":     2,
    "platforms": 2,
    "models":    2,
    "concepts":  2,
    "people":    1,
}

# Палитра для сегментов стекового графика: 10 разнесённых по hue-колесу
# оттенков. Раньше было 15 со множеством дублей (#818cf8≈#c084fc, три зелёных,
# два жёлтых, два оранжевых, три розовых) — их глаз всё равно путает.
PALETTE = [
    "#a78bfa",  # violet
    "#60a5fa",  # blue
    "#38bdf8",  # sky
    "#2dd4bf",  # teal
    "#34d399",  # emerald
    "#a3e635",  # lime
    "#fbbf24",  # amber
    "#fb923c",  # orange
    "#f87171",  # coral
    "#f472b6",  # pink
]

# Окно истории: считаем данные, начиная с этой даты.
SINCE = "2026-03-01"

# Сколько сущностей показывать на столбик стекового графика (каждая колонка
# считается независимо — в разные недели могут быть разные лица).
STACK_TOP_N = 15

# Сколько последних недель показывать в стеке «горячие темы» (5 недель даёт
# достаточно широкие столбцы, чтобы в них помещались имена целиком).
STACK_WEEKS = 5

# Пути по умолчанию.
ALIASES_PATH  = Path(__file__).parent / "aliases.yml"
EXCLUDED_PATH = Path(__file__).parent / "excluded.yml"


# ── Вспомогательные функции группировки по периодам ─────────────────────────

def get_week(date_str: str) -> str:
    """Возвращает дату понедельника недели, к которой принадлежит дата."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def get_month(date_str: str) -> str:
    """Возвращает 'YYYY-MM' для группировки по месяцам."""
    return date_str[:7]


# ── Загрузка словаря алиасов ────────────────────────────────────────────────

def load_aliases(path: Path = ALIASES_PATH) -> dict[str, str]:
    """
    Читает aliases.yml и возвращает плоский словарь {вариант.lower() → каноническое_имя}.
    Если файла нет или YAML битый — возвращает пустой dict (нормализация тогда no-op).
    """
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"⚠️  aliases.yml: ошибка YAML ({e}), пропускаю нормализацию")
        return {}
    flat: dict[str, str] = {}
    for canonical, variants in raw.items():
        if not canonical:
            continue
        flat[canonical.lower()] = canonical
        for v in (variants or []):
            if v:
                flat[v.lower()] = canonical
    return flat


def normalize_title(title: str, aliases: dict[str, str]) -> str:
    """Если title — один из вариантов в aliases.yml, возвращает каноническое имя."""
    return aliases.get(title.lower(), title)


# ── Загрузка списка исключений ──────────────────────────────────────────────

def load_excluded(path: Path = EXCLUDED_PATH) -> dict[str, set[str]]:
    """
    Читает excluded.yml и возвращает {category → set_of_lowercased_titles}.
    Эти сущности полностью выкинутся из дашборда (не попадут даже в total).
    Матчинг регистронезависимый; имена ожидаются в канонической форме (т.е.
    уже после применения aliases.yml).
    """
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"⚠️  excluded.yml: ошибка YAML ({e}), пропускаю исключения")
        return {}
    result: dict[str, set[str]] = {}
    for cat, names in raw.items():
        if cat in CATEGORIES and isinstance(names, list):
            result[cat] = {n.lower() for n in names if n}
    return result


# ── Загрузка raw-сообщений ──────────────────────────────────────────────────

def load_messages(data_dir: str, since: str = SINCE) -> dict[int, str]:
    """
    Читает все messages_YYYY-MM-DD.json из data_dir.
    Возвращает словарь {message_id: date_str} только для сообщений >= since.
    """
    id_to_date: dict[int, str] = {}
    pattern = os.path.join(data_dir, "messages_*.json")

    for fpath in glob.glob(pattern):
        with open(fpath, encoding="utf-8") as f:
            msgs = json.load(f)
        for m in msgs:
            msg_id = m.get("id")
            date   = m.get("date", "")[:10]
            if msg_id and date >= since:
                id_to_date[msg_id] = date

    return id_to_date


# ── Загрузка wiki-сущностей (с нормализацией имён) ──────────────────────────

def load_wiki_entities(
    wiki_pages_dir: str,
    aliases:  dict[str, str]      | None = None,
    excluded: dict[str, set[str]] | None = None,
) -> list[dict]:
    """
    Читает все .md файлы из wiki/pages/{category}/.
    Нормализует title через aliases, выкидывает записи из excluded и объединяет
    страницы с одинаковым каноническим именем (source_ids склеиваются без
    дубликатов).

    Возвращает список словарей: {category, title, source_ids}
    """
    aliases  = aliases  or {}
    excluded = excluded or {}

    # Первый проход: собираем всё, нормализуя title.
    raw_entries: list[tuple[str, str, list[int]]] = []
    for cat in CATEGORIES:
        cat_dir = os.path.join(wiki_pages_dir, cat)
        for fpath in glob.glob(os.path.join(cat_dir, "*.md")):
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not match:
                continue
            try:
                meta = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                continue
            title      = (meta.get("title") or "").strip()
            source_ids = meta.get("source_ids") or []
            if not title or not source_ids:
                continue
            # В категории «people» держим только публичных (external). Участники
            # чатов (internal) засоряют топ — их много упоминают внутри своих
            # же сообществ, это дублирует сигнал from-chat в людей-как-сущности.
            if cat == "people" and meta.get("affiliation") != "external":
                continue
            raw_entries.append((cat, normalize_title(title, aliases), list(source_ids)))

    # Второй проход: схлопываем одноимённые (после нормализации) wiki-страницы.
    merged: dict[tuple[str, str], set[int]] = defaultdict(set)
    for cat, title, ids in raw_entries:
        merged[(cat, title)].update(ids)

    # Третий проход: выкидываем исключённые.
    result: list[dict] = []
    for (cat, title), ids in merged.items():
        if title.lower() in excluded.get(cat, set()):
            continue
        result.append({"category": cat, "title": title, "source_ids": sorted(ids)})
    return result


# ── Агрегация данных ────────────────────────────────────────────────────────

def aggregate(entities: list[dict], id_to_date: dict[int, str]) -> dict:
    """
    Для каждой wiki-сущности находит даты упоминаний через source_ids.
    Накапливает счётчики по:
      - total  : за всё время
      - week   : по неделям
      - month  : по месяцам
    (Дневная гранулярность выпилена в v2 — не используется в UI.)
    """
    total = defaultdict(Counter)
    week  = defaultdict(lambda: defaultdict(Counter))  # [cat][week_key][title]
    month = defaultdict(lambda: defaultdict(Counter))  # [cat][month_key][title]

    for e in entities:
        cat, title = e["category"], e["title"]
        for sid in e["source_ids"]:
            d = id_to_date.get(sid)
            if not d:
                continue
            total[cat][title] += 1
            week[cat][get_week(d)][title]  += 1
            month[cat][get_month(d)][title] += 1

    return {"total": total, "week": week, "month": month}


# ── Построение stacked-серий (rank-dataset: сортировка внутри столбца) ─────
#
# Проблема: обычный stacked-bar в Chart.js имеет фиксированный порядок
# датасетов для всех колонок. Если датасет[0] — это «Claude», Claude всегда
# внизу стека, даже если в конкретной неделе её по числу упоминаний обогнала
# другая сущность. Это портит визуальную сортировку «самое обсуждаемое внизу».
#
# Решение: делаем датасеты по РАНГАМ, а не по сущностям. Датасет i — это
# «сущность на i-м месте по убыванию в столбце», значение = её count в этом
# периоде. Цвет каждого столбца задаём индивидуально (per-bar color) по
# глобальному color_map, чтобы одна и та же сущность оставалась в одном
# цвете везде. Подпись сегмента берётся из `titles[idx]` датасета.

def _build_rank_series(
    periods:       list[str],
    by_time_cat:   dict,
    total_counter: Counter,
    top_n:         int,
    filter_fn                   = None,
    tag_fn                      = None,
    shared_color_map: dict | None = None,
) -> dict:
    """
    Строит rank-dataset структуру для стекового бара с per-column сортировкой.

    periods         — список периодов (x-ось)
    by_time_cat     — {period → Counter{title: count}}
    total_counter   — Counter{title: total_count} (для ранжирования цветов)
    top_n           — сколько рангов держать на столбик
    filter_fn       — необязательно: (title, count, period) → bool. Сущности,
                      не прошедшие фильтр, не попадают в столбик этого периода.
    tag_fn          — необязательно: (title, count, period) → dict. Вернувшийся
                      dict кладётся в rank.tags[period_idx] — можно использовать
                      на клиенте (например, метить isNew для всплесков).
    shared_color_map — необязательно: если передан, используется вместо локального
                      (для визуальной согласованности между разными графиками).

    Возвращает:
      periods    — как вход
      ranks      — [top_n элементов], каждый: {counts: [...], titles: [...], tags: [...]}
      color_map  — {title → hex} на всю вселенную этого графика
    """
    # 1. Для каждого периода — сортированный по убыванию top_n (с фильтром).
    period_ranks: dict[str, list[tuple[str, int]]] = {}
    for p in periods:
        items = Counter(by_time_cat.get(p, {})).most_common()
        if filter_fn is not None:
            items = [(t, c) for t, c in items if filter_fn(t, c, p)]
        period_ranks[p] = items[:top_n]

    # 2. «Ранговые» датасеты: индекс i = i-е место в столбце.
    ranks: list[dict] = []
    for rank_idx in range(top_n):
        counts_row: list[int]  = []
        titles_row: list[str]  = []
        tags_row:   list[dict] = []
        for p in periods:
            row = period_ranks[p]
            if rank_idx < len(row):
                title, cnt = row[rank_idx]
                counts_row.append(cnt)
                titles_row.append(title)
                tags_row.append(tag_fn(title, cnt, p) if tag_fn else {})
            else:
                counts_row.append(0)
                titles_row.append("")
                tags_row.append({})
        ranks.append({"counts": counts_row, "titles": titles_row, "tags": tags_row})

    # 3. Палитру раздаём по глобальному рангу в total_counter — тогда одна
    #    сущность получит один и тот же цвет в разных графиках и в разных
    #    категориях (если total_counter один и тот же).
    universe: set[str] = set()
    for row in period_ranks.values():
        for t, _ in row:
            universe.add(t)

    if shared_color_map is not None:
        color_map = {t: shared_color_map.get(t) for t in universe if t in shared_color_map}
        # Добиваем цвета для сущностей, которых почему-то нет в shared (на всякий).
        global_order = [t for t, _ in total_counter.most_common()]
        rank_of = {t: i for i, t in enumerate(global_order)}
        for t in universe:
            if t not in color_map or color_map[t] is None:
                color_map[t] = PALETTE[rank_of.get(t, 99) % len(PALETTE)]
    else:
        global_order = [t for t, _ in total_counter.most_common()]
        rank_of = {t: i for i, t in enumerate(global_order)}
        color_map = {t: PALETTE[rank_of.get(t, 99) % len(PALETTE)] for t in universe}

    return {
        "periods":   periods,
        "ranks":     ranks,
        "color_map": color_map,
    }


def _delta_isnew_tags(
    by_time_cat: dict,
    all_periods: list[str],
    window:      list[str],
) -> dict[tuple[str, str], dict]:
    """
    Предрасчёт для каждой (период, title) пары:
      - delta  = count(p) - count(prev_p)
      - isNew  = True если count(prev_p) == 0
    prev_p берётся из all_periods (может быть за пределами window).
    """
    out: dict[tuple[str, str], dict] = {}
    for p in window:
        idx  = all_periods.index(p)
        prev = all_periods[idx - 1] if idx > 0 else None
        cur_map  = by_time_cat.get(p, {})
        prev_map = by_time_cat.get(prev, {}) if prev else {}
        for title, cnt in cur_map.items():
            prev_cnt = prev_map.get(title, 0)
            out[(p, title)] = {
                "delta": cnt - prev_cnt,
                "isNew": prev_cnt == 0,
            }
    return out


def make_stacked(
    cat: str,
    agg: dict,
    granularity: str,
    top_n:     int = STACK_TOP_N,
    min_count: int = 2,
) -> dict:
    """
    Горячие темы: в каждом столбике свой top_n по count этого периода.
    Фильтр min_count=2 отсекает «одноразовые» сущности (их сегменты слишком
    тонкие для подписи). К каждой точке прилагается тег {delta, isNew} —
    клиент отрисует +N или ✨ прямо в подписи бара.
    """
    by_time = agg[granularity]
    min_key = SINCE[:7] if granularity == "month" else SINCE
    all_periods = sorted(k for k in by_time[cat] if k >= min_key)
    periods = all_periods[-STACK_WEEKS:] if granularity == "week" else all_periods

    tags = _delta_isnew_tags(by_time[cat], all_periods, periods)

    return _build_rank_series(
        periods,
        by_time[cat],
        agg["total"][cat],
        top_n,
        filter_fn=lambda t, c, p: c >= min_count,
        tag_fn=lambda t, c, p: tags.get((p, t), {"delta": 0, "isNew": False}),
    )


def make_novelty_stacked(
    cat: str,
    agg: dict,
    top_n:        int = STACK_TOP_N,
    weeks_limit:  int = STACK_WEEKS,
    min_count:    int = 2,
) -> dict:
    """
    Новинки как stacked-бар: сущности, впервые появившиеся в окне именно в
    этой неделе (до этого ни в одной из предыдущих недель окна их не было).
    Фильтр min_count=2 убирает одноразовый шум.

    Важно: именно «впервые в окне», а не week-over-week. Если сущность
    мелькнула в начале окна, пропала на пару недель и вернулась — это НЕ
    новинка (пользователь её уже видел). isNew из _delta_isnew_tags такие
    случаи пропускает, поэтому здесь считаем first_seen самостоятельно.
    """
    all_weeks = sorted(w for w in agg["week"][cat] if w >= SINCE)
    periods   = all_weeks[-weeks_limit:]
    tags      = _delta_isnew_tags(agg["week"][cat], all_weeks, periods)

    # first_seen[title] — самая ранняя неделя, где у сущности был count > 0.
    first_seen: dict[str, str] = {}
    for w in all_weeks:
        for t in agg["week"][cat].get(w, {}):
            first_seen.setdefault(t, w)

    return _build_rank_series(
        periods,
        agg["week"][cat],
        agg["total"][cat],
        top_n,
        filter_fn=lambda t, c, p: c >= min_count and first_seen.get(t) == p,
        tag_fn=lambda t, c, p: tags.get((p, t), {"delta": c, "isNew": True}),
    )


# ── Всплески ────────────────────────────────────────────────────────────────

def _last_two_weeks(agg: dict) -> tuple[str | None, str | None]:
    """Находит две самые свежие недели, в которых вообще есть данные (по всем
    категориям). prev может быть None, если это самая первая неделя окна."""
    all_weeks: set[str] = set()
    for cat in CATEGORIES:
        all_weeks.update(w for w in agg["week"][cat] if w >= SINCE)
    weeks = sorted(all_weeks)
    if not weeks:
        return (None, None)
    last = weeks[-1]
    prev = weeks[-2] if len(weeks) >= 2 else None
    return (last, prev)


def _surge_items(agg: dict, cat: str, last: str, prev: str | None) -> list[dict]:
    """Возвращает список dict-ов о сущностях, которые в `last`-неделю либо
    выросли (delta > 0), либо появились впервые (isNew). Сортировка по delta ↓,
    затем по count ↓."""
    cur_map  = agg["week"][cat].get(last, {})
    prev_map = agg["week"][cat].get(prev, {}) if prev else {}
    items: list[dict] = []
    for title, count in cur_map.items():
        prev_count = prev_map.get(title, 0)
        delta  = count - prev_count
        is_new = prev_count == 0
        if delta > 0 or is_new:
            items.append({
                "title":    title,
                "category": cat,
                "count":    count,
                "delta":    delta,
                "isNew":    is_new,
            })
    items.sort(key=lambda x: (-x["delta"], -x["count"]))
    return items


def make_hero_stats(agg: dict) -> dict:
    """
    KPI-строка для hero-стрипа сверху дашборда. Считает на последнюю неделю:
      - сколько сущностей впервые появилось (с разбивкой по категориям)
      - сколько выросло vs прошлой недели (не-new, delta > 0)
      - сколько упало (delta < 0)
      - какая сущность дала самый большой прирост (delta ↑) — имя + категория.

    Фильтр min_count по категориям (CATEGORY_MIN_COUNT) согласован с
    Novelty/Hot-topics графиками: одноразовый шум не учитываем, но для «Люди»
    порог 1 (иначе метрики часто пустые).
    """
    last, prev = _last_two_weeks(agg)
    zero_counts = {cat: 0 for cat in CATEGORIES}
    empty = {"total": 0, "by_cat": dict(zero_counts)}

    if not last:
        return {
            "week": None, "prev_week": None,
            "new": empty, "rising": empty, "falling": empty,
            "top_gainer": None,
        }

    new     = {"total": 0, "by_cat": dict(zero_counts)}
    rising  = {"total": 0, "by_cat": dict(zero_counts)}
    falling = {"total": 0, "by_cat": dict(zero_counts)}
    top_gainer: dict | None = None

    for cat in CATEGORIES:
        cat_min = CATEGORY_MIN_COUNT[cat]
        cur_map  = agg["week"][cat].get(last, {})
        prev_map = agg["week"][cat].get(prev, {}) if prev else {}
        for title, count in cur_map.items():
            if count < cat_min:
                continue
            prev_count = prev_map.get(title, 0)
            delta = count - prev_count
            is_new = prev_count == 0
            if is_new:
                new["total"] += 1
                new["by_cat"][cat] += 1
            elif delta > 0:
                rising["total"] += 1
                rising["by_cat"][cat] += 1
            elif delta < 0:
                falling["total"] += 1
                falling["by_cat"][cat] += 1
            # Top gainer = максимум delta (учитываем и new — у них delta = count).
            if delta > 0 and (top_gainer is None or delta > top_gainer["delta"]):
                top_gainer = {
                    "title":    title,
                    "category": cat,
                    "count":    count,
                    "delta":    delta,
                    "isNew":    is_new,
                }

    return {
        "week":        last,
        "prev_week":   prev,
        "new":         new,
        "rising":      rising,
        "falling":     falling,
        "top_gainer":  top_gainer,
    }


def make_category_surges(agg: dict, max_items: int = 40) -> dict:
    """
    Всплески за последнюю неделю отдельно по каждой категории.
    Структура: {cat: {week, prev_week, items}}.
    """
    last, prev = _last_two_weeks(agg)
    result: dict[str, dict] = {}
    for cat in CATEGORIES:
        items = _surge_items(agg, cat, last, prev) if last else []
        result[cat] = {
            "week":      last,
            "prev_week": prev,
            "items":     items[:max_items],
        }
    return result


# ── Финальная сборка данных ─────────────────────────────────────────────────

def build_data(data_dir: str, wiki_pages_dir: str) -> dict:
    """
    Полный пайплайн: загрузка → агрегация → подготовка данных для HTML.
    Возвращает словарь, который напрямую сериализуется в JS-переменную DATA.
    """
    print("📥 Загружаю сообщения...")
    id_to_date = load_messages(data_dir)
    print(f"   {len(id_to_date)} сообщений с датами")

    print("📚 Загружаю aliases.yml...")
    aliases = load_aliases()
    print(f"   {len(aliases)} правил нормализации")

    print("🚫 Загружаю excluded.yml...")
    excluded = load_excluded()
    excl_total = sum(len(s) for s in excluded.values())
    print(f"   {excl_total} исключений")

    print("📚 Загружаю wiki-сущности...")
    entities = load_wiki_entities(wiki_pages_dir, aliases, excluded)
    print(f"   {len(entities)} сущностей (после aliases + excluded)")

    print("🔢 Агрегирую данные...")
    agg = aggregate(entities, id_to_date)

    def top_n(counter, n=60):
        return [[t, c] for t, c in counter.most_common(n)]

    # Топ-листы за весь период (для правой колонки + облака слов).
    total_data = {cat: top_n(agg["total"][cat]) for cat in CATEGORIES}

    # Stacked «горячие темы» — недели + месяцы.
    stacked_week  = {cat: make_stacked(cat, agg, "week",  min_count=CATEGORY_MIN_COUNT[cat]) for cat in CATEGORIES}
    stacked_month = {cat: make_stacked(cat, agg, "month", min_count=CATEGORY_MIN_COUNT[cat]) for cat in CATEGORIES}

    # Stacked «новинки» — только то, что появилось ВПЕРВЫЕ в данную неделю
    # (prev_count == 0). Недельная разбивка; месяцы бессмысленны на коротком
    # окне данных.
    novelty_stacked = {cat: make_novelty_stacked(cat, agg, min_count=CATEGORY_MIN_COUNT[cat]) for cat in CATEGORIES}

    # Per-категорийный блок всплесков (чипы во вкладке категории) и hero-KPI.
    category_surges = make_category_surges(agg)
    hero_stats      = make_hero_stats(agg)

    # Последняя дата сообщения во входных данных — для футера.
    data_last_date = max(id_to_date.values()) if id_to_date else SINCE

    return {
        "meta": {
            "total_entities":  len(entities),
            "total_messages":  len(id_to_date),
            "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
            "since":           SINCE,
            "data_last_date":  data_last_date,
        },
        "categories":      CATEGORY_META,
        "total":           total_data,
        "stacked_week":    stacked_week,
        "stacked_month":   stacked_month,
        "novelty_stacked": novelty_stacked,
        "category_surges": category_surges,
        "hero_stats":      hero_stats,
    }



# ── HTML-шаблон (Editorial «Weekly Briefing») ───────────────────────────────
#
# Дизайн из Claude Design: редакционная эстетика, тёплая бумага, серифы и
# монотонные числа. Исходные файлы — .design-bundle/dima/project/
#   • AI Pulse Weekly.html  — HTML + inline CSS
#   • app.js                — рендер таблиц/bump-chart'а/лидерборда/новизны
# Мы заинлайниваем и то, и другое — выход остаётся self-contained HTML.
#
# Shape DATA, ожидаемая app.js, совпадает с тем, что отдаёт build_data():
#   meta{since,data_last_date,total_entities,total_messages,generated_at}
#   hero_stats{week,prev_week,new,rising,falling,top_gainer}
#   category_surges[cat]{week,items:[{title,count,delta,isNew,category}]}
#   stacked_week[cat], stacked_month[cat], novelty_stacked[cat]
#     {periods, ranks:[{counts,titles,tags:[{delta,isNew}]}]}
#   total[cat]  → [[title,count], ...]

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>AI Pulse — Weekly Briefing</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600;9..144,700&family=JetBrains+Mono:wght@400;500;600&family=Geist:wght@300;400;500;600;700&display=swap" rel="stylesheet">

<style>
/* ─────────────────────────────────────────────────────────────────────
   AI PULSE · WEEKLY BRIEFING
   Editorial redesign — "analyst memo" aesthetic, warm paper, ink, red.
   ───────────────────────────────────────────────────────────────────── */

:root{
  --paper:        oklch(96.5% 0.008 82);
  --paper-deep:   oklch(93% 0.012 82);
  --ink:          oklch(20% 0.015 260);
  --ink-soft:     oklch(38% 0.012 260);
  --ink-mute:     oklch(55% 0.01 260);
  --rule:         oklch(82% 0.015 80);
  --rule-soft:    oklch(88% 0.012 80);

  --red:          oklch(52% 0.17 25);    /* rising / hot */
  --red-soft:     oklch(52% 0.17 25 / 0.1);
  --blue:         oklch(48% 0.09 245);   /* new */
  --blue-soft:    oklch(48% 0.09 245 / 0.1);
  --grey:         oklch(55% 0.005 260);  /* falling */

  --cat-projects:  oklch(52% 0.14 50);
  --cat-tools:     oklch(48% 0.09 245);
  --cat-platforms: oklch(55% 0.11 200);
  --cat-models:    oklch(58% 0.12 80);
  --cat-concepts:  oklch(46% 0.12 300);
  --cat-people:    oklch(50% 0.10 165);

  --serif:  "Fraunces", "Instrument Serif", Georgia, serif;
  --sans:   "Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --mono:   "JetBrains Mono", ui-monospace, Menlo, monospace;
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--paper);color:var(--ink)}
body{
  font-family:var(--sans);
  font-size:15px;
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
  min-height:100vh;
  padding-bottom:60px;
}

/* ── Shell ─────────────────────────────────────────────────── */
.shell{max-width:1180px;margin:0 auto;padding:0 36px}
@media(max-width:720px){.shell{padding:0 20px}}

/* ── Masthead ──────────────────────────────────────────────── */
.masthead{
  padding:26px 0 20px;
  border-bottom:1.5px solid var(--ink);
  display:grid;
  grid-template-columns:1fr auto;
  align-items:end;
  gap:20px;
}
.masthead-left .eyebrow{
  font-family:var(--mono);
  font-size:11px;
  letter-spacing:0.18em;
  text-transform:uppercase;
  color:var(--ink-soft);
  margin-bottom:8px;
  display:flex;align-items:center;gap:10px;
}
.eyebrow .dot{
  width:7px;height:7px;border-radius:50%;
  background:var(--red);
  box-shadow:0 0 0 3px oklch(52% 0.17 25 / 0.18);
  animation:pulse 2.4s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}

.wordmark{
  font-family:var(--serif);
  font-weight:400;
  font-size:clamp(36px, 6vw, 64px);
  line-height:0.95;
  letter-spacing:-0.02em;
  font-variation-settings:"opsz" 144, "SOFT" 30;
}
.wordmark em{font-style:italic;font-weight:300}

.masthead-right{
  font-family:var(--mono);
  font-size:11px;
  color:var(--ink-soft);
  text-align:right;
  line-height:1.7;
}
.masthead-right b{color:var(--ink);font-weight:500}

@media(max-width:720px){
  .masthead{grid-template-columns:1fr;align-items:start}
  .masthead-right{text-align:left}
}

/* ── Issue strip: dateline + volume + metrics ──────────────── */
.dateline{
  display:flex;flex-wrap:wrap;align-items:center;gap:0 22px;
  padding:10px 0;
  border-bottom:1px solid var(--rule);
  font-family:var(--mono);
  font-size:11px;
  letter-spacing:0.04em;
  color:var(--ink-soft);
  text-transform:uppercase;
}
.dateline span b{color:var(--ink);font-weight:500}

/* ── Lede (Pulse sentence) ─────────────────────────────────── */
.lede{
  padding:36px 0 30px;
  border-bottom:1px solid var(--rule);
  display:grid;
  grid-template-columns:72px 1fr;
  gap:24px;
}
.lede-tag{
  font-family:var(--mono);
  font-size:11px;
  letter-spacing:0.12em;
  text-transform:uppercase;
  color:var(--red);
  border-top:2px solid var(--red);
  padding-top:10px;
  line-height:1.3;
}
.lede-body{
  font-family:var(--serif);
  font-weight:300;
  font-size:clamp(22px, 2.6vw, 30px);
  line-height:1.3;
  letter-spacing:-0.012em;
  color:var(--ink);
  text-wrap:pretty;
}
.lede-body .hl{
  font-style:italic;
  font-weight:400;
  color:var(--red);
  border-bottom:1px solid oklch(52% 0.17 25 / 0.4);
  padding-bottom:1px;
}
.lede-body .num{
  font-family:var(--mono);
  font-weight:500;
  font-size:0.85em;
  font-style:normal;
  background:var(--red-soft);
  color:var(--red);
  padding:1px 8px;
  border-radius:3px;
  margin:0 2px;
  letter-spacing:-0.01em;
}
.lede-body .num.blue{background:var(--blue-soft);color:var(--blue)}
.lede-body .num.grey{background:oklch(0% 0 0 / 0.05);color:var(--ink-soft)}

/* ── Pulse meter (4 tick columns) ──────────────────────────── */
.meter{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:0;
  border-bottom:1px solid var(--rule);
}
@media(max-width:700px){.meter{grid-template-columns:repeat(2,1fr)}}
.meter-cell{
  padding:22px 20px 24px;
  border-right:1px solid var(--rule);
  position:relative;
}
.meter-cell:last-child{border-right:none}
@media(max-width:700px){
  .meter-cell:nth-child(2){border-right:none}
  .meter-cell:nth-child(-n+2){border-bottom:1px solid var(--rule)}
}
.meter-cell::before{
  content:"";position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--tone, var(--ink));
}
.meter-cell[data-tone=new]     {--tone:var(--blue)}
.meter-cell[data-tone=rising]  {--tone:var(--red)}
.meter-cell[data-tone=falling] {--tone:var(--grey)}
.meter-cell[data-tone=gainer]  {--tone:var(--ink)}

.meter-label{
  font-family:var(--mono);
  font-size:10.5px;
  letter-spacing:0.14em;
  text-transform:uppercase;
  color:var(--ink-mute);
  margin-bottom:12px;
  display:flex;justify-content:space-between;align-items:baseline;
}
.meter-label .glyph{
  font-family:var(--serif);
  font-style:italic;
  font-size:20px;
  color:var(--tone);
  letter-spacing:normal;
  text-transform:none;
}
.meter-value{
  font-family:var(--serif);
  font-weight:400;
  font-size:52px;
  line-height:0.95;
  letter-spacing:-0.03em;
  color:var(--ink);
  font-variant-numeric:tabular-nums;
  margin-bottom:14px;
}
.meter-value.small{font-size:22px;font-weight:500;letter-spacing:-0.01em;line-height:1.2}
.meter-breakdown{
  font-family:var(--mono);
  font-size:11px;
  color:var(--ink-mute);
  display:flex;flex-wrap:wrap;gap:4px 12px;
}
.meter-breakdown .bd{display:inline-flex;align-items:center;gap:5px}
.meter-breakdown .bd::before{
  content:"";width:6px;height:6px;border-radius:1px;background:var(--bd-c);
  display:inline-block;
}
.meter-sub{
  font-family:var(--mono);
  font-size:11px;
  color:var(--ink-mute);
  line-height:1.5;
}
.meter-sub .delta{color:var(--red);font-weight:500}
.meter-sub .delta.new{color:var(--blue)}

/* ── Section header ────────────────────────────────────────── */
.section{padding:40px 0 10px;border-bottom:1px solid var(--rule)}
.section-head{
  display:grid;
  grid-template-columns:60px 1fr auto;
  gap:18px;
  align-items:baseline;
  margin-bottom:22px;
}
.section-num{
  font-family:var(--mono);
  font-size:11px;
  letter-spacing:0.1em;
  color:var(--ink-mute);
  border-top:2px solid var(--ink);
  padding-top:8px;
}
.section-title{
  font-family:var(--serif);
  font-weight:400;
  font-size:clamp(24px, 3vw, 32px);
  line-height:1.1;
  letter-spacing:-0.015em;
  color:var(--ink);
}
.section-title em{font-style:italic;font-weight:300}
.section-tools{
  display:flex;gap:2px;align-items:center;
  font-family:var(--mono);font-size:11px;color:var(--ink-mute);
}
.section-desc{
  grid-column:2/4;
  font-size:13.5px;
  line-height:1.55;
  color:var(--ink-soft);
  max-width:68ch;
  margin-top:-14px;
  margin-bottom:4px;
}
@media(max-width:720px){
  .section-head{grid-template-columns:auto 1fr;grid-template-rows:auto auto}
  .section-tools{grid-column:1/-1;justify-self:start}
  .section-desc{grid-column:1/-1;margin-top:0}
}

/* ── Category tabs (filmstrip) ─────────────────────────────── */
.catbar-wrap{
  position:sticky;top:0;z-index:30;
  background:var(--paper);
  border-bottom:1px solid var(--ink);
  margin:0 -36px;
  padding:0 36px;
}
@media(max-width:720px){
  .catbar-wrap{margin:0 -20px;padding:0 20px}
}
.catbar{
  display:flex;gap:0;overflow-x:auto;
  -webkit-overflow-scrolling:touch;
}
.cat-btn{
  flex:1 1 auto;
  min-width:120px;
  padding:14px 16px 12px;
  background:transparent;
  border:none;
  border-right:1px solid var(--rule);
  cursor:pointer;
  text-align:left;
  font-family:inherit;
  color:var(--ink-mute);
  transition:background .12s, color .12s;
  position:relative;
}
.cat-btn:last-child{border-right:none}
.cat-btn:hover{background:var(--paper-deep);color:var(--ink-soft)}
.cat-btn.active{color:var(--ink);background:var(--paper-deep)}
.cat-btn.active::before{
  content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;
  background:var(--cat-c);
}
.cat-btn[data-cat=projects] {--cat-c:var(--cat-projects)}
.cat-btn[data-cat=tools]    {--cat-c:var(--cat-tools)}
.cat-btn[data-cat=platforms]{--cat-c:var(--cat-platforms)}
.cat-btn[data-cat=models]   {--cat-c:var(--cat-models)}
.cat-btn[data-cat=concepts] {--cat-c:var(--cat-concepts)}
.cat-btn[data-cat=people]   {--cat-c:var(--cat-people)}

.cat-btn .cat-kicker{
  font-family:var(--mono);
  font-size:10px;letter-spacing:0.14em;text-transform:uppercase;
  color:var(--ink-mute);display:block;margin-bottom:3px;
}
.cat-btn.active .cat-kicker{color:var(--cat-c)}
.cat-btn .cat-name{
  font-family:var(--serif);
  font-size:20px;line-height:1.1;font-weight:400;letter-spacing:-0.01em;
}
.cat-btn .cat-count{
  font-family:var(--mono);
  font-size:11px;color:var(--ink-mute);margin-top:2px;
}

/* ── Surges table ──────────────────────────────────────────── */
.surge-table{
  width:100%;border-collapse:collapse;
  font-family:var(--sans);
}
.surge-table th{
  text-align:left;
  font-family:var(--mono);font-weight:400;font-size:10.5px;
  letter-spacing:0.12em;text-transform:uppercase;color:var(--ink-mute);
  padding:8px 10px 8px 0;border-bottom:1px solid var(--ink);
}
.surge-table th.num{text-align:right}
.surge-table td{
  padding:13px 10px 13px 0;
  border-bottom:1px solid var(--rule);
  vertical-align:middle;
  font-size:14.5px;
}
.surge-table tr:last-child td{border-bottom:none}
.surge-table td.rank{
  font-family:var(--mono);font-size:11px;color:var(--ink-mute);width:34px;
  font-variant-numeric:tabular-nums;
}
.surge-table td.title{color:var(--ink);font-weight:500}
.surge-table td.title .badge-new{
  display:inline-block;
  font-family:var(--mono);font-size:9.5px;letter-spacing:0.1em;
  color:var(--blue);background:var(--blue-soft);
  padding:2px 6px;border-radius:2px;margin-left:8px;
  vertical-align:1px;text-transform:uppercase;
}
.surge-table td.spark{width:38%;padding-right:20px}
.spark-row{display:flex;align-items:center;gap:10px}
.spark-bar{
  height:8px;flex:1;background:var(--paper-deep);position:relative;border-radius:1px;
}
.spark-bar-prev{
  position:absolute;top:0;left:0;bottom:0;background:oklch(0% 0 0 / 0.12);border-radius:1px;
}
.spark-bar-now{
  position:absolute;top:0;left:0;bottom:0;background:var(--cat-c, var(--red));border-radius:1px;
}
.spark-bar.is-new .spark-bar-now{background:var(--blue)}
.surge-table td.delta{
  font-family:var(--mono);text-align:right;width:80px;
  font-variant-numeric:tabular-nums;font-size:13px;
  color:var(--red);font-weight:500;
}
.surge-table td.delta.new{color:var(--blue)}
.surge-table td.count{
  font-family:var(--mono);text-align:right;width:56px;
  font-variant-numeric:tabular-nums;font-size:13px;color:var(--ink);font-weight:500;
}

/* ── Bump chart (hot themes) ───────────────────────────────── */
.bump-wrap{position:relative;width:100%}
#bump-svg{display:block;width:100%;height:auto;overflow:visible}

.bump-legend{
  display:flex;flex-wrap:wrap;gap:2px 18px;
  font-family:var(--mono);font-size:11px;color:var(--ink-mute);
  margin-top:14px;
  padding-top:12px;border-top:1px solid var(--rule);
}
.bump-legend span b{color:var(--ink);font-weight:500}
.bump-legend .sw{
  display:inline-block;width:14px;height:2px;vertical-align:2px;margin-right:5px;
}

/* ── Tabbed granularity (segmented) ────────────────────────── */
.seg{
  display:inline-flex;border:1px solid var(--ink);border-radius:0;overflow:hidden;
  font-family:var(--mono);font-size:11px;letter-spacing:0.1em;text-transform:uppercase;
}
.seg button{
  background:transparent;border:none;padding:5px 11px;cursor:pointer;
  color:var(--ink-mute);font-family:inherit;font-size:inherit;letter-spacing:inherit;
  text-transform:inherit;border-right:1px solid var(--rule);
}
.seg button:last-child{border-right:none}
.seg button:hover{color:var(--ink)}
.seg button.active{background:var(--ink);color:var(--paper)}

/* ── Leaderboard (Top-15) ──────────────────────────────────── */
.leader-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 48px}
@media(max-width:700px){.leader-grid{grid-template-columns:1fr}}
.leader-row{
  display:grid;grid-template-columns:28px 1fr auto;gap:12px;
  padding:10px 0;border-bottom:1px solid var(--rule);
  align-items:baseline;
}
.leader-row:last-child{border-bottom:none}
.leader-rank{
  font-family:var(--mono);font-size:11px;color:var(--ink-mute);
  font-variant-numeric:tabular-nums;text-align:right;
  border-right:1px solid var(--rule);padding-right:10px;
}
.leader-row.top3 .leader-rank{color:var(--red);font-weight:600}
.leader-name{
  font-family:var(--sans);font-size:14.5px;color:var(--ink);font-weight:500;
  display:flex;align-items:baseline;gap:8px;min-width:0;
}
.leader-name > span:first-child{
  overflow-wrap:anywhere;word-break:break-word;max-width:100%;
}
.leader-name .dots{
  flex:1;border-bottom:1px dotted var(--rule);
  transform:translateY(-4px);min-width:20px;
}
.leader-count{
  font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-size:13px;color:var(--ink);font-weight:500;padding-left:4px;
}

/* ── Novelty: list of new entries with weekly sparks ──────── */
.novelty-list{display:grid;grid-template-columns:1fr;gap:0}
.novelty-row{
  display:grid;
  grid-template-columns:110px 1fr 200px 60px;
  gap:18px;
  padding:14px 0;
  border-bottom:1px solid var(--rule);
  align-items:center;
}
.novelty-row:last-child{border-bottom:none}
.novelty-when{
  font-family:var(--mono);font-size:11px;color:var(--ink-mute);letter-spacing:0.05em;
}
.novelty-name{
  font-family:var(--serif);font-size:20px;font-weight:400;color:var(--ink);
  letter-spacing:-0.01em;line-height:1.15;
}
.novelty-name em{font-style:italic;font-weight:300;color:var(--ink-mute);font-size:0.72em;margin-left:6px;letter-spacing:0;font-family:var(--sans)}
.novelty-spark{display:flex;gap:3px;align-items:flex-end;height:28px}
.novelty-spark .tick{
  flex:1;background:var(--paper-deep);min-height:2px;position:relative;border-radius:1px;
}
.novelty-spark .tick.live{background:var(--blue)}
.novelty-count{
  font-family:var(--mono);font-variant-numeric:tabular-nums;
  text-align:right;font-size:14px;color:var(--ink);font-weight:500;
}
@media(max-width:720px){
  .novelty-row{grid-template-columns:1fr auto;grid-template-rows:auto auto}
  .novelty-when{grid-row:1;font-size:10px}
  .novelty-count{grid-row:1;text-align:right}
  .novelty-name{grid-column:1/-1;grid-row:2;font-size:17px}
  .novelty-spark{grid-column:1/-1;grid-row:3;margin-top:6px}
}

/* ── Colophon ──────────────────────────────────────────────── */
.colophon{
  padding:36px 0 0;
  border-top:2px solid var(--ink);
  margin-top:24px;
  display:grid;grid-template-columns:1fr 1fr 1fr;gap:32px;
  font-family:var(--mono);font-size:11px;line-height:1.7;color:var(--ink-soft);
}
@media(max-width:720px){.colophon{grid-template-columns:1fr;gap:20px}}
.colophon h4{
  font-family:var(--serif);font-weight:400;font-style:italic;
  font-size:15px;color:var(--ink);margin-bottom:6px;letter-spacing:-0.01em;
}
.colophon code{background:var(--paper-deep);padding:1px 5px;border-radius:2px}

/* Accent variants (изменяются через data-accent на <body>) */
body[data-accent=crimson]{--red:oklch(52% 0.17 25);--red-soft:oklch(52% 0.17 25 / 0.1)}
body[data-accent=forest] {--red:oklch(45% 0.12 155);--red-soft:oklch(45% 0.12 155 / 0.1)}
body[data-accent=ink]    {--red:oklch(25% 0.02 260);--red-soft:oklch(25% 0.02 260 / 0.08)}

body[data-density=spacious] .section{padding:56px 0 10px}
body[data-density=spacious] .lede{padding:56px 0 44px}

body[data-serif=fraunces]   {--serif:"Fraunces", Georgia, serif}
body[data-serif=instrument] {--serif:"Instrument Serif", Georgia, serif}

@media print{.catbar-wrap{position:static}}
</style>
</head>
<body data-accent="crimson" data-density="normal" data-serif="fraunces">

<div class="shell">

  <!-- ══════ MASTHEAD ══════ -->
  <header class="masthead">
    <div class="masthead-left">
      <div class="eyebrow"><span class="dot"></span> AI Pulse · Weekly Briefing</div>
      <h1 class="wordmark">Что обсуждает <em>AI-сообщество</em><br>прямо сейчас.</h1>
    </div>
    <div class="masthead-right">
      <div>№ <b id="issue-no">—</b> · <b id="issue-date">—</b></div>
      <div>Окно <b id="window-range">—</b></div>
      <div><b id="entity-count">—</b> сущностей / <b id="msg-count">—</b> сообщений</div>
    </div>
  </header>

  <!-- ══════ DATELINE ══════ -->
  <div class="dateline">
    <span>Источник: <b>Telegram</b></span>
    <span>Каналы: <b>Поляков считает и болтает</b>, <b>Чатик мыслителей</b>, <b>Чат Kovalskii</b>, <b>Промптинг</b></span>
    <span>Неделя <b id="dl-week">—</b></span>
    <span>Пред. <b id="dl-prev">—</b></span>
  </div>

  <!-- ══════ LEDE ══════ -->
  <section class="lede">
    <div class="lede-tag">Pulse<br>0419</div>
    <p class="lede-body" id="lede-body">—</p>
  </section>

  <!-- ══════ METER ══════ -->
  <section class="meter" id="meter"></section>

  <!-- ══════ CATEGORY TABS (sticky) ══════ -->
  <div class="catbar-wrap">
    <nav class="catbar" id="catbar"></nav>
  </div>

  <!-- § 01 — Surges -->
  <section class="section" id="sec-surges">
    <div class="section-head">
      <div class="section-num">§ 01</div>
      <h2 class="section-title">Всплески недели <em id="surge-cat-label">—</em></h2>
      <div class="section-tools" id="surge-week">—</div>
      <p class="section-desc">Сущности с наибольшим приростом упоминаний за последнюю неделю относительно предыдущей. Тёмная полоса — прошлая неделя, цветная — текущая.</p>
    </div>
    <div id="surge-table-wrap"></div>
  </section>

  <!-- § 02 — Bump / Hot themes -->
  <section class="section" id="sec-hot">
    <div class="section-head">
      <div class="section-num">§ 02</div>
      <h2 class="section-title">Траектории топ-тем <em id="hot-cat-label">—</em></h2>
      <div class="section-tools">
        <div class="seg" id="gran-btns">
          <button class="active" data-gran="week">Нед.</button>
          <button data-gran="month">Месяц</button>
        </div>
      </div>
      <p class="section-desc">Как меняется ранг каждой темы от периода к периоду. Линии, поднимающиеся к верху, — темы, забирающиеся в топ. Точка = ранг в столбце, размер = доля упоминаний этой темы в столбце.</p>
    </div>
    <div class="bump-wrap">
      <svg id="bump-svg" viewBox="0 0 1180 620" preserveAspectRatio="xMidYMid meet"></svg>
    </div>
    <div class="bump-legend">
      <span><span class="sw" style="background:var(--red)"></span><b>Жирная линия</b> — тема продержалась весь период</span>
      <span><span class="sw" style="background:var(--blue);height:2px;border-top:1px dashed var(--blue)"></span><b>Пунктир</b> — появилась впервые</span>
      <span><b>Круг</b> — ранг и масса упоминаний</span>
    </div>
  </section>

  <!-- § 03 — Leaderboard -->
  <section class="section" id="sec-top">
    <div class="section-head">
      <div class="section-num">§ 03</div>
      <h2 class="section-title">Таблица рекордов <em id="top-cat-label">—</em></h2>
      <div class="section-tools">за всё окно</div>
      <p class="section-desc">Абсолютный рейтинг за всё окно данных. Топ-3 отмечены акцентным цветом.</p>
    </div>
    <div class="leader-grid" id="leader-grid"></div>
  </section>

  <!-- § 04 — Novelty -->
  <section class="section" id="sec-novelty">
    <div class="section-head">
      <div class="section-num">§ 04</div>
      <h2 class="section-title">Впервые замечено <em id="novelty-cat-label">—</em></h2>
      <div class="section-tools">хронология</div>
      <p class="section-desc">Сущности, о которых на конкретной неделе заговорили впервые. Точки справа — по какой неделе пришло упоминание.</p>
    </div>
    <div id="novelty-list" class="novelty-list"></div>
  </section>

  <!-- Colophon -->
  <footer class="colophon">
    <div>
      <h4>О выпуске</h4>
      Еженедельный отчёт о темах, которые обсуждает русскоязычное AI-комьюнити в Telegram. Автоматическая агрегация сущностей из сообщений с последующей нормализацией.
    </div>
    <div>
      <h4>Данные</h4>
      Окно: <span id="col-window">—</span><br>
      Сущностей: <span id="col-entities">—</span><br>
      Сообщений: <span id="col-msgs">—</span><br>
      Последняя точка: <span id="col-last">—</span>
    </div>
    <div>
      <h4>Сборка</h4>
      Собрано: <span id="col-built">—</span>
    </div>
  </footer>
</div>

<script>window.DATA = __DATA_JSON__;</script>
<script>__APP_JS__</script>
</body>
</html>
"""

# app.js из Claude Design, заинлайненный. Tweaks-панель и editmode-IPC
# убраны — они нужны только внутри Claude Design iframe.
_APP_JS = r"""
(function(){
'use strict';

const D = window.DATA;
const CAT_META = {
  projects:  {label:"Инициативы",   kicker:"§ Initiatives", var:"--cat-projects"},
  tools:     {label:"Инструменты", kicker:"§ Tools",     var:"--cat-tools"},
  platforms: {label:"Платформы",   kicker:"§ Platforms", var:"--cat-platforms"},
  models:    {label:"Модели",      kicker:"§ Models",    var:"--cat-models"},
  concepts:  {label:"Концепции",   kicker:"§ Concepts",  var:"--cat-concepts"},
  people:    {label:"Люди",        kicker:"§ People",    var:"--cat-people"}
};
const CAT_ORDER = ["tools","models","platforms","concepts","people","projects"];
const CAT_COLOR = {
  projects: "oklch(52% 0.14 50)",
  tools:    "oklch(48% 0.09 245)",
  platforms:"oklch(55% 0.11 200)",
  models:   "oklch(58% 0.12 80)",
  concepts: "oklch(46% 0.12 300)",
  people:   "oklch(50% 0.10 165)"
};

let currentCat  = "tools";
let currentGran = "week";

// ── Format ───────────────────────────────────────────────────────────
const MONTHS      = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"];
const MONTHS_FULL = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
function fmtDate(s){const d=new Date(s);return `${d.getDate()} ${MONTHS[d.getMonth()]}`}
function fmtWeek(s){const d=new Date(s),e=new Date(d);e.setDate(d.getDate()+6);return fmtDate(s)+"–"+fmtDate(e.toISOString().slice(0,10))}
function fmtMonth(s){return new Date(s+"-01").toLocaleDateString("ru-RU",{month:"short"})}
function fmtFullDate(s){const d=new Date(s);return `${d.getDate()} ${MONTHS_FULL[d.getMonth()]} ${d.getFullYear()}`}

function el(tag, cls, text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function setText(id,t){const e=document.getElementById(id);if(e)e.textContent=t}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m]))}

// ── Masthead / dateline / colophon ──────────────────────────────────
function fillChrome(){
  const m = D.meta, h = D.hero_stats;
  const d = new Date(m.data_last_date);
  const onejan = new Date(d.getFullYear(),0,1);
  const week = Math.ceil((((d - onejan)/86400000) + onejan.getDay()+1)/7);
  setText("issue-no", String(week).padStart(2,"0"));
  setText("issue-date", fmtFullDate(m.generated_at.slice(0,10)));
  setText("window-range", m.since + " → " + m.data_last_date);
  setText("entity-count", m.total_entities.toLocaleString("ru-RU"));
  setText("msg-count", m.total_messages.toLocaleString("ru-RU"));
  setText("dl-week", h && h.week ? fmtWeek(h.week) : "—");
  setText("dl-prev", h && h.prev_week ? fmtWeek(h.prev_week) : "—");
  setText("col-window", m.since + " → " + m.data_last_date);
  setText("col-entities", m.total_entities.toLocaleString("ru-RU"));
  setText("col-msgs", m.total_messages.toLocaleString("ru-RU"));
  setText("col-last", m.data_last_date);
  setText("col-built", m.generated_at);
}

// ── Lede ────────────────────────────────────────────────────────────
function renderLede(){
  const h = D.hero_stats;
  if (!h || !h.week) return;
  const g = h.top_gainer;
  const gMeta = g ? CAT_META[g.category] : null;
  const gCat = g ? gMeta.label.toLowerCase() : "";

  let domCat = null, domN = 0;
  for (const c of CAT_ORDER){
    const n = h.new.by_cat[c]||0;
    if (n > domN){domN=n;domCat=c}
  }
  const domLabel = domCat ? CAT_META[domCat].label.toLowerCase() : "";

  const lede = document.getElementById("lede-body");
  lede.innerHTML = `На этой неделе в эфире <span class="num blue">${h.new.total}</span> новых сущностей — больше всего среди <em>${domLabel}</em> (${domN}). <span class="hl">${h.rising.total}</span> тем <span class="hl">растут</span>, <span class="num grey">${h.falling.total}</span> угасают.` +
    (g ? ` Главный всплеск — <em>${escapeHtml(g.title)}</em> ${g.isNew ? '<span class="num blue">впервые</span>' : `<span class="num">+${g.delta}</span>`} (${g.count} упоминаний, ${gCat}).` : "");
}

// ── Meter ────────────────────────────────────────────────────────────
function renderMeter(){
  const h = D.hero_stats;
  const meter = document.getElementById("meter");
  meter.innerHTML = "";
  if (!h || !h.week){ meter.appendChild(el("div","meter-cell","Нет данных")); return; }

  // Расшифровка, что означает каждый большой KPI — показывается в title
  // при наведении на ячейку. Разбивка «инст. 14» тоже получает тултип с
  // полным названием категории + счётом.
  const TONE_DESC = {
    new:     "сущностей впервые появилось на этой неделе",
    rising:  "сущностей выросли по упоминаниям (delta>0 к прошлой неделе)",
    falling: "сущностей упали по упоминаниям (delta<0 к прошлой неделе)",
  };
  function breakdown(byCat, total, tone){
    const wrap = el("div","meter-breakdown");
    const verb = tone === "new" ? "новых" : (tone === "rising" ? "растущих" : "падающих");
    for (const c of CAT_ORDER){
      const n = byCat[c]||0;
      if (!n) continue;
      const bd = el("span","bd");
      bd.style.setProperty("--bd-c", CAT_COLOR[c]);
      bd.textContent = `${CAT_META[c].label.slice(0,4).toLowerCase()}. ${n}`;
      bd.title = `${CAT_META[c].label}: ${n} из ${total} ${verb}`;
      wrap.appendChild(bd);
    }
    return wrap;
  }
  function cell(tone, label, glyph, val, extra){
    const c = el("div","meter-cell");
    c.dataset.tone = tone;
    if (TONE_DESC[tone]) c.title = `${val} ${TONE_DESC[tone]}`;
    const lbl = el("div","meter-label");
    lbl.appendChild(el("span","",label));
    lbl.appendChild(el("span","glyph",glyph));
    c.appendChild(lbl);
    c.appendChild(el("div","meter-value", val));
    if (extra) c.appendChild(extra);
    return c;
  }
  meter.appendChild(cell("new",    "Новинок", "N", h.new.total,    breakdown(h.new.by_cat,    h.new.total,    "new")));
  meter.appendChild(cell("rising", "Растут",  "↗", h.rising.total, breakdown(h.rising.by_cat, h.rising.total, "rising")));
  meter.appendChild(cell("falling","Падают",  "↘", h.falling.total,breakdown(h.falling.by_cat,h.falling.total,"falling")));

  const g = h.top_gainer;
  const gc = el("div","meter-cell");
  gc.dataset.tone = "gainer";
  const lbl = el("div","meter-label");
  lbl.appendChild(el("span","","Всплеск №1"));
  lbl.appendChild(el("span","glyph","★"));
  gc.appendChild(lbl);
  if (g){
    gc.appendChild(el("div","meter-value small", g.title));
    const sub = el("div","meter-sub");
    const meta = CAT_META[g.category];
    sub.innerHTML = `${meta.label.toLowerCase()} · ${g.count} упом. · ` +
      (g.isNew ? `<span class="delta new">впервые</span>` : `<span class="delta">+${g.delta} нед/нед</span>`);
    gc.appendChild(sub);
  } else {
    gc.appendChild(el("div","meter-value","—"));
  }
  meter.appendChild(gc);
}

// ── Category tabs ────────────────────────────────────────────────────
function renderCatbar(){
  const bar = document.getElementById("catbar");
  bar.innerHTML = "";
  CAT_ORDER.forEach(c=>{
    const meta = CAT_META[c];
    const btn = el("button","cat-btn"+(c===currentCat?" active":""));
    btn.dataset.cat = c;
    btn.innerHTML = `
      <span class="cat-kicker">${meta.kicker}</span>
      <span class="cat-name">${meta.label}</span>
      <span class="cat-count">${(D.total[c]||[]).length} сущностей</span>`;
    btn.addEventListener("click", ()=>{
      currentCat = c;
      document.querySelectorAll(".cat-btn").forEach(b=>b.classList.toggle("active", b.dataset.cat===c));
      document.documentElement.style.setProperty("--cat-c", CAT_COLOR[c]);
      renderCategory();
    });
    bar.appendChild(btn);
  });
  document.documentElement.style.setProperty("--cat-c", CAT_COLOR[currentCat]);
}

// ── Surges table ─────────────────────────────────────────────────────
function renderSurges(){
  const cs = D.category_surges[currentCat] || {week:null, items:[]};
  const meta = CAT_META[currentCat];
  setText("surge-cat-label", "— " + meta.label.toLowerCase());
  setText("surge-week", cs.week ? fmtWeek(cs.week) : "—");

  const wrap = document.getElementById("surge-table-wrap");
  wrap.innerHTML = "";
  if (!cs.items.length){ wrap.appendChild(el("p","section-desc","Нет всплесков в этой категории.")); return; }
  const items = cs.items.slice(0, 12);
  const maxC = Math.max(...items.map(x=>x.count));

  const table = el("table","surge-table");
  table.innerHTML = `<thead><tr>
    <th>#</th><th>Сущность</th><th>Нед. / нед.</th>
    <th class="num">Δ</th><th class="num">Упом.</th>
  </tr></thead>`;
  const tb = el("tbody");
  items.forEach((x,i)=>{
    const row = el("tr");
    const prev = x.isNew ? 0 : Math.max(0, x.count - x.delta);
    const pctNow  = Math.max(4, Math.round(x.count/maxC*100));
    const pctPrev = Math.max(0, Math.round(prev/maxC*100));
    row.innerHTML = `
      <td class="rank">${String(i+1).padStart(2,"0")}</td>
      <td class="title">${escapeHtml(x.title)}${x.isNew?'<span class="badge-new">NEW</span>':''}</td>
      <td class="spark">
        <div class="spark-row">
          <div class="spark-bar ${x.isNew?'is-new':''}">
            <div class="spark-bar-prev" style="width:${pctPrev}%"></div>
            <div class="spark-bar-now" style="width:${pctNow}%;background:${x.isNew?'var(--blue)':CAT_COLOR[currentCat]}"></div>
          </div>
        </div>
      </td>
      <td class="delta ${x.isNew?'new':''}">${x.isNew?'new':'+'+x.delta}</td>
      <td class="count">${x.count}</td>`;
    tb.appendChild(row);
  });
  table.appendChild(tb);
  wrap.appendChild(table);
}

// ── Bump chart (rank over time) ──────────────────────────────────────
function renderBump(){
  const key = currentGran === "week" ? "stacked_week" : "stacked_month";
  const sd = D[key][currentCat];
  setText("hot-cat-label", "— " + CAT_META[currentCat].label.toLowerCase());
  const svg = document.getElementById("bump-svg");
  svg.innerHTML = "";

  const periods = sd.periods;
  const N = periods.length;
  const R = sd.ranks.length;

  const tracks = new Map();
  for (let r = 0; r < R; r++){
    const rank = sd.ranks[r];
    for (let pi = 0; pi < N; pi++){
      const title = rank.titles[pi];
      const count = rank.counts[pi];
      if (!title || !count) continue;
      const tag = (rank.tags && rank.tags[pi]) || {};
      if (!tracks.has(title)) tracks.set(title, []);
      tracks.get(title).push({pi, rank:r, count, isNew:!!tag.isNew, delta:tag.delta||0});
    }
  }

  const W = 1180, H = 620;
  const padL = 48, padR = 220, padT = 30, padB = 34;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const xStep = N > 1 ? plotW / (N - 1) : 0;
  const yStep = plotH / (R - 1 || 1);
  function xOf(pi){return padL + pi * xStep}
  function yOf(rank){return padT + rank * yStep}

  let maxCount = 0;
  tracks.forEach(t=>t.forEach(p=>{if(p.count>maxCount)maxCount=p.count}));
  function rOf(c){return 3 + Math.sqrt(c/maxCount) * 9}

  const NS = "http://www.w3.org/2000/svg";

  for (let pi = 0; pi < N; pi++){
    const x = xOf(pi);
    const line = document.createElementNS(NS,"line");
    line.setAttribute("x1",x);line.setAttribute("x2",x);
    line.setAttribute("y1",padT-8);line.setAttribute("y2",padT+plotH+8);
    line.setAttribute("stroke","oklch(82% 0.015 80)");
    line.setAttribute("stroke-width",1);
    svg.appendChild(line);

    // Лёгкая «решётка» 15 рангов — показывает, что слот предусмотрен даже
    // если в нём нет данных (так видно, где просто не набралось тем).
    for (let ri = 0; ri < R; ri++){
      const dot = document.createElementNS(NS,"circle");
      dot.setAttribute("cx", x);
      dot.setAttribute("cy", yOf(ri));
      dot.setAttribute("r", 1.5);
      dot.setAttribute("fill", "oklch(82% 0.015 80)");
      svg.appendChild(dot);
    }

    const lbl = document.createElementNS(NS,"text");
    lbl.setAttribute("x",x);lbl.setAttribute("y",padT+plotH+22);
    lbl.setAttribute("text-anchor","middle");
    lbl.setAttribute("font-family","JetBrains Mono, monospace");
    lbl.setAttribute("font-size","10.5");
    lbl.setAttribute("letter-spacing","0.04em");
    lbl.setAttribute("fill","oklch(38% 0.012 260)");
    lbl.textContent = currentGran === "week" ? fmtWeek(periods[pi]) : fmtMonth(periods[pi]);
    svg.appendChild(lbl);
  }

  [0,4,9,14].forEach(ri=>{
    if (ri >= R) return;
    const y = yOf(ri);
    const lbl = document.createElementNS(NS,"text");
    lbl.setAttribute("x",padL-14);lbl.setAttribute("y",y+4);
    lbl.setAttribute("text-anchor","end");
    lbl.setAttribute("font-family","JetBrains Mono, monospace");
    lbl.setAttribute("font-size","10");
    lbl.setAttribute("fill","oklch(55% 0.01 260)");
    lbl.textContent = "#" + (ri+1);
    svg.appendChild(lbl);
  });

  const catHueMap = {projects:50, tools:245, platforms:200, models:80, concepts:300, people:165};
  const baseHue = catHueMap[currentCat];
  function colorFor(title, isNewTrack){
    let h = 0;
    for (let i = 0; i < title.length; i++) h = ((h<<5)-h) + title.charCodeAt(i);
    const hue = ((Math.abs(h) % 80) - 40) + baseHue;
    const L = isNewTrack ? 55 : 48;
    const C = isNewTrack ? 0.09 : 0.13;
    return `oklch(${L}% ${C} ${hue})`;
  }

  const trackArr = [...tracks.entries()].map(([title, pts])=>{
    pts.sort((a,b)=>a.pi-b.pi);
    const totalCount = pts.reduce((s,p)=>s+p.count,0);
    const everNew    = pts.some(p=>p.isNew);
    return {title, pts, totalCount, everNew};
  });
  trackArr.sort((a,b)=>a.totalCount-b.totalCount);

  trackArr.forEach(tr=>{
    if (tr.pts.length < 2){
      const p = tr.pts[0];
      const c = document.createElementNS(NS,"circle");
      c.setAttribute("cx",xOf(p.pi));
      c.setAttribute("cy",yOf(p.rank));
      c.setAttribute("r",rOf(p.count));
      c.setAttribute("fill", p.isNew ? "oklch(48% 0.09 245)" : colorFor(tr.title, false));
      c.setAttribute("fill-opacity","0.55");
      c.setAttribute("stroke", p.isNew ? "oklch(48% 0.09 245)" : colorFor(tr.title, false));
      c.setAttribute("stroke-width","1");
      svg.appendChild(c);
      return;
    }
    let d = "";
    tr.pts.forEach((p,i)=>{
      const x = xOf(p.pi), y = yOf(p.rank);
      if (i === 0){ d += `M ${x} ${y}`; }
      else {
        const prev = tr.pts[i-1];
        const px = xOf(prev.pi), py = yOf(prev.rank);
        const cx1 = px + (x-px)/2, cx2 = cx1;
        d += ` C ${cx1} ${py} ${cx2} ${y} ${x} ${y}`;
      }
    });
    const path = document.createElementNS(NS,"path");
    path.setAttribute("d",d);
    path.setAttribute("fill","none");
    const col = colorFor(tr.title, tr.everNew);
    path.setAttribute("stroke", col);
    const sw = 1 + Math.sqrt(tr.totalCount/maxCount)*2;
    path.setAttribute("stroke-width", sw);
    path.setAttribute("stroke-linecap","round");
    path.setAttribute("stroke-linejoin","round");
    if (tr.everNew) path.setAttribute("stroke-dasharray","5 4");
    path.setAttribute("opacity", tr.totalCount > 3 ? "0.9" : "0.55");
    svg.appendChild(path);

    tr.pts.forEach(p=>{
      const c = document.createElementNS(NS,"circle");
      c.setAttribute("cx",xOf(p.pi));
      c.setAttribute("cy",yOf(p.rank));
      c.setAttribute("r",rOf(p.count));
      c.setAttribute("fill","var(--paper)");
      c.setAttribute("stroke", col);
      c.setAttribute("stroke-width","1.5");
      svg.appendChild(c);
    });
  });

  // ── Labels — каждая точка в каждом столбце получает подпись.
  // Правая колонка (последняя неделя): подписи справа от точки.
  // Остальные столбцы: подписи сверху от точки (над окружностью).
  // Для каждой колонки — отдельный пас с разрешением вертикальных коллизий.
  const labelsByCol = Array.from({length:N}, ()=>[]);
  trackArr.forEach(tr=>{
    tr.pts.forEach(p=>{
      const placeRight = p.pi === N-1;
      const t = document.createElementNS(NS,"text");
      t.setAttribute("font-family","Fraunces, Georgia, serif");
      t.setAttribute("fill","oklch(20% 0.015 260)");
      const shortName = tr.title.length > 22 ? tr.title.slice(0,21)+"…" : tr.title;
      t.textContent = shortName;
      if (placeRight){
        t.setAttribute("x", xOf(p.pi) + 10);
        t.setAttribute("y", yOf(p.rank) + 3.5);
        t.setAttribute("font-size", 13);
        t.setAttribute("font-weight", "500");
        t.setAttribute("text-anchor","start");
      } else {
        t.setAttribute("x", xOf(p.pi));
        t.setAttribute("y", yOf(p.rank) - rOf(p.count) - 4);
        t.setAttribute("font-size", 10.5);
        t.setAttribute("font-weight", "400");
        t.setAttribute("text-anchor","middle");
        t.setAttribute("opacity", tr.totalCount >= 3 ? "0.72" : "0.48");
      }
      svg.appendChild(t);
      labelsByCol[p.pi].push({
        t,
        rank: p.rank,
        count: p.count,
        y: +t.getAttribute("y"),
        placeRight
      });
    });
  });

  // Разрешение коллизий по каждой колонке.
  // Правая колонка — классический вертикальный stacking по 16px.
  // Средние — если соседние ранги идут подряд, нижнюю подпись смещаем ПОД точку.
  const minGap = 14;

  const rights = labelsByCol[N-1].slice().sort((a,b)=>a.y-b.y);
  for (let i = 1; i < rights.length; i++){
    const prev = rights[i-1], cur = rights[i];
    if (cur.y - prev.y < minGap){
      cur.y = prev.y + minGap;
      cur.t.setAttribute("y", cur.y);
    }
  }

  for (let pi = 0; pi < N - 1; pi++){
    const col = labelsByCol[pi].slice().sort((a,b)=>a.rank-b.rank);
    for (let i = 0; i < col.length; i++){
      const cur = col[i];
      const prev = i > 0 ? col[i-1] : null;
      if (prev && Math.abs(cur.rank - prev.rank) <= 1){
        const newY = yOf(cur.rank) + rOf(cur.count) + 11;
        cur.t.setAttribute("y", newY);
        cur.y = newY;
      }
    }
  }
}

// ── Leaderboard ──────────────────────────────────────────────────────
function renderLeader(){
  const items = (D.total[currentCat]||[]).slice(0, 16);
  const grid = document.getElementById("leader-grid");
  const meta = CAT_META[currentCat];
  setText("top-cat-label", "— " + meta.label.toLowerCase());
  grid.innerHTML = "";
  if (!items.length){ grid.appendChild(el("p","section-desc","Нет данных.")); return; }
  items.forEach(([name, count], i)=>{
    const row = el("div","leader-row"+(i<3?" top3":""));
    row.innerHTML = `
      <div class="leader-rank">${String(i+1).padStart(2,"0")}</div>
      <div class="leader-name"><span>${escapeHtml(name)}</span><span class="dots"></span></div>
      <div class="leader-count">${count}</div>`;
    grid.appendChild(row);
  });
}

// ── Novelty list ─────────────────────────────────────────────────────
function renderNovelty(){
  const sd = D.novelty_stacked[currentCat];
  setText("novelty-cat-label", "— " + CAT_META[currentCat].label.toLowerCase());
  const list = document.getElementById("novelty-list");
  list.innerHTML = "";
  if (!sd){ list.appendChild(el("p","section-desc","Нет данных.")); return; }

  const entries = [];
  const N = sd.periods.length;
  sd.ranks.forEach(rank=>{
    for (let pi = 0; pi < N; pi++){
      const t = rank.titles[pi]; const c = rank.counts[pi];
      if (!t || !c) continue;
      const tag = (rank.tags||[])[pi]||{};
      if (tag.isNew) entries.push({title:t, pi, count:c});
    }
  });
  const byTitle = new Map();
  entries.forEach(e=>{
    const prev = byTitle.get(e.title);
    if (!prev || prev.pi < e.pi) byTitle.set(e.title, e);
  });
  const rows = [...byTitle.values()].sort((a,b)=>b.pi-a.pi || b.count-a.count).slice(0, 16);
  if (!rows.length){ list.appendChild(el("p","section-desc","На этой выборке новинок не зафиксировано.")); return; }

  const meta = CAT_META[currentCat];
  rows.forEach(r=>{
    const when = fmtWeek(sd.periods[r.pi]);
    const row = el("div","novelty-row");
    const spark = el("div","novelty-spark");
    for (let pi = 0; pi < N; pi++){
      const tick = el("div","tick"+(pi===r.pi?" live":""));
      const h = pi === r.pi ? 28 : (pi > r.pi ? 8 : 3);
      tick.style.height = h + "px";
      spark.appendChild(tick);
    }
    row.innerHTML = `
      <div class="novelty-when">${when.toUpperCase()}</div>
      <div class="novelty-name">${escapeHtml(r.title)} <em>${meta.label.toLowerCase()}</em></div>`;
    row.appendChild(spark);
    row.appendChild(el("div","novelty-count", String(r.count)));
    list.appendChild(row);
  });
}

// ── Orchestration ────────────────────────────────────────────────────
function renderCategory(){ renderSurges(); renderBump(); renderLeader(); renderNovelty(); }

function wireGran(){
  document.querySelectorAll("#gran-btns button").forEach(b=>{
    b.addEventListener("click",()=>{
      document.querySelectorAll("#gran-btns button").forEach(x=>x.classList.remove("active"));
      b.classList.add("active");
      currentGran = b.dataset.gran;
      renderBump();
    });
  });
}

function boot(){
  fillChrome();
  renderLede();
  renderMeter();
  renderCatbar();
  renderCategory();
  wireGran();
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();

let rz;
window.addEventListener("resize", ()=>{clearTimeout(rz);rz=setTimeout(renderBump,200)});

})();
"""


def render_html(data: dict) -> str:
    """
    Генерирует самодостаточный HTML (editorial «Weekly Briefing»).
    Данные и app.js инлайнятся — один файл, никаких внешних ресурсов кроме
    Google Fonts CDN (шрифты Fraunces / Geist / JetBrains Mono).
    """
    js_data = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")
    return (
        _HTML_TEMPLATE
        .replace("__DATA_JSON__", js_data)
        .replace("__APP_JS__", _APP_JS)
    )
