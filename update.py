"""
선행형 매크로-모멘텀 자동 투자 시스템 v4.0
변경사항:
- Top8 엄격 필터: 달걀 단계 선호 섹터만 허용, 보충 없음
- 보유종목 + Top8 통합 배정금 계산 (탈락 보유종목 포함)
- 12일 자동 입금 (월 1회 중복 방지)
- 보유 탈락 종목도 WR 계산 및 prices.json 포함
- JOBY 고정 감시 종목 유지
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
# 고정 감시 종목 (Top8 외 별도 WR 감시)
# ═══════════════════════════════════════════
WATCH_TICKERS = {
    'JOBY': 'UAM',
}

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
    if wr is None: return 0
    if isinstance(wr, float) and (math.isnan(wr) or math.isinf(wr)): return 0
    if wr > -60: return 0
    if wr > -70: return 1
    if wr > -85: return 2
    return 3

WR_LEVEL_LABEL = {
    0: None,
    1: '🟢 약매수 (WR ≤ -60, 30%)',
    2: '🟡 중매수 (WR ≤ -70, 40%)',
    3: '🔴 강매수 (WR ≤ -85, 30%)',
}

# ═══════════════════════════════════════════
# JSON 직렬화
# ═══════════════════════════════════════════
def sanitize(obj):
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
# portfolio.json 로드/저장
# ═══════════════════════════════════════════
def load_portfolio() -> dict:
    try:
        if os.path.exists('portfolio.json'):
            with open('portfolio.json', 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

def save_portfolio(pf: dict):
    with open('portfolio.json', 'w', encoding='utf-8') as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════════
# 12일 자동 입금 처리
# ═══════════════════════════════════════════
def auto_deposit_if_needed(now: datetime, budget_amount: int) -> bool:
    """
    매월 12일 자동 입금
    - portfolio.json의 last_deposit_ym 확인해 중복 방지
    - 반환: 입금 발생 여부
    """
    if now.day != 12:
        return False

    pf = load_portfolio()
    this_ym = now.strftime('%Y-%m')
    last_ym = pf.get('last_deposit_ym', '')

    if last_ym == this_ym:
        print(f"  → 이미 {this_ym} 입금 완료 (스킵)")
        return False

    # 입금 기록 추가
    pf.setdefault('extra_deposits', [])
    pf['extra_deposits'].append({
        'date':       now.strftime('%Y-%m-%d'),
        'amount_krw': budget_amount,
        'note':       f'자동 월정기 입금 ({this_ym})',
    })
    pf['last_deposit_ym'] = this_ym
    save_portfolio(pf)
    print(f"  → 자동 입금 완료: ₩{budget_amount:,} ({this_ym})")
    return True

# ═══════════════════════════════════════════
# 보유 탈락 종목 조회 (리밸런싱 전까지 유지)
# ═══════════════════════════════════════════
def get_held_dropped_tickers(top_tickers: list) -> list:
    """
    portfolio.json holdings 중 Top8에 없는 종목 반환
    (WATCH_TICKERS 제외)
    """
    pf = load_portfolio()
    held = [h['ticker'] for h in pf.get('holdings', []) if h.get('shares', 0) > 0.0001]
    dropped = [t for t in held if t not in top_tickers and t not in WATCH_TICKERS]
    return dropped

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
        if ticker in WATCH_TICKERS:
            continue
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
                'held_drop':  data.get('held_drop', False),
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
        held_tag = " 🔒보유탈락" if c.get('held_drop') else ""
        lines += [
            f"\n{new_label}",
            f"<b>{c['ticker']}</b>{held_tag} ${c['price']:.2f} ({c['sector']})",
            f"WR: {c['wr']:.1f}  |  이전: {prev_label.split()[0] if c['prev_level'] > 0 else '없음'}",
            f"배정금: ₩{alloc.get('amount', 0):,}",
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

def send_daily_summary(egg, budget, signals, fx, held_signals=None):
    lines = [
        f"📈 <b>달걀이론 포트폴리오 일일 현황</b>",
        f"달걀 {egg['stage']}단계 | {egg['desc']}",
        f"종합 점수: {egg['score']}점",
        f"이번 달 예산: ₩{budget['amount']:,} ({budget['reason']})",
        f"환율: ₩{fx:,.0f}",
    ]
    all_sigs = (signals or []) + (held_signals or [])
    if all_sigs:
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append(f"📌 현재 매수 신호 {len(all_sigs)}개")
        for s in all_sigs:
            icon = "🔴" if s['allocation']['signal']=='STRONG' else "🟡" if s['allocation']['signal']=='MEDIUM' else "🟢"
            wr_str = f"{s['wr']:.1f}" if s.get('wr') else "--"
            tag = " 🔒" if s.get('held_drop') else ""
            lines.append(f"{icon} <b>{s['ticker']}</b>{tag} WR {wr_str} | ₩{s['allocation']['amount']:,}")
    else:
        lines.append("매수 신호: 없음")
    tg('\n'.join(lines))

# ═══════════════════════════════════════════
# 달걀 단계 변화 감지
# ═══════════════════════════════════════════
def detect_egg_change(prev: dict, egg: dict) -> bool:
    prev_stage = prev.get('egg', {}).get('stage')
    new_stage  = egg.get('stage')
    return prev_stage is not None and prev_stage != new_stage

def send_egg_change_alert(prev: dict, egg: dict, budget: dict, fx: float):
    prev_stage = prev.get('egg', {}).get('stage', '?')
    new_stage  = egg['stage']
    prev_desc  = prev.get('egg', {}).get('desc', '')
    new_desc   = egg['desc']
    direction  = '📈' if new_stage > prev_stage else '📉'
    stage_sectors = {
        1: '필수소비재 · 헬스케어 · 유틸리티',
        2: '필수소비재 · 헬스케어 · 유틸리티',
        3: '금융 · 산업재 · 소재',
        4: '기술 · 경기소비재 · 금융',
        5: '헬스케어 · 경기소비재 · 기술',
        6: '에너지 · 소재 · 산업재',
    }
    lines = [
        f"{direction} <b>달걀 단계 변화!</b>",
        f"{prev_stage}단계 ({prev_desc}) → <b>{new_stage}단계 ({new_desc})</b>",
        f"종합 점수: {egg['score']}점",
        "━━━━━━━━━━━━━━━━━",
        f"▶ 선호 섹터: {stage_sectors.get(new_stage, '--')}",
        f"▶ 이번 달 예산: ₩{budget['amount']:,} ({budget['reason']})",
        f"▶ 환율: ₩{fx:,.0f}",
        "━━━━━━━━━━━━━━━━━",
        "📌 RS 모멘텀 Top8 및 섹터 배분이 자동 재계산됩니다.",
    ]
    tg('\n'.join(lines))
    print(f"  → 달걀 단계 변화 알림 ({prev_stage}→{new_stage}단계)")

# ═══════════════════════════════════════════
# 자동 지표 임계값 알림
# ═══════════════════════════════════════════
def send_indicator_threshold_alerts(prev: dict, ind: dict):
    prev_ind = prev.get('egg', {}).get('indicators', {})
    alerts   = []
    prev_sp = prev_ind.get('spread')
    new_sp  = ind.get('spread')
    if prev_sp is not None and new_sp is not None:
        if prev_sp >= 0 and new_sp < 0:
            alerts.append(
                "⚠️ <b>[경보] 장단기 스프레드 역전!</b>\n"
                f"이전: +{prev_sp:.2f}% → 현재: {new_sp:.2f}%\n"
                "경기 침체 선행 신호입니다."
            )
        elif prev_sp < 0 and new_sp >= 0:
            alerts.append(
                "✅ <b>[정상화] 스프레드 역전 해소</b>\n"
                f"이전: {prev_sp:.2f}% → 현재: +{new_sp:.2f}%"
            )
    prev_cpi = prev_ind.get('cpi_yoy')
    new_cpi  = ind.get('cpi_yoy')
    if prev_cpi is not None and new_cpi is not None:
        if prev_cpi < 3.0 and new_cpi >= 3.0:
            alerts.append(f"🔴 <b>[경보] CPI 3% 돌파!</b>\n{prev_cpi:.2f}% → {new_cpi:.2f}%")
        elif prev_cpi >= 3.0 and new_cpi < 3.0:
            alerts.append(f"✅ <b>[안정] CPI 3% 이하 복귀</b>\n{prev_cpi:.2f}% → {new_cpi:.2f}%")
        elif prev_cpi > 2.5 and new_cpi <= 2.5:
            alerts.append(f"🟢 <b>[호재] CPI 2.5% 이하 진입</b>\n{prev_cpi:.2f}% → {new_cpi:.2f}%")
    prev_ur = prev_ind.get('unemp')
    new_ur  = ind.get('unemp')
    if prev_ur is not None and new_ur is not None:
        if prev_ur < 4.5 and new_ur >= 4.5:
            alerts.append(f"🔴 <b>[경보] 실업률 4.5% 돌파!</b>\n{prev_ur:.1f}% → {new_ur:.1f}%")
        elif prev_ur >= 4.5 and new_ur < 4.5:
            alerts.append(f"✅ <b>[개선] 실업률 4.5% 이하 복귀</b>\n{prev_ur:.1f}% → {new_ur:.1f}%")
    for msg in alerts:
        tg(msg)
    if alerts:
        print(f"  → 지표 임계값 알림 {len(alerts)}건 발송")

# ═══════════════════════════════════════════
# 금요일 WR 신호 요약 (보유탈락 + JOBY 포함)
# ═══════════════════════════════════════════
def send_friday_wr_summary(signals: list, held_signals: list, egg: dict,
                           budget: dict, fx: float, watch_data: dict = None):
    now = datetime.now(KST)
    if now.weekday() != 4 or not (7 <= now.hour <= 8):
        return
    lines = [
        f"📅 <b>금요일 WR 매수 신호 요약</b>",
        f"달걀 {egg['stage']}단계 | {egg['desc']}",
        f"이번 달 예산: ₩{budget['amount']:,}",
        f"환율: ₩{fx:,.0f}",
        "━━━━━━━━━━━━━━━━━",
    ]
    if signals:
        lines.append("📊 <b>Top8 매수 신호</b>")
        for s in signals:
            icon = "🔴" if s['allocation']['signal']=='STRONG' else "🟡" if s['allocation']['signal']=='MEDIUM' else "🟢"
            wr_str = f"{s['wr']:.1f}" if s.get('wr') else "--"
            lines.append(f"{icon} <b>{s['ticker']}</b> ${s.get('price',0):.2f} | WR {wr_str} | ₩{s['allocation']['amount']:,}")
    else:
        lines.append("Top8 매수 신호 없음 (WR ≤ -60 미충족)")

    if held_signals:
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("🔒 <b>보유 탈락 종목 신호</b>")
        for s in held_signals:
            icon = "🔴" if s['allocation']['signal']=='STRONG' else "🟡" if s['allocation']['signal']=='MEDIUM' else "🟢"
            wr_str = f"{s['wr']:.1f}" if s.get('wr') else "--"
            lines.append(f"{icon} <b>{s['ticker']}</b> ${s.get('price',0):.2f} | WR {wr_str} | ₩{s['allocation']['amount']:,}")

    if watch_data:
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("✈️ <b>고정 감시 종목</b>")
        for ticker, data in watch_data.items():
            w_lv  = wr_level(data.get('wr'))
            w_lbl = WR_LEVEL_LABEL.get(w_lv)
            wr_str = f"{data.get('wr'):.1f}" if data.get('wr') is not None else "--"
            if w_lbl:
                icon = "🔴" if w_lv==3 else "🟡" if w_lv==2 else "🟢"
                lines.append(f"{icon} <b>{ticker}</b> ${data.get('price',0):.2f} | WR {wr_str} — 배정금 없음")
            else:
                lines.append(f"  • <b>{ticker}</b> ${data.get('price',0):.2f} | WR {wr_str} | 신호없음")

    lines += ["━━━━━━━━━━━━━━━━━", "📌 오늘 매수 후 앱 거래탭에 기록해주세요."]
    tg('\n'.join(lines))
    cnt = len(signals) + len(held_signals) + len(watch_data or {})
    print(f"  → 금요일 WR 요약 알림 발송 (총 {cnt}개)")

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
    if weekday == 4:
        claims_val = mo.get('claims', '없음')
        lines.append(
            "📋 <b>[주간] 신규실업청구 업데이트 필요</b>\n"
            "→ <a href='https://fred.stlouisfed.org/series/ICSA'>FRED ICSA</a>\n"
            f"현재: {claims_val}건"
        )
    if weekday == 0:
        hy_val = mo.get('hy_spread', 'VIX추정')
        lines.append(
            "📋 <b>[주간] HY 스프레드 점검</b>\n"
            "→ <a href='https://fred.stlouisfed.org/series/BAMLH0A0HYM2'>FRED BAMLH0A0HYM2</a>\n"
            f"현재: {hy_val}%"
        )
    if 26 <= day <= 28 and weekday in [0, 1, 2]:
        m2_val = mo.get('m2_yoy', '없음')
        lines.append(
            "📋 <b>[월간] M2 통화량 업데이트 필요</b>\n"
            "→ <a href='https://fred.stlouisfed.org/series/M2SL'>FRED M2SL</a>\n"
            f"현재: {m2_val}%"
        )
    if weekday == 2 and 8 <= day <= 14:
        lines.append("ℹ️ <b>[월간] CPI 발표일</b>\n오늘 밤 22:30 KST")
    if weekday == 4 and day <= 7:
        lines.append("ℹ️ <b>[월간] 고용보고서 발표일</b>\n오늘 밤 21:30 KST")
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
        pf = load_portfolio()
        return pf.get('manual_overrides', {})
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
            timeout=20, headers={'User-Agent': 'egg-portfolio/4.0'}
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

    if ov.get('vix') is not None:
        ind['vix'] = ov['vix']
        print(f"    vix         = {ind['vix']} (수동)")
    else:
        try:    ind['vix'] = round(yf.Ticker('^VIX').fast_info.last_price, 2)
        except: ind['vix'] = co('vix', 18.0)
        print(f"    vix         = {ind['vix']}")

    try:    ind['fed_rate'] = round(yf.Ticker('^IRX').fast_info.last_price, 2)
    except: ind['fed_rate'] = co('fed_rate', 4.5)
    print(f"    fed_rate    = {ind['fed_rate']}%")

    try:
        t10 = yf.Ticker('^TNX').fast_info.last_price
        ind['spread'] = round(t10 - ind['fed_rate'], 2) if t10 else co('spread', 0.2)
    except: ind['spread'] = co('spread', 0.2)
    print(f"    spread      = {ind['spread']}%")

    time.sleep(0.5)

    if ov.get('cpi_yoy') is not None:
        ind['cpi_yoy'] = ov['cpi_yoy']
        print(f"    cpi_yoy     = {ind['cpi_yoy']}% (수동)")
    else:
        print("    [BLS] CPI 조회...")
        bls_cpi = bls_fetch('CUUR0000SA0')
        if bls_cpi:
            try:
                rows = sorted(
                    [x for x in bls_cpi if x['period'].startswith('M') and x['period'] != 'M13'],
                    key=lambda x: (x['year'], x['period']), reverse=True
                )
                ind['cpi_yoy'] = round((float(rows[0]['value'])/float(rows[12]['value'])-1)*100, 2) if len(rows)>=13 else co('cpi_yoy')
            except: ind['cpi_yoy'] = co('cpi_yoy')
        else: ind['cpi_yoy'] = co('cpi_yoy')
        print(f"    cpi_yoy     = {ind['cpi_yoy']}%")

    if ov.get('unemp') is not None:
        ind['unemp'] = ov['unemp']
        print(f"    unemp       = {ind['unemp']}% (수동)")
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
            except: ind['unemp'] = co('unemp')
        else: ind['unemp'] = co('unemp')
        print(f"    unemp       = {ind['unemp']}%")

    if ov.get('m2_yoy') is not None:
        ind['m2_yoy'] = ov['m2_yoy']
        print(f"    m2_yoy      = {ind['m2_yoy']}% (수동)")
    else:
        ind['m2_yoy'] = co('m2_yoy', 3.5)
        print(f"    m2_yoy      = {ind['m2_yoy']}% (캐시)")

    if ov.get('hy_spread') is not None:
        ind['hy_spread'] = ov['hy_spread']
        print(f"    hy_spread   = {ind['hy_spread']}% (수동)")
    else:
        vx = ind.get('vix', 18)
        ind['hy_spread'] = (
            2.8 if vx<13 else 3.0 if vx<15 else 3.3 if vx<18 else
            3.8 if vx<22 else 4.8 if vx<27 else 5.8 if vx<35 else 7.0
        )
        print(f"    hy_spread   = {ind['hy_spread']}% (VIX추정)")

    if ov.get('pmi') is not None:
        ind['pmi'] = ov['pmi']
        print(f"    pmi         = {ind['pmi']} (수동)")
    else:
        pmi_val = None
        try:
            r = requests.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UMCSENT",
                timeout=15, headers={'User-Agent': 'Mozilla/5.0'}
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
                            print(f"    pmi(FRED) = {pmi_val}")
                            break
        except Exception as e:
            print(f"    FRED CSV 실패: {e}")
        if pmi_val is None:
            pmi_val = co('pmi', 56.6)
            print(f"    pmi(캐시) = {pmi_val}")
        ind['pmi'] = pmi_val
        print(f"    pmi         = {ind['pmi']}")

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
# RS 모멘텀 — 엄격 필터 (보충 없음)
# ═══════════════════════════════════════════
def calc_rs(ticker: str) -> float | None:
    try:
        closes = yf.Ticker(ticker).history(period="1y", interval="1d")['Close'].dropna()
        if len(closes) < 130: return None
        ma200 = closes.iloc[-min(200, len(closes)):].mean()
        if closes.iloc[-1] < ma200: return None   # 200MA 이하 제외
        if len(closes) >= 127:
            v = round((closes.iloc[-22] / closes.iloc[-127] - 1) * 100, 2)
            return None if (math.isnan(v) or math.isinf(v)) else v
    except:
        pass
    return None

def select_top_rs(stage: int, top_n: int = 8) -> tuple[list, list]:
    """
    ★ 엄격 필터: 달걀 단계 선호 섹터 종목만 허용
    → 200MA 위 + 해당 섹터만 RS 랭킹
    → 종목 수 부족해도 보충 없음 (6개면 6개로 운용)
    """
    pref   = STAGE_SECTORS.get(stage, [])
    pref_t = [t for t, s in NOBL_UNIVERSE.items() if s in pref]
    scores = []

    print(f"  선호 섹터 {pref} — {len(pref_t)}개 종목 RS 계산 (엄격 필터)...")
    for t in pref_t:
        time.sleep(0.2)
        rs = calc_rs(t)
        if rs is not None:
            scores.append({'ticker': t, 'sector': NOBL_UNIVERSE[t], 'rs': rs})

    scores.sort(key=lambda x: x['rs'], reverse=True)
    top_scores = scores[:top_n]
    selected   = [x['ticker'] for x in top_scores]

    print(f"  → 선정 {len(selected)}개 (200MA 통과 + 섹터 조건 충족)")
    return selected, top_scores

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

def calc_allocation_by_surplus(wr, surplus: int) -> dict:
    """
    ★ 잉여현금 기반 % 매수
    매주 잉여현금에서 WR 단계별 % 차감
    """
    if wr is None or wr > -60 or surplus <= 0:
        return {'pct': 0, 'amount': 0, 'signal': 'NONE'}
    if wr <= -85:
        pct = 30
        return {'pct': pct, 'amount': int(surplus * pct / 100), 'signal': 'STRONG'}
    if wr <= -70:
        pct = 40
        return {'pct': pct, 'amount': int(surplus * pct / 100), 'signal': 'MEDIUM'}
    pct = 30
    return {'pct': pct, 'amount': int(surplus * pct / 100), 'signal': 'WEAK'}

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
    print(f"[{now.strftime('%Y-%m-%d %H:%M')} KST] 시스템 시작 v4.0")
    print(f"{'='*50}\n")

    prev = load_previous()
    print(f"[0] 이전 prices.json: top_tickers={prev.get('top_tickers', [])}\n")

    # ── 12일 자동 입금
    print("[0-1] 12일 자동 입금 확인...")
    deposited = auto_deposit_if_needed(now, MONTHLY_BUDGET)

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

    # ── Top8 선정 (엄격 필터)
    print(f"[4/8] RS 모멘텀 Top8 선정 (달걀 {egg['stage']}단계 — 엄격 필터)...")
    top_tickers, rs_ranking = select_top_rs(egg['stage'])
    print(f"  → Top8: {top_tickers}\n")

    # ── 보유 탈락 종목 확인
    held_dropped = get_held_dropped_tickers(top_tickers)
    if held_dropped:
        print(f"  → 보유 탈락 종목: {held_dropped} (리밸런싱 전 유지)\n")

    # ── 배정 종목 수 계산 (Top8 + 보유탈락)
    all_active_tickers = top_tickers + held_dropped
    n_tickers = len(all_active_tickers)
    per_ticker_budget = int(budget['amount'] / n_tickers) if n_tickers > 0 else budget['amount']
    print(f"  → 배정 종목 수: {n_tickers}개 (Top{len(top_tickers)} + 보유탈락 {len(held_dropped)}개)")
    print(f"  → 종목당 배정금: ₩{per_ticker_budget:,}\n")

    # ── 잉여현금 계산 함수 (portfolio.json 기반)
    pf = load_portfolio()
    holdings_map = {h['ticker']: h for h in pf.get('holdings', [])}

    def get_invested(ticker):
        """거래 내역에서 실제 투자금 계산"""
        total = 0
        for tx in pf.get('transactions', []):
            k = (tx.get('shares', 0) * tx.get('price_usd', 0) * tx.get('fx', fx))
            if tx.get('ticker') == ticker:
                if tx.get('type') in ('buy', 'div_reinvest'): total += k
                elif tx.get('type') == 'sell': total -= k
        return max(0, total)

    def get_total_alloc(ticker):
        """총 배정금 = 누적 배정금 (월별 per_ticker_budget × 경과월) + 보너스"""
        months = months_elapsed(START_DATE)
        base = per_ticker_budget * months
        stg = (pf.get('signal_stages') or {}).get(ticker, {})
        bonus = stg.get('bonus', 0) if stg else 0
        return base + bonus

    def get_surplus(ticker):
        alloc   = get_total_alloc(ticker)
        invested = get_invested(ticker)
        return max(0, alloc - invested)

    print("[5/8] 주가 및 WR 계산...")
    stock_data = {}

    # Top8 종목
    for t in top_tickers:
        time.sleep(0.4)
        p       = get_price(t)
        wr      = get_weekly_wr(t)
        surplus = get_surplus(t)
        alloc   = calc_allocation_by_surplus(wr, surplus)
        stock_data[t] = {
            **p, 'wr': wr,
            'sector':     NOBL_UNIVERSE.get(t, '--'),
            'rs':         next((x['rs'] for x in rs_ranking if x['ticker'] == t), None),
            'allocation': alloc,
            'surplus':    surplus,
            'per_budget': per_ticker_budget,
            'held_drop':  False,
        }
        print(f"  {t:6} ${p['price']:>8.2f} | WR {str(wr):>7} | 잉여 ₩{surplus:,} | {alloc['signal']}")

    # 보유 탈락 종목 (WR 감시 유지)
    for t in held_dropped:
        time.sleep(0.4)
        p       = get_price(t)
        wr      = get_weekly_wr(t)
        surplus = get_surplus(t)
        alloc   = calc_allocation_by_surplus(wr, surplus)
        stock_data[t] = {
            **p, 'wr': wr,
            'sector':     NOBL_UNIVERSE.get(t, '--'),
            'rs':         None,
            'allocation': alloc,
            'surplus':    surplus,
            'per_budget': per_ticker_budget,
            'held_drop':  True,   # 탈락 보유 표시
        }
        print(f"  {t:6} ${p['price']:>8.2f} | WR {str(wr):>7} | 잉여 ₩{surplus:,} | {alloc['signal']} 🔒보유탈락")
    print()

    # 고정 감시 종목 (JOBY)
    watch_data = {}
    for w_ticker, w_sector in WATCH_TICKERS.items():
        time.sleep(0.4)
        w_p  = get_price(w_ticker)
        w_wr = get_weekly_wr(w_ticker)
        w_lv = wr_level(w_wr)
        watch_data[w_ticker] = {
            **w_p, 'wr': w_wr, 'sector': w_sector,
            'rs': None, 'fixed': True,
            'allocation': {'pct': 0, 'amount': 0, 'signal': 'NONE'},
        }
        stock_data[w_ticker] = watch_data[w_ticker]
        print(f"  {w_ticker:6} ${w_p['price']:>8.2f} | WR {str(w_wr):>7} | {WR_LEVEL_LABEL.get(w_lv) or '신호없음'} [고정감시]")
    print()

    # 신호 목록 분리
    signals = sorted(
        [{'ticker': t, 'sector': NOBL_UNIVERSE.get(t, '--'), **v}
         for t, v in stock_data.items()
         if v['allocation']['signal'] != 'NONE'
         and not v.get('held_drop', False)
         and t not in WATCH_TICKERS],
        key=lambda x: x.get('wr') or 0
    )
    held_signals = sorted(
        [{'ticker': t, 'sector': NOBL_UNIVERSE.get(t, '--'), **v}
         for t, v in stock_data.items()
         if v['allocation']['signal'] != 'NONE' and v.get('held_drop', False)],
        key=lambda x: x.get('wr') or 0
    )
    print(f"[6/8] Top8 신호: {len(signals)}개 | 보유탈락 신호: {len(held_signals)}개\n")

    print("[6-1] WR 단계 변화 감지...")
    wr_changes = detect_wr_changes(prev, stock_data)
    print(f"  → {'단계 변화 ' + str(len(wr_changes)) + '건' if wr_changes else '변화 없음'}")

    print("[6-2] 리밸런싱 감지...")
    rb    = detect_rebalancing(prev, top_tickers)
    do_rb = is_rebalancing_month() and (rb['dropped'] or rb['added'])
    print(f"  → {'리밸런싱 감지! 탈락: ' + str(rb['dropped']) if do_rb else '변화 없음'}\n")

    print("[7/8] 포트폴리오 계산...")
    holdings = []
    ps = {'total_value_usd': 0, 'total_value_krw': 0, 'total_pnl_pct': 0, 'total_invested_krw': 0}
    try:
        for h in pf.get('holdings', []):
            t   = h['ticker']
            p   = stock_data.get(t) or get_price(t)
            cur = p.get('price', 0)
            avg = h.get('avg_price_usd', 0)
            sh  = h.get('shares', 0)
            wr  = stock_data.get(t, {}).get('wr') or get_weekly_wr(t)
            is_fixed = t in WATCH_TICKERS
            holdings.append({
                'ticker':            t,
                'sector':            h.get('sector', NOBL_UNIVERSE.get(t, WATCH_TICKERS.get(t, '--'))),
                'shares':            sh,
                'avg_price_usd':     avg,
                'current_price':     cur,
                'current_value_usd': round(cur * sh, 2),
                'current_value_krw': round(cur * sh * fx),
                'pnl_pct':           round((cur / avg - 1) * 100, 2) if avg > 0 else 0,
                'day_change_pct':    p.get('pct', 0),
                'wr':                wr,
                'dividends_usd':     h.get('dividends_received_usd', 0),
                'held_drop':         t in held_dropped,
                'fixed_watch':       is_fixed,
                'surplus':           get_surplus(t) if not is_fixed else 0,
                'per_budget':        per_ticker_budget if not is_fixed else 0,
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
        'updated_at':        now.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate':           fx,
        'egg':               egg,
        'budget':            budget,
        'top_tickers':       top_tickers,
        'held_dropped':      held_dropped,
        'all_active':        all_active_tickers,
        'n_tickers':         n_tickers,
        'per_ticker_budget': per_ticker_budget,
        'rs_ranking':        rs_ranking,
        'stock_data':        stock_data,
        'watch_data':        watch_data,
        'signals':           signals,
        'held_signals':      held_signals,
        'holdings':          holdings,
        'portfolio_summary': ps,
        'benchmarks':        benchmarks,
        'settings': {
            'monthly_budget':  MONTHLY_BUDGET,
            'start_date':      START_DATE,
            'months_elapsed':  months_elapsed(START_DATE),
        },
    }

    output = sanitize(output)
    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=serial)
    print("  → prices.json 저장 완료\n")

    # ══ 텔레그램 알림 ══
    print("[알림] 텔레그램 발송...")

    if deposited:
        tg(f"💰 <b>월정기 자동 입금</b>\n₩{MONTHLY_BUDGET:,} 입금 완료\n({now.strftime('%Y-%m-%d')})")

    if detect_egg_change(prev, egg):
        send_egg_change_alert(prev, egg, budget, fx)

    if wr_changes:
        send_wr_alerts(wr_changes, fx, egg['stage'], budget)

    if do_rb:
        send_rebalancing_alert(rb, egg['stage'], rs_ranking)

    send_indicator_threshold_alerts(prev, ind)

    send_friday_wr_summary(signals, held_signals, egg, budget, fx, watch_data=watch_data)

    all_signals = signals + held_signals
    if all_signals and not wr_changes and 7 <= now.hour <= 10 and now.weekday() != 4:
        sig_lines = [
            f"🔔 <b>달걀이론 기존 매수 신호 유지</b>",
            f"달걀 {egg['stage']}단계 | ₩{budget['amount']:,}",
        ]
        for s in all_signals:
            icon = "🔴" if s['allocation']['signal']=='STRONG' else "🟡" if s['allocation']['signal']=='MEDIUM' else "🟢"
            wr_str = f"{s['wr']:.1f}" if s.get('wr') else "--"
            tag = " 🔒" if s.get('held_drop') else ""
            sig_lines.append(f"{icon} <b>{s['ticker']}</b>{tag} WR {wr_str} | ₩{s['allocation']['amount']:,}")
        tg('\n'.join(sig_lines))

    if now.hour == 7 and now.minute <= 30:
        send_daily_summary(egg, budget, signals, fx, held_signals)
    elif not all_signals and not wr_changes and 7 <= now.hour <= 10:
        send_daily_summary(egg, budget, signals, fx, held_signals)

    send_indicator_reminder(now)

    print("🎉 모든 작업 완료!")


if __name__ == '__main__':
    main()
