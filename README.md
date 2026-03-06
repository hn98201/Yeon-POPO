# 🥚 달걀이론 NOBL 포트폴리오 시스템

> 코스톨라니 달걀이론 × NOBL 배당귀족 자동 포트폴리오 관리

---

## 📁 파일 구조
```
├── index.html          ← 모바일 웹 앱 (GitHub Pages)
├── update.py           ← 자동 가격/신호 업데이트 스크립트
├── portfolio.json      ← 포트폴리오 데이터 (거래내역 저장)
├── prices.json         ← 자동 생성 (건드리지 마세요)
└── .github/workflows/
    └── daily.yml       ← 매일 자동 실행 스케줄러
```

---

## 🚀 설치 방법 (5단계)

### 1️⃣ GitHub 저장소 생성
- GitHub에서 **New Repository** 생성
- Repository 이름 예: `nobl-portfolio`
- **Public** 으로 설정 (GitHub Pages 무료 사용)
- 위 파일들을 모두 업로드

### 2️⃣ GitHub Secrets 설정
Repository → Settings → **Secrets and variables** → Actions → **New repository secret**

| 이름 | 값 |
|------|---|
| `FINNHUB_KEY` | Finnhub API 키 |
| `TELEGRAM_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 텔레그램 Chat ID |

### 3️⃣ GitHub Pages 활성화
Repository → Settings → **Pages**
- Source: **Deploy from a branch**
- Branch: `main` / `/ (root)`
- Save

→ 약 1분 후 `https://[유저명].github.io/[저장소명]` 접속 가능

### 4️⃣ GitHub Actions 확인
Repository → **Actions** 탭
- `📊 포트폴리오 자동 업데이트` 워크플로우 확인
- **Run workflow** 버튼으로 즉시 실행 테스트

### 5️⃣ 웹앱 설정
`https://[유저명].github.io/[저장소명]` 접속
→ ⚙️ 설정 탭에서:
- **GitHub 토큰**: Personal Access Token 입력
  - GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
  - `repo` 권한 부여
- **저장소**: `유저명/저장소명` 입력
- **비교 시작일**: 투자 시작일 입력
- **월 투자금**: 800000 (80만원)

---

## 📱 사용 방법

### 매수 신호 받기
- 평일 오전 9시, 오후 11시 자동 체크
- 윌리엄스 %R -60 이하 종목 텔레그램 알림
- 🔴 강 (-80↓) / 🟡 중 (-70↓) / 🟢 약 (-60↓)

### 거래 입력 (수동)
1. 텔레그램 신호 확인
2. 증권사 앱에서 직접 매수
3. 웹앱 → 📝 거래 입력 탭에서 기록
4. 자동으로 GitHub에 저장

### 자동 업데이트 항목
- 현재가 (Finnhub)
- 달걀 단계 (FRED 13개 지표)
- 주봉 WR (yfinance)
- 배당금 지급 예정 알림
- 벤치마크 비교 (VOO/QQQ/NOBL)

---

## ⚙️ portfolio.json 초기 설정

처음에 아래 내용으로 portfolio.json을 수정하세요:

```json
{
  "settings": {
    "monthly_krw": 800000,
    "start_date": "2024-01-01",
    "wr_period": 14,
    "wr_threshold": -60,
    "github_repo": "유저명/저장소명"
  },
  "holdings": [],
  "transactions": [],
  "extra_deposits": []
}
```

---

## 🔔 텔레그램 봇 만드는 법

1. 텔레그램 앱 → `@BotFather` 검색
2. `/newbot` 입력 → 이름 설정
3. **Bot Token** 복사 (예: `7123456789:AAF...`)
4. 봇과 대화 시작 후 아래 URL 접속:
   `https://api.telegram.org/bot[토큰]/getUpdates`
5. `"id":` 숫자가 **Chat ID**

---

## 📊 달걀 단계별 종목 배분

| 단계 | 설명 | 주요 섹터 |
|------|------|----------|
| ① | 침체·방어 | 헬스케어 + 필수소비재 + 유틸리티 |
| ② | 회복 초기 | 필수소비재 + 헬스케어 + 금융 |
| ③ | 상승 초입 | 금융 + 산업재 + 기술 |
| ④ | 호황기 | 산업재 + 소재 + 에너지 |
| ⑤ | 과열 근접 | 소재 + 산업재 + 에너지 |
| ⑥ | 전환·하락 | 필수소비재 + 헬스케어 + 유틸리티 |

---

## ❓ 자주 묻는 질문

**Q: PC를 꺼도 되나요?**
→ 예. GitHub Actions가 클라우드에서 자동 실행됩니다.

**Q: prices.json은 언제 업데이트되나요?**
→ 평일 오전 9시, 오후 11시 (KST) 자동 업데이트

**Q: 거래 수동 입력이 번거롭지 않나요?**
→ 텔레그램 신호 → 증권사 앱 매수 → 웹앱 3초 입력으로 끝납니다.

**Q: 환율은 어떻게 적용되나요?**
→ 거래 입력 시 당시 환율을 직접 입력, 이후 평가는 실시간 환율 자동 적용
