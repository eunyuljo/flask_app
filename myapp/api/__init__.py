# api/__init__.py
# AWS Lambda 에 배포할 핸들러들을 모아두는 패키지.
# Flask 앱(app 패키지)과 분리해 둔 이유: Lambda 는 Flask 를 전혀 모르고,
# 순수 파이썬 함수로만 동작해야 하기 때문이다.
# 이 방향 의존성(app -> api)은 있지만, 반대(api -> app)는 없다.
