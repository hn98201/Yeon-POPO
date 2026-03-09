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

import finnhub

# ── 설정
FINNHUB_KEY  = os.environ.get('FINNHUB_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
MONTHLY_BUDGET = int(os.environ.get('MONTHLY_BUDGET', '800000'))  # 월 투자금
START_DATE     = os.environ.get('START_DATE', '')                 # 투자 시작일 YYYY-MM-DD
WR_THRESHOLD   = float(os.environ.get('WR_THRESHOLD', '-60'))     # WR 임계값
KST = ZoneInfo('Asia/Seoul')

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

def safe_sleep(sec=0.5):
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
    headers = {'Content-Type': 'application/json'}

    # 기준금리 (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS',
            timeout=10)
        lines = r.text.strip().split('\n')
        ind['fed_rate'] = float(lines[-1].split(',')[1])
    except:
        ind['fed_rate'] = None

    # 10Y-2Y 스프레드 (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=T10Y2Y',
            timeout=10)
        lines = r.text.strip().split('\n')
        val = lines[-1].split(',')[1]
        ind['spread'] = float(val) if val != '.' else None
    except:
        ind['spread'] = None

    # VIX (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS',
            timeout=10)
        lines = r.text.strip().split('\n')
        # 최신 값이 '.'일 수 있으므로 역순으로 유효값 탐색
        for line in reversed(lines[1:]):
            val = line.split(',')[1]
            if val != '.':
                ind['vix'] = round(float(val), 1)
                break
        else:
            ind['vix'] = None
    except:
        ind['vix'] = None

    # 실업률 (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE',
            timeout=10)
        lines = r.text.strip().split('\n')
        ind['unemp'] = float(lines[-1].split(',')[1])
    except:
        ind['unemp'] = None

    # CPI YoY (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL',
            timeout=10)
        lines = r.text.strip().split('\n')
        cur = float(lines[-1].split(',')[1])
        yr  = float(lines[-13].split(',')[1])
        ind['cpi_yoy'] = round((cur/yr - 1)*100, 1)
    except:
        ind['cpi_yoy'] = None

    # M2 YoY (FRED)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL',
            timeout=10)
        lines = r.text.strip().split('\n')
        cur = float(lines[-1].split(',')[1])
        yr  = float(lines[-13].split(',')[1])
        ind['m2_yoy'] = round((cur/yr - 1)*100, 1)
    except:
        ind['m2_yoy'] = None

    # ═══ 선행지표 3종 ═══

    # 소비자심리지수 (FRED: UMCSENT — 미시간대, 선행지표)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=UMCSENT',
            timeout=10)
        lines = r.text.strip().split('\n')
        val = lines[-1].split(',')[1]
        ind['pmi'] = float(val) if val != '.' else None
    except:
        ind['pmi'] = None

    # 신규 실업수당 청구건수 (FRED: ICSA)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=ICSA',
            timeout=10)
        lines = r.text.strip().split('\n')
        ind['claims'] = float(lines[-1].split(',')[1])
    except:
        ind['claims'] = None

    # 하이일드 스프레드 (FRED: BAMLH0A0HYM2)
    try:
        r = requests.get(
            'https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2',
            timeout=10)
        lines = r.text.strip().split('\n')
        val = lines[-1].split(',')[1]
        ind['hy_spread'] = float(val) if val != '.' else None
    except:
        ind['hy_spread'] = None

    return ind

def calc_egg_stage(ind: dict) -> dict:
    """코스톨라니 달걀 단계 계산"""
    score = 0
    reasons = []

    fed   = ind.get('fed_rate')
    spread = ind.get('spread')
    vix   = ind.get('vix')
    unemp = ind.get('unemp')
    cpi   = ind.get('cpi_yoy')
    m2    = ind.get('m2_yoy')

    # 금리 방향
    if fed is not None:
        if fed < 2.0:   score += 2; reasons.append('초저금리')
        elif fed < 4.0: score += 1; reasons.append('저금리')
        else:           score -= 1; reasons.append('고금리')

    # 장단기 스프레드
    if spread is not None:
        if spread > 0.5:   score += 2; reasons.append('정상 수익률 곡선')
        elif spread > 0:   score += 1; reasons.append('완만한 곡선')
        elif spread > -0.5:score -= 1; reasons.append('평탄화')
        else:              score -= 2; reasons.append('역전')

    # VIX (공포지수)
    if vix is not None:
        if vix < 15:   score += 2; reasons.append('저변동성')
        elif vix < 20: score += 1; reasons.append('보통 변동성')
        elif vix < 30: score -= 1; reasons.append('고변동성')
        else:          score -= 2; reasons.append('극도 공포')

    # 실업률
    if unemp is not None:
        if unemp < 4.0:   score += 1; reasons.append('완전고용')
        elif unemp < 5.0: score += 0
        elif unemp < 7.0: score -= 1; reasons.append('실업 증가')
        else:              score -= 2; reasons.append('고실업')

    # CPI
    if cpi is not None:
        if cpi < 2.0:   score += 1; reasons.append('저인플레')
        elif cpi < 3.5: score += 0
        elif cpi < 6.0: score -= 1; reasons.append('인플레 압력')
        else:           score -= 2; reasons.append('고인플레')

    # M2
    if m2 is not None:
        if m2 > 5:    score += 1; reasons.append('유동성 풍부')
        elif m2 > 0:  score += 0
        else:         score -= 1; reasons.append('유동성 축소')

    # ═══ 선행지표 3종 (경기 선행 → 가중치 높게) ═══
    pmi    = ind.get('pmi')
    claims = ind.get('claims')
    hy     = ind.get('hy_spread')

    # 소비자심리: 80 이상 낙관, 60 이하 비관
    if pmi is not None:
        if pmi >= 85:   score += 3; reasons.append('소비심리 강낙관')
        elif pmi >= 70: score += 1; reasons.append('소비심리 양호')
        elif pmi >= 60: score -= 1; reasons.append('소비심리 위축')
        else:           score -= 3; reasons.append('소비심리 급냉')

    # 신규실업수당: 낮을수록 건강
    if claims is not None:
        ck = claims / 1000
        if ck < 220:    score += 2; reasons.append('고용 견조')
        elif ck < 260:  score += 1; reasons.append('고용 양호')
        elif ck < 300:  score -= 1; reasons.append('고용 둔화')
        else:           score -= 2; reasons.append('고용 악화')

    # HY 스프레드: 높을수록 신용 위축
    if hy is not None:
        if hy < 3.0:    score += 2; reasons.append('신용 안정')
        elif hy < 4.0:  score += 1; reasons.append('신용 양호')
        elif hy < 5.0:  score -= 1; reasons.append('신용 경계')
        else:           score -= 2; reasons.append('신용 위축')

  
    # 단계 결정 (1~6)
    if   score >= 10: stage, desc = 4, '④ 상승 본격 — 기술/헬스케어 중심'
    elif score >= 5:  stage, desc = 3, '③ 상승 초입 — 금융/산업재 선호'
    elif score >= 0:  stage, desc = 2, '② 바닥 형성 — 산업재/소재 관심'
    elif score >= -5: stage, desc = 5, '⑤ 과열 후 하락 — 헬스케어/경기소비재'
    elif score >= -9: stage, desc = 6, '⑥ 하락기 — 필수소비재/유틸리티 방어'
    else:             stage, desc = 1, '① 위기 — 에너지/소재 역발상'

    return {'stage': stage, 'desc': desc, 'score': score, 'indicators': ind, 'reasons': reasons}

# ══════════════════════════════════════════
# 30종목 선정 (달걀 단계 + 모멘텀)
# ══════════════════════════════════════════
def select_30(stage: int, price_data: dict) -> list:
    preferred = STAGE_SECTORS.get(stage, [])
    scored = []
    for ticker, sector in NOBL_UNIVERSE.items():
        p = price_data.get(ticker, {})
        pct = p.get('pct', 0) or 0
        base_score = 3 if sector in preferred[:1] else 2 if sector in preferred else 1
        momentum_bonus = pct / 10  # 6개월 수익률 반영 (실제 구현 시 확장)
        scored.append({
            'ticker': ticker,
            'sector': sector,
            'score':  base_score + momentum_bonus,
        })
    scored.sort(key=lambda x: x['score'], reverse=True)
    return [s['ticker'] for s in scored[:30]]

# ══════════════════════════════════════════
# 매월 12일 기준 경과 개월 수
# ══════════════════════════════════════════
def months_allocated(start_date_str: str) -> int:
    if not start_date_str:
        return 0
    start = datetime.strptime(start_date_str, '%Y-%m-%d')
    now   = datetime.now()
    # 첫 배정일
    if start.day <= 12:
        first_alloc = start.replace(day=12)
    else:
        if start.month == 12:
            first_alloc = start.replace(year=start.year+1, month=1, day=12)
        else:
            first_alloc = start.replace(month=start.month+1, day=12)

    if now < first_alloc:
        return 0

    count = 0
    d = first_alloc
    while d <= now:
        count += 1
        if d.month == 12:
            d = d.replace(year=d.year+1, month=1)
        else:
            d = d.replace(month=d.month+1)
    return count

# ══════════════════════════════════════════
# 환율 조회
# ══════════════════════════════════════════
def get_fx_rate() -> float:
    try:
        r = requests.get('https://api.exchangerate-api.com/v4/latest/USD', timeout=5)
        return round(r.json()['rates']['KRW'], 0)
    except:
        try:
            q = client.quote('USDKRW')
            return round(q.get('c', 1350), 0)
        except:
            return 1350.0

# ══════════════════════════════════════════
# 벤치마크 수익률 (VOO, QQQ, NOBL)
# ══════════════════════════════════════════
def get_benchmark_data() -> dict:
    benchmarks = {}
    tickers = {'VOO': 'VOO', 'QQQ': 'QQQ', 'NOBL': 'NOBL'}
    end   = int(time.time())
    start = end - 5 * 365 * 24 * 3600  # 5년

    for name, symbol in tickers.items():
        try:
            safe_sleep(0.4)
            candles = client.stock_candles(symbol, 'W', start, end)
            if candles.get('s') != 'ok':
                continue
            dates  = [datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in candles['t']]
            closes = candles['c']
            base   = closes[0]
            pcts   = [round((c/base - 1)*100, 2) for c in closes]
            benchmarks[name] = {'dates': dates, 'pct': pcts}
        except Exception as e:
            print(f"벤치마크 오류 {name}: {e}")

    return benchmarks

# ══════════════════════════════════════════
# 텔레그램 알림 (상세 매수 안내 포함)
# ══════════════════════════════════════════
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, json={
            'chat_id': TELEGRAM_CHAT,
            'text': msg,
            'parse_mode': 'HTML'
        }, timeout=10)
    except Exception as e:
        print(f"텔레그램 오류: {e}")

def build_signal_message(signals: list, fx_rate: float, months: int) -> str:
    if not signals:
        return ''

    monthly = MONTHLY_BUDGET
    per_stock = monthly / 30

    lines = [f'🔔 <b>달걀이론 매수 신호 ({len(signals)}종목)</b>']
    lines.append(f'📅 배정 {months}개월 | 종목당 {int(per_stock*months):,}원 누적\n')

    for s in signals:
        wr    = s['wr']
        level = 3 if wr <= -80 else 2 if wr <= -70 else 1
        ratio = 1.0 if level >= 3 else 0.667 if level >= 2 else 0.333
        alloc = per_stock * months
        # 잉여현금은 서버에서 portfolio.json 없이는 정확히 모르므로 배정금 기준 표시
        buy_krw = int(per_stock * ratio / 100) * 100
        buy_usd = round(buy_krw / fx_rate, 2) if fx_rate else 0
        shares  = round(buy_usd / s['price'], 4) if s.get('price', 0) > 0 else 0

        emoji  = '🔴' if level >= 3 else '🟡' if level >= 2 else '🟢'
        lvtxt  = '강매수' if level >= 3 else '중매수' if level >= 2 else '약매수'
        lines.append(
            f'{emoji} <b>{s["ticker"]}</b> ({s.get("sector","--")}) — {lvtxt}\n'
            f'   WR {wr:.1f} | ${s.get("price",0):.2f}\n'
            f'   ≈ {buy_krw:,}원 ({shares}주 / ${buy_usd})'
        )

    lines.append(f'\n🕐 {datetime.now(KST).strftime("%Y-%m-%d %H:%M")} KST')
    return '\n'.join(lines)

# ══════════════════════════════════════════
# 메인 실행
# ══════════════════════════════════════════
def main():
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
        safe_sleep(1.2)
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
