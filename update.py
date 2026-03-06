#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
포트폴리오 자동 업데이트 스크립트
- Finnhub: 실시간 가격
- FRED: 경제지표 → 달걀 단계
- yfinance: 주봉 WR 계산
- Telegram: 신호 알림
"""
import os, json, requests, warnings
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf

warnings.filterwarnings('ignore')

FINNHUB_KEY      = os.environ.get('FINNHUB_KEY', '')
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

NOBL_UNIVERSE = {
    'KO':'필수소비재','PG':'필수소비재','PEP':'필수소비재','CL':'필수소비재',
    'KMB':'필수소비재','MKC':'필수소비재','GIS':'필수소비재','SYY':'필수소비재',
    'WMT':'필수소비재','TGT':'필수소비재','HRL':'필수소비재','CLX':'필수소비재',
    'ADM':'필수소비재','CAT':'산업재','EMR':'산업재','ITW':'산업재','DOV':'산업재',
    'GWW':'산업재','SWK':'산업재','CHRW':'산업재','EXPD':'산업재','CTAS':'산업재',
    'ROL':'산업재','FAST':'산업재','NDSN':'산업재','CB':'금융','CINF':'금융',
    'AFL':'금융','BEN':'금융','SPGI':'금융','AMP':'금융','AON':'금융',
    'NUE':'소재','PPG':'소재','SHW':'소재','LIN':'소재','ECL':'소재',
    'APD':'소재','ALB':'소재','JNJ':'헬스케어','ABT':'헬스케어','BDX':'헬스케어',
    'ABBV':'헬스케어','CAH':'헬스케어','MDT':'헬스케어','LOW':'경기소비재',
    'GPC':'경기소비재','AWK':'유틸리티','ATO':'유틸리티','NFG':'유틸리티',
    'FRT':'부동산','ESS':'부동산','O':'부동산','IBM':'기술','TXN':'기술',
    'XOM':'에너지','CVX':'에너지',
}

STAGE_ALLOCATION = {
    1: {'헬스케어':8,'필수소비재':9,'유틸리티':5,'부동산':5,'금융':3},
    2: {'헬스케어':6,'필수소비재':7,'유틸리티':4,'부동산':4,'금융':5,'경기소비재':2,'산업재':2},
    3: {'금융':8,'산업재':9,'기술':4,'경기소비재':4,'헬스케어':3,'소재':2},
    4: {'산업재':8,'소재':7,'에너지':4,'금융':5,'경기소비재':3,'기술':3},
    5: {'에너지':5,'소재':7,'산업재':7,'필수소비재':5,'금융':4,'헬스케어':2},
    6: {'필수소비재':10,'헬스케어':8,'유틸리티':6,'부동산':4,'금융':2},
}

STAGE_DESC = {
    1:"① 침체·방어", 2:"② 회복 초기", 3:"③ 상승 초입",
    4:"④ 호황기",    5:"⑤ 과열 근접", 6:"⑥ 전환·하락",
}

FRED_SERIES = ['FEDFUNDS','DGS10','DGS2','CPIAUCSL','UNRATE',
               'M2SL','BAMLH0A0HYM2','UMCSENT','VIXCLS','A191RL1Q225SBEA']

# ── 유틸 ──────────────────────────────────────────────────────

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] 토큰 없음, 스킵")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': msg,
            'parse_mode': 'HTML'
        }, timeout=10)
        print(f"[TG] 전송 완료")
    except Exception as e:
        print(f"[TG] 오류: {e}")

def fetch_fred_latest(series_id: str) -> float:
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        df = pd.read_csv(url, index_col=0, parse_dates=True, na_values='.')
        s = pd.to_numeric(df.iloc[:, 0], errors='coerce').dropna()
        return float(s.iloc[-1]) if not s.empty else None
    except:
        return None

def fetch_fred_series(series_id: str, months: int = 24) -> pd.Series:
    try:
        start = (datetime.now() - timedelta(days=months*31)).strftime('%Y-%m-%d')
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        df = pd.read_csv(url, index_col=0, parse_dates=True, na_values='.')
        s = pd.to_numeric(df.iloc[:, 0], errors='coerce')
        return s[s.index >= pd.Timestamp(start)].dropna()
    except:
        return pd.Series(dtype=float)

def get_fx_rate() -> float:
    """USD/KRW 환율"""
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=8)
        return float(r.json()['rates']['KRW'])
    except:
        try:
            tk = yf.Ticker('KRW=X')
            hist = tk.history(period='5d')
            if not hist.empty:
                return float(hist['Close'].iloc[-1])
        except:
            pass
        return 1350.0

def finnhub_price(ticker: str) -> dict:
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=8)
        d = r.json()
        if d.get('c', 0) > 0:
            return {
                'price':  round(d['c'], 4),
                'change': round(d['d'] or 0, 4),
                'pct':    round(d['dp'] or 0, 2),
                'high':   round(d['h'] or 0, 4),
                'low':    round(d['l'] or 0, 4),
            }
    except:
        pass
    return {'price': 0, 'change': 0, 'pct': 0, 'high': 0, 'low': 0}

def calc_williams_r(high, low, close, period=14):
    r = []
    for i in range(len(close)):
        if i < period - 1:
            r.append(None)
            continue
        h = max(high[i-period+1:i+1])
        l = min(low[i-period+1:i+1])
        if h != l:
            r.append(((h - close[i]) / (h - l)) * -100)
        else:
            r.append(-50)
    return r

def get_weekly_wr(ticker: str, period: int = 14) -> float:
    try:
        df = yf.download(ticker, period='60wk', interval='1wk',
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < period:
            return None
        H = df['High'].values.flatten().tolist()
        L = df['Low'].values.flatten().tolist()
        C = df['Close'].values.flatten().tolist()
        wr = calc_williams_r(H, L, C, period)
        vals = [v for v in wr if v is not None]
        return round(vals[-1], 2) if vals else None
    except:
        return None

def get_momentum_6m(ticker: str) -> float:
    try:
        df = yf.download(ticker, period='7mo', interval='1wk',
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < 26:
            return 0
        c = df['Close'].values.flatten()
        return float((c[-1] - c[0]) / c[0] * 100) if c[0] > 0 else 0
    except:
        return 0

# ── 달걀 단계 계산 ─────────────────────────────────────────────

def calc_egg_stage() -> dict:
    data = {}
    for sid in FRED_SERIES:
        s = fetch_fred_series(sid, 15)
        if not s.empty:
            data[sid] = float(s.iloc[-1])
            if sid == 'CPIAUCSL' and len(s) >= 12:
                data['CPI_YOY'] = float((s.iloc[-1] - s.iloc[-12]) / s.iloc[-12] * 100)
            if sid == 'M2SL' and len(s) >= 12:
                data['M2_YOY'] = float((s.iloc[-1] - s.iloc[-12]) / s.iloc[-12] * 100)

    fed    = data.get('FEDFUNDS', 3.0)
    dgs10  = data.get('DGS10', 4.0)
    dgs2   = data.get('DGS2', 3.5)
    spread = dgs10 - dgs2
    cpi    = data.get('CPI_YOY', 2.5)
    rrate  = dgs10 - cpi
    m2     = data.get('M2_YOY', 5.0)
    credit = data.get('BAMLH0A0HYM2', 4.0)
    vix    = data.get('VIXCLS', 20.0)
    conf   = data.get('UMCSENT', 80.0)
    unemp  = data.get('UNRATE', 4.5)
    gdp    = data.get('A191RL1Q225SBEA', 2.0)

    def interp(x, xp, fp):
        return float(np.interp(x, xp, fp))

    fed_s    = interp(fed,    [0,1,2,3,4,5,7],   [1,1.5,2,3,4,5,6])
    spread_s = interp(spread, [-1,0,0.5,1.5,2.5], [6,5,3,2,1])
    rrate_s  = interp(rrate,  [-2,0,1,2,3],       [1,2,3,4,5])
    int_s    = fed_s*0.5 + spread_s*0.3 + rrate_s*0.2

    m2_s     = interp(m2,     [0,2,5,8,12],  [5,4,3,2,1])
    credit_s = interp(credit, [2,3,4,6,8],   [1,2,3,5,6])
    liq_s    = m2_s*0.5 + credit_s*0.5

    vix_s  = interp(vix,  [10,15,20,25,35,50],  [1,1.5,3,4,5,6])
    conf_s = interp(conf, [55,65,75,85,100,110], [6,5,4,3,2,1])
    psy_s  = vix_s*0.6 + conf_s*0.4

    unemp_s = interp(unemp, [3,4,5,6,7,10], [1,2,3,4,5,6])
    gdp_s   = interp(gdp,   [-5,-2,0,1,3,5],[6,5,4,3,2,1])
    eco_s   = unemp_s*0.5 + gdp_s*0.5

    total = int_s*0.40 + liq_s*0.25 + psy_s*0.20 + eco_s*0.15

    if   total >= 4.8: stage = 1
    elif total >= 4.0: stage = 2
    elif total >= 3.2: stage = 3
    elif total >= 2.5: stage = 4
    elif total >= 1.8: stage = 5
    else:              stage = 6

    return {
        'stage': stage,
        'desc':  STAGE_DESC[stage],
        'score': round(total, 2),
        'indicators': {
            'fed_rate':  round(fed, 2),
            'spread':    round(spread, 2),
            'real_rate': round(rrate, 2),
            'vix':       round(vix, 2),
            'unemp':     round(unemp, 2),
            'cpi_yoy':   round(cpi, 2),
            'm2_yoy':    round(m2, 2),
            'credit_spread': round(credit, 2),
            'consumer_conf': round(conf, 2),
            'gdp':       round(gdp, 2),
        }
    }

def select_30(stage: int, wr_data: dict) -> list:
    alloc = STAGE_ALLOCATION.get(stage, STAGE_ALLOCATION[3])
    selected = []
    for sector, count in alloc.items():
        candidates = [t for t, s in NOBL_UNIVERSE.items() if s == sector]
        # 모멘텀 정렬 (wr_data에 momentum 있으면 사용)
        candidates_with_mom = []
        for t in candidates:
            mom = wr_data.get(t, {}).get('momentum_6m', 0)
            candidates_with_mom.append((t, mom))
        candidates_with_mom.sort(key=lambda x: x[1], reverse=True)
        selected.extend([t for t, _ in candidates_with_mom[:count]])
    if len(selected) < 30:
        extras = [t for t in NOBL_UNIVERSE if t not in selected]
        selected.extend(extras[:30 - len(selected)])
    return selected[:30]

# ── 벤치마크 히스토리 (비교 차트용) ──────────────────────────

def get_benchmark_history(tickers: list, start_date: str) -> dict:
    result = {}
    try:
        raw = yf.download(tickers, start=start_date, interval='1wk',
                          auto_adjust=True, progress=False)
        for t in tickers:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    s = raw['Close'][t].dropna()
                else:
                    s = raw['Close'].dropna()
                if s.empty: continue
                base = float(s.iloc[0])
                result[t] = {
                    'dates': [d.strftime('%Y-%m-%d') for d in s.index],
                    'pct':   [round((float(v) - base) / base * 100, 2) for v in s.values]
                }
            except:
                pass
    except:
        pass
    return result

# ── 메인 ──────────────────────────────────────────────────────

def main():
    now_kst = datetime.utcnow() + timedelta(hours=9)
    print(f"[{now_kst.strftime('%Y-%m-%d %H:%M KST')}] 업데이트 시작")

    # portfolio.json 로드
    try:
        with open('portfolio.json', encoding='utf-8') as f:
            portfolio = json.load(f)
    except:
        portfolio = {'settings': {}, 'holdings': [], 'transactions': [], 'extra_deposits': []}

    settings   = portfolio.get('settings', {})
    wr_period  = settings.get('wr_period', 14)
    wr_thr     = settings.get('wr_threshold', -60)
    start_date = settings.get('start_date', '2024-01-01')

    # 환율
    fx_rate = get_fx_rate()
    print(f"[환율] {fx_rate:.0f} KRW/USD")

    # 달걀 단계
    print("[달걀] 경제지표 계산 중...")
    egg = calc_egg_stage()
    print(f"[달걀] 단계 {egg['stage']}: {egg['desc']} (점수: {egg['score']})")

    # 전체 NOBL 종목 WR + 모멘텀 + 가격
    print("[종목] 가격/WR 수집 중...")
    wr_data = {}
    signals = []  # 매수 신호 발생 종목

    all_tickers = list(NOBL_UNIVERSE.keys()) + ['VOO', 'QQQ', 'NOBL']
    for t in all_tickers:
        price_info = finnhub_price(t)
        wr_val     = get_weekly_wr(t, wr_period)
        mom        = get_momentum_6m(t)
        wr_data[t] = {
            **price_info,
            'wr':          wr_val,
            'momentum_6m': round(mom, 2),
            'sector':      NOBL_UNIVERSE.get(t, '-'),
        }
        # 신호 체크 (NOBL 유니버스만)
        if t in NOBL_UNIVERSE and wr_val is not None:
            if   wr_val <= -80: sig_level = "🔴 강매수 (WR≤-80)"
            elif wr_val <= -70: sig_level = "🟡 중매수 (WR≤-70)"
            elif wr_val <= -60: sig_level = "🟢 약매수 (WR≤-60)"
            else:               sig_level = None
            if sig_level:
                signals.append({'ticker': t, 'wr': wr_val, 'level': sig_level,
                                 'price': price_info['price'], 'sector': NOBL_UNIVERSE[t]})

    # 현재 단계에 맞는 30종목
    current_30 = select_30(egg['stage'], wr_data)
    # 30종목 중 신호 발생한 것만 필터
    active_signals = [s for s in signals if s['ticker'] in current_30]

    # 보유 종목 현재가 반영
    holdings_enriched = []
    for h in portfolio.get('holdings', []):
        t = h['ticker']
        info = wr_data.get(t, finnhub_price(t))
        cur_price = info.get('price', 0)
        avg_price = h.get('avg_price_usd', cur_price)
        shares    = h.get('shares', 0)
        cur_val_usd = shares * cur_price
        cost_usd    = shares * avg_price
        pnl_usd     = cur_val_usd - cost_usd
        pnl_pct     = (pnl_usd / cost_usd * 100) if cost_usd > 0 else 0
        holdings_enriched.append({
            **h,
            'current_price_usd': cur_price,
            'current_value_usd': round(cur_val_usd, 2),
            'current_value_krw': round(cur_val_usd * fx_rate),
            'cost_usd':          round(cost_usd, 2),
            'pnl_usd':           round(pnl_usd, 2),
            'pnl_pct':           round(pnl_pct, 2),
            'day_change_pct':    info.get('pct', 0),
            'wr':                wr_data.get(t, {}).get('wr'),
            'sector':            NOBL_UNIVERSE.get(t, h.get('sector', '-')),
        })

    # 총 자산
    total_val_usd = sum(h['current_value_usd'] for h in holdings_enriched)
    total_val_krw = round(total_val_usd * fx_rate)
    total_cost_usd = sum(h['cost_usd'] for h in holdings_enriched)
    total_pnl_pct  = ((total_val_usd - total_cost_usd) / total_cost_usd * 100
                      if total_cost_usd > 0 else 0)

    # 벤치마크 히스토리
    print("[벤치마크] 히스토리 수집 중...")
    bm_history = get_benchmark_history(['VOO', 'QQQ', 'NOBL'], start_date)

    # 텔레그램 알림
    if active_signals:
        kst_str = now_kst.strftime('%m/%d %H:%M')
        msg = f"🥚 <b>달걀이론 매수 신호</b> [{kst_str} KST]\n"
        msg += f"현재 단계: <b>{egg['stage']}단계 {egg['desc']}</b>\n\n"
        for s in active_signals[:10]:
            msg += (f"{s['level']}\n"
                    f"  종목: <b>{s['ticker']}</b> ({s['sector']})\n"
                    f"  가격: ${s['price']:.2f}  WR: {s['wr']:.1f}\n\n")
        msg += "📌 수동 매수 후 히스토리를 기록해 주세요."
        send_telegram(msg)
        print(f"[신호] {len(active_signals)}개 발생 → 텔레그램 전송 완료")
    else:
        print("[신호] 매수 신호 없음")

    # 배당금 알림 (보유 종목 체크)
    for h in holdings_enriched:
        t = h['ticker']
        try:
            tk = yf.Ticker(t)
            cal = tk.calendar
            if cal is not None and hasattr(cal, 'get'):
                ex_date = cal.get('Ex-Dividend Date')
                if ex_date:
                    ex_dt = pd.Timestamp(ex_date)
                    if abs((ex_dt - pd.Timestamp(now_kst.date())).days) <= 3:
                        div_amt = tk.info.get('lastDividendValue', 0)
                        div_total = div_amt * h['shares']
                        msg = (f"💰 <b>배당금 지급 예정</b>\n"
                               f"종목: <b>{t}</b>\n"
                               f"Ex-Date: {ex_dt.strftime('%Y-%m-%d')}\n"
                               f"주당: ${div_amt:.4f}\n"
                               f"예상 수령: ${div_total:.2f} "
                               f"(≈ {div_total*fx_rate:,.0f}원)\n"
                               f"📌 수령 후 재투자 히스토리를 기록해 주세요.")
                        send_telegram(msg)
        except:
            pass

    # prices.json 저장
    output = {
        'updated_at':     now_kst.strftime('%Y-%m-%d %H:%M KST'),
        'fx_rate':        round(fx_rate, 2),
        'egg':            egg,
        'current_30':     current_30,
        'wr_data':        wr_data,
        'active_signals': active_signals,
        'holdings':       holdings_enriched,
        'portfolio_summary': {
            'total_value_usd':  round(total_val_usd, 2),
            'total_value_krw':  total_val_krw,
            'total_cost_usd':   round(total_cost_usd, 2),
            'total_pnl_pct':    round(total_pnl_pct, 2),
        },
        'benchmarks':     bm_history,
        'transactions':   portfolio.get('transactions', []),
        'settings':       settings,
    }

    with open('prices.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[완료] prices.json 저장 완료")
    print(f"[요약] 총자산 {total_val_krw:,}원 | 수익률 {total_pnl_pct:+.2f}%")
    print(f"[요약] 달걀 {egg['stage']}단계 | 신호 {len(active_signals)}개 | 30종목 선정")

if __name__ == '__main__':
    main()
