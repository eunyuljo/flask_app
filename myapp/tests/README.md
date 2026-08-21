# 테스트

DB 없이 도는 **순수 로직 테스트**가 대부분입니다. 이 프로젝트에서 실제로
버그가 났던 자리를 우선으로 골랐습니다.

```bash
pip install -r requirements.txt
python -m pytest -q          # 전부
python -m pytest -q -m db    # DB 가 필요한 것만 (PostgreSQL 이 떠 있어야 함)
python -m pytest -q -m "not db"
```

`-m db` 표시가 붙은 테스트는 PostgreSQL 이 없으면 자동으로 건너뜁니다.
