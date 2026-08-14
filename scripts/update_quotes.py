#!/usr/bin/env python3
"""
Обновление котировок дашборда из MOEX ISS.

Логика:
- Тянет LAST по всем тикерам через iss.moex.com (без ключей).
- Обновляет ряд close в data/payload.json для сегодняшнего дня:
    * если сегодняшняя дата уже есть в конце ряда — переписывает значение (внутридневное);
    * если нет — добавляет новую точку.
- Пересчитывает зависимые от цены поля (mcap, pe_ltm, pb_ltm, pe_ntm, pb_ntm, ey_ntm,
  таблицу форвардных мультипликаторов, KPI last).
- Обновляет метку D.updated.
- Пишет data/status.json с деталями последнего запуска.

Финализация (флаг --finalize) вызывается последним запуском дня после закрытия рынка —
она ничего дополнительно не меняет в ряду, только записывает status.final=true.
Внутридневная точка и есть цена закрытия последней сделки, что и требуется.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PAYLOAD_PATH = DATA_DIR / "payload.json"
STATUS_PATH = DATA_DIR / "status.json"
QUOTES_PATH = DATA_DIR / "quotes.json"  # компактный лог последних котировок

# MOEX тикеры и board'ы. IMOEX — индекс на SNDX.
SHARE_TICKERS = ["SBER", "VTBR", "T", "DOMRF", "SVCB", "BSPB", "BSPBP", "MBNK"]
INDEX_TICKERS = ["IMOEX"]

# Тикеры дашборда (для БСПБ — только BSPB как основной ряд;
# BSPBP тянем отдельно чтобы правильно пересчитать капитализацию с префами).
DASHBOARD_TICKERS = ["SBER", "T", "VTBR", "DOMRF", "SVCB", "BSPB", "MBNK"]

# Число префов BSPBP (для расчёта mcap с учётом префов)
BSPBP_SHARES = 20_100_000  # штук


def http_get_json(url: str, timeout: int = 20, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "imoex-dashboard/1.0"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (URLError, HTTPError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"HTTP failed after {retries} tries: {url} — {last_err}")


def fetch_moex_last() -> dict[str, dict]:
    """Возвращает {ticker: {last, prev, updatetime, date}}."""
    out: dict[str, dict] = {}

    # 1) Акции через TQBR
    q = ",".join(SHARE_TICKERS)
    url = (
        "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
        f"securities.json?iss.meta=off&iss.only=marketdata,securities&securities={q}"
    )
    d = http_get_json(url)
    md_cols = d["marketdata"]["columns"]
    sec_cols = d["securities"]["columns"]
    md_by = {row[md_cols.index("SECID")]: row for row in d["marketdata"]["data"]}
    sec_by = {row[sec_cols.index("SECID")]: row for row in d["securities"]["data"]}

    for tk in SHARE_TICKERS:
        md = md_by.get(tk)
        sec = sec_by.get(tk)
        if md is None or sec is None:
            print(f"WARN: {tk} not in MOEX response", file=sys.stderr)
            continue
        last = md[md_cols.index("LAST")]
        prev = sec[sec_cols.index("PREVPRICE")]
        # LAST может быть None, если сегодня нет сделок — тогда возьмём PREVPRICE
        upd = md[md_cols.index("UPDATETIME")] if "UPDATETIME" in md_cols else None
        systime = md[md_cols.index("SYSTIME")] if "SYSTIME" in md_cols else None
        out[tk] = {
            "last": last,
            "prev": prev,
            "updatetime": upd,
            "systime": systime,
        }

    # 2) Индексы (IMOEX). У индекса LAST/PREV лежат в marketdata: CURRENTVALUE и LASTVALUE.
    for tk in INDEX_TICKERS:
        url = (
            f"https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/"
            f"securities/{tk}.json?iss.meta=off&iss.only=marketdata"
        )
        d = http_get_json(url)
        md_cols = d["marketdata"]["columns"]
        if d["marketdata"]["data"]:
            md = d["marketdata"]["data"][0]
            def _get(col):
                return md[md_cols.index(col)] if col in md_cols else None
            last = _get("CURRENTVALUE")
            prev = _get("LASTVALUE")
            upd = _get("UPDATETIME")
            systime = _get("SYSTIME")
            out[tk] = {"last": last, "prev": prev, "updatetime": upd, "systime": systime}
    return out


def recompute_ticker(payload: dict, tk: str, new_close: float, today_iso: str) -> None:
    """Обновляет payload по одному тикеру: close ряды, mcap, PE/PB (LTM+NTM), last."""
    meta = payload["meta"][tk]
    s = meta["series"]

    # 1) Обновляем ряд close
    close = s["close"]
    if close["x"] and close["x"][-1] == today_iso:
        close["y"][-1] = round(float(new_close), 4)
    else:
        close["x"].append(today_iso)
        close["y"].append(round(float(new_close), 4))

    # 2) shares outstanding из предыдущего mcap/close (стабильно)
    last_prev = payload["last"][tk]
    # Возьмём shares из СОХРАНЁННОГО snapshot, не пересчитанного
    shares = payload.get("_shares", {}).get(tk)
    if shares is None:
        # backfill из last (первый запуск)
        prev_close = last_prev["close"]
        prev_mcap_bln = last_prev["mcap"]
        shares = prev_mcap_bln * 1e9 / prev_close  # штук
        payload.setdefault("_shares", {})[tk] = shares

    new_mcap_bln = shares * new_close / 1e9  # в млрд руб

    # 3) Пересчёт LTM-мультипликаторов
    # Берём NI_ltm и EQUITY из last (это последние отчётные значения, не зависят от цены)
    ni_ltm = last_prev["net_income_parent_ltm"]  # млрд
    # equity_parent: возьмём из ряда equity_parent (последняя точка)
    eq_series = s.get("equity_parent", {})
    eq_last = eq_series["y"][-1] if eq_series.get("y") else None

    pe_ltm = new_mcap_bln / ni_ltm if ni_ltm else None
    pb_ltm = new_mcap_bln / eq_last if eq_last else None

    # ptbv_ltm: (equity - goodwill). Оставим прежний множитель pb_ltm.
    prev_pb = last_prev.get("pb_ltm")
    prev_ptbv = last_prev.get("ptbv_ltm")
    ptbv_ltm = None
    if pb_ltm is not None and prev_pb and prev_ptbv:
        ptbv_ltm = pb_ltm * (prev_ptbv / prev_pb)

    # 4) NTM P/E, P/B — берём коэффициенты как отношение к текущим значениям
    prev_pe_ntm = last_prev.get("pe_ntm")
    prev_pb_ntm = last_prev.get("pb_ntm")
    prev_pe_ltm = last_prev.get("pe_ltm")
    prev_pb_ltm_ = last_prev.get("pb_ltm")
    pe_ntm = None
    pb_ntm = None
    if prev_pe_ntm and prev_pe_ltm and pe_ltm is not None:
        pe_ntm = pe_ltm * (prev_pe_ntm / prev_pe_ltm)
    if prev_pb_ntm and prev_pb_ltm_ and pb_ltm is not None:
        pb_ntm = pb_ltm * (prev_pb_ntm / prev_pb_ltm_)

    ey_ntm = (100.0 / pe_ntm) if pe_ntm else None

    # 5) Обновляем ряды mcap, pe_ltm, pb_ltm, ptbv_ltm, ey, pe_ntm, pb_ntm, ey_ntm
    def _upsert(series_key: str, val):
        if val is None:
            return
        ser = s.get(series_key)
        if not ser:
            return
        if ser["x"] and ser["x"][-1] == today_iso:
            ser["y"][-1] = round(float(val), 6)
        else:
            ser["x"].append(today_iso)
            ser["y"].append(round(float(val), 6))

    _upsert("mcap", new_mcap_bln)
    _upsert("pe_ltm", pe_ltm)
    _upsert("pb_ltm", pb_ltm)
    _upsert("ptbv_ltm", ptbv_ltm)
    _upsert("pe_ntm", pe_ntm)
    _upsert("pb_ntm", pb_ntm)
    if pe_ltm:
        _upsert("ey", 100.0 / pe_ltm)
    _upsert("ey_ntm", ey_ntm)

    # 6) tr_index — приблизительное обновление: сохраняем предыдущее отношение
    tr = s.get("tr_index")
    close_hist = s["close"]
    if tr and tr["x"]:
        # tr = close_ratio_since_start * (1 + reinvested_div/close). Проще: скорректировать пропорц. изменению цены
        prev_close = close_hist["y"][-2] if len(close_hist["y"]) >= 2 else close_hist["y"][-1]
        prev_tr_last = tr["y"][-1]
        if close_hist["x"][-1] == today_iso and len(close_hist["y"]) >= 2:
            # обновление внутри дня — база: цена вчерашнего close + tr
            # tr[-1] соответствует новой цене; пересчитаем от tr[-2]
            base_tr = tr["y"][-2] if len(tr["y"]) >= 2 else prev_tr_last
            base_px = close_hist["y"][-2]
            new_tr = base_tr * (new_close / base_px)
            tr["y"][-1] = round(new_tr, 6)
        else:
            # добавляем новую точку
            new_tr = prev_tr_last * (new_close / prev_close)
            tr["x"].append(today_iso)
            tr["y"].append(round(new_tr, 6))

    # dd_tr — drawdown TR; приблизительно = (tr/tr_peak - 1)*100
    if tr and s.get("dd_tr"):
        peak = max(tr["y"])
        dd_val = (tr["y"][-1] / peak - 1.0) * 100.0
        dd = s["dd_tr"]
        if dd["x"] and dd["x"][-1] == today_iso:
            dd["y"][-1] = round(dd_val, 4)
        else:
            dd["x"].append(today_iso)
            dd["y"].append(round(dd_val, 4))

    # 7) Обновляем last[tk]
    prev_last = payload["last"][tk]
    y_all = close_hist["y"]
    x_all = close_hist["x"]

    def _find_offset(days: int) -> float | None:
        target = datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=days)
        # ищем ближайшую предыдущую дату
        for i in range(len(x_all) - 1, -1, -1):
            di = datetime.strptime(x_all[i], "%Y-%m-%d")
            if di <= target:
                return y_all[i]
        return None

    chg = None
    if len(y_all) >= 2:
        chg = (y_all[-1] / y_all[-2] - 1) * 100.0

    px_1y = _find_offset(365)
    px_3m = _find_offset(90)
    r1y = ((y_all[-1] / px_1y - 1) * 100.0) if px_1y else None
    r3m = ((y_all[-1] / px_3m - 1) * 100.0) if px_3m else None

    # tr returns
    tr_y = tr["y"] if tr else None
    tr_x = tr["x"] if tr else None
    r1y_tr = None
    r3m_tr = None
    if tr_y and tr_x:
        def _tr_offset(days: int) -> float | None:
            target = datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=days)
            for i in range(len(tr_x) - 1, -1, -1):
                di = datetime.strptime(tr_x[i], "%Y-%m-%d")
                if di <= target:
                    return tr_y[i]
            return None
        base_1y = _tr_offset(365)
        base_3m = _tr_offset(90)
        r1y_tr = ((tr_y[-1] / base_1y - 1) * 100.0) if base_1y else None
        r3m_tr = ((tr_y[-1] / base_3m - 1) * 100.0) if base_3m else None

    prev_last.update({
        "close": round(float(new_close), 4),
        "mcap": round(new_mcap_bln, 3),
        "pe_ltm": round(pe_ltm, 3) if pe_ltm else None,
        "pb_ltm": round(pb_ltm, 3) if pb_ltm else None,
        "ptbv_ltm": round(ptbv_ltm, 3) if ptbv_ltm else None,
        "pe_ntm": round(pe_ntm, 3) if pe_ntm else None,
        "pb_ntm": round(pb_ntm, 3) if pb_ntm else None,
        "ey_ntm": round(ey_ntm, 3) if ey_ntm else None,
        "chg": round(chg, 2) if chg is not None else None,
        "r1y": round(r1y, 1) if r1y is not None else None,
        "r3m": round(r3m, 1) if r3m is not None else None,
        "r1y_tr": round(r1y_tr, 1) if r1y_tr is not None else None,
        "r3m_tr": round(r3m_tr, 1) if r3m_tr is not None else None,
    })


def recompute_forward_table(payload: dict) -> None:
    """Пересчитывает pe/pb в D.table.rows под новую капитализацию.
    dy оставляем: для 'paid' лет — фиксированное поле от даты отсечки, для будущих — dps/close_now*100."""
    if "table" not in payload:
        return
    for tk, row in payload["table"]["rows"].items():
        last = payload["last"].get(tk)
        if not last:
            continue
        mcap = last["mcap"]
        close_now = last["close"]
        # восстановим NI[y], EQ[y] один раз и сохраним в payload._fwd для стабильности
        fwd_const = payload.setdefault("_fwd_const", {}).setdefault(tk, {})
        for year, cell in row.items():
            if year not in fwd_const:
                # bootstrap: восстановим NI и EQ из старой (pre-update) записи
                # Используем прежние pe/pb и прежний mcap — считаем один раз при первом запуске.
                # Если это не первый запуск, fwd_const уже заполнено при бутстрапе (в migrate_bootstrap).
                pe = cell.get("pe"); pb = cell.get("pb")
                # Здесь mcap может уже быть обновлён. Сохранён исходный snapshot через _shares.
                # Восстанавливаем через СТАРЫЕ pe/pb и СТАРЫЕ NI/EQ = mcap_prev/pe:
                # Но у нас нет mcap_prev. Логика бутстрапа перенесена в migrate_bootstrap().
                continue
            ni_y = fwd_const[year].get("ni")
            eq_y = fwd_const[year].get("eq")
            if ni_y:
                cell["pe"] = round(mcap / ni_y, 3)
            if eq_y:
                cell["pb"] = round(mcap / eq_y, 3)
            # dividend yield для непроплаченных — от текущей цены
            dps = cell.get("dps")
            if dps and not cell.get("paid"):
                cell["dy"] = round(dps / close_now * 100.0, 2)


def migrate_bootstrap(payload: dict) -> None:
    """Однократный бутстрап: сохраняет shares и NI/EQ по годам из исходного payload,
    чтобы последующие пересчёты были стабильны."""
    if payload.get("_bootstrap_done"):
        return

    # 1) shares outstanding
    shares_map = {}
    for tk, last in payload["last"].items():
        px = last["close"]; mcap_bln = last["mcap"]
        shares_map[tk] = mcap_bln * 1e9 / px
    payload["_shares"] = shares_map

    # 2) NI/EQ per year из table.rows (pe, pb) и текущего mcap
    fwd_const = {}
    for tk, row in payload.get("table", {}).get("rows", {}).items():
        mcap = payload["last"][tk]["mcap"]
        d = {}
        for year, cell in row.items():
            pe = cell.get("pe"); pb = cell.get("pb")
            d[year] = {
                "ni": (mcap / pe) if pe else None,
                "eq": (mcap / pb) if pb else None,
            }
        fwd_const[tk] = d
    payload["_fwd_const"] = fwd_const

    payload["_bootstrap_done"] = True


def load_payload() -> dict:
    with open(PAYLOAD_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_payload(payload: dict) -> None:
    tmp = PAYLOAD_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, PAYLOAD_PATH)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize", action="store_true",
                        help="Финальный прогон дня (после закрытия рынка): помечает точку как закрытие.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now_msk = datetime.now(MSK)
    today = now_msk.date().isoformat()

    payload = load_payload()
    migrate_bootstrap(payload)

    moex = fetch_moex_last()

    per_ticker = {}
    for tk in DASHBOARD_TICKERS:
        q = moex.get(tk)
        if not q:
            print(f"WARN: no MOEX quote for {tk}", file=sys.stderr)
            continue
        # Для BSPB нужно скорректировать shares: обыкновенные + префы*(цена_bspbp/цена_bspb)
        # Но в payload shares уже включают эквивалент — mcap = shares_eff * close_bspb.
        # Для точности пересчитаем эффективный mcap отдельно: обыкновенные*close + префы*close_pref
        if tk == "BSPB":
            bspbp = moex.get("BSPBP", {})
            bspbp_last = bspbp.get("last") or bspbp.get("prev")
            close = q["last"] if q["last"] is not None else q["prev"]
            if close is None:
                continue
            # Обыкновенные акции = shares_common (из устава 445_828_521)
            shares_common = 445_828_521
            eff_mcap = shares_common * close + BSPBP_SHARES * (bspbp_last if bspbp_last else close)
            # Обновим "shares_eff" так, чтобы shares_eff * close = eff_mcap
            payload["_shares"][tk] = eff_mcap / close
            new_close = close
        else:
            close = q["last"] if q["last"] is not None else q["prev"]
            if close is None:
                continue
            new_close = close

        recompute_ticker(payload, tk, new_close, today)
        per_ticker[tk] = {"close": new_close, "systime": q.get("systime"), "updatetime": q.get("updatetime")}

    recompute_forward_table(payload)

    # Метаданные
    payload["updated"] = now_msk.strftime("%d.%m.%Y %H:%M МСК")

    # IMOEX — сохраним в payload на случай будущего графика
    imoex = moex.get("IMOEX")
    if imoex:
        payload.setdefault("_index", {})["IMOEX"] = {
            "last": imoex["last"], "prev": imoex["prev"],
            "date": today, "updated": payload["updated"],
        }

    status = {
        "updated_at": now_msk.isoformat(),
        "date": today,
        "finalize": bool(args.finalize),
        "tickers": per_ticker,
        "imoex": imoex,
    }

    if not args.dry_run:
        save_payload(payload)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        # Компактный лог последних котировок (последние 30 дней по каждому банку)
        quotes_log = {"updated_at": now_msk.isoformat(), "quotes": {}}
        for tk in DASHBOARD_TICKERS:
            s = payload["meta"][tk]["series"]["close"]
            quotes_log["quotes"][tk] = {"x": s["x"][-30:], "y": s["y"][-30:]}
        with open(QUOTES_PATH, "w", encoding="utf-8") as f:
            json.dump(quotes_log, f, ensure_ascii=False, indent=2)

    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
