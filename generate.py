"""
generate.py — модуль генерации AI Pulse Dashboard.

Читает данные из двух источников:
  1. community-brain/data/messages_YYYY-MM-DD.json  — сырые сообщения
  2. community-brain/wiki/pages/{category}/*.md     — wiki-сущности с source_ids

Строит:
  - агрегированные счётчики по неделям / дням / месяцам
  - stacked-данные для двух видов графиков (все темы + горячие темы)
  - топ-списки по каждой категории

Затем рендерит самодостаточный HTML-файл с Chart.js + wordcloud2.js внутри.
"""

import json
import re
import glob
import os
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml  # pyyaml — для чтения YAML frontmatter wiki-страниц

# ── Категории сущностей (совпадают с папками в wiki/pages/) ─────────────────
CATEGORIES = ["concepts", "entities", "people", "projects"]

# ── Цветовая палитра для сегментов стекового графика ────────────────────────
PALETTE = [
    "#818cf8", "#34d399", "#fb923c", "#f472b6", "#fbbf24",
    "#38bdf8", "#a3e635", "#e879f9", "#2dd4bf", "#f87171",
]


# ── Вспомогательные функции группировки по периодам ─────────────────────────

def get_week(date_str: str) -> str:
    """Возвращает дату понедельника недели, к которой принадлежит дата."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def get_month(date_str: str) -> str:
    """Возвращает 'YYYY-MM' для группировки по месяцам."""
    return date_str[:7]


# ── Загрузка raw-сообщений ──────────────────────────────────────────────────

def load_messages(data_dir: str, since: str = "2026-03-01") -> dict[int, str]:
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
            # Берём только сообщения начиная с since
            if msg_id and date >= since:
                id_to_date[msg_id] = date

    return id_to_date


# ── Загрузка wiki-сущностей ─────────────────────────────────────────────────

def load_wiki_entities(wiki_pages_dir: str) -> list[dict]:
    """
    Читает все .md файлы из wiki/pages/{category}/.
    Парсит YAML frontmatter и возвращает список словарей:
      {category, title, source_ids}
    """
    entities = []
    for cat in CATEGORIES:
        cat_dir = os.path.join(wiki_pages_dir, cat)
        for fpath in glob.glob(os.path.join(cat_dir, "*.md")):
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
            # YAML frontmatter между тройными дефисами
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not match:
                continue
            try:
                meta = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                continue
            title      = (meta.get("title") or "").strip()
            source_ids = meta.get("source_ids") or []
            if title and source_ids:
                entities.append({
                    "category":   cat,
                    "title":      title,
                    "source_ids": source_ids,
                })
    return entities


# ── Агрегация данных ────────────────────────────────────────────────────────

def aggregate(entities: list[dict], id_to_date: dict[int, str]) -> dict:
    """
    Для каждой wiki-сущности находит даты упоминаний через source_ids.
    Накапливает счётчики по:
      - total  : за всё время
      - week   : по неделям
      - day    : по дням
      - month  : по месяцам
    Возвращает dict со всеми счётчиками, отдельно для каждой категории.
    """
    # Структура: {category: Counter{title: count}}
    total = defaultdict(Counter)
    week  = defaultdict(lambda: defaultdict(Counter))  # [cat][week][title]
    day   = defaultdict(lambda: defaultdict(Counter))  # [cat][day][title]
    month = defaultdict(lambda: defaultdict(Counter))  # [cat][month][title]

    for e in entities:
        cat, title = e["category"], e["title"]
        for sid in e["source_ids"]:
            d = id_to_date.get(sid)
            if not d:
                continue
            total[cat][title] += 1
            week[cat][get_week(d)][title]  += 1
            day[cat][d][title]             += 1
            month[cat][get_month(d)][title] += 1

    return {"total": total, "week": week, "day": day, "month": month}


# ── Построение stacked-серий ────────────────────────────────────────────────

def make_stacked(cat: str, agg: dict, granularity: str) -> dict:
    """
    Строит данные для stacked bar chart по заданной гранулярности.

    Параметры:
      cat         — категория (concepts / entities / people / projects)
      agg         — результат aggregate()
      granularity — 'week' | 'day' | 'month'

    Возвращает:
      periods      — отсортированный список периодов
      entities     — топ-8 сущностей по суммарным упоминаниям
      series       — {entity: [count_per_period]}  (для графика "все темы")
      others       — [остаток_per_period]           (то, что не вошло в топ-8)
      hot_entities — топ-6 (для графика "горячие темы")
      hot_series   — {entity: [count_per_period]}  (только топ-6, без "остальных")
    """
    by_time = agg[granularity]  # [cat][period][title]
    # Минимальный ключ зависит от гранулярности
    min_key = "2026-03" if granularity == "month" else "2026-03-01"
    periods = sorted(k for k in by_time[cat] if k >= min_key)

    # Топ-8 сущностей за весь период (стабильный порядок)
    top8 = [t for t, _ in agg["total"][cat].most_common(8)]
    top6 = top8[:6]

    # Серия для каждой из топ-8 сущностей
    series = {
        ent: [by_time[cat][p].get(ent, 0) for p in periods]
        for ent in top8
    }

    # "Остальные" — всё что не вошло в топ-8
    others = []
    for p in periods:
        top8_sum = sum(by_time[cat][p].get(ent, 0) for ent in top8)
        all_sum  = sum(by_time[cat][p].values())
        others.append(max(0, all_sum - top8_sum))

    return {
        "periods":      periods,
        "entities":     top8,
        "series":       series,
        "others":       others,
        "hot_entities": top6,
        "hot_series":   {ent: series[ent] for ent in top6},
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

    print("📚 Загружаю wiki-сущности...")
    entities = load_wiki_entities(wiki_pages_dir)
    print(f"   {len(entities)} сущностей")

    print("🔢 Агрегирую данные...")
    agg = aggregate(entities, id_to_date)

    def top_n(counter, n=60):
        return [[t, c] for t, c in counter.most_common(n)]

    # Данные для облака слов и топ-листа (по всему периоду / по конкретному дню/неделе)
    total_data = {cat: top_n(agg["total"][cat]) for cat in CATEGORIES}

    # Данные по неделям и дням — для переключения периода облака
    weeks_data: dict = {}
    for cat in CATEGORIES:
        for week_key, counter in agg["week"][cat].items():
            if week_key < "2026-03-01":
                continue
            if week_key not in weeks_data:
                weeks_data[week_key] = {c: [] for c in CATEGORIES}
            weeks_data[week_key][cat] = top_n(counter, 40)

    days_data: dict = {}
    for cat in CATEGORIES:
        for day_key, counter in agg["day"][cat].items():
            if day_key < "2026-03-01":
                continue
            if day_key not in days_data:
                days_data[day_key] = {c: [] for c in CATEGORIES}
            days_data[day_key][cat] = top_n(counter, 30)

    # Stacked данные для трёх гранулярностей
    stacked = {}
    for gran in ("week", "day", "month"):
        stacked[f"stacked_{gran}"] = {
            cat: make_stacked(cat, agg, gran)
            for cat in CATEGORIES
        }

    return {
        "total": total_data,
        "weeks": weeks_data,
        "days":  days_data,
        **stacked,
    }


# ── HTML-шаблон ─────────────────────────────────────────────────────────────

def render_html(data: dict) -> str:
    """
    Генерирует самодостаточный HTML: встраивает data как JS-переменную DATA,
    Chart.js и wordcloud2.js подключаются с cdnjs (CDN).
    """
    # Безопасная сериализация: экранируем </script> внутри JSON
    js_data = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")

    palette_js = json.dumps(PALETTE)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AI Community Brain — Что обсуждали</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.1.0/wordcloud2.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0b0e1a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}}
    .header{{background:linear-gradient(135deg,#12162a 0%,#0f172a 60%,#13102a 100%);border-bottom:1px solid #1e2540;padding:26px 28px 18px}}
    .header-inner{{max-width:1200px;margin:0 auto}}
    .tag{{display:inline-block;background:rgba(139,92,246,.18);border:1px solid rgba(139,92,246,.35);color:#a78bfa;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:3px 10px;border-radius:20px;margin-bottom:10px}}
    h1{{font-size:clamp(16px,3vw,26px);font-weight:700;line-height:1.2;margin-bottom:5px;background:linear-gradient(135deg,#e2e8f0,#818cf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
    .subtitle{{color:#475569;font-size:12px}}
    .subtitle strong{{color:#64748b}}
    .controls{{max-width:1200px;margin:14px auto;padding:0 24px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
    .cat-btn{{padding:6px 14px;border-radius:8px;border:1px solid #2d3748;background:transparent;color:#64748b;font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap}}
    .cat-btn:hover{{border-color:#4b5563;color:#94a3b8}}
    .cat-btn.active{{border-color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent);color:var(--c)}}
    .cat-btn[data-cat="concepts"]{{--c:#a78bfa}}
    .cat-btn[data-cat="entities"]{{--c:#60a5fa}}
    .cat-btn[data-cat="people"]{{--c:#34d399}}
    .cat-btn[data-cat="projects"]{{--c:#fb923c}}
    .period-wrap{{display:flex;gap:5px;margin-left:auto;align-items:center;flex-wrap:wrap}}
    .period-btn{{padding:6px 13px;border-radius:8px;border:1px solid #2d3748;background:transparent;color:#64748b;font-size:12px;cursor:pointer;transition:all .15s}}
    .period-btn:hover{{border-color:#4b5563;color:#94a3b8}}
    .period-btn.active{{border-color:#475569;background:#1e2540;color:#e2e8f0}}
    select.period-select{{padding:6px 10px;border-radius:8px;border:1px solid #2d3748;background:#161b2e;color:#94a3b8;font-size:12px;cursor:pointer;outline:none;display:none}}
    select.period-select.visible{{display:block}}
    .top-zone{{max-width:1200px;margin:0 auto;padding:0 24px;display:grid;grid-template-columns:1fr 300px;gap:16px}}
    @media(max-width:800px){{.top-zone{{grid-template-columns:1fr}}}}
    .card{{background:#111827;border:1px solid #1e2540;border-radius:14px;padding:18px}}
    .cloud-header{{display:flex;align-items:center;gap:8px;margin-bottom:12px}}
    .cloud-title-text{{font-size:11px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#475569}}
    .period-badge{{margin-left:auto;font-size:11px;padding:2px 9px;border-radius:10px;border:1px solid #2d3748;color:#64748b;background:#0d1017;white-space:nowrap}}
    #cloud-wrap{{width:100%;height:360px;position:relative}}
    canvas#wordcloud{{width:100%;height:100%}}
    .cloud-empty{{display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:#475569;font-size:13px}}
    .top-list-card{{display:flex;flex-direction:column;gap:6px}}
    .top-list-title{{font-size:11px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#475569;margin-bottom:4px}}
    .top-item{{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #1e2540}}
    .top-item:last-child{{border-bottom:none}}
    .top-rank{{font-size:11px;color:#374151;width:18px;flex-shrink:0;text-align:right}}
    .top-name{{font-size:13px;color:#94a3b8;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .top-bar-wrap{{width:70px;flex-shrink:0;position:relative;height:6px;background:#1e2540;border-radius:3px}}
    .top-bar-fill{{position:absolute;left:0;top:0;height:100%;border-radius:3px;transition:width .3s}}
    .top-count{{font-size:11px;font-weight:600;color:#e2e8f0;width:28px;text-align:right;flex-shrink:0}}
    .fw-section{{max-width:1200px;margin:16px auto 0;padding:0 24px}}
    .chart-card{{background:#111827;border:1px solid #1e2540;border-radius:14px;padding:20px 24px}}
    .chart-header{{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}}
    .chart-title{{font-size:12px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#475569;flex:1}}
    .chart-desc{{font-size:12px;color:#374151;margin-bottom:14px}}
    .gran-btns{{display:flex;gap:4px}}
    .gran-btn{{padding:3px 10px;border-radius:6px;border:1px solid #2d3748;background:transparent;color:#475569;font-size:11px;cursor:pointer;transition:all .12s}}
    .gran-btn:hover{{border-color:#374151;color:#64748b}}
    .gran-btn.active{{border-color:#374151;background:#1e2540;color:#94a3b8}}
    .chart-wrap-tall{{height:300px;position:relative}}
    .chart-wrap-hot{{height:340px;position:relative}}
    .legend-row{{display:flex;flex-wrap:wrap;gap:5px 14px;margin-top:12px}}
    .legend-item{{display:flex;align-items:center;gap:5px;font-size:11px;color:#64748b}}
    .legend-dot{{width:10px;height:10px;border-radius:3px;flex-shrink:0}}
    .rising-card{{background:#111827;border:1px solid #1e2540;border-radius:14px;padding:18px 22px;margin-bottom:40px}}
    .rising-header{{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}}
    .rising-title{{font-size:11px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;color:#475569}}
    .rising-hint{{font-size:11px;color:#2d3748}}
    .rising-grid{{display:flex;flex-wrap:wrap;gap:7px}}
    .r-chip{{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:16px;font-size:12px;border:1px solid;white-space:nowrap}}
    .r-chip .ricon{{font-size:10px}}
    .r-chip .rname{{color:#e2e8f0}}
    .r-chip .rcount{{font-weight:700}}
    .r-chip .rdelta{{font-size:10px;color:#475569}}
  </style>
</head>
<body data-cat="concepts">

<div class="header">
  <div class="header-inner">
    <div class="tag">📡 Community Brain Analytics</div>
    <h1>Что обсуждало AI-сообщество</h1>
    <p class="subtitle"><strong>1 253 сущности</strong>, извлечённые LLM из 15 тыс. сообщений · март–апрель 2026 · 4 Telegram-канала</p>
  </div>
</div>

<div class="controls">
  <button class="cat-btn active" data-cat="concepts">💡 Концепции</button>
  <button class="cat-btn" data-cat="entities">🛠 Инструменты</button>
  <button class="cat-btn" data-cat="people">👤 Люди</button>
  <button class="cat-btn" data-cat="projects">🚀 Проекты</button>
  <div class="period-wrap">
    <button class="period-btn active" data-period="total">Весь период</button>
    <button class="period-btn" data-period="week">По неделям</button>
    <button class="period-btn" data-period="day">По дням</button>
    <select class="period-select" id="period-select"></select>
  </div>
</div>

<div class="top-zone">
  <div class="card">
    <div class="cloud-header">
      <span class="cloud-title-text" id="cloud-title">💡 Концепции</span>
      <span class="period-badge" id="cloud-period-label">март–апрель 2026</span>
    </div>
    <div id="cloud-wrap">
      <canvas id="wordcloud"></canvas>
      <div class="cloud-empty" id="cloud-empty">Нет данных</div>
    </div>
  </div>
  <div class="card top-list-card">
    <div class="top-list-title" id="top-list-title">Топ — Концепции</div>
    <div id="top-list-items"></div>
  </div>
</div>

<div class="fw-section">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">📊 Все темы по периодам</span>
      <div class="gran-btns" id="gran1-btns">
        <button class="gran-btn active" data-gran="week">Нед.</button>
        <button class="gran-btn" data-gran="day">Дни</button>
        <button class="gran-btn" data-gran="month">Месяц</button>
      </div>
    </div>
    <p class="chart-desc">Топ-8 сущностей + остальные. Каждый столбик = один период.</p>
    <div class="chart-wrap-tall"><canvas id="stackedChart1"></canvas></div>
    <div class="legend-row" id="legend1"></div>
  </div>
</div>

<div class="fw-section" style="margin-top:14px">
  <div class="chart-card">
    <div class="chart-header">
      <span class="chart-title">🔥 Горячие темы — подробно</span>
      <div class="gran-btns" id="gran2-btns">
        <button class="gran-btn active" data-gran="week">Нед.</button>
        <button class="gran-btn" data-gran="day">Дни</button>
        <button class="gran-btn" data-gran="month">Месяц</button>
      </div>
    </div>
    <p class="chart-desc">Только топ-6 сущностей (без «Остальных»), с подписями прямо внутри столбиков.</p>
    <div class="chart-wrap-hot"><canvas id="stackedChart2"></canvas></div>
  </div>
</div>

<div class="fw-section" style="margin-top:14px">
  <div class="rising-card">
    <div class="rising-header">
      <span class="rising-title" id="rising-title">🔥 Всплески последней недели</span>
      <span class="rising-hint">число = упоминаний &nbsp;·&nbsp; +N = рост vs предыдущая неделя &nbsp;·&nbsp; ✨ = новое</span>
    </div>
    <div class="rising-grid" id="rising-chips"></div>
  </div>
</div>

<script>
// ── Данные, сгенерированные generate.py ──────────────────────────────────
const DATA = {js_data};
const PALETTE = {palette_js};
const OTHERS_COLOR = "#1e2540";

// ── Состояние ──────────────────────────────────────────────────────────────
let currentCat    = "concepts";
let currentPeriod = "total";
let currentKey    = null;
let gran1 = "week";
let gran2 = "week";

const catConfig = {{
  concepts: {{ label:"💡 Концепции", color:"#a78bfa" }},
  entities: {{ label:"🛠 Инструменты", color:"#60a5fa" }},
  people:   {{ label:"👤 Люди",       color:"#34d399" }},
  projects: {{ label:"🚀 Проекты",    color:"#fb923c" }},
}};

// ── Форматирование дат ─────────────────────────────────────────────────────
function fmtDate(s) {{
  return new Date(s).toLocaleDateString("ru-RU", {{day:"numeric", month:"short"}});
}}
function fmtWeek(s) {{
  const d=new Date(s), d2=new Date(d); d2.setDate(d.getDate()+6);
  return fmtDate(s) + "–" + fmtDate(d2.toISOString().slice(0,10));
}}
function fmtMonth(s) {{
  return new Date(s+"-01").toLocaleDateString("ru-RU", {{month:"long", year:"numeric"}});
}}
function fmtPeriod(gran, key) {{
  if (gran==="week")  return fmtWeek(key);
  if (gran==="day")   return fmtDate(key);
  if (gran==="month") return fmtMonth(key);
  return key;
}}

// ── Получение данных по текущему состоянию ────────────────────────────────
function getCloudData() {{
  if (currentPeriod==="total") return DATA.total[currentCat] || [];
  if (currentPeriod==="week")  return (DATA.weeks[currentKey] || {{}})[currentCat] || [];
  if (currentPeriod==="day")   return (DATA.days[currentKey]  || {{}})[currentCat] || [];
  return [];
}}
function getStackedData(gran) {{
  const key = gran==="week" ? "stacked_week" : gran==="day" ? "stacked_day" : "stacked_month";
  return DATA[key][currentCat];
}}

// ── Дропдаун выбора периода ────────────────────────────────────────────────
function populateSelect(period) {{
  const sel = document.getElementById("period-select");
  sel.innerHTML = "";
  if (period==="week") {{
    Object.keys(DATA.weeks).sort().forEach(w => {{
      const o=document.createElement("option"); o.value=w;
      o.textContent="Нед. "+fmtWeek(w); sel.appendChild(o);
    }});
    currentKey = Object.keys(DATA.weeks).sort().at(-1);
    sel.value = currentKey; sel.classList.add("visible");
  }} else if (period==="day") {{
    Object.keys(DATA.days).sort().forEach(d => {{
      const o=document.createElement("option"); o.value=d;
      o.textContent=fmtDate(d); sel.appendChild(o);
    }});
    currentKey = Object.keys(DATA.days).sort().at(-1);
    sel.value = currentKey; sel.classList.add("visible");
  }} else {{
    sel.classList.remove("visible"); currentKey=null;
  }}
}}

// ── Облако слов ────────────────────────────────────────────────────────────
const cloudCanvas = document.getElementById("wordcloud");
const cloudWrap   = document.getElementById("cloud-wrap");

function drawCloud() {{
  const items = getCloudData();
  const empty = document.getElementById("cloud-empty");
  if (!items.length) {{ empty.style.display="flex"; return; }}
  empty.style.display = "none";
  cloudCanvas.width  = cloudWrap.offsetWidth  || 700;
  cloudCanvas.height = cloudWrap.offsetHeight || 360;
  const maxC  = items[0][1];
  const color = catConfig[currentCat].color;
  WordCloud(cloudCanvas, {{
    list        : items.map(([w,c]) => [w, Math.round(13+(c/maxC)*58)]),
    gridSize    : Math.round(7*cloudCanvas.width/700),
    fontFamily  : "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    color       : (_,w) => w/71>.7 ? color : w/71>.4 ? color+"cc" : color+"77",
    rotateRatio : 0.2, rotationSteps:2,
    backgroundColor:"transparent", drawOutOfBound:false, shrinkToFit:true,
  }});
}}

// ── Топ-список (сайдбар) ───────────────────────────────────────────────────
function drawTopList() {{
  const items = getCloudData().slice(0, 14);
  const maxC  = items[0]?.[1] || 1;
  const color = catConfig[currentCat].color;
  const label = catConfig[currentCat].label.split(" ").slice(1).join(" ");
  document.getElementById("top-list-title").textContent = "Топ — " + label;

  const container = document.getElementById("top-list-items");
  container.innerHTML = "";
  items.forEach(([name, count], i) => {{
    const pct = Math.round((count/maxC)*100);
    const el  = document.createElement("div");
    el.className = "top-item";
    el.innerHTML = `
      <span class="top-rank">${{i+1}}</span>
      <span class="top-name" title="${{name}}">${{name}}</span>
      <div class="top-bar-wrap"><div class="top-bar-fill" style="width:${{pct}}%;background:${{color}}"></div></div>
      <span class="top-count">${{count}}</span>`;
    container.appendChild(el);
  }});
}}

// ── Stacked chart 1: все темы + легенда ───────────────────────────────────
let chart1Inst = null;
function drawChart1() {{
  const sd     = getStackedData(gran1);
  const labels = sd.periods.map(p => fmtPeriod(gran1, p));
  const hilite = (currentPeriod==="week"||currentPeriod==="day") ? sd.periods.indexOf(currentKey) : -1;

  const datasets = sd.entities.map((ent,i) => ({{
    label:ent.length>20?ent.slice(0,18)+"…":ent, fullLabel:ent,
    data:sd.series[ent], backgroundColor:PALETTE[i%PALETTE.length],
    borderColor:"transparent", borderWidth:0,
  }}));
  if (sd.others.some(v=>v>0)) datasets.push({{
    label:"Остальные", fullLabel:"Остальные", data:sd.others,
    backgroundColor:OTHERS_COLOR, borderColor:"#2d3748", borderWidth:1,
  }});

  const ctx = document.getElementById("stackedChart1");
  if (chart1Inst) chart1Inst.destroy();
  chart1Inst = new Chart(ctx, {{
    type:"bar", data:{{labels, datasets}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      interaction:{{mode:"index", intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{backgroundColor:"#1e293b",borderColor:"#334155",borderWidth:1,
                  titleColor:"#e2e8f0",bodyColor:"#94a3b8",
                  callbacks:{{label:c=>` ${{c.dataset.fullLabel}}: ${{c.raw}}`}}}}
      }},
      scales:{{
        x:{{stacked:true, grid:{{display:false}},
            ticks:{{color:(_,i)=>i.index===hilite?"#e2e8f0":"#475569",
                   font:(_,i)=>{{return{{size:gran1==="day"?9:10,weight:i.index===hilite?"700":"400"}}}},
                   maxRotation:gran1==="day"?55:0}}}},
        y:{{stacked:true, grid:{{color:"rgba(255,255,255,0.04)"}}, ticks:{{color:"#475569",font:{{size:10}}}}}}
      }}
    }},
    plugins:[{{id:"h1",beforeDraw(chart){{
      if (hilite<0) return;
      const {{ctx,chartArea:{{top,bottom}}}} = chart;
      const bar = chart.getDatasetMeta(0).data[hilite];
      if (!bar) return;
      chart.ctx.save(); chart.ctx.fillStyle="rgba(255,255,255,0.035)";
      chart.ctx.fillRect(bar.x-bar.width/2-2,top,bar.width+4,bottom-top); chart.ctx.restore();
    }}}}]
  }});

  const leg = document.getElementById("legend1");
  leg.innerHTML = "";
  datasets.forEach(ds => {{
    const item=document.createElement("div"); item.className="legend-item";
    item.innerHTML=`<div class="legend-dot" style="background:${{ds.backgroundColor}}"></div>${{ds.label}}`;
    leg.appendChild(item);
  }});
}}

// ── Stacked chart 2: горячие темы + подписи внутри ────────────────────────
let chart2Inst = null;
const barLabelsPlugin = {{
  id:"barLabels",
  afterDraw(chart) {{
    const {{ctx}} = chart;
    chart.data.datasets.forEach((ds, di) => {{
      chart.getDatasetMeta(di).data.forEach((bar, idx) => {{
        if (!ds.data[idx]) return;
        const segH = Math.abs(bar.base - bar.y);
        if (segH < 15) return;
        const lbl = ds.fullLabel || ds.label || "";
        const maxChars = Math.max(1, Math.floor(bar.width/7));
        const txt = lbl.length > maxChars ? lbl.slice(0, maxChars-1)+"…" : lbl;
        ctx.save();
        ctx.font = `600 ${{Math.min(11, segH*0.42)}}px -apple-system`;
        ctx.fillStyle = "rgba(255,255,255,0.82)";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(txt, bar.x, bar.y + segH/2);
        ctx.restore();
      }});
    }});
  }}
}};

function drawChart2() {{
  const sd     = getStackedData(gran2);
  const labels = sd.periods.map(p => fmtPeriod(gran2, p));
  const hilite = (currentPeriod==="week"||currentPeriod==="day") ? sd.periods.indexOf(currentKey) : -1;

  const datasets = sd.hot_entities.map((ent,i) => ({{
    label:ent.length>20?ent.slice(0,18)+"…":ent, fullLabel:ent,
    data:sd.hot_series[ent], backgroundColor:PALETTE[i%PALETTE.length],
    borderColor:"transparent", borderWidth:0,
  }}));

  const ctx = document.getElementById("stackedChart2");
  if (chart2Inst) chart2Inst.destroy();
  chart2Inst = new Chart(ctx, {{
    type:"bar", data:{{labels, datasets}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      interaction:{{mode:"index", intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{backgroundColor:"#1e293b",borderColor:"#334155",borderWidth:1,
                  titleColor:"#e2e8f0",bodyColor:"#94a3b8",
                  callbacks:{{label:c=>` ${{c.dataset.fullLabel}}: ${{c.raw}}`}}}}
      }},
      scales:{{
        x:{{stacked:true, grid:{{display:false}},
            ticks:{{color:(_,i)=>i.index===hilite?"#e2e8f0":"#475569",
                   font:(_,i)=>{{return{{size:gran2==="day"?9:10,weight:i.index===hilite?"700":"400"}}}},
                   maxRotation:gran2==="day"?55:0}}}},
        y:{{stacked:true, grid:{{color:"rgba(255,255,255,0.04)"}}, ticks:{{color:"#475569",font:{{size:10}}}}}}
      }}
    }},
    plugins:[barLabelsPlugin, {{id:"h2",beforeDraw(chart){{
      if (hilite<0) return;
      const {{ctx,chartArea:{{top,bottom}}}} = chart;
      const bar = chart.getDatasetMeta(0).data[hilite];
      if (!bar) return;
      chart.ctx.save(); chart.ctx.fillStyle="rgba(255,255,255,0.04)";
      chart.ctx.fillRect(bar.x-bar.width/2-2,top,bar.width+4,bottom-top); chart.ctx.restore();
    }}}}]
  }});
}}

// ── Всплески ───────────────────────────────────────────────────────────────
function drawRising() {{
  const weeks = Object.keys(DATA.weeks).sort();
  const chips = document.getElementById("rising-chips");
  chips.innerHTML = "";
  if (!weeks.length) return;
  const last = weeks.at(-1), prev = weeks.at(-2);
  document.getElementById("rising-title").textContent = `🔥 Всплески — нед. ${{fmtWeek(last)}}`;
  const lastData = (DATA.weeks[last]||{{}})[currentCat]||[];
  const prevMap  = Object.fromEntries(((DATA.weeks[prev]||{{}})[currentCat]||[]).map(([t,c])=>[t,c]));
  const cfg = catConfig[currentCat];
  lastData.map(([t,c])=>{{return{{t,c,delta:c-(prevMap[t]||0),isNew:!(prevMap[t])}};}})
    .filter(x=>x.delta>0||x.isNew).sort((a,b)=>b.delta-a.delta).slice(0,22)
    .forEach(({{t,c,delta,isNew}})=>{{
      const chip=document.createElement("div"); chip.className="r-chip";
      chip.style.borderColor=cfg.color+"40"; chip.style.background=cfg.color+"0d";
      chip.innerHTML=`<span class="ricon">${{isNew?"✨":"↑"}}</span><span class="rname">${{t}}</span><span class="rcount" style="color:${{cfg.color}}">${{c}}</span>${{!isNew?`<span class="rdelta">+${{delta}}</span>`:""}}`;
      chips.appendChild(chip);
    }});
}}

// ── Обновление подписей ────────────────────────────────────────────────────
function updateLabels() {{
  document.getElementById("cloud-title").textContent = catConfig[currentCat].label;
  let lbl = "март–апрель 2026";
  if (currentPeriod==="week"&&currentKey) lbl="Нед. "+fmtWeek(currentKey);
  if (currentPeriod==="day" &&currentKey) lbl=fmtDate(currentKey);
  document.getElementById("cloud-period-label").textContent = lbl;
  document.body.dataset.cat = currentCat;
}}

// ── Полный рендер ─────────────────────────────────────────────────────────
function render() {{
  updateLabels(); drawCloud(); drawTopList(); drawChart1(); drawChart2(); drawRising();
}}

// ── События ───────────────────────────────────────────────────────────────
document.querySelectorAll(".cat-btn").forEach(btn=>btn.addEventListener("click",()=>{{
  document.querySelectorAll(".cat-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active"); currentCat=btn.dataset.cat; render();
}}));
document.querySelectorAll(".period-btn").forEach(btn=>btn.addEventListener("click",()=>{{
  document.querySelectorAll(".period-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active"); currentPeriod=btn.dataset.period;
  populateSelect(currentPeriod); render();
}}));
document.getElementById("period-select").addEventListener("change",e=>{{ currentKey=e.target.value; render(); }});
document.querySelectorAll("#gran1-btns .gran-btn").forEach(btn=>btn.addEventListener("click",()=>{{
  document.querySelectorAll("#gran1-btns .gran-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active"); gran1=btn.dataset.gran; drawChart1();
}}));
document.querySelectorAll("#gran2-btns .gran-btn").forEach(btn=>btn.addEventListener("click",()=>{{
  document.querySelectorAll("#gran2-btns .gran-btn").forEach(b=>b.classList.remove("active"));
  btn.classList.add("active"); gran2=btn.dataset.gran; drawChart2();
}}));
window.addEventListener("load",   ()=>setTimeout(render, 120));
window.addEventListener("resize", ()=>setTimeout(drawCloud, 150));
</script>
</body>
</html>"""
