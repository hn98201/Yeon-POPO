"""
선행형 매크로-모멘텀 자동 투자 시스템 v3.1
- FRED 완전 제거 → yfinance + BLS API
- WR 단계 변화 알림 (-60/-70/-85 진입 시 각각 발송)
- 리밸런싱 감지 → 탈락(매도) / 신규 편입 알림
- 분기 리밸런싱 (1/4/7/10월 첫 실행 시 자동 감지)
- NaN/Inf JSON 직렬화 오류 완전 수정
"""
import os, json, time, requests, math
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd

KST = ZoneInfo('Asia/Seoul')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
MONTHLY_BUDGET = int(os.environ.get('MONTHLY_BUDGET', '800000'))
START_DATE     = os.environ.get('START_DATE', '2024-01-01')

# ═══════════════════════════════════════════
# NOBL 유니버스
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

STAGE_SECTORS = {
    1: ['필수소비재','헬스케어','유틸리티'],
    2: ['필수소비재','헬스케어','유틸리티'],
    3: ['금융','산업재','소재'],
    4: ['기술','경기소비재','금융'],
    5: ['헬스케어','경기소비재','기술'],
    6: ['에너지','소재','산업재'],
}

# ═══════════════════════════════════════════
# WR 단계 정의
# ═══════════════════════════════════════════
def wr_level(wr):
    if wr is None or wr > -60: return 0
    if wr > -70:               return 1  # -60 ~ -70: 약매수
    if wr > -85:               return 2  # -70 ~ -85: 중매수
    return 3                             # -85 이하: 강매수

WR_LEVEL_LABEL = {
    0: None,
    1: '🟢 약매수 (WR ≤ -60, 30%)',
    2: '🟡 중매수 (WR ≤ -70, 40%)',
    3: '🔴 강매수 (WR ≤ -85, 30%)',
}

# ═══════════════════════════════════════════
# JSON 직렬화 — NaN/Inf 완전 제거
# ═══════════════════════════════════════════
def sanitize(obj):
    """JSON 저장 전 NaN/Inf → None 변환 (재귀)"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj

def serial(obj):
    if hasattr(obj, 'tolist'): return obj.tolist()
    if hasattr(obj, 'item'):
        v = obj.item()
        return None if (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else v
    raise TypeError(type(obj))

# ═══════════════════════════════════════════
# 이전 prices.json 로드
# ═══════════════════════════════════════════
def load_previous() -> dict:
    try:
        if os.path.exists('prices.json'):
            with open('prices.json', 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

# ═══════════════════════════════════════════
# 리밸런싱 감지
# ═══════════════════════════════════════════
def is_rebalancing_month() -> bool:
    return datetime.now(KST).month in [1, 4, 7, 10]

def detect_rebalancing(prev: dict, new_top: list) -> dict:
    prev_top = prev.get('top_tickers', [])
    if not prev_top:
        return {'dropped': [], 'added': []}
    return {
        'dropped': [t for t in prev_top if t not in new_top],
        'added':   [t for t in new_top  if t not in prev_top],
    }

# ═══════════════════════════════════════════
# WR 단계 변화 감지
# ═══════════════════════════════════════════
def detect_wr_changes(prev: dict, new_stock_data: dict) -> list:
    changes = []
    prev_stock = prev.get('stock_data', {})
    for ticker, data in new_stock_data.items():
        wr_new = data.get('wr')
        new_lv = wr_level(wr_new)
        if new_lv == 0:
            continue
        prev_lv = wr_level(prev_stock.get(ticker, {}).get('wr'))
        if new_lv > prev_lv:
            changes.append({
                'ticker':     ticker,
                'prev_level': prev_lv,
                'new_level':  new_lv,
                'wr':         wr_new,
                'price':      data.get('price', 0),
                'sector':     NOBL_UNIVERSE.get(ticker, '--'),
                'allocation': data.get('allocation', {}),
            })
    return sorted(changes, key=lambda x: x['new_level'], reverse=True)

# ═══════════════════════════════════════════
# 텔레그램 발송
# ═══════════════════════════════════════════
def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10
        )
        time.sleep(0.3)
    except Exception as e:
        print(f"  텔레그램 발송 실패: {e}")

def send_wr_alerts(changes: list, fx: float, egg_stage: int, budget: dict):
    if not changes:
        return
    lines = [
        f"📊 <b>달걀이론 매수 신호 변화</b>",
        f"달걀 {egg_stage}단계 | 예산 ₩{budget['amount']:,}",
        "━━━━━━━━━━━━━━━━━",
    ]
    for c in changes:
        prev_label = WR_LEVEL_LABEL[c['prev_level']] or '신호없음'
        new_label  = WR_LEVEL_LABEL[c['new_level']]
        alloc = c['allocation']
        lines += [
            f"\n{new_label}",
            f"<b>{c['ticker']}</b> ${c['price']:.2f} ({c['sector']})",
            f"WR: {c['wr']:.1f}  |  이전: {prev_label.split()[0] if c['prev_level'] > 0 else '없음'}",
            f"투자금: ₩{alloc.get('amount', 0):,} ({alloc.get('pct', 0)}%)",
        ]
    lines.append(f"\n환율: ₩{fx:,.0f}")
    tg('\n'.join(lines))
    print(f"  → WR 변화 알림 {len(changes)}건 발송")

def send_rebalancing_alert(rb: dict, egg_stage: int, rs_ranking: list):
    if not rb['dropped'] and not rb['added']:
        return
    lines = [
        f"🔄 <b>분기 리밸런싱 감지</b>",
        f"달걀 {egg_stage}단계 | {datetime.now(KST).strftime('%Y년 %m월')}",
        "━━━━━━━━━━━━━━━━━",
    ]
    if rb['dropped']:
        lines.append("\n🔴 <b>매도 대상 (Top8 탈락)</b>")
        for t in rb['dropped']:
            lines.append(f"  • {t} ({NOBL_UNIVERSE.get(t,'--')})")
    if rb['added']:
        lines.append("\n🟢 <b>신규 편입 (Top8 진입)</b>")
        for t in rb['added']:
            rs = next((x['rs'] for x in rs_ranking if x['ticker']==t), None)
            rs_str = f" | RS +{rs:.1f}%" if rs is not None else ''
            lines.append(f"  • {t} ({NOBL_UNIVERSE.get(t,'--')}){rs_str}")
    lines += ["\n━━━━━━━━━━━━━━━━━", "📌 앱에서 탈락 종목 매도 후 신규 종목 매수하세요"]
    tg('\n'.join(lines))
    print(f"  → 리밸런싱 알림 발송 (탈락 {len(rb['dropped'])}개, 신규 {len(rb['added'])}개)")

def send_daily_summary(egg, budget, signals, fx):
    lines = [
        f"📈 <b>달걀이론 포트폴리오 일일 현황</b>",
        f"달걀 {egg['stage']}단계 | {egg['desc']}",
        f"종합 점수: {egg['score']}점",
        f"이번 달 예산: ₩{budget['amount']:,} ({budget['reason']})",
        f"환율: ₩{fx:,.0f}",
        "매수 신호: 없음",
    ]
    tg('\n'.join(lines))

# ═══════════════════════════════════════════
# 지표 업데이트 리마인더
# ═══════════════════════════════════════════
def send_indicator_reminder(now) -> bool:
    weekday = now.weekday()
    day     = now.day
    hour    = now.hour

    if not (9 <= hour <= 10):
        return False

    lines = []
    mo = load_manual_overrides()

    # ① 매주 금요일: 신규실업청구
    if weekday == 4:
        claims_val = mo.get('claims', '없음')
        lines.append(
            "📋 <b>[주간] 신규실업청구 업데이트 필요</b>\n"
            "어제(목요일) DOL 발표됨\n"
            "→ <a href='https://fred.stlouisfed.org/series/ICSA'>FRED ICSA</a> 확인 후 앱 설정탭에 입력\n"
            f"현재 저장값: {claims_val}건"
        )

    # ② 매주 월요일: HY 스프레드
    if weekday == 0:
        hy_val = mo.get('hy_spread', 'VIX추정')
        lines.append(
            "📋 <b>[주간] HY 스프레드 점검</b>\n"
            "→ <a href='https://fred.stlouisfed.org/series/BAMLH0A0HYM2'>FRED BAMLH0A0HYM2</a> 확인\n"
            f"현재 저장값: {hy_val}%"
        )

    # ③ 매월 26~28일: M2
    if 26 <= day <= 28 and weekday in [0, 1, 2]:
        m2_val = mo.get('m2_yoy', '없음')
        lines.append(
            "📋 <b>[월간] M2 통화량 업데이트 필요</b>\n"
            "→ <a href='https://fred.stlouisfed.org/series/M2SL'>FRED M2SL</a> 확인 후 앱 설정탭에 입력\n"
            f"현재 저장값: {m2_val}%"
        )

    # ④ 매월 둘째 주 수요일: CPI
    if weekday == 2 and 8 <= day <= 14:
        lines.append(
            "ℹ️ <b>[월간] CPI 발표일</b>\n"
            "오늘 밤 22:30 KST BLS CPI 발표 예정\n"
            "→ 내일 워크플로우 실행 시 자동 반영됨"
        )

    # ⑤ 매월 첫째 주 금요일: 고용지표
    if weekday == 4 and day <= 7:
        lines.append(
            "ℹ️ <b>[월간] 고용 지표 발표일</b>\n"
            "오늘 밤 21:30 KST BLS 고용보고서 발표 예정\n"
            "→ 내일 워크플로우 실행 시 실업률 자동 반영됨"
        )

    if lines:
        date_str = now.strftime('%m/%d %a')
        header = f"⏰ <b>경제지표 업데이트 알림</b> ({date_str} KST)\n━━━━━━━━━━━━━━━\n"
        tg(header + '\n━━━━━━━━━━━━━━━\n'.join(lines))
        print(f"  → 지표 리마인더 {len(lines)}건 발송")
        return True
    return False

# ═══════════════════════════════════════════
# 캐시 / 폴백
# ═══════════════════════════════════════════
def load_cache() -> dict:
    try:
        if os.path.exists('prices.json'):
            with open('prices.json', 'r', encoding='utf-8') as f:
                return json.load(f).get('egg', {}).get('indicators', {})
    except:
        pass
    return {}

def load_manual_overrides() -> dict:
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r', encoding='utf-8') as f:
                return json.load(f).get('manual_overrides', {})
    except:
        pass
    return {}

# ═══════════════════════════════════════════
# BLS API
# ═══════════════════════════════════════════
def bls_fetch(series_id: str) -> list | None:
    try:
        r = requests.get(
            f"https://api.bls.gov/publicAPI/v1/timeseries/data/{series_id}",
            timeout=20, headers={'User-Agent': 'egg-portfolio/3.1'}
        )
        d = r.json()
        if d.get('status') == 'REQUEST_SUCCEEDED':
            return d['Results']['series'][0]['data']
    except Exception as e:
        print(f"    BLS [{series_id}] 실패: {e}")
    return None

# ═══════════════════════════════════════════
# 경제 지표 수집
# ═══════════════════════════════════════════
def get_economic_indicators() -> dict:
    cache = load_cache()
    ov    = load_manual_overrides()
    ind   = {}

    def co(key, default=None):
        if ov.get(key)    is not None: return ov[key]
        if cache.get(key) is not None: return cache[key]
        return default

    # ① VIX
    if ov.get('vix') is not None:
        ind['vix'] = ov['vix']
        print(f"    vix         = {ind['vix']} (수동입력)")
    else:
        try:
            ind['vix'] = round(yf.Ticker('^VIX').fast_info.last_price, 2)
        except:
            ind['vix'] = co('vix', 18.0)
        print(f"    vix         = {ind['vix']}")

    # ② 기준금리 (^IRX)
    try:
        ind['fed_rate'] = round(yf.Ticker('^IRX').fast_info.last_price, 2)
    except:
        ind['fed_rate'] = co('fed_rate', 4.5)
    print(f"    fed_rate    = {ind['fed_rate']}%")

    # ③ 장단기 스프레드 (10Y - 3M)
    try:
        t10 = yf.Ticker('^TNX').fast_info.last_price
        ind['spread'] = round(t10 - ind['fed_rate'], 2) if t10 else co('spread', 0.2)
    except:
        ind['spread'] = co('spread', 0.2)
    print(f"    spread      = {ind['spread']}%")

    time.sleep(0.5)

    # ④ CPI YoY
    if ov.get('cpi_yoy') is not None:
        ind['cpi_yoy'] = ov['cpi_yoy']
        print(f"    cpi_yoy     = {ind['cpi_yoy']}% (수동입력)")
    else:
        print("    [BLS] CPI 조회...")
        bls_cpi = bls_fetch('CUUR0000SA0')
        if bls_cpi:
            try:
                rows = sorted(
                    [x for x in bls_cpi if x['period'].startswith('M') and x['period'] != 'M13'],
                    key=lambda x: (x['year'], x['period']), reverse=True
                )
                if len(rows) >= 13:
                    ind['cpi_yoy'] = round((float(rows[0]['value'])/float(rows[12]['value'])-1)*100, 2)
                else:
                    ind['cpi_yoy'] = co('cpi_yoy')
            except:
                ind['cpi_yoy'] = co('cpi_yoy')
        else:
            ind['cpi_yoy'] = co('cpi_yoy')
        print(f"    cpi_yoy     = {ind['cpi_yoy']}%")

    # ⑤ 실업률
    if ov.get('unemp') is not None:
        ind['unemp'] = ov['unemp']
        print(f"    unemp       = {ind['unemp']}% (수동입력)")
    else:
        print("    [BLS] 실업률 조회...")
        bls_unemp = bls_fetch('LNS14000000')
        if bls_unemp:
            try:
                rows = sorted(
                    [x for x in bls_unemp if x['period'].startswith('M') and x['period'] != 'M13'],
                    key=lambda x: (x['year'], x['period']), reverse=True
                )
                ind['unemp'] = float(rows[0]['value'])
            except:
                ind['unemp'] = co('unemp')
        else:
            ind['unemp'] = co('unemp')
        print(f"    unemp       = {ind['unemp']}%")

    # ⑥ M2
    if ov.get('m2_yoy') is not None:
        ind['m2_yoy'] = ov['m2_yoy']
        print(f"    m2_yoy      = {ind['m2_yoy']}% (수동입력)")
    else:
        ind['m2_yoy'] = co('m2_yoy', 3.5)
        print(f"    m2_yoy      = {ind['m2_yoy']}% (캐시)")

    # ⑦ HY 스프레드
    if ov.get('hy_spread') is not None:
        ind['hy_spread'] = ov['hy_spread']
        print(f"    hy_spread   = {ind['hy_spread']}% (수동입력)")
    else:
        vx = ind.get('vix', 18)
        ind['hy_spread'] = (
            2.8 if vx < 13 else 3.0 if vx < 15 else 3.3 if vx < 18 else
            3.8 if vx < 22 else 4.8 if vx < 27 else 5.8 if vx < 35 else 7.0
        )
        print(f"    hy_spread   = {ind['hy_spread']}% (VIX추정)")

    # ⑧ 소비자심리 (다중 소스)
    if ov.get('pmi') is not None:
        ind['pmi'] = ov['pmi']
        print(f"    pmi         = {ind['pmi']} (수동입력)")
    else:
        pmi_val = None

        # 방법1: FRED CSV
        if pmi_val is None:
            try:
                r = requests.get(
                    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UMCSENT",
                    timeout=15, headers={'User-Agent': 'Mozilla/5.0 (compatible; portfolio-bot/1.0)'}
                )
                if r.status_code == 200:
                    csv_lines = [l.strip() for l in r.text.strip().split('\n')
                                 if l.strip() and not l.startswith('DATE')]
                    for line in reversed(csv_lines):
                        parts = line.split(',')
                        if len(parts) >= 2 and parts[1].strip() not in ('.', ''):
                            v = float(parts[1].strip())
                            if 20 < v < 130:
                                pmi_val = round(v, 1)
                                print(f"    pmi(FRED CSV) = {pmi_val}")
                                break
            except Exception as e:
                print(f"    FRED CSV 실패: {e}")

        # 방법2: UMICH 직접
        if pmi_val is None:
            for url in [
                "https://data.sca.isr.umich.edu/get-chart.php?r=1&t=tbmics&f=csv",
                "https://data.sca.isr.umich.edu/get-chart.php?r=2&t=tbmics&f=csv",
            ]:
                try:
                    r = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                    if r.status_code != 200 or not r.text.strip():
                        continue
                    umich_lines = [l.strip() for l in r.text.strip().split('\n') if l.strip()]
                    for line in reversed(umich_lines):
                        parts = line.replace('\t', ',').split(',')
                        for p in reversed(parts):
                            try:
                                v = float(p.strip())
                                if 20 < v < 130:
                                    pmi_val = round(v, 1)
                                    break
                            except:
                                continue
                        if pmi_val is not None:
                            break
                    if pmi_val is not None:
                        print(f"    pmi(UMICH) = {pmi_val}")
                        break
                except Exception as e:
                    print(f"    UMICH 실패: {e}")

        # 캐시 폴백
        if pmi_val is None:
            pmi_val = co('pmi', 56.6)
            print(f"    pmi(캐시폴백) = {pmi_val}")

        ind['pmi'] = pmi_val
        print(f"    pmi         = {ind['pmi']}")

    # ⑨ 신규실업청구
    ind['claims'] = int(co('claims', 220000))
    print(f"    claims      = {ind['claims']:,} ({'수동' if ov.get('claims') else '캐시'})")

    return ind

# ═══════════════════════════════════════════
# 달걀 단계 계산
# ═══════════════════════════════════════════
def calc_egg_stage(ind: dict) -> dict:
    score = 0.0
    details = {}

    fr = ind.get('fed_rate', 0)
    if fr >= 5.0:   score -= 4.0; details['fed_rate'] = f"-4 (고금리 {fr}%)"
    elif fr <= 2.0: score += 2.0; details['fed_rate'] = f"+2 (저금리 {fr}%)"
    else:           details['fed_rate'] = f"0 (중립 {fr}%)"

    ts = ind.get('spread', 0)
    if ts < 0:      score -= 3.5; details['spread'] = f"-3.5 (역전 {ts}%)"
    elif ts >= 0.5: score += 1.5; details['spread'] = f"+1.5 (정상 {ts}%)"
    else:           details['spread'] = f"0 (평탄 {ts}%)"

    vx = ind.get('vix', 0)
    if vx >= 25:   score -= 2.5; details['vix'] = f"-2.5 (공포 {vx})"
    elif vx <= 15: score += 2.0; details['vix'] = f"+2 (안정 {vx})"
    else:          details['vix'] = f"0 (중립 {vx})"

    m2 = ind.get('m2_yoy', 0)
    if m2 <= 0:   score -= 3.0; details['m2_yoy'] = f"-3 (긴축 {m2}%)"
    elif m2 >= 5: score += 2.0; details['m2_yoy'] = f"+2 (완화 {m2}%)"
    else:         details['m2_yoy'] = f"0 (중립 {m2}%)"

    cp = ind.get('cpi_yoy', 0)
    if cp >= 4.0:   score -= 3.0; details['cpi_yoy'] = f"-3 (고물가 {cp}%)"
    elif cp <= 2.5: score += 1.5; details['cpi_yoy'] = f"+1.5 (저물가 {cp}%)"
    else:           details['cpi_yoy'] = f"0 (중립 {cp}%)"

    hy = ind.get('hy_spread', 0)
    if hy >= 4.5:   score -= 3.0; details['hy_spread'] = f"-3 (신용위기 {hy}%)"
    elif hy <= 3.5: score += 1.5; details['hy_spread'] = f"+1.5 (안정 {hy}%)"
    else:           details['hy_spread'] = f"0 (중립 {hy}%)"

    ur = ind.get('unemp', 0)
    if ur >= 4.5:   score -= 2.0; details['unemp'] = f"-2 (높음 {ur}%)"
    elif ur <= 3.8: score += 1.0; details['unemp'] = f"+1 (낮음 {ur}%)"
    else:           details['unemp'] = f"0 (중립 {ur}%)"

    pm = ind.get('pmi', 0)
    if pm <= 50:   score -= 1.5; details['pmi'] = f"-1.5 (위축 {pm})"
    elif pm >= 70: score += 1.0; details['pmi'] = f"+1 (호조 {pm})"
    else:          details['pmi'] = f"0 (중립 {pm})"

    cl = ind.get('claims', 0)
    if cl >= 250000:   score -= 1.0; details['claims'] = f"-1 (급증 {cl:,})"
    elif cl <= 200000: score += 0.5; details['claims'] = f"+0.5 (안정 {cl:,})"
    else:              details['claims'] = f"0 (중립 {cl:,})"

    if score <= -9:   stage = 1
    elif score <= -4: stage = 2
    elif score <= 1:  stage = 3
    elif score <= 5:  stage = 4
    elif score <= 9:  stage = 5
    else:             stage = 6

    return {
        'stage': stage, 'score': round(score, 1),
        'desc': {1:'① 하락 초입',2:'② 하락 본격',3:'③ 상승 초입',
                 4:'④ 상승 본격',5:'⑤ 과열 초입',6:'⑥ 과열 본격'}[stage],
        'indicators': ind, 'scoring_details': details,
    }

# ═══════════════════════════════════════════
# RS 모멘텀
# ═══════════════════════════════════════════
def calc_rs(ticker: str) -> float | None:
    try:
        closes = yf.Ticker(ticker).history(period="1y", interval="1d")['Close'].dropna()
        if len(closes) < 130: return None
        ma200 = closes.iloc[-min(200, len(closes)):].mean()
        if closes.iloc[-1] < ma200: return None
        if len(closes) >= 127:
            v = round((closes.iloc[-22] / closes.iloc[-127] - 1) * 100, 2)
            return None if (math.isnan(v) or math.isinf(v)) else v
    except:
        pass
    return None

def select_top_rs(stage: int, top_n: int = 8) -> tuple[list, list]:
    pref    = STAGE_SECTORS.get(stage, [])
    pref_t  = [t for t, s in NOBL_UNIVERSE.items() if s in pref]
    other_t = [t for t, s in NOBL_UNIVERSE.items() if s not in pref]
    scores  = []

    print(f"  선호 섹터 {len(pref_t)}개 RS 계산...")
    for t in pref_t:
        time.sleep(0.2)
        rs = calc_rs(t)
        if rs is not None:
            scores.append({'ticker': t, 'sector': NOBL_UNIVERSE[t], 'rs': rs})
    scores.sort(key=lambda x: x['rs'], reverse=True)

    if len(scores) < top_n:
        for t in other_t:
            if len(scores) >= top_n + 2: break
            time.sleep(0.2)
            rs = calc_rs(t)
            if rs is not None:
                scores.append({'ticker': t, 'sector': NOBL_UNIVERSE[t], 'rs': rs})
        scores.sort(key=lambda x: x['rs'], reverse=True)

    top_scores = scores[:top_n]
    selected   = [x['ticker'] for x in top_scores]
    if len(selected) < top_n:
        for t in pref_t + other_t:
            if t not in selected: selected.append(t)
            if len(selected) >= top_n: break
    return selected[:top_n], top_scores

# ═══════════════════════════════════════════
# 데이터 조회
# ═══════════════════════════════════════════
def get_weekly_wr(ticker: str, periods: int = 14) -> float | None:
    try:
        h = yf.Ticker(ticker).history(period="6mo", interval="1wk")
        if len(h) < periods: return None
        hi = h['High'].iloc[-periods:].max()
        lo = h['Low'].iloc[-periods:].min()
        cl = h['Close'].iloc[-1]
        if hi == lo: return None
        result = round((hi - cl) / (hi - lo) * -100, 1)
        return None if (math.isnan(result) or math.isinf(result)) else result
    except:
        return None

def get_price(ticker: str) -> dict:
    try:
        fi   = yf.Ticker(ticker).fast_info
        p    = fi.last_price
        prev = fi.previous_close
        chg  = p - prev
        return {
            'price':  round(p, 2),
            'prev':   round(prev, 2),
            'change': round(chg, 2),
            'pct':    round(chg / prev * 100, 2) if prev else 0.0,
        }
    except:
        return {'price': 0, 'prev': 0, 'change': 0, 'pct': 0}

def get_fx_rate() -> float:
    try:    return round(yf.Ticker("KRW=X").fast_info.last_price, 2)
    except: return 1350.0

def get_benchmarks() -> dict:
    result = {}
    for t in ['VOO', 'QQQ', 'NOBL']:
        try:
            h = yf.Ticker(t).history(period="1y", interval="1wk")['Close']
            if not h.empty:
                base = h.iloc[0]
                result[t] = {
                    'dates': h.index.strftime('%Y-%m-%d').tolist(),
                    'pct':   [round((v / base - 1) * 100, 2) for v in h],
                }
        except:
            pass
    return result

# ═══════════════════════════════════════════
# VIX 예산 조정
# ═══════════════════════════════════════════
def adjust_budget(vix, base: int) -> dict:
    if not vix: return {'amount': base, 'multiplier': 1.0, 'reason': '정상 투자', 'base': base}
    if vix >= 30:   m, r = 0.50, f"극심한 공포(VIX {vix:.0f}) -50%"
    elif vix >= 25: m, r = 0.70, f"시장 공포(VIX {vix:.0f}) -30%"
    elif vix >= 20: m, r = 0.85, f"불안(VIX {vix:.0f}) -15%"
    elif vix <= 12: m, r = 1.40, f"극저변동성(VIX {vix:.0f}) +40%"
    elif vix <= 15: m, r = 1.20, f"시장 안정(VIX {vix:.0f}) +20%"
    else:           m, r = 1.00, f"중립(VIX {vix:.0f})"
    return {'amount': int(base * m), 'multiplier': m, 'reason': r, 'base': base}

def calc_allocation(wr, budget: int) -> dict:
    if wr is None or wr > -60: return {'pct': 0,  'amount': 0,               'signal': 'NONE'}
    if wr <= -85:              return {'pct': 30, 'amount': int(budget*0.30), 'signal': 'STRONG'}
    if wr <= -70:              return {'pct': 40, 'amount': int(budget*0.40), 'signal': 'MEDIUM'}
    return                            {'pct': 30, 'amount': int(budget*0.30), 'signal': 'WEAK'}

def months_elapsed(start: str) -> int:
    try:
        s = datetime.strptime(start, '%Y-%m-%d')
        n = datetime.now()
        return max(0, (n.year - s.year) * 12 + n.month - s.month)
    except:
        return 0

# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════
def main():
    now = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} KST] 시스템 시작")
    print(f"{'='*50}\n")

    prev = load_previous()
    print(f"[0] 이전 prices.json: top_tickers={prev.get('top_tickers', [])}\n")

    print("[1/8] 경제 지표 수집...")
    ind = get_economic_indicators()
    egg = calc_egg_stage(ind)
    print(f"  → 달걀 {egg['stage']}단계 | {egg['desc']} | 점수: {egg['score']}\n")

    print("[2/8] 환율 조회...")
    fx = get_fx_rate()
    print(f"  → ₩{fx:,.0f}\n")

    print("[3/8] VIX 기반 예산 조정...")
    budget = adjust_budget(ind.get('vix'), MONTHLY_BUDGET)
    print(f"  → ₩{budget['amount']:,} ({budget['reason']})\n")

    print(f"[4/8] RS 모멘텀 Top 8 선정 (달걀 {egg['stage']}단계)...")
    top_tickers, rs_ranking = select_top_rs(egg['stage'])
    print(f"  → {top_tickers}\n")

    print("[5/8] 주가 및 WR 계산...")
    stock_data = {}
    for t in top_tickers:
        time.sleep(0.4)
        p     = get_price(t)
        wr    = get_weekly_wr(t)
        alloc = calc_allocation(wr, budget['amount'])
        stock_data[t] = {
            **p, 'wr': wr,
            'sector': NOBL_UNIVERSE.get(t, '--'),
            'rs':     next((x['rs'] for x in rs_ranking if x['ticker'] == t), None),
            'allocation': alloc,
        }
        print(f"  {t:6} ${p['price']:>8.2f} | WR {str(wr):>7} | {alloc['signal']}")
    print()

    signals = sorted(
        [{'ticker': t, 'sector': NOBL_UNIVERSE.get(t, '--'), **v}
         for t, v in stock_data.items() if v['allocation']['signal'] != 'NONE'],
        key=lambda x: x.get('wr') or 0
    )
    print(f"[6/8] 현재 신호: {len(signals)}개\n")

    print("[6-1] WR 단계 변화 감지...")
    wr_changes = detect_wr_changes(prev, stock_data)
    print(f"  → {'단계 변화 ' + str(len(wr_changes)) + '건' if wr_changes else '변화 없음'}")

    print("[6-2] 리밸런싱 감지...")
    rb = detect_rebalancing(prev, top_tickers)
    do_rb = is_rebalancing_month() and (rb['dropped'] or rb['added'])
    print(f"  → {'리밸런싱 감지! 탈락: ' + str(rb['dropped']) if do_rb else '변화 없음'}\n")

    print("[7/8] 포트폴리오 계산...")
    holdings = []
    ps = {'total_value_usd': 0, 'total_value_krw': 0, 'total_pnl_pct': 0, 'total_invested_krw': 0}
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r', encoding='utf-8') as f:
                pf = json.load(f)
            for h in pf.get('holdings', []):
                t   = h['ticker']
                p   = stock_data.get(t) or get_price(t)
                cur = p.get('price', 0)
                avg = h.get('avg_price_usd', 0)
                sh  = h.get('shares', 0)
                wr  = stock_data.get(t, {}).get('wr') or get_weekly_wr(t)
                holdings.append({
                    'ticker':             t,
                    'sector':             h.get('sector', NOBL_UNIVERSE.get(t, '--')),
                    'shares':             sh,
                    'avg_price_usd':      avg,
                    'current_price':      cur,
                    'current_value_usd':  round(cur * sh, 2),
                    'current_value_krw':  round(cur * sh * fx),
                    'pnl_pct':            round((cur / avg - 1) * 100, 2) if avg > 0 else 0,
                    'day_change_pct':     p.get('pct', 0),
                    'wr':                 wr,
                    'dividends_usd':      h.get('dividends_received_usd', 0),
                })
            tusd = sum(h['current_value_usd'] for h in holdings)
            cost = sum(h['shares'] * h['avg_price_usd'] for h in holdings)
            ps = {
                'total_value_usd':    round(tusd, 2),
                'total_value_krw':    round(tusd * fx),
                'total_pnl_pct':      round((tusd / cost - 1) * 100, 2) if cost > 0 else 0,
                'total_invested_krw': months_elapsed(START_DATE) * MONTHLY_BUDGET,
            }
    except Exception as e:
        print(f"  포트폴리오 오류: {e}")

    print("[8/8] 벤치마크 + prices.json 저장...")
    benchmarks = get_benchmarks()
    output = {
        'updated_at':     now.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate':        fx,
        'egg':            egg,
        'budget':         budget,
        'top_tickers':    top_tickers,
        'rs_ranking':     rs_ranking,
        'stock_data':     stock_data,
        'signals':        signals,
        'holdings':       holdings,
        'portfolio_summary': ps,
        'benchmarks':     benchmarks,
        'settings': {
            'monthly_budget':  MONTHLY_BUDGET,
            'start_date':      START_DATE,
            'months_elapsed':  months_elapsed(START_DATE),
        },
    }

    # NaN/Inf 완전 제거 후 저장
    output = sanitize(output)
    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=serial)
    print("  → prices.json 저장 완료\n")

    # ══ 텔레그램 알림 발송 ══
    print("[알림] 텔레그램 발송...")

    if wr_changes:
        send_wr_alerts(wr_changes, fx, egg['stage'], budget)

    if do_rb:
        send_rebalancing_alert(rb, egg['stage'], rs_ranking)

    if signals and not wr_changes and 7 <= now.hour <= 10:
        sig_lines = [
            f"🔔 <b>달걀이론 기존 매수 신호 유지</b>",
            f"달걀 {egg['stage']}단계 | ₩{budget['amount']:,}",
        ]
        for s in signals:
            icon = "🔴" if s['allocation']['signal'] == 'STRONG' else "🟡" if s['allocation']['signal'] == 'MEDIUM' else "🟢"
            sig_lines.append(f"{icon} <b>{s['ticker']}</b> WR {s['wr']:.1f} | ₩{s['allocation']['amount']:,}")
        tg('\n'.join(sig_lines))

    if not signals and not wr_changes and 7 <= now.hour <= 10:
        send_daily_summary(egg, budget, signals, fx)

    send_indicator_reminder(now)

    print("🎉 모든 작업 완료!")


if __name__ == '__main__':
    main()
