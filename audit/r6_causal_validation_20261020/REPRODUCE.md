# 오프라인 재현 및 다음 장 증거 수집

운영 프로그램을 시작하거나 중지하는 명령은 없다. 모든 출력은 저장소·LIVE 밖의 비공개 폴더로 지정한다. 경로 예시의 자리표시자는 실제 경로로 대체한다.

```powershell
python -B -m unittest tests.test_causal_replay tests.test_local_breakout_strategy tests.test_local_breakout_e2e_integration tests.test_research_evidence_collector tests.test_shadow_clock_consistency tests.test_observation_reaction
python -B tools/run_all_replays.py --output-dir <private-output>
```

캐시만 읽은 결과는 UNKNOWN이다. 최초 89건 및 계좌 5건의 원형은 인수 커밋 `8174b5d`에 남아 있다. 실행 시 import 부작용이 있는 원형을 현재 연구 모듈과 혼합하지 않는다.

위 여섯 모듈의 마지막 표적 검사 결과는 65 PASS다. 전체 회귀검사를 다시 완료한 결과가 아니다. 정확한 상태·원본 경로·AG 후속 순서는 [최종 인계](HANDOFF_TO_AG.md)를 따른다.

실측 재생:

```powershell
python -B tools/account_level_replay.py --ticks <oldest-journal> <next-journal> --capture <capture-file-1> <capture-file-2> --day 2026-10-20 --config <private-frozen-config> --automation <private-frozen-automation> --initial-cash <verified-starting-cash> --output <private-result.json>
```

저널은 회전 순서로 전달한다. `--initial-cash`는 명시해야 한다. 봇 자본 한도는 입력 automation에서 가져오고, 다른 한도를 비교할 때만 `--approved-cap`을 명시한다. `--latency-ms 500`, `--adverse-ticks 1`은 **가정에 따른 민감도**다. ask가 BUY 지정가를 넘으면 체결을 만들지 않는다. `--clock-basis receiver_counterfactual`은 처리 지연이 없었다는 가정이며 당시 LIVE 성능이 아니다. 전역 시장 단계 3, 최초 PAPER 장부, 휴리스틱 효율성 입력 및 호가 잔량 체결 가정도 결과에 표시한다. 실제 접수 여부나 미공개 거래소 대기열은 복원하지 못한다.

다음 장의 독립 수집기:

```powershell
python -B -m research.evidence_collector --source-root <existing-live-directory> --output <separate-private-directory> --duration-seconds 21600
```

현재 EOF 이후만 읽는다. 이미 존재하는 원본을 별도로 복사하는 오프라인 검증에만 `--include-existing`을 쓴다. LIVE의 시세·구독·포지션 보호 경로에는 코드를 추가하지 않는다. 후보는 사후 상승률이 아니라 기존 OPPORTUNITY_SAMPLE의 최초 형상 발생 순서로 선택한다.

한도: 동시 관측 에피소드 2개, 관측 창 30분, 메모리 링 1MiB, 쓰기 큐 2MiB, 파일 4MiB, 일 16MiB, 전체 64MiB. 관측 창 종료는 시간 청산이 아니다. 실제 원본의 누락·회전·용량 초과는 UNKNOWN이며, 수집기가 없는 과거 틱이나 호가를 채우지 않는다. 원본이 늦거나 빠지면 수집기로 해결됐다고 주장하지 않는다. 상태·손실 카운터는 출력 폴더에 남긴다.

수집기를 자동으로 시작하는 예약 작업은 만들지 않았다. 현재 전략은 이 연구 도구를 사용하거나 주문에 연결하지 않는다.
