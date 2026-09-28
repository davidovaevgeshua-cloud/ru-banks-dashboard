#!/usr/bin/env python3
"""One-off: внести МСФО 2К26 (SVCB, BSPB, MBNK), исправить дивиденды 2026,
восстановить ряд total return после 10.08.2026 и дотянуть квартальные ряды до сегодня."""
import json, bisect
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "data" / "payload.json"
ORIG = Path("/tmp/p0.json")  # payload из первого коммита (исходные tr_index на 10.08)
d = json.load(open(P))
d0 = json.load(open(ORIG))

# ---------- 0. Убрать субботние точки (поздний запуск финального обновления пятницы) ----------
for tk in d["order"]:
    for key, ser in d["meta"][tk]["series"].items():
        xs, ys = ser["x"], ser["y"]
        nx, ny = [], []
        for x, y in zip(xs, ys):
            dt = date.fromisoformat(x)
            if x > "2026-08-10" and dt.weekday() >= 5:
                fri = (dt - timedelta(days=dt.weekday() - 4)).isoformat()
                if nx and nx[-1] == fri:
                    ny[-1] = y          # суббота = итог пятницы с вечерней сессией
                else:
                    nx.append(fri); ny.append(y)
                continue
            nx.append(x); ny.append(y)
        ser["x"], ser["y"] = nx, ny

STEP = ["roe_ltm", "roe_q", "roe_4q", "roa_q", "nim_q", "nim_4q", "cir_q", "cor_q", "npl",
        "car_h20", "car_t1", "net_income_parent_ltm", "nii_ltm", "nfi_ltm", "equity_parent",
        "assets", "loans", "deposits", "roe_adj_q", "ni_growth_yoy"]

# ---------- 1. Новые квартальные значения ----------
# LTM ЧП = LTM(1К26) + ЧП 2К26 − ЧП 2К25; ЧП 2К25 = ЧП 2К26 / (1 + рост г/г)
REP = {
    "SVCB": dict(pub="2026-08-14", ni_q=24.704, yoy=487.0, roe_q=24.0, roa_q=2.2033, nim_q=7.6,
                 cir_q=42.0, cor_q=2.2, assets=4485.0, equity_parent=445.503, loans=3006.0,
                 deposits=3519.318, car_h20=11.0),
    "BSPB": dict(pub="2026-08-21", ni_q=5.58, yoy=-39.0, roe_q=10.1, nim_q=6.4, cir_q=37.75,
                 assets=1353.699, equity_parent=218.353, loans=983.438, deposits=909.336,
                 npl=4.0, car_h20=19.26, car_t1=17.3),
    "MBNK": dict(pub="2026-08-25", ni_q=4.733, yoy=89.7, roe_q=14.9, roa_q=2.6, nim_q=8.5,
                 cir_q=37.2, cor_q=6.7, assets=692.766, equity_parent=126.198, loans=339.7,
                 deposits=369.5, npl=8.6, car_h20=12.5),
}

def steps(ser):
    out, prev = [], object()
    for x, y in zip(ser["x"], ser["y"]):
        if y != prev:
            out.append((x, y)); prev = y
    return out

audit = {}
for tk, r in REP.items():
    s = d["meta"][tk]["series"]
    ni_ltm_old = s["net_income_parent_ltm"]["y"][-1]
    ni_q2_25 = r["ni_q"] / (1 + r["yoy"] / 100)
    r["net_income_parent_ltm"] = round(ni_ltm_old + r["ni_q"] - ni_q2_25, 3)
    eq_pts = [y for _, y in steps(s["equity_parent"])][-3:] + [r["equity_parent"]]
    r["roe_ltm"] = round(r["net_income_parent_ltm"] / (sum(eq_pts) / 4) * 100, 4)
    r["roe_adj_q"] = r["roe_q"]  # банк не раскрыл скорректированный ROE
    audit[tk] = dict(ni_ltm_old=ni_ltm_old, ni_q2_25=round(ni_q2_25, 3),
                     ni_ltm_new=r["net_income_parent_ltm"], roe_ltm=round(r["roe_ltm"], 2))

# ---------- 2. Квартальные ряды: дотянуть до последней даты котировок ----------
for tk in d["order"]:
    s = d["meta"][tk]["series"]
    cx = s["close"]["x"]
    r = REP.get(tk, {})
    for k in STEP:
        ser = s.get(k)
        if not ser or not ser["x"]:
            continue
        last_x, last_y = ser["x"][-1], ser["y"][-1]
        new_v = r.get(k)
        for x in cx:
            if x <= last_x:
                continue
            ser["x"].append(x)
            ser["y"].append(new_v if (new_v is not None and x >= r["pub"]) else last_y)

# ---------- 3. LTM-мультипликаторы с даты публикации ----------
for tk, r in REP.items():
    s = d["meta"][tk]["series"]; L = d["last"][tk]
    ratio = (L["ptbv_ltm"] / L["pb_ltm"]) if L.get("pb_ltm") and L.get("ptbv_ltm") else 1.0
    ni, eq = r["net_income_parent_ltm"], r["equity_parent"]
    mc = dict(zip(s["mcap"]["x"], s["mcap"]["y"]))
    for key, fn in [("pe_ltm", lambda m: m / ni), ("pb_ltm", lambda m: m / eq),
                    ("ptbv_ltm", lambda m: m / eq * ratio), ("ey", lambda m: 100 * ni / m)]:
        ser = s[key]
        ser["y"] = [round(fn(mc[x]), 6) if (x >= r["pub"] and x in mc) else y
                    for x, y in zip(ser["x"], ser["y"])]
    m = L["mcap"]
    L.update(net_income_parent_ltm=ni, roe_ltm=round(r["roe_ltm"], 3), nim_q=r["nim_q"],
             cir_q=r["cir_q"], car_h20=r["car_h20"], period="2026-06-30",
             pe_ltm=round(m / ni, 3), pb_ltm=round(m / eq, 3), ptbv_ltm=round(m / eq * ratio, 3))
    if "cor_q" in r and "cor_q" in L:
        L["cor_q"] = r["cor_q"]

# ---------- 4. Дивиденды 2026 (даты — закрытие реестра) ----------
PAY26 = {
    "SBER":  [(37.64, "2026-07-20")],
    "VTBR":  [(9.71, "2026-07-20")],
    "DOMRF": [(246.88, "2026-07-20")],
    "SVCB":  [(0.35, "2026-07-13")],                       # было 0.50 — ошибка
    "BSPB":  [(26.23, "2026-05-12"), (19.17, "2026-10-05")],  # было 31.50 от 05.05
    "MBNK":  [(96.12, "2026-07-10")],                      # не было отмечено
    "T":     [(3.6, "2026-01-08"), (4.5, "2026-05-25"), (4.6, "2026-08-10"), (4.7, "2026-10-12")],
}
DROP = {"SVCB": ["2026-07-13"], "BSPB": ["2026-05-05", "2024-10-07"],
        "T": ["2025-06-02", "2025-09-01", "2025-12-01", "2026-06-01"]}
# исправления истории (ключ = первый день без дивиденда)
HIST = {"MBNK": {"2025-07-11": 89.31},
        "BSPB": {"2024-09-30": 27.26, "2023-05-10": 21.16},
        "T": {"2025-05-16": 3.2, "2025-07-17": 3.3, "2025-10-06": 3.5}}
for tk, keys in DROP.items():
    for k in keys:
        d["divs"][tk].pop(k, None)
for tk, h in HIST.items():
    d["divs"][tk].update(h)
    d["divs"][tk] = dict(sorted(d["divs"][tk].items()))
for tk, pays in PAY26.items():
    for dps, rd in pays:
        d["divs"][tk][rd] = dps
    d["divs"][tk] = dict(sorted(d["divs"][tk].items()))
    cell = d["table"]["rows"][tk]["2026"]
    for k in ("px_ref", "rec_date"):
        cell.pop(k, None)
    cell["payments"] = [{"dps": dps, "rec_date": rd} for dps, rd in pays]
    cell["paid"] = True

# ---------- 5. Total return: полный пересчёт из цен и дивидендов ----------
for tk in d["order"]:
    s = d["meta"][tk]["series"]
    tr0 = s["tr_index"]
    start, y = tr0["x"][0], tr0["y"][0]
    cx, cy = s["close"]["x"], s["close"]["y"]
    ci = cx.index(start)
    divs = d["divs"][tk]
    xs, ys = [start], [y]
    for j in range(ci + 1, len(cx)):
        y = y * (cy[j] + divs.get(cx[j], 0.0)) / cy[j - 1]
        xs.append(cx[j]); ys.append(round(y, 6))
    tr0["x"], tr0["y"] = xs, ys
    peak = 0; ddx, ddy = [], []
    for x, v in zip(xs, ys):
        peak = max(peak, v); ddx.append(x); ddy.append(round((v / peak - 1) * 100, 4))
    s["dd_tr"] = {"x": ddx, "y": ddy}
    if s.get("tr_1y") is not None:   # 1Y TR = tr[i] / tr[i-253] − 1 (как в исходных данных)
        s["tr_1y"] = {"x": xs[253:], "y": [round((ys[i] / ys[i - 253] - 1) * 100, 4) for i in range(253, len(xs))]}
    # KPI
    L = d["last"][tk]; today = xs[-1]
    def base(days):
        tgt = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
        k = bisect.bisect_right(xs, tgt) - 1
        return ys[k] if k >= 0 else None
    for key, days in (("r1y_tr", 365), ("r3m_tr", 90)):
        b = base(days)
        L[key] = round((ys[-1] / b - 1) * 100, 1) if b else None

import sys; sys.path.insert(0, str(ROOT / "scripts"))
import update_quotes as uq
uq.recompute_forward_table(d)
uq.recompute_div_ltm(d)
d["table"]["note"] = ("P/E и P/B — форвардные к текущей цене. Див. доходность: за 2026 год — по дивидендам с отсечкой в 2026 году: "
    "выплаченные (●) — к цене последнего дня покупки под дивиденд, объявленные (○) — к текущей цене; "
    "за 2027–2028 — прогнозный DPS к текущей цене.")
json.dump(d, open(P, "w"), ensure_ascii=False, separators=(",", ":"))
print(json.dumps(audit, ensure_ascii=False, indent=1))
