"""
달걀이론 포트폴리오 자동 업데이터 (yfinance 버전)
- 윌리엄스 %R: 주봉(Weekly) 14기간 기준
- 환율, 가격, 벤치마크 모두 야후 파이낸스 사용
- FRED 지표 수집 로직 수정 (컬럼명 자동 대응)
"""

import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from io import StringIO
import yfinance as yf
import pandas as pd

# ── 설정
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
MONTHLY_BUDGET = int(os.environ.get('MONTHLY_BUDGET', '800000'))
START_DATE     = os.environ.get('START_DATE', '2024-01-01')
WR_THRESHOLD   = float(os.environ.get('WR_THRESHOLD', '-60'))
KST = ZoneInfo('Asia/Seoul')

# ── NOBL 유니버스 (섹터별)
NOBL_UNIVERSE = {
    'KO':'필수소비재','PG':'필수소비재','PEP':'필수소비재','CL':'필수소비재',
    'KMB':'필수소비재','MKC':'필수소비재','GIS':'필수소비재','SYY':'필수소비재',
    'WMT':'필수소비재','TGT':'필수소비재','HRL':'필수소비재','CLX':'필수소비재',
    'ADM':'필수소비재','CPB':'필수소비재','SJM':'필수소비재','CHD':'필수소비재',
    'EMR':'산업재','SWK':'산업재','EXPD':'산업재','DOV':'산업재','GPC':'산업재',
    'PNR':'산업재','ITT':'산업재','TXN':'기술','IBM':'기술','ADP':'기술',
    'VFC':'경기소비재','LOW':'경기소비재','TROW':'금융','BEN':'금융'
}

def get_fred_indicators_v2():
    """FRED CSV 직접 다운로드 방식 (Surgically Fixed)"""
    indicators = {
        'fed_rate': 'FEDFUNDS', 'spread': 'T10Y2Y', 'vix': 'VIXCLS',
        'unemp': 'UNRATE', 'cpi_yoy': 'CPIAUCSL', 'm2_yoy': 'M2SL',
        'pmi': 'UMCSENT', 'claims': 'ICSA', 'hy_spread': 'BAMLH0A0HYM2'
    }
    results = {}
    
    for key, series_id in indicators.items():
        try:
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                df = pd.read_csv(StringIO(resp.text))
                # VALUE 컬럼이 아닌 데이터가 들어있는 두 번째 컬럼을 선택
                val_col = df.columns[1] 
                last_val = df[val_col].iloc[-1]
                
                # 결측치('.') 처리 및 실수 변환
                if last_val == '.':
                    last_val = df[val_col].iloc[-2]
                results[key] = float(last_val)
                print(f"  ✅ {key} ({series_id}) = {results[key]}")
            else:
                print(f"  ❌ {key} ({series_id}) 실패 (HTTP {resp.status_code})")
        except Exception as e:
            print(f"  ❌ {key} ({series_id}) 에러: {e}")
    return results

def calculate_egg_stage(ind):
    """지표 기반 달걀 단계 결정 로직"""
    score = 0
    if not ind: return {'stage': 1, 'score': 0, 'desc': "데이터 부족 (기본 1단계)", 'indicators': {}}
    
    if ind.get('fed_rate', 0) > 3.0: score += 1
    if ind.get('spread', 0) < 0: score += 2
    if ind.get('vix', 0) > 25: score += 1
    if ind.get('unemp', 0) > 5.0: score += 1
    
    stage = 1
    desc = "금리 인하 시작 (A점 통과)"
    if score >= 4: stage, desc = 4, "금리 인상 정점 (D점 통과)"
    elif score >= 3: stage, desc = 3, "저금리 지속 (C점 통과)"
    elif score >= 2: stage, desc = 2, "금리 최저점 (B점 통과)"
    
    return {'stage': stage, 'score': score, 'desc': desc, 'indicators': ind}

def get_fx_rate():
    try:
        data = yf.download("USDKRW=X", period="1d")
        return float(data['Close'].iloc[-1])
    except:
        return 1300.0

def get_benchmark_data():
    bm = {}
    for ticker in ['VOO', 'QQQ', 'NOBL']:
        try:
            d = yf.download(ticker, period="1y")
            bm[ticker] = round(float(d['Close'].iloc[-1]), 2)
        except: bm[ticker] = 0
    return bm

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'})

def build_signal_message(signals, fx, months):
    lines = [f"<b>🔔 달걀이론 매수 신호 ({months}개월차)</b>\n"]
    for s in signals:
        lines.append(f"• {s['ticker']} ({s['strength']}매수): ${s['recommend_usd']:.2f} (약 {s['recommend_krw']:,}원)")
    return "\n".join(lines)

def main():
    now_kst = datetime.now(KST)
    print(f"🚀 업데이트 시작: {now_kst}")

    # 1. 환율
    fx_rate = get_fx_rate()
    print(f"1. 환율: {fx_rate}")

    # 2. 경제 지표 및 달걀 단계
    print("2. 경제 지표 수집 중...")
    ind_data = get_fred_indicators_v2()
    egg = calculate_egg_stage(ind_data)
    print(f"   결과: {egg['stage']}단계 ({egg['desc']})")

    # 3. NOBL 신호 분석
    print("3. 신호 분석 중...")
    active_signals = []
    current_30 = []
    for ticker in NOBL_UNIVERSE.keys():
        try:
            df = yf.download(ticker, period="1y", interval="1wk", progress=False)
            if df.empty: continue
            
            # WR 계산
            high = df['High'].rolling(14).max()
            low = df['Low'].rolling(14).min()
            wr = -100 * (high - df['Close']) / (high - low)
            current_wr = float(wr.iloc[-1])
            curr_p = float(df['Close'].iloc[-1])
            
            # 신호 강도
            strength = ""
            if current_wr <= -80: strength = "강"
            elif current_wr <= -70: strength = "중"
            elif current_wr <= WR_THRESHOLD: strength = "약"
            
            if strength:
                # 8.10달러와 같은 추천 금액 계산 (단순 예시 로직)
                rec_usd = 8.10 if strength == "약" else (15.0 if strength == "중" else 25.0)
                active_signals.append({
                    'ticker': ticker, 'strength': strength, 'wr': current_wr,
                    'recommend_usd': rec_usd, 'recommend_krw': int(rec_usd * fx_rate)
                })
            current_30.append({'ticker': ticker, 'price': round(curr_p, 2), 'wr': round(current_wr, 1)})
        except: continue

    # 4. 포트폴리오 요약 (portfolio.json 기반)
    holdings = []
    portfolio_summary = {}
    try:
        with open('portfolio.json', 'r', encoding='utf-8') as f:
            port = json.load(f)
            total_val_usd = 0
            for h in port.get('holdings', []):
                ticker = h['ticker']
                curr_p = yf.download(ticker, period="1d", progress=False)['Close'].iloc[-1]
                val_usd = float(curr_p) * h['shares']
                pnl = (float(curr_p) / h['avg_price_usd'] - 1) * 100
                
                holdings.append({
                    'ticker': ticker, 'sector': h['sector'], 'shares': h['shares'],
                    'avg_price_usd': h['avg_price_usd'], 'current_price': float(curr_p),
                    'current_value_usd': round(val_usd, 2), 'current_value_krw': int(val_usd * fx_rate),
                    'pnl_pct': round(pnl, 2)
                })
                total_val_usd += val_usd
            
            portfolio_summary = {
                'total_value_usd': round(total_val_usd, 2),
                'total_value_krw': int(total_val_usd * fx_rate),
                'total_pnl_pct': 0 # 필요시 계산 추가
            }
    except Exception as e: print(f"4. 포트폴리오 계산 오류: {e}")

    # 5. 저장
    output = {
        'updated_at': now_kst.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate': fx_rate,
        'egg': egg,
        'active_signals': active_signals,
        'holdings': holdings,
        'portfolio_summary': portfolio_summary,
        'benchmarks': get_benchmark_data()
    }
    
    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("✅ prices.json 저장 완료")

    # 6. 알림
    if active_signals:
        send_telegram(build_signal_message(active_signals, fx_rate, 1))

if __name__ == "__main__":
    main()
