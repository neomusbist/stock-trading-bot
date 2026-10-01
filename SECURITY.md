# 인증정보와 사고 대응

Private 저장소도 secret 저장소가 아닙니다. .env/실제 config/승인/runtime/releases/실제 계좌·거래 자료는 Git 제외합니다. 토큰은 메모리 전용이며 자격증명은 로컬 .env 또는 OS 환경변수로 관리합니다. 파일은 사용자 접근권한과 별도 암호화 백업으로 보호하세요. Windows 자격증명 관리자 연동은 이번 작업에 추가하지 않았습니다.

## commit 전

staged 검사 실패 시 push/commit을 멈춥니다. `git restore --staged -- <파일>`로 staging을 취소하고 제외규칙/로컬 분리를 적용합니다. 아직 최초 commit이 없어 restore가 안 되면 `git rm --cached -- <파일>`을 사용합니다. --cached는 로컬 파일을 지우지 않습니다. staged 목록과 실제 staged 바이트를 다시 검사합니다.

## local commit 후 / push 전

원격 노출은 아직 없지만 현재 파일 삭제만으로 과거 commit의 secret이 사라지지 않습니다. 공유하지 않은 최초 commit만 문제라면 안전한 index로 amend하여 이전 commit을 업로드하지 않는지 확인합니다. 여러 commit에 포함됐으면 백업 후 git-filter-repo 등으로 관련 모든 history를 정리하고 refs/reflog/객체 및 업로드할 commit을 다시 검사합니다. 자동 rewrite/revoke/rotate는 하지 않습니다. 실행 중인 실계좌 코드에서 history 작업을 하지 마세요.

## GitHub push 후

1. 추가 push를 중단하고 실제 credential 노출이면 소유자가 해당 credential revoke/rotate 및 새 발급을 우선 수행합니다. 저장소 Private 전환 또는 파일 삭제만으로 안전해지지 않습니다.
2. 별도 백업 후 git-filter-repo로 모든 관련 branch/tag/history를 정리합니다. 강제 push가 필요할 수 있으므로 협업자와 조율합니다.
3. GitHub 캐시/PR 참조·fork·clone·협업자 사본까지 확인하고 필요한 경우 GitHub Support에 제거를 요청합니다. 이미 복제된 secret을 history 정리로 회수할 수 없습니다.
4. 새 credential은 로컬 저장소만 갱신하고 제외·staged scan·접근권한을 재점검합니다.

공식 안내: [GitHub 민감정보 제거](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository). 이번 작업에서는 revoke/rotate/history rewrite/remote 생성/push를 수행하지 않습니다.
