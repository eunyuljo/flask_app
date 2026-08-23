-- db/schema.sql
-- Lambda 가 정규화한 이벤트를 적재하는 테이블. api/normalize_handler.py 의 INSERT 문과 짝이다.
-- 적용:  flask --app run init-db      (또는 psql -f db/schema.sql)

CREATE TABLE IF NOT EXISTS events (
    -- Lambda 가 만든 UUID(하이픈 없는 32자). 재시도로 같은 이벤트가 두 번 와도
    -- ON CONFLICT DO NOTHING 으로 중복 적재를 막는 기준이 된다.
    event_id     TEXT        PRIMARY KEY,

    event_type   TEXT        NOT NULL,
    source       TEXT        NOT NULL,

    -- 정규화된 4단계 심각도. DB 에서도 한 번 더 막아서, 코드에 버그가 생겨도
    -- 이상한 값이 들어오지 않게 한다.
    severity     TEXT        NOT NULL
                 CHECK (severity IN ('critical', 'error', 'warning', 'info')),

    message      TEXT        NOT NULL,

    -- 시각은 반드시 타임존을 포함하는 타입으로 저장한다.
    -- TIMESTAMP(타임존 없음)로 두면 서버 위치에 따라 값이 달라져 나중에 로그를 맞춰볼 수 없다.
    occurred_at  TIMESTAMPTZ NOT NULL,   -- 이벤트가 실제로 발생한 시각
    received_at  TIMESTAMPTZ NOT NULL,   -- Lambda 가 받은 시각

    -- 같은 종류의 이벤트를 묶는 해시. 알람 억제 규칙에 쓴다.
    fingerprint  TEXT        NOT NULL,

    -- 이 이벤트가 어느 AWS 계정에서 났는가. 모르면 빈 문자열.
    --
    -- 이게 없어서 세 곳에서 타협했다: 사후 보고서 타임라인은 무관한 알람이
    -- 섞여 사람이 출처를 지정해야 했고, 고객사 현황은 알람 칸을 비웠고,
    -- 노이즈 순위는 고객사별로 나누지 못했다.
    -- source 는 'pay-api' 같은 서비스 이름이라 계정으로 쓸 수 없다.
    account_id   TEXT        NOT NULL DEFAULT '',

    -- 표준 필드에 없는 나머지 값들. JSONB 라서 내부 키로도 조회할 수 있다.
    meta         JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- 최근 이벤트 조회용. 목록 화면은 항상 "최신순"이므로 DESC 로 만들어 둔다.
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events (occurred_at DESC);

-- "같은 지문이 최근 5분 안에 있었나?" 를 빠르게 확인하기 위한 복합 인덱스.
CREATE INDEX IF NOT EXISTS idx_events_fingerprint ON events (fingerprint, occurred_at DESC);

-- 심각도별 집계(대시보드)용.
CREATE INDEX IF NOT EXISTS idx_events_severity ON events (severity, occurred_at DESC);


-- ======================================================================
-- AWS 리소스 스냅샷
-- ----------------------------------------------------------------------
-- 주기적으로 리소스 상태를 통째로 찍어두고, 스냅샷끼리 비교해서
-- 무엇이 생기고 사라지고 바뀌었는지를 뽑아내기 위한 표.
-- ======================================================================

CREATE TABLE IF NOT EXISTS resource_snapshots (
    snapshot_id  BIGSERIAL   PRIMARY KEY,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    account_id   TEXT        NOT NULL,
    region       TEXT        NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'demo',  -- 'demo' 또는 'aws'

    -- 수집이 끝까지 성공했는지.
    -- 이 값이 없으면 '수집 도중 실패'를 '리소스가 삭제됨'으로 잘못 읽는다.
    -- 미완료 스냅샷은 비교 대상에서 빼야 한다.
    complete     BOOLEAN     NOT NULL DEFAULT false,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS resources (
    snapshot_id   BIGINT REFERENCES resource_snapshots ON DELETE CASCADE,
    resource_id   TEXT   NOT NULL,   -- ARN 또는 고유 ID
    resource_type TEXT   NOT NULL,   -- 'ec2:instance', 's3:bucket' ...

    -- 리소스 속성 전체
    attributes    JSONB  NOT NULL DEFAULT '{}'::jsonb,

    -- 정규화한 속성의 해시.
    -- 변경 감지를 JSONB 전체 비교가 아니라 짧은 문자열 비교로 끝낼 수 있어
    -- 리소스가 많아져도 빠르다.
    digest        TEXT   NOT NULL,

    PRIMARY KEY (snapshot_id, resource_id)
);

-- 특정 리소스의 이력을 따라갈 때 쓴다.
CREATE INDEX IF NOT EXISTS idx_resources_id ON resources (resource_id, snapshot_id DESC);
CREATE INDEX IF NOT EXISTS idx_resources_type ON resources (resource_type);


-- ======================================================================
-- 고객사 AWS 계정
-- ----------------------------------------------------------------------
-- 어떤 계정에 어떤 역할로 들어갈지를 적어둔다.
-- role_arn 이 비어 있으면 '데모 계정' 으로 취급해 실제 AWS 를 부르지 않는다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS aws_accounts (
    id          BIGSERIAL PRIMARY KEY,
    customer    TEXT    NOT NULL,              -- 고객사 이름
    account_id  TEXT    NOT NULL UNIQUE,       -- 12자리 AWS 계정 번호
    alias       TEXT,                          -- 화면에 보여줄 짧은 이름
    role_arn    TEXT    NOT NULL DEFAULT '',   -- 비어 있으면 데모 계정

    -- 혼동된 대리인(confused deputy) 문제를 막는 값.
    -- 주의: 학습용이라 평문으로 둔다. 실제 서비스라면 Secrets Manager 등에
    -- 보관하고 여기에는 참조만 저장해야 한다.
    external_id TEXT    NOT NULL DEFAULT '',

    regions     TEXT[]  NOT NULL DEFAULT '{}', -- 수집/조회를 허용할 리전
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_accounts_customer ON aws_accounts (customer, account_id);

-- ======================================================================
-- 계정 접속 확인 기록
-- ----------------------------------------------------------------------
-- MSP 는 고객사 계정에 AssumeRole 로 들어간다. 그 접점이 살아 있는지를
-- 아무도 안 보고 있었다. 계약이 끝난 계정에 우리 역할이 남아 있으면
-- 그건 사고고, 반대로 죽어 있으면 장애 때 못 들어간다. 둘 다 평소에는
-- 아무 증상이 없다가 가장 나쁜 때 드러난다.
--
-- ── 추론하지 않는다 ──────────────────────────────────────────────
-- audit_log 의 마지막 기록으로 "최근에 들어갔다" 를 유추할 수도 있다.
-- 하지만 거기에는 역할을 안 쓰는 행동(보고서 확정 등)도 섞여 있어서,
-- 그걸 접속 가능 증거로 쓰면 SLA 에서 '계정을 들여다본 것' 과
-- '알람에 대응한 것' 을 구분 못 했던 것과 같은 실수가 된다.
-- 실제로 AssumeRole 을 시도한 결과만 여기 남는다.
--
-- ── 최신 한 줄만 남긴다 ──────────────────────────────────────────
-- 알고 싶은 것은 "지금 들어갈 수 있나" 하나다. alarm_state, sla_notices
-- 와 같은 판단이다. 이력이 필요해지면 그때 쌓는 표를 따로 만든다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS account_probes (
    account_id  TEXT        NOT NULL,
    region      TEXT        NOT NULL,

    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN     NOT NULL,
    detail      TEXT        NOT NULL DEFAULT '',
    probed_by   TEXT        NOT NULL DEFAULT '',

    PRIMARY KEY (account_id, region)
);

-- 스냅샷을 계정/리전으로 찾을 일이 많아진다.
CREATE INDEX IF NOT EXISTS idx_snapshots_scope
    ON resource_snapshots (account_id, region, snapshot_id DESC);

-- ======================================================================
-- 작업 기록 (변경 증적)
-- ----------------------------------------------------------------------
-- MSP 엔지니어가 고객 요청으로 작업할 때 "요청한 것만 바뀌었다" 를 증명하기 위한
-- 기록이다. 작업 전/후로 스냅샷을 한 벌씩 찍어두고, 그 둘의 차이를 증적으로 남긴다.
--
-- resource_snapshots 만으로는 '무엇이 바뀌었나' 까지만 알 수 있다.
-- 여기서 '누가, 왜, 어떤 요청으로' 를 붙여야 감사 자료가 된다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS work_orders (
    id          BIGSERIAL   PRIMARY KEY,

    ticket      TEXT        NOT NULL DEFAULT '',   -- 외부 티켓 번호 (Jira 등)
    title       TEXT        NOT NULL,
    request     TEXT        NOT NULL DEFAULT '',   -- 고객 요청 원문
    expected    TEXT        NOT NULL DEFAULT '',   -- 바뀔 것으로 예상한 것

    customer    TEXT        NOT NULL,
    account_id  TEXT        NOT NULL,
    region      TEXT        NOT NULL,
    operator    TEXT        NOT NULL,              -- 작업자

    -- open        : 만들어졌고 아직 아무 스냅샷도 없음
    -- before_taken: 작업 전 스냅샷을 찍음 (이제 실제 작업을 해도 됨)
    -- after_taken : 작업 후 스냅샷을 찍음 (차이를 볼 수 있음)
    -- closed      : 증적을 확정함. 더 이상 스냅샷을 바꾸지 않는다
    status      TEXT        NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'before_taken', 'after_taken', 'closed')),

    -- 스냅샷이 지워지면 증적의 근거가 사라진다. CASCADE 로 같이 지우지 않고
    -- 삭제 자체를 막는다(RESTRICT). 증적은 남아 있는 편이 안전하다.
    before_snapshot_id BIGINT REFERENCES resource_snapshots ON DELETE RESTRICT,
    after_snapshot_id  BIGINT REFERENCES resource_snapshots ON DELETE RESTRICT,

    note        TEXT        NOT NULL DEFAULT '',   -- 작업 결과 메모
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_work_orders_scope
    ON work_orders (customer, account_id, id DESC);

-- ======================================================================
-- 런북 (알람 종류별 대응 절차)
-- ----------------------------------------------------------------------
-- "이 알람이 뜨면 이렇게 하세요" 를 지문(fingerprint)에 붙여둔다.
--
-- 지문이 제 역할을 해야만 성립하는 테이블이다. 지문이 메시지 전문을 해시하던
-- 시절에는 같은 알람이 매번 다른 지문이 되어 런북을 붙일 대상이 없었다.
-- (api/normalize_handler.py 의 _fingerprint 참고)
--
-- customer 가 빈 문자열이면 '모든 고객사 공통' 절차다.
-- 같은 지문에 공통 절차와 고객사 전용 절차가 둘 다 있으면 전용이 이긴다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS runbooks (
    id          BIGSERIAL   PRIMARY KEY,

    fingerprint TEXT        NOT NULL,
    customer    TEXT        NOT NULL DEFAULT '',   -- '' = 공통

    title       TEXT        NOT NULL,
    body        TEXT        NOT NULL,              -- 대응 절차 본문
    author      TEXT        NOT NULL,

    -- 이 런북이 어떤 알람에서 나왔는지 사람이 알아볼 수 있게 남긴다.
    -- 지문만 남기면 나중에 목록에서 무슨 알람인지 알 수가 없다.
    sample      TEXT        NOT NULL DEFAULT '',

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 같은 지문 + 같은 고객사에 런북이 둘일 이유가 없다.
    -- 이 제약이 있어야 '수정' 을 ON CONFLICT 로 처리할 수 있다.
    UNIQUE (fingerprint, customer)
);

CREATE INDEX IF NOT EXISTS idx_runbooks_fingerprint ON runbooks (fingerprint);

-- ======================================================================
-- 런북 실행 기록
-- ----------------------------------------------------------------------
-- runbooks 는 "이럴 땐 이렇게 한다" 는 규칙이고, 이 표는 "실제로 그렇게
-- 했고 결과가 이랬다" 는 실행 상태다. alarm_rules/alarm_state,
-- sla_targets/sla_notices 와 같은 이유로 나눠 둔다. 절차는 사람이 정하고
-- 오래 남지만, 실행은 따를 때마다 한 줄씩 쌓인다.
--
-- ── 이 표가 없으면 알 수 없는 것 ──────────────────────────────────
-- 지금은 런북을 몇 개 썼는지만 보인다. 그런데 정작 궁금한 것은
--   * 한 번도 안 쓰인 절차는 무엇인가 (죽은 문서)
--   * 따랐는데 반복해서 안 되는 절차는 무엇인가 (틀린 문서)
-- 이고, 둘 다 "따랐다" 는 기록이 있어야만 나온다.
--
-- ── 추론하지 않는다 ──────────────────────────────────────────────
-- 감사 로그로 "이 사람이 런북을 봤으니 따랐겠지" 를 유추할 수도 있다.
-- SLA 에서 그렇게 했다가 '계정을 들여다본 것' 과 '알람에 대응한 것' 을
-- 구분하지 못했다. 여기서는 사람이 눌러야만 남는다. 기록이 적게 쌓이는
-- 편이, 있지도 않은 이행 증거를 만들어 내는 것보다 낫다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS runbook_runs (
    id          BIGSERIAL   PRIMARY KEY,

    -- 런북이 지워져도 이 기록은 남아야 한다. 그래서 CASCADE 가 아니라
    -- SET NULL 이고, 그때 무엇을 따랐는지 알 수 있도록 지문과 제목을
    -- 실행 시점 그대로 복사해 둔다(런북 제목은 나중에 바뀐다).
    runbook_id  BIGINT      REFERENCES runbooks (id) ON DELETE SET NULL,
    fingerprint TEXT        NOT NULL DEFAULT '',
    title       TEXT        NOT NULL DEFAULT '',

    customer    TEXT        NOT NULL DEFAULT '',
    account_id  TEXT        NOT NULL DEFAULT '',
    -- 어떤 알람을 처리하다 따랐는지. 알람 화면에서 누르면 채워지고,
    -- 런북 화면에서 직접 기록하면 빈 값이다.
    event_id    TEXT        NOT NULL DEFAULT '',

    --   resolved : 절차대로 했고 해결됐다
    --   partial  : 절차는 맞는데 이걸로 끝나지 않았다
    --   failed   : 절차대로 했는데 해결되지 않았다
    --   stale    : 절차가 지금 환경과 맞지 않는다(명령어/리소스가 다름)
    -- failed 와 stale 은 런북을 고치라는 신호다. 그래서 이 둘은
    -- 메모 없이 저장할 수 없다(app/runbook.py 에서 막는다).
    outcome     TEXT        NOT NULL
                CHECK (outcome IN ('resolved', 'partial', 'failed', 'stale')),

    note        TEXT        NOT NULL DEFAULT '',
    -- 걸린 시간(분). 0 이면 안 적은 것이다. 목표가 아니라 참고용이라
    -- 비워도 저장된다.
    minutes     INTEGER     NOT NULL DEFAULT 0 CHECK (minutes >= 0),

    ran_by      TEXT        NOT NULL DEFAULT '',
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 런북별 집계(몇 번 썼나, 마지막이 언제인가)에 쓴다.
CREATE INDEX IF NOT EXISTS idx_runbook_runs_book
    ON runbook_runs (runbook_id, ran_at DESC);
-- 런북이 지워진 뒤에도 지문으로는 이어서 볼 수 있어야 한다.
CREATE INDEX IF NOT EXISTS idx_runbook_runs_fingerprint
    ON runbook_runs (fingerprint, ran_at DESC);

-- ======================================================================
-- 고객사 정기 점검
-- ----------------------------------------------------------------------
-- MSP 계약에는 보통 "월 1회 점검", "분기 리뷰" 같은 주기 업무가 붙는다.
-- 그런데 이 도구에는 "이 고객사 이번 달 것 했나" 를 볼 곳이 없었다.
--
-- job_runs 와 헷갈리면 안 된다. 저쪽은 '우리 cron 이 살아 있나' 이고
-- 이쪽은 '고객사에 약속한 일을 했나' 다. 하나는 기계가 하고 하나는
-- 사람이 한다.
--
-- ── 왜 시간대가 없나 ────────────────────────────────────────────
-- 주기를 달력(매월 1일)이 아니라 경과일로 잡는다. 달력으로 하면
-- "2월은 28일이라 미달" 같은 판정이 생기고, 그 순간 영업시간·공휴일·
-- 시간대를 전부 따져야 한다. SLA 가 어려운 이유가 정확히 그것이었다.
-- 경과일이면 그 함정이 통째로 없고, "지난번 언제 했나" 만 보면 된다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS customer_routines (
    id            BIGSERIAL   PRIMARY KEY,

    customer      TEXT        NOT NULL,
    name          TEXT        NOT NULL,          -- '월간 리소스 점검'
    interval_days INTEGER     NOT NULL CHECK (interval_days > 0),

    -- 왜 하는 일인지. 인수인계 때 이게 없으면 다음 사람이 형식만 따라 한다.
    why           TEXT        NOT NULL DEFAULT '',

    -- 계약이 끝났거나 잠시 멈춘 것. 지우지 않는 이유는 수행 이력을
    -- 남기기 위해서다.
    active        BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (customer, name)
);

CREATE TABLE IF NOT EXISTS routine_runs (
    id         BIGSERIAL   PRIMARY KEY,

    -- 약속을 지워도 "그때 했다" 는 남는다. runbook_runs 와 같은 판단이다.
    routine_id BIGINT      REFERENCES customer_routines (id) ON DELETE SET NULL,
    customer   TEXT        NOT NULL DEFAULT '',
    name       TEXT        NOT NULL DEFAULT '',

    done_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_by    TEXT        NOT NULL DEFAULT '',
    note       TEXT        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_routine_runs_routine
    ON routine_runs (routine_id, done_at DESC);

-- ======================================================================
-- 고객사 연락처
-- ----------------------------------------------------------------------
-- 이 도구는 고객사에 대해 계정과 알람은 알지만, 정작 "누구에게 말하나" 는
-- 몰랐다. 새벽에 장애가 나면 사람이 사내 위키나 카톡방을 뒤진다.
--
-- oncall_members 와 다르다. 저쪽은 우리 쪽 당직자(호출받는 사람)이고
-- 여기는 고객사 쪽 사람(우리가 연락하는 사람)이다. 방향이 반대다.
--
-- ── 개인정보다 ──────────────────────────────────────────────────
-- 이름·이메일·전화번호가 들어간다. 그래서 리포트(엑셀/PPT)에 절대
-- 싣지 않는다. 고객사에 나가는 산출물에 그 고객사 담당자 연락처를
-- 넣을 이유가 없고, 우리 쪽 자료에 섞이면 통제 범위를 벗어난다.
-- 학습용이라 평문으로 두지만, 실제 서비스라면 접근 권한을 따로 나눠야
-- 한다(지금은 로그인한 사람이면 모두 본다).
-- ======================================================================

CREATE TABLE IF NOT EXISTS customer_contacts (
    id         BIGSERIAL   PRIMARY KEY,

    customer   TEXT        NOT NULL,
    name       TEXT        NOT NULL,

    --   primary   : 평소 소통하는 기술 담당
    --   report    : 정기 보고를 받는 사람
    --   emergency : 야간·휴일에 깨워도 되는 사람
    --   approver  : 작업 승인 권한이 있는 사람
    -- 한 사람이 여러 역할이면 줄을 여러 개 만든다. 역할로 찾는 일이
    -- 대부분이라(보고 받을 사람이 누구지?) 그쪽을 편하게 둔다.
    kind       TEXT        NOT NULL
               CHECK (kind IN ('primary', 'report', 'emergency', 'approver')),

    email      TEXT        NOT NULL DEFAULT '',
    phone      TEXT        NOT NULL DEFAULT '',

    -- '평일만' 처럼 사람이 알아야 할 것. 규칙이 아니라 메모다.
    note       TEXT        NOT NULL DEFAULT '',

    -- 퇴사·인사이동. 지우지 않는 이유는 지난 발송 기록에 남은 이름이
    -- 누구였는지 확인할 수 있어야 하기 때문이다(users 와 같은 판단).
    active     BOOLEAN     NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (customer, name, kind)
);

CREATE INDEX IF NOT EXISTS idx_contacts_customer
    ON customer_contacts (customer, kind);

-- ======================================================================
-- 장애 (사후 보고서 / RCA)
-- ----------------------------------------------------------------------
-- 장애 하나에 대해 "언제 무슨 일이 있었나" 를 앱이 모아주고,
-- "왜 그랬고 무엇을 했나" 는 사람이 쓴다.
--
-- 이 구분이 이 테이블의 핵심이다. 타임라인은 데이터에서 나오지만
-- 원인과 조치는 엔지니어 머릿속에만 있다. 앱이 원인을 지어내면
-- 그건 잘못된 사후 보고서가 되고, 없느니만 못하다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS incidents (
    id          BIGSERIAL   PRIMARY KEY,

    title       TEXT        NOT NULL,
    customer    TEXT        NOT NULL DEFAULT '',
    account_id  TEXT        NOT NULL DEFAULT '',
    region      TEXT        NOT NULL DEFAULT '',

    -- 장애 구간. ended_at 이 비어 있으면 '아직 진행 중' 이다.
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,

    severity    TEXT        NOT NULL DEFAULT 'error'
                CHECK (severity IN ('critical', 'error', 'warning', 'info')),

    -- 이 장애와 관련된 이벤트 출처(source). 비어 있으면 구간의 모든 이벤트를 본다.
    --
    -- events 에는 계정 정보가 없다. source 는 'pay-api' 같은 서비스 이름이라
    -- 계정이나 고객사로 좁힐 수단이 없다. 그래서 무엇이 이 장애와 관련
    -- 있는지는 사람이 지정해야 한다. 지정하지 않으면 그 시간대에 우연히
    -- 같이 난 무관한 알람까지 타임라인에 들어와 읽을 수 없게 된다.
    sources     TEXT[]      NOT NULL DEFAULT '{}',

    -- 여기서부터는 전부 사람이 쓰는 칸이다. 앱은 채우지 않는다.
    impact      TEXT        NOT NULL DEFAULT '',   -- 고객 관점의 영향
    cause       TEXT        NOT NULL DEFAULT '',   -- 원인
    action      TEXT        NOT NULL DEFAULT '',   -- 조치
    prevention  TEXT        NOT NULL DEFAULT '',   -- 재발 방지

    author      TEXT        NOT NULL,

    -- 내부 RCA 의 상태.
    --   draft     : 작성 중
    --   published : 내부 확정. 더 고치지 않는다.
    -- 고객 제출본과 시점이 다르다. 보통 내부에서 먼저 정리하고 며칠 뒤 나간다.
    status      TEXT        NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published')),

    -- ---- 고객 제출본 -------------------------------------------------
    -- 내부 RCA 와 독자가 다르다. 같은 사실을 쓰되 표현과 입도가 달라진다.
    --   내부  : 알람 하나하나, i-0abc123 / sg-pay / #3 · admin · OPS-1600
    --   고객  : 마일스톤 4~6줄, 내부 식별자와 사람 이름 없음
    -- 그래서 칸을 따로 둔다. 다만 기록과 근거(타임라인 원본)는 하나다.
    -- 두 벌로 관리하면 나중에 어긋났을 때 어느 쪽이 맞는지 알 수 없다.
    customer_timeline   TEXT NOT NULL DEFAULT '',
    customer_impact     TEXT NOT NULL DEFAULT '',
    customer_cause      TEXT NOT NULL DEFAULT '',
    customer_action     TEXT NOT NULL DEFAULT '',
    customer_prevention TEXT NOT NULL DEFAULT '',

    --   none  : 아직 안 만듦
    --   draft : 작성 중
    --   sent  : 고객사에 냄. 더 고치지 않는다.
    customer_status TEXT NOT NULL DEFAULT 'none'
                CHECK (customer_status IN ('none', 'draft', 'sent')),
    customer_sent_at TIMESTAMPTZ,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,

    -- 끝이 시작보다 앞설 수는 없다. 타임라인 질의가 통째로 빈다.
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_incidents_window
    ON incidents (started_at DESC);


-- ======================================================================
-- 나중에 추가된 열 보정
-- ----------------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS 는 테이블이 이미 있으면 아무 일도 하지 않는다.
-- 그래서 위에서 열을 추가해도 기존 설치에는 반영되지 않는다.
-- 이 프로젝트에는 마이그레이션 도구가 없으므로, init-db 를 다시 돌리면
-- 따라잡을 수 있게 여기에 적어둔다. 전부 IF NOT EXISTS 라 여러 번 실행해도 된다.
-- ======================================================================

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS sources TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_timeline   TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_impact     TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_cause      TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_action     TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_prevention TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS customer_sent_at TIMESTAMPTZ;

-- 열만 추가하면 CHECK 이 따라오지 않는다. 제약이 반쪽만 걸린 상태가
-- 제일 나쁘므로(새 설치에만 걸림) 이름을 지정해 조건부로 붙인다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'incidents_customer_status_check'
    ) THEN
        ALTER TABLE incidents ADD CONSTRAINT incidents_customer_status_check
            CHECK (customer_status IN ('none', 'draft', 'sent'));
    END IF;
END $$;

-- ======================================================================
-- 알람 억제 (노이즈 줄이기)
-- ----------------------------------------------------------------------
-- 지문별로 "이 알람은 N분에 한 번만" 또는 "아예 보내지 마" 를 정한다.
--
-- events 스키마 주석에 "억제 규칙에 쓴다" 고 적어두고 구현이 없었다.
-- 지문이 메시지 전문을 해시하던 시절에는 같은 알람이 매번 다른 지문이라
-- 억제가 성립하지 않았기 때문이다. 지문을 고치고 나서야 만들 수 있게 됐다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS alarm_rules (
    fingerprint     TEXT        PRIMARY KEY,

    -- 이 시간 안에 같은 지문으로 이미 알람을 보냈으면 건너뛴다.
    -- 0 이면 억제하지 않는다(매번 보낸다).
    window_minutes  INTEGER     NOT NULL DEFAULT 0
                    CHECK (window_minutes >= 0),

    -- 아예 보내지 않는다. window_minutes 보다 우선한다.
    muted           BOOLEAN     NOT NULL DEFAULT false,

    -- 왜 이 규칙을 걸었는지. 나중에 푸는 사람이 알아야 한다.
    note            TEXT        NOT NULL DEFAULT '',
    sample          TEXT        NOT NULL DEFAULT '',   -- 어떤 알람인지 알아볼 예시

    author          TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 마지막으로 알람을 보낸 시각. 억제 판정의 근거다.
-- alarm_rules 와 분리한 이유: 규칙은 사람이 만들고 오래 남지만,
-- 이 값은 알람이 나갈 때마다 바뀌는 실행 상태다. 섞어두면
-- 규칙을 지웠을 때 발송 이력까지 사라진다.
CREATE TABLE IF NOT EXISTS alarm_state (
    fingerprint     TEXT        PRIMARY KEY,
    last_alarmed_at TIMESTAMPTZ NOT NULL,
    sent_count      BIGINT      NOT NULL DEFAULT 1,
    suppressed_count BIGINT     NOT NULL DEFAULT 0
);


-- events 에 나중에 추가한 열. 기존 설치가 따라잡을 수 있게 여기에도 적어둔다.
ALTER TABLE events ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';

-- 고객사/계정으로 이벤트를 좁히는 질의가 많아진다.
CREATE INDEX IF NOT EXISTS idx_events_account
    ON events (account_id, occurred_at DESC);

-- ======================================================================
-- 감사 로그
-- ----------------------------------------------------------------------
-- 고객사 계정을 건드린 기록. 콘솔 명령과 AI 진단이 여기 쌓인다.
--
-- 처음에는 event_store(메모리 deque, 100건)에 넣었는데 잘못이었다.
-- 재시작하면 사라지고 101건째부터 앞이 밀려난다. 감사 로그의 요건은
-- '지워지지 않는 것' 인데 정반대였다.
--
-- events 와 섞지 않고 따로 두는 이유: events 는 '고객 인프라에서 일어난 일',
-- 여기는 '우리가 고객 인프라에 한 일' 이다. 보존 기간도 조회 방식도 다르고,
-- 이벤트 정리 정책이 감사 기록을 지워버리면 안 된다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    actor       TEXT        NOT NULL,              -- 로그인 사용자
    -- human : 사람이 직접 한 것 (콘솔에서 명령을 침)
    -- agent : 모델이 만든 것 (AI 진단이 조회를 실행)
    -- 이 구분이 없으면 나중에 "모델이 무엇을 조회했나" 를 분리해낼 수 없다.
    actor_kind  TEXT        NOT NULL DEFAULT 'human'
                CHECK (actor_kind IN ('human', 'agent')),

    action      TEXT        NOT NULL,              -- console_command / ai_diagnose ...
    customer    TEXT        NOT NULL DEFAULT '',
    account_id  TEXT        NOT NULL DEFAULT '',
    region      TEXT        NOT NULL DEFAULT '',

    -- ok           : 실행됨
    -- failed       : 실행됐으나 종료코드가 0 이 아님
    -- rejected     : 허용 목록에 걸려 실행하지 않음  <- 감사에서 눈여겨볼 것
    -- exec_failed  : 허용됐으나 실행 환경 문제
    -- no_credentials : 자격증명을 얻지 못함
    outcome     TEXT        NOT NULL,
    summary     TEXT        NOT NULL DEFAULT '',   -- 한 줄 요약(명령 등)
    detail      TEXT        NOT NULL DEFAULT '',
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_account ON audit_log (account_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_outcome ON audit_log (outcome, at DESC);

-- ======================================================================
-- SLA 목표
-- ----------------------------------------------------------------------
-- 고객사와 심각도별로 "몇 분 안에 최초 대응" 을 정한다.
-- 계약 조건이므로 코드가 아니라 데이터로 둔다.
--
-- customer 가 빈 문자열이면 '모든 고객사 기본값' 이다.
-- 같은 심각도에 기본값과 고객사 전용이 둘 다 있으면 전용이 이긴다
-- (런북과 같은 규칙).
-- ======================================================================

CREATE TABLE IF NOT EXISTS sla_targets (
    customer    TEXT        NOT NULL DEFAULT '',
    severity    TEXT        NOT NULL
                CHECK (severity IN ('critical', 'error', 'warning', 'info')),

    -- 최초 대응 목표(분). 0 이면 목표 없음(집계에서 제외).
    first_response_minutes INTEGER NOT NULL
                CHECK (first_response_minutes >= 0),

    note        TEXT        NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (customer, severity)
);

-- SLA 경고를 언제 보냈는지. 같은 알람에 반복해서 찌르지 않기 위한 기록이다.
-- 20건이 같은 이유로 위반이면 20번 보내는 게 아니라 한 번만 보낸다.
--
-- alarm_state 와 같은 발상이고 같은 이유로 규칙(sla_targets)과 분리했다:
-- 목표는 사람이 정하고 오래 남지만, 이 값은 보낼 때마다 바뀌는 실행 상태다.
CREATE TABLE IF NOT EXISTS sla_notices (
    account_id      TEXT        NOT NULL,
    fingerprint     TEXT        NOT NULL,
    last_notified_at TIMESTAMPTZ NOT NULL,
    notice_count    BIGINT      NOT NULL DEFAULT 1,
    PRIMARY KEY (account_id, fingerprint)
);

-- ======================================================================
-- 에스컬레이션
-- ----------------------------------------------------------------------
-- SLA 목표를 넘겼는데 아무도 안 보면 사람을 부른다.
-- 지금까지는 위반을 세어놓고 그 다음에 아무 일도 일어나지 않았다.
--
-- 주의: 날짜 기반 당번표(이번 주는 누가 1차)는 넣지 않았다. 달력과
-- 교대 규칙이 따라오는데 그건 이 앱의 성격을 넘는다. 여기 level 은
-- '당번 순번' 이 아니라 '단계' 다 - 1차 대응자, 2차, 관리자.
-- ======================================================================

CREATE TABLE IF NOT EXISTS oncall_members (
    id          BIGSERIAL   PRIMARY KEY,
    name        TEXT        NOT NULL,

    -- 1 = 1차 대응자, 2 = 2차, 3 = 관리자 ...
    -- 같은 단계에 여러 명을 두면 그 단계에서 모두에게 알린다.
    level       INTEGER     NOT NULL CHECK (level >= 1),

    -- Slack 사용자 ID(U01ABCDEF). @이름 이 아니라 ID 여야 멘션이 걸린다.
    -- 비워두면 이름만 적힌다.
    slack_id    TEXT        NOT NULL DEFAULT '',

    -- 특정 고객사 전담이면 채운다. 비우면 모든 고객사.
    customer    TEXT        NOT NULL DEFAULT '',

    enabled     BOOLEAN     NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_oncall_level ON oncall_members (level, customer);

-- 어디까지 올렸는지. 같은 단계를 두 번 부르지 않기 위한 기록이다.
-- sla_notices / alarm_state 와 같은 발상 - 규칙과 실행 상태를 나눈다.
CREATE TABLE IF NOT EXISTS escalations (
    account_id  TEXT        NOT NULL,
    fingerprint TEXT        NOT NULL,
    level       INTEGER     NOT NULL,
    notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 이 단계에서 만든 Jira 이슈. 티켓 시스템은 Jira 가 하고 여기는
    -- 넘긴 흔적만 남긴다.
    jira_key    TEXT        NOT NULL DEFAULT '',

    PRIMARY KEY (account_id, fingerprint, level)
);

-- ======================================================================
-- 장애 보고서를 진단 재료로
-- ----------------------------------------------------------------------
-- 사후 보고서가 쌓이면 "지난번 이 알람은 무엇이 원인이었나" 를 답할 수 있다.
-- 그러려면 장애와 알람 종류(지문)를 이어야 한다.
--
-- 지금까지 장애는 시간 범위로만 이벤트를 조회했다. 그래서 이벤트를 정리하면
-- 연결이 끊긴다. 이 표는 그 연결을 따로 남겨서, 이벤트가 지워져도
-- "이 지문은 장애 #2 와 관련이 있었다" 가 남게 한다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS incident_fingerprints (
    incident_id BIGINT      NOT NULL REFERENCES incidents ON DELETE CASCADE,
    fingerprint TEXT        NOT NULL,

    -- 그 장애 구간에 이 지문이 몇 건 났는가. 많이 난 것이 더 관련이 깊다.
    event_count INTEGER     NOT NULL DEFAULT 0,
    sample      TEXT        NOT NULL DEFAULT '',

    PRIMARY KEY (incident_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_incident_fp ON incident_fingerprints (fingerprint);

-- ======================================================================
-- 사용자
-- ----------------------------------------------------------------------
-- 처음에는 계정이 코드에 하드코딩되어 있었다(admin / 1234). 그건 '가장
-- 단순한 인증' 을 보여주는 데는 성공했지만, 이 앱이 고객사 AWS 계정에
-- 들어가고 감사 로그를 남기게 된 지금은 맞지 않는다.
--
-- 비밀번호는 해시로만 저장한다. werkzeug.security 를 쓴다 - Flask 가
-- 이미 의존하는 패키지라 새로 설치할 것이 없다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL   PRIMARY KEY,
    username      TEXT        NOT NULL UNIQUE,

    -- 평문은 어디에도 저장하지 않는다. 형식: 알고리즘$솔트$해시
    password_hash TEXT        NOT NULL,

    -- admin    : 전부 (관리자 화면, 콘솔, 감사 로그)
    -- operator : 운영 화면 (알람, 작업, 장애, 리포트 ...)
    -- viewer   : 읽기만 (쓰기 라우트는 막는다)
    role          TEXT        NOT NULL DEFAULT 'operator'
                  CHECK (role IN ('admin', 'operator', 'viewer')),

    enabled       BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);


-- ======================================================================
-- 컴플라이언스 예외
-- ----------------------------------------------------------------------
-- 점검 항목 자체는 코드(app/compliance.py)에 있다. 규칙은 '데이터'가 아니라
-- '로직'이라서, 표에 넣으면 조건식을 문자열로 저장했다가 다시 해석하는
-- 일이 생긴다. 표에 넣어야 하는 건 규칙이 아니라 예외다.
--
-- MSP 에서 위반 목록이 쓸모없어지는 이유는 대개 하나다. "이건 고객이
-- 알고 승인한 건데 계속 빨갛게 뜬다." 그런 항목이 몇 개만 쌓이면
-- 아무도 목록을 안 보게 된다.
--
-- 그래서 예외에는 반드시 사유와 만료일이 붙는다. 만료일이 없는 예외는
-- 예외가 아니라 그냥 못 본 척하는 것이다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS compliance_exceptions (
    id          BIGSERIAL   PRIMARY KEY,

    -- 어느 계정의, 어느 점검 항목에 대한 예외인가.
    account_id  TEXT        NOT NULL,
    check_id    TEXT        NOT NULL,

    -- 리소스 하나만 빼려면 그 ID 를, 계정 전체를 빼려면 빈 문자열.
    -- 계정 전체 예외는 위험해서 화면에서 따로 표시한다.
    resource_id TEXT        NOT NULL DEFAULT '',

    reason      TEXT        NOT NULL,
    approved_by TEXT        NOT NULL DEFAULT '',

    -- 만료일. 지난 예외는 자동으로 효력을 잃고 위반이 다시 뜬다.
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (account_id, check_id, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_compliance_exc
    ON compliance_exceptions (account_id, check_id);


-- ======================================================================
-- 스냅샷이 무엇을 수집했는지
-- ----------------------------------------------------------------------
-- 이게 없으면 "RDS 위반 0건" 이 두 가지 뜻을 갖는다.
--   (1) RDS 를 봤는데 문제가 없다
--   (2) RDS 를 아예 수집하지 않았다
-- 화면에서는 둘 다 똑같이 '0' 으로 보인다. 그건 보고서에서 가장 나쁜 종류의
-- 거짓말이다 - 안전하다고 읽히지만 사실은 안 본 것이다.
--
-- 수집기가 "이번에 이 종류들을 봤다" 를 적어두면, 그 목록에 없는 종류를
-- 요구하는 점검은 '통과' 가 아니라 '점검하지 못함' 으로 분류할 수 있다.
--
-- 빈 배열은 이 열이 생기기 전에 찍힌 스냅샷이라는 뜻이다. 그때는 실제로
-- 들어 있는 종류만 수집한 것으로 본다(가장 보수적인 추측).
ALTER TABLE resource_snapshots
    ADD COLUMN IF NOT EXISTS collected_types TEXT[] NOT NULL DEFAULT '{}';


-- 사후 보고서를 Jira 이슈로 넘겼을 때의 이슈 키.
-- 비어 있으면 아직 안 넘긴 것이다. 이 값이 있어야 같은 보고서로 이슈를
-- 두 번 만드는 것을 막을 수 있다 - 버튼을 두 번 누르는 일은 반드시 생긴다.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS jira_key TEXT NOT NULL DEFAULT '';


-- ======================================================================
-- 알람 확인(ack)
-- ----------------------------------------------------------------------
-- 이게 없어서 SLA 를 감사 로그로 추론하고 있었다.
--
--   추론:  "그 계정을 처음 들여다본 시각"
--   사실:  "이 알람을 누가 언제 확인했는가"
--
-- 둘은 다르다. 콘솔에 들어가 다른 일을 해도 감사 기록이 남으므로, 추론은
-- 실제보다 대응이 빨랐던 것처럼 보이게 만들 수 있다. 반대로 이 도구를
-- 거치지 않고 대응하면 영원히 '미대응' 으로 남는다.
--
-- 그렇다고 추론을 버리면 지난 기록이 전부 미대응이 된다. 그래서 둘 다
-- 쓰되, 어느 쪽에서 나온 값인지를 세어서 화면에 밝힌다. 확인 버튼이
-- 자리를 잡을수록 추론 비중이 줄어드는 것이 보여야 한다.
ALTER TABLE events ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS acknowledged_by TEXT NOT NULL DEFAULT '';

-- "아직 아무도 안 본 알람" 을 찾는 질의가 화면의 기본값이 된다.
CREATE INDEX IF NOT EXISTS idx_events_unacked
    ON events (occurred_at DESC) WHERE acknowledged_at IS NULL;


-- ======================================================================
-- 배치 실행 기록
-- ----------------------------------------------------------------------
-- 이 앱의 자동화는 전부 CLI 명령이다. cron 에 걸어두고 죽으면 아무도
-- 모른다. 컴플라이언스 점검이 3주째 안 돌았는데 화면은 3주 전 결과를
-- 오늘 것처럼 보여주는 상황이 가능했다.
--
-- 명령이 스스로 "나 돌았다" 를 남기게 하고, 관리자 화면에서 마지막 실행
-- 시각을 본다. 오래 안 돈 것은 경고한다.
--
-- 한 명령의 기록을 여러 줄로 쌓는다. 마지막 것만 덮어쓰면 "어제도
-- 실패했나" 를 알 수 없다 - 한 번 실패는 흔하지만 연속 실패는 다른 문제다.
-- ======================================================================

CREATE TABLE IF NOT EXISTS job_runs (
    id         BIGSERIAL   PRIMARY KEY,
    job        TEXT        NOT NULL,        -- CLI 명령 이름 (compliance-check ...)

    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,

    -- running : 시작만 하고 아직 안 끝남 (중간에 죽으면 여기서 멈춘다)
    -- ok      : 끝까지 성공
    -- failed  : 예외로 끝남
    outcome    TEXT        NOT NULL DEFAULT 'running'
               CHECK (outcome IN ('running', 'ok', 'failed')),

    summary    TEXT        NOT NULL DEFAULT '',   -- 사람이 읽을 한 줄
    detail     TEXT        NOT NULL DEFAULT '',   -- 실패 사유 등
    meta       JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs (job, started_at DESC);


-- ======================================================================
-- 변경 승인
-- ----------------------------------------------------------------------
-- work_orders 는 open 에서 시작했다. 만들어지자마자 작업해도 되는 상태다.
-- 고객 승인 없이 프로덕션을 건드리는 MSP 는 없는데, 그 앞 단계가 통째로
-- 비어 있었다. 그래서 "승인 없이 실행된 작업" 을 찾아낼 수 없었다.
--
-- 상태를 앞으로 늘린다. open 의 뜻은 그대로 두었다 - 이미 쌓인 기록이
-- 전부 open 이라, 이름을 바꾸면 지난 작업이 미승인으로 보인다.
--
--   requested -> open(승인됨) -> before_taken -> after_taken -> closed
--             -> rejected
-- ======================================================================

ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS requested_by TEXT NOT NULL DEFAULT '';
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS approved_by  TEXT NOT NULL DEFAULT '';
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS approved_at  TIMESTAMPTZ;
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS decided_note TEXT NOT NULL DEFAULT '';

-- 작업창. 벗어나도 막지는 않는다(아래 app/work.py 참고).
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS window_end   TIMESTAMPTZ;

-- 되돌리는 방법. 승인자가 가장 먼저 보는 칸이다.
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS rollback TEXT NOT NULL DEFAULT '';

-- 작업창을 벗어나서 시작했는가. 막지 않는 대신 증적에 남긴다.
ALTER TABLE work_orders ADD COLUMN IF NOT EXISTS out_of_window BOOLEAN NOT NULL DEFAULT false;

DO $$ BEGIN
  ALTER TABLE work_orders DROP CONSTRAINT IF EXISTS work_orders_status_check;
  ALTER TABLE work_orders ADD CONSTRAINT work_orders_status_check
    CHECK (status IN ('requested', 'rejected', 'open',
                      'before_taken', 'after_taken', 'closed'));
END $$;

-- 승인 대기 목록이 화면의 첫 화면이 된다.
CREATE INDEX IF NOT EXISTS idx_work_orders_pending
    ON work_orders (created_at DESC) WHERE status = 'requested';


-- ======================================================================
-- 장애-지문 연결이 어디서 왔는가
-- ----------------------------------------------------------------------
-- 지금까지 이 연결은 전부 추론이었다. 보고서를 확정할 때 그 시간대에 난
-- 이벤트를 훑어서 이어붙였고(link_fingerprints), 시간이 겹쳤다는 이유만
-- 있으므로 무관한 알람도 함께 딸려 들어왔다.
--
-- 알람 화면에서 "이 알람으로 보고서 만들기" 를 누르면 그 지문은 추론이
-- 아니라 사람이 지목한 것이다. 둘을 섞으면 어느 쪽이 근거 있는 연결인지
-- 알 수 없게 되므로 표시를 남긴다. 이 프로젝트에서 반복해서 나온 구분이다
-- (SLA 의 acked/inferred, 런북 실행 기록).
--
-- 나중에 link_fingerprints 가 같은 지문을 다시 이어도 이 표시는 지우지
-- 않는다. 사람이 지목한 사실이 배치 때문에 사라지면 안 된다.
-- ======================================================================

ALTER TABLE incident_fingerprints
    ADD COLUMN IF NOT EXISTS origin BOOLEAN NOT NULL DEFAULT false;
