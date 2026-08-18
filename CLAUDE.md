# 작업 워크플로우

새 요구사항/기능/수정 요청이 들어오면 항상 아래 순서를 따른다 (사용자가 다시
지시하지 않아도 기본값):

1. **새 브랜치 + PR** — main에 직접 커밋하지 않고 새 브랜치를 만들어 커밋하고
   PR을 연다.
2. **테스트 추가** — 변경한 로직에 대한 회귀 테스트를 반드시 같이 추가한다
   (fixture 기반, 무거운 의존성이 있는 모듈이면 순수 로직만 뽑아서 테스트).
3. **검증 후 머지** — 테스트가 통과하고 문제가 없으면 PR을 머지한다.
4. **Docker에 실제 적용 + 실행** — 머지 후 관련 이미지를 리빌드하고 컨테이너에
   반영해서 실제로 돌아가는 상태로 만든다. 머지만 하고 끝내지 않는다.

## Docker 적용 시 주의사항

- **orchestrator**: `docker-compose.yml`에서 `./orchestrator:/app`로 바인드 마운트돼
  있고 `uvicorn --reload`로 뜬다 — 소스 수정 시 컨테이너가 자동 재시작되며 반영됨.
  단, 정합성을 위해 이미지도 리빌드해두는 게 좋음(`docker build ... orchestrator`).
- **pm/designer/architect/release**: 바인드 마운트 없음. `agents/base`에서 빌드한
  이미지(`ai-dev-team-agent-pm`, 4개 역할이 전부 이 한 이미지를 공유)를 리빌드한
  뒤, `orchestrator/team_spawner.py`가 프로젝트별로 띄운 기존 컨테이너
  (`agent-{role}-{project_id}`)를 새 이미지로 재생성해야 반영됨 — 리빌드만 하고
  기존 컨테이너를 그대로 두면 안 반영된다.
- **implement/qa/autotest**: docker-compose 정적 싱글턴 서비스. 각자 Dockerfile로
  리빌드 후 `docker-compose up -d --build <service>`.
- **주의**: 파이프라인 스테이지가 `running` 중인 프로젝트의 에이전트 컨테이너를
  재생성하면 진행 중이던 작업이 끊긴다(에이전트 쪽은 xack 전에 죽으면 자동
  재전달되지 않음 — pm/designer/architect/release/qa/autotest 전부 마찬가지).
  재생성 전에 `GET /projects`로 각 프로젝트 스테이지 상태를 확인해서, 활성
  중인(`running`) 스테이지를 담당하는 컨테이너는 건드리지 말고 완료된 뒤에
  재생성한다. orchestrator 자체는 안전함(`orchestrator:events` 스트림은
  컨슈머 그룹 unack 재전달을 지원하고, 프로젝트 상태는 `STATE_DIR`에
  JSON으로 영속화돼 재시작해도 복원됨).

## 배포 러너(scripts/deploy_runner.py)는 Docker가 아니라 호스트에서 뜬다

스프린트 최하단 "배포" 버튼(`orchestrator/main.py`의 `POST
/projects/{id}/deploy`)이 트리거하는 실제 `flutter build`/`fastlane` 업로드는
Xcode가 필요해서 orchestrator의 Linux Docker 컨테이너 안에서는 못 돈다.
`scripts/deploy_runner.py`(표준 라이브러리만 씀, 호스트 python3.9로 바로 실행
가능)를 이 Mac 호스트에서 `python3 scripts/deploy_runner.py`로 직접 띄워야
배포 버튼이 동작한다 — orchestrator 컨테이너를 재빌드/재시작해도 이 프로세스는
안 딸려온다는 뜻이므로, 배포 관련 코드를 고치면 이 프로세스도 별도로
재시작해야 반영된다.

# Docker 빌드 관련 이슈

이 환경에서 `docker build`(BuildKit)가 키체인 접근 문제로 실패할 수 있다
(`security -v unlock-keychain` 필요 — 비대화형 세션에서는 불가). 이 경우
`DOCKER_BUILDKIT=0 docker build ...`(레거시 빌더)로 우회.
