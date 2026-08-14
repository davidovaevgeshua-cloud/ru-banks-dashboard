#!/usr/bin/env python3
"""
Ежедневная проверка выхода новой квартальной МСФО-отчётности российских банков.

Источник: smart-lab.ru — для каждого тикера отдаёт таблицу с колонками YYYYQn
и датой отчёта. Смотрим на самый свежий заголовок колонки и сравниваем с
state/reports_last_seen.json.

Если для банка появился новый квартал:
  - формируется запись в списке "new"
  - если ещё не уведомляли за этот квартал (last_notified[tk] != new_q),
    добавляется в письмо
  - state[last_notified][tk] = new_q после отправки

Само поле state[seen] переписывается ТОЛЬКО когда payload был обновлён вручную
(отдельная команда --mark-seen TK QUARTER), чтобы уведомления не пропадали
между запусками до обновления дашборда.

Использование:
    python check_reports.py                # прогон, писать письмо если Resend key задан
    python check_reports.py --dry-run      # прогон, не слать письмо, вывести отчёт в stdout
    python check_reports.py --mark-seen SBER 2026Q3   # зафиксировать, что квартал уже в payload
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "reports_last_seen.json"

TICKERS = ["SBER", "VTBR", "T", "DOMRF", "SVCB", "BSPB", "MBNK"]

BANK_NAMES = {
    "SBER": "Сбербанк",
    "VTBR": "ВТБ",
    "T": "Т-Технологии",
    "DOMRF": "ДОМ.РФ",
    "SVCB": "Совкомбанк",
    "BSPB": "Банк Санкт-Петербург",
    "MBNK": "МТС Банк",
}

SMARTLAB_URL = "https://smart-lab.ru/q/{tk}/f/q/MSFO/"

IR_LINKS = {
    "SBER": "https://www.sberbank.com/ru/investor-relations/groupresults",
    "VTBR": "https://www.vtb.ru/ir/statements/results/",
    "T": "https://t-technologies.ru/results/",
    "DOMRF": "https://xn--d1aqf.xn--p1ai/investors/reports/msfo/",
    "SVCB": "https://sovcombank.ru/about/finances",
    "BSPB": "https://www.bspb.ru/investors/financial-statements/IFRS",
    "MBNK": "https://www.mtsbank.ru/investors-and-shareholders/reports/",
}

MOSCOW_TZ_OFFSET_HOURS = 3

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def http_get(url: str, timeout: int = 30, retries: int = 4) -> str:
    """GET с retry: smart-lab периодически отдаёт 502/504 gateway timeout."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"})
            with urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode("utf-8", errors="replace")
        except HTTPError as e:
            last_err = e
            if e.code in (502, 503, 504, 429):
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (URLError, TimeoutError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    assert last_err is not None
    raise last_err


def parse_smartlab(html: str) -> Optional[dict]:
    """
    Ищет самый свежий заголовок колонки формата YYYYQn и дату отчёта под ним.
    Возвращает {"latest": "2026Q2", "report_date": "11.08.2026"} или None.
    """
    # Все заголовки квартальных колонок
    q_headers = re.findall(r"\b(20\d{2})Q([1-4])\b", html)
    if not q_headers:
        return None
    # Сортируем: (year, quarter) → максимум
    quarters = sorted({(int(y), int(q)) for y, q in q_headers})
    y, q = quarters[-1]
    latest = f"{y}Q{q}"

    # Дата отчёта: обычно в ячейке под годом колонки. Ищем DD.MM.YYYY
    # рядом с латинским заголовком. Простая эвристика: найдём все даты
    # вида DD.MM.YYYY в HTML, отфильтруем те, что >= начала соответствующего
    # квартала и <= сегодня, и возьмём максимум.
    dates = re.findall(r"\b([0-3]\d)\.([01]\d)\.(20\d{2})\b", html)
    quarter_start = datetime(y, (q - 1) * 3 + 1, 1)
    today = datetime.utcnow() + _msk_offset()
    latest_report_date = None
    for dd, mm, yy in dates:
        try:
            dt = datetime(int(yy), int(mm), int(dd))
        except ValueError:
            continue
        if quarter_start <= dt <= today:
            if latest_report_date is None or dt > latest_report_date:
                latest_report_date = dt
    return {
        "latest": latest,
        "report_date": latest_report_date.strftime("%d.%m.%Y") if latest_report_date else None,
    }


def _msk_offset():
    from datetime import timedelta
    return timedelta(hours=MOSCOW_TZ_OFFSET_HOURS)


def quarter_gt(a: str, b: str) -> bool:
    """a=2026Q3, b=2026Q2 → True."""
    ay, aq = int(a[:4]), int(a[5])
    by, bq = int(b[:4]), int(b[5])
    return (ay, aq) > (by, bq)


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_all() -> tuple[list[dict], list[dict]]:
    """
    Возвращает (new_reports, errors).
    new_reports: список dict {tk, name, seen_quarter, new_quarter, report_date, smartlab, ir}
    errors:      список dict {tk, name, error}
    """
    state = load_state()
    seen = state["seen"]
    new_reports = []
    errors = []
    for tk in TICKERS:
        url = SMARTLAB_URL.format(tk=tk)
        try:
            html = http_get(url)
            parsed = parse_smartlab(html)
            if not parsed:
                errors.append({"tk": tk, "name": BANK_NAMES[tk], "error": "не смог распарсить smart-lab"})
                continue
            latest = parsed["latest"]
            if quarter_gt(latest, seen.get(tk, "0000Q0")):
                new_reports.append({
                    "tk": tk,
                    "name": BANK_NAMES[tk],
                    "seen_quarter": seen.get(tk),
                    "new_quarter": latest,
                    "report_date": parsed["report_date"],
                    "smartlab": url,
                    "ir": IR_LINKS[tk],
                })
        except (HTTPError, URLError, TimeoutError) as e:
            errors.append({"tk": tk, "name": BANK_NAMES[tk], "error": f"сетевая ошибка: {e}"})
        except Exception as e:
            errors.append({"tk": tk, "name": BANK_NAMES[tk], "error": f"{type(e).__name__}: {e}"})
        # Небольшая пауза, чтобы не долбить smart-lab
        time.sleep(0.5)

    now_utc = datetime.now(timezone.utc)
    state["last_check"] = now_utc.isoformat()
    save_state(state)

    return new_reports, errors


def format_email(new_reports: list[dict], errors: list[dict]) -> tuple[str, str, str]:
    """Возвращает (subject, text_body, html_body)."""
    now_msk = (datetime.utcnow() + _msk_offset()).strftime("%d.%m.%Y %H:%M МСК")

    if not new_reports:
        subject = f"[RU Banks] Новых МСФО нет — {now_msk}"
    else:
        banks = ", ".join(r["tk"] for r in new_reports)
        subject = f"[RU Banks] Новая МСФО: {banks}"

    # Text
    lines = [f"Проверка МСФО-отчётностей российских банков — {now_msk}", ""]
    if new_reports:
        lines.append("=== НОВЫЕ ОТЧЁТЫ ===")
        for r in new_reports:
            lines.append("")
            lines.append(f"{r['name']} ({r['tk']}): {r['new_quarter']}")
            if r["report_date"]:
                lines.append(f"  Дата публикации: {r['report_date']}")
            lines.append(f"  Было в дашборде: {r['seen_quarter']}")
            lines.append(f"  IR: {r['ir']}")
            lines.append(f"  Smart-Lab: {r['smartlab']}")
        lines.append("")
        lines.append("Скачайте датабук с IR-страницы и пришлите в чат Perplexity — я обновлю дашборд.")
    else:
        lines.append("Новых квартальных отчётов не обнаружено.")
    if errors:
        lines.append("")
        lines.append("=== ОШИБКИ ===")
        for e in errors:
            lines.append(f"  {e['name']} ({e['tk']}): {e['error']}")
    text_body = "\n".join(lines)

    # HTML
    html = [f"<p><b>Проверка МСФО-отчётностей российских банков</b><br>{now_msk}</p>"]
    if new_reports:
        html.append("<h2 style='color:#2b7a3f'>Новые отчёты</h2>")
        html.append("<table cellpadding='8' cellspacing='0' border='1' style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px'>")
        html.append("<tr style='background:#f2f2f2'><th align='left'>Банк</th><th align='left'>Квартал</th><th align='left'>Публикация</th><th align='left'>Было</th><th align='left'>Ссылки</th></tr>")
        for r in new_reports:
            html.append(
                "<tr>"
                f"<td><b>{r['name']}</b> <span style='color:#888'>({r['tk']})</span></td>"
                f"<td>{r['new_quarter']}</td>"
                f"<td>{r['report_date'] or '—'}</td>"
                f"<td style='color:#888'>{r['seen_quarter']}</td>"
                f"<td><a href='{r['ir']}'>IR</a> · <a href='{r['smartlab']}'>Smart-Lab</a></td>"
                "</tr>"
            )
        html.append("</table>")
        html.append("<p style='color:#555;margin-top:16px'>Скачайте датабук с IR-страницы и пришлите в чат Perplexity — я обновлю дашборд.</p>")
    else:
        html.append("<p style='color:#666'>Новых квартальных отчётов не обнаружено.</p>")
    if errors:
        html.append("<h3 style='color:#a33'>Ошибки</h3><ul>")
        for e in errors:
            html.append(f"<li>{e['name']} ({e['tk']}): {e['error']}</li>")
        html.append("</ul>")

    html_body = "\n".join(html)
    return subject, text_body, html_body


def send_email(subject: str, text_body: str, html_body: str, to_addr: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY не задан в окружении")
    from_addr = os.environ.get("RESEND_FROM", "RU Banks Dashboard <onboarding@resend.dev>")

    import json as _json
    import urllib.request as _ur
    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    req = _ur.Request(
        "https://api.resend.com/emails",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _ur.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", errors="replace")
        if r.status >= 300:
            raise RuntimeError(f"Resend вернул {r.status}: {body}")
        print(f"[email] отправлено: {body[:200]}")


def mark_notified(new_reports: list[dict]) -> None:
    state = load_state()
    ln = state.setdefault("last_notified", {})
    for r in new_reports:
        ln[r["tk"]] = r["new_quarter"]
    save_state(state)


def filter_already_notified(new_reports: list[dict]) -> list[dict]:
    state = load_state()
    ln = state.get("last_notified", {})
    result = []
    for r in new_reports:
        if ln.get(r["tk"]) != r["new_quarter"]:
            result.append(r)
    return result


def cmd_mark_seen(tk: str, quarter: str) -> None:
    if tk not in TICKERS:
        raise SystemExit(f"неизвестный тикер {tk}")
    if not re.fullmatch(r"20\d{2}Q[1-4]", quarter):
        raise SystemExit(f"квартал должен быть формата YYYYQn, дано: {quarter}")
    state = load_state()
    prev = state["seen"].get(tk)
    state["seen"][tk] = quarter
    save_state(state)
    print(f"[mark-seen] {tk}: {prev} → {quarter}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Не слать email, вывести в stdout")
    ap.add_argument("--force", action="store_true", help="Слать email, даже если для банка уже уведомляли этот квартал")
    ap.add_argument("--mark-seen", nargs=2, metavar=("TICKER", "QUARTER"),
                    help="Пометить квартал как загруженный в payload (например: --mark-seen SBER 2026Q3)")
    ap.add_argument("--to", default=os.environ.get("REPORTS_EMAIL_TO", "e.davydova@ukmil.ru"),
                    help="Куда слать письмо")
    args = ap.parse_args()

    if args.mark_seen:
        cmd_mark_seen(args.mark_seen[0], args.mark_seen[1])
        return 0

    new_reports, errors = check_all()

    if not args.force:
        candidates_for_email = filter_already_notified(new_reports)
    else:
        candidates_for_email = new_reports

    print(f"Найдено новых отчётов: {len(new_reports)} (после фильтрации на дубли: {len(candidates_for_email)})")
    for r in new_reports:
        print(f"  {r['tk']}: {r['seen_quarter']} → {r['new_quarter']} (публ: {r['report_date']})")
    for e in errors:
        print(f"  ERR {e['tk']}: {e['error']}")

    if args.dry_run:
        subject, text_body, html_body = format_email(candidates_for_email, errors)
        print()
        print(f"SUBJECT: {subject}")
        print()
        print(text_body)
        return 0

    # Пишем письмо только если есть новые отчёты (тихий режим для будних дней без публикаций)
    if candidates_for_email:
        subject, text_body, html_body = format_email(candidates_for_email, errors)
        send_email(subject, text_body, html_body, args.to)
        mark_notified(candidates_for_email)
    else:
        print("[email] нет новых отчётов — письмо не отправляется")

    return 0


if __name__ == "__main__":
    sys.exit(main())
