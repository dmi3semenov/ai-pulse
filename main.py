"""
main.py — точка входа AI Pulse Dashboard.

Запуск:
    uv run python main.py

Что делает:
  1. Читает данные из ../community-brain/data/ и ../community-brain/wiki/pages/
  2. Генерирует out/dashboard_YYYY-MM-DD_HHMM.html (новый файл, старые не трогаем)
  3. Выводит путь к файлу — открывай в браузере
"""

from datetime import datetime
from pathlib import Path

# Импортируем логику из generate.py (не дублируем код здесь)
from generate import build_data, render_html

# ── Пути ──────────────────────────────────────────────────────────────────
# Оба проекта лежат рядом в Pet projects/
BASE_DIR        = Path(__file__).parent
COMMUNITY_BRAIN = BASE_DIR.parent / "community-brain"

DATA_DIR        = COMMUNITY_BRAIN / "data"
WIKI_PAGES_DIR  = COMMUNITY_BRAIN / "wiki" / "pages"
OUT_DIR         = BASE_DIR / "out"


def main() -> None:
    # Проверяем что данные есть
    if not DATA_DIR.exists():
        print(f"❌ Папка с данными не найдена: {DATA_DIR}")
        print("   Убедись что community-brain лежит рядом с ai-pulse")
        return
    if not WIKI_PAGES_DIR.exists():
        print(f"❌ Wiki не найдена: {WIKI_PAGES_DIR}")
        print("   Запусти extract_knowledge.py в community-brain чтобы сгенерировать wiki")
        return

    # Создаём out/ если нет
    OUT_DIR.mkdir(exist_ok=True)

    # Строим данные и рендерим HTML
    data = build_data(str(DATA_DIR), str(WIKI_PAGES_DIR))
    html = render_html(data)

    # Версионированное имя файла: старые сборки не перезатираем, чтобы можно было
    # сравнить и при необходимости откатиться.
    ts       = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_file = OUT_DIR / f"dashboard_{ts}.html"
    out_file.write_text(html, encoding="utf-8")

    size_kb = out_file.stat().st_size / 1024
    print(f"\n✅ Готово! {size_kb:.0f} KB → {out_file}")
    print(f"   Открывай в браузере: file://{out_file.resolve()}")


if __name__ == "__main__":
    main()
