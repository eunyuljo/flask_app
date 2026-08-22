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
8. [리소스 변경 추적](#리소스-변경-추적)
9. [운영 리포트](#운영-리포트)
10. [AWS 콘솔 (다중 계정)](#aws-콘솔-다중-계정)
11. [알람 AI 진단](#알람-ai-진단)
12. [작업 기록 (변경 증적)](#작업-기록-변경-증적)
13. [런북](#런북)
14. [당직 인계](#당직-인계)
15. [장애 사후 보고서 (RCA)](#장애-사후-보고서-rca)
16. [알람 노이즈](#알람-노이즈)
17. [고객사 현황](#고객사-현황)
18. [테스트](#테스트)
19. [이벤트의 계정 귀속](#이벤트의-계정-귀속)
20. [로그인과 권한](#로그인과-권한)
21. [감사 로그](#감사-로그)
22. [최초 대응 시간 (SLA)](#최초-대응-시간-sla)
23. [데이터 보존](#데이터-보존)
24. [Slack 연동](#slack-연동)
25. [에스컬레이션](#에스컬레이션)
26. [지난 장애를 진단 재료로](#지난-장애를-진단-재료로)
27. [컴플라이언스 점검](#컴플라이언스-점검)
28. [메뉴와 카테고리](#메뉴와-카테고리)
29. [월간 서비스 리뷰 (MSR)](#월간-서비스-리뷰-msr)
30. [온보딩 준비도](#온보딩-준비도)
31. [Lambda 이벤트 정규화](#lambda-이벤트-정규화)
32. [환경변수 전체 목록](#환경변수-전체-목록)
33. [알려진 한계](#알려진-한계)

---

## 무엇을 하는 앱인가

열일곱 개의 블루프린트로 이루어져 있습니다.

| 블루프린트 | url_prefix | 하는 일 |
|---|---|---|
| `main` | 없음 | 인덱스 페이지 |
| `auth` | `/auth` | 로그인 / 로그아웃 (세션 기반, 계정은 `users` 테이블) |
| `agent` | `/agent` | Claude 기반 채팅 에이전트 (도구 호출) |
| `alarm` | `/alarm` | 이벤트 접수 → Lambda로 정규화 → 알람 |
| `admin` | `/admin` | 설정·라우트 확인 (관리자 계정만 접근) |
| `dashboard` | `/dashboard` | PostgreSQL 집계 지표와 차트 |
| `explore` | `/explore` | 질의어로 이벤트 조회 (PromQL 스타일) |
| `resources` | `/resources` | AWS 리소스 스냅샷 비교 (무엇이 바뀌었나) |
| `report` | `/report` | 기간별 운영 리포트 + Markdown/PowerPoint 내보내기 |
| `console` | `/console` | 고객사 계정 선택 후 AWS CLI 읽기 전용 실행 (관리자 전용) |
| `work` | `/work` | 작업 전후 스냅샷 비교로 변경 증적 남기기 |
| `runbook` | `/runbook` | 알람 종류(지문)별 대응 절차 |
| `handover` | `/handover` | 당직 인계 (지난 근무 구간 요약 + 미해결 작업) |
| `incident` | `/incident` | 장애 사후 보고서 (타임라인 자동 조립 + RCA 문서) |
| `noise` | `/noise` | 시끄러운 알람 순위와 지문별 억제 규칙 |
| `customer` | `/customer` | 고객사 하나의 현황 (유일한 '고객사 축' 화면) |
| `compliance` | `/compliance` | 스냅샷 기준 모범사례 점검 (Config 미사용) |

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
│   └── schema.sql              events / resources / accounts / work_orders
│                               / runbooks / incidents / alarm_rules
│                               / audit_log / sla_targets / oncall_members
│                               / escalations / incident_fingerprints DDL
│
├── app/                        ─── Flask 애플리케이션 ───
│   ├── __init__.py             create_app() 팩토리 + 블루프린트 등록
│   ├── config.py               환경별 설정 (개발/테스트/운영)
│   ├── cli.py                  flask init-db / db-check 명령
│   ├── agent_core.py           AI 에이전트의 도구 정의와 실행 루프
│   ├── event_store.py          이벤트 조회 (events 테이블)
│   ├── users.py                계정과 역할
│   ├── nav.py                  왼쪽 메뉴와 카테고리
│   ├── compliance.py           스냅샷 기준 모범사례 점검
│   ├── compliance_xlsx.py      점검 결과를 보고용 엑셀로
│   ├── readiness.py            고객사 온보딩 준비도
│   ├── msr.py                  월간 서비스 리뷰 자료
│   ├── msr_pptx.py             월간 리뷰를 슬라이드로
│   ├── stats.py                대시보드용 집계 질의 (SQL)
│   ├── query.py                이벤트 질의어 파서 + SQL 컴파일러
│   ├── resources.py            리소스 스냅샷 저장 / 정규화 / diff
│   ├── collect.py              리소스 수집 (데모 / 실제 AWS)
│   ├── work.py                 작업 기록 (상태 전이)
│   ├── evidence.py             작업 증적 문서 생성
│   ├── runbook.py              알람 대응 절차 (지문 기준)
│   ├── handover.py             당직 인계 집계 + Markdown
│   ├── incident.py             장애 기록 + 타임라인 조립
│   ├── rca.py                  사후 보고서 문서 생성
│   ├── noise.py                알람 노이즈 집계 + 억제 규칙
│   ├── customer.py             고객사 현황 집계
│   ├── audit.py                감사 로그 (고객사 계정을 건드린 기록)
│   ├── sla.py                  최초 대응 시간 목표와 집계
│   ├── slack.py                Slack Incoming Webhook 전송
│   ├── jira.py                 Jira 이슈 생성 (에스컬레이션 넘기기)
│   ├── escalation.py           단계 판정 · 담당자 · 호출 기록
│   ├── report.py               기간 리포트 집계 / Markdown / AI 요약
│   ├── report_pptx.py          리포트를 PowerPoint 슬라이드로
│   ├── accounts.py             고객사 AWS 계정 목록
│   ├── aws_session.py          AssumeRole + 임시 자격증명 캐싱
│   ├── awscli.py               AWS CLI 허용 목록 + 실행
│   ├── lambda_client.py        Lambda 호출 계층 (local / aws 전환)
│   ├── views/                  블루프린트별 라우트
│   │   ├── main.py  auth.py  agent.py  alarm.py  admin.py
│   │       dashboard.py  explore.py  resources.py  report.py  console.py
│   │       work.py  runbook.py  handover.py  incident.py
│   │       noise.py  customer.py
│   ├── templates/              Jinja 템플릿
│   │   └── base.html  index.html  login.html  agent.html  alarm.html
│   │       admin.html  dashboard.html  explore.html  resources.html
│   │       report.html  console.html  work.html  work_detail.html
│   │       runbook.html  runbook_edit.html  handover.html  admin_audit.html
│   │       report_sla.html
│   │       incident.html  incident_detail.html  noise.html  customer.html
│   └── static/                 정적 파일 (Flask 가 /static/... 으로 자동 공개)
│       └── css/style.css       전체 스타일. base.html 에서 link 로 연결
│
├── api/                        ─── AWS Lambda 함수 ───
│   └── normalize_handler.py    이벤트 정규화 → DB 적재 → 알람 발송(+억제)
│
└── tests/                      pytest. DB 없이 도는 것이 대부분
    └── test_normalize.py  test_awscli.py  test_query.py
        test_resources.py  test_app.py  test_suppression.py
        test_noise_customer.py  test_account_audit.py
        test_sla_prune.py  test_slack.py  test_escalation.py
        test_incident_archive.py
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
| `psycopg[binary]` | PostgreSQL 접속 | DB 기능만 못 씀 |
| `python-pptx` | 리포트 PowerPoint 내보내기 | pptx 내보내기만 못 씀 |

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

계정이 하나도 없으면 **부트스트랩 계정**으로 들어갈 수 있습니다.

| 항목 | 값 |
|---|---|
| 아이디 | `admin` |
| 비밀번호 | `1234` |

이 계정은 코드에 그대로 박혀 있습니다(`app/users.py`). DB 없이 앱을 처음 켠
사람이 로그인조차 못 하면 아무것도 못 해보기 때문에 남겨둔 통로입니다.
대신 이 상태로 들어와 있으면 화면 위에 계속 경고가 뜨고,
**계정을 하나라도 만드는 순간 이 통로는 닫힙니다.**

```bash
flask --app run add-user 내이름 --role admin    # 비밀번호는 물어봅니다
flask --app run list-users
flask --app run passwd 내이름
```

비밀번호를 `--password` 로 직접 주지 않고 물어보게 한 이유는, 명령줄에 적으면
셸 기록(`~/.bash_history`)과 프로세스 목록(`ps`)에 그대로 남기 때문입니다.

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
| `/alarm/api/events` | POST | **API 키** | JSON 수집 엔드포인트 |
| `/dashboard/` | GET | 필요 | 지표 대시보드 (`?hours=6\|24\|72`) |
| `/explore/` | GET | 필요 | 질의어로 이벤트 조회 (`?q=`, `?hours=`) |
| `/resources/` | GET | 필요 | 리소스 스냅샷 비교 (`?base=`, `?target=`) |
| `/report/` | GET | 필요 | 운영 리포트 (`?days=1\|7\|30`, `?summary=1`) |
| `/report/download` | GET | 필요 | 리포트를 Markdown 파일로 |
| `/report/download.pptx` | GET | 필요 | 리포트를 PowerPoint 파일로 |
| `/console/` | GET, POST | **관리자** | 계정 선택 후 AWS CLI 실행 |
| `/admin/` | GET | 관리자 | 설정·라우트 확인 |
| `/admin/events/clear` | POST | 관리자 | 이벤트 비우기 |
| `/admin/audit` | GET | 관리자 | 감사 로그 |
| `/admin/users` | GET | 관리자 | 계정 목록 |
| `/admin/users/create` | POST | 관리자 | 계정 만들기 |
| `/admin/users/password` | POST | 관리자 | 비밀번호 바꾸기 |
| `/admin/users/enabled` | POST | 관리자 | 계정 켜기/끄기 |
| `/compliance/` | GET | 필요 | 스냅샷 기준 모범사례 점검 (`?account=`, `?region=`) |
| `/compliance/exceptions` | GET | 필요 | 승인된 예외 목록 |
| `/compliance/exceptions/add` | POST | 필요 | 예외 등록 (사유·만료일 필수) |
| `/compliance/download.xlsx` | GET | 필요 | 보고용 엑셀 (`?account=` 없으면 전체) |
| `/report/msr` | GET | 필요 | 월간 서비스 리뷰 (`?customer=`, `?year=`, `?month=`) |
| `/report/msr/download.pptx` | GET | 필요 | 월간 리뷰를 PowerPoint 로 |
| `/customer/readiness` | GET | 필요 | 온보딩 준비도 (`?name=`) |

권한은 계정의 **역할**로 정합니다. 예전에는 `ADMIN_USERS` 환경변수에 적은
이름 목록과 비교했는데, 계정과 권한이 서로 다른 곳에 저장되어 있어서
계정을 지워도 권한이 남았습니다.

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

## 리소스 변경 추적

인프라 상태를 주기적으로 스냅샷으로 찍고, 스냅샷끼리 비교해 **무엇이 생기고 사라지고
바뀌었는지**를 보여줍니다. AWS Config 가 하는 일의 축소판입니다.

```bash
flask --app run collect-resources --demo          # AWS 없이 합성 리소스로
flask --app run collect-resources --demo --drift 1.0   # 두 번째 실행 (변경 발생)
```

`/resources/` 에서 결과를 봅니다.

```
[생성  ] i-0new5326    ec2:instance
[변경  ] app-logs      s3:bucket
         public_access_blocked   true → false
[변경  ] sg-web        ec2:security_group
         ingress  [80, 443] → [80, 443, 22]
[삭제  ] i-0e4f5a6b    ec2:instance
```

실제 AWS 에서 수집하려면 `--demo` 를 빼고 실행합니다. 읽기 전용 호출만 하므로
`ReadOnlyAccess` 수준의 권한이면 충분합니다.

### 되돌릴 수 있는 것과 없는 것

스냅샷은 **"무엇이 어떻게 바뀌었나"에 대한 기록이자 되돌릴 목표값**입니다.
실제로 되돌리는 것은 별도의 쓰기 API 호출이며, 이 프로젝트는 그 일을 하지 않습니다.

| 부류 | 예시 | 스냅샷만으로 복원 |
|---|---|---|
| 설정 변경 | SG 규칙, S3 공개 설정, IAM 정책, 태그 | **가능** — 이전 값을 다시 쓰면 됨 |
| 수명주기 | 종료된 EC2, 삭제된 RDS | **불가** — 새로 만들어야 함 |
| 소실성 | 릴리스된 EIP, 삭제된 로그 | **불가** |

### 두 가지 안전장치

**1. 노이즈 정규화** — `LastModified`, 요청 토큰, 리스트 순서처럼 매번 달라지지만
의미 없는 값을 해시 계산 전에 걷어냅니다. 이걸 빼먹으면 아무것도 안 바뀐 날에도
전부 "변경됨"으로 떠서 diff 가 쓸모없어집니다. 스냅샷 비교가 실무에서 실패하는
가장 흔한 이유입니다.

**2. `complete` 플래그** — 수집이 도중에 실패하면 못 읽은 리소스가 "삭제됨"으로
보입니다. 완료 표시가 없는 스냅샷은 비교 대상에서 자동으로 빠집니다.

---

## 운영 리포트

`/report/` 에서 기간별 리포트를 보고 Markdown 으로 내려받을 수 있습니다.

```
/report/?days=7                 화면
/report/download?days=7         Markdown 파일
/report/download.pptx?days=7    PowerPoint 파일 (6장)
/report/?days=7&summary=1       AI 요약 포함 (API 키 필요)
```

PowerPoint 는 표지 · 요약 · 심각도 분포 · 출처별 비교 · 인프라 변경 ·
읽을 때 주의할 점 순서로 만들어집니다. 차트는 이미지가 아니라 PowerPoint
네이티브 차트라서 파일을 열어 직접 편집할 수 있고, 심각도 색은 화면과 같은 값을 씁니다.

마지막 "읽을 때 주의할 점" 슬라이드를 넣은 이유는, 발표 자리에서 순위가 사실처럼
굳어지는 것을 막기 위해서입니다. 표본이 적으면 그 경고가 맨 앞에 붙습니다.

### 설계에서 신경 쓴 것

**절대 건수보다 변화를 앞에 둡니다.** 모든 지표에 직전 같은 길이의 기간을 함께 뽑아
증감률을 붙입니다. "web-02 가 11건으로 1위" 보다 "web-02 가 5건 → 11건(+120%)" 이
훨씬 신뢰할 만한 신호이기 때문입니다.

**표본이 적으면 경고합니다.** 건수가 적으면 아무 문제 없는 인프라에서도 그럴듯한
1등이 만들어집니다. 30건 미만이면 "순위는 우연일 수 있다" 는 배너가 화면과
Markdown 양쪽에 붙습니다.

**조용해진 출처를 따로 보여줍니다.** 직전 기간엔 이벤트가 있었는데 이번엔 0건인
출처입니다. 정말 안정된 것일 수도, **수집이 끊긴 것일 수도** 있습니다.
조용한 것과 건강한 것은 다릅니다.

**AI 요약은 선택입니다.** 키가 없으면 버튼이 나타나지 않고, 집계는 그대로 전부 나옵니다.
요약 프롬프트에는 "알람이 몰린 곳을 원인으로 단정하지 말 것", "표본이 적으면 그렇게
밝힐 것" 같은 제약을 넣어 뒀습니다.

---

## AWS 콘솔 (다중 계정)

`/console/` 에서 **고객사 → 계정 → 리전**을 고르고 AWS CLI 읽기 전용 명령을 실행합니다.
관리자만 접근할 수 있습니다.

```bash
# 계정 등록 (role-arn 을 비우면 데모 계정 - 실제 AWS 를 부르지 않음)
flask --app run add-account --customer "A커머스" --account-id 123456789012 \
      --alias prod --regions "ap-northeast-2,us-east-1"

flask --app run add-account --customer "B물류" --account-id 999988887777 \
      --role-arn "arn:aws:iam::999988887777:role/ViewOnly" \
      --external-id "..." --regions "ap-northeast-2"

flask --app run list-accounts
```

### 동작

```
고객사 [A커머스 ▾]  계정 [prod ▾]  리전 [ap-northeast-2 ▾]
> aws ec2 describe-instances

  ① 그 계정의 role_arn 으로 sts:AssumeRole
  ② 임시 자격증명 획득 (캐시, 만료 5분 전 갱신)
  ③ 자격증명을 환경변수로 넣어 셸 없이 실행
  ④ 출력 표시 + 감사 로그 기록
```

자격증명은 **파일에 쓰지 않고** 해당 프로세스의 환경변수로만 넘깁니다.
계정을 바꾸면 자격증명도 통째로 바뀌므로 계정 간 섞일 여지가 없습니다.

### 두 겹으로 막습니다

**1. 셸을 쓰지 않습니다 (`shell=False`)** — 이게 실제 방어선입니다.
`;` `|` `&&` `$( )` 가 특별한 뜻을 잃고 그냥 문자열이 됩니다. 셸이 없으니 셸 주입도 없습니다.

**2. 허용 목록** — 오류 메시지를 분명히 하기 위한 두 번째 겹입니다.

| 검사 | 거부 대상 |
|---|---|
| 실행 파일 | `aws` 외 전부 |
| 하위 명령 | `describe` `list` `get` `search` `lookup` `batch-get` 로 시작하지 않는 것 |
| 옵션 | `--region` `--profile` `--endpoint-url` 등 (리전·자격증명은 화면 선택으로만) |
| 셸 문법 | `;` `\|` `&&` `$( )` `` ` `` `>` `<` |

서버 환경변수를 자식 프로세스에 통째로 물려주지 않습니다.
`.env` 의 `ANTHROPIC_API_KEY` 나 DB 비밀번호가 노출될 이유가 없기 때문입니다.
`HOME` 도 `/tmp` 로 바꿉니다. 그대로 두면 CLI 가 서버의 `~/.aws` 를 읽어서,
화면에서 고른 계정이 아니라 **서버 자신의 자격증명으로 명령이 나갈 수** 있습니다.

넘겨주는 것은 딱 두 가지입니다.

| 넘겨주는 것 | 왜 |
|---|---|
| AssumeRole 로 받은 임시 자격증명 | 이게 없으면 아무것도 안 됩니다 |
| 프록시 · CA 설정 (`HTTPS_PROXY`, `AWS_CA_BUNDLE` 등) | 비밀이 아니고, 없으면 밖으로 나가지 못합니다 |

두 번째는 처음에 빼먹었다가 넣었습니다. 사내 프록시가 자체 CA 로 TLS 를
다시 맺는 망에서는 CLI 가 `certificate verify failed` 로 죽습니다.
**검증을 끄는 선택지(`--no-verify-ssl`)는 두지 않았습니다** &mdash;
그건 고객사 계정으로 가는 연결을 아무나 가로챌 수 있게 만드는 것입니다.

> 콘솔을 쓰려면 서버에 **AWS CLI 가 설치되어 있어야** 합니다.
> 없으면 `exec_failed` 로 기록되고 화면에 안내가 뜹니다.

### 감사 로그

명령 실행은 이벤트로 남습니다. 결과를 세 가지로 구분합니다.

| outcome | 뜻 |
|---|---|
| `ok` / `failed` | 실행됨 (종료코드에 따라) |
| `rejected` | **금지된 명령을 시도함** — 감사에서 눈여겨봐야 할 기록 |
| `exec_failed` | 허용됐으나 실행 환경 문제 (AWS CLI 미설치, 시간 초과) |

`rejected` 와 `exec_failed` 를 같은 값으로 남기면 감사 로그에서 위험 신호를
골라낼 수 없어서 분리했습니다.

---

## 알람 AI 진단

알람 하나를 열고 계정을 지정하면, 그 계정에서 읽기 전용 조회를 몇 번 돌려
짧은 진단을 돌려줍니다. `/alarm/` 의 각 이벤트 카드에 있는 **AI 진단** 버튼입니다.

```
알람 카드 → [고객사/계정] [리전] [AI 진단]
              ↓
   이벤트 내용 + 발생 이력 + 그 계정 전용 조회 도구
              ↓
   ## 추정 원인 / ## 확인한 것 / ## 다음에 확인할 것
```

### 대화가 아니라 요청 한 번입니다

에이전트 채팅(`/agent/`)과 달리 진단은 **기록을 남기지 않습니다.**
진단이 끝나면 그 대화는 사라집니다. 고객사 A의 조회 결과가 다음 진단에
남아 고객사 B의 답변에 섞이는 일을 구조적으로 막기 위해서입니다.

### 계정은 모델이 고르지 않습니다

조회 도구(`aws_read`)가 받는 인자는 `command` 하나뿐입니다.
계정과 리전은 담당자가 화면에서 고른 값이 요청 컨텍스트(`flask.g`)에 실려
전달됩니다. 계정을 도구 인자로 두면 모델이 다른 고객사의 계정 번호를 지어내
조회할 수 있기 때문입니다. 리전도 그 계정에 등록된 것으로만 교정됩니다.

실행 계층은 [AWS 콘솔](#aws-콘솔-다중-계정)과 **완전히 같은 코드**입니다.
사람이 친 명령이든 모델이 만든 명령이든 같은 허용 목록을 통과해야 하므로,
AI를 붙였다고 해서 새로운 신뢰 경계가 생기지 않습니다.

### 거부는 예외가 아니라 답변으로 돌아갑니다

모델이 `aws ec2 reboot-instances` 를 시도하면 도구가 예외를 던지지 않고
`[거부됨] 읽기 전용 명령만 실행할 수 있습니다...` 라는 **문자열**을 돌려줍니다.
예외로 던지면 루프가 죽고 모델은 이유를 알 수 없지만, 문자열로 주면
허용되는 형태로 스스로 고쳐서 다시 시도합니다.
무한히 변형을 시도하는 것은 `DIAG_MAX_ITERATIONS`(12회)가 끊습니다.

### 출력 상한이 콘솔과 다릅니다

| | 상한 | 이유 |
|---|---|---|
| 콘솔(사람) | 200,000자 | 긴 JSON은 스크롤해서 보면 됨 |
| 진단(모델) | 12,000자 | 그대로 컨텍스트를 채우고 비용이 됨 |

잘리면 "`--query` 나 `--max-items` 로 좁혀서 다시 조회하라"는 안내가 함께
전달되어, 모델이 범위를 줄여 다시 부릅니다.

### 발생 이력이 함께 전달됩니다

같은 지문의 이벤트가 전체 몇 건인지, 최근 168시간에 몇 건인지,
최근 값이 어떻게 변해왔는지가 프롬프트에 들어갑니다.
같은 알람이라도 **처음 발생인지 사흘째 반복인지에 따라 봐야 할 곳이 달라지기**
때문입니다. DB를 조회할 수 없으면 "집계 불가"로 표시하고 진단은 그대로 진행합니다.

### 감사 로그

진단 한 건이 이벤트 하나로 남습니다(명령 하나하나가 아니라).
`meta.actor` 가 `agent` 로 기록되어, 콘솔에서 사람이 직접 친 명령과 구분됩니다.
이 구분이 없으면 나중에 "모델이 무엇을 조회했나"를 분리해낼 수 없습니다.

---

## 작업 기록 (변경 증적)

고객 요청으로 작업할 때 **"요청한 것만 바뀌었다"** 를 증명하기 위한 기록입니다.
`/resources/` 화면이 "무엇이 바뀌었나"를 보여준다면, 여기는 거기에
**누가, 왜, 어떤 요청으로** 를 붙여 감사 자료로 만듭니다.

### 흐름

```
1. 작업 기록 만들기      티켓 번호 / 고객 요청 원문 / 예상한 변경 / 계정·리전
2. 작업 전 스냅샷  [찍기]   ← 리소스를 한 벌 수집해 저장
3. 실제 작업              ← 이 앱 밖에서 (콘솔, CLI, IaC 무엇이든)
4. 작업 후 스냅샷  [찍기]
5. 차이 확인 → 증적 확정 → Markdown 내려받기
```

각 단계는 앞 단계가 끝나야 넘어갑니다. 작업 전 스냅샷 없이 작업 후를 찍거나,
확정한 뒤에 스냅샷을 바꾸는 것은 거부됩니다.

### 상태 전이를 DB 에서 판정합니다

```sql
UPDATE work_orders SET before_snapshot_id = %s, status = 'before_taken'
 WHERE id = %s AND status = 'open'
```

파이썬에서 "읽고 → 판단하고 → 쓰면" 두 사람이 동시에 눌렀을 때 둘 다 통과할 수
있습니다. 확인과 쓰기 사이가 벌어지기 때문입니다. `WHERE` 절에 현재 상태를
넣으면 DB 가 한 번만 통과시킵니다.

다만 **수집은 그 전에 한 번 더 검사합니다.** 리소스 수집은 고객사 계정에 실제
조회를 날리는 일이라, 어차피 거부될 요청에 그 비용을 치를 이유가 없습니다.
순서가 반대면 거부된 요청마다 쓸모없는 스냅샷이 하나씩 남습니다.

### 데모 계정은 변화 없이 수집합니다

`role_arn` 이 비어 있는 데모 계정은 합성 리소스를 만들어 씁니다. 이때
**변화율(drift)을 0으로 두고 이전 스냅샷을 그대로 복사**합니다.
합성 데이터가 저 혼자 바뀌면 "작업 때문에 바뀐 것"과 구분할 수 없어
증적이 통째로 의미를 잃기 때문입니다.

(`collect-resources --demo` 는 반대로 기본 15% 확률로 흔듭니다. 그쪽은
diff 화면이 어떻게 보이는지 확인하는 게 목적이라 변화가 있어야 합니다.)

### 증적 문서

`/work/<번호>/evidence.md` 로 내려받습니다. 들어가는 것:

- 티켓 번호, 고객사, 계정/리전, 작업자, 생성·확정 시각
- 고객 요청 원문과 예상한 변경 (입력한 그대로)
- **결론** — 변경 건수
- 근거 스냅샷 두 개의 번호와 수집 시각, 리소스 수
- 변경 목록 (리소스별 속성 전후 비교)

결론에서 앱은 **"정상"이라고 판정하지 않습니다.** 요청은 자연어이고 변경은
리소스 속성이라 자동으로 맞대볼 수단이 없습니다. 숫자만 제시하고 판단은
읽는 사람에게 남깁니다. 여기서 임의로 "요청대로 처리됨"이라고 찍으면
그게 곧 잘못된 증적이 됩니다.

실제로 이 기능이 잡아내는 것은 이런 경우입니다.

```
요청: sg-web 에 443/tcp:203.0.113.10/32 한 줄 추가

변경 목록 (2건)
  [변경] sg-web       ingress   ... → ..., 443/tcp:203.0.113.10/32   ← 요청한 것
  [변경] i-0e4f5a6b   instance_type   t3.small → t3.large            ← 요청에 없던 것
```

---

## 런북

알람 종류별 대응 절차입니다. **지문(fingerprint)에 절차를 붙입니다.**

지문이 제 역할을 해야만 성립하는 기능입니다. 지문이 메시지 전문을 해시하던
시절에는 같은 알람이 매번 다른 지문이 되어, 절차를 붙일 대상 자체가
없었습니다. 그래서 지문 수정이 이 기능의 선행 조건이었습니다.

### 쓰는 흐름

```
/alarm/  알람 카드
   ├ 절차가 있으면  →  [▾ 대응 절차: EC2 CPU 임계값 초과 (공통)]  펼쳐 보기
   └ 절차가 없으면  →  [이 알람에 절차 쓰기]  (지문이 자동으로 채워짐)
```

지문은 16자리 해시라 사람이 손으로 옮겨 적을 것이 아닙니다. 알람 카드의
링크가 쿼리스트링으로 넘겨줍니다.

### 공통 절차와 고객사 전용 절차

같은 지문에 둘 다 있으면 **전용이 이깁니다.**

```
지문 99be11fd  ├ 공통      "EC2 CPU 임계값 초과"
               └ A커머스 전용 "[A커머스] 프로모션 일정 먼저 확인"

A커머스 계정 진단 → 전용 절차
B물류 계정 진단   → 공통 절차로 떨어짐
```

목록 화면(`find_many`)과 진단(`find`)은 **고르는 기준이 다릅니다.**
진단은 "이 고객사 계정에서 무엇을 따를까"라 전용이 이깁니다.
목록은 "이 알람에 절차가 있기는 한가"를 보는 것이라 고객사를 가리지 않습니다.
목록에서 고객사를 걸러버리면, B물류 전용 절차만 써둔 알람이 "절차 없음"으로
보여서 이미 쓴 절차를 또 쓰게 됩니다.

### AI 진단과 붙습니다

절차가 있으면 진단 프롬프트에 그대로 들어갑니다.

```
등록된 대응 절차 (A커머스 전용) — [A커머스] EC2 CPU 임계값 초과
1. 프로모션 일정 먼저 확인 (마케팅 슬랙)
2. 프로모션이면 조치 없이 관찰
3. 아니면 공통 절차 진행
```

이게 없으면 모델은 일반적인 AWS 지식으로만 답합니다. 절차가 있으면
**"이 조직이 이 알람에 실제로 하는 것"** 을 답합니다. 진단이 일반론에
그치던 이유가 도구 부족이 아니라 조직 맥락 부재였습니다.

---

## 당직 인계

다음 당직자에게 넘길 내용을 한 장으로 모읍니다. `/handover/` 입니다.

리포트(`/report/`)와 목적이 다릅니다.

| | 리포트 | 인계 |
|---|---|---|
| 받는 사람 | 고객사 | 다음 당직자 |
| 주기 | 월 단위 | 근무 구간 (8/12/24시간) |
| 핵심 | 기간 대비 변화 | **지금 뭐가 열려 있나** |

그래서 집계 방식도 다릅니다. 인계는 **처음 나타난 알람**과 **미해결 작업**을
앞세웁니다.

### 담기는 것

- **이번 근무에 처음 나타난 알람** — 전체 이력의 첫 발생이 이 구간 안에 있는 지문.
  반복되던 알람보다 이쪽이 중요합니다. 새로 생긴 문제이기 때문입니다.
- **많이 난 알람** — 지문 기준. 메시지로 묶으면 측정값이 달라서 전부 1건씩 나옵니다.
- **진행 중인 작업** — 확정되지 않은 작업 기록. 인계의 핵심입니다.
- **대응 절차가 없는 알람** — 이번 구간의 critical/error 중 런북이 없는 것.
  인계받는 사람이 판단 근거 없이 마주치게 되는 항목입니다. 바로 절차를 쓰러 갈 수 있습니다.

Markdown 으로 내려받으면 메신저에 그대로 붙여넣을 수 있습니다.

### 묶음의 심각도는 '가장 나쁜 것'

`max(severity)` 를 쓰면 안 됩니다. 문자열 비교라 알파벳 순으로 가장 큰 값이
이깁니다.

```
critical < error < info < warning     ← 알파벳 순
```

critical 과 warning 이 섞인 묶음이 `warning` 으로 보고됩니다. 실제로 이 앱의
샘플 데이터에서 10개 묶음 중 2개가 이렇게 잘못 표시되고 있었습니다.
심각도 순서를 `CASE` 로 명시해 가장 나쁜 것을 고릅니다.

대표 메시지도 `min(message)`(알파벳 순 아무거나) 대신, 가장 최근 메시지를
씁니다. "처음 나타난 알람"에서는 반대로 첫 메시지를 씁니다.

---

## 장애 사후 보고서 (RCA)

장애 구간을 지정하면 **그 시간대의 알람·리소스 변경·작업을 한 시간축에 세워**줍니다.
`/incident/` 입니다.

### 이 앱에서 세 데이터가 처음 만나는 곳입니다

지금까지 이 앱의 데이터는 서로를 몰랐습니다.

```
events             무슨 일이 있었나    →  /alarm/,     /explore/
resource_snapshots 무엇이 바뀌었나    →  /resources/
work_orders        누가 무엇을 했나   →  /work/
```

셋 다 시각이 찍혀 있는데 한 화면에서 겹쳐본 적이 없었습니다. 장애를 조사할 때
제일 먼저 묻는 **"장애 직전에 뭐가 바뀌었나"** 에 답하려면 이 셋을 한 시간축에
세워야 합니다.

```
15:43:16  작업   작업 시작: 결제 DB 보안그룹 규칙 추가   #3 · OPS-1600
15:58:16  알람   결제 API 응답 지연 2100ms 감지
16:02:16  알람   결제 API 응답 지연 2230ms 감지
16:10:16  알람   결제 API 응답 지연 2490ms 감지
16:26:16  알람   결제 API 응답 지연 3010ms 감지
16:33:16  변경   리소스 변경 1건 — sg-pay ingress 에 5432/tcp:0.0.0.0/0 추가
```

### 앱이 채우는 칸과 사람이 쓰는 칸을 나눕니다

| 앱이 모음 | 사람이 씀 |
|---|---|
| 타임라인 | 영향 |
| 알람 요약 (지문 기준) | **원인** |
| 리소스 변경 (스냅샷 diff) | **조치** |
| 이 구간의 작업 | 재발 방지 |

이 경계가 이 기능의 핵심입니다. 타임라인은 데이터에서 나오지만 원인과 조치는
엔지니어 머릿속에만 있습니다. **앱이 원인을 지어내면 그건 잘못된 사후 보고서가
되고, 없느니만 못합니다.** 화면과 문서 양쪽에서 어느 쪽이 어느 쪽인지 표시합니다.

원인과 조치가 비어 있으면 제출되지 않습니다. 그 두 칸이 사후 보고서의 본체이고,
비어 있는 채로 나가면 타임라인만 붙인 문서가 됩니다.
제출한 뒤에는 고칠 수 없습니다 — 고객사에 낸 문서가 조용히 바뀌면 안 됩니다.

### 범위를 두 겹으로 좁힙니다

```
1) 계정   이 장애가 난 계정의 이벤트만        (자동)
2) 출처   그 안에서도 관련 서비스만            (사람이 지정)
```

계정 조건만으로도 크게 줄어듭니다.

```
범위 없음   : 알람 35건 / 18종
계정만      : 알람 16건 /  7종   ← 사람이 아무것도 안 해도 여기까지
계정 + 출처 : 알람 14건 /  5종
```

**계정을 지정했는데 그 계정 이벤트가 하나도 없으면 계정 조건을 풉니다.**
계정을 실어 보내지 않는 환경일 수 있는데, 거기서 빈 타임라인을 내면
사람은 "장애 때 아무 일도 없었다"로 읽습니다. 화면에는 계정으로 좁히지
못했다고 표시합니다.

작업 기록은 `work_orders.account_id` 로 항상 걸러냅니다.

### 스냅샷의 한계를 문서에 적습니다

리소스 변경은 장애 구간을 감싸는 스냅샷 두 개를 비교해서 냅니다. 그래서
**정확한 변경 시각을 모릅니다** — "두 수집 시점 사이"까지만 알 수 있습니다.
수집 주기보다 장애 구간이 짧으면 아예 잡아내지 못합니다. 이 사실을 화면과
문서 양쪽에 적어둡니다. 타임라인에 시각이 찍혀 있으면 정확한 시각으로
읽히기 때문입니다.

### 내부 RCA 와 고객 제출본

두 문서는 독자가 다릅니다.

| | 내부 RCA | 고객 제출본 |
|---|---|---|
| 경과 | 알람 하나하나, 초 단위 (17줄) | 마일스톤 4~6줄 |
| 식별자 | `i-0abc123`, `sg-pay`, 스냅샷 #15 | 없음 |
| 사람 | 작업자 이름, 티켓 번호 | 없음 |
| 근거 표 | 타임라인·알람 요약·리소스 변경 전부 | **통째로 빠짐** |
| 내려받기 | `/incident/<번호>/report.md` | `/incident/<번호>/customer.md` |

**기록과 근거는 하나입니다.** 새 테이블이나 블루프린트를 만들지 않고 칸만
나눴습니다. 두 벌로 관리하면 나중에 어긋났을 때 어느 쪽이 맞는지 알 수
없습니다. 고객 제출본은 내부 기록에서 골라내고 다듬은 파생물이지 별개의
사실이 아닙니다.

**상태는 나눕니다.** 내부 확정과 고객 제출은 시점이 다릅니다 — 보통 내부에서
먼저 정리하고 며칠 뒤 나갑니다. 하나로 두면 고객 제출본을 손대는 순간
내부 RCA까지 잠기거나, 반대로 제출한 고객 문서가 조용히 바뀝니다.

### 초안 채우기

빈 칸에서 시작하지 않게 합니다. 기록에서 시각을 뽑아 경과 초안을 만들고,
내부 RCA 내용을 복사해 넣습니다.

```
[초안 채우기] →
15:30  모니터링 알람 감지
15:43  작업 수행: 결제 DB 보안그룹 규칙 추가
16:26  마지막 알람 발생
16:33  설정 변경 확인

(위는 기록에서 뽑은 시각입니다. 문장을 고치고,
 조사 착수·복구 확인 같은 항목을 직접 채워 주세요.)
```

앱이 뽑는 것은 **첫 알람 / 마지막 알람 / 작업 시각 / 변경 감지 시각** 넷입니다.
"원인 조사 착수" 같은 항목은 앱이 알 수 없으므로 사람이 써넣습니다.
초안에도 내부 식별자는 넣지 않습니다 — 그걸 빼는 게 이 문서를 따로 만드는
이유이기 때문입니다.

이미 작성된 칸은 덮어쓰지 않습니다. 초안 버튼을 잘못 눌러 써둔 문장이
날아가면 안 됩니다.

### 마이그레이션에 대해

이 프로젝트에는 마이그레이션 도구가 없습니다. `CREATE TABLE IF NOT EXISTS`는
테이블이 이미 있으면 아무 일도 하지 않으므로, 나중에 열을 추가해도 기존
설치에는 반영되지 않습니다.

그래서 `db/schema.sql` 끝에 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 블록을
두었습니다. `flask --app run init-db`를 다시 돌리면 따라잡습니다. 여러 번
실행해도 안전합니다.

---

## 알람 노이즈

시끄러운 알람을 지문으로 묶어 순위를 냅니다. `/noise/` 입니다.

**건수만으로는 부족합니다.** 500번 났어도 절차와 규칙이 있으면 관리되는
것이고, 20번 났는데 아무것도 없으면 그게 문제입니다. 그래서 같은 줄에
**런북 유무**와 **억제 규칙 유무**를 붙여서 봅니다.

```
11건  CPU usage 94.5% on i-0abc123
      critical · web-01 · 지문 99be11fd84e1f21d
      절차 있음  |  억제 규칙 없음
```

### 억제 규칙

지문별로 두 가지를 정합니다.

| | 뜻 |
|---|---|
| `window_minutes` | 이 시간 안에 같은 지문으로 이미 보냈으면 건너뜀 |
| `muted` | 아예 보내지 않음 (window 보다 우선) |

**둘 다 비어 있는 규칙은 만들지 못합니다.** 목록만 늘리고 "규칙이 걸려
있다"는 착각을 주기 때문입니다.

`note` 는 선택이 아니라 사실상 필수입니다 — 나중에 이 규칙을 푸는 사람이
왜 걸었는지 알아야 합니다.

### 판정은 Lambda 에서 합니다

```
app/noise.py                    사람이 보고 규칙을 정하는 쪽
api/normalize_handler.py        실제 억제 판정 (알람을 보내는 쪽)
```

알람은 Lambda 가 보내므로 판정도 거기서 일어나야 합니다. 웹에서 판정하면
Lambda 를 직접 호출하는 경로에서는 억제가 걸리지 않습니다.

**DATABASE_URL 이 없으면 판정하지 않고 통과시킵니다.** 억제를 못 해서 알람이
더 가는 것보다, 판정에 실패했다고 알람을 막는 쪽이 훨씬 위험합니다.

### 상태 테이블을 규칙과 분리했습니다

```
alarm_rules   사람이 만든 규칙. 오래 남는다
alarm_state   마지막 발송 시각과 횟수. 알람마다 바뀌는 실행 상태
```

섞어두면 규칙을 지웠을 때 발송 이력까지 사라집니다.

---

## 고객사 현황

`/customer/` 입니다. **이 앱에서 유일한 '고객사 축' 화면입니다.**

나머지 블루프린트는 전부 기능 축입니다 — 알람은 알람끼리, 작업은 작업끼리.
그런데 MSP 는 고객사 단위로 일하므로 "A커머스 지금 어떤 상태야?"에 답하려면
화면을 여섯 개 돌아야 했습니다.

한 장에 모읍니다.

- **계정** — 데모/실계정, 리전, **마지막 리소스 수집 시각**
- **진행 중인 작업** — 확정되지 않은 작업 기록
- **장애** — 최근 기록과 고객 제출본 미완 경고
- **런북** — 이 고객사 전용 + 공통

마지막 수집 시각을 계정 옆에 붙인 이유가 있습니다. **그게 오래됐으면
리소스 변경도 사후 보고서의 근거도 전부 그만큼 낡은 상태**라는 뜻입니다.

### 낼 수 없는 것은 낼 수 없다고 적습니다

**고객사별 알람 건수는 나오지 않습니다.** `events` 에 계정이나 고객사
정보가 없기 때문입니다 — `source` 가 `pay-api` 같은 서비스 이름이라 고객사로
묶을 수단이 없습니다. RCA 타임라인에서 출처를 사람이 지정해야 했던 것과
같은 원인입니다.

화면에 그 사실과 이유를 적어둡니다. 임의로 전체 수치를 고객사 것처럼
보여주는 것이 제일 나쁩니다.

고치려면 이벤트를 보내는 쪽에서 계정 번호를 함께 넣으면 됩니다. 정규화가
표준 필드 외의 값을 `meta` 에 보존하므로 값은 이미 살아남습니다.

---

## 테스트

```bash
python -m pytest -q                # 전부
python -m pytest -q -m "not db"    # DB 없이 도는 것만
```

DB 가 없으면 DB 가 필요한 테스트는 **실패가 아니라 건너뜁니다.** 이 앱은
DB 없이도 뜨는 것을 전제로 하는데, DB 없는 개발자가 매번 빨간 화면을 볼
이유가 없습니다.

```
251 passed                       (PostgreSQL 있을 때)
198 passed, 53 skipped           (없을 때)
```

### 무엇을 덮었나

실제로 버그가 났던 자리를 우선으로 골랐습니다.

| 파일 | 덮는 것 |
|---|---|
| `test_normalize.py` | 필드 별칭, 심각도 매핑, **지문 묶임** |
| `test_awscli.py` | 허용 목록 — 쓰기 명령·다른 프로그램·셸 문법·금지 옵션 |
| `test_query.py` | 질의어 파싱과 **SQL 인젝션 방어** |
| `test_resources.py` | 리소스 정규화·다이제스트·필드 비교 |
| `test_app.py` | 팩토리, 인증 리다이렉트, **모든 GET 라우트** |
| `test_suppression.py` | 억제 판정 (DB 없을 때 통과시키는 것 포함) |
| `test_noise_customer.py` | 노이즈 집계, 심각도 정렬, 고객사 격리 |
| `test_account_audit.py` | 계정 귀속(별칭·형식 검사), 감사 로그 |
| `test_sla_prune.py` | SLA 목표 우선순위, 보존 정책의 보호 대상 |
| `test_slack.py` | 전송 계층 (가짜 웹훅 서버로 실제 POST 경로 검증) |
| `test_escalation.py` | 단계 판정, 담당자 우선순위, Jira 호출 |
| `test_incident_archive.py` | 무엇이 진단 재료가 되고 안 되는가 |

`test_app.py` 의 라우트 훑기는 `testing` 설정(= `sqlite://`)으로 돌기 때문에
**DB 가 전혀 없는 상태에서 모든 화면이 200 을 내는지**까지 함께 확인합니다.
이 프로젝트에서 500 에러가 두 번 났는데(pptx 표 좌표, RCA 커서 덮어쓰기)
둘 다 이 테스트 하나면 즉시 잡혔을 것들입니다.

### 표시가 붙은 테스트

```python
@pytest.mark.db      # PostgreSQL 필요. 없으면 건너뜀
```

---

## 이벤트의 계정 귀속

`events.account_id` 는 나중에 추가한 열입니다. 이게 없어서 **세 곳에서
타협해야 했습니다.**

| 어디서 | 어떻게 타협했나 |
|---|---|
| 사후 보고서 타임라인 | 무관한 알람이 섞여서 "관련 출처"를 사람이 치게 함 |
| 고객사 현황 | 알람 칸을 통째로 비움 |
| 알람 노이즈 | 고객사별로 나누지 못함 |

`source` 가 `pay-api` 같은 서비스 이름이라 계정으로 쓸 수 없었기 때문입니다.

### 받는 방법

이벤트를 보낼 때 계정 번호를 함께 넣으면 됩니다. 보내는 쪽마다 이름이
다르므로 별칭을 모아뒀습니다.

```
account_id · account · accountId · aws_account_id
awsAccountId          ← CloudWatch 알람이 SNS 로 보낼 때
recipientAccountId    ← CloudTrail
```

**12자리 숫자일 때만 표준 필드로 올립니다.** 형식이 다르면 계정으로 쓰지
않고 `meta.account_id_raw` 에 남깁니다 &mdash; 엉뚱한 값이 들어오면 이벤트가
없는 계정에 묶여 조용히 사라지고, 보낸 쪽은 뭘 잘못 줬는지 알 수 없습니다.

계정은 **지문에 영향을 주지 않습니다.** 지문은 "무슨 알람인가"이고 계정은
"어디서 났나"입니다. 섞으면 같은 알람이 계정마다 다른 종류가 됩니다.

### 예전 이벤트

```bash
flask --app run backfill-account-ids --dry-run   # 몇 건인지만
flask --app run backfill-account-ids             # meta 에서 끌어올림
```

열이 생기기 전에 들어온 이벤트는 계정이 `meta` 안에 문자열로만 남아 있습니다.
12자리 숫자면 표준 열로 옮깁니다.

---

## 로그인과 권한

계정은 `users` 테이블에, 비밀번호는 해시로만 들어갑니다. 오래 하드코딩되어
있던 `admin` / `1234` 를 옮긴 것입니다.

```python
generate_password_hash("비밀번호")
# 'scrypt:32768:8:1$8lYb...$e3c1...'   알고리즘$파라미터$솔트$해시
```

`werkzeug.security` 를 쓰는 이유는 단순합니다. Flask 가 이미 의존하는
패키지라 새로 설치할 것이 없고, 솔트와 반복 횟수를 알아서 처리합니다.

### 부트스트랩 — 두 요구가 부딪히는 자리

이 앱은 **DB 없이도 떠야 한다**는 전제로 만들어져 있습니다. DB 가 필요한
화면은 500 대신 안내를 보여주고, 테스트에도 그 규칙이 박혀 있습니다.
그런데 계정을 DB 로 옮기면 **DB 가 없을 때 로그인 자체가 불가능**해집니다.
처음 켠 사람이 아무것도 못 보게 됩니다.

타협은 이렇습니다.

| 상황 | 로그인 |
|---|---|
| `users` 테이블이 없거나 DB 가 안 뜸 | 부트스트랩 계정만 통함 |
| 테이블은 있는데 계정이 0개 | 부트스트랩 계정만 통함 |
| 계정이 하나라도 있음 | **부트스트랩 계정은 막힘.** DB 계정만 통함 |

열려 있는 동안에는 로그인 화면과 모든 화면 상단에 경고가 뜹니다.
"안전한 기본값" 은 아니지만, **조용히 열려 있지는 않습니다.**

### 역할

| 역할 | 볼 수 있는 것 |
|---|---|
| `admin` | 전부 (관리자 화면, 콘솔, 감사 로그, 계정) |
| `operator` | 운영 화면 (알람, 작업, 장애, 리포트 ...) |
| `viewer` | 읽기만. `POST` 는 전부 막힘 |

읽기 전용 차단은 블루프린트마다 걸지 않고 `create_app()` 의
`@app.before_request` 한 곳에서 합니다. `POST` 라우트가 열여섯 개
블루프린트에 흩어져 있어서, 각자 막게 하면 새 화면을 만들 때마다
빠뜨리게 됩니다.

역할은 로그인할 때 세션에 넣어둡니다. 화면마다 DB 를 다시 보지 않아도
되는 대신, **역할을 바꾸면 그 사람이 다시 로그인해야 반영됩니다.**

### 예전에는 이름 목록이었습니다

```python
# 예전
ADMIN_USERS = {"admin", "eunyul"}          # 환경변수
if session["username"] not in config["ADMIN_USERS"]: abort(403)

# 지금
if not users.can(session.get("role"), "admin"): abort(403)
```

계정과 권한이 서로 다른 곳(DB 와 환경변수)에 저장되어 있으면 둘이 어긋납니다.
계정을 지워도 이름 목록에는 남고, 이름이 같은 다른 사람을 만들면 권한이
따라옵니다. 권한은 계정에 붙어 있어야 합니다.

### 알람 수집 API

`/alarm/api/events` 는 브라우저 세션이 아니라 다른 서버가 부르는 곳이라
로그인 대신 API 키로 막습니다.

```
X-API-Key: <키>
Authorization: Bearer <키>
```

`INGEST_API_KEYS` 를 비워두면 열립니다. 이 앱이 "설정하지 않은 것은 오류가
아니다" 를 여기저기서 지키고 있어서 여기서도 그렇게 했지만,
**이 자리만은 대가가 다릅니다.**

```
익명 POST → critical 이벤트 적재 → SLA 위반 → 에스컬레이션 → Slack 호출 → Jira 티켓
```

아무나 당직자를 깨울 수 있다는 뜻입니다. 그래서 열려 있는 동안에는 관리자
화면에 경고를 띄우고 앱 로그에도 매 요청 남깁니다.

키 비교에는 `hmac.compare_digest` 를 씁니다. `==` 는 다른 글자가 나오면
바로 멈춰서 걸린 시간으로 키를 한 글자씩 맞춰볼 여지를 줍니다.

> 한 번 데인 곳: `compare_digest` 는 `str` 을 받으면 **ASCII 만** 허용합니다.
> 한글 비밀번호를 넣었더니 `TypeError` 로 500 이 났습니다. `bytes` 로 바꿔서
> 넘기면 어떤 문자든 그대로 비교됩니다.

### 세션

- `session.clear()` 를 **로그인 성공 직후**에 부릅니다. 로그인 전에 심어둔
  쿠키를 그대로 이어받는 세션 고정(session fixation)을 막습니다.
- `session.permanent = True` 를 켜야 `PERMANENT_SESSION_LIFETIME`
  (기본 12시간, `SESSION_HOURS`)이 적용됩니다. 켜지 않으면 브라우저를
  닫을 때까지 무제한입니다.
- 로그인 실패 메시지는 "아이디가 없다" 와 "비밀번호가 틀렸다" 를 구분하지
  않습니다. 구분해 주면 어떤 아이디가 존재하는지 알려주는 셈입니다.

### 계정은 지우지 않고 끕니다

`enabled` 를 `false` 로 바꿉니다. 지워버리면 감사 로그에 남은 이름이
누구였는지 나중에 확인할 수 없습니다. 마지막 관리자는 끌 수 없습니다
&mdash; 끄면 아무도 관리자 화면에 들어가지 못합니다.

---

## 감사 로그

`/admin/audit` &mdash; 관리자만 봅니다. **고객사 계정을 건드린 기록**입니다.

콘솔 명령과 AI 진단이 여기 쌓입니다. `actor_kind` 로 사람과 모델을
구분합니다. 이 구분이 없으면 나중에 "모델이 무엇을 조회했나"를 분리해낼
수 없습니다.

| outcome | 뜻 |
|---|---|
| `ok` / `failed` | 실행됨 (종료코드에 따라) |
| `rejected` | **금지된 명령을 시도함** &mdash; 감사에서 눈여겨봐야 할 기록 |
| `exec_failed` | 허용됐으나 실행 환경 문제 |
| `no_credentials` | 자격증명을 얻지 못함 |

### 처음에는 메모리에 넣었습니다 — 잘못이었습니다

```python
# 예전
event_store.add(...)          # deque(maxlen=100)
```

재시작하면 사라지고 101건째부터 앞이 밀려납니다. **감사 로그의 요건은
"지워지지 않는 것"인데 정반대였습니다.** `rejected` 와 `exec_failed` 를
굳이 구분해놓고 정작 그 기록이 휘발성이라는 걸 놓쳤습니다.

`events` 와 섞지 않고 따로 둡니다. `events` 는 "고객 인프라에서 일어난 일",
감사 로그는 "우리가 고객 인프라에 한 일"입니다. 보존 기간도 조회 방식도
다르고, 이벤트 정리 정책이 감사 기록을 지워버리면 안 됩니다.

### 기록 실패가 작업을 막지는 않습니다

감사 기록에 실패해도 예외를 밖으로 던지지 않고 앱 로그에 남깁니다.
DB 가 잠깐 흔들릴 때 콘솔 전체가 멎으면 곤란하기 때문입니다.

(감사 기록이 반드시 남아야 하는 환경이라면 반대로 막아야 합니다.
그건 이 앱의 성격을 넘는 판단이라 여기서 정하지 않았습니다.)

---

## 최초 대응 시간 (SLA)

`/report/sla` &mdash; 리포트 블루프린트 안에 있습니다. 최초 대응 시간은 월간
고객사 리포트에 들어가는 항목이라, 별도 화면으로 빼면 같은 독자·같은 주기를
가진 것이 둘로 갈라집니다.

### 무엇을 재고 무엇을 못 재는가

이걸 먼저 못 박습니다.

| | |
|---|---|
| **재는 것** | 알람이 난 뒤, 그 알람의 계정을 **처음 들여다본 시각**까지의 시간 |
| **재지 못하는 것** | "이 알람에 대응했는가" |

감사 로그는 계정 단위 기록이라, 같은 계정에 알람이 여러 개 떠 있으면 첫
조회 하나가 그 전부의 대응 시각으로 잡힙니다. **근사치입니다.**
화면 맨 위에 이 문구를 띄웁니다 &mdash; 근사치를 계약 이행 증거로 내밀면
나중에 훨씬 곤란해집니다.

이 지표는 [계정 귀속](#이벤트의-계정-귀속)과 [감사 로그](#감사-로그)가
둘 다 있어야 성립합니다. 둘 중 하나라도 없으면 알람과 대응을 이을 수 없습니다.

### 목표

계약 조건이므로 코드가 아니라 `sla_targets` 에 넣습니다.

```
고객사 비움 → 모든 고객사 기본값
고객사 지정 → 그 고객사 전용 (기본값을 이김)
```

런북과 같은 규칙입니다.

**0분은 "목표 없음"이고 집계에서 빠집니다.** '목표 없음'을 '항상 달성'으로
보여주면 지표가 거짓말을 합니다. `warning`/`info` 의 기본 제안값이 0인
이유도 같습니다.

```
심각도     목표             건수  대응 잡힘      중앙값  최악    판정
critical  15분 (A커머스)     21   4 / 미대응 17   19분   590분  미달
error     120분 (기본값)     32  21 / 미대응 11   25분   594분  달성
warning   목표 없음          45  18 / 미대응 27   91분   238분  집계 안 함
```

---

## 데이터 보존

```bash
flask --app run prune-events --days 90 --dry-run    # 몇 건인지만
flask --app run prune-events --days 90              # 확인 후 삭제
flask --app run prune-events --days 90 --snapshots  # 스냅샷도 함께
```

| | |
|---|---|
| 지우는 것 | `events`, (`--snapshots` 일 때) `resource_snapshots` |
| 지우지 않는 것 | `audit_log`, `incidents`, `work_orders`, `runbooks` |

**감사 로그는 절대 지우지 않습니다.** "우리가 고객 인프라에 한 일"이라
이벤트 정리와 수명이 다릅니다. 애초에 테이블을 나눈 이유가 이것입니다.

**작업 증적이 참조하는 스냅샷도 지우지 않습니다.** 증적의 근거가 사라지면
그 문서가 무의미해집니다. 정리 질의가 걸러낼 뿐 아니라 스키마의
`ON DELETE RESTRICT` 가 한 겹 더 막습니다 &mdash; 명령이 실수로 지우려 해도
DB 가 거부합니다.

### 주의: 오래된 장애의 타임라인이 빕니다

사후 보고서는 이벤트를 참조하지 않고 **시간 범위로 조회**합니다. 그래서
이벤트를 지우면 그 기간 장애의 타임라인이 비어 보입니다. 이미 내보낸
문서(`report.md`, `customer.md`)는 남지만, 화면에서 다시 조립하면 비어
있습니다.

명령이 실행 전에 몇 건의 사후 보고서가 영향을 받는지 알려줍니다.

---

## Slack 연동

Incoming Webhook 두 가지를 씁니다. **알람 발송은 넣지 않았습니다** &mdash;
그건 대시보드와 [알람 노이즈](#알람-노이즈) 화면으로 충분합니다.

```bash
flask --app run handover --hours 12 --slack        # 당직 인계
flask --app run sla-check --slack                  # SLA 목표 초과
```

### 왜 Incoming Webhook 인가

URL 하나면 되고 OAuth 도 앱 설치도 필요 없습니다. 슬래시 커맨드나 봇 토큰은
**공개 엔드포인트와 서명 검증**이 따라오는데, 이 앱은 인증이 하드코딩 계정
하나라 그대로 열면 안 됩니다.

전송은 표준 라이브러리(`urllib`)만 씁니다. JSON 한 번의 POST 라 `requests`
를 끌어올 이유가 없고, Lambda 배포 패키지에도 영향이 없습니다.

### 채널 나누기

```bash
SLACK_WEBHOOK_URL=...          # 공통 (이것만 있어도 됨)
SLACK_HANDOVER_WEBHOOK=...     # 당직 인계  (예: #ops-daily)
SLACK_SLA_WEBHOOK=...          # SLA 경고   (예: #ops-alert)
```

용도별 웹훅이 있으면 그것을, 없으면 공통을 씁니다.
**비워두면 전송을 건너뜁니다** &mdash; 설정하지 않은 것은 오류가 아닙니다.
그래서 `SlackNotConfigured` 를 `SlackError` 와 나눠 두었습니다.

### 당직 인계는 따로 렌더링합니다

`to_markdown` 을 그대로 보내면 안 됩니다. **Slack mrkdwn 은 표를 렌더링하지
못해서** 파이프 문자가 잔뜩 찍힌 글덩어리가 됩니다. 문법도 다릅니다
(`**굵게**` 가 아니라 `*굵게*`, `[링크](url)` 이 아니라 `<url|링크>`).

같은 내용을 목록으로 다시 씁니다.

```
*당직 인계 — 지난 24시간*
_2026-08-22 04:59 UTC_

이벤트 138건 (critical 19 / error 26 / warning 33 / info 60)

*이번 근무에 처음 나타난 알람*
• `critical` CPU usage 92.4% on i-0abc123 — web-01, 11건
...
*진행 중인 작업*
• #3 결제 DB 보안그룹 규칙 추가 — OPS-1600 · A커머스 · 증적 확정 대기 · admin  <…|열기>
```

`--base-url` 을 주면 작업과 인계 화면으로 가는 링크가 붙습니다.

### SLA 경고에는 억제가 붙습니다

```
*SLA 목표 초과 — 아직 대응 기록이 없는 알람 16종*

• `critical` *A커머스* CPU usage 94.5% on i-0abc123
    11건 · web-01 · 계정 123456789013 · 목표 15분 / 경과 *772분*

_대응 여부는 감사 로그(콘솔 조회·AI 진단) 기준입니다.
 다른 경로로 대응했다면 여기 잡히지 않습니다._
```

**같은 `(계정, 지문)` 은 `SLA_NOTICE_WINDOW_MINUTES`(기본 60분) 안에 한 번만
보냅니다.** 이 억제가 없으면 10분마다 도는 cron 이 같은 위반을 계속 알리고,
그러면 아무도 안 보게 됩니다 &mdash; 노이즈를 줄이려고 만든 기능이 노이즈가
됩니다.

`sla_notices` 를 `sla_targets` 와 분리한 것도 [알람 억제](#알람-노이즈)와
같은 이유입니다. 목표는 사람이 정하고 오래 남지만, 알림 기록은 보낼 때마다
바뀌는 실행 상태입니다.

메시지에 **이 지표의 한계를 함께 싣습니다.** 화면에서만 밝히면 Slack 으로
받은 사람은 그 맥락 없이 숫자만 봅니다.

### cron 예시

```cron
# 매일 09:00 — 지난 12시간 인계
0 9 * * *     cd /path/to/myapp && .venv/bin/flask --app run handover --hours 12 --slack

# 10분마다 — SLA 목표 초과 확인
*/10 * * * *  cd /path/to/myapp && .venv/bin/flask --app run sla-check --slack
```

---

## 에스컬레이션

SLA 목표를 넘겼는데 아무도 안 보면 사람을 부릅니다. 지금까지는 위반을
세어놓고 그 다음에 아무 일도 일어나지 않았습니다.

```bash
flask --app run add-oncall --name 김운영 --level 1 --slack-id U01ABCDEF
flask --app run add-oncall --name 최전담 --level 1 --slack-id U03 --customer A커머스
flask --app run list-oncall

flask --app run sla-check --escalate          # 단계를 올린다
flask --app run sla-check --slack --escalate  # 요약도 함께
```

### 단계

```
ESCALATION_STEPS=0,30,120
  목표 초과 즉시    → 1단계 (1차 대응자)
  목표 + 30분      → 2단계 (2차)
  목표 + 120분     → 3단계 (관리자)
```

**고객사 전담이 있으면 전체 담당보다 우선합니다** &mdash; 런북·SLA 목표와
같은 규칙입니다.

**중간 단계를 건너뛰지 않습니다.** 3단계까지 가야 하는데 1단계만 올렸으면
2·3을 한 번에 올립니다. 건너뛰면 그 사람은 자기가 호출된 적 없다는 것도
모른 채 지나갑니다.

**같은 단계를 두 번 부르지 않습니다.** `escalations` 에 기록이 남습니다
(`sla_notices`·`alarm_state` 와 같은 발상 &mdash; 규칙과 실행 상태를 나눕니다).

### 넣지 않은 것: 날짜 기반 당번표

"이번 주 1차는 누구"는 달력과 교대 규칙이 따라오는데, 그건 이 앱의 성격을
넘습니다. 여기 `level` 은 **당번 순번이 아니라 단계**입니다.

### 요약 알림과 에스컬레이션은 독립입니다

```
sla_notices  "위반이 있다" 는 요약을 얼마나 자주 보낼지
escalations  어느 단계까지 사람을 불렀는지
```

묶어두면 요약이 억제 창에 걸린 사이에 위반이 3단계까지 커져도 아무도
불리지 않습니다. `--escalate` 는 `--slack` 없이도 동작합니다.

### 티켓은 Jira 로 넘깁니다

이 앱은 **티켓 시스템을 만들지 않습니다.** 상태 관리와 담당자 배정,
코멘트 스레드는 Jira 가 이미 훨씬 잘 합니다. 여기서 흉내내면 두 곳에
같은 내용을 적게 됩니다.

`JIRA_ESCALATION_LEVEL`(기본 2)부터 이슈를 만들고 키만 기록합니다.
1단계에서 매번 만들면 Jira 가 노이즈로 찹니다.

Slack 이나 Jira 가 막혀도 **다음 단계와 기록은 계속됩니다.** 한 채널이
막혔다고 에스컬레이션 전체가 멎으면 안 됩니다.

---

## 지난 장애를 진단 재료로

사후 보고서가 쌓이면 **"지난번 이 알람은 무엇이 원인이었나"** 를 답할 수
있습니다.

```
알람 카드
  ▾ 지난번 이 알람: 결제 API 응답 지연 (2026-08-21)
     보안그룹 규칙 추가 작업 중 5432/tcp 를 0.0.0.0/0 으로 열면서
     외부 스캔 트래픽이 유입되어 DB 커넥션 풀이 고갈됨.
```

### 무엇이 재료가 되는가

**확정됐고(`published`) 원인이 적힌 보고서만** 나옵니다.

"지난번에도 이 알람이 있었다"까지만 알려주는 건 진단에 도움이 안 됩니다.
초안 상태의 추측이 재료로 흘러들면 진단이 근거 없는 이야기를 물고 옵니다.

### 이벤트가 지워져도 남습니다

장애는 이벤트를 **시간 범위로** 조회합니다. 그래서 [보존 정책](#데이터-보존)이
돌면 "어떤 알람의 장애였는지"가 통째로 사라집니다.

`incident_fingerprints` 가 그 연결을 따로 남깁니다. 보고서를 확정할 때
자동으로 만들어지고, 그 전에 확정된 것은 `flask --app run link-incidents`
로 따라잡습니다.

### AI 진단에 함께 넣습니다

```
같은 알람이 관련됐던 지난 장애 1건
  [2026-08-21] 결제 API 응답 지연 (심각도 critical)
    원인: 보안그룹 규칙 추가 작업(OPS-1600) 중 5432/tcp 를 …
    조치: 해당 규칙을 10.0.0.0/8 로 좁히고 커넥션 풀을 재기동.
    재발 방지: 보안그룹 변경 시 0.0.0.0/0 을 자동으로 걸러내는 점검 항목 추가.
```

[런북](#런북)이 "이럴 땐 이렇게 하세요"라면 이건 **"지난번엔 이게
원인이었다"** 입니다.

시스템 프롬프트에 못을 하나 박아뒀습니다 &mdash; **"지난번과 같은 원인이라고
단정하지 말고 도구로 확인한 뒤 쓰라"**. 확인할 수 없으면 그렇다고 쓰게 합니다.
과거가 재료가 되는 건 좋지만, 그게 결론을 미리 정해버리면 진단이 아니라
편견이 됩니다.

---

## 컴플라이언스 점검

`/compliance/` &mdash; **AWS Config 를 부르지 않고**, 이미 찍어둔
[리소스 스냅샷](#리소스-변경-추적)을 기준으로 인프라가 모범사례를 지키는지 봅니다.

원래 스냅샷은 "무엇이 바뀌었나" 를 보려고 찍던 것입니다. 같은 자료로
"지금 상태가 기준에 맞나" 도 볼 수 있습니다. 새로 수집할 것이 없습니다.

### Config 와 무엇이 다른가

Config 는 **지금 이 리소스가 규칙에 맞나**를 봅니다. 이쪽은 스냅샷이
쌓여 있어서 시간축이 있습니다.

| 물어볼 수 있는 것 | |
|---|---|
| 언제부터 위반이었나 | 고객사 보고에서 반드시 나오는 질문입니다 |
| 이번에 새로 생긴 위반은 | 지난 점검 이후 무엇이 늘었나 |
| **고쳤다가 다시 열린 것은** | 지금 상태만 보면 처음 열린 것과 세 번째가 똑같아 보입니다 |

마지막이 핵심입니다. **재발은 "고쳤다" 는 보고가 임시 조치였거나,
무언가가 설정을 되돌리고 있다는 뜻**입니다. 대응이 완전히 달라야 하는데,
현재 상태만 보는 도구로는 구분되지 않습니다.

대신 한계가 분명합니다. **우리는 수집한 것만 볼 수 있습니다.** Config 는
자기가 지원하는 리소스를 전부 봅니다. IAM 사용자 MFA, root 액세스 키,
CloudTrail 활성화, KMS 키 회전, EBS 암호화는 지금 수집하지 않아서
점검할 수 없습니다. 수집기(`app/collect.py`)를 늘리면 점검 항목도 함께 늘어납니다.

**Config 를 대체하는 물건이 아닙니다.** 이미 찍고 있는 스냅샷을 한 번 더
쓰는 것입니다.

### 점검 항목

| 심각도 | 항목 | 근거 |
|---|---|---|
| critical | 관리 포트(SSH·RDP)가 인터넷에 열림 | CIS AWS 5.2 / 5.3 |
| critical | DB 포트가 인터넷에 열림 | CIS AWS 5.2 |
| critical | 모든 포트가 인터넷에 열림 | CIS AWS 5.2 |
| critical | 관리 포트가 열린 보안그룹을 쓰는 인스턴스 | CIS AWS 5.2 |
| critical | S3 퍼블릭 액세스 차단이 꺼짐 | CIS AWS 2.1.5 |
| high | RDS 퍼블릭 접근 | CIS AWS 2.3.3 |
| high | 역할에 광범위한 권한(`*FullAccess`) | CIS AWS 1.16 |
| high | S3 기본 암호화 없음 | CIS AWS 2.1.1 |
| medium | RDS 단일 AZ | Well-Architected |
| medium | S3 버저닝 꺼짐 | Well-Architected |
| low | 필수 태그 누락 | 태깅 정책 |

443 이 열려 있는 것은 위반이 아닙니다. 웹은 열려 있는 게 정상이고,
**여기서 걸리면 아무도 목록을 보지 않게 됩니다.**

### 리소스 하나만 봐서는 답할 수 없는 것

`ec2-exposed-admin-port` 는 인스턴스와 보안그룹을 함께 봅니다.

```
보안그룹만 보면        → "sg-web 의 22 번이 열려 있다"
인스턴스까지 보면      → "그 그룹에 running 인스턴스가 2대 붙어 있다"
```

붙은 인스턴스가 없으면 치우면 되는 문제고, 붙어 있으면 **지금 뚫려 있는**
문제입니다. Config 의 규칙 하나로는 다루기 어려운 구분인데, 스냅샷은
통째로 들고 있으니 그냥 찾아보면 됩니다.

### 규칙은 코드에, 예외는 표에

점검 항목은 `app/compliance.py` 에 함수로 있습니다. 규칙은 조건식이라
표에 넣으면 문자열로 저장했다가 다시 해석하는 일이 생깁니다.

표에 넣어야 하는 것은 규칙이 아니라 **예외**입니다. MSP 에서 위반 목록이
쓸모없어지는 이유는 대개 하나입니다.

> "이건 고객이 알고 승인한 건데 계속 빨갛게 뜬다."

그런 항목이 몇 개만 쌓이면 아무도 목록을 안 봅니다. 그래서 예외에는
**사유와 만료일이 반드시** 붙습니다. 끝나지 않는 예외는 예외가 아니라
그냥 못 본 척하는 것입니다. 만료되면 위반이 자동으로 다시 뜹니다.

예외로 뺀 항목도 목록에서 지우지 않고 "예외 처리됨" 으로 함께 보여줍니다.
지워버리면 예외가 몇 개 쌓여 있는지 아무도 모르게 됩니다.
등록·삭제는 [감사 로그](#감사-로그)에도 남습니다 &mdash; 위반을 목록에서
치우는 행위이기 때문입니다.

### 결과를 저장하지 않습니다

위반 목록을 표에 넣지 않고 볼 때마다 다시 계산합니다. 스냅샷이 이미
시점 데이터라서 언제든 재계산할 수 있고, 저장하면 진실이 둘이 됩니다.
**점검 항목을 고치면 과거 시점도 새 기준으로 다시 계산됩니다** &mdash;
저장했다면 옛 기준으로 판정된 결과가 섞여서 어느 쪽이 맞는지 알 수 없습니다.

(`event_store` 를 메모리에 두었다가 DB 로 옮기면서 배운 것과 같습니다.
원본이 있는데 사본을 따로 들고 있을 이유가 없습니다.)

```bash
flask --app run compliance-check                      # 전체
flask --app run compliance-check --severity high      # high 이상만
flask --app run compliance-check --slack              # critical 이 있으면 Slack
flask --app run compliance-check --xlsx 8월보고서.xlsx  # 보고용 엑셀
```

결과를 저장하지 않으므로 이 명령은 아무것도 바꾸지 않습니다. 몇 번을
돌려도 안전합니다.

### 보고용 엑셀

`/compliance/download.xlsx` &mdash; 화면 위쪽의 **이 계정 엑셀로** /
**전체 계정 엑셀로** 에서 받습니다.

화면은 "지금 무엇이 문제인가" 를 보는 데 맞춰져 있습니다. 고객사에 내는
보고서는 다릅니다. 담당자가 항목을 골라 사람을 배정하고, 처리 여부를 옆
칸에 적고, 다음 달 것과 나란히 놓고 봅니다. 그건 엑셀이 하는 일입니다.

| 시트 | 내용 |
|---|---|
| 요약 | 심각도별 합계, 계정별 한 줄 요약 |
| 위반 상세 | 본문. **심각도 순**으로 놓습니다 |
| 시간축 | 스냅샷별 위반 수와 변화 내역 |
| 재발 | 고쳤다가 다시 열린 항목 |
| 예외 | 사유·승인자·만료일 |
| 점검 항목 | **무엇을 봤는지** |

몇 가지는 의도적으로 그렇게 했습니다.

- **위반 상세는 계정이 아니라 심각도 순**입니다. 계정별로 묶으면 두 번째
  계정의 critical 이 첫 번째 계정의 low 아래로 내려갑니다. 보고서를 여는
  사람이 맨 위에서 보고 싶은 건 가장 급한 것입니다.
- **담당자·조치 결과 열이 비어 있습니다.** 받은 사람이 채우는 자리입니다.
  열이 없으면 옆에 새 열을 만들어 쓰게 되고, 그러면 다음 달 파일과 모양이
  달라져서 두 달을 나란히 놓을 수 없습니다.
- **재발이 시간축과 다른 시트**입니다. 자동 필터가 걸린 시트 아래에 다른
  표를 두면, 위쪽 표에 필터를 거는 순간 아래 표가 통째로 숨습니다.
- **점검 항목 시트가 들어갑니다.** 이게 없으면 "위반 0건" 이 무슨 뜻인지
  알 수 없습니다 &mdash; 안전하다는 뜻인지, 안 봤다는 뜻인지.
- **시각은 전부 UTC** 로 적고 머리글에 밝힙니다. 로컬 시각으로 바꾸면
  서버 시간대에 따라 값이 달라져서, 같은 점검을 두 사람이 뽑으면 다른
  문서가 나옵니다.

> 파일 이름에 한글이 들어갑니다(`컴플라이언스-A커머스-20260821.xlsx`).
> HTTP 헤더는 latin-1 이라 그대로 넣으면 깨집니다. RFC 5987 의
> `filename*=UTF-8''` 로 알려주고, 그걸 모르는 옛 클라이언트를 위해
> ASCII 이름도 함께 보냅니다.

`XlsxWriter` 를 씁니다. 새 파일을 쓰기만 하면 되고(기존 파일을 여는 일이
없다), 서식·자동 필터·틀 고정을 한 번에 지정할 수 있습니다. import 는
함수 안에서 합니다 &mdash; **엑셀 내려받기 하나 때문에 앱 전체가 뜨지 않으면
곤란하기 때문**입니다. (`report_pptx.py` 는 모듈 맨 위에서 import 하는데,
그건 python-pptx 가 없으면 리포트 블루프린트 자체가 등록되지 않는다는
뜻입니다. 같은 실수를 반복하지 않으려고 여기서는 늦게 부릅니다.)

---

## 메뉴와 카테고리

왼쪽 메뉴는 `app/nav.py` 에 **데이터로** 있습니다.

### 예전에는 템플릿에 박혀 있었습니다

`base.html` 에 `<a>` 태그가 열여섯 개 있었습니다. 기능을 하나 붙일 때마다
HTML 다섯 줄을 옳은 자리에 끼워 넣어야 했고, 권한 검사(관리자만 보이기)도
태그마다 따로 감쌌습니다. **메뉴에 넣는 걸 잊어도 아무도 몰랐습니다.**

지금은 한 줄입니다.

```python
{"endpoint": "compliance.index", "label": "컴플라이언스", "icon": "✓",
 "category": "infra", "hint": "스냅샷 기준 모범사례 점검"},
```

### 카테고리

예전에는 `관측` 하나에 열세 개가 들어 있었습니다. 그건 카테고리가 아니라
**'나머지 전부'** 입니다. 지금은 MSP 업무의 흐름을 따라 나눕니다.

| 카테고리 | 뜻 | 들어 있는 것 |
|---|---|---|
| 고객사 | 누구의 것인가 | 고객사 현황, 온보딩 준비도 |
| 알람 | 무슨 일이 일어났나 | 대시보드, 이벤트, 탐색, 알람 노이즈 |
| 인프라 | 지금 어떤 상태인가 | 리소스 변경, 컴플라이언스 |
| 대응 | 지금 무엇을 하나 | 당직 인계, 런북, 작업 기록, AI 에이전트, 콘솔 |
| 보고 | 밖으로 무엇을 내보내나 | 사후 보고서, 리포트, 월간 리뷰 |
| 관리 | 도구 자체 | 관리자 |

**새 기능이 어디 들어갈지 대개 바로 정해집니다.**

```
변경 승인   → 대응        만료 추적   → 알람
비용        → 인프라      용량 예측   → 알람
```

한 칸이 다시 여섯 개를 넘으면 테스트가 막습니다 &mdash; 그렇게 커지면
또 '나머지 전부' 가 되기 때문입니다.

### 어느 메뉴에 불이 들어오는가

`match` 에 적은 접두사로 판단하고, **겹치면 긴 쪽이 이깁니다.**

```
report.msr_page  는  'report.'  와  'report.msr'  둘 다에 걸린다
                     → 긴 쪽(report.msr)이 이겨서 '월간 리뷰' 에 불이 들어온다
```

짧은 쪽을 고르면 월간 리뷰를 보는 동안 '리포트' 에 불이 들어옵니다.
이 규칙 덕분에 **한 블루프린트 안의 화면을 메뉴에서 따로 뗄 수 있습니다**
(`/report/` 와 `/report/msr`, `/customer/` 와 `/customer/readiness`).

### 테스트가 잡아주는 것

메뉴가 데이터가 되면서 기계가 볼 수 있게 됐습니다.

| 테스트 | 막는 실수 |
|---|---|
| 모든 endpoint 가 실제로 있는가 | 화면을 떼어냈는데 메뉴에 남아 → 전 페이지 500 |
| 전부 GET 으로 열리는가 | POST 전용을 걸어두면 405 |
| 모든 블루프린트가 메뉴에서 닿는가 | **화면을 만들고 메뉴에 안 넣음** |
| 아이콘이 겹치지 않는가 | 접힌 사이드바에서 구분 안 됨 |
| 한 칸이 여섯 개를 넘지 않는가 | 카테고리가 다시 '나머지 전부' 로 |

세 번째가 핵심입니다. 메뉴에 없어도 되는 블루프린트는 **이유와 함께**
적어둡니다 &mdash; 그래야 "왜 없지?" 를 다시 확인하지 않습니다.

```python
NOT_IN_MENU = {
    "main": "홈은 카테고리 위에 따로 있다",
    "auth": "로그인/로그아웃은 사이드바 아래에 있다",
}
```

면제 목록에 **이제 없는 이름이 남아 있는지도** 검사합니다. 오타나 유령
이름이 남아 있으면 진짜 빠진 것을 가려주기 때문입니다.

### 곁들여 드러난 CSS 버그

메뉴가 늘어나자 좁은 화면에서 **페이지 전체에 가로 스크롤**이 생겼습니다.
사이드바에 `overflow-x: auto` 가 이미 걸려 있었는데도요.

```css
/* 이것만으로는 소용이 없다 */
.sidebar { overflow-x: auto; }
```

그리드 항목의 기본값이 `min-width: auto` &mdash; **"내용보다 작아지지
않는다"** 이기 때문입니다. 그래서 사이드바가 스크롤되는 대신 그리드 칸
자체가 넓어집니다. `min-width: 0` 을 줘야 비로소 줄어들고, 그때부터
`overflow-x` 가 일을 합니다.

스크롤은 사이드바가 아니라 **메뉴 줄**이 맡게 했습니다. 사이드바를 통째로
스크롤시키면 로고와 사용자 칩까지 같이 밀려나갑니다.

같은 원인으로 열이 많은 표도 페이지를 넓히고 있었습니다(관리자 화면의
라우트 표는 이 작업 전부터 그랬습니다). 좁은 화면에서 표가 자기 안에서
스크롤되도록 고쳤고, **22개 화면 전부 390px 에서 가로 스크롤이 없는 것을
확인**했습니다.

---

## 월간 서비스 리뷰 (MSR)

`/report/msr` &mdash; 고객사 하나의 한 달치를 한 권으로 묶습니다.
화면에서 보고, **PowerPoint** 로 받습니다.

### 새로 만든 게 거의 없습니다

여기 들어가는 숫자는 전부 이미 다른 화면에 있습니다. SLA 는 리포트에,
장애는 사후 보고서에, 작업은 작업 기록에, 위반은 컴플라이언스에.

없던 것은 **"고객사 하나를 놓고 한 달치를 한 권으로 묶은 것"** 입니다.
지금까지는 고객사와 미팅하려면 화면 여섯 개를 돌면서 옮겨 적어야 했습니다.

### 왜 달(month) 단위인가

`/report/` 는 "지난 7일" 처럼 지금부터 거슬러 셉니다. 운영자가 보기에는
그게 맞습니다. 고객사 리뷰는 다릅니다.

> "8월 보고" 는 8월 1일부터 31일까지지, 오늘부터 30일 전까지가 아닙니다.

**지난달 것을 다시 뽑았을 때 값이 달라지는 보고서는 보고서가 아닙니다.**
그래서 `sla.measure()` 에 구간(start/end)을 받는 길을 열었습니다 &mdash;
같은 계산을 두 벌 두면 리포트 화면과 고객사 보고서의 숫자가 언젠가 갈라집니다.

### 슬라이드 아홉 장

표지 / 한 달 요약 / SLA / 장애 / 작업 / 컴플라이언스 / 자주 발생한 알람 /
다음 달 계획 / **이 숫자가 말하지 않는 것**.

몇 가지는 의도적으로 그렇게 했습니다.

- **다음 달 계획이 화면에서는 위에 있습니다.** 숫자를 다 읽고 나서야 할 일이
  나오면, 바쁜 사람은 숫자만 보고 닫습니다. 자동으로 뽑은 초안이라
  담당자가 미팅 전에 손봐야 하지만, **빈 칸을 앞에 두면 결국 아무도 안 씁니다.**
- **컴플라이언스만 '지금' 기준**입니다. 스냅샷이 있으니 지난달 말 시점으로
  다시 계산할 수도 있지만, 고객사가 보고서를 받아 드는 시점에 알고 싶은 것은
  "지금 남아 있는 위반" 입니다. 지난달 말에 몇 개였는지는 고쳐야 할 일을
  알려주지 않습니다.
- **자료가 비어도 장 수는 같습니다.** 달마다 목차가 달라지면 지난달 것과
  나란히 놓고 보기 어렵습니다.
- **마지막 장은 "이 숫자가 말하지 않는 것"** 입니다. 자신 없는 부분을
  적어두는 건 약점이 아닙니다 &mdash; 적지 않으면 읽는 사람이 숫자를
  실제보다 넓게 해석합니다.

### 표가 슬라이드 밖으로 나가는 문제

`pptx` 는 도형을 슬라이드 밖에 놓아도 예외를 내지 않습니다. **인쇄물에서만
조용히 사라집니다.** 장애 30건짜리 나쁜 달을 넣어보고서야 알았습니다.

줄 수에 상한을 두고, 잘라냈으면 **잘랐다고 적습니다**. 말없이 빠지면
보고서가 거짓말을 하게 됩니다.

테스트는 좌표를 직접 잽니다.

```python
for sh in slide.shapes:
    if sh.left + sh.width > W or sh.top + sh.height > H:
        ...   # 밖으로 나감
```

"예외 없이 만들어졌다" 만 보는 테스트로는 이 버그를 못 잡습니다.
글자 상자만 검사했을 때도 못 잡았습니다 &mdash; 표는 `GraphicFrame` 이라
`has_text_frame` 이 False 입니다.

---

## 온보딩 준비도

`/customer/readiness` &mdash; **이 고객사를 받을 준비가 됐는가.**

컴플라이언스와 구조가 같습니다. 점검은 코드에 두고, 결과는 저장하지 않고
볼 때마다 다시 계산합니다. 다른 점은 점검 대상입니다.

| | 점검 대상 |
|---|---|
| 컴플라이언스 | 고객사의 **AWS 리소스**가 기준에 맞는가 |
| 준비도 | **우리 쪽 설정**이 갖춰져 있는가 |

### 왜 필요한가

신규 고객사를 받으면 할 일이 흩어져 있습니다. 계정 등록은 CLI 에서,
SLA 목표는 리포트 화면에서, 당직 담당자는 또 다른 CLI 에서, 런북은 런북
화면에서. **어느 하나만 빠져도 조용히 망가집니다.**

```
당직 담당자가 없으면  →  에스컬레이션이 아무도 부르지 않는다
SLA 목표가 없으면     →  초과를 판정할 기준이 없다
스냅샷이 없으면       →  컴플라이언스도 RCA 도 작업 증적도 못 쓴다
```

셋 다 **에러가 아니라 "조용한 없음"** 이라서, 사고가 나고 나서야 압니다.

### 점검 항목

| 등급 | 항목 |
|---|---|
| 필수 | 계정 등록 · AssumeRole · 리전 · 알람 인입 · 당직 담당자 · SLA 목표 |
| 권장 | 리소스 스냅샷 · 자주 나는 알람의 대응 절차 |
| 선택 | 작업 기록 사용 |

상태는 네 가지입니다. **`없음` 과 `판단 불가` 를 구분**합니다 &mdash;
테이블이 없는데 "알람이 안 온다" 고 하면 엉뚱한 곳을 고치게 됩니다.

### 퍼센트를 쓰지 않습니다

필수 항목이 빠져 있어도 90% 가 나올 수 있고, 그러면 "거의 다 됐다" 로
읽힙니다. **필수가 하나라도 비면 '받을 수 없음'** 입니다.

`확인 필요`(있긴 한데 온전하지 않음)도 필수 항목에서는 막습니다.
그렇지 않으면 Slack ID 없는 담당자만 등록해두고 준비가 끝난 줄 알게 됩니다.

### 만들면서 잡은 것

준비도 점검이 **이 앱의 다른 부분과 다른 말을 하고 있었습니다.**
`app/sla.py` 는 고객사 전용 목표가 없으면 기본값으로 판정하는데,
준비도는 전용 목표만 보고 "없음" 이라고 했습니다. 두 화면이 같은 상태를
두고 다르게 말하면 어느 쪽도 믿을 수 없습니다.

지금은 이렇게 나눕니다.

| 상태 | 뜻 |
|---|---|
| 완료 | 고객사 전용 목표가 있다 |
| 확인 필요 | 기본값으로 돌고 있다 &mdash; **계약서 값과 같은지 확인해야 한다** |
| 없음 | 전용에도 기본값에도 없다 |

### 안 됐다는 것만 알려주지 않습니다

항목마다 **어디서 고치는지**를 함께 둡니다. 화면이 있으면 링크로,
없으면 명령을 그대로 적습니다. 어디서 고치는지 안 알려주면 화면을
두 번 돌게 됩니다.

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
"같은 지문은 5분에 한 번만 알람" 같은 억제 규칙이나
"이 알람이 최근에 몇 번 났나" 집계가 여기에 기댑니다.

지문은 메시지를 그대로 해시하지 않습니다. 실제 알람 메시지에는 측정값이 섞여
있어서, 그대로 해시하면 같은 알람이 매번 다른 지문이 되어 묶임이 성립하지 않습니다.
그래서 **변하는 값을 자리표시자로 바꾼 '틀'** 을 먼저 만듭니다.

```
"CPU usage 92.4% on i-0abc123456789def0"  ┐
"CPU usage 87.1% on i-0abc123456789def0"  ├─→ "cpu usage <n>% on <id>"  → 같은 지문
"CPU usage 71%   on i-0fedcba987654321"   ┘
```

지워지는 것: 숫자, IP, UUID, ARN, `i-`/`vol-`/`sg-` 등 리소스 ID, 타임스탬프, 긴 16진수.
`meta`에 `AlarmName`(CloudWatch)이나 `alertname`(Alertmanager)이 있으면
메시지 대신 그 이름을 기준으로 묶습니다.

한계: 숫자를 전부 지우므로 `HTTP 500`과 `HTTP 404`도 같은 지문이 됩니다.
값 자체가 사건의 종류를 가르는 경우는 메시지 본문이 아니라 `meta`에 담아 보내야 합니다.

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
| `SESSION_HOURS` | `12` | 로그인이 풀리기까지의 시간 |
| `INGEST_API_KEYS` | (없음) | 알람 수집 API 키, 쉼표로 구분. 비우면 인증 없이 열립니다 |

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

- **계정이 하나도 없으면 코드에 박힌 부트스트랩 계정**(`admin` / `1234`)으로
  들어올 수 있습니다. 계정을 하나라도 만들면 닫힙니다.
- **`INGEST_API_KEYS` 를 비워두면 알람 수집 API 가 인증 없이 열립니다.**
  이 앱은 "설정하지 않은 기능 때문에 앱이 뜨지 않는 것" 을 피하는 쪽을
  택하고 있는데, 이 자리만은 그 대가가 큽니다(아래 참고).
- **에이전트 대화가 메모리에만** 저장됩니다. 서버를 재시작하면 사라지고,
  워커를 여러 개 띄우면(`gunicorn -w 4`) 요청마다 다른 메모리를 보게 됩니다.
  이벤트는 `events` 테이블로 옮겼습니다.

### 아직 고치지 않은 문제

| 문제 | 영향 |
|---|---|
| 로그아웃해도 에이전트 대화가 남음 | 같은 브라우저의 다음 사용자에게 이전 대화가 보일 수 있음 |
| `production` 설정이 DB 비밀번호를 요구 | `DATABASE_URL` 만 쓰는 배포에서도 `DB_PASSWORD` 를 요구할 수 있음 |
| `WTF_CSRF_ENABLED` 설정이 실제로는 무의미 | Flask-WTF 미설치. CSRF 방어가 없는데 있는 것처럼 보임 |
| 대화 길이 상한 없음 | 대화가 길어질수록 토큰 비용이 계속 증가 |
| 세션에 담은 역할이 즉시 반영되지 않음 | 역할을 바꿔도 그 사람이 다시 로그인해야 적용됨 |

### 검증되지 않은 부분

- **Bedrock 경로는 실제 호출로 검증되지 않았습니다.** 요청이 올바른 형태로
  만들어지는 것까지만 확인했습니다.
- **`LAMBDA_MODE=aws` 경로도 마찬가지**입니다. 로컬 모드만 실제로 동작을 확인했습니다.
- **리소스 수집의 실제 AWS 경로(`collect-resources` 를 `--demo` 없이)도 미검증**입니다.
  스키마·정규화·diff·화면은 합성 데이터로 검증했습니다.
- **콘솔의 `aws` 명령 실행은 검증했습니다.** AWS CLI 를 설치하고 실제로
  돌려서, 허용 목록을 통과한 명령이 AWS 엔드포인트까지 닿는 것을 확인했습니다.
  자격증명이 자리표시자(`DEMO`)라 응답은 `InvalidClientTokenId` 에서 멈춥니다.
  **남은 것은 AssumeRole 한 조각**입니다 &mdash; 실제 계정과 역할이 필요합니다.

  > 여기서 하나 고쳤습니다. 자식 프로세스에 최소한의 환경변수만 넘기는데,
  > 프록시와 CA 설정까지 빼고 있었습니다. 사내 프록시가 자체 CA 로 TLS 를
  > 다시 맺는 망(MSP 환경에서 흔합니다)에서는 CLI 가
  > `certificate verify failed` 로 죽습니다. 이 값들은 비밀이 아니고 없으면
  > 아예 나가지 못하므로 물려주도록 했습니다. 검증을 끄는 선택지
  > (`--no-verify-ssl`)는 두지 않았습니다 &mdash; 그건 고객사 계정으로 가는
  > 연결을 아무나 가로챌 수 있게 만드는 것입니다.
- **알람 AI 진단의 실제 모델 호출은 미검증**입니다. 개발 환경에 API 키가 없어
  모델 호출만 가짜로 바꿔 검증했습니다. 도구 바인딩·허용 목록 거부·출력 잘림·
  발생 이력 조회·감사 기록·화면은 실제 코드로 검증했습니다.
- **작업 기록의 실제 AWS 수집 경로도 미검증**입니다. 데모 계정(합성 리소스)으로
  상태 전이·차이·증적 문서를 검증했습니다. 실제 계정은 AssumeRole 로 받은
  자격증명으로 boto3 를 부르는데, 이 경로가 아직 한 번도 실행되지 않았습니다.

