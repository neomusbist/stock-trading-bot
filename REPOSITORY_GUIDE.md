# Windows 최초 Private 업로드

실계좌 설치 폴더 전체를 업로드하지 않습니다. 준비된 github_source만 사용합니다. 이 절차는 사용자 직접 실행용이며 이번 작업에서 GitHub 저장소 생성이나 push를 수행하지 않습니다.

1. 메뉴에서 STOP을 확인합니다. 감시 NOT RUNNING, 봇 보유/미체결/UNKNOWN 없음, 기존 HALT 여부를 확인합니다. 이 확인만으로 실제 현재 계좌까지 새로 조회한 것으로 해석하지 않습니다.
2. 설치본·로컬 설정·승인·실거래 기록을 별도 안전한 저장장치에 백업합니다. releases는 로컬 복구용으로 보존합니다.
3. .env와 실제 설정/계좌/runtime/releases가 소스 폴더에 없는지 확인합니다. Windows 파일탐색기로 github_source를 열고 주소창에 powershell을 입력하여 이 폴더에서 실행합니다.
4. 아래 명령을 순서대로 실행합니다. Python과 Git 설치가 필요합니다. <...>는 본인의 실제 Private 저장소 주소로 바꿉니다. 실패하면 다음 단계로 진행하지 않습니다.

```powershell
py -3 tools/repo_guard.py
git init -b main
git status --short --untracked-files=all
git check-ignore --no-index .env config.json automation.json runtime/state.json releases/backup.json
git add --dry-run .
git add .
git diff --cached --name-only
py -3 tools/repo_guard.py --staged
git diff --cached --check
git commit -m "Prepare safe V1.1 source baseline"
```

5. GitHub 웹에서 새 저장소를 **Private**로 만듭니다. 이 단계는 직접 수행하며 공개로 만들지 않습니다. README/.gitignore 자동 생성은 선택하지 않습니다.
6. commit 파일목록과 GitHub Private 표시를 다시 확인하고 remote를 연결합니다. 인증은 Git Credential Manager/브라우저 로그인 등을 사용하고 토큰을 URL/명령에 넣지 않습니다.

```powershell
git remote add origin <PRIVATE_REPOSITORY_HTTPS_URL>
git ls-tree -r --name-only HEAD
git log --oneline --all
py -3 tools/repo_guard.py --history
py -3 tools/repo_guard.py --staged
git status --short
git push -u origin main
```

staged scan이 0파일인 것은 이미 commit한 HEAD가 검사됐다는 뜻이 아닙니다. 첫 commit은 직전에 검사한 index 그대로 만들고, commit 후에는 HEAD 파일 목록과 변경 없음 상태를 함께 확인해야 합니다. 기존 다른 history를 가져왔다면 본 절차로 안전을 보장할 수 없습니다.

7. GitHub 웹에서 실제 업로드된 전체 파일, config/automation 예시 안전값, .env/runtime/releases 부재를 직접 확인합니다. SOURCE_MANIFEST.json은 생성 시 소스 SHA256 목록이며 실행 중 데이터 목록이 아닙니다.

## 현재 PC에서 사용할 실행파일 경로

Codex 안의 Git/Python 경로가 일반 PowerShell PATH와 다를 수 있어, 현재 PC에서는 아래 변수를 먼저 사용합니다. 개인 사용자명을 문서에 고정하지 않습니다. 다른 PC에서는 Python/Git 설치 후 자신의 실행파일 경로를 사용하세요.

```powershell
$appDir = Join-Path $env:USERPROFILE 'Desktop\주식자동매매'
$repoDir = Join-Path $appDir 'github_source'
$pythonExe = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$gitExe = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe'
Test-Path -LiteralPath $pythonExe
Test-Path -LiteralPath $gitExe
$env:PATH = (Split-Path -Parent $gitExe) + ';' + $env:PATH
Set-Location -LiteralPath $repoDir
```

두 Test-Path 결과가 True여야 합니다. 아래 `py -3`는 `& $pythonExe -X utf8`로, `git`는 `& $gitExe`로 바꿔 실행할 수 있습니다. 예:

```powershell
& $pythonExe -X utf8 tools/repo_guard.py
& $gitExe status --short
```

프로그램 STOP/감시 중지/봇 잔여·UNKNOWN 확인 후, 아직 별도 백업을 하지 않았다면 설치본은 저장소 밖 Documents에 보존할 수 있습니다. 자격증명 외부 파일은 별도의 안전한 방식으로 보관하고 Git 폴더로 복사하지 않습니다.

```powershell
$backupDir = Join-Path $env:USERPROFILE ('Documents\주식자동매매_백업_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
Copy-Item -LiteralPath $appDir -Destination $backupDir -Recurse
```

commit에서 작성자 설정이 없다는 오류가 나면 **소스 저장소에서만** 표시 이름과 GitHub noreply 이메일을 설정하세요. 사용자 실명/실제 이메일을 임의로 넣지 않습니다.

```powershell
git config user.name '<표시 이름>'
git config user.email '<GitHub noreply 이메일>'
```

## push 직전 확인

- STOP / 감시 중지 / 예상하지 않은 주문·UNKNOWN 없음.
- 설치 폴더 전체가 아닌 github_source 안에서 작업.
- .env, 실제 config/automation, account/events/reconcile, releases/vendor/원본 API 문서 없음.
- 일반 소스·기존 테스트·합성 fixture·안전 예시만 staged.
- worktree/staged 검사 PASS, diff 확인, Private 저장소 주소 확인.
- clone 예시는 cap0/livefalse/LIVE 승인 없음. 실제 3,000원 승인과 기존 계좌 자료는 업로드되지 않음.

## 백업·버전 관리

Git commit/tag는 소스 변경을 추적합니다. releases는 현재 실계좌의 설정·runtime까지 함께 되돌아볼 로컬 증거/복구 백업이므로 Git tag로 대체하지 않습니다. 실제 events/trade_review/MFE·MAE/account snapshot은 코드와 분리해 날짜별 암호화 백업하고 복구 시험을 합니다. 운영자료를 삭제하거나 자동으로 정리하지 않습니다.
