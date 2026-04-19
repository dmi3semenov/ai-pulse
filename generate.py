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
CATEGORIES = ["concepts", "entities", "people", "projects"]

# Иконки и цвета категорий — используются в UI и передаются в JS.
CATEGORY_META = {
    "concepts": {"icon": "💡", "label": "Концепции",  "color": "#a78bfa"},
    "entities": {"icon": "🛠", "label": "Инструменты","color": "#60a5fa"},
    "people":   {"icon": "👤", "label": "Люди",       "color": "#34d399"},
    "projects": {"icon": "🚀", "label": "Проекты",    "color": "#fb923c"},
}

# Цветовая палитра для сегментов стекового графика («горячие темы», топ-6).
PALETTE = [
    "#818cf8", "#34d399", "#fb923c", "#f472b6", "#fbbf24",
    "#38bdf8", "#a3e635", "#e879f9", "#2dd4bf", "#f87171",
]

# Окно истории: считаем данные, начиная с этой даты.
SINCE = "2026-03-01"

# Сколько последних недель показывать в heatmap.
HEATMAP_WEEKS = 8

# Пути по умолчанию.
ALIASES_PATH = Path(__file__).parent / "aliases.yml"


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

def load_wiki_entities(wiki_pages_dir: str, aliases: dict[str, str] | None = None) -> list[dict]:
    """
    Читает все .md файлы из wiki/pages/{category}/.
    Нормализует title через aliases и объединяет страницы с одинаковым каноническим
    именем в одну сущность (source_ids склеиваются без дубликатов).

    Возвращает список словарей: {category, title, source_ids}
    """
    aliases = aliases or {}

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
            raw_entries.append((cat, normalize_title(title, aliases), list(source_ids)))

    # Второй проход: схлопываем одноимённые (после нормализации) wiki-страницы.
    merged: dict[tuple[str, str], set[int]] = defaultdict(set)
    for cat, title, ids in raw_entries:
        merged[(cat, title)].update(ids)

    return [
        {"category": cat, "title": title, "source_ids": sorted(ids)}
        for (cat, title), ids in merged.items()
    ]


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


# ── Построение stacked-серий (для графика «горячие темы», топ-6) ────────────

def make_stacked(cat: str, agg: dict, granularity: str) -> dict:
    """
    Данные для stacked bar chart по заданной гранулярности (week / month).
    Оставлен только вариант «горячие темы»: топ-6 без «Остальных», с подписями
    внутри столбиков. Датасеты упорядочены от самой частой сущности к менее
    частой — в Chart.js первая серия рисуется снизу, поэтому визуально
    наиболее обсуждаемая тема окажется у оси X.
    """
    by_time = agg[granularity]
    min_key = SINCE[:7] if granularity == "month" else SINCE
    periods = sorted(k for k in by_time[cat] if k >= min_key)

    top6 = [t for t, _ in agg["total"][cat].most_common(6)]
    series = {
        ent: [by_time[cat][p].get(ent, 0) for p in periods]
        for ent in top6
    }

    return {
        "periods":      periods,
        "hot_entities": top6,
        "hot_series":   series,
    }


# ── Heatmap (топ-15 × последние N недель) ───────────────────────────────────

def make_heatmap(cat: str, agg: dict, top_n: int = 15, weeks_limit: int = HEATMAP_WEEKS) -> dict:
    """
    Строит heatmap-данные для категории: top_n сущностей × последние weeks_limit
    недель. Сущности берутся по суммарным упоминаниям за весь окно (total).
    max — максимальное значение по матрице (для нормализации интенсивности цвета).
    """
    weeks = sorted(k for k in agg["week"][cat] if k >= SINCE)
    weeks = weeks[-weeks_limit:]

    top = [t for t, _ in agg["total"][cat].most_common(top_n)]

    matrix = [
        [agg["week"][cat][w].get(t, 0) for w in weeks]
        for t in top
    ]
    flat = [v for row in matrix for v in row]
    return {
        "weeks":    weeks,
        "entities": top,
        "matrix":   matrix,
        "max":      max(flat) if flat else 1,
    }


# ── Unified surges (всплески всех 4 категорий одним блоком) ─────────────────

def make_unified_surges(agg: dict, max_items: int = 40) -> dict:
    """
    Всплески за последнюю неделю по всем категориям в одном списке.
    Правила попадания в блок:
      - delta > 0 (рост относительно предыдущей недели)  ИЛИ
      - isNew = True (сущность не упоминалась на прошлой неделе)
    Сортировка: по delta ↓, при равенстве — по абсолютному count ↓.
    Каждая запись тегируется категорией, чтобы в UI нарисовать чипу нужного
    цвета и с нужной иконкой.
    """
    # Собираем все недели по всем категориям, чтобы найти реальную «последнюю».
    all_weeks: set[str] = set()
    for cat in CATEGORIES:
        all_weeks.update(w for w in agg["week"][cat] if w >= SINCE)
    weeks = sorted(all_weeks)
    if not weeks:
        return {"week": None, "prev_week": None, "items": []}

    last = weeks[-1]
    prev = weeks[-2] if len(weeks) >= 2 else None

    items: list[dict] = []
    for cat in CATEGORIES:
        cur_map  = agg["week"][cat].get(last, {})
        prev_map = agg["week"][cat].get(prev, {}) if prev else {}
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
    return {
        "week":      last,
        "prev_week": prev,
        "items":     items[:max_items],
    }


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

    print("📚 Загружаю wiki-сущности...")
    entities = load_wiki_entities(wiki_pages_dir, aliases)
    print(f"   {len(entities)} сущностей (после схлопывания алиасов)")

    print("🔢 Агрегирую данные...")
    agg = aggregate(entities, id_to_date)

    def top_n(counter, n=60):
        return [[t, c] for t, c in counter.most_common(n)]

    # Топ-листы за весь период (для правой колонки + облака слов).
    total_data = {cat: top_n(agg["total"][cat]) for cat in CATEGORIES}

    # Stacked «горячие темы» — недели + месяцы.
    stacked_week  = {cat: make_stacked(cat, agg, "week")  for cat in CATEGORIES}
    stacked_month = {cat: make_stacked(cat, agg, "month") for cat in CATEGORIES}

    # Heatmap по каждой категории.
    heatmap = {cat: make_heatmap(cat, agg) for cat in CATEGORIES}

    # Единый блок всплесков.
    unified_surges = make_unified_surges(agg)

    return {
        "meta": {
            "total_entities": len(entities),
            "total_messages": len(id_to_date),
            "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M"),
            "since":          SINCE,
        },
        "categories":     CATEGORY_META,
        "total":          total_data,
        "stacked_week":   stacked_week,
        "stacked_month":  stacked_month,
        "heatmap":        heatmap,
        "unified_surges": unified_surges,
    }


# ── HTML-шаблон ─────────────────────────────────────────────────────────────

def render_html(data: dict) -> str:
    """
    Генерирует самодостаточный HTML: встраивает data как JS-переменную DATA,
    Chart.js и wordcloud2.js подключаются с cdnjs (CDN).

    Layout v2 (Trend Radar):
      [A] Unified surges  — главный блок, все 4 категории в одном списке
      [Tabs]              — переключатель категорий (общий для B, C, D)
      [B] Heatmap         — топ-15 × последние 8 недель
      [C] Stacked hot     — топ-6 с подписями, неделя/месяц
      [D] Top-14 list     — рейтинг за всё время
      [E] Word cloud      — опциональный низ страницы
    """
    # Безопасная сериализация: экранируем </script> внутри JSON.
    js_data    = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")
    palette_js = json.dumps(PALETTE)

    meta = data["meta"]
    subtitle = (
        f"<strong>{meta['total_entities']}</strong> сущностей из "
        f"<strong>{meta['total_messages']:,}</strong> сообщений · "
        f"с {meta['since']} · сгенерировано {meta['generated_at']}"
    ).replace(",", " ")

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AI Pulse — Trend Radar</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.1.0/wordcloud2.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0b0e1a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding-bottom:40px}}

    /* ── Header ──────────────────────────────────────────────────────── */
    .header{{background:linear-gradient(135deg,#12162a 0%,#0f172a 60%,#13102a 100%);border-bottom:1px solid #1e2540;padding:26px 28px 18px}}
    .header-inner{{max-width:1200px;margin:0 auto}}
    .tag{{display:inline-block;background:rgba(139,92,246,.18);border:1px solid rgba(139,92,246,.35);color:#a78bfa;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:3px 10px;border-radius:20px;margin-bottom:10px}}
    h1{{font-size:clamp(16px,3vw,26px);font-weight:700;line-height:1.2;margin-bottom:5px;background:linear-gradient(135deg,#e2e8f0,#818cf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
    .subtitle{{color:#475569;font-size:12px}}
    .subtitle strong{{color:#64748b}}

    /* ── Cards ───────────────────────────────────────────────────────── */
    .fw-section{{max-width:1200px;margin:16px auto 0;padding:0 24px}}
    .card{{background:#111827;border:1px solid #1e2540;border-radius:14px;padding:18px 22px}}
    .card-header{{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}}
    .card-title{{font-size:12px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#94a3b8;flex:1}}
    .card-desc{{font-size:12px;color:#475569;margin-bottom:14px}}
    .card-hint{{font-size:11px;color:#374151;margin-left:auto;white-space:nowrap}}
    @media(max-width:700px){{.card-hint{{width:100%;margin-left:0;margin-top:4px}}}}

    /* ── Unified surges ─────────────────────────────────────────────── */
    .surge-card{{border-color:#2d2150;background:linear-gradient(180deg,#14112a 0%,#111827 100%)}}
    .surge-card .card-title{{color:#e2e8f0}}
    .surge-filters{{display:flex;gap:4px;margin-right:10px}}
    .filter-btn{{padding:4px 11px;border-radius:6px;border:1px solid #2d3748;background:transparent;color:#64748b;font-size:11px;cursor:pointer;transition:all .12s}}
    .filter-btn:hover{{border-color:#374151;color:#94a3b8}}
    .filter-btn.active{{border-color:#a78bfa;background:rgba(167,139,250,.12);color:#c4b5fd}}
    .surge-chips{{display:flex;flex-wrap:wrap;gap:7px}}
    .s-chip{{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:16px;font-size:12px;border:1px solid;white-space:nowrap;transition:transform .1s}}
    .s-chip:hover{{transform:translateY(-1px)}}
    .s-chip .cat-icon{{font-size:11px;opacity:.85}}
    .s-chip .s-name{{color:#e2e8f0;max-width:200px;overflow:hidden;text-overflow:ellipsis}}
    .s-chip .s-count{{font-weight:700}}
    .s-chip .s-delta{{font-size:10px;color:#475569}}
    .s-chip .s-new{{font-size:11px}}

    /* ── Category tabs ───────────────────────────────────────────────── */
    .controls{{max-width:1200px;margin:18px auto 4px;padding:0 24px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
    .controls-hint{{font-size:11px;color:#374151;margin-right:auto;align-self:center}}
    .cat-btn{{padding:6px 14px;border-radius:8px;border:1px solid #2d3748;background:transparent;color:#64748b;font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap}}
    .cat-btn:hover{{border-color:#4b5563;color:#94a3b8}}
    .cat-btn.active{{border-color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent);color:var(--c)}}
    .cat-btn[data-cat="concepts"]{{--c:#a78bfa}}
    .cat-btn[data-cat="entities"]{{--c:#60a5fa}}
    .cat-btn[data-cat="people"]{{--c:#34d399}}
    .cat-btn[data-cat="projects"]{{--c:#fb923c}}

    /* ── Heatmap ─────────────────────────────────────────────────────── */
    .heatmap-wrap{{overflow-x:auto;margin-top:6px}}
    .heatmap-grid{{display:grid;gap:3px;min-width:720px}}
    .hm-spacer{{}}
    .hm-header{{text-align:center;font-size:10px;color:#475569;padding:2px 2px 6px;font-weight:500}}
    .hm-header.current{{color:#e2e8f0;font-weight:700}}
    .hm-name{{text-align:right;font-size:12px;color:#cbd5e1;padding:0 10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;justify-content:flex-end}}
    .hm-cell{{font-size:11px;font-weight:600;border-radius:4px;min-height:28px;display:flex;align-items:center;justify-content:center;transition:transform .1s;cursor:default}}
    .hm-cell:hover{{transform:scale(1.08);z-index:2;position:relative}}
    .hm-cell.empty{{background:rgba(255,255,255,0.025);color:#232838}}

    /* ── Stacked chart + granularity buttons ─────────────────────────── */
    .gran-btns{{display:flex;gap:4px}}
    .gran-btn{{padding:3px 10px;border-radius:6px;border:1px solid #2d3748;background:transparent;color:#475569;font-size:11px;cursor:pointer;transition:all .12s}}
    .gran-btn:hover{{border-color:#374151;color:#64748b}}
    .gran-btn.active{{border-color:#374151;background:#1e2540;color:#94a3b8}}
    .chart-wrap-hot{{height:340px;position:relative}}

    /* ── Top-14 list ─────────────────────────────────────────────────── */
    .top-list-items{{display:grid;grid-template-columns:1fr 1fr;gap:3px 28px;margin-top:6px}}
    @media(max-width:700px){{.top-list-items{{grid-template-columns:1fr}}}}
    .top-item{{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #1e2540}}
    .top-rank{{font-size:11px;color:#374151;width:20px;flex-shrink:0;text-align:right}}
    .top-name{{font-size:13px;color:#cbd5e1;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .top-bar-wrap{{width:60px;flex-shrink:0;position:relative;height:5px;background:#1e2540;border-radius:3px}}
    .top-bar-fill{{position:absolute;left:0;top:0;height:100%;border-radius:3px;transition:width .3s}}
    .top-count{{font-size:11px;font-weight:600;color:#e2e8f0;width:28px;text-align:right;flex-shrink:0}}

    /* ── Word cloud ──────────────────────────────────────────────────── */
    #cloud-wrap{{width:100%;height:320px;position:relative}}
    canvas#wordcloud{{width:100%;height:100%}}
    .cloud-empty{{display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:#475569;font-size:13px}}

    /* ── Footer ──────────────────────────────────────────────────────── */
    .footer{{max-width:1200px;margin:24px auto 0;padding:0 24px;color:#2d3748;font-size:11px;text-align:center}}
  </style>
</head>
<body data-cat="concepts">

<div class="header">
  <div class="header-inner">
    <div class="tag">📡 AI Pulse · Trend Radar</div>
    <h1>Что обсуждает AI-сообщество прямо сейчас</h1>
    <p class="subtitle">{subtitle}</p>
  </div>
</div>

<!-- [A] Unified surges ─ главный блок сверху ─────────────────────────── -->
<div class="fw-section">
  <div class="card surge-card">
    <div class="card-header">
      <span class="card-title">🔥 Всплески — нед. <span id="surge-week-label">—</span></span>
      <div class="surge-filters">
        <button class="filter-btn active" data-filter="all">Все</button>
        <button class="filter-btn" data-filter="new">Только ✨ новое</button>
      </div>
      <span class="card-hint">число = упоминаний · +N = рост vs прошлой недели · ✨ = не было на прошлой</span>
    </div>
    <div class="surge-chips" id="unified-chips"></div>
  </div>
</div>

<!-- Category tabs (переключают B, C, D) ─────────────────────────────── -->
<div class="controls">
  <span class="controls-hint">Глубже по категориям ↓</span>
  <button class="cat-btn active" data-cat="concepts">💡 Концепции</button>
  <button class="cat-btn" data-cat="entities">🛠 Инструменты</button>
  <button class="cat-btn" data-cat="people">👤 Люди</button>
  <button class="cat-btn" data-cat="projects">🚀 Проекты</button>
</div>

<!-- [B] Heatmap ─ топ-15 × 8 недель ──────────────────────────────────── -->
<div class="fw-section">
  <div class="card">
    <div class="card-header">
      <span class="card-title">📊 Топ-15 <span id="heatmap-cat-label">концепций</span> по неделям</span>
      <span class="card-hint">чем ярче ячейка, тем активнее обсуждение в ту неделю</span>
    </div>
    <div class="heatmap-wrap"><div class="heatmap-grid" id="heatmap"></div></div>
  </div>
</div>

<!-- [C] Stacked «горячие темы» ───────────────────────────────────────── -->
<div class="fw-section">
  <div class="card">
    <div class="card-header">
      <span class="card-title">🔥 Горячие темы — топ-6 стеком</span>
      <div class="gran-btns" id="gran-btns">
        <button class="gran-btn active" data-gran="week">Нед.</button>
        <button class="gran-btn" data-gran="month">Месяц</button>
      </div>
    </div>
    <p class="card-desc">Топ-6 сущностей с подписями прямо внутри столбиков. Самая частая — внизу.</p>
    <div class="chart-wrap-hot"><canvas id="stackedChart"></canvas></div>
  </div>
</div>

<!-- [D] Top-14 list ──────────────────────────────────────────────────── -->
<div class="fw-section">
  <div class="card">
    <div class="card-header">
      <span class="card-title">📌 Топ-14 <span id="top-cat-label">концепций</span> за всё время</span>
    </div>
    <div class="top-list-items" id="top-list-items"></div>
  </div>
</div>

<!-- [E] Word cloud ─ внизу, опционально ──────────────────────────────── -->
<div class="fw-section">
  <div class="card">
    <div class="card-header">
      <span class="card-title">☁️ Облако слов <span id="cloud-cat-label">— концепции</span></span>
      <span class="card-hint">декоративный вид поверх тех же данных</span>
    </div>
    <div id="cloud-wrap">
      <canvas id="wordcloud"></canvas>
      <div class="cloud-empty" id="cloud-empty">Нет данных</div>
    </div>
  </div>
</div>

<div class="footer">
  AI Pulse · сгенерировано {meta['generated_at']} · окно данных с {meta['since']}
</div>

<script>
// ── Данные, сгенерированные generate.py ──────────────────────────────────
const DATA = {js_data};
const PALETTE = {palette_js};
const CAT_META = DATA.categories;

// ── Состояние ──────────────────────────────────────────────────────────────
let currentCat     = "concepts";
let currentGran    = "week";
let surgeFilter    = "all";  // "all" | "new"

// ── Форматирование дат ─────────────────────────────────────────────────────
const MONTHS_SHORT = ["янв.","фев.","мар.","апр.","мая","июн.","июл.","авг.","сен.","окт.","ноя.","дек."];
function fmtDate(s) {{
  const d = new Date(s);
  return `${{d.getDate()}} ${{MONTHS_SHORT[d.getMonth()]}}`;
}}
function fmtWeek(s) {{
  const d = new Date(s), d2 = new Date(d); d2.setDate(d.getDate()+6);
  return fmtDate(s) + "–" + fmtDate(d2.toISOString().slice(0,10));
}}
function fmtWeekShort(s) {{
  const d = new Date(s); return `${{d.getDate()}} ${{MONTHS_SHORT[d.getMonth()]}}`;
}}
function fmtMonth(s) {{
  return new Date(s+"-01").toLocaleDateString("ru-RU", {{month:"long"}});
}}

// ── Утилиты ────────────────────────────────────────────────────────────────
function hexToRgb(hex) {{
  const m = hex.match(/^#([0-9a-f]{{2}})([0-9a-f]{{2}})([0-9a-f]{{2}})$/i);
  return m ? {{r:parseInt(m[1],16), g:parseInt(m[2],16), b:parseInt(m[3],16)}} : {{r:130,g:140,b:248}};
}}
function el(tag, cls, text) {{
  const e = document.createElement(tag);
  if (cls)  e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}}

// ── [A] Unified surges ─────────────────────────────────────────────────────
function drawSurges() {{
  const us = DATA.unified_surges;
  const label = document.getElementById("surge-week-label");
  label.textContent = us.week ? fmtWeek(us.week) : "—";

  const chips = document.getElementById("unified-chips");
  chips.innerHTML = "";

  const items = us.items.filter(x => surgeFilter === "all" ? true : x.isNew);
  if (!items.length) {{
    chips.appendChild(el("div", "card-desc", "Нет всплесков в этой категории."));
    return;
  }}

  items.forEach(x => {{
    const meta = CAT_META[x.category];
    const chip = el("div", "s-chip");
    chip.style.borderColor = meta.color + "55";
    chip.style.background  = meta.color + "14";
    chip.title = `${{meta.label}} · ${{x.title}} · ${{x.count}} упоминаний`;

    chip.appendChild(el("span", "cat-icon", meta.icon));
    chip.appendChild(el("span", "s-name", x.title));

    const cnt = el("span", "s-count", x.count);
    cnt.style.color = meta.color;
    chip.appendChild(cnt);

    if (x.isNew)  chip.appendChild(el("span", "s-new", "✨"));
    else          chip.appendChild(el("span", "s-delta", "+" + x.delta));

    chips.appendChild(chip);
  }});
}}

// ── [B] Heatmap ────────────────────────────────────────────────────────────
function drawHeatmap() {{
  const hm      = DATA.heatmap[currentCat];
  const meta    = CAT_META[currentCat];
  const rgb     = hexToRgb(meta.color);
  const grid    = document.getElementById("heatmap");
  const nWeeks  = hm.weeks.length;
  const lastW   = hm.weeks[nWeeks - 1];

  grid.innerHTML = "";
  grid.style.gridTemplateColumns = `200px repeat(${{nWeeks}}, minmax(60px, 1fr))`;

  // Header row: пустой угол + недели
  grid.appendChild(el("div", "hm-spacer"));
  hm.weeks.forEach(w => {{
    const cls = "hm-header" + (w === lastW ? " current" : "");
    grid.appendChild(el("div", cls, fmtWeekShort(w)));
  }});

  // Data rows
  hm.entities.forEach((name, i) => {{
    const nameCell = el("div", "hm-name", name);
    nameCell.title = name;
    grid.appendChild(nameCell);
    hm.matrix[i].forEach(count => {{
      const cell = el("div", "hm-cell" + (count === 0 ? " empty" : ""), count || "·");
      if (count > 0) {{
        // Минимальный floor .12 чтобы 1-упоминание тоже было видно
        const v = Math.max(0.12, count / hm.max);
        cell.style.background = `rgba(${{rgb.r}},${{rgb.g}},${{rgb.b}},${{v.toFixed(3)}})`;
        cell.style.color = v > 0.55 ? "#0b0e1a" : "#e2e8f0";
      }}
      grid.appendChild(cell);
    }});
  }});

  document.getElementById("heatmap-cat-label").textContent = meta.label.toLowerCase();
}}

// ── [C] Stacked chart «горячие темы» ───────────────────────────────────────
let chartInst = null;

const barLabelsPlugin = {{
  id: "barLabels",
  afterDraw(chart) {{
    const {{ctx}} = chart;
    chart.data.datasets.forEach((ds, di) => {{
      chart.getDatasetMeta(di).data.forEach((bar, idx) => {{
        if (!ds.data[idx]) return;
        const segH = Math.abs(bar.base - bar.y);
        if (segH < 15) return;
        const lbl = ds.fullLabel || ds.label || "";
        const maxChars = Math.max(1, Math.floor(bar.width / 7));
        const txt = lbl.length > maxChars ? lbl.slice(0, maxChars - 1) + "…" : lbl;
        ctx.save();
        ctx.font = `600 ${{Math.min(11, segH * 0.42)}}px -apple-system, 'Segoe UI'`;
        ctx.fillStyle = "rgba(255,255,255,0.88)";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(txt, bar.x, bar.y + segH / 2);
        ctx.restore();
      }});
    }});
  }}
}};

function drawStackedChart() {{
  const key = currentGran === "week" ? "stacked_week" : "stacked_month";
  const sd  = DATA[key][currentCat];
  const labels = sd.periods.map(p => currentGran === "week" ? fmtWeek(p) : fmtMonth(p));

  const datasets = sd.hot_entities.map((ent, i) => ({{
    label: ent.length > 20 ? ent.slice(0, 18) + "…" : ent,
    fullLabel: ent,
    data: sd.hot_series[ent],
    backgroundColor: PALETTE[i % PALETTE.length],
    borderColor: "transparent", borderWidth: 0,
  }}));

  const ctx = document.getElementById("stackedChart");
  if (chartInst) chartInst.destroy();
  chartInst = new Chart(ctx, {{
    type: "bar", data: {{labels, datasets}},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{mode: "index", intersect: false}},
      plugins: {{
        legend: {{display: false}},
        tooltip: {{
          backgroundColor: "#1e293b", borderColor: "#334155", borderWidth: 1,
          titleColor: "#e2e8f0", bodyColor: "#94a3b8",
          callbacks: {{label: c => ` ${{c.dataset.fullLabel}}: ${{c.raw}}`}}
        }}
      }},
      scales: {{
        x: {{stacked: true, grid: {{display: false}},
             ticks: {{color: "#64748b", font: {{size: 10}}}}}},
        y: {{stacked: true, grid: {{color: "rgba(255,255,255,0.04)"}},
             ticks: {{color: "#475569", font: {{size: 10}}}}}}
      }}
    }},
    plugins: [barLabelsPlugin]
  }});
}}

// ── [D] Top-14 list ────────────────────────────────────────────────────────
function drawTopList() {{
  const items = (DATA.total[currentCat] || []).slice(0, 14);
  const maxC  = items[0]?.[1] || 1;
  const meta  = CAT_META[currentCat];
  document.getElementById("top-cat-label").textContent = meta.label.toLowerCase();

  const container = document.getElementById("top-list-items");
  container.innerHTML = "";
  items.forEach(([name, count], i) => {{
    const pct = Math.round((count / maxC) * 100);
    const row = el("div", "top-item");
    row.innerHTML = `
      <span class="top-rank">${{i + 1}}</span>
      <span class="top-name" title="${{name}}">${{name}}</span>
      <div class="top-bar-wrap"><div class="top-bar-fill" style="width:${{pct}}%;background:${{meta.color}}"></div></div>
      <span class="top-count">${{count}}</span>`;
    container.appendChild(row);
  }});
}}

// ── [E] Word cloud ─────────────────────────────────────────────────────────
const cloudCanvas = document.getElementById("wordcloud");
const cloudWrap   = document.getElementById("cloud-wrap");

function drawCloud() {{
  const items = (DATA.total[currentCat] || []);
  const empty = document.getElementById("cloud-empty");
  const meta  = CAT_META[currentCat];
  document.getElementById("cloud-cat-label").textContent = "— " + meta.label.toLowerCase();

  if (!items.length) {{ empty.style.display = "flex"; return; }}
  empty.style.display = "none";
  cloudCanvas.width  = cloudWrap.offsetWidth  || 800;
  cloudCanvas.height = cloudWrap.offsetHeight || 320;
  const maxC  = items[0][1];
  const color = meta.color;
  WordCloud(cloudCanvas, {{
    list:         items.map(([w, c]) => [w, Math.round(12 + (c / maxC) * 50)]),
    gridSize:     Math.round(7 * cloudCanvas.width / 800),
    fontFamily:   "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    color:        (_, w) => w / 62 > .7 ? color : w / 62 > .4 ? color + "cc" : color + "77",
    rotateRatio:  0.2, rotationSteps: 2,
    backgroundColor: "transparent", drawOutOfBound: false, shrinkToFit: true,
  }});
}}

// ── Рендер по категории / полный рендер ───────────────────────────────────
function renderCategoryBlocks() {{
  document.body.dataset.cat = currentCat;
  drawHeatmap();
  drawStackedChart();
  drawTopList();
  drawCloud();
}}

function renderAll() {{
  drawSurges();
  renderCategoryBlocks();
}}

// ── События ───────────────────────────────────────────────────────────────
document.querySelectorAll(".cat-btn").forEach(btn => btn.addEventListener("click", () => {{
  document.querySelectorAll(".cat-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  currentCat = btn.dataset.cat;
  renderCategoryBlocks();
}}));

document.querySelectorAll("#gran-btns .gran-btn").forEach(btn => btn.addEventListener("click", () => {{
  document.querySelectorAll("#gran-btns .gran-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  currentGran = btn.dataset.gran;
  drawStackedChart();
}}));

document.querySelectorAll(".filter-btn").forEach(btn => btn.addEventListener("click", () => {{
  document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  surgeFilter = btn.dataset.filter;
  drawSurges();
}}));

window.addEventListener("load",   () => setTimeout(renderAll, 80));
window.addEventListener("resize", () => setTimeout(drawCloud, 150));
</script>
</body>
</html>"""
