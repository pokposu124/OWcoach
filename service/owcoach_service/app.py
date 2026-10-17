"""OWcoach friends beta: Discord login, video upload, analysis queue, private result pages.

Run (see service/README.md for the environment variables):
    uvicorn owcoach_service.app:create_app --factory --host 0.0.0.0 --port 8000

Pages: / (intro + login) · /jobs (my analyses, limits, upload) · /jobs/{id} (progress / result link)
       /results/{id}/ (the analyzer's result page, owner only).
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .db import Store
from .limits import usage
from .worker import ANALYZER_DIR, Worker

sys.path.insert(0, str(ANALYZER_DIR))
from owcoach.config import load_config as load_analyzer_config  # noqa: E402
from owcoach.media import InputRejected, probe, validate_input  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parents[1]
DOCS_DIR = SERVICE_DIR.parent / "docs"
DISCORD_API = "https://discord.com/api"
_e = html.escape


class Settings:
    """Secrets and deployment values from the environment; limits from service/config.yaml."""

    def __init__(self, env: dict | None = None, config_path: Path | None = None):
        env = os.environ if env is None else env
        self.cfg = yaml.safe_load((config_path or SERVICE_DIR / "config.yaml").read_text("utf-8"))
        self.data_dir = Path(env.get("OWCOACH_DATA_DIR", SERVICE_DIR / "data"))
        self.base_url = env.get("OWCOACH_BASE_URL", "http://localhost:8000").rstrip("/")
        self.session_secret = env.get("OWCOACH_SESSION_SECRET", "")
        self.discord_client_id = env.get("DISCORD_CLIENT_ID", "")
        self.discord_client_secret = env.get("DISCORD_CLIENT_SECRET", "")
        self.allowed = [x.strip() for x in env.get("OWCOACH_ALLOWED_DISCORD_IDS", "").split(",") if x.strip()]
        if len(self.allowed) > int(self.cfg["max_users"]):
            raise SystemExit(f"OWCOACH_ALLOWED_DISCORD_IDS 에 {len(self.allowed)}명이 있습니다. 베타 최대 {self.cfg['max_users']}명.")
        if len(self.session_secret) < 32:
            raise SystemExit("OWCOACH_SESSION_SECRET 이 없거나 짧습니다 (32자 이상).")

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/auth/callback"


def create_app(settings: Settings | None = None, start_worker: bool = True,
               discord_user=None) -> FastAPI:
    """discord_user: async (code) -> {"id", "name"}; replaced in tests so no request goes to Discord."""
    s = settings or Settings()
    store = Store(s.data_dir / "owcoach.db")
    worker = Worker(store, s.cfg, s.data_dir)
    acfg = load_analyzer_config()["input"]
    uploads = s.data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.session_secret, session_cookie="owcoach",
                       max_age=30 * 24 * 3600, same_site="lax", https_only=s.base_url.startswith("https://"))
    app.state.store, app.state.worker, app.state.settings = store, worker, s

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/", status_code=302)
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail != "Not Found" else "페이지를 찾을 수 없습니다."
        return page("찾을 수 없음" if exc.status_code == 404 else "오류", f"<p>{_e(detail)}</p><p><a href=/jobs>← 내 분석</a></p>",
                    current_user(request), exc.status_code)

    async def discord_login(code: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            tok = await client.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": s.discord_client_id, "client_secret": s.discord_client_secret,
                "grant_type": "authorization_code", "code": code, "redirect_uri": s.redirect_uri})
            tok.raise_for_status()
            me = await client.get(f"{DISCORD_API}/users/@me",
                                  headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
            me.raise_for_status()
            u = me.json()
        return {"id": str(u["id"]), "name": u.get("global_name") or u.get("username") or str(u["id"])}

    get_discord_user = discord_user or discord_login

    def current_user(request: Request) -> dict | None:
        uid = request.session.get("uid")
        if not uid or uid not in s.allowed:      # removed from the list = logged out
            return None
        return store.user(uid)

    def need_user(request: Request) -> dict:
        u = current_user(request)
        if not u:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
        return u

    def own_job(request: Request, job_id: str) -> dict:
        u = need_user(request)
        job = store.job(job_id)
        if not job or job["user_id"] != u["discord_id"]:
            raise HTTPException(status_code=404, detail="분석을 찾을 수 없습니다.")
        return job

    # auth ----------------------------------------------------------------------
    @app.get("/login")
    async def login(request: Request):
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        q = urlencode({"client_id": s.discord_client_id, "response_type": "code", "redirect_uri": s.redirect_uri,
                       "scope": "identify", "state": state, "prompt": "none"})
        return RedirectResponse(f"https://discord.com/oauth2/authorize?{q}", status_code=302)

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        expected = request.session.pop("oauth_state", None)
        if not code or not expected or not secrets.compare_digest(state, expected):
            return page("로그인 실패", "<p>로그인 요청이 만료되었거나 올바르지 않습니다. <a href=/login>다시 로그인</a></p>", None, 400)
        try:
            u = await get_discord_user(code)
        except httpx.HTTPError:
            return page("로그인 실패", "<p>디스코드에서 계정 정보를 받지 못했습니다. <a href=/login>다시 시도</a></p>", None, 502)
        if u["id"] not in s.allowed:
            return page("초대되지 않은 계정", f"<p>{_e(u['name'])} 계정은 이 베타에 초대되지 않았습니다. "
                        "운영자에게 디스코드 사용자 ID를 알려 주세요.</p>", None, 403)
        store.upsert_user(u["id"], u["name"])
        request.session["uid"] = u["id"]
        return RedirectResponse("/jobs", status_code=302)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # pages ---------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        u = current_user(request)
        if u:
            return RedirectResponse("/jobs", status_code=302)
        demos = "".join(f'<li><a href="/demo/{d}/">{label}</a></li>' for d, label in
                        (("demo", "예시 1: 공격 맵 아나 (4분 47초)"), ("demo2", "예시 2: 킹스 로우 아나 (4분 52초)"))
                        if (DOCS_DIR / d / "index.html").exists())
        body = f"""
<p class=lead>오버워치 플레이 영상을 올리면 장면마다 스킬 사용·포지셔닝·궁극기 사용 피드백을 만들어 줍니다.</p>
<p><a class=btn href=/login>디스코드로 로그인</a></p>
<section class=box><h2>지금 지원하는 영상</h2><ul>
<li>영웅: 아나</li><li>해상도: 1280×720 ~ 1920×1080, 16:9 화면 비율, 기본 HUD 배치</li>
<li>길이: {acfg['max_duration_seconds'] // 60}분 이하 · 파일 {acfg['max_file_size_mb']}MB 이하</li>
<li>분석 시간: 영상 1개에 약 15~20분</li></ul></section>
{f'<section class=box><h2>결과 예시</h2><ul>{demos}</ul></section>' if demos else ''}
<p class=muted>초대된 디스코드 계정만 쓸 수 있는 비공개 베타입니다.</p>"""
        return page("OWcoach 베타", body, None)

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        u = current_user(request)
        if not u:
            return RedirectResponse("/", status_code=302)
        us = usage(store, u["discord_id"], s.cfg)
        refusal = us.refusal()
        rows = "".join(_job_row(j, s.cfg["timezone"]) for j in store.jobs_of(u["discord_id"])) \
            or "<tr><td colspan=4 class=muted>아직 분석한 영상이 없습니다.</td></tr>"
        upload = (f"<p class=warn>{_e(refusal)}</p>" if refusal else UPLOAD_FORM.format(
            max_mb=acfg["max_file_size_mb"], max_sec=acfg["max_duration_seconds"],
            min_w=acfg["min_width"], min_h=acfg["min_height"], max_w=acfg["max_width"], max_h=acfg["max_height"]))
        body = f"""
<section class=box><h2>새 분석</h2>
<p class=muted>이번 주 남은 요청 <b>{us.week_left}/{us.week_limit}회</b>
{f'· 다음 요청 가능: {us.next_slot_at.strftime("%m월 %d일 %H:%M")}' if us.week_left == 0 and us.next_slot_at else ''}</p>
{upload}</section>
<section class=box><h2>내 분석</h2><div class=scroll><table>
<tr><th>요청</th><th>영상</th><th>상태</th><th></th></tr>{rows}</table></div></section>"""
        return page("내 분석", body, u)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_page(request: Request, job_id: str):
        job = own_job(request, job_id)
        u = current_user(request)
        st = job["status"]
        if st == "done":
            body = f'<p><a class=btn href="/results/{job_id}/">결과 보기</a></p>'
        elif st in ("queued", "running"):
            ahead = sum(1 for j in store.jobs_since("") if j["status"] == "queued" and j["created_at"] < job["created_at"])
            where = (f"대기 중 · 앞에 {ahead}건" if st == "queued" else _e(job["stage"] or "분석 중"))
            body = (f"<p class=lead>{where}</p><p class=muted>영상 1개에 약 15~20분 걸립니다. 이 페이지는 30초마다 새로 고칩니다. "
                    "창을 닫아도 분석은 계속됩니다.</p>")
        else:
            body = (f"<p class=warn>{'입력 조건 불충족' if st == 'rejected' else '분석 실패'}</p>"
                    f"<pre>{_e(job['error'])}</pre><p class=muted>이 요청은 이번 주 횟수에서 빠집니다.</p>")
        body = (f"<p class=muted>{_e(job['file_name'])} · 요청 {_local(job['created_at'], s.cfg['timezone'])}</p>" + body
                + '<p><a href="/jobs">← 내 분석</a></p>')
        refresh = st in ("queued", "running")
        return page("분석 " + {"queued": "대기", "running": "진행", "done": "완료"}.get(st, "실패"), body, u, refresh=refresh)

    @app.get("/api/jobs/{job_id}")
    async def job_api(request: Request, job_id: str):
        job = own_job(request, job_id)
        return {k: job[k] for k in ("id", "status", "stage", "file_name", "created_at", "finished_at", "error")}

    @app.put("/api/upload")
    async def upload(request: Request, name: str = "video.mp4", patch: str = ""):
        u = need_user(request)
        refusal = usage(store, u["discord_id"], s.cfg).refusal()
        if refusal:
            return JSONResponse({"error": refusal}, status_code=429)
        suffix = Path(name).suffix.lower()
        if suffix not in acfg["allowed_extensions"]:
            return JSONResponse({"error": f"지원하지 않는 파일 형식입니다: {suffix or '(없음)'}"}, status_code=400)
        limit = acfg["max_file_size_mb"] * 1024 * 1024
        dst = uploads / f"{secrets.token_hex(8)}{suffix}"
        sha, size = hashlib.sha256(), 0
        try:
            with dst.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"파일 크기가 {acfg['max_file_size_mb']}MB를 넘습니다.")
                    sha.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise ValueError("빈 파일입니다.")
            info = probe(dst)
            reasons = validate_input(dst, info, acfg)
        except (ValueError, InputRejected) as e:
            dst.unlink(missing_ok=True)
            reasons = getattr(e, "reasons", None) or [str(e)]
            return JSONResponse({"error": "\n".join(reasons)}, status_code=400)
        if reasons:
            dst.unlink(missing_ok=True)
            jid = store.create_job(u["discord_id"], Path(name).name[:200], "", patch[:80], sha.hexdigest(),
                                   status="rejected", error="\n".join(reasons))
            return JSONResponse({"error": "\n".join(reasons), "job": jid}, status_code=400)
        same = store.done_job_for_video(u["discord_id"], sha.hexdigest())
        if same:                                 # same file again: no new run, no request used
            dst.unlink(missing_ok=True)
            return {"job": same["id"], "existing": True}
        jid = store.create_job(u["discord_id"], Path(name).name[:200], str(dst), patch[:80], sha.hexdigest())
        worker.notify()
        return {"job": jid}

    @app.get("/results/{job_id}/{path:path}")
    async def results(request: Request, job_id: str, path: str = ""):
        job = own_job(request, job_id)
        if job["status"] != "done":
            raise HTTPException(status_code=404, detail="아직 결과가 없습니다.")
        return _safe_file(s.data_dir / "results" / job_id, path or "index.html")

    @app.get("/demo/{name}/{path:path}")
    async def demo(name: str, path: str = ""):
        if name not in ("demo", "demo2"):
            raise HTTPException(status_code=404)
        return _safe_file(DOCS_DIR / name, path or "index.html")

    return app


def _safe_file(root: Path, rel: str) -> FileResponse:
    root = root.resolve()
    p = (root / rel).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(p)   # Range requests are honoured, so the result video can seek


def _local(iso: str, tz: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%m월 %d일 %H:%M")


STATUS_KO = {"queued": "대기", "running": "분석 중", "done": "완료", "failed": "실패", "rejected": "입력 조건 불충족"}


def _job_row(j: dict, tz: str) -> str:
    link = (f'<a href="/results/{j["id"]}/">결과 보기</a>' if j["status"] == "done" else f'<a href="/jobs/{j["id"]}">자세히</a>')
    stage = f' <span class=muted>{_e(j["stage"])}</span>' if j["status"] == "running" else ""
    return (f"<tr><td>{_local(j['created_at'], tz)}</td><td>{_e(j['file_name'])}</td>"
            f"<td><span class='st st-{j['status']}'>{STATUS_KO.get(j['status'], j['status'])}</span>{stage}</td><td>{link}</td></tr>")


STYLE = """
:root{--bg:#f3f4f7;--panel:#fff;--fg:#151a22;--muted:#5f6a78;--line:#dde1e8;--accent:#2457c5;--ok:#257a3e;--ok-bg:#e3f2e7;
--bad:#b4232a;--bad-bg:#fbe5e6;--warn:#8a5a00;--warn-bg:#fbefd6;--sans:"IBM Plex Sans KR",system-ui,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1218;--panel:#171c24;--fg:#e5e9f0;--muted:#97a2b0;--line:#2a313c;
--accent:#7ea6f2;--ok:#6fd08c;--ok-bg:#16301f;--bad:#f4878b;--bad-bg:#3a1a1c;--warn:#e9b860;--warn-bg:#352a12;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1218;--panel:#171c24;--fg:#e5e9f0;--muted:#97a2b0;--line:#2a313c;--accent:#7ea6f2;--ok:#6fd08c;--ok-bg:#16301f;
--bad:#f4878b;--bad-bg:#3a1a1c;--warn:#e9b860;--warn-bg:#352a12;color-scheme:dark}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 var(--sans)}
header.top{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header.top a.brand{font-weight:700;color:var(--fg);text-decoration:none} header.top .me{margin-left:auto;color:var(--muted);font-size:.9rem}
header.top form{margin:0} main{max-width:860px;margin:0 auto;padding:20px 16px;display:grid;gap:16px}
h1{font-size:1.3rem;margin:0} h2{font-size:1rem;margin:0 0 8px} a{color:var(--accent)}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.lead{font-size:1.05rem} .muted{color:var(--muted)} .warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;white-space:pre-line}
.btn,button{display:inline-block;font:inherit;font-weight:700;background:var(--accent);color:var(--panel);border:0;border-radius:8px;padding:8px 16px;cursor:pointer;text-decoration:none}
button.link{background:none;color:var(--muted);font-weight:400;padding:0}
button:disabled{opacity:.5;cursor:default} input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);width:100%}
.form{display:grid;gap:10px} progress{width:100%} pre{white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px}
.scroll{overflow-x:auto} table{border-collapse:collapse;width:100%;font-size:.9rem} th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td:nth-child(2){white-space:normal;overflow-wrap:anywhere}
.st{font-size:.8rem;font-weight:700;padding:1px 8px;border-radius:999px;background:var(--bg)} .st-done{background:var(--ok-bg);color:var(--ok)}
.st-failed,.st-rejected{background:var(--bad-bg);color:var(--bad)} .st-running{background:var(--warn-bg);color:var(--warn)}
"""

UPLOAD_FORM = """
<form class=form id=up>
  <label>영상 파일 (아나, {min_w}×{min_h} ~ {max_w}×{max_h} 16:9, {max_sec}초 이하, {max_mb}MB 이하)
    <input type=file id=file accept="video/mp4,video/quicktime,video/x-matroska,video/webm,.mp4,.mov,.mkv,.webm" required></label>
  <label>게임 패치 (선택, 기록용) <input type=text id=patch maxlength=80 placeholder="예: 시즌 19 중반"></label>
  <p class=muted id=check></p>
  <button id=go type=submit disabled>분석 요청</button>
  <progress id=bar max=100 value=0 hidden></progress>
  <p class=warn id=err hidden></p>
</form>
<script>
const f=document.getElementById('file'),go=document.getElementById('go'),chk=document.getElementById('check'),
  err=document.getElementById('err'),bar=document.getElementById('bar');
const LIM={{maxMb:{max_mb},maxSec:{max_sec},minW:{min_w},minH:{min_h},maxW:{max_w},maxH:{max_h}}};
function fail(m){{err.textContent=m;err.hidden=false;go.disabled=true;}}
f.addEventListener('change',()=>{{err.hidden=true;go.disabled=true;chk.textContent='';const file=f.files[0];if(!file)return;
  if(file.size>LIM.maxMb*1048576)return fail('파일이 '+LIM.maxMb+'MB보다 큽니다.');
  // check length and size in the browser first, so a video the analyzer cannot read is not uploaded
  const v=document.createElement('video');v.preload='metadata';v.src=URL.createObjectURL(file);
  v.onloadedmetadata=()=>{{const w=Math.max(v.videoWidth,v.videoHeight),h=Math.min(v.videoWidth,v.videoHeight),d=v.duration;URL.revokeObjectURL(v.src);
    if(d>LIM.maxSec)return fail('영상이 '+Math.round(d)+'초입니다. '+LIM.maxSec+'초 이하만 분석합니다.');
    if(w<LIM.minW||h<LIM.minH)return fail('해상도 '+v.videoWidth+'×'+v.videoHeight+'는 너무 낮습니다.');
    if(w>LIM.maxW||h>LIM.maxH)return fail('해상도 '+v.videoWidth+'×'+v.videoHeight+'는 최대 '+LIM.maxW+'×'+LIM.maxH+'를 넘습니다.');
    if(Math.abs(w/h-16/9)>0.02)return fail('16:9 영상만 분석합니다 ('+v.videoWidth+'×'+v.videoHeight+').');
    chk.textContent=v.videoWidth+'×'+v.videoHeight+' · '+Math.floor(d/60)+'분 '+Math.round(d%60)+'초 · '+(file.size/1048576).toFixed(0)+'MB';go.disabled=false;}};
  v.onerror=()=>{{URL.revokeObjectURL(v.src);chk.textContent='브라우저가 이 영상의 정보를 읽지 못했습니다. 서버에서 다시 확인합니다.';go.disabled=false;}};
}});
document.getElementById('up').addEventListener('submit',e=>{{e.preventDefault();const file=f.files[0];if(!file)return;
  go.disabled=true;f.disabled=true;bar.hidden=false;err.hidden=true;
  const x=new XMLHttpRequest();
  x.open('PUT','/api/upload?'+new URLSearchParams({{name:file.name,patch:document.getElementById('patch').value}}));
  x.upload.onprogress=ev=>{{if(ev.lengthComputable)bar.value=ev.loaded/ev.total*100;}};
  x.onload=()=>{{let r={{}};try{{r=JSON.parse(x.responseText)}}catch(_){{}}
    if(x.status===200&&r.job)location.href='/jobs/'+r.job;else{{fail(r.error||('업로드 실패 ('+x.status+')'));f.disabled=false;bar.hidden=true;}}}};
  x.onerror=()=>{{fail('업로드 중 연결이 끊겼습니다.');f.disabled=false;bar.hidden=true;}};
  x.send(file);}});
</script>"""


def page(title: str, body: str, user: dict | None, status: int = 200, refresh: bool = False) -> HTMLResponse:
    me = (f'<span class=me>{_e(user["name"])}</span><form method=post action=/logout><button class=link type=submit>로그아웃</button></form>'
          if user else "")
    return HTMLResponse(f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">{'<meta http-equiv=refresh content=30>' if refresh else ''}
<title>{_e(title)} · OWcoach</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;700&display=swap">
<style>{STYLE}</style></head><body>
<header class=top><a class=brand href=/>OWcoach 베타</a>{me}</header>
<main><h1>{_e(title)}</h1>{body}</main></body></html>""", status_code=status)


