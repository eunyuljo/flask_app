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
