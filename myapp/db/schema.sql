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
