# Flask Blueprint 학습용 샘플

Flask의 **블루프린트(Blueprint)** 구조를 눈으로 익히기 위한 예제 프로젝트입니다.
로그인, AI 에이전트, 이벤트 알람, 관리자 페이지를 각각 별개의 블루프린트로 만들어서
"기능이 늘어날 때 파일이 어떻게 나뉘는가"를 보여줍니다.

모든 소스 파일에는 그 파일이 하는 일과 헷갈리기 쉬운 지점을 한국어 주석으로 적어두었습니다.

---

## 목차

1. [무엇을 하는 앱인가](#무엇을-하는-앱인가)
2. [프로젝트 구조](#프로젝트-구조)
3. [설치 및 실행](#설치-및-실행) ← **처음이면 여기부터**
4. [화면과 URL](#화면과-url)
5. [블루프린트 구조 이해하기](#블루프린트-구조-이해하기)
6. [AI 에이전트](#ai-에이전트)
7. [이벤트 질의어](#이벤트-질의어)
8. [Lambda 이벤트 정규화](#lambda-이벤트-정규화)
9. [환경변수 전체 목록](#환경변수-전체-목록)
10. [알려진 한계](#알려진-한계)

---

## 무엇을 하는 앱인가

일곱 개의 블루프린트로 이루어져 있습니다.

| 블루프린트 | url_prefix | 하는 일 |
|---|---|---|
| `main` | 없음 | 인덱스 페이지 |
| `auth` | `/auth` | 로그인 / 로그아웃 (세션 기반, 하드코딩 계정 1개) |
| `agent` | `/agent` | Claude 기반 채팅 에이전트 (도구 호출) |
| `alarm` | `/alarm` | 이벤트 접수 → Lambda로 정규화 → 알람 |
| `admin` | `/admin` | 설정·라우트 확인 (관리자 계정만 접근) |
| `dashboard` | `/dashboard` | PostgreSQL 집계 지표와 차트 |
| `explore` | `/explore` | 질의어로 이벤트 조회 (PromQL 스타일) |

핵심은 **각 기능이 서로의 코드를 건드리지 않는다**는 점입니다.
`agent`를 추가할 때 `main`과 `auth`는 한 줄도 수정하지 않았습니다.

---

## 프로젝트 구조

```
myapp/
├── run.py                      진입점. create_app() 을 호출해 앱 객체를 만든다
├── requirements.txt
├── .env.example                환경변수 견본 (복사해서 .env 로 사용)
├── docker-compose.yml          로컬 개발용 PostgreSQL
├── db/
│   └── schema.sql              events 테이블 DDL
│
├── app/                        ─── Flask 애플리케이션 ───
│   ├── __init__.py             create_app() 팩토리 + 블루프린트 등록
│   ├── config.py               환경별 설정 (개발/테스트/운영)
│   ├── cli.py                  flask init-db / db-check 명령
│   ├── agent_core.py           AI 에이전트의 도구 정의와 실행 루프
│   ├── event_store.py          이벤트 임시 보관소 (메모리)
│   ├── stats.py                대시보드용 집계 질의 (SQL)
│   ├── query.py                이벤트 질의어 파서 + SQL 컴파일러
│   ├── lambda_client.py        Lambda 호출 계층 (local / aws 전환)
│   ├── views/                  블루프린트별 라우트
│   │   ├── main.py  auth.py  agent.py  alarm.py  admin.py  dashboard.py  explore.py
│   ├── templates/              Jinja 템플릿
│   │   └── base.html  index.html  login.html  agent.html  alarm.html
│   │       admin.html  dashboard.html  explore.html
│   └── static/                 정적 파일 (Flask 가 /static/... 으로 자동 공개)
│       └── css/style.css       전체 스타일. base.html 에서 link 로 연결
│
└── api/                        ─── AWS Lambda 함수 ───
    └── normalize_handler.py    이벤트 정규화 → DB 적재 → 알람 발송
```

**의존 방향은 `app` → `api` 단방향입니다.** `api/`는 Flask를 전혀 import하지 않으므로
Lambda에 그대로 올릴 수 있고, 파이썬만 있으면 단독 실행됩니다.

---

## 설치 및 실행

### 사전 준비물

- **Python 3.9 이상** — `requirements.txt`의 핀 버전 기준 (검증은 3.11에서 진행)
- 그 외에는 아무것도 필요 없습니다. DB, AWS 계정, API 키 **없이도 앱은 실행됩니다.**
- (선택) **PostgreSQL 16** 또는 컨테이너 런타임 — 로컬 DB를 띄울 때만 필요합니다.
  회사에서 쓴다면 [선택 2 - 로컬 DB 띄우기](#선택-2--로컬-db-띄우기)의 라이선스 안내를 먼저 보세요.

> **Windows 사용자에게**
>
> 아래 명령은 **PowerShell** 기준으로 병기했습니다. 명령 프롬프트(cmd)가 아니라
> PowerShell을 여세요.
>
> - `python3` 대신 **`python`** 을 쓰세요. (Windows 파이썬은 보통 `python3` 별칭이 없습니다)
> - `pip`, `flask`, `docker` 명령은 양쪽이 동일합니다.
> - 명령을 `&&` 로 이어 붙이는 예시가 있다면, 줄을 나눠서 하나씩 실행하세요.

### 1단계 — 소스 받기

```bash
git clone https://github.com/eunyuljo/flask_app.git
cd flask_app/myapp
```

> 이후 모든 명령은 `myapp/` 안에서 실행합니다. `run.py`가 보이는 위치입니다.

### 2단계 — 가상환경 만들기

프로젝트 전용 파이썬 공간을 만들어 시스템 파이썬을 더럽히지 않습니다.

```bash
# macOS / Linux
python3 -m venv .venv

# Windows (PowerShell)
python -m venv .venv
```

활성화합니다. **터미널을 새로 열 때마다 다시 해야 합니다.**

```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

프롬프트 앞에 `(.venv)`가 붙으면 성공입니다.

### 3단계 — 패키지 설치

```bash
pip install -r requirements.txt
```

설치되는 것:

| 패키지 | 용도 | 없으면 |
|---|---|---|
| `Flask` | 웹 프레임워크 | 앱이 안 뜸 |
| `python-dotenv` | `.env` 파일 읽기 | 환경변수를 직접 export 해야 함 |
| `anthropic[bedrock]` | Claude 호출 + AWS SigV4 서명 | 에이전트 페이지만 못 씀 |

### 4단계 — 환경변수 파일 만들기

```bash
# macOS / Linux
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

**이 단계를 건너뛰어도 앱은 실행됩니다.** 기본값으로 동작하고, 설정이 필요한 기능만
화면에서 "설정이 필요합니다"라고 안내합니다.

`.env`는 `.gitignore`에 등록되어 있어 커밋되지 않습니다.

### 5단계 — 실행

```bash
python run.py
```

이렇게 나오면 정상입니다.

```
 * Serving Flask app 'app'
 * Debug mode: on
 * Running on http://127.0.0.1:5000
```

### 6단계 — 접속

브라우저에서 **http://127.0.0.1:5000** 을 엽니다.

로그인 정보는 다음과 같습니다.

| 항목 | 값 |
|---|---|
| 아이디 | `admin` |
| 비밀번호 | `1234` |

> 학습용이라 코드에 그대로 박혀 있습니다 (`app/views/auth.py`).

### 7단계 — 동작 확인

로그인 후 아래 순서로 눌러보면 전체 기능을 훑을 수 있습니다.

1. **이벤트** 메뉴 → 메시지 `디스크 사용률 95%`, 심각도 `FATAL` 입력 후 전송
   → `FATAL`이 `critical`로 정규화되어 표시되면 Lambda 로컬 호출이 성공한 것입니다.
2. **관리자** 메뉴 → 방금 보낸 이벤트가 통계에 집계되는지 확인
3. **AI 에이전트** 메뉴 → API 키를 넣었다면 "이 앱에 어떤 페이지가 있어?" 질문

### 종료

터미널에서 `Ctrl + C`. 가상환경을 빠져나오려면 `deactivate`.

---

### 선택 1 — AI 에이전트 켜기

`.env`를 열어 키를 넣고 서버를 재시작합니다.

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

AWS Bedrock을 경유하려면 `AGENT_PROVIDER`만 바꾸면 됩니다.
`AGENT_MODEL`은 그대로 두세요 — Bedrock용 `anthropic.` 접두사는 코드가 자동으로 붙입니다.

```bash
AGENT_PROVIDER=bedrock
AWS_REGION=ap-northeast-2
# 키를 비워두면 ~/.aws/credentials 나 IAM 역할에서 자동으로 찾습니다
```

### 선택 2 — 로컬 DB 띄우기

DB 없이도 앱은 돌지만, DB를 붙이면 Lambda가 정규화한 이벤트가 **실제로 테이블에 쌓입니다.**

> #### ⚠️ 회사에서 쓴다면: Docker Desktop 라이선스 확인 필요
>
> **Docker Desktop**은 다음 중 **하나라도** 해당하는 조직에서 유료 구독이 필요합니다.
>
> - 직원 **250명 초과**
> - 연 매출 **미화 1,000만 달러 초과**
>
> 개인 사용, 교육, 비상업적 오픈소스, 그리고 위 기준 미만의 소규모 사업체는 무료입니다.
> (2026년 8월 기준 — 약관은 바뀔 수 있으니 반드시 현재 조건을 확인하세요.)
>
> **중요: 이 프로젝트는 Docker Desktop이 필요 없습니다.** 필요한 건 `docker compose` 명령뿐이고,
> 이를 제공하는 무료·오픈소스 방법이 여러 가지 있습니다.

로컬 DB를 띄우는 방법은 네 가지입니다. **Windows 라면 맨 위(직접 설치)를 권합니다** —
컨테이너 런타임 자체가 필요 없고, 이 프로젝트는 DB 한 대만 쓰므로 컨테이너로 얻는 이점이 거의 없습니다.

| 방법 | 라이선스 | Windows | 비고 |
|---|---|---|---|
| **PostgreSQL 직접 설치** | PostgreSQL License | [설치본](https://www.postgresql.org/download/windows/) (16 권장) | Docker 자체가 불필요. **가장 단순** |
| **Docker Engine on WSL2** | Apache-2.0 | WSL2 안에서 설치 | Desktop 없이 CLI만. 무료. ↓ 아래 상세 |
| **Podman Desktop** | Apache-2.0 | 지원 | `docker compose` 호환 |
| Docker Desktop | 조건부 유료 | 지원 | 위 라이선스 조건 확인 |

**2-1. DB 준비하기 — 방법 A 또는 B 중 하나만 고르세요**

---

#### 방법 A — PostgreSQL 직접 설치 (Windows 권장)

**A-1.** [PostgreSQL Windows 설치본](https://www.postgresql.org/download/windows/)을 내려받아 실행합니다.

> **버전은 16을 받으세요.** 다운로드 페이지의 버전 목록에서 16.x 를 고르고,
> 플랫폼은 **Windows x86-64** 를 선택합니다.
>
> 이 프로젝트는 16에서 검증했고, `docker-compose.yml` 도 `postgres:16-alpine` 으로
> 맞춰져 있습니다. 나중에 컨테이너로 옮기더라도 버전이 같아서 헷갈릴 일이 없습니다.
>
> 최신 버전(18.x)을 써도 동작에는 문제가 없습니다. 이 프로젝트가 쓰는 SQL 기능
> (`JSONB`, `ON CONFLICT`, `CREATE INDEX IF NOT EXISTS`)은 PostgreSQL 9.5 이상이면
> 모두 지원합니다. 다만 `db-check` 에 찍히는 버전 문자열이 아래 예시와 달라집니다.
> **13 이하는 이미 지원이 끝났으니 피하세요.**

설치 중 물어보는 것들:

| 항목 | 입력 |
|---|---|
| 설치 구성요소 | 기본값 그대로 (pgAdmin 4, Command Line Tools 포함되어야 함) |
| **postgres 비밀번호** | **`postgres`** 로 하세요 (아래 설명 참고) |
| Port | `5432` (기본값 유지) |
| Locale | 기본값 |
| Stack Builder | 실행할 필요 없음 (마지막 체크 해제) |

> **비밀번호는 `postgres` 를 권합니다.** 계정 이름과 같아서 잊어버릴 일이 없고,
> 로컬 개발에서 관례적으로 쓰는 값입니다. A-2 단계에서 바로 다시 입력해야 하므로
> 복잡하게 정할 이유가 없습니다.
>
> 이 프로젝트에서 다루는 비밀번호는 두 개입니다. 헷갈리지 마세요.
>
> | 계정 | 비밀번호 | 언제 쓰나 |
> |---|---|---|
> | `postgres` (관리자) | `postgres` ← 지금 정하는 값 | DB/사용자를 만들 때. A-2, A-3 |
> | `flask_user` (앱 전용) | `flask_password` ← 이미 고정됨 | 앱이 DB에 접속할 때. 건드릴 필요 없음 |
>
> **왜 이래도 되나:** PostgreSQL 은 기본 설정에서 `localhost` 만 열어두므로(`listen_addresses`),
> 이 DB 는 사용자분 PC 밖에서 접근할 수 없습니다. 학습·테스트 목적의 로컬 DB 이고
> 실제 데이터가 없으므로 강한 비밀번호가 필요 없습니다.
>
> **다만 이렇게 하세요:**
> - 다른 곳에서 쓰는 비밀번호를 재사용하지 마세요
> - 이 PC를 공용으로 쓴다면 다른 값을 정하세요
> - 외부에서 접근 가능한 서버에는 이 설정을 그대로 옮기지 마세요

**A-2.** 시작 메뉴에서 **SQL Shell (psql)** 을 엽니다.

Server / Database / Port / Username은 전부 Enter로 넘기고,
Password에 A-1에서 정한 값(`postgres`)을 입력합니다.

> 입력해도 화면에 아무것도 안 보이는 게 정상입니다. 그대로 Enter 를 누르세요.

**A-3.** 아래 두 줄을 실행합니다.

```sql
CREATE USER flask_user WITH PASSWORD 'flask_password';
CREATE DATABASE flask_app OWNER flask_user;
```

`CREATE ROLE` / `CREATE DATABASE` 가 출력되면 성공입니다. `\q` 로 나옵니다.

> 이 계정 정보는 `app/config.py` 기본값과 같아서 **`.env` 를 건드릴 필요가 없습니다.**

**→ 아래 2-2로 진행하세요.** (`docker compose` 관련 명령은 전부 건너뜁니다)

---

#### 방법 B — 컨테이너로 띄우기

Docker Desktop / Docker Engine on WSL2 / Podman 중 하나가 이미 준비된 경우에만 해당합니다.

```bash
docker compose up -d
```

`docker-compose.yml`의 계정 정보도 `app/config.py` 기본값과 동일하게 맞춰두었습니다.

| 항목 | 값 |
|---|---|
| 호스트 / 포트 | `localhost` / `5432` |
| 사용자 / 비밀번호 | `flask_user` / `flask_password` |
| DB 이름 | `flask_app` |

**DB 끄기 / 초기화**

```bash
docker compose down        # 끄기 (데이터는 남음)
docker compose down -v     # 데이터까지 삭제
```

> 포트 5432가 이미 쓰이고 있다면 `docker-compose.yml`의 왼쪽 포트 번호와
> `.env`의 `DB_PORT`를 함께 바꾸세요.

---

> **검증 범위:** 방법 A(직접 설치 → 위 SQL → `init-db` → 적재)는 Linux에서 실제로
> 검증했습니다. 방법 B의 `docker compose` 경로는 설정값 대조만 했고 실행 검증은
> 하지 못했습니다. Windows 설치 프로그램의 화면 흐름도 검증하지 못했습니다.

<details>
<summary><b>Windows에서 Docker Desktop 없이 컨테이너를 쓰려면 (WSL2 + Docker Engine)</b></summary>

컨테이너는 Linux 커널 기능이라 Windows에서 그냥 돌지 않습니다. Docker Desktop이 하는 일이
바로 그 Linux 환경을 제공하는 것인데, **WSL2가 이미 Linux 커널**이므로 Desktop 없이
WSL2 안에 Docker Engine을 직접 설치하면 됩니다. Engine과 Compose는 둘 다 Apache-2.0입니다.

> Windows용 Docker 설치 프로그램을 돌리는 게 아니라, **WSL2 안에 들어가서
> Ubuntu용 Docker를 설치**하는 것입니다.

**1. WSL2와 Ubuntu 설치** (PowerShell을 관리자로 실행)

```powershell
wsl --install -d Ubuntu
```

재부팅 후 Ubuntu가 시작되며 사용자 이름과 비밀번호를 만듭니다.
이미 WSL을 쓰고 있다면 버전이 2인지 확인하세요.

```powershell
wsl -l -v      # VERSION 열이 2 여야 합니다
```

**2. Ubuntu 안에서 Docker Engine 설치**

WSL2의 Ubuntu 터미널에서 실행합니다. 아래는 Docker 공식 설치 스크립트입니다.

```bash
curl -fsSL https://get.docker.com | sh
```

> 스크립트를 파이프로 실행하는 게 꺼려진다면 apt 저장소를 직접 등록하는 방법을 쓰세요.
> 명령이 버전마다 바뀌므로 [공식 문서](https://docs.docker.com/engine/install/ubuntu/)의
> 최신 절차를 그대로 따르는 것이 안전합니다. 이때 `docker-compose-plugin` 패키지를
> 반드시 포함해야 `docker compose` 명령이 생깁니다.

**3. sudo 없이 쓰도록 설정**

```bash
sudo usermod -aG docker $USER
```

적용하려면 Ubuntu 터미널을 닫았다가 다시 엽니다.

**4. 데몬 시작 및 확인**

```bash
sudo service docker start     # systemd 가 켜져 있다면 자동 시작됩니다
docker compose version
```

**5. ⚠️ Flask 앱도 WSL2 안에서 실행하세요**

여기가 가장 헷갈리는 부분입니다.

| 구성 | 결과 |
|---|---|
| DB는 WSL2, Flask는 Windows | WSL2의 localhost 포워딩에 의존. 대체로 되지만 WSL 재시작이나 네트워크 설정에 따라 깨질 수 있음 |
| **DB도 WSL2, Flask도 WSL2** | 같은 Linux 안이라 문제 없음. **이쪽을 권장** |

즉 WSL2 경로를 택한다는 것은 "Docker만 WSL2에 깐다"가 아니라
**"개발 환경을 WSL2로 옮긴다"**에 가깝습니다. 소스도 WSL2 파일시스템
(`/home/사용자/...`)에 두는 편이 빠릅니다. Windows 경로(`/mnt/c/...`)는
파일 접근이 느립니다.

VS Code의 **WSL 확장**을 쓰면 Windows에서 편집하면서 실행만 WSL2에서 하도록
연결할 수 있습니다.

이후로는 이 문서의 모든 Linux 명령을 WSL2 터미널에서 그대로 쓰면 됩니다
(`python3 -m venv`, `source .venv/bin/activate`, `cp` 등 — PowerShell 버전이 아니라
**macOS / Linux 쪽 명령**을 쓰세요).

---

**이 절차는 검증되지 않았습니다.** 이 프로젝트를 개발한 환경은 Linux 컨테이너라
WSL2가 없습니다. 명령이 맞지 않으면 Docker 공식 문서를 기준으로 삼으세요.

**이 프로젝트만 놓고 보면 WSL2까지 갈 이유는 크지 않습니다.** DB 한 대만 필요한데
컨테이너로 얻는 이점이 거의 없기 때문입니다. 이미 WSL2를 쓰고 있거나 다른 이유로
컨테이너가 필요한 경우에 선택하세요. 그 외에는 위의 PostgreSQL 직접 설치가 빠릅니다.

</details>

**2-2. 테이블 만들기** (A / B 공통)

```bash
flask --app run init-db
```

```
완료: /경로/myapp/db/schema.sql 적용됨
```

여러 번 실행해도 안전합니다 (`CREATE TABLE IF NOT EXISTS`).

**2-3. 확인**

```bash
flask --app run db-check
```

```
접속 대상: postgresql://flask_user:***@localhost:5432/flask_app
서버: PostgreSQL 16.13
events 테이블: 있음 (0 건)
```

**2-3-1. 샘플 데이터 넣어보기 (선택)**

대시보드가 어떻게 보이는지 바로 확인하고 싶다면 샘플 이벤트를 만들어 넣을 수 있습니다.

```bash
flask --app run seed-events --count 300 --hours 48 --clear
```

```
300 건을 최근 48 시간에 걸쳐 넣었습니다. (현재 총 300 건)
  info      166
  warning   66
  error     47
  critical  21
```

| 옵션 | 뜻 | 기본값 |
|---|---|---|
| `--count` | 만들 이벤트 수 | 200 |
| `--hours` | 몇 시간에 걸쳐 흩뿌릴지 | 48 |
| `--clear` | 기존 이벤트를 모두 지우고 시작 | 끔 |

원본 표기를 일부러 제각각으로(`FATAL`, `p1`, `msg`, `service` …) 만들어서 넣으므로,
**정규화가 실제로 동작하는 것도 함께 확인**할 수 있습니다. 최근일수록 촘촘하게
분포시켜서 시간대별 그래프에 추세가 보이도록 했습니다.

**2-4. Lambda가 DB에 쓰도록 켜기**

`store_event()`는 `DATABASE_URL`이 있을 때만 적재합니다. `.env`에 추가하세요.

```bash
DATABASE_URL=postgresql://flask_user:flask_password@localhost:5432/flask_app
```

> `DB_*` 항목과 달리 여기엔 `+psycopg`를 **붙이지 않습니다.** Lambda 핸들러는
> SQLAlchemy가 아니라 psycopg를 직접 쓰기 때문입니다.

이제 이벤트를 보내면 `store` 결과가 `{"stored": true}`로 바뀝니다.

```bash
# 방법 A (직접 설치) — psql 이 함께 설치되어 있습니다
psql -h localhost -U flask_user -d flask_app -c "SELECT severity, source, message FROM events;"

# 방법 B (컨테이너) — 컨테이너 안의 psql 을 그대로 씁니다
docker compose exec db psql -U flask_user -d flask_app -c "SELECT severity, source, message FROM events;"
```

> Windows에서 방법 A로 설치했다면 시작 메뉴의 **SQL Shell (psql)** 을 열어
> `SELECT ... FROM events;` 를 입력해도 됩니다. `psql` 명령이 PATH에 없을 수 있는데,
> 그때는 이 방법이 확실합니다.
>
> 건수만 빠르게 보려면 `flask --app run db-check` 로도 충분합니다.

### 선택 3 — 실제 Lambda 호출하기

기본값 `LAMBDA_MODE=local`은 `api/normalize_handler.py`를 같은 프로세스에서 직접 부릅니다.
실제 AWS Lambda를 호출하려면:

```bash
LAMBDA_MODE=aws
LAMBDA_FUNCTION_NAME=flask-app-normalize-event
AWS_REGION=ap-northeast-2
```

배포 방법은 [Lambda 이벤트 정규화](#lambda-이벤트-정규화) 절을 참고하세요.

---

## 화면과 URL

| URL | 메서드 | 로그인 | 설명 |
|---|---|---|---|
| `/` | GET | – | 인덱스 |
| `/auth/login` | GET, POST | – | 로그인 폼 / 처리 |
| `/auth/logout` | GET | – | 로그아웃 |
| `/agent/` | GET | 필요 | 에이전트 채팅 화면 |
| `/agent/ask` | POST | 필요 | 질문 전송 |
| `/agent/reset` | POST | 필요 | 대화 초기화 |
| `/alarm/` | GET | 필요 | 이벤트 목록 |
| `/alarm/send` | POST | 필요 | 폼으로 이벤트 전송 |
| `/alarm/api/events` | POST | **불필요** | JSON 수집 엔드포인트 |
| `/dashboard/` | GET | 필요 | 지표 대시보드 (`?hours=6\|24\|72`) |
| `/explore/` | GET | 필요 | 질의어로 이벤트 조회 (`?q=`, `?hours=`) |
| `/admin/` | GET | 관리자 | 설정·라우트 확인 |
| `/admin/events/clear` | POST | 관리자 | 이벤트 비우기 |

관리자 계정은 `ADMIN_USERS` 환경변수로 정합니다(기본 `admin`, 쉼표로 여러 명).

---

## 이벤트 질의어

`/explore/` 에서 PromQL 을 흉내 낸 질의어로 이벤트를 조회할 수 있습니다.

```
{}                                          전체
{severity="critical"}                       심각도가 critical
{severity=~"critical|error"}                critical 또는 error (정규식)
{source="web-01", severity!="info"}         web-01 의 info 아닌 것
{message=~"디스크.*"}                        메시지가 '디스크' 로 시작
{} | count by severity                      심각도별 건수
{severity=~"critical|error"} | count by source   알람 대상을 출처별로
{source=~"web-.*"} | count                  총 건수 하나만
```

**라벨**: `severity` `source` `type` `message` `fingerprint`
**연산자**: `=` `!=` `=~`(정규식) `!~`
**집계**: `| count` · `| count by 라벨`

### 왜 SQL 을 직접 입력받지 않았나

화면에서 SQL 을 그대로 받으면 편하지만, 그 순간 `DROP TABLE` 부터 다른 테이블 열람까지
전부 열립니다. 문자열에서 위험한 낱말을 걸러내는 방식은 우회가 쉬워 방어가 되지 않습니다.

그래서 **전용 질의어를 만들고 파라미터 바인딩된 SQL 로 컴파일**합니다.
사용자가 쓴 글자는 SQL 문장에 절대 들어가지 않습니다.

| | 어디서 오나 |
|---|---|
| 컬럼 이름 | `app/query.py` 의 `LABELS` 허용 목록 (사용자 입력 아님) |
| 연산자 | `OPERATORS` 허용 목록 |
| **값** | 전부 `%s` 파라미터로 분리 전달 |

여기에 질의당 5초 제한(`statement_timeout`)을 걸어, 무거운 정규식이 DB 를 붙잡는 것도 막습니다.
탐색 화면 우측 하단에 **생성된 SQL 과 파라미터가 그대로 표시**되므로,
질의어가 무엇으로 바뀌는지 직접 확인할 수 있습니다.

---

## 블루프린트 구조 이해하기

### url_prefix는 등록하는 쪽에서 붙는다

블루프린트 파일 안에서는 접두사를 쓰지 않습니다.

```python
# app/views/auth.py
@auth_bp.route("/login")      # "/auth/login" 이 아니다
def login():
    ...
```

```python
# app/__init__.py
app.register_blueprint(auth_bp, url_prefix="/auth")   # 여기서 "/auth" 가 붙는다
```

최종 URL은 `/auth/login`이 됩니다. 파일 안에 `/auth/login`이라고 쓰면
`/auth/auth/login`이 되어버립니다.

### url_for의 이름은 파일명이 아니다

```python
main_bp = Blueprint("main", __name__)   # ← 이 "main" 이 기준
```

따라서 `url_for('main.index')`입니다. 파일명(`main.py`)도, 변수명(`main_bp`)도 아닙니다.
셋이 비슷해서 가장 많이 헷갈리는 부분입니다.

### 접근 제어는 블루프린트 단위로

```python
# app/views/agent.py
@agent_bp.before_request
def require_login():
    if not session.get("username"):
        return redirect(url_for("auth.login"))
```

`@app.before_request`였다면 앱 전체가 막혀 로그인 페이지조차 못 들어갑니다.
블루프린트에 붙이면 그 블루프린트의 라우트에만 적용됩니다.

---

## AI 에이전트

단순히 질문을 던지고 답을 받는 게 아니라, **모델이 스스로 판단해 서버의 함수를 실행시키는**
구조입니다.

```
사용자: "이 앱에 어떤 페이지가 있어?"
   ↓
모델: list_app_routes 도구를 호출해줘        ← 1차 응답
   ↓
서버: 실제로 함수 실행, url_map 읽어서 결과 반환
   ↓
모델: 결과를 보고 최종 답변 작성              ← 2차 응답
```

현재 도구는 두 개입니다.

| 도구 | 하는 일 |
|---|---|
| `get_current_time` | 서버의 현재 시각 |
| `list_app_routes` | 이 앱에 등록된 URL 목록 |

### 도구 추가하는 법

`app/agent_core.py`에서 함수를 만들고 `TOOLS`에 넣기만 하면 됩니다.

```python
@beta_tool
def count_events() -> str:
    """보관 중인 이벤트 개수를 돌려준다. 이벤트가 몇 건 쌓였는지 물어보면 사용한다."""
    return str(len(event_store.recent()))

TOOLS = [get_current_time, list_app_routes, count_events]
```

**docstring이 곧 "언제 이 도구를 쓸지"에 대한 모델의 판단 근거입니다.**
성의 없이 쓰면 도구를 안 부르거나 엉뚱하게 부릅니다.

---

## Lambda 이벤트 정규화

### 왜 정규화가 필요한가

보내는 쪽마다 필드 이름과 심각도 표기가 제각각입니다.
Lambda가 이걸 하나의 형태로 맞춥니다.

**필드 별칭**

| 표준 이름 | 이렇게 들어와도 받아줌 |
|---|---|
| `message` | `msg`, `description`, `text` |
| `severity` | `level`, `priority` |
| `source` | `from`, `origin`, `service` |
| `event_type` | `type`, `kind`, `category` |
| `occurred_at` | `timestamp`, `time`, `ts` |

**심각도 통일** (4단계)

| 표준 | 이렇게 들어와도 받아줌 |
|---|---|
| `critical` | critical, crit, fatal, p1, 5, emergency |
| `error` | error, err, high, p2, 4 |
| `warning` | warn, warning, medium, p3, 3 |
| `info` | info, information, low, debug, p4, 2, 1 |

`critical`과 `error`만 알람 발송 대상입니다.

### 입력과 출력 예시

```bash
# macOS / Linux
curl -X POST http://127.0.0.1:5000/alarm/api/events \
  -H 'Content-Type: application/json' \
  -d '{"msg":"결제 실패율 급증","priority":"p1","origin":"pay-api","host":"i-123"}'
```

```powershell
# Windows (PowerShell)
$body = @{ msg = "결제 실패율 급증"; priority = "p1"; origin = "pay-api"; host = "i-123" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:5000/alarm/api/events `
  -Method Post -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

> PowerShell에서 `curl` 은 `Invoke-WebRequest` 의 별칭이라 위 bash 명령이 그대로 동작하지 않습니다.
> 한글이 깨지지 않도록 본문을 UTF-8 바이트로 변환해서 보내는 점에 주의하세요.

응답은 정규화 결과(`record`)와 후속 처리 결과(`store`, `alarm`)를 함께 돌려줍니다.

```json
{
  "ok": true,
  "record": {
    "event_id": "a88d886a176548da8e8986447360f02b",
    "event_type": "unknown",
    "source": "pay-api",
    "severity": "critical",
    "message": "결제 실패율 급증",
    "occurred_at": "2026-08-20T17:26:22.328074+00:00",
    "received_at": "2026-08-20T17:26:22.328091+00:00",
    "fingerprint": "17ad93510385dc5a",
    "meta": { "host": "i-123" }
  },
  "store": { "stored": false, "reason": "DATABASE_URL 미설정" },
  "alarm": { "alarmed": false, "reason": "ALARM_SNS_TOPIC_ARN 미설정" }
}
```

`priority: "p1"` → `severity: "critical"`, `origin` → `source`, `msg` → `message`로 바뀌고,
표준 필드에 없는 `host`는 버려지지 않고 `meta`에 담깁니다.

`store`와 `alarm`이 `false`인 것은 실패가 아니라 **설정이 없어 건너뛴 것**입니다.
`DATABASE_URL`과 `ALARM_SNS_TOPIC_ARN`을 Lambda 쪽에 넣으면 실제로 적재·발송합니다.

`fingerprint`는 같은 종류의 이벤트를 묶는 해시입니다.
"같은 지문은 5분에 한 번만 알람" 같은 억제 규칙을 걸 때 씁니다.

### local 모드와 aws 모드

| | `LAMBDA_MODE=local` (기본) | `LAMBDA_MODE=aws` |
|---|---|---|
| 호출 방식 | 핸들러를 파이썬 함수로 직접 실행 | boto3로 실제 Lambda 호출 |
| AWS 필요 | 없음 | 자격증명 + 함수 배포 |
| 재현되는 것 | 정규화 로직 전부 | 정규화 + 타임아웃 + 동시성 + 비동기 |

Lambda 핸들러는 결국 `(event, context)`를 받는 평범한 함수라서, 로컬에서는 그냥 호출하면 됩니다.

### 배포

`api/` 디렉터리 내용만 zip으로 묶는 경우:

```bash
# macOS / Linux
cd api && zip -r ../function.zip . && cd ..
```

```powershell
# Windows (PowerShell)
Compress-Archive -Path api\* -DestinationPath function.zip -Force
```

이때 핸들러 이름은 **`normalize_handler.lambda_handler`** 입니다.
프로젝트 루트째 묶었다면 `api.normalize_handler.lambda_handler`가 됩니다.

Lambda에서 DB 적재와 SNS 알람까지 쓰려면 배포 패키지에 `psycopg`와 `boto3`가 필요하고,
아래 환경변수를 Lambda 쪽에 설정해야 합니다.

```
DATABASE_URL=postgresql://...
ALARM_SNS_TOPIC_ARN=arn:aws:sns:...
```

두 값이 없으면 정규화만 하고 적재·알람은 조용히 건너뜁니다(에러가 아닙니다).

테이블 DDL은 `db/schema.sql`에 있습니다. 로컬에서는 `flask --app run init-db` 로 적용합니다.

---

## 환경변수 전체 목록

`.env.example`을 복사해서 쓰면 됩니다. **전부 선택 사항이며, 없으면 기본값으로 동작합니다.**

### 기본

| 변수 | 기본값 | 설명 |
|---|---|---|
| `FLASK_CONFIG` | `default` | `development` / `testing` / `production` |
| `SECRET_KEY` | `dev-secret-key-change-me` | 세션·flash 쿠키 서명 키 |
| `ADMIN_USERS` | `admin` | 관리자 계정, 쉼표로 구분 |

운영 환경(`FLASK_CONFIG=production`)에서는 `SECRET_KEY`가 기본값이면 기동을 거부합니다.
새 키는 이렇게 만듭니다.

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### DB

| 변수 | 기본값 |
|---|---|
| `DATABASE_URL` | (없음, 있으면 아래 항목들보다 우선) |
| `DB_DRIVER` | `postgresql+psycopg` |
| `DB_USER` / `DB_PASSWORD` | `flask_user` / `flask_password` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | `localhost` / `5432` / `flask_app` |

`DB_*` 항목은 `flask init-db` / `db-check` 가 사용합니다. Lambda 의 이벤트 적재는
별도로 `DATABASE_URL` 을 봅니다(`+psycopg` 없이). 자세한 내용은
[선택 2 - 로컬 DB 띄우기](#선택-2--로컬-db-띄우기)를 참고하세요.

### AI 에이전트

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AGENT_PROVIDER` | `claude_api` | `claude_api` 또는 `bedrock` |
| `ANTHROPIC_API_KEY` | (없음) | `claude_api` 모드에서 필요 |
| `AGENT_MODEL` | `claude-opus-5` | Bedrock 접두사는 자동 처리 |
| `AGENT_MAX_TOKENS` | `16000` | 응답 최대 토큰 |
| `AGENT_EFFORT` | `medium` | `low`~`max`. 높을수록 느리고 정확 |
| `AGENT_FALLBACK_MODEL` | `claude-opus-4-8` | 거절 시 대체 모델 |

### Lambda / AWS

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LAMBDA_MODE` | `local` | `local` 또는 `aws` |
| `LAMBDA_FUNCTION_NAME` | `flask-app-normalize-event` | `aws` 모드에서 호출할 함수 |
| `LAMBDA_INVOCATION_TYPE` | `RequestResponse` | 동기. 비동기는 `Event` |
| `AWS_REGION` | `us-east-1` | |
| `AWS_ACCESS_KEY_ID` 등 | (없음) | 비우면 IAM 역할·`~/.aws/credentials` 사용 |

---

## 알려진 한계

학습용 샘플이라 의도적으로 단순화한 부분과, 아직 고치지 않은 문제가 있습니다.

### 의도적 단순화

- **계정이 코드에 하드코딩**되어 있습니다 (`admin` / `1234`).
- **이벤트와 대화가 메모리에만** 저장됩니다. 서버를 재시작하면 사라지고,
  워커를 여러 개 띄우면(`gunicorn -w 4`) 요청마다 다른 메모리를 보게 됩니다.
  실제 서비스라면 Redis나 DB가 필요합니다.
- 이벤트 보관 상한은 **100건**이며, 넘으면 오래된 것부터 밀려납니다.

### 아직 고치지 않은 문제

| 문제 | 영향 |
|---|---|
| 로그아웃해도 에이전트 대화가 남음 | 같은 브라우저의 다음 사용자에게 이전 대화가 보일 수 있음 |
| `production` 설정이 DB 비밀번호를 요구 | `DATABASE_URL` 만 쓰는 배포에서도 `DB_PASSWORD` 를 요구할 수 있음 |
| `WTF_CSRF_ENABLED` 설정이 실제로는 무의미 | Flask-WTF 미설치. CSRF 방어가 없는데 있는 것처럼 보임 |
| 대화 길이 상한 없음 | 대화가 길어질수록 토큰 비용이 계속 증가 |
| `/alarm/api/events`에 인증 없음 | 누구나 이벤트를 밀어넣을 수 있음 |

### 검증되지 않은 부분

- **Bedrock 경로는 실제 호출로 검증되지 않았습니다.** 요청이 올바른 형태로
  만들어지는 것까지만 확인했습니다.
- **`LAMBDA_MODE=aws` 경로도 마찬가지**입니다. 로컬 모드만 실제로 동작을 확인했습니다.
- 자동화된 테스트 코드가 없습니다.
