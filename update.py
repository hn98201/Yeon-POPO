"""
선행형 매크로-모멘텀 자동 투자 시스템 v2.0
FRED 완전 제거 → yfinance + BLS API (공식 무료)
RS 모멘텀 종목 선정 + VIX 예산 조절 + Williams %R 분할매수
"""
import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd

KST = ZoneInfo('Asia/Seoul')

# ── 환경변수
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
MONTHLY_BUDGET = int(os.environ.get('MONTHLY_BUDGET', '800000'))
START_DATE     = os.environ.get('START_DATE', '2024-01-01')

# ═══════════════════════════════════════════
# NOBL 유니버스 (섹터별)
# ═══════════════════════════════════════════
NOBL_UNIVERSE = {
    'KO':'필수소비재','PG':'필수소비재','PEP':'필수소비재','CL':'필수소비재',
    'KMB':'필수소비재','MKC':'필수소비재','GIS':'필수소비재','SYY':'필수소비재',
    'WMT':'필수소비재','TGT':'필수소비재','HRL':'필수소비재','CLX':'필수소비재',
    'ADM':'필수소비재',
    'CAT':'산업재','EMR':'산업재','ITW':'산업재','DOV':'산업재',
    'GWW':'산업재','SWK':'산업재','CHRW':'산업재','EXPD':'산업재',
    'CTAS':'산업재','ROL':'산업재','FAST':'산업재','NDSN':'산업재',
    'CB':'금융','CINF':'금융','AFL':'금융','BEN':'금융',
    'SPGI':'금융','AMP':'금융','AON':'금융',
    'NUE':'소재','PPG':'소재','SHW':'소재','LIN':'소재',
    'ECL':'소재','APD':'소재','ALB':'소재',
    'JNJ':'헬스케어','ABT':'헬스케어','BDX':'헬스케어',
    'ABBV':'헬스케어','CAH':'헬스케어','MDT':'헬스케어',
    'LOW':'경기소비재','GPC':'경기소비재',
    'AWK':'유틸리티','ATO':'유틸리티','NFG':'유틸리티',
    'FRT':'부동산','ESS':'부동산','O':'부동산',
    'IBM':'기술','TXN':'기술',
    'XOM':'에너지','CVX':'에너지',
}

# ── 달걀 단계별 선호 섹터 (설계서 기준)
STAGE_SECTORS = {
    1: ['필수소비재','헬스케어','유틸리티'],   # 하락 초입: 방어주
    2: ['필수소비재','헬스케어','유틸리티'],   # 하락 본격: 방어주 유지
    3: ['금융','산업재','필수소비재'],          # 상승 초입: 경기민감 전환
    4: ['기술','경기소비재','금융'],            # 상승 본격: 성장주
    5: ['헬스케어','경기소비재','기술'],        # 과열 초입: 선별적
    6: ['에너지','소재','산업재'],              # 과열 본격: 실물자산
}

# ═══════════════════════════════════════════
# 캐시 로드 (FRED 대체용 폴백)
# ═══════════════════════════════════════════
def load_cache() -> dict:
    """이전 prices.json에서 지표값 캐시 로드"""
    try:
        if os.path.exists('prices.json'):
            with open('prices.json', 'r', encoding='utf-8') as f:
                return json.load(f).get('egg', {}).get('indicators', {})
    except:
        pass
    return {}

def load_manual_overrides() -> dict:
    """portfolio.json의 수동 입력값 로드 (M2, claims 등)"""
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r', encoding='utf-8') as f:
                return json.load(f).get('manual_overrides', {})
    except:
        pass
    return {}

# ═══════════════════════════════════════════
# BLS API (미국 노동통계국 공식 무료 API)
# 인증 불필요, GitHub Actions에서 정상 동작
# ═══════════════════════════════════════════
def bls_fetch(series_id: str) -> list | None:
    """BLS Public Data API v1 - 공식 무료, API키 불필요"""
    try:
        r = requests.get(
            f"https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}",
            timeout=20,
            headers={'User-Agent': 'egg-portfolio-system/2.0'}
        )
        d = r.json()
        if d.get('status') == 'REQUEST_SUCCEEDED':
            return d['Results']['series'][0]['data']
    except Exception as e:
        print(f"    BLS [{series_id}] 실패: {e}")
    return None

# ═══════════════════════════════════════════
# 경제 지표 수집 (FRED 완전 미사용)
# ═══════════════════════════════════════════
def get_economic_indicators() -> dict:
    cache = load_cache()
    overrides = load_manual_overrides()
    ind = {}

    def cache_or(key, default=None):
        """수동 입력 → 캐시 → 기본값 순서로 폴백"""
        if overrides.get(key) is not None:
            return overrides[key]
        if cache.get(key) is not None:
            return cache[key]
        return default

    # ① VIX (yfinance ^VIX)
    try:
        ind['vix'] = round(yf.Ticker('^VIX').fast_info.last_price, 2)
    except:
        ind['vix'] = cache_or('vix', 18.0)
    print(f"    vix         = {ind['vix']}")

    # ② 단기금리: ^IRX (13주 T-Bill ≈ Fed Funds Rate 프록시)
    # FRED 'FEDFUNDS' 대체 — T-Bill은 Fed Rate를 즉각 추적함
    try:
        ind['fed_rate'] = round(yf.Ticker('^IRX').fast_info.last_price, 2)
    except:
        ind['fed_rate'] = cache_or('fed_rate', 4.5)
    print(f"    fed_rate    = {ind['fed_rate']}% (^IRX 프록시)")

    # ③ 장단기 스프레드: ^TNX(10Y) - ^IRX(3M)
    # FRED 'T10Y2Y' 대체 — 2Y 미제공으로 3M 사용 (방향성 동일)
    try:
        t10 = yf.Ticker('^TNX').fast_info.last_price
        if t10 and ind.get('fed_rate'):
            ind['spread'] = round(t10 - ind['fed_rate'], 2)
        else:
            ind['spread'] = cache_or('spread', 0.2)
    except:
        ind['spread'] = cache_or('spread', 0.2)
    print(f"    spread      = {ind['spread']}% (10Y-3M)")

    time.sleep(0.5)

    # ④ CPI YoY (BLS API - CUUR0000SA0: CPI All Items)
    # FRED 'CPIAUCSL' 완전 대체 — 동일 원천 데이터
    print("    [BLS] CPI 조회...")
    bls_cpi = bls_fetch('CUUR0000SA0')
    if bls_cpi:
        try:
            rows = sorted(
                [x for x in bls_cpi
                 if x['period'].startswith('M') and x['period'] != 'M13'],
                key=lambda x: (x['year'], x['period']),
                reverse=True
            )
            if len(rows) >= 13:
                cur, prev = float(rows[0]['value']), float(rows[12]['value'])
                ind['cpi_yoy'] = round((cur / prev - 1) * 100, 2)
            else:
                ind['cpi_yoy'] = cache_or('cpi_yoy')
        except:
            ind['cpi_yoy'] = cache_or('cpi_yoy')
    else:
        ind['cpi_yoy'] = cache_or('cpi_yoy')
    print(f"    cpi_yoy     = {ind['cpi_yoy']}%")

    # ⑤ 실업률 (BLS API - LNS14000000: Unemployment Rate SA)
    # FRED 'UNRATE' 완전 대체 — 동일 원천 데이터
    print("    [BLS] 실업률 조회...")
    bls_unemp = bls_fetch('LNS14000000')
    if bls_unemp:
        try:
            rows = sorted(
                [x for x in bls_unemp
                 if x['period'].startswith('M') and x['period'] != 'M13'],
                key=lambda x: (x['year'], x['period']),
                reverse=True
            )
            ind['unemp'] = float(rows[0]['value'])
        except:
            ind['unemp'] = cache_or('unemp')
    else:
        ind['unemp'] = cache_or('unemp')
    print(f"    unemp       = {ind['unemp']}%")

    # ⑥ M2 증가율 (수동 입력 or 캐시 유지)
    # GitHub Actions에서 Fed DDP 접근 불안정 → 수동 관리
    # portfolio.json의 manual_overrides.m2_yoy 에 최신값 입력 권장
    ind['m2_yoy'] = cache_or('m2_yoy', 3.5)
    src = "수동입력" if overrides.get('m2_yoy') else "캐시"
    print(f"    m2_yoy      = {ind['m2_yoy']}% ({src})")

    # ⑦ HY 스프레드 (VIX 수준 기반 추정)
    # FRED 'BAMLH0A0HYM2' 대체 — VIX와 HY 스프레드 상관관계 활용
    # 수동 입력 시 portfolio.json manual_overrides.hy_spread 사용
    manual_hy = overrides.get('hy_spread')
    if manual_hy is not None:
        ind['hy_spread'] = manual_hy
        print(f"    hy_spread   = {ind['hy_spread']}% (수동입력)")
    else:
        vx = ind.get('vix', 18) or 18
        ind['hy_spread'] = (
            2.8 if vx < 13 else
            3.0 if vx < 15 else
            3.3 if vx < 18 else
            3.8 if vx < 22 else
            4.8 if vx < 27 else
            5.8 if vx < 35 else 7.0
        )
        print(f"    hy_spread   ≈ {ind['hy_spread']}% (VIX {vx:.0f} 기반 추정)")

    # ⑧ 소비자심리 - UMICH 직접 시도
    try:
        r = requests.get(
            "https://data.sca.isr.umich.edu/get-chart.php?r=1&t=tbmics&f=csv",
            timeout=10, headers={'User-Agent': 'Mozilla/5.0'}
        )
        lines = [l.strip() for l in r.text.strip().split('\n') if l.strip()]
        val = float(lines[-1].split(',')[-1])
        ind['pmi'] = round(val, 1) if 20 < val < 130 else cache_or('pmi', 65.0)
        print(f"    pmi         = {ind['pmi']} (UMICH 실시간)")
    except:
        ind['pmi'] = cache_or('pmi', 65.0)
        print(f"    pmi         = {ind['pmi']} (캐시)")

    # ⑨ 신규실업수당 청구 (수동 입력 or 캐시)
    # DOL ETA 접근 불안정 → 수동 관리 권장
    ind['claims'] = int(cache_or('claims', 220000))
    print(f"    claims      = {ind['claims']:,} (캐시)")

    return ind


# ═══════════════════════════════════════════
# 달걀 단계 계산
# ═══════════════════════════════════════════
def calc_egg_stage(ind: dict) -> dict:
    score = 0.0
    details = {}

    # [1] 기준금리 (±4.0 / +2.0)
    fr = ind.get('fed_rate') or 0
    if fr >= 5.0:   score -= 4.0; d = f"-4 (고금리 {fr}%)"
    elif fr <= 2.0: score += 2.0; d = f"+2 (저금리 {fr}%)"
    else:           d = f"0 (중립 {fr}%)"
    details['fed_rate'] = d

    # [2] 장단기 스프레드 (-3.5 / +1.5)
    ts = ind.get('spread') or 0
    if ts < 0:      score -= 3.5; d = f"-3.5 (역전 {ts}%)"
    elif ts >= 0.5: score += 1.5; d = f"+1.5 (정상 {ts}%)"
    else:           d = f"0 (평탄 {ts}%)"
    details['spread'] = d

    # [3] VIX (-2.5 / +2.0)
    vx = ind.get('vix') or 0
    if vx >= 25:   score -= 2.5; d = f"-2.5 (공포 {vx})"
    elif vx <= 15: score += 2.0; d = f"+2 (안정 {vx})"
    else:          d = f"0 (중립 {vx})"
    details['vix'] = d

    # [4] M2 증가율 (-3.0 / +2.0)
    m2 = ind.get('m2_yoy') or 0
    if m2 <= 0:    score -= 3.0; d = f"-3 (긴축 {m2}%)"
    elif m2 >= 5:  score += 2.0; d = f"+2 (완화 {m2}%)"
    else:          d = f"0 (중립 {m2}%)"
    details['m2_yoy'] = d

    # [5] CPI (-3.0 / +1.5)
    cp = ind.get('cpi_yoy') or 0
    if cp >= 4.0:   score -= 3.0; d = f"-3 (고물가 {cp}%)"
    elif cp <= 2.5: score += 1.5; d = f"+1.5 (저물가 {cp}%)"
    else:           d = f"0 (중립 {cp}%)"
    details['cpi_yoy'] = d

    # [6] HY 스프레드 (-3.0 / +1.5)
    hy = ind.get('hy_spread') or 0
    if hy >= 4.5:   score -= 3.0; d = f"-3 (신용위기 {hy}%)"
    elif hy <= 3.5: score += 1.5; d = f"+1.5 (안정 {hy}%)"
    else:           d = f"0 (중립 {hy}%)"
    details['hy_spread'] = d

    # [7] 실업률 (-2.0 / +1.0)
    ur = ind.get('unemp') or 0
    if ur >= 4.5:   score -= 2.0; d = f"-2 (높음 {ur}%)"
    elif ur <= 3.8: score += 1.0; d = f"+1 (낮음 {ur}%)"
    else:           d = f"0 (중립 {ur}%)"
    details['unemp'] = d

    # [8] 소비자심리 (-1.5 / +1.0)
    pm = ind.get('pmi') or 0
    if pm <= 50:   score -= 1.5; d = f"-1.5 (위축 {pm})"
    elif pm >= 70: score += 1.0; d = f"+1 (호조 {pm})"
    else:          d = f"0 (중립 {pm})"
    details['pmi'] = d

    # [9] 신규실업수당 (-1.0 / +0.5)
    cl = ind.get('claims') or 0
    if cl >= 250000:   score -= 1.0; d = f"-1 (급증 {cl:,})"
    elif cl <= 200000: score += 0.5; d = f"+0.5 (안정 {cl:,})"
    else:              d = f"0 (중립 {cl:,})"
    details['claims'] = d

    # 단계 결정 (최대 +18 / 최소 -18)
    if score <= -9:   stage = 1
    elif score <= -4: stage = 2
    elif score <= 1:  stage = 3
    elif score <= 5:  stage = 4
    elif score <= 9:  stage = 5
    else:             stage = 6

    return {
        'stage': stage,
        'score': round(score, 1),
        'desc': {
            1: "① 하락 초입",
            2: "② 하락 본격",
            3: "③ 상승 초입",
            4: "④ 상승 본격",
            5: "⑤ 과열 초입",
            6: "⑥ 과열 본격",
        }[stage],
        'indicators': ind,
        'scoring_details': details,
    }


# ═══════════════════════════════════════════
# RS 모멘텀 + 종목 선정
# ═══════════════════════════════════════════
def calc_rs(ticker: str) -> float | None:
    """
    RS = Price(t-21) / Price(t-126) - 1
    조건: 현재가 > 200MA
    """
    try:
        closes = yf.Ticker(ticker).history(period="1y", interval="1d")['Close'].dropna()
        if len(closes) < 130:
            return None
        # 200MA 필터
        ma200 = closes.iloc[-min(200, len(closes)):].mean()
        if closes.iloc[-1] < ma200:
            return None  # 200MA 아래 제외
        # RS 계산
        if len(closes) >= 127:
            rs = (closes.iloc[-22] / closes.iloc[-127] - 1) * 100
            return round(rs, 2)
    except:
        pass
    return None


def select_top_rs(stage: int, top_n: int = 8) -> tuple[list, list]:
    """RS 모멘텀 기반 Top N 종목 선정 (선호 섹터 우선)"""
    pref_sectors = STAGE_SECTORS.get(stage, [])
    pref_tickers = [t for t, s in NOBL_UNIVERSE.items() if s in pref_sectors]
    other_tickers = [t for t, s in NOBL_UNIVERSE.items() if s not in pref_sectors]

    scores = []

    # 1차: 선호 섹터
    print(f"  선호 섹터 {len(pref_tickers)}개 RS 계산...")
    for t in pref_tickers:
        time.sleep(0.25)
        rs = calc_rs(t)
        if rs is not None:
            scores.append({'ticker': t, 'sector': NOBL_UNIVERSE[t], 'rs': rs})

    scores.sort(key=lambda x: x['rs'], reverse=True)

    # 2차: 부족하면 다른 섹터 보충
    if len(scores) < top_n:
        print(f"  선호 섹터 부족({len(scores)}개), 다른 섹터 보충...")
        for t in other_tickers:
            if len(scores) >= top_n + 2:
                break
            time.sleep(0.25)
            rs = calc_rs(t)
            if rs is not None:
                scores.append({'ticker': t, 'sector': NOBL_UNIVERSE[t], 'rs': rs})
        scores.sort(key=lambda x: x['rs'], reverse=True)

    top_scores = scores[:top_n]
    selected = [x['ticker'] for x in top_scores]

    # 그래도 부족하면 단순 섹터 기반으로 채우기
    if len(selected) < top_n:
        for t in pref_tickers + other_tickers:
            if t not in selected:
                selected.append(t)
            if len(selected) >= top_n:
                break

    return selected[:top_n], top_scores


# ═══════════════════════════════════════════
# 데이터 조회 유틸리티
# ═══════════════════════════════════════════
def get_weekly_wr(ticker: str, periods: int = 14) -> float | None:
    """주봉 14기간 Williams %R"""
    try:
        h = yf.Ticker(ticker).history(period="6mo", interval="1wk")
        if len(h) < periods:
            return None
        hi = h['High'].iloc[-periods:].max()
        lo = h['Low'].iloc[-periods:].min()
        cl = h['Close'].iloc[-1]
        if hi == lo:
            return None
        return round((hi - cl) / (hi - lo) * -100, 1)
    except:
        return None


def get_price(ticker: str) -> dict:
    try:
        fi = yf.Ticker(ticker).fast_info
        p, prev = fi.last_price, fi.previous_close
        chg = p - prev
        return {
            'price':  round(p, 2),
            'prev':   round(prev, 2),
            'change': round(chg, 2),
            'pct':    round(chg / prev * 100, 2) if prev else 0.0,
        }
    except:
        return {'price': 0, 'prev': 0, 'change': 0, 'pct': 0}


def get_fx_rate() -> float:
    try:
        return round(yf.Ticker("KRW=X").fast_info.last_price, 2)
    except:
        return 1350.0


def get_benchmarks() -> dict:
    result = {}
    for t in ['VOO', 'QQQ', 'NOBL']:
        try:
            h = yf.Ticker(t).history(period="1y", interval="1wk")['Close']
            if not h.empty:
                base = h.iloc[0]
                result[t] = {
                    'dates': h.index.strftime('%m/%d').tolist(),
                    'pct':   [round((v / base - 1) * 100, 2) for v in h],
                }
        except:
            pass
    return result


# ═══════════════════════════════════════════
# 투자 로직
# ═══════════════════════════════════════════
def adjust_budget(vix, base: int) -> dict:
    """VIX 기반 월 투자금 자동 조절"""
    if not vix:
        return {'amount': base, 'multiplier': 1.0, 'reason': '정상 투자'}
    if vix >= 30:   m, r = 0.50, f"극심한 공포(VIX {vix:.0f}) -50%"
    elif vix >= 25: m, r = 0.70, f"시장 공포(VIX {vix:.0f}) -30%"
    elif vix >= 20: m, r = 0.85, f"불안(VIX {vix:.0f}) -15%"
    elif vix <= 12: m, r = 1.40, f"극저 변동성(VIX {vix:.0f}) +40%"
    elif vix <= 15: m, r = 1.20, f"시장 안정(VIX {vix:.0f}) +20%"
    else:           m, r = 1.00, f"중립(VIX {vix:.0f})"
    return {'amount': int(base * m), 'multiplier': m, 'reason': r}


def calc_allocation(wr, budget: int) -> dict:
    """
    WR 기반 분할매수 비중 (설계서 기준)
    WR ≤ -50: 30% (약매수)
    WR ≤ -70: 40% (중매수)
    WR ≤ -85: 30% (강매수)
    """
    if wr is None or wr > -50:
        return {'pct': 0, 'amount': 0, 'signal': 'NONE'}
    if wr <= -85:
        return {'pct': 30, 'amount': int(budget * 0.30), 'signal': 'STRONG'}
    if wr <= -70:
        return {'pct': 40, 'amount': int(budget * 0.40), 'signal': 'MEDIUM'}
    return {'pct': 30, 'amount': int(budget * 0.30), 'signal': 'WEAK'}


def months_elapsed(start: str) -> int:
    try:
        s = datetime.strptime(start, '%Y-%m-%d')
        n = datetime.now()
        return max(0, (n.year - s.year) * 12 + n.month - s.month)
    except:
        return 0


# ═══════════════════════════════════════════
# 텔레그램
# ═══════════════════════════════════════════
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10
        )
    except:
        pass


def serial(obj):
    if hasattr(obj, 'tolist'): return obj.tolist()
    if hasattr(obj, 'item'):   return obj.item()
    raise TypeError(type(obj))


# ═══════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════
def main():
    now = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} KST] 시스템 시작")
    print(f"{'='*50}\n")

    # ── 1. 경제 지표
    print("[1/8] 경제 지표 수집 (yfinance + BLS API)...")
    ind = get_economic_indicators()
    egg = calc_egg_stage(ind)
    print(f"  → 달걀 {egg['stage']}단계 | {egg['desc']} | 점수: {egg['score']}\n")

    # ── 2. 환율
    print("[2/8] 환율 조회...")
    fx = get_fx_rate()
    print(f"  → ₩{fx:,.0f}\n")

    # ── 3. VIX 예산 조정
    print("[3/8] VIX 기반 투자금 조절...")
    budget = adjust_budget(ind.get('vix'), MONTHLY_BUDGET)
    budget['base'] = MONTHLY_BUDGET
    print(f"  → 기본 ₩{MONTHLY_BUDGET:,} × {budget['multiplier']} = ₩{budget['amount']:,}")
    print(f"  → 사유: {budget['reason']}\n")

    # ── 4. RS 모멘텀 종목 선정
    print(f"[4/8] RS 모멘텀 Top 8 선정 (달걀 {egg['stage']}단계)...")
    top_tickers, rs_ranking = select_top_rs(egg['stage'])
    print(f"  → 선정: {top_tickers}\n")

    # ── 5. 가격 + WR 계산
    print("[5/8] 주가 및 WR 계산...")
    stock_data = {}
    for t in top_tickers:
        time.sleep(0.4)
        p    = get_price(t)
        wr   = get_weekly_wr(t)
        alloc = calc_allocation(wr, budget['amount'])
        stock_data[t] = {
            **p,
            'wr':       wr,
            'sector':   NOBL_UNIVERSE.get(t, '--'),
            'rs':       next((x['rs'] for x in rs_ranking if x['ticker'] == t), None),
            'allocation': alloc,
        }
        print(f"  {t:6} ${p['price']:>8.2f} | WR {str(wr):>7} | {alloc['signal']}")
    print()

    # ── 6. 매수 신호
    signals = sorted(
        [{'ticker': t, **v} for t, v in stock_data.items()
         if v['allocation']['signal'] != 'NONE'],
        key=lambda x: x.get('wr') or 0
    )
    print(f"[6/8] 매수 신호: {len(signals)}개\n")

    # ── 7. 포트폴리오
    print("[7/8] 포트폴리오 계산...")
    holdings = []
    ps = {'total_value_usd': 0, 'total_value_krw': 0,
          'total_pnl_pct': 0, 'total_invested_krw': 0}
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r', encoding='utf-8') as f:
                pf = json.load(f)
            for h in pf.get('holdings', []):
                t = h['ticker']
                if t in stock_data:
                    p = stock_data[t]; wr = stock_data[t]['wr']
                else:
                    time.sleep(0.3); p = get_price(t); wr = get_weekly_wr(t)
                cur, avg, sh = p.get('price', 0), h.get('avg_price_usd', 0), h.get('shares', 0)
                holdings.append({
                    'ticker':               t,
                    'sector':               h.get('sector', NOBL_UNIVERSE.get(t, '--')),
                    'shares':               sh,
                    'avg_price_usd':        avg,
                    'current_price':        cur,
                    'current_value_usd':    round(cur * sh, 2),
                    'current_value_krw':    round(cur * sh * fx),
                    'pnl_pct':              round((cur/avg - 1)*100, 2) if avg > 0 else 0,
                    'day_change_pct':       p.get('pct', 0),
                    'wr':                   wr,
                    'dividends_usd':        h.get('dividends_received_usd', 0),
                })
            total_usd = sum(h['current_value_usd'] for h in holdings)
            cost      = sum(h['shares'] * h['avg_price_usd'] for h in holdings)
            ps = {
                'total_value_usd':    round(total_usd, 2),
                'total_value_krw':    round(total_usd * fx),
                'total_pnl_pct':      round((total_usd/cost - 1)*100, 2) if cost > 0 else 0,
                'total_invested_krw': months_elapsed(START_DATE) * MONTHLY_BUDGET,
                'dividends_total_usd': round(sum(h['dividends_usd'] for h in holdings), 2),
            }
    except Exception as e:
        print(f"  포트폴리오 오류: {e}")
    print()

    # ── 8. 벤치마크 + 저장
    print("[8/8] 벤치마크 데이터 + prices.json 저장...")
    benchmarks = get_benchmarks()

    output = {
        'updated_at':        now.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate':           fx,
        'egg':               egg,
        'budget':            budget,
        'top_tickers':       top_tickers,
        'rs_ranking':        rs_ranking,
        'stock_data':        stock_data,
        'signals':           signals,
        'holdings':          holdings,
        'portfolio_summary': ps,
        'benchmarks':        benchmarks,
        'settings': {
            'monthly_budget':   MONTHLY_BUDGET,
            'start_date':       START_DATE,
            'months_elapsed':   months_elapsed(START_DATE),
        },
    }
    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=serial)
    print("  → prices.json 저장 완료\n")

    # 텔레그램 알림
    if signals:
        lines = [
            "🔔 <b>달걀이론 매수 신호</b>",
            f"단계: {egg['desc']} ({egg['score']}점)",
            f"예산: ₩{budget['amount']:,} ({budget['reason']})\n",
        ]
        for s in signals:
            icon = "🔴" if s['allocation']['signal'] == 'STRONG' else \
                   "🟡" if s['allocation']['signal'] == 'MEDIUM' else "🟢"
            lines += [
                f"{icon} <b>{s['ticker']}</b> ${s['price']:.2f}",
                f"   WR: {s['wr']} | ₩{s['allocation']['amount']:,}",
            ]
        send_telegram('\n'.join(lines))
    elif 8 <= now.hour <= 10:
        send_telegram(
            f"📊 달걀이론 포트폴리오\n"
            f"{egg['desc']} ({egg['score']}점)\n"
            f"신호 없음 | ₩{budget['amount']:,}"
        )

    print("🎉 모든 작업 완료!")


if __name__ == '__main__':
    main()
