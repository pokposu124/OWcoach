# OWcoach 지인 베타 서비스

지인 3명까지 디스코드로 로그인해 영상을 올리고, 분석이 끝나면 자기 결과 화면을 보는 서비스입니다.
서버 1대에서 웹 서버와 분석 작업자 1개가 함께 돌고, 분석은 한 번에 1건씩 순서대로 합니다.

| 항목 | 값 (`config.yaml`) |
|---|---|
| 사용자 | 최대 3명 (`OWCOACH_ALLOWED_DISCORD_IDS`에 적은 디스코드 계정만) |
| 요청 횟수 | 1인당 최근 7일 2회 (입력 조건 불충족·분석 실패는 세지 않음, 같은 영상 다시 올리면 이전 결과) |
| 월 예산 | $50 (한국 시간 기준 이번 달 Gemini 비용 합계. 대기·진행 중 분석은 1건당 $1.5를 미리 잡음) |
| 지원 영상 | 아나, 1280×720 ~ 1920×1080, 16:9, 5분 이하, 2000MB 이하 |
| 분석 시간 | 영상 1개에 약 15~20분 (2 vCPU 기준 실측 필요) |

Gemini 비용은 서버에 넣은 API 키 주인이 냅니다. 예산 상한은 코드가 새 요청을 거절하는 방식이라,
Google Cloud 결제 예산 알림도 함께 걸어 두는 것을 권합니다(알림은 청구를 막지 않음).

## 1. 디스코드 앱 만들기

1. https://discord.com/developers/applications → New Application (이름 예: OWcoach 베타)
2. OAuth2 → Client ID 복사, Client Secret 재설정 후 복사
3. OAuth2 → Redirects 에 `https://<도메인>/auth/callback` 추가
4. 초대할 사람의 디스코드 사용자 ID 모으기: 디스코드 설정 → 고급 → 개발자 모드 켜기 → 프로필 우클릭 → 사용자 ID 복사

봇은 만들지 않습니다. 로그인은 `identify` 권한(사용자 ID와 이름)만 씁니다.

## 2. 환경 변수

| 변수 | 내용 |
|---|---|
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | 1번에서 복사한 값 |
| `OWCOACH_ALLOWED_DISCORD_IDS` | 쉼표로 구분한 디스코드 사용자 ID, 최대 3개 |
| `OWCOACH_BASE_URL` | `https://<도메인>` (디스코드 Redirect와 같아야 함) |
| `OWCOACH_SESSION_SECRET` | 로그인 쿠키 서명 키, 32자 이상 무작위 (`python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
| `GEMINI_API_KEY`, `OWCOACH_GEMINI_MODEL` | 분석기와 같음 (`gemini-3.6-flash`) |
| `OWCOACH_DATA_DIR` | DB·업로드·분석 캐시·결과 저장 위치 (도커 이미지 기본 `/data`) |
| `OWCOACH_PROVIDER` | (선택) `mock`이면 Gemini 없이 흐름만 시험. 결과는 실제 분석이 아님. 비우면 `config.yaml` 값(gemini) |

## 3. 서버에서 실행 (도커)

Oracle Cloud 무료 서버에 올리는 순서는 [DEPLOY_ORACLE.md](DEPLOY_ORACLE.md)에 단계별로 있습니다.

서버는 리눅스 VM 1대(예: 2 vCPU, 4GB 메모리, 디스크 40GB 이상)를 기준으로 합니다.

```bash
docker build -t owcoach-beta .
docker run -d --name owcoach --restart unless-stopped -p 127.0.0.1:8000:8000 \
  --env-file owcoach.env -v owcoach-data:/data owcoach-beta
```

HTTPS는 앞에 Caddy를 두는 것이 가장 간단합니다 (`/etc/caddy/Caddyfile`):

```
<도메인> {
    reverse_proxy 127.0.0.1:8000
}
```

로컬에서 시험할 때는 `OWCOACH_BASE_URL=http://localhost:8000` 으로 두고 디스코드 Redirect에도
`http://localhost:8000/auth/callback`을 추가한 뒤, `service/`에서
`uvicorn owcoach_service.app:create_app --factory --port 8000` 으로 실행합니다.
`config.yaml`의 `provider: mock` 이면 Gemini 비용 없이 흐름만 확인할 수 있습니다(결과는 실제 분석이 아님).

## 4. 운영

- 사용자 추가·제거: `OWCOACH_ALLOWED_DISCORD_IDS`를 고치고 컨테이너 재시작. 목록에서 빠진 사람은 바로 로그아웃 상태가 됩니다.
- 이번 달 비용: `sqlite3 /data/owcoach.db "select sum(cost_usd) from jobs where status='done' and created_at >= '2026-10-01'"`
  (`cost_usd`는 분석기가 토큰 수 × `analyzer/config/pricing.yaml` 단가로 계산한 추정치)
- 로그: `docker logs owcoach` (분석 완료·실패 한 줄씩)
- 서버가 분석 도중 재시작되면 그 분석은 대기열로 돌아가고, 끝난 단계는 캐시(`/data/runs`)를 다시 씁니다.
- 원본 영상은 분석이 끝나면 지웁니다. 결과 화면(`/data/results/<작업>`)은 축소 영상(480p)과 근거 프레임을 가집니다.
  분석 캐시(`/data/runs`)에는 720p 사본이 남으니 디스크가 차면 오래된 것부터 지워도 됩니다(결과 화면은 영향 없음).
- 결과 화면은 올린 본인만 볼 수 있습니다. 영상 속 다른 플레이어 닉네임은 가리지 않습니다(사용자 결정).

## 5. 구조

```
브라우저 ─ PUT /api/upload (영상 원본, 횟수·예산 확인 후 저장, ffprobe로 조건 확인)
   │          └→ jobs 테이블 (queued)
   │                 └→ 작업자: python -m owcoach analyze → owcoach export → results/<작업>/ (done)
   └─ /jobs, /jobs/<작업> (진행: 분석기의 [n/5] 줄), /results/<작업>/ (본인만)
```

- `owcoach_service/app.py` 페이지·로그인·업로드, `worker.py` 분석 작업자, `limits.py` 횟수·예산, `db.py` SQLite
- 테스트: `service/`에서 `python -m pytest tests` (디스코드는 가짜 응답, 분석은 mock 공급자)
