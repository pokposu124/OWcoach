import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from owcoach_service.app import Settings, create_app  # noqa: E402
from owcoach_service.db import Store  # noqa: E402
from owcoach_service.limits import usage  # noqa: E402

SERVICE = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((SERVICE / "config.yaml").read_text("utf-8"))
ME, FRIEND, STRANGER = "111", "222", "999"


def _video(path: Path, size: str, seconds: int = 4) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30", "-t", str(seconds),
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture
def settings(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({**CFG, "provider": "mock"}, allow_unicode=True), "utf-8")
    return Settings({"OWCOACH_DATA_DIR": str(tmp_path / "data"), "OWCOACH_SESSION_SECRET": "x" * 40,
                     "OWCOACH_ALLOWED_DISCORD_IDS": f"{ME},{FRIEND}", "DISCORD_CLIENT_ID": "cid",
                     "DISCORD_CLIENT_SECRET": "secret"}, cfg)


async def _fake_discord(code: str) -> dict:
    return {"id": code, "name": f"user{code}"}


@pytest.fixture
def client(settings):
    app = create_app(settings, start_worker=False, discord_user=_fake_discord)
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def login(c: TestClient, uid: str):
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://discord.com/oauth2/authorize?")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == ["identify"] and q["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    return c.get(f"/auth/callback?code={uid}&state={q['state'][0]}", follow_redirects=False)


def test_settings_refuse_more_than_three_users(tmp_path):
    with pytest.raises(SystemExit):
        Settings({"OWCOACH_DATA_DIR": str(tmp_path), "OWCOACH_SESSION_SECRET": "x" * 40,
                  "OWCOACH_ALLOWED_DISCORD_IDS": "1,2,3,4"})
    with pytest.raises(SystemExit):
        Settings({"OWCOACH_DATA_DIR": str(tmp_path), "OWCOACH_SESSION_SECRET": "short", "OWCOACH_ALLOWED_DISCORD_IDS": "1"})


def test_login_only_for_invited_accounts(client):
    assert client.get("/jobs", follow_redirects=False).headers["location"] == "/"
    r = login(client, STRANGER)
    assert r.status_code == 403 and "초대되지 않았습니다" in r.text
    assert client.get("/jobs", follow_redirects=False).status_code == 302
    r = login(client, ME)
    assert r.status_code == 302 and r.headers["location"] == "/jobs"
    page = client.get("/jobs")
    assert "user111" in page.text and "이번 주 남은 요청 <b>2/2회</b>" in page.text
    # a callback with a wrong state is refused
    client.get("/login", follow_redirects=False)
    assert client.get("/auth/callback?code=111&state=forged").status_code == 400


def test_upload_queues_a_job_and_counts_toward_the_week(client, tmp_path):
    login(client, ME)
    v = _video(tmp_path / "a.mp4", "1920x1080")
    r = client.put("/api/upload?name=a.mp4&patch=s19", content=v.read_bytes())
    assert r.status_code == 200, r.text
    job = client.app_ref.state.store.job(r.json()["job"])
    assert job["status"] == "queued" and job["patch"] == "s19" and Path(job["video_path"]).exists()
    # the same file again: the earlier analysis, no new request used
    again = client.put("/api/upload?name=a.mp4", content=v.read_bytes()).json()
    assert again == {"job": job["id"], "existing": True}
    assert "1/2회" in client.get("/jobs").text
    w = _video(tmp_path / "b.mp4", "1280x720")
    assert client.put("/api/upload?name=b.mp4", content=w.read_bytes()).status_code == 200
    x = _video(tmp_path / "c.mp4", "1280x720", seconds=3)
    r = client.put("/api/upload?name=c.mp4", content=x.read_bytes())
    assert r.status_code == 429 and "이번 주 분석 2회를 모두 썼습니다" in r.json()["error"]


def test_hero_is_recognised_not_chosen(client, tmp_path):
    """No hero picker: a job starts as auto and the list says the hero is not recognised yet; an explicit
    hero (operator use) must be one the analyzer knows."""
    login(client, ME)
    page = client.get("/jobs").text
    assert "<select" not in page and "자동 인식" in page
    v = _video(tmp_path / "t.mp4", "1280x720")
    assert client.put("/api/upload?name=t.mp4&hero=nobody", content=v.read_bytes()).status_code == 400
    jid = client.put("/api/upload?name=t.mp4", content=v.read_bytes()).json()["job"]
    assert client.app_ref.state.store.job(jid)["hero"] == "auto"
    assert "t.mp4 <span class=muted>· 영웅 인식 전</span>" in client.get("/jobs").text


def test_upload_rejects_unsupported_video_without_using_a_request(client, tmp_path):
    login(client, ME)
    low = _video(tmp_path / "low.mp4", "640x360")
    r = client.put("/api/upload?name=low.mp4", content=low.read_bytes())
    assert r.status_code == 400 and "해상도" in r.json()["error"]
    wide = _video(tmp_path / "wide.mp4", "1920x822")
    assert "화면 비율" in client.put("/api/upload?name=wide.mp4", content=wide.read_bytes()).json()["error"]
    assert client.put("/api/upload?name=x.avi", content=b"123").status_code == 400
    assert "2/2회" in client.get("/jobs").text
    assert not list((client.app_ref.state.settings.data_dir / "uploads").iterdir())


def test_results_are_private_to_their_owner(client, tmp_path):
    store = client.app_ref.state.store
    store.upsert_user(FRIEND, "friend")
    jid = store.create_job(FRIEND, "f.mp4", "", "", "sha", status="done")
    out = client.app_ref.state.settings.data_dir / "results" / jid
    out.mkdir(parents=True)
    (out / "index.html").write_text("<p>friend result</p>")
    login(client, ME)
    assert client.get(f"/results/{jid}/").status_code == 404
    assert client.get(f"/api/jobs/{jid}").status_code == 404
    c2 = TestClient(client.app_ref)
    login(c2, FRIEND)
    assert "friend result" in c2.get(f"/results/{jid}/").text
    assert c2.get(f"/results/{jid}/../../owcoach.db").status_code == 404


def test_admins_see_every_analysis_and_result(settings, tmp_path):
    settings.admins = [ME]
    app = create_app(settings, start_worker=False, discord_user=_fake_discord)
    with TestClient(app) as c:
        store = app.state.store
        store.upsert_user(FRIEND, "friend")
        jid = store.create_job(FRIEND, "f.mp4", "", "", "sha", status="done")
        out = settings.data_dir / "results" / jid
        out.mkdir(parents=True)
        (out / "index.html").write_text("<p>friend result</p>")
        login(c, ME)
        page = c.get("/jobs").text
        assert "전체 분석" in page and "<b>friend</b> · f.mp4" in page
        assert "friend result" in c.get(f"/results/{jid}/").text and c.get(f"/jobs/{jid}").status_code == 200
        c2 = TestClient(app)
        login(c2, FRIEND)
        assert "전체 분석" not in c2.get("/jobs").text   # everyone else still sees only their own
    with pytest.raises(SystemExit):
        Settings({"OWCOACH_DATA_DIR": str(tmp_path), "OWCOACH_SESSION_SECRET": "x" * 40,
                  "OWCOACH_ALLOWED_DISCORD_IDS": "1", "OWCOACH_ADMIN_DISCORD_IDS": "2"})


def test_monthly_budget_holds_reserves_for_unfinished_jobs(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.upsert_user(ME, "me")
    store.upsert_user(FRIEND, "friend")
    for i in range(30):     # $1.6 x 30 = $48 spent this month by a friend (not this user's weekly count)
        jid = store.create_job(FRIEND, f"{i}.mp4", "", "", f"s{i}", status="done")
        store.update_job(jid, cost_usd=1.6)
    u = usage(store, ME, CFG)
    assert u.month_spent == pytest.approx(48.0) and u.refusal() is None
    store.create_job(FRIEND, "q.mp4", "", "", "q")             # queued: holds the $1.5 reserve -> 49.5
    u = usage(store, ME, CFG)
    assert u.month_spent == pytest.approx(49.5) and "예산" in u.refusal()
    store.create_job(ME, "r.mp4", "", "", "r", status="rejected")   # rejected input costs nothing
    assert usage(store, ME, CFG).month_spent == pytest.approx(49.5)


def test_week_limit_window_is_seven_days(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.upsert_user(ME, "me")
    for _ in range(2):
        store.create_job(ME, "a.mp4", "", "", "a", status="done")
    now = datetime.now(timezone.utc)
    u = usage(store, ME, CFG, now)
    assert u.week_left == 0 and "이번 주 분석 2회를 모두 썼습니다" in u.refusal()
    assert usage(store, ME, CFG, now + timedelta(days=7, minutes=1)).week_left == 2


def test_worker_runs_the_analyzer_and_exports_the_result(client, tmp_path):
    """End to end with the mock provider: queued -> running -> done, the result page is served, the
    uploaded original is deleted (the page keeps its own small copy)."""
    login(client, ME)
    v = _video(tmp_path / "e2e.mp4", "1280x720", seconds=20)
    jid = client.put("/api/upload?name=e2e.mp4&hero=ana", content=v.read_bytes()).json()["job"]
    app = client.app_ref
    app.state.worker.run_job(app.state.store.job(jid))
    job = app.state.store.job(jid)
    assert job["status"] == "done", job["error"]
    assert job["stage"] == "완료" and not Path(job["video_path"]).exists()
    page = client.get(f"/results/{jid}/")
    assert page.status_code == 200 and "플레이 분석" in page.text
    head = client.get(f"/results/{jid}/media/web.mp4", headers={"Range": "bytes=0-99"})
    assert head.status_code == 206 and len(head.content) == 100
    assert "결과 보기" in client.get("/jobs").text
    # the list shows the map and the most played heroes the analyzer reported
    assert job["map_name"] == "[모의] 맵" and json.loads(job["main_heroes"])[0]["hero"] == "아나"
    assert "[모의] 맵 · 아나" in client.get("/jobs").text
