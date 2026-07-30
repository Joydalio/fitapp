"""수영 기록 서버 - iOS 단축어가 보낸 애플 건강앱 수영 기록을 저장하고 대시보드용 통계를 낸다."""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, field_validator

# 한국은 서머타임이 없어 고정 +9. zoneinfo를 쓰면 윈도우에서 tzdata 패키지가 추가로 필요하다.
KST = timezone(timedelta(hours=9))
DB = os.environ.get("SWIM_DB", "swim.db")
TOKEN = os.environ.get("SWIM_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "static", "index.html")
SHORTCUT = os.path.join(HERE, "swim_shortcut.shortcut")
FIELDS = ["start", "end", "duration_sec", "distance_m", "energy_kcal", "type", "avg_hr", "location"]

app = FastAPI(title="수영 기록")


# ---------- DB ----------

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS workouts (
            start        TEXT PRIMARY KEY,
            "end"        TEXT,
            duration_sec REAL,
            distance_m   REAL,
            energy_kcal  REAL,
            type         TEXT,
            avg_hr       REAL,
            location     TEXT
        )""")


init_db()


# ---------- 스키마 ----------

class Workout(BaseModel):
    """iOS 버전마다 단축어가 주는 필드가 달라서 전부 optional."""
    start: Optional[str] = None
    end: Optional[str] = None
    duration_sec: Optional[float] = None
    distance_m: Optional[float] = None
    energy_kcal: Optional[float] = None
    type: Optional[str] = None
    avg_hr: Optional[float] = None
    location: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def blank_to_none(cls, v):
        # 단축어는 값이 없을 때 "" 를 보낸다. 그대로 두면 422로 그날 전송 전체가 실패함.
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("duration_sec", "distance_m", "energy_kcal", "avg_hr", mode="before")
    @classmethod
    def strip_units(cls, v, info):
        # 단축어가 "1,500 m" / "420 kcal" 처럼 단위째 보내는 경우가 있다. 숫자만 뽑는다.
        if not isinstance(v, str):
            return v
        m = re.search(r"-?\d+(\.\d+)?", v.replace(",", ""))
        if not m:
            return None
        n = float(m.group())
        if info.field_name == "distance_m":
            # 건강앱 단위 설정에 따라 "1.5 km" 로 올 수 있다. 미터로 안 맞추면 1000배 틀린다.
            for unit, mul in (("km", 1000), ("mi", 1609.34), ("yd", 0.9144), ("ft", 0.3048)):
                if unit in v.lower():
                    return n * mul
        return n


def norm_start(s):
    """모든 시각을 UTC ISO 문자열로 정규화. +09:00 과 Z 표기가 섞여도 같은 키가 되게."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc).isoformat()


# ---------- 수집 ----------

@app.post("/api/workouts")
def post_workouts(items: list[dict], x_token: str = Header(default="")):
    if not TOKEN or x_token != TOKEN:
        raise HTTPException(401, "invalid token")

    rows = {}
    for raw in items:
        try:
            w = Workout(**raw)
        except Exception:
            continue  # 한 건이 이상해도 나머지 400일치는 저장되게 (전체 422 방지)
        start = norm_start(w.start)
        if not start:
            continue  # start가 UNIQUE 키. 없거나 ISO 8601이 아니면 그 기록은 버린다
        if w.type and not re.search(r"swim|수영", w.type, re.I):
            continue  # 단축어 필터가 안 걸려서 러닝·걷기까지 올라와도 수영만 남긴다
        d = w.model_dump()
        d["start"] = start
        end = norm_start(w.end)
        d["end"] = end
        if d["duration_sec"] is None and end:
            d["duration_sec"] = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        rows[start] = d  # 같은 payload 안의 중복도 여기서 정리

    rows = list(rows.values())
    if not rows:
        return {"received": len(items), "inserted": 0, "updated": 0, "skipped": len(items)}

    cols = ",".join(f'"{f}"' for f in FIELDS)
    ph = ",".join("?" for _ in FIELDS)
    upd = ",".join(f'"{f}"=excluded."{f}"' for f in FIELDS if f != "start")
    keys = [r["start"] for r in rows]
    with conn() as c:
        q = f'SELECT start FROM workouts WHERE start IN ({",".join("?" * len(keys))})'
        existing = {r[0] for r in c.execute(q, keys)}
        c.executemany(
            f'INSERT INTO workouts({cols}) VALUES({ph}) ON CONFLICT(start) DO UPDATE SET {upd}',
            [[r[f] for f in FIELDS] for r in rows],
        )
    inserted = sum(1 for k in keys if k not in existing)
    # skipped > 0 이면 단축어의 날짜 포맷이 ISO 8601이 아닐 가능성이 높다.
    return {"received": len(items), "inserted": inserted,
            "updated": len(rows) - inserted, "skipped": len(items) - len(rows)}


# ---------- 집계 ----------

def pace(duration_sec, distance_m):
    """100m 페이스(초). 거리가 없거나 0이면 계산에서 제외."""
    if not duration_sec or not distance_m:
        return None
    return round(duration_sec / distance_m * 100, 1)


def avg_pace(sessions):
    """거리 가중 평균 페이스. 페이스 없는 세션은 제외."""
    valid = [s for s in sessions if s["pace_sec"]]
    dist = sum(s["distance_m"] for s in valid)
    return round(sum(s["duration_sec"] for s in valid) / dist * 100, 1) if dist else None


def build_advice(sessions, days, imp):
    if not sessions:
        return [{"level": "info", "title": "기록이 없습니다",
                 "detail": "아이폰 단축어를 실행해 수영 기록을 보내주세요."}]

    out = []
    n = len(sessions)
    per_week = n / max(days / 7, 1)
    if per_week < 2:
        out.append({"level": "warn", "title": "수영 빈도를 늘려보세요",
                    "detail": f"주당 평균 {per_week:.1f}회 ({days}일간 {n}회). 주 2회 이상부터 페이스가 눈에 띄게 좋아집니다."})

    if imp["delta_sec"] is not None:
        if imp["delta_sec"] < 0:
            out.append({"level": "warn", "title": "최근 페이스가 느려졌습니다",
                        "detail": f"최근 90일 {fmt_pace(imp['recent_pace_sec'])} vs 이전 90일 {fmt_pace(imp['prev_pace_sec'])} "
                                  f"({abs(imp['delta_sec']):.1f}초 느려짐). 수면·피로·폼 중 뭐가 달라졌는지 점검해보세요."})
        elif imp["pct"] < 2:
            out.append({"level": "info", "title": "페이스가 정체 상태입니다",
                        "detail": f"90일 개선폭 {imp['pct']:.1f}% ({imp['delta_sec']:.1f}초). "
                                  f"같은 거리를 계속 도는 대신 100m x 8회 인터벌을 섞어보세요."})

    d30 = sum(s["distance_m"] for s in sessions_within(sessions, 30))
    d60 = sum(s["distance_m"] for s in sessions_within(sessions, 60)) - d30
    if d60 > 0 and d30 > d60 * 1.5:
        out.append({"level": "warn", "title": "훈련량이 너무 빨리 늘었습니다",
                    "detail": f"최근 30일 {d30:,.0f}m vs 이전 30일 {d60:,.0f}m (+{(d30 / d60 - 1) * 100:.0f}%). "
                              f"어깨 부상 위험 구간이라 주당 증가폭을 10% 안쪽으로 줄이세요."})

    per_session = sum(s["distance_m"] for s in sessions) / n
    if per_session < 1000:
        out.append({"level": "info", "title": "세션당 볼륨이 짧습니다",
                    "detail": f"세션당 평균 {per_session:,.0f}m. 지구력을 올리려면 한 번에 1,000~1,500m를 목표로 해보세요."})

    return out


def fmt_pace(sec):
    return "-" if not sec else f"{int(sec // 60)}:{sec % 60:04.1f}"


def sessions_within(sessions, days, offset=0):
    """지금부터 offset일 전 ~ (offset+days)일 전 구간의 세션."""
    now = datetime.now(timezone.utc)
    hi = (now - timedelta(days=offset)).isoformat()
    lo = (now - timedelta(days=offset + days)).isoformat()
    return [s for s in sessions if lo <= s["start"] < hi]


@app.get("/api/stats")
def stats(days: int = 365):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with conn() as c:
        rows = c.execute("SELECT * FROM workouts WHERE start >= ? ORDER BY start", (cutoff,)).fetchall()

    sessions = []
    for r in rows:
        local = datetime.fromisoformat(r["start"]).astimezone(KST)
        sessions.append({
            "start": r["start"],
            "date": local.strftime("%Y-%m-%d"),
            "month": local.strftime("%Y-%m"),
            "distance_m": r["distance_m"] or 0,
            "duration_sec": r["duration_sec"] or 0,
            "energy_kcal": r["energy_kcal"],
            "avg_hr": r["avg_hr"],
            "pace_sec": pace(r["duration_sec"], r["distance_m"]),
        })

    # 4주 이동평균. ponytail: 세션 수가 400 이하라 O(n^2)로 충분, 느려지면 슬라이딩 윈도우로.
    dts = [datetime.fromisoformat(s["start"]) for s in sessions]
    for i, s in enumerate(sessions):
        win = [sessions[j]["pace_sec"] for j in range(i + 1)
               if sessions[j]["pace_sec"] and dts[j] >= dts[i] - timedelta(days=28)]
        s["ma4w_sec"] = round(sum(win) / len(win), 1) if win else None

    monthly = {}
    for s in sessions:
        m = monthly.setdefault(s["month"], [])
        m.append(s)
    monthly = [{
        "month": k,
        "distance_m": sum(s["distance_m"] for s in v),
        "sessions": len(v),
        "duration_sec": sum(s["duration_sec"] for s in v),
        "avg_pace_sec": avg_pace(v),
    } for k, v in sorted(monthly.items())]

    paces = [s["pace_sec"] for s in sessions if s["pace_sec"]]
    recent, prev = sessions_within(sessions, 90), sessions_within(sessions, 90, offset=90)
    rp, pp = avg_pace(recent), avg_pace(prev)
    imp = {
        "recent_pace_sec": rp,
        "prev_pace_sec": pp,
        "delta_sec": round(pp - rp, 1) if rp and pp else None,  # 양수 = 빨라짐
        "pct": round((pp - rp) / pp * 100, 1) if rp and pp else None,
    }

    return {
        "days": days,
        "summary": {
            "total_distance_m": sum(s["distance_m"] for s in sessions),
            "sessions": len(sessions),
            "avg_pace_sec": avg_pace(sessions),
            "best_pace_sec": min(paces) if paces else None,
        },
        "improvement": imp,
        "monthly": monthly,
        "sessions": sessions,
        "advice": build_advice(sessions, days, imp),
    }


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/shortcut")
def shortcut(request: Request):
    """아이폰에서 이 주소를 열면 단축어 파일을 받는다.

    다운로드에 쓴 주소를 그대로 단축어에 박아넣기 때문에, 터널 주소가 바뀌어도
    다시 받기만 하면 맞는 주소가 들어간다.
    """
    with open(SHORTCUT, encoding="utf-8") as f:
        xml = f.read()
    xml = xml.replace("__BASE_URL__", str(request.base_url).rstrip("/")).replace("__TOKEN__", TOKEN)
    return Response(xml, media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="swim.shortcut"'})
