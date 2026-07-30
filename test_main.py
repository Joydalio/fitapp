import os
from datetime import datetime, timedelta, timezone

import pytest

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_swim.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["SWIM_DB"] = DB_PATH
os.environ["SWIM_TOKEN"] = "testtoken"

import main  # noqa: E402  (환경변수 설정 후에 import 해야 함)
from fastapi import HTTPException  # noqa: E402

# TestClient는 httpx가 필요해서 라우트 함수를 직접 호출한다.


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture(autouse=True)
def clean_db():
    with main.conn() as c:
        c.execute("DELETE FROM workouts")


def test_upsert_is_idempotent():
    payload = [
        {"start": ago(2), "duration_sec": 2400, "distance_m": 1500, "type": "Swimming"},
        {"start": ago(4), "duration_sec": 1800, "distance_m": 1000, "type": "Swimming"},
    ]
    assert main.post_workouts(payload, "testtoken") == {"received": 2, "inserted": 2, "updated": 0, "skipped": 0}
    assert main.post_workouts(payload, "testtoken") == {"received": 2, "inserted": 0, "updated": 2, "skipped": 0}
    with main.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 2


def test_pace_excludes_zero_distance():
    main.post_workouts([
        {"start": ago(3), "duration_sec": 2400, "distance_m": 1500},
        {"start": ago(1), "duration_sec": 1200, "distance_m": 0},
    ], "testtoken")

    d = main.stats(days=365)
    paces = [s["pace_sec"] for s in d["sessions"]]
    assert 160.0 in paces                       # 2400 / 1500 * 100
    assert None in paces                        # 거리 0은 페이스 계산에서 제외
    assert d["summary"]["sessions"] == 2
    assert d["summary"]["avg_pace_sec"] == 160.0
    assert d["summary"]["best_pace_sec"] == 160.0


def test_bad_token_is_401():
    with pytest.raises(HTTPException) as e:
        main.post_workouts([{"start": ago(1), "distance_m": 1000}], "wrong-token")
    assert e.value.status_code == 401
    with main.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 0
