# 예전엔 python:3.12-slim이라 Flutter/Android SDK가 아예 없어서, OpenHands가
# `flutter create`를 못 쓰고 android/ 밑 Gradle 파일들을 전부 손으로 타이핑
# 해왔다 — 그래서 매번 Gradle/AGP 버전이 미묘하게 안 맞아 QA 빌드가 반복
# 실패했다 (실제로 lib/ 코드는 하나도 없이 Gradle 스캐폴딩만 4번 만든 사고 있었음).
# QA와 동일한 Flutter 이미지를 써서 OpenHands가 직접 flutter create/build로
# 검증하며 작업하게 한다.
FROM ghcr.io/cirruslabs/flutter:3.41.6

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# pip의 백트래킹 리졸버는 openhands-sdk/openhands-tools의 무거운 전이 의존성
# (opentelemetry, lmnr 등)에서 풀리지 않는 충돌을 낸다. 업스트림이 실제로
# 검증하는 uv 리졸버를 사용한다.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --break-system-packages --no-cache -r requirements.txt

COPY run.py git_workspace.py prompt_helpers.py build_apk.py .

CMD ["python3", "run.py"]
