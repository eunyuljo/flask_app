# run.py
# 애플리케이션 진입점. app 패키지에서 create_app() 을 가져와 실제 앱 객체를 만든다.
# 프로젝트 루트에 있어야 app / api 두 패키지를 모두 import 할 수 있다.

from app import create_app

# 여기서 딱 한 번 앱을 만든다.
# `flask --app run run` 은 이 파일 안의 `app` 이라는 이름의 변수를 찾아 실행한다.
app = create_app()


if __name__ == "__main__":
    # `python run.py` 로 직접 실행했을 때 개발 서버를 띄운다.
    # debug=True 이면 코드를 저장할 때마다 서버가 자동 재시작되고, 에러 화면이 자세히 나온다.
    app.run(debug=True, port=5000)
