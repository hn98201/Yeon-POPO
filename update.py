"""
달걀이론 포트폴리오 자동 업데이터 (yfinance 버전)
- 윌리엄스 %R: 주봉(Weekly) 14기간 기준
- 환율, 가격, 벤치마크 모두 야후 파이낸스 사용
- API 키 불필요 (Telegram 제외)
"""

import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
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
    'ADM':'필수소비재',
    'CAT':'산업재','EMR':'산업재','ITW':'산업재','DOV':'산업재',
    'GWW':'산업재','SWK':'산업재','CHRW':'산업재','EXPD':'산업재',
    'CTAS':'산업재','ROL':'산업재','FAST':'산업재','NDSN':'산업재',
    'CB':'금융','CINF':'금융','AFL':'금융','BEN':'금융','SPGI':'금융','AMP':'금융','AON':'금융',
    'NUE':'소재','PPG':'소재','SHW':'소재','LIN':'소재','ECL':'소재','APD':'소재','ALB':'소재',
    'JNJ':'헬스케어','ABT':'헬스케어','BDX':'헬스케어','ABBV':'헬스케어','CAH':'헬스케어','MDT':'헬스케어',
    'LOW':'경기소비재','GPC':'경기소비재',
    'AWK':'유틸리티','ATO':'유틸리티','NFG':'유틸리티',
    'FRT':'부동산','ESS':'부동산','O':'부동산',
    'IBM':'기술','TXN':'기술',
    'XOM':'에너지','CVX':'에너지',
}

STAGE_SECTORS = {
    1: ['에너지','소재','산업재'],
    2: ['산업재','금융','기술'],
    3: ['금융','산업재','기술'],
    4: ['기술','헬스케어','경기소비재'],
    5: ['헬스케어','경기소비재','부동산'],
    6: ['필수소비재','헬스케어','유틸리티'],
}

# ══════════════════════════════════════════
# 야후 파이낸스 데이터 조회
# ══════════════════════════════════════════
def get_weekly_wr(symbol: str, periods: int = 14) -> float | None:
    """yfinance 주봉 기준 Williams %R 계산"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo", interval="1wk")
        if len(hist) < periods:
            return None
        highs  = hist['High'][-periods:]
        lows   = hist['Low'][-periods:]
        closes = hist['Close'][-periods:]
        
        h14 = highs.max()
        l14 = lows.min()
        c   = closes.iloc[-1]
        
        if h14 == l14: return None
        wr = (h14 - c) / (h14 - l14) * -100
        return round(wr, 1)
    except Exception as e:
        print(f"  WR 계산 실패 {symbol}: {e}")
        return None

def get_price(symbol: str) -> dict:
    """yfinance 현재가 조회"""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = info.last_price
        prev = info.previous_close
        change = price - prev
        pct = (change / prev) * 100 if prev else 0
        return {
            'price': round(price, 2),
            'prev': round(prev, 2),
            'change': round(change, 2),
            'pct': round(pct, 2)
        }
    except:
        return {'price': 0, 'prev': 0, 'change': 0, 'pct': 0}

def get_fx_rate() -> float:
    """yfinance 원/달러 환율 조회"""
    try:
        ticker = yf.Ticker("KRW=X")
        return round(ticker.fast_info.last_price, 2)
    except:
        return 1350.0  # 기본값

def get_benchmark_data() -> dict:
    """벤치마크(VOO, QQQ, NOBL) 수익률 비교 데이터 생성"""
    benchmarks = {}
    tickers = ['VOO', 'QQQ', 'NOBL']
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period="1y", interval="1d")
            if not hist.empty:
                dates = hist.index.strftime('%Y-%m-%d').tolist()
                base_price = hist['Close'].iloc[0]
                pcts = ((hist['Close'] / base_price) - 1) * 100
                benchmarks[t] = {
                    'dates': dates,
                    'pct': [round(p, 2) for p in pcts.tolist()]
                }
        except:
            pass
    return benchmarks

# ══════════════════════════════════════════
# 경제 지표 및 달걀 단계 (FRED API 크롤링)
# ══════════════════════════════════════════
def get_economic_indicators() -> dict:
    ind = {}
    def parse_fred_latest(url: str):
        try:
            r = requests.get(url, timeout=10)
            lines = [line.strip() for line in r.text.strip().split('\n') if ',' in line]
            for line in reversed(lines[1:]):
                parts = line.split(',')
                if len(parts) >= 2:
                    val = parts[1].strip()
                    if val and val != '.':
                        return float(val)
        except: pass
        return None

    ind['fed_rate'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS')
    ind['spread'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y')
    ind['vix'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS')
    ind['unemp'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE')
    return ind

def calc_egg_stage(ind: dict) -> dict:
    """간이 경제 지표 스코어링"""
    spread = ind.get('spread', 0)
    fed_rate = ind.get('fed_rate', 5.0)
    
    stage = 3 # 기본값
    if fed_rate > 4.5 and spread < 0: stage = 6
    elif fed_rate > 4.5 and spread >= 0: stage = 5
    elif fed_rate <= 4.5 and spread >= 1.0: stage = 4
    elif fed_rate <= 3.0: stage = 3
    elif spread < -0.5: stage = 2
    else: stage = 1
    
    descs = {
        1: "① 하락 초입", 2: "② 하락 본격", 3: "③ 상승 초입",
        4: "④ 상승 본격", 5: "⑤ 과열 초입", 6: "⑥ 과열 본격"
    }
    return {'stage': stage, 'score': round(stage * 1.5, 1), 'desc': descs.get(stage, "알 수 없음")}

# ══════════════════════════════════════════
# 포트폴리오 유틸리티
# ══════════════════════════════════════════
def select_30(stage: int) -> list:
    """현재 달걀 단계에 맞는 섹터 위주로 30종목 선정"""
    pref_sectors = STAGE_SECTORS.get(stage, [])
    selected = [t for t, s in NOBL_UNIVERSE.items() if s in pref_sectors]
    others = [t for t, s in NOBL_UNIVERSE.items() if s not in pref_sectors]
    
    result = selected + others
    return result[:30]

def months_allocated(start_date: str) -> int:
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d')
        now = datetime.now()
        months = (now.year - start.year) * 12 + now.month - start.month
        return max(0, months)
    except:
        return 0

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': TELEGRAM_CHAT, 'text': msg})
    except: pass

def build_signal_message(signals: list, fx_rate: float, months: int) -> str:
    msg = f"🔔 달걀이론 매수 신호 알림\n"
    msg += f"환율: ₩{fx_rate:,.0f} | 투자경과: {months}개월\n\n"
    for s in signals:
        strength = "🔴 강매수" if s['wr'] <= -80 else "🟡 중매수" if s['wr'] <= -70 else "🟢 약매수"
        msg += f"{strength} {s['ticker']} (${s['price']:.2f})\n"
        msg += f"WR: {s['wr']:.1f} | 섹터: {s['sector']}\n\n"
    msg += "웹앱에서 '✅ 매수' 또는 '⏭ PASS'를 처리해 주세요."
    return msg

# ══════════════════════════════════════════
# 메인 실행 로직
# ══════════════════════════════════════════
def main():
    now_kst = datetime.now(KST)
    print(f"[{now_kst.strftime('%Y-%m-%d %H:%M')} KST] 야후 파이낸스 데이터 업데이트 시작")

    print("1. 경제 지표 조회 중...")
    ind = get_economic_indicators()
    egg = calc_egg_stage(ind)
    print(f"  -> 달걀 {egg['stage']}단계")

    print("2. 환율 조회 중...")
    fx_rate = get_fx_rate()
    print(f"  -> 환율: ₩{fx_rate:,.0f}")

    print("3. 종목 가격 및 WR 계산 중 (yfinance)...")
    price_data = {}
    current_30 = select_30(egg['stage'])
    
    # 30종목 위주로 조회하여 속도 최적화
    for ticker in current_30:
        time.sleep(0.5) # API 호출 제한 방지
        p  = get_price(ticker)
        wr = get_weekly_wr(ticker)
        price_data[ticker] = {**p, 'wr': wr}
        print(f"  {ticker}: ${p['price']} | WR {wr}")

    print("4. 신호 발생 여부 확인...")
    months = months_allocated(START_DATE)
    active_signals = []
    
    for ticker in current_30:
        p  = price_data.get(ticker, {})
        wr = p.get('wr')
        if wr is not None and wr <= float(WR_THRESHOLD):
            active_signals.append({
                'ticker': ticker,
                'sector': NOBL_UNIVERSE.get(ticker, '--'),
                'wr':     wr,
                'price':  p.get('price', 0),
                'pct':    p.get('pct', 0),
            })
    
    active_signals.sort(key=lambda x: x['wr'])
    print(f"  -> 신호 발생: {len(active_signals)}개")

    print("5. 포트폴리오 요약(기존 데이터 병합)...")
    portfolio_summary = {'total_value_krw': 0, 'total_value_usd': 0, 'total_pnl_pct': 0}
    holdings = []
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r') as f:
                pf = json.load(f)
            
            for h in pf.get('holdings', []):
                ticker = h['ticker']
                p = price_data.get(ticker, get_price(ticker)) # 30종목 외 보유종목 추가 조회
                cur_price = p.get('price', 0)
                avg_price = h.get('avg_price_usd', 0)
                shares    = h.get('shares', 0)
                cur_val_usd = cur_price * shares
                cur_val_krw = cur_val_usd * fx_rate
                pnl_pct     = (cur_price/avg_price - 1)*100 if avg_price > 0 else 0
                
                holdings.append({
                    'ticker':           ticker,
                    'sector':           h.get('sector', NOBL_UNIVERSE.get(ticker, '--')),
                    'shares':           shares,
                    'avg_price_usd':    avg_price,
                    'current_price':    cur_price,
                    'current_value_krw':round(cur_val_krw),
                    'current_value_usd':round(cur_val_usd, 2),
                    'pnl_pct':          round(pnl_pct, 2),
                    'day_change_pct':   p.get('pct', 0),
                    'wr':               p.get('wr'),
                })
                
            total_val_usd = sum(h['current_value_usd'] for h in holdings)
            total_cost    = sum(h['shares']*h['avg_price_usd'] for h in holdings)
            pnl_pct = (total_val_usd/total_cost - 1)*100 if total_cost > 0 else 0
            
            portfolio_summary = {
                'total_value_usd': round(total_val_usd, 2),
                'total_value_krw': round(total_val_usd * fx_rate),
                'total_pnl_pct':   round(pnl_pct, 2),
            }
    except Exception as e:
        print(f"  portfolio.json 로드 오류: {e}")

    print("6. 벤치마크 데이터 생성 중...")
    benchmarks = get_benchmark_data()

    print("7. prices.json 저장 중...")
    output = {
        'updated_at':       now_kst.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate':          fx_rate,
        'egg':              egg,
        'current_30':       current_30,
        'active_signals':   active_signals,
        'holdings':         holdings,
        'portfolio_summary':portfolio_summary,
        'benchmarks':       benchmarks,
        'settings': {
            'monthly_budget': MONTHLY_BUDGET,
            'start_date':     START_DATE,
            'wr_threshold':   WR_THRESHOLD,
            'months_allocated': months,
        }
    }

    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("  -> prices.json 성공적으로 저장됨")

    if active_signals:
        msg = build_signal_message(active_signals, fx_rate, months)
        send_telegram(msg)
    else:
        if 8 <= now_kst.hour <= 10:
            msg = (f"📊 달걀이론 포트폴리오 업데이트\n"
                   f"달걀 {egg['stage']}단계 | {egg['desc']}\n"
                   f"현재 매수 신호 없음 (WR > {WR_THRESHOLD})")
            send_telegram(msg)

    print("🎉 모든 작업 완료!")

if __name__ == '__main__':
    main()
