# Oracle Cloud 무료 서버에 올리기

순서: 계정 → 서버 만들기 → 방화벽 열기 → 도메인 → 디스코드 앱 → 설치 → mock 시험 → 실제 분석.
명령은 모두 서버(Ubuntu)에서 실행합니다. `<...>`는 본인 값으로 바꿉니다.

## 1. 계정 만들기

1. https://www.oracle.com/cloud/free/ 에서 가입합니다. 본인 확인용 카드 등록이 필요하지만, Always Free 범위 안에서는 청구되지 않습니다.
2. **홈 리전**은 나중에 바꿀 수 없고, 무료 ARM 서버는 홈 리전에서만 만들 수 있습니다.
   목록에 South Korea Central (Seoul) / South Korea North (Chuncheon) 가 있으면 그중 하나를 고릅니다.
   신규 무료 가입에는 한국 리전이 목록에 없을 수 있습니다(2026-10 사용자 확인). 그때는 **Japan Central (Osaka)**,
   다음으로 Japan East (Tokyo) 를 고릅니다. 한국에서 지연은 수십 ms 수준이고 업로드가 조금 더 걸리는 정도입니다.
   싱가포르 등 더 먼 리전은 업로드가 느려 권하지 않습니다.

## 2. 서버 만들기

콘솔 → Compute → Instances → **Create instance**

| 항목 | 값 |
|---|---|
| Image | Canonical **Ubuntu 24.04** (aarch64) |
| Shape | Ampere **VM.Standard.A1.Flex**, OCPU **2**, 메모리 **12GB** (무료 한도: 계정 전체 4 OCPU · 24GB) |
| Networking | 새 VCN 만들기(기본값), **Assign a public IPv4 address** 켜기 |
| SSH keys | "Generate a key pair" → 개인 키 다운로드 (잃어버리면 접속 불가) |
| Boot volume | 크기 지정 켜고 **100GB** (무료 한도: 계정 전체 200GB) |

- **"Out of capacity"** 가 뜨면 그 리전에 남는 ARM 자리가 없다는 뜻입니다. 다른 Availability Domain을 고르거나,
  몇 시간 뒤 다시 시도합니다. 며칠째 안 되면 OCPU 1 · 메모리 6GB로 줄여 시도합니다(분석이 느려질 뿐 동작은 함).
- 만들어지면 **Public IP address** 를 적어 둡니다.

접속 (내 PC에서):

```bash
chmod 600 <다운로드한-키>.key
ssh -i <다운로드한-키>.key ubuntu@<공인 IP>
```

## 3. 방화벽 열기 (두 군데)

Oracle은 **클라우드 쪽**과 **서버 안쪽** 방화벽이 따로 있고, 둘 다 열어야 합니다.

**클라우드 쪽**: 인스턴스 화면 → Subnet 링크 → Security Lists → Default Security List → **Add Ingress Rules**
- Source CIDR `0.0.0.0/0`, IP Protocol TCP, Destination Port Range `80` → 추가
- 같은 방식으로 `443` 추가

**서버 안쪽** (Oracle Ubuntu 이미지는 기본으로 22번만 열려 있음):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 4. 도메인

HTTPS와 디스코드 로그인에 도메인이 필요합니다. 무료로는 DuckDNS가 간단합니다.

1. https://www.duckdns.org 에 로그인 → 서브도메인 만들기 (예: `owcoach-beta` → `owcoach-beta.duckdns.org`)
2. current ip 칸에 서버 **공인 IP** 를 넣고 update ip

이미 가진 도메인이 있다면 A 레코드를 공인 IP로 지정하면 됩니다. **Cloudflare 프록시(주황 구름)는 끄세요.**
무료 플랜은 요청 하나를 100MB로 제한해 영상 업로드가 실패합니다.

## 5. 디스코드 앱

[README.md 1번](README.md#1-디스코드-앱-만들기)대로 만들고, Redirect 에 `https://<도메인>/auth/callback` 을 넣습니다.
Client ID · Client Secret, 그리고 초대할 사람(최대 3명)의 디스코드 사용자 ID를 모아 둡니다.

## 6. 설치

```bash
# 도커 설치
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && newgrp docker

# 코드 받기 (베타 브랜치)
git clone -b claude/clever-turing-34j6ul https://github.com/pokposu124/OWcoach.git
cd OWcoach
```

저장소가 비공개라면 GitHub에서 읽기 전용 토큰(Fine-grained, Contents: Read)을 만들어
`https://<토큰>@github.com/pokposu124/OWcoach.git` 으로 받습니다.

설정 파일 두 개를 만듭니다 (`OWcoach/` 안에서):

```bash
echo "OWCOACH_DOMAIN=<도메인>" > .env

cat > owcoach.env <<'EOF'
DISCORD_CLIENT_ID=<Client ID>
DISCORD_CLIENT_SECRET=<Client Secret>
OWCOACH_ALLOWED_DISCORD_IDS=<내 ID>,<친구1 ID>,<친구2 ID>
OWCOACH_BASE_URL=https://<도메인>
OWCOACH_SESSION_SECRET=<아래 명령으로 만든 값>
GEMINI_API_KEY=<Gemini 키>
OWCOACH_GEMINI_MODEL=gemini-3.6-flash
OWCOACH_PROVIDER=mock
EOF
chmod 600 owcoach.env
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # 이 값을 OWCOACH_SESSION_SECRET 에
```

실행:

```bash
docker compose up -d --build      # 첫 빌드는 몇 분 걸림
docker compose logs -f owcoach    # Ctrl+C 로 빠져나옴 (서비스는 계속 돔)
```

## 7. mock으로 시험 (비용 없음)

`OWCOACH_PROVIDER=mock` 상태에서:

1. `https://<도메인>` 접속 → 자물쇠(HTTPS)가 보이는지 확인
2. 디스코드로 로그인 → "내 분석" 화면
3. 짧은 영상(1280×720 또는 1920×1080)을 올려서 대기 → 분석 중 → 완료 → 결과 보기까지 되는지 확인
   (결과 내용은 실제 분석이 아니라 시험용 가짜입니다)

mock 시험은 **이번 주 요청 횟수를 씁니다.** 시험이 끝나면 기록을 지워 횟수를 돌려놓습니다:

```bash
docker compose exec owcoach python -c "import sqlite3;c=sqlite3.connect('/data/owcoach.db');c.execute('delete from jobs');c.commit()"
```

## 8. 실제 분석으로 전환

### 8-1. Gemini 키 넣고 확인 (mock 인 채로)

키는 **Google AI Studio(aistudio.google.com/apikey) → Create API key** 에서 만듭니다. 지금 AI Studio 가 주는 키는
`AQ.` 로 시작하고 39자보다 깁니다(정상, 자르지 않음). 예전 형식은 `AIza` 로 시작하는 39자.
Google Cloud 콘솔의 OAuth 클라이언트 ID·비밀, 서비스 계정 JSON, `ya29.` 로 시작하는 액세스 토큰은 키가 아닙니다.

서버에서(`cd ~/OWcoach`). 키를 파일에 쓰기 **전에** 실제 분석과 같은 요청(generateContent)으로 Google 에 먼저 확인합니다:

```bash
read -rsp "Gemini 키 붙여넣고 Enter: " K; echo
K=$(printf %s "$K" | tr -d '\r\n\t '); echo "글자 수 ${#K}, 앞 4자 ${K:0:4}"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -H "x-goog-api-key: $K" -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"hi"}]}]}' \
  https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent
```

`HTTP 200` 이면 파일에 씁니다. 401 이면 5분 뒤 curl 만 다시(새 키는 바로 안 될 때가 있음). 그래도 401 이면
`AQ.` 키에서 같은 401(`ACCESS_TOKEN_TYPE_UNSUPPORTED`)이 나는 사례가 Google 포럼에 여럿 있어(2026-10 기준 확정 해결책 없음),
Google Cloud 콘솔 → API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → API 키(같은 프로젝트, API 제한: Generative Language API)로
`AIza` 키를 만들어 같은 순서로 다시 확인합니다. 거절 이유 보기:
`curl -s -H "x-goog-api-key: $K" https://generativelanguage.googleapis.com/v1beta/models | grep -E '"(message|reason)"'`

```bash
grep -v '^GEMINI_API_KEY=' owcoach.env > owcoach.env.new && printf 'GEMINI_API_KEY=%s\n' "$K" >> owcoach.env.new \
  && mv owcoach.env.new owcoach.env && chmod 600 owcoach.env && unset K      # 키에 특수문자가 있어도 안전(sed 치환 대신)
grep -c $'\r' owcoach.env                     # 0 이어야 함 (Windows 줄바꿈이 섞이면 키 끝에 붙음)
grep -c '^GEMINI_API_KEY=' owcoach.env        # 1 이어야 함
docker compose up -d --force-recreate owcoach
docker compose exec owcoach sh -c 'echo "키 길이 ${#GEMINI_API_KEY}"; env | grep -c "^GOOGLE_API_KEY="'   # 길이는 위와 같게, 두 번째 값 0 (있으면 그 키가 먼저 쓰임)
docker compose exec owcoach python -c "import os; from google import genai; c=genai.Client(); print(c.models.generate_content(model=os.environ['OWCOACH_GEMINI_MODEL'], contents='hi').text[:40])"
```

마지막 줄에 짧은 답이 나오면 됩니다. (`Client` 를 변수 `c` 에 담지 않으면 'Cannot send a request' 오류가 납니다.)
curl 은 200 인데 여기서만 401 이면 컨테이너의 google-genai 가 새 키 형식을 모르는 오래된 판일 수 있습니다:
`docker compose exec owcoach pip show google-genai | grep Version` 확인 후 `docker compose build --no-cache owcoach && docker compose up -d --force-recreate owcoach`.

### 8-2. mock 끄기

```bash
sed -i '/^OWCOACH_PROVIDER=/d' owcoach.env
git pull && docker compose up -d --build --force-recreate owcoach      # 최신 코드(인코딩 단축 포함)도 같이 반영
docker compose exec owcoach sh -c 'echo "provider=[${OWCOACH_PROVIDER:-}]"'   # provider=[] 이면 config.yaml 의 gemini
docker compose exec owcoach python -c "import sqlite3;c=sqlite3.connect('/data/owcoach.db');c.execute('delete from jobs');c.commit()"   # mock 시험 기록 삭제(주간 횟수·월 예산 원상복구)
```

첫 실제 분석은 운영자 본인 영상으로 돌려 보고, 걸린 시간과 비용(`/jobs`에서 결과 확인 후 README 4번의 비용 확인 명령)을 봅니다.
Google Cloud 결제에도 예산 알림을 걸어 둡니다.

## 9. 업데이트 · 백업

```bash
cd ~/OWcoach && git pull && docker compose up -d --build     # 새 버전 반영 (진행 중 분석은 대기열로 돌아갔다가 이어짐)
docker run --rm -v owcoach_owcoach-data:/data -v $PWD:/backup alpine tar czf /backup/owcoach-data.tgz /data/owcoach.db /data/results
```

## 알아 둘 것

- **무료 서버 회수**: Oracle은 Always Free 인스턴스가 7일 동안 거의 쓰이지 않으면(CPU·네트워크·메모리 사용이 아주 낮으면)
  회수할 수 있다고 안내합니다. 분석이 없는 주가 길어지면 해당될 수 있습니다. 계정을 Pay As You Go로 바꾸면
  이 회수 대상에서 빠진다고 안내되며 무료 한도 안의 사용은 계속 무료입니다. 조건은 Oracle 안내를 직접 확인하세요.
- **ARM 서버**: 분석기는 ffmpeg·numpy만 쓰므로 ARM에서 돌아야 하지만, 아직 ARM에서 실측한 적은 없습니다. 7번 mock 시험이 그 확인입니다.
- **공인 IP**: 인스턴스를 지우지 않는 한 유지됩니다. 지웠다 다시 만들면 IP가 바뀌니 DuckDNS를 갱신합니다.
