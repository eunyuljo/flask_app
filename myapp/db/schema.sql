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
