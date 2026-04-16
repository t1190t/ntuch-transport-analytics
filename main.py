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


def verify(cred: HTTPAuthorizationCredentials = Depends(security)):
    if not ANALYTICS_PASSWORD or cred.credentials != ANALYTICS_PASSWORD:
        raise HTTPException(status_code=401, detail="密碼錯誤")


def cv(row, cm, name):
    i = cm.get(name)
    if i is None or i >= len(row):
        return ""
    v = str(row[i]).strip()
    return "" if v in ("", "nan", "None", "NaN") else v


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
        rows = sh.worksheet("外接出勤").get_all_values()
        headers = rows[0]

        # 建立欄位索引（同名取第一個）
        cm = {}
        for i, h in enumerate(headers):
            hs = h.strip()
            if hs and hs not in cm:
                cm[hs] = i

        # Central line 欄位可能重複，取所有索引
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

            cl = []
            for i in cl_idx[:4]:
                v = str(row[i]).strip() if i < len(row) else ""
                cl.append("" if v in ("", "nan", "None", "NaN") else v)

            records.append(
                {
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
                    "cl":        cl,
                }
            )
    except Exception as e:
        raise HTTPException(500, f"外接出勤讀取失敗: {e}")

    # ── 轉出（只計趟次）──
    transfer = 0
    try:
        r2 = sh.worksheet("轉出").get_all_values()
        transfer = max(0, len(r2) - 1)
    except Exception:
        pass

    dates = [r["date"] for r in records if r["date"]]
    years = sorted(set(r["year"] for r in records if r["year"]))

    result = {
        "stats": {
            "outbound":  len(records),
            "transfer":  transfer,
            "total":     len(records) + transfer,
            "last_date": max(dates) if dates else "",
            "years":     years,
        },
        "records": records,
    }
    _cache.update({"data": result, "ts": time.time()})
    return result
