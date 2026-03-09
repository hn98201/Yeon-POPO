"""
달걀이론 포트폴리오 자동 업데이터
- 윌리엄스 %R: 주봉(Weekly) 14기간 기준
- 매월 12일 기준 배정금 반영
- 텔레그램: 종목별 매수금액/잉여현금 포함 알림
- GitHub Actions: 평일 오전 9시, 오후 11시 (KST) 자동 실행
"""

import os, json, time, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import finnhub
except ModuleNotFoundError:
    print("❌ finnhub-python 패키지가 설치되지 않았습니다. 워크플로우를 확인하세요.")
    exit(1)

# ── 설정
FINNHUB_KEY  = os.environ.get('FINNHUB_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
MONTHLY_BUDGET = int(os.environ.get('MONTHLY_BUDGET', '800000'))  # 월 투자금
START_DATE     = os.environ.get('START_DATE', '')                 # 투자 시작일 YYYY-MM-DD
WR_THRESHOLD   = float(os.environ.get('WR_THRESHOLD', '-60'))     # WR 임계값
KST = ZoneInfo('Asia/Seoul')

if not FINNHUB_KEY:
    print("❌ FINNHUB_KEY가 설정되지 않았습니다. Secrets를 확인하세요.")
    exit(1)

client = finnhub.Client(api_key=FINNHUB_KEY)

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

# 달걀 단계별 선호 섹터
STAGE_SECTORS = {
    1: ['에너지','소재','산업재'],
    2: ['산업재','금융','기술'],
    3: ['금융','산업재','기술'],
    4: ['기술','헬스케어','경기소비재'],
    5: ['헬스케어','경기소비재','부동산'],
    6: ['필수소비재','헬스케어','유틸리티'],
}

def safe_sleep(sec=0.9):
    time.sleep(sec)

# ══════════════════════════════════════════
# 윌리엄스 %R 계산 (주봉 14기간)
# ══════════════════════════════════════════
def get_weekly_wr(symbol: str, periods: int = 14) -> float | None:
    """주봉 기준 Williams %R 계산"""
    end = int(time.time())
    start = end - (periods + 10) * 7 * 24 * 3600  # 여유분 포함
    try:
        candles = client.stock_candles(symbol, 'W', start, end)
        if candles.get('s') != 'ok':
            return None
        highs  = candles['h']
        lows   = candles['l']
        closes = candles['c']
        if len(closes) < periods:
            return None
        # 최근 14주
        h14 = max(highs[-periods:])
        l14 = min(lows[-periods:])
        c   = closes[-1]
        if h14 == l14:
            return None
        wr = (h14 - c) / (h14 - l14) * -100
        return round(wr, 1)
    except Exception as e:
        print(f"  WR 계산 실패 {symbol}: {e}")
        return None

# ══════════════════════════════════════════
# 현재 주가 조회
# ══════════════════════════════════════════
def get_price(symbol: str) -> dict:
    try:
        q = client.quote(symbol)
        return {
            'price':    round(q.get('c', 0), 2),
            'prev':     round(q.get('pc', 0), 2),
            'change':   round(q.get('d', 0), 2),
            'pct':      round(q.get('dp', 0), 2),
        }
    except:
        return {'price': 0, 'prev': 0, 'change': 0, 'pct': 0}

# ══════════════════════════════════════════
# 달걀이론 지표 계산
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
        except:
            pass
        return None

    ind['fed_rate'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS')
    ind['spread'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y')
    ind['vix'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS')
    ind['unemp'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE')
    ind['cpi_yoy'] = None
    try:
        r = requests.get('https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL', timeout=10)
        lines = [line for line in r.text.strip().split('\n') if ',' in line]
        if len(lines) >= 14:
            cur = float(lines[-1].split(',')[1])
            yr = float(lines[-13].split(',')[1])
            ind['cpi_yoy'] = round((cur / yr - 1) * 100, 1)
    except:
        pass
    ind['m2_yoy'] = None
    try:
        r = requests.get('https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL', timeout=10)
        lines = [line for line in r.text.strip().split('\n') if ',' in line]
        if len(lines) >= 14:
            cur = float(lines[-1].split(',')[1])
            yr = float(lines[-13].split(',')[1])
            ind['m2_yoy'] = round((cur / yr - 1) * 100, 1)
    except:
        pass
    ind['pmi'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=UMCSENT')
    ind['claims'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=ICSA')
    ind['hy_spread'] = parse_fred_latest('https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2')
    return ind

# ══════════════════════════════════════════
# 달걀 단계 계산 (원본 유지 - 가정: 원본에 calc_egg_stage 있음, 실제 코드에서 추가 필요시 원본 붙여넣기)
def calc_egg_stage(ind: dict) -> dict:
    # 원본 calc_egg_stage 함수 내용 (사용자 문서에 truncated 되어 있으니, 원본에서 복사. 여기서는 placeholder)
    return {'stage': 1, 'score': 0, 'desc': '예시'}  # 실제 원본 함수로 교체

# ══════════════════════════════════════════
# 환율 조회 (원본 유지 - 가정)
def get_fx_rate() -> float:
    # 원본 get_fx_rate 함수 내용
    return 1300.0  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 30종목 선정 (원본 유지 - 가정)
def select_30(stage: int, price_data: dict) -> list:
    # 원본 select_30 함수 내용
    return []  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 배정 월수 계산 (원본 유지 - 가정)
def months_allocated(start_date: str) -> int:
    # 원본 months_allocated 함수 내용
    return 1  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 벤치마크 데이터 (원본 유지 - 가정)
def get_benchmark_data() -> dict:
    # 원본 get_benchmark_data 함수 내용
    return {}  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 텔레그램 메시지 빌드 (원본 유지 - 가정)
def build_signal_message(signals: list, fx_rate: float, months: int) -> str:
    # 원본 build_signal_message 함수 내용
    return ''  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 텔레그램 전송 (원본 유지 - 가정)
def send_telegram(msg: str):
    # 원본 send_telegram 함수 내용
    pass  # 실제 원본으로 교체

# ══════════════════════════════════════════
# 메인 실행
# ══════════════════════════════════════════
def main():
    if not FINNHUB_KEY:
        print("❌ FINNHUB_KEY가 설정되지 않았습니다. Secret을 확인하세요.")
        return

    now_kst = datetime.now(KST)
    print(f"[{now_kst.strftime('%Y-%m-%d %H:%M')} KST] 업데이트 시작")

    # 1. 경제 지표 + 달걀 단계
    print("경제 지표 조회 중...")
    ind = get_economic_indicators()
    egg = calc_egg_stage(ind)
    print(f"  달걀 {egg['stage']}단계 | 점수 {egg['score']}")

    # 2. 환율
    fx_rate = get_fx_rate()
    print(f"  환율: ₩{fx_rate:,.0f}")

    # 3. 전체 가격 + 주봉 WR 계산
    print("종목 가격 및 WR 계산 중...")
    price_data = {}
    for ticker in NOBL_UNIVERSE:
        safe_sleep(0.9)
        p  = get_price(ticker)
        wr = get_weekly_wr(ticker)
        price_data[ticker] = {**p, 'wr': wr}
        if wr is not None:
            print(f"  {ticker}: ${p['price']} | WR {wr}")

    # 4. 30종목 선정
    current_30 = select_30(egg['stage'], price_data)
    print(f"  선정 30종목: {current_30}")

    # 5. 매수 신호 (30종목 중 WR <= 임계값)
    months = months_allocated(START_DATE)
    per_stock = MONTHLY_BUDGET / 30

    active_signals = []
    for ticker in current_30:
        p  = price_data.get(ticker, {})
        wr = p.get('wr')
        if wr is not None and wr <= WR_THRESHOLD:
            active_signals.append({
                'ticker': ticker,
                'sector': NOBL_UNIVERSE.get(ticker, '--'),
                'wr':     wr,
                'price':  p.get('price', 0),
                'pct':    p.get('pct', 0),
            })
    active_signals.sort(key=lambda x: x['wr'])
    print(f"  신호 발생: {len(active_signals)}개")

    # 6. 포트폴리오 요약 (portfolio.json 읽기 — 없으면 기본값)
    portfolio_summary = {'total_value_krw': 0, 'total_value_usd': 0, 'total_pnl_pct': 0}
    holdings = []
    try:
        with open('portfolio.json', 'r') as f:
            pf = json.load(f)
        for h in pf.get('holdings', []):
            ticker = h['ticker']
            p = price_data.get(ticker, {})
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
        print(f"  portfolio.json 없음 또는 오류: {e}")

    # 7. 벤치마크
    print("벤치마크 조회 중...")
    benchmarks = get_benchmark_data()

    # 8. prices.json 저장
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
    print("prices.json 저장 완료")

    # 9. 텔레그램 알림
    if active_signals:
        msg = build_signal_message(active_signals, fx_rate, months)
        send_telegram(msg)
        print(f"텔레그램 알림 발송: {len(active_signals)}종목")
    else:
        # 신호 없을 때는 하루 1회 (오전 9시)만 알림
        if 8 <= now_kst.hour <= 10:
            msg = (
                f'📊 달걀이론 포트폴리오 업데이트\n'
                f'달걀 {egg["stage"]}단계 | {egg["desc"]}\n'
                f'현재 매수 신호 없음 (WR > {WR_THRESHOLD})\n'
                f'🕐 {now_kst.strftime("%Y-%m-%d %H:%M")} KST'
            )
            send_telegram(msg)

    print(f"완료! 신호: {len(active_signals)}개, 달걀: {egg['stage']}단계")

if __name__ == '__main__':
    main()
