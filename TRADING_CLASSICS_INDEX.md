# 투자 원전 9권 학술·실전 전략 레퍼런스 카탈로그 (TRADING CLASSICS INDEX)

## 0. 개요 및 목적
본 문서는 `C:\Users\MJS\Documents\주식책`에 보관된 세계 최고 수준의 투자 원전 9권의 이론 체계, 수식 모델, 시장 미시구조 원리를 체계적으로 정리하고, 한국 주식시장(KRX) 장중 자동매매 엔진(주식자동매매 v1.71)의 핵심 병목 현상(매수 기회 극심한 부족, 노이즈 손절, 10초 가속비 불합리 등)을 해결하기 위한 실전 알고리즘 매핑 명세서이다.

---

## 1. 수록 원전 마스터 목록 (9 Books Master Catalog)

| 번호 | 도서명 (Title) | 저자 (Author) | 파일명 (Filename) | 파일 크기 | 전체 페이지 | 텍스트 추출 상태 | 핵심 기여 분야 |
| :---: | :--- | :--- | :--- | :---: | :---: | :---: | :--- |
| **1** | Technical Analysis of the Financial Markets | John J. Murphy | `Technical Analysis of the Financial Markets by John J. Murphy.pdf` | 17,449,477 bytes | 501 pages | Text OK (Outline 포함) | 추세 정의, 지지·저항, 지지저항 역할 반전(Role Reversal), 거래량 확증 |
| **2** | Trading for a Living | Alexander Elder | `epdf.pub_trading-for-a-living.pdf` | 5,820,933 bytes | 306 pages | Text OK | 삼중창(Triple Screen) 기법, 2% 계좌 위험 규칙, 포스 인덱스(Force Index) |
| **3** | How to Make Money in Stocks | William J. O'Neil | `How to Make Money in Stocks - William J. O'Neil.pdf` | 3,885,254 bytes | 142 pages | Text OK | CAN SLIM, 피벗 돌파(Pivot Point), 거래량 급증(+40~50% 이상) |
| **4** | Way of the Turtle | Curtis Faith | `Way of the Turtle.pdf` | 989,569 bytes | 313 pages | Text OK (Depth config 조정) | 변동성 기반 N(ATR) 포지션 사이징, 돈키언 채널 돌파, 기대값(E-ratio) |
| **5** | Market Wizards | Jack D. Schwager | `Market-Wizards.pdf` | 1,219,582 bytes | 165 pages | Text OK (Outline 포함) | 비대칭 손익비(Risk/Reward Asymmetry), 심리 통제, 손절의 불가역성 |
| **6** | 기술적 분석은 처음이지 | 저자 미상 (국내 실전서) | `기술적 분석은 처음이지.pdf` | 8,834,844 bytes | 269 pages | Text OK (OCR 완료) | 한국 주식시장 수평 매물대, 종가 마감 원칙, 캔들 중심값, 지지선 침범 규칙 |
| **7** | The Evaluation and Optimization of Trading Strategies | Robert Pardo | `Robert Pardo The Evaluation and Optimization of Trading Strategies.pdf` | 3,390,510 bytes | 367 pages | Text OK (Outline 포함) | 전진 분석(Walk-Forward Analysis), 과최적화(Overfitting) 방지, 견고성 검정 |
| **8** | Evidence-Based Technical Analysis | David Aronson | `evidence-based-technical-analysis.pdf` | 40,751,320 bytes | 544 pages | Text OK | 데이터 마이닝 편향(Data-Mining Bias), 통계적 유의성(p-value), 객관적 규칙 검증 |
| **9** | Trading and Exchanges: Market Microstructure for Practitioners | Larry Harris | `Trading-Exchanges-Market-Microstructure-Practitioners Draft Copy.pdf` | 620,148 bytes | 113 pages | Text OK (Outline 포함) | 호가창(LOB) 미시구조, 스프레드 비용, 역선택(Adverse Selection), 체결 슬리피지 |

---

## 2. 권별 심층 이론 및 알고리즘 변환 명세 (Deep Theoretical & Practical Mapping)

### Book 1: John J. Murphy — Technical Analysis of the Financial Markets
- **원전 핵심 이론:**
  - **추세(Trend)의 본질:** 다우 이론에 기초한 고점(Peak)과 저점(Trough)의 점진적 상승(Higher Highs & Higher Lows)이 유지되는 한 상승 추세로 정의.
  - **지지·저항의 역할 반전 (Role Reversal, Ch. 4):** 명확한 저항선이 상향 돌파되면, 이후 발생하는 되돌림(Pullback/Throwback)에서 과거의 저항선은 강력한 지지선으로 역할이 전환된다.
  - **거래량 확증 (Volume Confirmation, Ch. 7):** 거래량은 가격 추세와 동행해야 한다. 즉, 저항 돌파 시 거래량이 수반되어야 하며, 눌림목 과정에서는 거래량이 마르고(Dry-up), 지지선 반등 시 다시 거래량이 증가해야 진짜 돌파다.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* Setup B가 무리하게 1분봉 종가 1.2% 급등을 강제하여 정상적인 지지저항 전환 눌림목을 `IMPULSE_CLOSE_PROGRESS_INSUFFICIENT`로 전량 기각.
  - *알고리즘 구현:* 직전 확인된 저항 피벗 $R$ 돌파 후, $R \pm 0.3\%$ 구역으로 되돌림이 발생하고 지지 확인 캔들(반등 틱) 출현 시 매수 진입하는 `RoleReversalStrategy` 구현.

---

### Book 2: Alexander Elder — Trading for a Living
- **원전 핵심 이론:**
  - **삼중창 거래 시스템 (Triple Screen, Ch. 9):**
    - 제1창 (조류/Tide): 상위 타임프레임(예: 5분봉/15분봉)의 추세 확인 (EMA 기울기 양수, MACD 히스토그램 상승).
    - 제2창 (파도/Wave): 중기 타임프레임(예: 1분봉)의 역추세 눌림목 확인 (Stochastic / Force Index 침체).
    - 제3창 (물결/Ripples): 단기 틱/초봉 단위의 브레이크아웃 진입 (직전 고점 돌파 트레일링 매수 스톱).
  - **2% 계좌 위험 규칙 (The 2% Rule, Ch. 10):**
    - 단일 거래에서 감수하는 최대 손실액은 전체 계좌 순자산(Equity)의 2% 이내로 제한.
    - **핵심 공식:** $\text{Position Size} = \frac{\text{Equity} \times 0.02}{\text{Entry Price} - \text{Structural Stop Price}}$
    - *주의:* 손절선을 진입가 대비 2% 아래에 두는 것이 아니라, 구조적 무효화 선(지지선 아래)에 손절을 두고 그 거리에 맞춰 수량을 조절하는 것임!
  - **포스 인덱스 (Force Index):** $\text{Force Index}(1) = \text{Volume} \times (\text{Close} - \text{Close}_{-1})$.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* 아리바이오홀딩스(`290690`) 손절 사건처럼, 구조선이 진입가 대비 불과 1.2% 아래(노이즈 밴드 이내)에 위치하여 32.7초 만에 손절됨.
  - *알고리즘 구현:* 엘더의 공식에 따라 손절 폭은 노이즈 밴드($1.5 \times \text{Noise}$) 이상으로 충분히 이격시키고, 2% 위험 한도 내에서 수량을 수학적으로 역산하는 `ElderRiskSizer` 구현.

---

### Book 3: William J. O'Neil — How to Make Money in Stocks
- **원전 핵심 이론:**
  - **피벗 돌파 (Pivot Point Breakout):** 탄탄한 베이스(횡보 수렴, 컵위드핸들 등)의 최상단 저항선을 상향 돌파하는 순간이 최적의 매수 타점.
  - **거래량 급증 (Volume Expansion):** 피벗 돌파 시점의 거래량은 평소 평균 거래량 대비 **최소 +40% ~ +50% 이상, 이상적으로는 +100% 이상 폭증**해야 함.
  - **추격 매수 제한:** 피벗 가격 대비 +5% 이상 상승한 위치에서는 절대 신규 매수 금지 (Chasing 엄격 차단).
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* Setup A의 10초 거래대금 가속비 3.0 배수(`min_turnover_ratio=3.0`)는 틱 단위 분산으로 인해 진짜 분봉 대량 거래량 돌파를 `ACCELERATION_FAIL`로 왜곡 기각.
  - *알고리즘 구현:* 오닐의 원칙에 따라 1분봉 거래량이 최근 5개 완성봉 평균 거래량 대비 $\ge 150\%$ 이상 급증하고 피벗을 상향 돌파할 때 진입하는 `ONeilPivotBreakout` 구현.

---

### Book 4: Curtis Faith — Way of the Turtle
- **원전 핵심 이론:**
  - **N (ATR, Average True Range) 변동성 측정:**
    - $\text{TR} = \max(H - L, |H - C_{-1}|, |L - C_{-1}|)$
    - $N = \text{EMA}(\text{TR}, 20)$
  - **변동성 기반 단위 사이징 (Volatility Sizing Unit):**
    - $1 \text{ Unit} = \frac{1\% \text{ of Account Equity}}{N \times \text{Point Value}}$
  - **손절선 설정:** 진입가 대비 $2N$ (터틀 기본) 또는 $1/2N \sim 1N$ (타이트 스톱).
  - **기대값(Expectancy)과 E-Ratio:** 에지가 있는 시스템은 장기적으로 $E > 0$을 증명해야 함.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* Setup C의 박스권 조건이 고정 `1.5%` 이하로 고정되어 있어 시장 변동성에 적응하지 못함.
  - *알고리즘 구현:* 동적 ATR($N$)을 산출하여 박스권 허용 폭을 $\text{Box Range} \le 1.8 \times N$으로 동적화하고, 변동성 기반 위험 관리를 수행하는 `TurtleVolatilityManager` 구현.

---

### Book 5: Jack D. Schwager — Market Wizards
- **원전 핵심 이론:**
  - **비대칭 위험-보상 (Asymmetric Risk-Reward):** 톱 트레이더(폴 튜더 존스, 에드 세이코타 등)는 1을 걸고 최소 3~5를 버는 기회에만 집중.
  - **방어 최우선 (Defense First):** 수익보다 자본 보존이 10배 중요하다. 포지션 진입 전 반드시 탈출할 손절가를 확정한다.
  - **즉각적 청산:** 진입 후 시장이 예상대로 즉시 강하게 반응하지 않으면 미련 없이 나온다.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* 순 손익비(Net RR)가 제반 비용(세금 0.18%, 수수료, 슬리피지 2회) 감안 시 1.5 미만으로 떨어지는 부실 타점을 걸러내지 못하거나, 반대로 억지 RR 계산으로 기회를 날림.
  - *알고리즘 구현:* 현실적 수수료·세금·슬리피지를 정밀 차감한 실제 $\text{Net RR} \ge 1.5$를 필터링하는 `AsymmetricRiskFilter` 적용.

---

### Book 6: 기술적 분석은 처음이지 (국내 실전서)
- **원전 핵심 이론:**
  - **한국 주식시장 수평 매물대:** 직전 고점/저점, 라운드피겨, 당일 시가 및 상한가 파동 매물대.
  - **지지와 저항의 침범 규칙:** 지지선은 많이 닿을수록 신뢰도가 높아지나, 여러 번 깊게 침범(훼손)되면 폐기한다.
  - **캔들 중심값 지지:** 장대 양봉의 50% 중심값(Midpoint = $(H+L)/2$)이 강력한 지지선으로 작용한다.
  - **종가 마감(Close Confirmation):** 꼬리(Wick)에 의한 일시적 돌파나 이탈에 속지 말고, 완성봉의 종가 마감으로 지지/돌파를 확정한다.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* 틱 단위 1개 체결로 섣불리 돌파를 판정하거나, 장대 양봉의 50% 중심값 지지라는 강력한 국내 패턴을 미활용.
  - *알고리즘 구현:* 장대 양봉 중심값 지지 확인 및 수평 매물대 기반 1분봉 종가 안착을 검증하는 `KoreanMicroSupport` 모듈 구현.

---

### Book 7: Robert Pardo — The Evaluation and Optimization of Trading Strategies
- **원전 핵심 이론:**
  - **전진 분석 (Walk-Forward Analysis, WFA):** 데이터를 인샘플(IS)과 아웃오브샘플(OOS)로 번갈아 분할하여 최적화 및 실전 검증 수행.
  - **과최적화(Overfitting)의 위험:** 매개변수가 너무 많거나, 좁은 데이터 구간에 맞추면 실전에서 필연적으로 붕괴.
  - **견고성 지표 (WFE, Walk-Forward Efficiency):** $\text{WFE} = \frac{\text{OOS Annualized Return}}{\text{IS Annualized Return}} > 50\%\sim 60\%$.
- **알고리즘 매핑:** 파라미터를 최소화(3개 이하)하고 과거 틱/봉 리플레이를 통해 롤링 전진 검증을 수행하는 프레임워크 구현.

---

### Book 8: David Aronson — Evidence-Based Technical Analysis
- **원전 핵심 이론:**
  - **데이터 마이닝 편향 (Data-Mining Bias):** 수천 개의 규칙을 테스트하면 순수한 무작위 데이터에서도 우연히 돈을 버는 규칙이 발견됨.
  - **객관적 가설 검정:** 진입 및 청산 규칙은 주관적 해석의 여지가 전혀 없는 100% 결정론적(Deterministic) 코드로 명문화되어야 함.
  - **통계적 유의성:** 벤치마크(무작위 무수익 모델) 대비 p-value < 0.05 검증.
- **알고리즘 매핑:** 모든 진입/청산 조건을 불리언 게이트(Boolean Gates) 및 단일 결정론적 평가 함수로 구현하여 재현성 100% 확보.

---

### Book 9: Larry Harris — Trading and Exchanges: Market Microstructure for Practitioners
- **원전 핵심 이론:**
  - **호가창(Limit Order Book)과 스프레드:** 매수호가와 매도호가 간격(Spread)은 즉시성의 대가(Cost of Immediacy).
  - **역선택 (Adverse Selection):** 지정가 주문이 즉시 체결되면 정보 우위자(Informed Trader)의 투매일 가능성이 높음.
  - **실제 순손익 모델:** $\text{Net PnL} = \text{Gross Gain} - \text{Round-trip Commissions} - \text{Securities Tax}(0.18\%) - 2 \times \text{Half-Spread} - 2 \times \text{Slippage}$.
- **기존 봇 결함 및 개선 방향:**
  - *기존 결함:* 체결·호가 동기화 지연으로 인한 2,455회 거절 중 정상적인 호가 갱신 시차를 결함으로 오인 차단한 부분.
  - *알고리즘 구현:* 최신 호가 잔량과 스프레드를 선제적으로 점검하고, 비용을 차감한 실제 기대수익을 계산하는 `MicrostructureLiquidityGate` 구현.

---

## 3. 원전 이론 $\rightarrow$ AG 알고리즘 아키텍처 매핑 요약

```
[원전 이론 (Books Theory)]              [주식자동매매 DEV 알고리즘 (Classics Strategy Engine)]
─────────────────────────────────────────────────────────────────────────────────────────────
1. Murphy/O'Neil 거래량 돌파    ───>   ClassicsBreakoutStrategy (1분봉 거래량 >= 1.5x 5봉평균, 피벗 돌파)
2. Murphy/Elder/국내 지지저항반전───>   ClassicsRoleReversalStrategy (돌파 저항선 리테스트 지지 확인 매수)
3. Elder 2% & Turtle N(ATR)    ───>   ClassicsRiskSizer (구조선 이탈 위험 = 계좌 2% 한도 동적 사이징)
4. Larry Harris 시장 미시구조   ───>   ClassicsMicrostructureGate (스프레드, 호가 깊이, 세금·슬리피지 Net RR)
5. Pardo / Aronson 전진 검증    ───>   ClassicsWalkForwardEvaluator (과거 틱 리플레이 객관적 통계 검증)
```

---

## 4. 라이브 운영 및 안전 수칙 (Absolute Safety Constraints)
1. **HARD_LOCK 3종목 절대 보호:** `005930`(삼성전자), `017900`(광전자), `052260`(현대바이오랜드)는 어떠한 신규 전략에서도 매매 대상에서 완벽히 배제한다.
2. **단일 거래 2% / 당일 4% 손실 한도:** 엘더와 터틀의 철칙에 따라 누적 손실이 4%에 도달하면 당일 매매를 영구 정지한다.
3. **순 손익비(Net RR) 1.5 이상:** 해리스의 미시구조 비용을 모두 차감한 순 기대 손익비가 1.5 미만인 거래는 원천 차단한다.
4. **15:10 당일 전량 청산:** 오버나잇 위험을 회피하기 위해 정규장 마감 전 잔여 포지션을 모두 정리한다.
5. **독립 브랜치 개발:** 본 연구 및 전략 코드는 `feat/classics-strategy-dev` 브랜치에 독립 격리하며, 15:35 자동 배포 대상인 `be52416` 운영본에 일체 영향을 주지 않는다.
