# 외부 자료·의존성

openpyxl: MIT 라이선스. websocket-client 1.9.0: Apache-2.0 라이선스. 의존성은 requirements.txt로 설치하며 복사한 vendor 소스/바이너리는 저장소에서 제외합니다. 설치 시 각 패키지의 LICENSE/NOTICE를 함께 확인하세요.

PySide6 6.11.2 / PySide6_Essentials / PySide6_Addons / shiboken6는 GUI 전용 의존성입니다. 배포 패키지 metadata의 License 표기는 LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only이며 상용 라이선스 옵션도 제공됩니다. 각 패키지에 포함된 licenses와 Qt 공식 배포 조건을 확인하세요: https://www.qt.io/development/qt-framework/python-bindings . 로컬 gui_vendor를 통째로 Git에 넣지 않으며 패키지 metadata/라이선스 파일을 제거하지 않습니다. 콘솔·주문 코어는 PySide6를 import하지 않습니다.

Kiwoom 공식 문서/PDF/API 전체 원본 및 외부 예제 코드는 로컬 참고자료로 보존하고 업로드하지 않습니다. reference/contract.json은 기존 회귀검사가 사용하는 6개 API 요청/응답의 필드 식별자와 필수 여부만 포함하며 설명·표·인증값은 없습니다. 공식 문서 재배포 허가가 확인된 것으로 표현하지 않습니다.

tests/fixtures CSV는 합성 데이터입니다. 자체 코드에 대해 새로운 오픈소스 라이선스를 임의 부여하지 않았습니다. Private 저장소의 접근권한·사용범위는 저장소 소유자가 정합니다.
