# tests/test_layout_css.py
# 화면이 깨지는 방식 중 조용한 것들.
#
# CSS 는 테스트하기 나쁜 대상이라 전부를 덮지 않는다. 다만 여기 셋은
# 브라우저에서 눈으로 보기 전에는 아무도 모르고, 지워져도 아무 오류가
# 안 나서 조용히 되돌아온다.

import pathlib

import pytest

CSS = (pathlib.Path(__file__).parent.parent / "app" / "static" / "css" /
       "style.css").read_text("utf-8")


def test_content_links_have_an_explicit_color():
    """a 규칙이 없으면 브라우저 기본 파랑(#0000EE)이 쓰인다.

    라이트 모드에서는 그럭저럭 보이지만 다크 모드 배경(#16181c)에서는
    거의 안 보인다. 실제로 그 상태로 한참 있었다.
    """
    assert "a, a:visited { color: var(--accent); }" in CSS


def test_the_left_column_is_painted_below_the_sticky_sidebar():
    """사이드바는 height: 100vh 라 페이지가 길면 그 아래가 빈다.

    .shell 이 왼쪽 띠를 칠하고 .main 이 오른쪽을 덮는다. 둘 중 하나만
    있으면 아래가 하얗게(라이트) 또는 어둡게(좁은 화면) 뜬다.
    """
    assert "background: var(--sidebar);" in CSS
    main_rule = CSS[CSS.index(".main {"):]
    assert "background: var(--bg);" in main_rule[:400]


def test_the_menu_can_scroll_when_it_is_taller_than_the_window():
    """메뉴 26개면 1200px 가 넘는다. 창이 낮으면 아래쪽 항목에 닿을 수 없다.

    min-height: 0 이 없으면 flex 항목이 내용보다 작아지지 못해서
    overflow-y 를 줘도 스크롤이 안 걸린다.
    """
    nav_rule = CSS[CSS.index(".side-nav {"):]
    nav_rule = nav_rule[:nav_rule.index("}")]
    assert "overflow-y: auto" in nav_rule
    assert "min-height: 0" in nav_rule
