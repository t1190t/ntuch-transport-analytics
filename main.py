import os, json, time
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import gspread
from google.oauth2.service_account import Credentials

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SHEET_ID           = "1oG1ydRWD7eELqB2myuoECuQFTffkCGqirwROLe3SXcE"
GOOGLE_SA_JSON     = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
ANALYTICS_PASSWORD = os.environ.get("ANALYTICS_PASSWORD", "")
CACHE_SEC          = 1800
_cache             = {"data": None, "ts": 0}
security           = HTTPBearer()
EMPTY              = ("", "nan", "None", "NaN", "沒打", "N/A", "n/a")


def verify(cred: HTTPAuthorizationCredentials = Depends(security)):
    if not ANALYTICS_PASSWORD or cred.credentials != ANALYTICS_PASSWORD:
        raise HTTPException(status_code=401, detail="密碼錯誤")


def cv(row, cm, name):
    i = cm.get(name)
    if i is None or i >= len(row):
        return ""
    v = str(row[i]).strip()
    return "" if v in EMPTY else v


def cv_float(row, cm, name):
    v = cv(row, cm, name)
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


def date_fmt(s):
    if not s:
        return ""
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep)[0]
    if "/" in s:
        p = s.split("/")
        if len(p) == 3:
            return f"{p[0]}-{p[1].zfill(2)}-{p[2].zfill(2)}"
    return s


def parse_cl_types(raw_cells):
    types = []
    seen = set()
    for cell in raw_cells:
        if not cell or cell in EMPTY or cell == "無":
            continue
        for part in cell.split(","):
            t = part.strip()
            if t and t != "無" and t not in seen:
                types.append(t)
                seen.add(t)
    return types


def find_extreme(records, key, maximize=True):
    valid = [(r[key], r["date"], r["hospital"]) for r in records if r.get(key) is not None]
    if not valid:
        return {"value": None, "date": "", "hospital": ""}
    best = max(valid, key=lambda x: x[0]) if maximize else min(valid, key=lambda x: x[0])
    return {"value": best[0], "date": best[1], "hospital": best[2]}


@app.get("/")
def root():
    return {"status": "NTUCH Analytics API ✅"}


@app.head("/")
def head():
    return Response(status_code=200)


@app.get("/api/analytics-data")
async def analytics(_=Depends(verify)):
    if _cache["data"] and time.time() - _cache["ts"] < CACHE_SEC:
        return _cache["data"]

    if not GOOGLE_SA_JSON:
        raise HTTPException(500, "GOOGLE_SERVICE_ACCOUNT_JSON 未設定")

    try:
        sh = gspread.authorize(
            Credentials.from_service_account_info(
                json.loads(GOOGLE_SA_JSON),
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )
        ).open_by_key(SHEET_ID)
    except Exception as e:
        raise HTTPException(500, f"Sheets 連線失敗: {e}")

    # ── 外接出勤 ──
    records = []
    try:
        rows    = sh.worksheet("外接出勤").get_all_values()
        headers = rows[0]

        cm = {}
        for i, h in enumerate(headers):
            hs = h.strip()
            if hs and hs not in cm:
                cm[hs] = i

        cl_idx = [i for i, h in enumerate(headers) if h.strip() == "是否有以下central line"]

        for row in rows[1:]:
            if not any(row):
                continue

            yr = cv(row, cm, "年份")
            if yr:
                try:
                    yr = str(int(float(yr)))
                except Exception:
                    pass

            raw_cl = []
            for i in cl_idx[:4]:
                v = str(row[i]).strip() if i < len(row) else ""
                raw_cl.append("" if v in EMPTY else v)

            tiss = cv_float(row, cm, "TISS")
            if tiss is None:
                tiss = cv_float(row, cm, "TISS分數")

            ntiss = cv_float(row, cm, "NTISS")
            if ntiss is None:
                ntiss = cv_float(row, cm, "NTISS分數")

            records.append({
                "year":      yr,
                "date":      date_fmt(cv(row, cm, "出勤日期")),
                "hospital":  cv(row, cm, "轉出院所名稱"),
                "inst_type": cv(row, cm, "醫療機構分類"),
                "county":    cv(row, cm, "出勤縣市"),
                "unit":      cv(row, cm, "轉出單位"),
                "nurse":     cv(row, cm, "轉診成員-護理師姓名"),
                "doctor":    cv(row, cm, "轉診成員-醫師姓名"),
                "specialty": cv(row, cm, "病人疾病科別"),
                "adm_unit":  cv(row, cm, "入住單位"),
                "airway":    cv(row, cm, "呼吸"),
                "aline":     cv(row, cm, "動脈導管(A-line)"),
                "cl_types":  parse_cl_types(raw_cl),
                "gender":    cv(row, cm, "病人性別"),
                "cv_drug":   cv(row, cm, "心血管用藥種類"),
                "ga":        cv_float(row, cm, "GA(週)"),
                "weight":    cv_float(row, cm, "病人現在體重"),
                "tiss":      tiss,
                "ntiss":     ntiss,
            })
    except Exception as e:
        raise HTTPException(500, f"外接出勤讀取失敗: {e}")

    # ── 轉出（讀取每筆日期）──
    transfers = []
    try:
        t_rows    = sh.worksheet("轉出").get_all_values()
        t_headers = t_rows[0]
        tcm = {}
        for i, h in enumerate(t_headers):
            hs = h.strip()
            if hs and hs not in tcm:
                tcm[hs] = i

        for row in t_rows[1:]:
            if not any(row):
                continue
            transfers.append({
                "date": date_fmt(cv(row, tcm, "出勤日期")),
            })
    except Exception as e:
        raise HTTPException(500, f"轉出讀取失敗: {e}")

    # ── 全歷史統計 ──
    dates      = [r["date"] for r in records if r["date"]]
    years      = sorted(set(r["year"] for r in records if r["year"]))
    tiss_vals  = [r["tiss"]  for r in records if r["tiss"]  is not None]
    ntiss_vals = [r["ntiss"] for r in records if r["ntiss"] is not None]

    result = {
        "stats": {
            "outbound":        len(records),
            "transfer":        len(transfers),
            "total":           len(records) + len(transfers),
            "last_date":       max(dates) if dates else "",
            "years":           years,
            "all_hospitals":   len(set(r["hospital"] for r in records if r["hospital"])),
            "all_counties":    len(set(r["county"]   for r in records if r["county"])),
            "avg_tiss":        round(sum(tiss_vals)  / len(tiss_vals),  1) if tiss_vals  else None,
            "avg_ntiss":       round(sum(ntiss_vals) / len(ntiss_vals), 1) if ntiss_vals else None,
            "tiss_count":      len(tiss_vals),
            "ntiss_count":     len(ntiss_vals),
            "max_weight":      find_extreme(records, "weight", maximize=True),
            "min_weight":      find_extreme(records, "weight", maximize=False),
            "max_tiss":        find_extreme(records, "tiss",   maximize=True),
            "max_ntiss":       find_extreme(records, "ntiss",  maximize=True),
        },
        "records":   records,
        "transfers": transfers,
    }
    _cache.update({"data": result, "ts": time.time()})
    return result
