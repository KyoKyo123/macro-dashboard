"""
daily_macro.py — 아침 리포트용 매크로 수집
소스별 독립 함수 + 부분 실패 격리. 하나 죽어도 리포트는 나옴.

키는 전부 환경변수로 읽는다 (하드코딩 금지 — .env에 넣고 .gitignore 처리).
필요 env: FRED_API_KEY, (선택) TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
import sqlite3

# .env 자동 로드 (python-dotenv 있으면 사용, 없으면 시스템 환경변수로 폴백)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import urllib.parse
from datetime import datetime, timedelta

# ── SEC 폼 종류 한글 설명 (모르는 코드만 봐도 뭔지 알게) ──
FORM_DESC = {
    "4":            "내부자거래 신고 (임원·대주주가 자사주 매매)",
    "3":            "신규 내부자 최초 보유신고 (임원 취임 등)",
    "5":            "내부자거래 연간 정정신고",
    "SCHEDULE 13G": "대량보유 신고 5%↑ (수동적 투자자 — 단순 투자 목적)",
    "SCHEDULE 13D": "대량보유 신고 5%↑ (경영참여 의도 — 행동주의 가능, 더 강한 신호)",
    "10-K":         "연간 사업보고서 (실적·재무·리스크 풀버전)",
    "10-Q":         "분기 사업보고서 (분기 실적·재무)",
    "8-K":          "수시공시 (중대사건 발생 시 즉시 신고)",
    "424B":         "증권 발행 관련 (신주·채권 발행 서류)",
    "S-1":          "신규 상장/증권 등록 (IPO 등)",
    "SC 13D/A":     "대량보유 정정 (경영참여형)",
    "DEF 14A":      "주주총회 위임장 (안건·임원 보수 등)",
}


def _form_label(form):
    desc = FORM_DESC.get(form.upper() if isinstance(form, str) else form)
    # 숫자만 있는 폼(3·4·5)은 'Form 4'처럼 명시
    disp = f"Form {form}" if form in ("3", "4", "5") else form
    return f"{disp} ({desc})" if desc else f"{disp} (설명없음)"
FRED_SERIES = {
    "CPI(전체)":         ("CPIAUCSL", "yoy"),   # 전년比 % (물가는 이게 진짜)
    "CPI(코어)":         ("CPILFESL", "yoy"),
    "근원PCE":           ("PCEPILFE", "yoy"),
    "VIX(공포지수)":     ("VIXCLS", "raw"),
    "10년물(명목)":      ("DGS10", "raw"),
    "10년물(실질/TIPS)": ("DFII10", "raw"),
    "2년물":             ("DGS2", "raw"),
    "장단기금리차":      ("T10Y2Y", "raw"),
    "기준금리(상단)":    ("DFEDTARU", "raw"),   # 정책금리 밴드 상단 (밴드로 봄)
    "실업률":            ("UNRATE", "raw"),
    "하이일드스프레드":  ("BAMLH0A0HYM2", "raw"),
    "달러인덱스":        ("DTWEXBGS", "raw"),
}

# ── 지수: 간밤 시장 (SOX = yfinance[A안], 실패 시 kis MCP[B안]는 로컬 폴백) ──
INDEX_TICKERS = {
    "SOX(반도체)":  "^SOX",
    "나스닥100":    "^NDX",
    "나스닥종합":   "^IXIC",
}

# ── 한국: 오늘/직전 한국장 (미장 다음 순서로 배치) ──────────
KR_TICKERS = {
    "KOSPI":       "^KS11",
    "KOSDAQ":      "^KQ11",
    "원달러":      "KRW=X",
    "삼성전자":    "005930.KS",
    "SK하이닉스":  "000660.KS",
}

# ── 미국 반도체 개별종목 (RS·52주 위치용) ──────────────────
US_SEMI_TICKERS = {
    "엔비디아":  "NVDA",
    "AMD":       "AMD",
    "마이크론":  "MU",
    "브로드컴":  "AVGO",
    "TSMC":      "TSM",
}

# ── Tier2: 관심 대형주 (실적 임박시에만 캘린더 등장, 카드는 안 만듦) ──
# 시장 방향 좌우하는 거대기업. 반도체 대장주는 Tier1(US_SEMI)이라 제외.
WATCHLIST_TICKERS = {
    "애플": "AAPL", "마이크로소프트": "MSFT", "구글": "GOOGL", "아마존": "AMZN",
    "메타": "META", "테슬라": "TSLA", "넷플릭스": "NFLX", "팔란티어": "PLTR",
    "오라클": "ORCL", "퀄컴": "QCOM", "ASML": "ASML", "ARM": "ARM",
    "JP모건": "JPM", "일라이릴리": "LLY",
}
WATCHLIST_IMMINENT_DAYS = 14  # 이 안으로 실적 다가오면 캘린더에 표시

# 이벤트 카테고리 → 클릭 시 상세 설명 (뭐고 왜 중요한지)
EVENT_INFO = {
    "FOMC": {
        "정의": "미국 연방공개시장위원회 회의. 여기서 기준금리를 결정한다. 미국·글로벌 증시 최대 이벤트 중 하나.",
        "해석": "금리를 올리면 증시 부담, 내리면 유동성 공급으로 위험자산 우호. 결정 자체보다 '점도표(향후 금리 전망)'와 의장 기자회견 톤이 시장을 더 흔들 때가 많다.",
        "예시": "결정은 회의 2일째 오후 2시(ET) 발표, 30분 뒤 기자회견. 3·6·9·12월 회의엔 경제전망(SEP)도 나와 더 중요.",
    },
    "지표": {
        "정의": "미국 주요 경제지표 발표일(CPI·고용·PCE 등). 물가·고용 상태를 보여줘 연준 정책 방향을 좌우한다.",
        "해석": "예상 대비 결과에 따라 시장이 크게 출렁인다. CPI가 예상보다 높으면 금리 우려로 급락, 낮으면 급등하는 식.",
        "예시": "발표 직후 몇 분 안에 지수·환율이 급변동. 발표일 전엔 관망세가 흔하다.",
    },
    "금통위": {
        "정의": "한국은행 금융통화위원회 통화정책방향 결정회의. 한국 기준금리를 정한다.",
        "해석": "한국 금리·환율·부동산에 직접 영향. 미국(FOMC)과의 금리차가 원달러 환율과 외국인 수급을 좌우한다.",
        "예시": "현재 한국 기준금리 2.50%. 결정 후 총재 기자회견에서 향후 방향 힌트를 준다.",
    },
    "실적": {
        "정의": "기업의 분기 실적 발표. 반도체 대장주 실적은 섹터 전체의 이벤트.",
        "해석": "매출·EPS가 예상 대비 어떤지, 그리고 다음 분기 가이던스가 핵심. 엔비디아 실적은 반도체·AI 섹터 전체를 흔든다.",
        "예시": "실적 자체보다 '가이던스(전망)'에 주가가 더 크게 반응하는 경우가 많다.",
    },
    "실적대형": {
        "정의": "시장 방향을 좌우하는 거대기업(빅테크 등)의 분기 실적. 직접 매매 안 해도 시장 전체에 영향.",
        "해석": "구글·애플 등 대형주 실적이 나쁘면 나스닥 전체가 흔들려 반도체·성장주도 동반 하락. 그래서 매매 대상이 아니어도 일정은 알아야 한다.",
        "예시": "빅테크 실적 시즌엔 개별 종목보다 지수 전체가 크게 움직인다.",
    },
    "실적KR": {
        "정의": "삼성전자·SK하이닉스 등 한국 대표기업 실적(잠정실적 포함). 날짜는 대략이며 정확일은 직전 공지된다.",
        "해석": "한국 반도체 실적은 코스피 전체를 좌우. 미국 마이크론 실적이 선행지표가 되기도 한다.",
        "예시": "삼성은 보통 분기 다음달 초 잠정실적(매출·영업이익만)을 먼저 발표한다.",
    },
}

# 상대강도(RS) 벤치마크: 종목이 벤치보다 세면 대장주 후보
RS_BENCH = {
    "엔비디아": "SOX(반도체)", "AMD": "SOX(반도체)", "마이크론": "SOX(반도체)",
    "브로드컴": "SOX(반도체)", "TSMC": "SOX(반도체)",
    "삼성전자": "KOSPI", "SK하이닉스": "KOSPI",
}


def _get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── 소스 1: FRED (최근값 + 발표일) ─────────────────────────
def fetch_fred(series_map, window=60):
    """최근 관측치 + 전대비/추세/시계열. mode='yoy'면 12개월 전 대비 %로 변환."""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return {"error": "FRED_API_KEY 없음"}
    out = {}
    for label, spec in series_map.items():
        sid, mode = spec if isinstance(spec, tuple) else (spec, "raw")
        try:
            limit = 400 if mode == "yoy" else window  # yoy는 13개월+ 필요
            q = urllib.parse.urlencode({
                "series_id": sid, "api_key": key, "file_type": "json",
                "sort_order": "desc", "limit": limit,
            })
            data = _get_json(f"https://api.stlouisfed.org/fred/series/observations?{q}")
            obs = [o for o in data["observations"] if o["value"] != "."]
            if not obs:
                out[label] = {"error": "데이터 없음"}
                continue

            if mode == "yoy":
                by_date = {o["date"]: float(o["value"]) for o in obs}
                yoy = []
                for o in reversed(obs):  # 오래된→최신
                    d = datetime.strptime(o["date"], "%Y-%m-%d")
                    prior = by_date.get(f"{d.year-1}-{d.month:02d}-{d.day:02d}")
                    if prior is None:  # 정확히 1년전 없으면 같은 달 근사
                        cand = [v for k, v in by_date.items() if k.startswith(f"{d.year-1}-{d.month:02d}")]
                        prior = cand[0] if cand else None
                    if prior:
                        yoy.append((o["date"], (float(o["value"]) / prior - 1) * 100))
                if not yoy:
                    out[label] = {"error": "YoY 계산 불가"}
                    continue
                dates = [d for d, _ in yoy][-window:]
                vals = [v for _, v in yoy][-window:]
                latest = vals[-1]
                prev = vals[-2] if len(vals) > 1 else None
                out[label] = {
                    "value": f"{latest:.2f}", "unit": "% YoY", "as_of": dates[-1],
                    "delta_prev": (latest - prev) if prev is not None else None,
                    "delta_trend": latest - vals[0], "trend_n": len(vals),
                    "series": list(zip(dates, vals)),
                }
            else:
                latest = float(obs[0]["value"])
                prev = float(obs[1]["value"]) if len(obs) > 1 else None
                oldest = float(obs[-1]["value"])
                out[label] = {
                    "value": obs[0]["value"], "as_of": obs[0]["date"],
                    "delta_prev": (latest - prev) if prev is not None else None,
                    "delta_trend": latest - oldest,
                    "trend_n": len(obs),
                    "series": [(o["date"], float(o["value"])) for o in reversed(obs)],
                }
        except Exception as e:
            out[label] = {"error": str(e)}
    return out


# ── 소스 1.5: 간밤 지수 등락 (yfinance = A안) ──────────────
def fetch_indices(ticker_map, window=60):
    """등락률 + 52주 위치 + 저장용 시계열. yfinance 실패 시 항목별 격리."""
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance 미설치 (python -m pip install yfinance)"}
    out = {}
    for label, tk in ticker_map.items():
        try:
            h = yf.Ticker(tk).history(period="1y")["Close"].dropna()  # 1년치(52주용)
            if len(h) < 2:
                out[label] = {"error": "데이터 부족"}
                continue
            latest, prev = float(h.iloc[-1]), float(h.iloc[-2])
            hi52, lo52 = float(h.max()), float(h.min())
            pos52 = (latest - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0
            tail = h.iloc[-(window):]  # 스파크/저장은 최근 window개만
            oldest = float(tail.iloc[0])
            out[label] = {
                "value": f"{latest:.2f}",
                "as_of": h.index[-1].strftime("%Y-%m-%d"),
                "pct_prev": (latest / prev - 1) * 100,
                "pct_trend": (latest / oldest - 1) * 100,
                "trend_n": len(tail),
                "pos52": pos52, "hi52": hi52, "lo52": lo52,
                "series": [(d.strftime("%Y-%m-%d"), float(v)) for d, v in tail.items()],
                "full_series": [float(v) for v in h.values],  # RS 계산용 전체
            }
        except Exception as e:
            out[label] = {"error": type(e).__name__}
    return out


# ── 소스 2: 크립토 공포탐욕 (키 불필요, 검증 완료) ──────────
def fetch_crypto_fng(window=60):
    data = _get_json(f"https://api.alternative.me/fng/?limit={window}")
    obs = data["data"]  # 최신순으로 옴
    latest = int(obs[0]["value"])
    prev = int(obs[1]["value"]) if len(obs) > 1 else None
    oldest = int(obs[-1]["value"])
    as_of = datetime.fromtimestamp(int(obs[0]["timestamp"])).strftime("%Y-%m-%d")
    series = [(datetime.fromtimestamp(int(o["timestamp"])).strftime("%Y-%m-%d"), int(o["value"]))
              for o in reversed(obs)]
    return {
        "value": latest, "class": obs[0]["value_classification"], "as_of": as_of,
        "delta_prev": (latest - prev) if prev is not None else None,
        "delta_trend": latest - oldest,
        "trend_n": len(obs),
        "series": series,
    }


# ── 소스 3: SEC EDGAR 최신 공시 (User-Agent 필수) ──────────
_UA = {"User-Agent": "gyobeom-research bee@example.com"}  # SEC 규칙: 식별 가능한 UA
_CIK_CACHE = {}


def _ticker_to_cik(ticker):
    if not _CIK_CACHE:
        data = _get_json("https://www.sec.gov/files/company_tickers.json", headers=_UA)
        for row in data.values():
            _CIK_CACHE[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return _CIK_CACHE.get(ticker.upper())


def _edgar_link(cik, accession):
    """SEC 원문 filing index 페이지 링크."""
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc_nodash}/{accession}-index.htm"


def fetch_edgar(tickers, recent_days=14):
    out = {}
    for t in tickers:
        try:
            cik = _ticker_to_cik(t)
            if not cik:
                out[t] = {"error": "CIK 못 찾음"}
                continue
            data = _get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_UA)
            recent = data["filings"]["recent"]
            form = recent["form"][0]
            accession = recent["accessionNumber"][0]
            primary_doc = recent["primaryDocument"][0]
            entry = {"form": form, "as_of": recent["filingDate"][0],
                     "link": _edgar_link(cik, accession)}
            if form == "4":
                entry["detail"] = fetch_form4_detail(cik, accession, primary_doc)
            # 최근 8-K(중대사건) 최근 3건 + 각 원문 링크
            eightks = []
            for i, f in enumerate(recent["form"]):
                if f == "8-K":
                    items = recent.get("items", [""] * len(recent["form"]))[i]
                    acc_i = recent["accessionNumber"][i]
                    desc, hint = _eightk_items(items)
                    eightks.append({
                        "date": recent["filingDate"][i],
                        "items": items,
                        "item_desc": desc,
                        "hint": hint,
                        "link": _edgar_link(cik, acc_i),
                    })
                if len(eightks) >= 3:
                    break
            if eightks:
                entry["eightk"] = eightks
            out[t] = entry
        except Exception as e:
            out[t] = {"error": str(e)}
    return out


# ── 8-K 항목코드 → 사건명 (SEC 8-K 전체 항목) ──
EIGHTK_ITEMS = {
    "1.01": "중대계약 체결", "1.02": "중대계약 종료", "1.03": "파산·법정관리",
    "2.01": "자산 인수/매각 완료", "2.02": "실적 발표", "2.03": "채무 발생",
    "2.04": "채무 조기상환 촉발", "2.05": "구조조정 결정", "2.06": "자산 손상차손",
    "3.01": "상장폐지 경고", "3.02": "미등록 주식 매각", "3.03": "주주 권리 변경",
    "4.01": "회계법인 교체", "4.02": "과거 재무제표 신뢰불가(재작성)",
    "5.01": "경영권 변동", "5.02": "임원 선임/사임", "5.03": "정관 변경",
    "5.07": "주주총회 표결결과", "7.01": "공정공시(Reg FD)",
    "8.01": "기타 중요사건", "9.01": "재무제표·첨부",
}
# 트레이딩 관점 자세한 해석 (원문 안 봐도 감 잡게)
EIGHTK_HINT = {
    "1.01": "대형 수주·공급계약·파트너십 가능성. 계약 규모가 시총 대비 크면 강한 호재.",
    "1.02": "주요 계약이 끝남. 매출처 상실이면 악재, 단순 만료면 중립.",
    "1.03": "파산·법정관리 신청. 심각한 악재, 주가 폭락 위험.",
    "2.01": "인수합병(M&A) 완료. 사업구조가 바뀌므로 성장·시너지 vs 부채 부담을 따져야 함.",
    "2.02": "분기 실적 공개. 매출·EPS가 예상 상회면 급등, 하회면 급락. 다음 분기 가이던스가 실적보다 중요할 때 많음.",
    "2.03": "새 빚(회사채·대출) 발생. 자금조달인지 재무악화인지 규모로 판단.",
    "2.05": "구조조정·감원 결정. 단기 비용이지만 체질개선 기대로 주가엔 중립~긍정인 경우도.",
    "2.06": "자산 가치 손상 인식(손상차손). 대규모면 실적 쇼크 악재.",
    "3.01": "상장폐지 요건 미달 경고. 심각한 악재.",
    "4.01": "회계법인 교체. 이유가 회계 이견이면 경계 신호.",
    "4.02": "과거 실적을 못 믿겠다며 재작성. 회계 신뢰 붕괴, 강한 악재.",
    "5.01": "지배주주·경영권이 바뀜. 인수·행동주의 가능성.",
    "5.02": "임원·이사 변동. CEO·CFO 교체는 중대 — 갑작스러우면 경계.",
}


def _eightk_items(items_str):
    """8-K 항목코드 → (표시용 사건명, 자세한 해석). 부속항목(9.01,5.07)은 노이즈라 뒤로."""
    if not items_str:
        return "내용 미상", ""
    codes = [c for c in items_str.replace(" ", "").split(",")]
    # 부속·형식 항목은 뒤로 빼서 주요 사건이 앞에 오게
    minor = {"9.01", "5.07", "7.01"}
    main_codes = [c for c in codes if c not in minor]
    show = main_codes if main_codes else codes  # 주요 없으면 원래대로
    descs = [EIGHTK_ITEMS.get(c, c) for c in show]
    hints = [EIGHTK_HINT[c] for c in show if c in EIGHTK_HINT]
    return ", ".join(descs), " / ".join(hints)


# ── 거래코드 설명 (P만 진짜 강세 시그널, 나머진 노이즈일 확률 높음) ──
TX_CODE_DESC = {
    "P": "시장매수 (진짜 강세 시그널)",
    "S": "매도",
    "M": "옵션행사 (매수/매도 아님, 권리행사)",
    "A": "부여/취득 (보상, 시장매수 아님)",
    "F": "세금원천징수용 처분 (본인 의지 아님)",
    "G": "증여",
    "J": "기타 취득/처분 (이전·분배 등 비정형)",
    "C": "전환 (전환사채 등 → 주식)",
    "D": "발행사에 처분/반납",
    "W": "상속/유증 관련",
    "X": "옵션 행사 (아웃오브머니 등)",
    "I": "비공개 거래",
}


def fetch_form4_detail(cik, accession, primary_document):
    """Form 4 raw XML 파싱 — 누가·언제·몇 주·얼마에 거래했는지."""
    accession_nodash = accession.replace("-", "")
    cik_nolead = cik.lstrip("0")
    raw_filename = primary_document.split("/")[-1]  # xsl 렌더링 폴더 접두어 제거
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_nolead}/{accession_nodash}/{raw_filename}"
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    name = root.findtext(".//reportingOwnerId/rptOwnerName") or "?"
    title = root.findtext(".//reportingOwnerRelationship/officerTitle") or ""
    txs = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = tx.findtext(".//transactionCode") or "?"
        shares = tx.findtext(".//transactionShares/value")
        price = tx.findtext(".//transactionPricePerShare/value")
        date = tx.findtext(".//transactionDate/value")
        owned = tx.findtext(".//sharesOwnedFollowingTransaction/value")
        # 원본 숫자 보존해서 금액·비중 계산
        try:
            sh_f, pr_f = float(shares), float(price)
            amount = sh_f * pr_f
            is_cash = pr_f > 0                          # 주당 0 = 비매매(이전/분배)
            owned_f = float(owned) if owned else None
            ratio = None
            if owned_f is not None:
                base = owned_f + sh_f if code in ("S", "F", "G") else owned_f
                ratio = (sh_f / base * 100) if base else None
        except (ValueError, TypeError):
            amount, owned_f, ratio, is_cash = None, None, None, False
        try:
            shares = f"{int(float(shares)):,}"
        except (ValueError, TypeError):
            pass
        try:
            price = f"{float(price):,.2f}"
        except (ValueError, TypeError):
            pass
        txs.append({
            "code": code,
            "code_desc": TX_CODE_DESC.get(code, "기타"),
            "shares": shares, "price": price, "date": date,
            "amount": amount, "owned": owned_f, "ratio": ratio, "is_cash": is_cash,
        })
    return {"name": name, "title": title, "transactions": txs}


# ── 지표 설명 사전 (고정 텍스트 = 환각 0) ──────────────────
METRIC_INFO = {
    "VIX(공포지수)": {
        "정의": "미국 S&P500 옵션 가격으로 계산하는 '앞으로 30일간 예상 변동성'. 흔히 공포지수라 부른다. 투자자가 불안할수록 하락 대비 보험(풋옵션)을 많이 사서 값이 올라간다.",
        "해석": "낮으면 시장이 안정·낙관, 높으면 불안·패닉. 주가가 급락할 때 같이 치솟기 때문에 VIX 급등은 조정·폭락 신호로 본다. 반대로 지나치게 낮으면 방심 구간.",
        "예시": "15 이하=매우 평온, 15~20=보통, 20~30=경계, 30 이상=위험. 40 넘으면 위기(2020 코로나 80 돌파, 2008 금융위기 80대).",
    },
    "SOX(반도체)": {
        "정의": "필라델피아 반도체지수. 엔비디아·AMD·TSMC·마이크론 등 미국 상장 주요 반도체 기업을 묶은 대표 지수. 반도체 섹터 전체의 체온계.",
        "해석": "간밤 SOX 방향이 다음날 한국 반도체(삼성·하이닉스)에 가장 직접적으로 영향을 준다. 개별 종목이 SOX보다 더 세게 오르면 그 종목이 '상대강도 우위'(대장주 후보).",
        "예시": "SOX 급등 → 다음날 한국 반도체 갭업 경향. 추세추종은 SOX가 신고가 흐름일 때 진입 우호적으로 본다.",
    },
    "나스닥100": {
        "정의": "미국 나스닥 상장 기업 중 시총 상위 100개(금융 제외). 애플·엔비디아·마이크로소프트 등 빅테크·성장주 중심 지수.",
        "해석": "성장주·기술주 전반의 분위기. 간밤 나스닥이 강하면 다음날 한국 성장주도 우호적. 금리에 민감하다.",
        "예시": "나스닥100이 오르는데 SOX가 더 세면 반도체가 시장을 주도하는 국면.",
    },
    "나스닥종합": {
        "정의": "나스닥에 상장된 모든 종목을 포함한 종합지수(3천여 개). 나스닥100보다 폭넓게 시장 전반을 반영.",
        "해석": "나스닥100과 방향은 대체로 같지만, 종합이 더 약하면 대형주만 오르고 중소형주는 부진한 '차별화 장세'.",
        "예시": "종합 < 100 이면 소수 대형주 쏠림. 종합이 같이 강하면 시장 전반 건강.",
    },
    "10년물(명목)": {
        "정의": "미국 10년 만기 국채의 명목 금리(연이자). 전 세계 장기 자금조달 비용의 기준선. '안전자산 대비 위험자산의 매력'을 재는 잣대.",
        "해석": "금리가 오르면 미래 이익 가치가 깎여서 성장주·반도체 같은 '롱듀레이션' 주식에 밸류에이션 압력. 급등이 특히 부담이다.",
        "예시": "4% 넘으면 성장주 경계, 5% 근접 시 증시 전반 부담. 반대로 금리 하락은 성장주에 순풍.",
    },
    "10년물(실질/TIPS)": {
        "정의": "물가연동국채(TIPS)로 계산한 실질 금리. 명목금리에서 시장이 기대하는 인플레이션을 뺀, '진짜' 금리 부담.",
        "해석": "반도체 같은 성장주엔 명목금리보다 이 실질금리가 더 정확한 압력 지표. 실질금리가 오르면 성장주 역풍이 강하다.",
        "예시": "2% 넘으면 역사적으로 높은 편(성장주 부담 큼). 마이너스면 위험자산에 매우 우호적.",
    },
    "2년물": {
        "정의": "미국 2년 만기 국채 금리. 단기물이라 '시장이 예상하는 연준의 향후 기준금리 경로'를 가장 잘 반영한다.",
        "해석": "2년물이 오르면 시장이 금리 인상(또는 고금리 지속)을 예상한다는 뜻. 10년물과의 차이(장단기금리차)를 보는 재료.",
        "예시": "2년물이 10년물보다 높으면 '금리 역전' = 침체 경고 신호.",
    },
    "장단기금리차": {
        "정의": "10년물 금리에서 2년물 금리를 뺀 값. 마이너스가 되면 단기금리가 장기보다 높은 '금리 역전' 상태.",
        "해석": "역전(마이너스)은 역사적으로 가장 유명한 경기침체 선행신호. 역전이 풀리며 다시 플러스로 전환하는 시점도 주의 깊게 본다.",
        "예시": "0 아래로 내려가면 역전. 과거 역전 후 6~18개월 내 침체가 온 사례가 많았다.",
    },
    "기준금리(상단)": {
        "정의": "미국 연방준비제도(연준)가 정하는 정책금리(연방기금금리). 모든 시장금리의 출발점. 여기 표시값은 월평균 유효금리라 실제 정책 밴드와 약간 다를 수 있다.",
        "해석": "올리면 돈줄을 죄어 물가를 잡지만 증시엔 부담, 내리면 유동성 공급으로 위험자산에 우호적. 인하 기대가 커지면 증시가 먼저 반응한다.",
        "예시": "연준은 물가 2%를 목표로 금리를 조절. '언제 내리나'가 시장 최대 관심사인 경우가 많다.",
    },
    "하이일드스프레드": {
        "정의": "신용등급이 낮은(투기등급) 회사채 금리와 안전한 국채 금리의 차이. 시장이 '부실 위험'을 얼마나 크게 보는지 재는 신용 온도계.",
        "해석": "벌어지면 시장이 위험을 크게 봄(리스크오프, 주식에 부정적). 좁으면 위험 선호(리스크온). 급등은 신용경색·위기 신호.",
        "예시": "3% 아래=안정, 5% 넘으면 경계, 급등 시 위기(2020·2008 때 폭등).",
    },
    "달러인덱스": {
        "정의": "유로·엔 등 주요 6개 통화 대비 달러의 상대 가치를 지수화한 것. 달러가 세면 올라간다.",
        "해석": "강달러는 한국·신흥국 증시에서 외국인 자금을 빠져나가게 하는 압력(원화 약세 동반). 약달러는 반대로 외국인 유입에 우호적.",
        "예시": "100이 기준선. 강달러 국면엔 원달러 환율 상승 → 한국 수급 부담.",
    },
    "CPI(전체)": {
        "정의": "소비자물가지수. 가정이 사는 물건·서비스 가격을 묶어 만든 인플레이션 대표 지표. 여기 값은 지수 원값이라 절대 크기보다 '전년 대비 상승률'이 중요하다.",
        "해석": "예상보다 높게 나오면 물가가 안 잡힌다는 뜻 → 금리 인상/고금리 지속 우려 → 증시 부담. 발표일마다 시장이 크게 출렁인다.",
        "예시": "연준 목표는 2%대. 발표가 예상을 웃돌면 증시 급락, 밑돌면 급등하는 경우가 잦다.",
    },
    "CPI(코어)": {
        "정의": "CPI에서 변동이 심한 식품·에너지를 뺀 '근원' 소비자물가. 물가의 추세적 흐름을 더 잘 보여준다.",
        "해석": "연준이 실제로 더 중시하는 지표. 코어가 안 떨어지면 금리 인하가 늦어진다는 신호로 해석된다.",
        "예시": "전체 CPI는 유가 때문에 출렁여도, 코어가 끈적하게 높으면 시장은 더 경계한다.",
    },
    "근원PCE": {
        "정의": "개인소비지출(PCE) 물가에서 식품·에너지를 뺀 지표. 연준이 물가 목표(2%)를 판단할 때 '공식적으로 가장 중시하는' 물가 지표.",
        "해석": "CPI보다 연준 의사결정에 직접 연결된다. 근원 PCE가 2%로 수렴하는지가 금리 방향의 핵심.",
        "예시": "연준 회의 전 이 지표가 나오면 시장이 특히 민감하게 반응한다.",
    },
    "실업률": {
        "정의": "일할 의사가 있는 사람 중 일자리가 없는 사람의 비율. 경기와 노동시장의 건강을 보는 핵심 지표.",
        "해석": "너무 낮으면 임금·물가 압력(금리 부담), 급등하면 경기 둔화·침체 우려. 연준은 '물가와 고용' 둘 다 본다.",
        "예시": "4%대는 완전고용에 가까운 수준. 갑자기 뛰기 시작하면 침체 신호로 경계.",
    },
    "크립토공포탐욕": {
        "정의": "비트코인 시장의 투자심리를 0~100으로 나타낸 지수(0=극단공포, 100=극단탐욕). 변동성·거래량·SNS 등을 종합해 산출.",
        "해석": "코인 심리지만 위험자산 전반의 분위기 대용으로도 본다. 바닥권의 극단공포는 오히려 반등 직전인 '역발상 매수' 구간으로 해석되기도 한다.",
        "예시": "25 이하=극단공포, 75 이상=극단탐욕. 모두가 공포에 질렸을 때가 바닥인 경우가 있다.",
    },
    "KOSPI": {
        "정의": "한국 유가증권시장 대표 지수. 삼성전자·SK하이닉스 등 대형주 중심. 한국 증시의 얼굴.",
        "해석": "간밤 미국(특히 나스닥·SOX) 흐름을 다음날 아침 반영하는 경향. 외국인 순매수/매도와 환율에 크게 좌우된다.",
        "예시": "미국 반도체 강세 → 다음날 코스피 갭업 경향. 강달러(원화 약세) 땐 외국인 이탈로 부진하기 쉽다.",
    },
    "KOSDAQ": {
        "정의": "한국 코스닥시장 지수. 중소형·성장·바이오·2차전지 등 성장주 비중이 큰 시장.",
        "해석": "코스피보다 변동성이 크고 개인 수급·테마에 민감. 위험선호가 강할 때 코스피보다 더 오르고, 리스크오프엔 더 빠진다.",
        "예시": "코스닥이 코스피보다 세면 시장이 위험선호(성장주 선호) 국면.",
    },
    "원달러": {
        "정의": "1달러를 사는 데 필요한 원화 금액(원/달러 환율). 오르면 원화 약세(달러 강세), 내리면 원화 강세.",
        "해석": "환율이 오르면(원화 약세) 외국인이 한국 주식을 팔고 나가는 압력. 수출기업엔 유리하지만 증시 수급엔 대체로 부담.",
        "예시": "1,400원 위는 원화가 상당히 약한 구간. 급등하면 외국인 순매도 동반이 잦다.",
    },
    "삼성전자": {
        "정의": "한국 시가총액 1위 기업. 메모리반도체(D램·낸드) 세계 1위이자 스마트폰·디스플레이도 하는 종합 전자기업.",
        "해석": "코스피 대장주라 삼성 방향이 지수 전체를 좌우한다. 반도체 업황과 HBM 등 첨단 메모리 경쟁력이 주가의 핵심 동력. 외국인 순매매의 최대 타깃.",
        "예시": "간밤 미국 반도체·마이크론 실적이 삼성 주가에 선반영되곤 한다. 삼성 강세일 때 코스피도 강한 경우가 많다.",
    },
    "SK하이닉스": {
        "정의": "한국 시총 상위, 메모리반도체(D램·HBM) 전문 기업. 특히 AI용 고대역폭메모리(HBM) 분야에서 존재감이 큰 대장주.",
        "해석": "AI·반도체 테마의 한국 대표주. 엔비디아·AI 수요 뉴스에 민감하게 반응한다. 간밤 미국 AI·반도체 흐름을 다음날 강하게 반영.",
        "예시": "엔비디아·SOX 급등 다음날 하이닉스가 앞장서 오르는 경우가 잦다. 미국서 거래되는 하이닉스 대용물(ADR)이 다음날 시가 힌트가 되기도.",
    },
}
DEFAULT_INFO = {"정의": "-", "해석": "차트로 추이 확인.", "예시": "-"}

# 매크로 지표 카테고리 → 색 (같은 그룹끼리 눈에 띄게)
METRIC_CATEGORY = {
    "CPI(전체)": ("물가", "#D97706"), "CPI(코어)": ("물가", "#D97706"), "근원PCE": ("물가", "#D97706"),
    "10년물(명목)": ("금리", "#2563EB"), "10년물(실질/TIPS)": ("금리", "#2563EB"),
    "2년물": ("금리", "#2563EB"), "장단기금리차": ("금리", "#2563EB"), "기준금리(상단)": ("금리", "#2563EB"),
    "VIX(공포지수)": ("변동성", "#DC2626"),
    "하이일드스프레드": ("신용", "#7C3AED"),
    "달러인덱스": ("통화", "#059669"),
    "실업률": ("고용", "#0891B2"),
}

# 지표 → 그걸 움직이는 이벤트 (카드에 D-day 표시 + 임박시 배경 강조)
METRIC_EVENT = {
    "기준금리(상단)": "FOMC 금리결정", "10년물(명목)": "FOMC 금리결정",
    "10년물(실질/TIPS)": "FOMC 금리결정", "2년물": "FOMC 금리결정",
    "장단기금리차": "FOMC 금리결정",
    "CPI(전체)": "미국 CPI (소비자물가)", "CPI(코어)": "미국 CPI (소비자물가)",
    "근원PCE": "미국 PCE (개인소비지출)",
    "실업률": "미국 고용보고서",
}


def _urgency_style(dday):
    """이벤트 D-day → (배경style, 뱃지html). 가까울수록 짙게."""
    if dday is None:
        return "", ""
    if dday == 0:
        bg, bc, txt = "#F9D5D5", "#C0392B", "🔴 오늘"
    elif dday <= 3:
        bg, bc, txt = "#FBE0C4", "#C05621", f"D-{dday}"
    elif dday <= 7:
        bg, bc, txt = "#FEF0C7", "#B7791F", f"D-{dday}"
    elif dday <= 14:
        bg, bc, txt = "#FCFBEA", "#8B8B3A", f"D-{dday}"
    else:
        return "", f'<span style="font-size:10px;color:#aaa;">D-{dday}</span>'
    badge = f'<span style="font-size:10px;color:{bc};font-weight:700;">{txt}</span>'
    return f"background:{bg};color:#1a1a1a;", badge

# 미국 반도체 개별 기업 설명 추가
METRIC_INFO.update({
    "엔비디아": {
        "정의": "AI 반도체(GPU) 세계 1위 기업. 데이터센터용 AI 가속기 시장을 사실상 독점하다시피 함. 티커 NVDA.",
        "해석": "AI 반도체 테마의 대장주 중 대장. 이 회사 실적·전망이 SOX와 반도체 섹터 전체를 좌우한다. 한국 하이닉스(HBM 납품)와도 직결.",
        "예시": "엔비디아 실적 발표는 반도체 섹터 전체의 이벤트. 강하면 다음날 한국 하이닉스·삼성도 동반 강세인 경우 많음.",
    },
    "AMD": {
        "정의": "CPU·GPU를 만드는 미국 반도체 기업. 엔비디아의 AI 가속기 시장 도전자이자 인텔의 CPU 경쟁자. 티커 AMD.",
        "해석": "AI 가속기 2인자 포지션. 엔비디아 대비 상대적 위치와 시장점유율 뉴스에 민감. 반도체 섹터 강세장에서 탄력 큼.",
        "예시": "엔비디아가 AI 대장이라면 AMD는 '2등의 추격' 스토리. 점유율 확대 뉴스에 급등하곤 함.",
    },
    "마이크론": {
        "정의": "미국 메모리반도체(D램·낸드) 기업. 삼성·SK하이닉스와 함께 메모리 3강. 티커 MU.",
        "해석": "메모리 업황의 미국 대표주. 마이크론 실적·가이던스가 한국 삼성·하이닉스 주가의 선행지표가 되기도 한다(같은 메모리 사이클).",
        "예시": "마이크론 실적이 좋으면 '메모리 업황 개선' 신호 → 다음날 한국 삼성·하이닉스 동반 강세 경향.",
    },
    "브로드컴": {
        "정의": "통신·네트워크 반도체와 인프라 소프트웨어 기업. AI 데이터센터용 맞춤형 칩(ASIC)·네트워킹으로 AI 수혜. 티커 AVGO.",
        "해석": "엔비디아와 다른 각도의 AI 수혜주(맞춤형 칩·네트워킹). AI 데이터센터 투자 확대의 수혜를 넓게 받음.",
        "예시": "빅테크의 자체 AI칩 수요가 늘면 브로드컴이 수혜. 엔비디아 대안 테마로 부각되곤 함.",
    },
    "TSMC": {
        "정의": "대만의 파운드리(반도체 위탁생산) 세계 1위. 엔비디아·애플·AMD 등 대부분의 첨단 칩을 실제로 만들어주는 회사. 티커 TSM.",
        "해석": "반도체 밸류체인의 핵심. TSMC 실적·가동률은 반도체 수요 전체의 바로미터. 지정학(대만) 리스크도 봐야 함.",
        "예시": "TSMC 매출이 늘면 '반도체 주문이 실제로 많다'는 증거 → 섹터 전반 긍정 신호.",
    },
    "KOSDAQ": {
        "정의": "한국 코스닥시장 지수. 중소형·성장·바이오·2차전지 등 성장주 비중이 큰 시장.",
        "해석": "코스피보다 변동성 크고 개인 수급·테마에 민감. 위험선호 강할 때 더 오르고, 리스크오프엔 더 빠진다.",
        "예시": "코스닥이 코스피보다 세면 시장이 위험선호(성장주 선호) 국면.",
    },
})

# RS·52주 위치 개념 설명 (지표 카드가 아니라 개념 도움말)
CONCEPT_INFO = {
    "RS(상대강도)": {
        "정의": "상대강도(Relative Strength). 개별 종목의 수익률에서 벤치마크(SOX·KOSPI) 수익률을 뺀 값. 여기선 최근 20일 기준. 양수면 시장보다 세게 올랐다는 뜻.",
        "해석": "추세추종의 핵심 잣대. '오늘 얼마 올랐나'가 아니라 '시장보다 꾸준히 센가'를 봄. RS 높은 종목이 대장주(주도주) 후보. 오늘 급등해도 RS가 마이너스면 아직 시장을 못 따라잡은 것.",
        "예시": "마이크론이 오늘 +12%여도 20일 RS가 -4%p면, 단기 반등일 뿐 아직 대장 아님. 반대로 매일 조금씩이라도 SOX보다 세면 RS 플러스 = 진짜 주도주.",
    },
    "52주 위치": {
        "정의": "현재가가 최근 52주(1년) 최고가~최저가 범위에서 어느 지점인지 %로 표시. 0%=1년 최저, 100%=1년 최고(신고가).",
        "해석": "90% 이상이면 신고가 근처(강세, 추세추종 진입 우호), 10% 이하면 바닥권(약세 또는 낙폭과대). 추세추종은 보통 고가권에서 신고가 돌파를 노림.",
        "예시": "52주 92%면 1년 고점 근처라 강한 상승추세. 52주 5%면 1년 저점 근처라 하락추세거나 바닥 다지는 중.",
    },
}






DB_PATH = "macro_history.db"


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS history(
        date TEXT, metric TEXT, value REAL,
        PRIMARY KEY(date, metric))""")  # PK=중복 자동 방지(upsert)
    return con


def save_history(data):
    """수집한 각 지표의 시계열을 DB에 upsert. series 있는 것만."""
    con = _db()
    for section in ("indices", "us_semi", "korea", "fred"):
        block = data.get(section, {})
        if "error" in block:
            continue
        for label, v in block.items():
            for date, value in v.get("series", []):
                con.execute("INSERT OR REPLACE INTO history VALUES(?,?,?)",
                            (date, label, value))
    fng = data.get("crypto_fng", {})
    if "error" not in fng:
        for date, value in fng.get("series", []):
            con.execute("INSERT OR REPLACE INTO history VALUES(?,?,?)",
                        (date, "크립토공포탐욕", value))
    con.commit()
    con.close()


def load_series(metric, days=60):
    """DB에서 특정 지표 최근 days개 시계열 → (dates, values) 오래된→최신."""
    con = _db()
    cur = con.execute(
        "SELECT date, value FROM history WHERE metric=? ORDER BY date DESC LIMIT ?",
        (metric, days))
    rows = list(reversed(cur.fetchall()))
    con.close()
    dates = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    return dates, vals


def _fmt_num(val, label=""):
    """숫자 표시 포맷 — 한국 주가·지수는 천단위 콤마+정수, 나머진 소수2자리."""
    try:
        f = float(val)
    except (ValueError, TypeError):
        return str(val)
    # 값이 크면(1000 이상) 콤마+정수, 작으면 소수 2자리
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    return f"{f:.2f}"


def _compare_lines(dates, values):
    """어제/5일/20일/60일 전 대비 계산 → 툴팁 텍스트 리스트."""
    if not values:
        return []
    latest = values[-1]
    out = [f"현재: {_fmt_num(latest)} ({dates[-1]})"]
    for label, back in [("어제", 1), ("5일전", 5), ("20일전", 20), ("60일전", 60)]:
        if len(values) > back:
            past = values[-1 - back]
            pct = (latest / past - 1) * 100 if past else 0
            sign = "▲" if pct > 0 else ("▼" if pct < 0 else "-")
            out.append(f"{label} {_fmt_num(past)} 대비 {sign}{abs(pct):.2f}%")
    return out


_SPARK_ID = [0]  # 툴팁 고유 id 카운터


def sparkline_svg(values, dates=None, w=120, h=28, log_scale=True):
    """호버 툴팁 지원 미니 꺾은선. log_scale=변동폭 작아도 티나게."""
    if not values or len(values) < 2:
        return '<span style="font-size:11px;color:#888780;">데이터 쌓이는 중</span>'
    import math
    dates = dates or [""] * len(values)
    # 로그 스케일: 변동폭 작은 지표(금리 등)도 눈에 띄게
    if log_scale and min(values) > 0:
        scaled = [math.log(v) for v in values]
    else:
        scaled = values
    lo, hi = min(scaled), max(scaled)
    rng = (hi - lo) or 1
    n = len(values)
    pts = " ".join(f"{i/(n-1)*w:.1f},{h - (s-lo)/rng*h:.1f}" for i, s in enumerate(scaled))
    up = values[-1] >= values[0]
    color = "#3B6D11" if up else "#A32D2D"
    _SPARK_ID[0] += 1
    sid = _SPARK_ID[0]
    tip = "\n".join(_compare_lines(dates, values))
    tip_attr = tip.replace('"', '&quot;')
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'style="display:block;margin-top:6px;overflow:visible;cursor:crosshair;" '
        f'class="spark" data-tip="{tip_attr}">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="transparent"/>'
        f'</svg>')



# ── 소스 4: DART 한국 공시 (미국 EDGAR 8-K 격) ─────────────
DART_CORP = {
    "삼성전자":   "00126380",
    "SK하이닉스": "00164779",
}
# DART 공시 유형 → (유형명, 쉬운 설명, 중요표시) — 제목에 키워드 있으면 매칭
DART_TYPES = [
    ("단일판매",       ("대형 수주/계약", "대규모 판매·공급 계약 체결 — 호재 가능", True)),
    ("공급계약",       ("대형 수주/계약", "대규모 공급 계약 체결 — 호재 가능", True)),
    ("잠정실적",       ("잠정 실적", "확정 전 실적 속보 — 어닝 서프라이즈/쇼크 체크", True)),
    ("영업(잠정)실적", ("잠정 실적", "확정 전 실적 속보", True)),
    ("분기보고서",     ("실적(분기)", "분기 재무·사업 내용 정식 보고", True)),
    ("반기보고서",     ("실적(반기)", "반기 재무·사업 내용 정식 보고", True)),
    ("사업보고서",     ("실적(연간)", "연간 재무·사업 내용 정식 보고", True)),
    ("유상증자",       ("유상증자", "새 주식 발행해 자금조달 — 물량부담(대체로 악재)", True)),
    ("무상증자",       ("무상증자", "주주에게 공짜 주식 배정 — 대체로 호재성", True)),
    ("전환사채",       ("전환사채(CB)", "주식전환 가능 채권 발행 — 잠재 물량부담", True)),
    ("신주인수권부사채", ("신주인수권부사채(BW)", "신주인수권 붙은 채권 발행", True)),
    ("자기주식취득",   ("자사주 매입", "회사가 자기 주식 사들임 — 대체로 호재(주가 방어)", True)),
    ("자기주식처분",   ("자사주 처분", "보유 자사주 매각 — 물량 나옴", True)),
    ("자기주식",       ("자사주 관련", "자기주식 매입/처분", True)),
    ("현금ㆍ현물배당", ("배당 결정", "주주 배당 결정", False)),
    ("배당",           ("배당 관련", "배당 관련 공시", False)),
    ("합병",           ("합병/인수", "회사 합병·인수 — 중대 이벤트", True)),
    ("영업양수도",     ("영업 인수/매각", "사업부 인수 또는 매각", True)),
    ("주식등의대량보유", ("대량보유 신고(5%룰)", "5% 이상 지분 변동 — 미국 13D/G 격", True)),
    ("최대주주",       ("최대주주 변동", "최대주주 지분·지위 변동", True)),
    ("임원ㆍ주요주주", ("내부자 지분변동", "임원·대주주가 자사주 매매 — 미국 Form4 격, 매수는 강세신호", False)),
    ("특수관계인",     ("특수관계 거래", "계열사·특수관계인과의 거래", False)),
    ("풍문또는보도",   ("루머·보도 해명", "언론보도·소문에 대한 회사 공식 입장", False)),
    ("주요사항보고",   ("주요사항 보고", "회사 중대사항 신고", True)),
    ("공정공시",       ("공정공시", "실적·전망 등 선제적 자율 공시", False)),
    ("기업설명회",     ("IR 개최", "기관·투자자 대상 기업설명회", False)),
    ("횡령",           ("횡령·배임", "횡령·배임 발생 — 악재", True)),
    ("소송",           ("소송", "소송 제기·판결 — 리스크 체크", True)),
]


def _dart_classify(title):
    """제목 → (유형명, 설명, 중요여부). 못 찾으면 원제목 그대로."""
    for kw, info in DART_TYPES:
        if kw in title:
            return {"type": info[0], "desc": info[1], "important": info[2]}
    return {"type": title, "desc": "", "important": False}


def fetch_dart(corp_map=None, recent_count=6):
    """삼성·하이닉스 최근 공시 + 유형 분류/설명. 키는 env DART_API_KEY."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return {"error": "DART_API_KEY 없음 (.env에 추가 필요)"}
    corp_map = corp_map or DART_CORP
    out = {}
    for name, corp in corp_map.items():
        try:
            q = urllib.parse.urlencode({
                "crtfc_key": key, "corp_code": corp,
                "page_count": recent_count, "page_no": 1,
            })
            data = _get_json(f"https://opendart.fss.or.kr/api/list.json?{q}")
            if data.get("status") != "000":
                out[name] = {"error": f"DART {data.get('status')}: {data.get('message')}"}
                continue
            items = []
            for r in data.get("list", [])[:recent_count]:
                title = r.get("report_nm", "")
                cls = _dart_classify(title)
                items.append({
                    "title": title, "date": r.get("rcept_dt", ""),
                    "type": cls["type"], "desc": cls["desc"], "important": cls["important"],
                    "rcept_no": r.get("rcept_no", ""),  # 원문 링크용
                })
            out[name] = {"items": items}
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


def compute_rs(all_data, period=20):
    """종목별 상대강도: (종목 period수익) - (벤치 period수익). 양수=아웃퍼폼(대장주)."""
    pool = {}
    for sec in ("indices", "korea", "us_semi"):
        block = all_data.get(sec, {})
        if "error" in block:
            continue
        for label, v in block.items():
            if isinstance(v, dict) and "error" not in v and v.get("full_series"):
                pool[label] = v["full_series"]

    def ret(series, p):
        return (series[-1] / series[-1 - p] - 1) * 100 if len(series) > p else None

    rs = {}
    for label, bench in RS_BENCH.items():
        s, b = pool.get(label), pool.get(bench)
        if not s or not b:
            continue
        sr, br = ret(s, period), ret(b, period)
        if sr is None or br is None:
            continue
        rs[label] = {"rs": sr - br, "stock_ret": sr, "bench_ret": br, "bench": bench}
    return rs


def anomaly_flags(all_data):
    """위험/특이 신호 → 상단 배너용 경고 리스트."""
    flags = []
    fred = all_data.get("fred", {})
    if "error" not in fred:
        vix = fred.get("VIX(공포지수)", {})
        if isinstance(vix, dict) and "error" not in vix and "value" in vix:
            try:
                v = float(vix["value"])
                if v >= 30:
                    flags.append(f"⚠️ VIX {v:.1f} — 공포 확대 (조정·변동성 경계)")
                elif v >= 25:
                    flags.append(f"△ VIX {v:.1f} — 변동성 상승")
            except (ValueError, TypeError):
                pass
        yc = fred.get("장단기금리차", {})
        if isinstance(yc, dict) and "error" not in yc and "value" in yc:
            try:
                if float(yc["value"]) < 0:
                    flags.append(f"⚠️ 장단기금리 역전 ({yc['value']}) — 침체 선행신호")
            except (ValueError, TypeError):
                pass
        hy = fred.get("하이일드스프레드", {})
        if isinstance(hy, dict) and "error" not in hy and hy.get("delta_prev"):
            if hy["delta_prev"] > 0.2:
                flags.append("⚠️ 하이일드 스프레드 급등 — 신용 리스크오프")
    for sec in ("indices", "korea"):
        block = all_data.get(sec, {})
        if "error" in block:
            continue
        for label, v in block.items():
            if isinstance(v, dict) and "error" not in v and v.get("pct_prev", 0) <= -3:
                flags.append(f"⚠️ {label} {v['pct_prev']:.1f}% 급락")
    return flags


# ── 이벤트 캘린더: 다가오는 예정 이벤트 (D-day 정렬) ────────
# FOMC 2026 금리결정 발표일 (2일 회의의 2일째). ⚠️ 연말에 다음해 일정으로 갱신할 것
FOMC_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
# FRED 릴리스 ID → 지표명 (다음 발표 예정일 조회용)
FRED_RELEASES = {
    10: "미국 CPI (소비자물가)",
    50: "미국 고용보고서",
    54: "미국 PCE (개인소비지출)",
}


# 한국 금통위 통화정책방향 결정회의 2026 (한은 확정). ⚠️ 연말에 갱신
BOK_2026 = [
    "2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28",
    "2026-07-16", "2026-08-27", "2026-10-22", "2026-11-26",
]
# 한국 반도체 잠정실적 발표 (대략 — 삼성·하이닉스는 분기 다음달 초순, 정확일은 직전 공지)
# ⚠️ 확정일 아님. "예상"으로 표시. 실제는 DART 잠정실적 공시로 확인
KR_EARNINGS_APPROX = [
    ("2026-10-08", "삼성전자 3Q 잠정실적(예상)"),
    ("2026-10-24", "SK하이닉스 3Q 실적(예상)"),
]


def fetch_events():
    """다가오는 예정 이벤트 수집 → D-day 계산 → 오름차순 정렬. 날짜는 공식소스만(추측 금지)."""
    today = datetime.now().date()
    events = []

    # 1) FOMC (하드코딩 확정 일정)
    for d in FOMC_2026:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        if dt >= today:
            events.append({"date": d, "title": "FOMC 금리결정", "cat": "FOMC",
                           "note": "오후 2시(ET) 발표"})

    # 1-2) 한국 금통위 (하드코딩 확정)
    for d in BOK_2026:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        if dt >= today:
            events.append({"date": d, "title": "한국 금통위(기준금리)", "cat": "금통위",
                           "note": "현재 2.50%"})

    # 1-3) 한국 실적 (대략 — 예상)
    for d, title in KR_EARNINGS_APPROX:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
        if dt >= today:
            events.append({"date": d, "title": title, "cat": "실적KR", "note": "확정 아님"})

    # 2) 미국 경제지표 (FRED 릴리스 캘린더 — 다음 예정일 1건)
    key = os.environ.get("FRED_API_KEY")
    if key:
        for rid, name in FRED_RELEASES.items():
            try:
                q = urllib.parse.urlencode({
                    "release_id": rid, "api_key": key, "file_type": "json",
                    "include_release_dates_with_no_data": "true", "sort_order": "asc",
                })
                data = _get_json(f"https://api.stlouisfed.org/fred/release/dates?{q}")
                for r in data.get("release_dates", []):
                    dt = datetime.strptime(r["date"], "%Y-%m-%d").date()
                    if dt >= today:
                        events.append({"date": r["date"], "title": name, "cat": "지표", "note": ""})
                        break
            except Exception:
                pass

    # 3) 미국 반도체 실적 (Tier1 — 되는 종목만, 없으면 스킵)
    try:
        import yfinance as yf
        for label, tk in US_SEMI_TICKERS.items():
            try:
                cal = yf.Ticker(tk).calendar
                ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if ed and len(ed) > 0:
                    dt = ed[0]
                    if dt >= today:
                        events.append({"date": dt.strftime("%Y-%m-%d"),
                                       "title": f"{label} 실적발표", "cat": "실적", "note": ""})
            except Exception:
                pass
        # 3-2) Tier2 관심 대형주 — 실적 D-14 이내로 임박할 때만
        for label, tk in WATCHLIST_TICKERS.items():
            try:
                cal = yf.Ticker(tk).calendar
                ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if ed and len(ed) > 0:
                    dt = ed[0]
                    dd = (dt - today).days
                    if 0 <= dd <= WATCHLIST_IMMINENT_DAYS:  # 임박한 것만
                        events.append({"date": dt.strftime("%Y-%m-%d"),
                                       "title": f"{label} 실적발표", "cat": "실적대형", "note": "대형주"})
            except Exception:
                pass
    except ImportError:
        pass

    for e in events:
        dt = datetime.strptime(e["date"], "%Y-%m-%d").date()
        e["dday"] = (dt - today).days
    events.sort(key=lambda e: e["dday"])
    return events


def collect_all(us_tickers=None):
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            return {"error": str(e)}
    events = fetch_events()  # 먼저 이벤트 수집 (실적 임박 대형주 파악)
    # 공시 대상 = 반도체 대장주 + 실적 임박한 대형주(캘린더에 뜬 것)
    semi = ["NVDA", "AMD", "MU", "AVGO"]  # TSM은 외국기업(8-K 없음) 제외
    imminent = []
    for e in events:
        if e["cat"] == "실적대형":
            label = e["title"].replace(" 실적발표", "")
            tk = WATCHLIST_TICKERS.get(label)
            if tk:
                imminent.append(tk)
    edgar_tickers = us_tickers if us_tickers else (semi + imminent)
    result = {
        "생성시각": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "indices": safe(fetch_indices, INDEX_TICKERS),
        "us_semi": safe(fetch_indices, US_SEMI_TICKERS),
        "korea": safe(fetch_indices, KR_TICKERS),
        "fred": safe(fetch_fred, FRED_SERIES),
        "crypto_fng": safe(fetch_crypto_fng),
        "edgar": safe(fetch_edgar, edgar_tickers),
        "dart": safe(fetch_dart),
    }
    result["rs"] = compute_rs(result)
    result["flags"] = anomaly_flags(result)
    result["events"] = events
    try:
        save_history(result)
    except Exception as e:
        print(f"[경고] 저장 실패(리포트는 진행): {e}")
    return result


# ── 리포트 포맷 (텍스트) ───────────────────────────────────
def _arrow(delta):
    if delta is None:
        return ""
    if delta > 0:
        return f" ▲{abs(delta):.2f}"
    if delta < 0:
        return f" ▼{abs(delta):.2f}"
    return " (변화없음)"


def _pct_arrow(pct):
    if pct is None:
        return ""
    a = "▲" if pct > 0 else ("▼" if pct < 0 else "-")
    return f"{a}{abs(pct):.2f}%"


def to_report(data):
    lines = [f"📊 매크로 데일리 · {data['생성시각']}", ""]
    # 이상 플래그 배너 (맨 위)
    flags = data.get("flags", [])
    if flags:
        lines.append("── 오늘의 경고 신호 ──")
        for f in flags:
            lines.append(f"  {f}")
        lines.append("")
    rs = data.get("rs", {})
    # 다가오는 이벤트 캘린더
    events = data.get("events", [])
    if events:
        lines.append("── 다가오는 이벤트 ──")
        for e in events[:10]:
            dd = e["dday"]
            tag = "🔴 오늘" if dd == 0 else (f"🟡 D-{dd}" if dd <= 7 else f"D-{dd}")
            note = f" ({e['note']})" if e.get("note") else ""
            lines.append(f"  {tag}  {e['date']}  {e['title']}{note}")
        lines.append("")
    idx = data.get("indices", {})
    if "error" not in idx:
        lines.append("[간밤 미국 시장]")
        for label, v in idx.items():
            if "error" in v:
                lines.append(f"  {label}: (수집실패)")
            else:
                p52 = f" · 52주 {v['pos52']:.0f}%" if v.get("pos52") is not None else ""
                lines.append(f"  {label}: {v['value']}  {_pct_arrow(v.get('pct_prev'))}{p52}  ({v['as_of']})")
        lines.append("")
    # 미국 반도체 개별종목 + RS (누가 대장주냐)
    semi = data.get("us_semi", {})
    if "error" not in semi:
        lines.append("[미국 반도체 — RS = SOX 대비 상대강도(20일), 양수=대장주]")
        # RS 높은 순 정렬
        ranked = sorted(semi.items(),
                        key=lambda kv: rs.get(kv[0], {}).get("rs", -999), reverse=True)
        for label, v in ranked:
            if "error" in v:
                lines.append(f"  {label}: (수집실패)")
                continue
            r = rs.get(label, {})
            rs_txt = f"RS {r['rs']:+.1f}%p" if r else "RS -"
            p52 = f"52주 {v['pos52']:.0f}%" if v.get("pos52") is not None else ""
            lines.append(f"  {label}: {v['value']} {_pct_arrow(v.get('pct_prev'))} · {rs_txt} · {p52}")
        lines.append("")
    kr = data.get("korea", {})
    if "error" not in kr:
        lines.append("[오늘 한국 시장]  ↑ 간밤 미국 흐름 참고 (일반적으로 미국→한국)")
        for label, v in kr.items():
            if "error" in v:
                lines.append(f"  {label}: (수집실패)")
            else:
                r = rs.get(label, {})
                rs_txt = f" · RS {r['rs']:+.1f}%p" if r else ""
                p52 = f" · 52주 {v['pos52']:.0f}%" if v.get("pos52") is not None else ""
                lines.append(f"  {label}: {v['value']}  {_pct_arrow(v.get('pct_prev'))}{rs_txt}{p52}  ({v['as_of']})")
        lines.append("")
    fred = data.get("fred", {})
    if "error" not in fred:
        lines.append("[미국 매크로] (▲▼ = 직전 관측 대비, 추세 = 최근 N회 시작 대비)")
        for label, v in fred.items():
            if "error" in v:
                lines.append(f"  {label}: (수집실패)")
                continue
            try:
                val = f"{float(v['value']):.2f}"
            except (ValueError, TypeError):
                val = v['value']
            trend = f" | 추세({v['trend_n']}회): {_arrow(v['delta_trend'])}" if v.get('trend_n', 0) > 1 else ""
            lines.append(f"  {label}: {val}{_arrow(v.get('delta_prev'))}  ({v['as_of']}){trend}")
    fng = data.get("crypto_fng", {})
    if "error" not in fng:
        lines.append(
            f"\n[크립토 공포탐욕] {fng['value']}{_arrow(fng.get('delta_prev'))} "
            f"({fng['class']}) · {fng['as_of']} | 추세({fng['trend_n']}회):{_arrow(fng.get('delta_trend'))}"
        )
    edg = data.get("edgar", {})
    if "error" not in edg:
        lines.append("\n[미국 최신 공시 (SEC EDGAR)]")
        for t, v in edg.items():
            if "error" in v:
                lines.append(f"  {t}: (실패)")
                continue
            lines.append(f"  {t}: {_form_label(v.get('form','?'))} · {v.get('as_of','?')}")
            detail = v.get("detail")
            if detail:
                lines.append(f"    → {detail['name']} ({detail['title']})")
                for tx in detail["transactions"]:
                    mark = "⭐" if tx["code"] == "P" else "·"
                    if tx.get("is_cash"):
                        detail_txt = f"{tx['shares']}주 · 주당 ${tx['price']}"
                        if tx.get("amount"):
                            detail_txt += f" = ${tx['amount']:,.0f}"
                    else:
                        detail_txt = f"{tx['shares']}주 · 이전/분배(현금거래 아님)"
                    ratio = f" (보유의 {tx['ratio']:.1f}%)" if tx.get("ratio") else ""
                    lines.append(f"    {mark} {tx['date']} {tx['code_desc']}: {detail_txt}{ratio}")
            for e in v.get("eightk", []):
                hint = f" → {e['hint']}" if e.get("hint") else ""
                lines.append(f"    📌 8-K {e['date']}: {e['item_desc']}{hint}")
    dart = data.get("dart", {})
    if "error" not in dart:
        lines.append("\n[한국 최신 공시 (DART)]")
        for name, v in dart.items():
            if "error" in v:
                lines.append(f"  {name}: (실패: {v['error']})")
                continue
            lines.append(f"  {name}:")
            for it in v.get("items", []):
                d = it["date"]
                dfmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
                mark = "📌" if it.get("important") else "·"
                desc = f" — {it['desc']}" if it.get("desc") else ""
                lines.append(f"    {mark} {dfmt} [{it['type']}]{desc}")
    return "\n".join(lines)


def _delta_html(delta, unit=""):
    if delta is None:
        return '<span style="color:#888780;font-size:12px;">-</span>'
    color = "#3B6D11" if delta > 0 else ("#A32D2D" if delta < 0 else "#888780")
    sign = "▲" if delta > 0 else ("▼" if delta < 0 else "-")
    return f'<span style="color:{color};font-size:12px;">{sign}{abs(delta):.2f}{unit}</span>'


def _spark_data(metric):
    """카드에 실을 클릭 데이터(JSON). 데이터 없으면 빈 문자열."""
    dates, vals = load_series(metric)
    if not vals or len(vals) < 2:
        return ""
    info = METRIC_INFO.get(metric, DEFAULT_INFO)
    return json.dumps({"metric": metric, "dates": dates, "values": vals, "info": info},
                      ensure_ascii=False).replace('"', "&quot;")


def _spark(metric, **kw):
    """스파크라인 SVG만 (호버 툴팁 포함). 클릭은 카드 전체가 담당."""
    dates, vals = load_series(metric)
    return sparkline_svg(vals, dates=dates, **kw)


def to_html(data):
    """report.html 생성 — 카드 그리드 + 크립토 공포탐욕 게이지."""
    # 이벤트 title → 가장 가까운 D-day (FOMC처럼 여러 번이면 최소값)
    event_dday = {}
    for e in data.get("events", []):
        t = e["title"]
        if t not in event_dday or e["dday"] < event_dday[t]:
            event_dday[t] = e["dday"]
    fred = data.get("fred", {})
    cards = []
    for label, v in fred.items():
        if "error" in v:
            cards.append(f'''<div class="card"><p class="lbl">{label}</p>
                <p class="val">-</p><p class="date">수집실패</p></div>''')
            continue
        try:
            val = f"{float(v['value']):.2f}"
        except (ValueError, TypeError):
            val = v['value']
        unit = v.get("unit", "")
        unit_html = f'<span style="font-size:13px;color:#888780;">{unit}</span>' if unit else ""
        cat = METRIC_CATEGORY.get(label)
        cat_bar = f'border-left:4px solid {cat[1]};' if cat else ''
        cat_tag = f'<span style="font-size:10px;color:{cat[1]};font-weight:600;">{cat[0]}</span> ' if cat else ''
        # 연결 이벤트 D-day → 임박할수록 배경 짙게
        ev_title = METRIC_EVENT.get(label)
        ev_dday = event_dday.get(ev_title) if ev_title else None
        urg_bg, urg_badge = _urgency_style(ev_dday)
        cards.append(f'''<div class="card clickable" data-chart="{_spark_data(label)}" style="{cat_bar}{urg_bg}">
            <p class="lbl">{cat_tag}{label} {urg_badge}</p>
            <p class="val">{val} {unit_html}</p>
            <p class="date">{_delta_html(v.get('delta_prev'))} · {v['as_of']}</p>
            {_spark(label)}
        </div>''')
    cards_html = "\n".join(cards)

    fng = data.get("crypto_fng", {})
    fng_html = ""
    if "error" not in fng:
        pos = max(0, min(100, fng['value']))
        delta_span = _delta_html(fng.get('delta_prev'))
        fng_html = f'''<div class="gauge-box clickable" data-chart="{_spark_data("크립토공포탐욕")}">
            <div class="gauge-head"><span class="lbl">크립토 공포탐욕지수</span>{delta_span}</div>
            <div class="gauge-num"><span class="val" style="font-size:28px;">{fng['value']}</span>
                <span class="date">{fng['class']}</span></div>
            <div class="gauge-track"><div class="gauge-marker" style="left:{pos}%;"></div></div>
            <div class="gauge-legend"><span>0 극단공포</span><span>50 중립</span><span>100 극단탐욕</span></div>
            {_spark("크립토공포탐욕", w=560, h=32, log_scale=False)}
        </div>'''

    edg = data.get("edgar", {})
    edg_rows = []
    if "error" in edg:
        edg = {}
    for t, v in edg.items():
        if isinstance(v, dict) and "error" in v:
            edg_rows.append(f'<tr><td>{t}</td><td colspan="2">수집실패</td></tr>')
            continue
        form_txt = _form_label(v.get("form", "?"))
        link = v.get("link", "")
        form_cell = f'<a href="{link}" style="color:#185FA5;text-decoration:none;">{form_txt}</a>' if link else form_txt
        edg_rows.append(f'<tr><td>{t}</td><td>{form_cell}</td><td>{v.get("as_of","?")}</td></tr>')
        detail = v.get("detail")
        if detail:
            for tx in detail["transactions"]:
                mark = " (강세신호)" if tx["code"] == "P" else ""
                if tx.get("is_cash"):
                    detail_txt = f'{tx["shares"]}주 · 주당 ${tx["price"]}'
                    if tx.get("amount"):
                        detail_txt += f' = ${tx["amount"]:,.0f}'
                else:
                    detail_txt = f'{tx["shares"]}주 · 이전/분배(현금거래 아님)'
                ratio = f' · 보유의 {tx["ratio"]:.1f}%' if tx.get("ratio") else ""
                edg_rows.append(
                    f'<tr><td></td><td colspan="2" style="color:#5F5E5A;font-size:12px;">'
                    f'{detail["name"]} · {tx["code_desc"]}{mark}: {detail_txt}{ratio}</td></tr>'
                )
        for e in v.get("eightk", []):
            elink = e.get("link", "")
            hint = f'<div style="color:#888780;font-size:11px;margin-left:16px;">→ {e["hint"]}</div>' if e.get("hint") else ""
            label = f'📌 8-K {e["date"]}: {e["item_desc"]}'
            cell = f'<a href="{elink}" style="color:#185FA5;text-decoration:none;">{label}</a>' if elink else label
            edg_rows.append(
                f'<tr><td></td><td colspan="2" style="font-size:12px;">{cell}{hint}</td></tr>')
    edg_html = "\n".join(edg_rows) if edg_rows else '<tr><td colspan="3">데이터 없음</td></tr>'

    dart = data.get("dart", {})
    dart_rows = []
    if "error" not in dart:
        for name, v in dart.items():
            if "error" in v:
                dart_rows.append(f'<tr><td>{name}</td><td colspan="2" style="color:#888780;">{v["error"]}</td></tr>')
                continue
            first = True
            for it in v.get("items", []):
                d = it["date"]
                dfmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
                nm = name if first else ""
                first = False
                mark = '📌 ' if it.get("important") else ''
                typ = it["type"]
                desc = f'<div style="color:#888780;font-size:11px;">{it["desc"]}</div>' if it.get("desc") else ""
                link = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it["rcept_no"]}'
                dart_rows.append(
                    f'<tr><td>{nm}</td>'
                    f'<td><a href="{link}" style="color:#185FA5;text-decoration:none;">{mark}{typ}</a>{desc}</td>'
                    f'<td>{dfmt}</td></tr>')
    dart_html = ("\n".join(dart_rows)) if dart_rows else ""

    idx = data.get("indices", {})
    idx_cards = []
    if "error" not in idx:
        for label, v in idx.items():
            if "error" in v:
                idx_cards.append(f'<div class="card"><p class="lbl">{label}</p><p class="val">-</p></div>')
                continue
            pct = v.get("pct_prev")
            color = "#3B6D11" if (pct or 0) > 0 else ("#A32D2D" if (pct or 0) < 0 else "#888780")
            arrow = "▲" if (pct or 0) > 0 else ("▼" if (pct or 0) < 0 else "-")
            idx_cards.append(f'''<div class="card clickable" data-chart="{_spark_data(label)}">
                <p class="lbl">{label}</p>
                <p class="val">{_fmt_num(v['value'])}</p>
                <p class="date" style="color:{color};">{arrow}{abs(pct):.2f}% · {v['as_of']}</p>
                {_spark(label)}
            </div>''')
    idx_html = f'<h1>간밤 미국 시장</h1><div class="grid">{"".join(idx_cards)}</div>' if idx_cards else ""

    # 미국 반도체 RS 카드 (RS 높은 순)
    rs = data.get("rs", {})
    semi = data.get("us_semi", {})
    semi_cards = []
    if "error" not in semi:
        ranked = sorted(semi.items(), key=lambda kv: rs.get(kv[0], {}).get("rs", -999), reverse=True)
        for label, v in ranked:
            if "error" in v:
                continue
            pct = v.get("pct_prev", 0)
            color = "#3B6D11" if pct > 0 else ("#A32D2D" if pct < 0 else "#888780")
            arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "-")
            r = rs.get(label, {})
            rs_val = r.get("rs")
            rs_color = "#3B6D11" if (rs_val or 0) > 0 else "#A32D2D"
            rs_html = f'<span style="color:{rs_color};font-size:12px;">RS {rs_val:+.1f}%p</span>' if rs_val is not None else ""
            p52 = v.get("pos52")
            p52_html = f'<span style="font-size:11px;color:#888780;"> · 52주 {p52:.0f}%</span>' if p52 is not None else ""
            semi_cards.append(f'''<div class="card clickable" data-chart="{_spark_data(label)}">
                <p class="lbl">{label}</p>
                <p class="val">{_fmt_num(v['value'])}</p>
                <p class="date" style="color:{color};">{arrow}{abs(pct):.2f}%</p>
                <p class="date">{rs_html}{p52_html}</p>
                {_spark(label)}
            </div>''')
    import json as _json
    rs_help = _json.dumps({"metric": "RS(상대강도)", "info": CONCEPT_INFO["RS(상대강도)"]}, ensure_ascii=False).replace('"', "&quot;")
    p52_help = _json.dumps({"metric": "52주 위치", "info": CONCEPT_INFO["52주 위치"]}, ensure_ascii=False).replace('"', "&quot;")
    help_badges = (f'<span class="concept" data-concept="{rs_help}" style="cursor:pointer;color:#185FA5;font-size:12px;">ⓘ RS란?</span> '
                   f'<span class="concept" data-concept="{p52_help}" style="cursor:pointer;color:#185FA5;font-size:12px;">ⓘ 52주 위치란?</span>')
    semi_html = (f'<h1 style="margin-top:1.5rem;">미국 반도체 <span style="font-size:13px;color:#888780;font-weight:400;">RS=SOX 대비 상대강도 · 양수=대장주</span></h1>'
                 f'<div style="margin:-4px 0 8px;">{help_badges}</div>'
                 f'<div class="grid">{"".join(semi_cards)}</div>') if semi_cards else ""

    # 이상 플래그 배너
    flags = data.get("flags", [])
    flags_html = ""
    if flags:
        items = "".join(f'<div style="padding:4px 0;">{f}</div>' for f in flags)
        flags_html = (f'<div style="background:#FCEBEB;border:0.5px solid #E24B4A;border-radius:8px;'
                      f'padding:12px 16px;margin:1rem 0;font-size:13px;color:#791F1F;">'
                      f'<b>오늘의 경고 신호</b>{items}</div>')

    # 다가오는 이벤트 캘린더 (노란 카드, D-day 정렬)
    events = data.get("events", [])
    ev_html = ""
    if events:
        cat_color = {"FOMC": "#B45309", "지표": "#1D4ED8", "실적": "#7C3AED",
                     "금통위": "#059669", "실적KR": "#DB2777", "실적대형": "#9333EA"}
        cards = []
        for e in events[:16]:
            dd = e["dday"]
            if dd == 0:
                bg, bd, ddtxt = "#FDE2E2", "#E24B4A", "오늘"
            elif dd <= 7:
                bg, bd, ddtxt = "#FEF3C7", "#F59E0B", f"D-{dd}"  # 노란 하이라이트
            else:
                bg, bd, ddtxt = "#FAFAF7", "#E1E0D9", f"D-{dd}"
            cc = cat_color.get(e["cat"], "#888780")
            note = f'<div style="font-size:11px;color:#888780;">{e["note"]}</div>' if e.get("note") else ""
            einfo = EVENT_INFO.get(e["cat"], {})
            ehelp = _json.dumps({"metric": e["title"], "info": einfo}, ensure_ascii=False).replace('"', "&quot;") if einfo else ""
            open_tag = f'<div class="concept" data-concept="{ehelp}" style="cursor:pointer;' if ehelp else '<div style="'
            cards.append(
                f'{open_tag}background:{bg};border:1px solid {bd};border-radius:8px;padding:10px 12px;min-width:130px;">'
                f'<div style="font-size:18px;font-weight:600;color:{bd if dd<=7 else "#555"};">{ddtxt}</div>'
                f'<div style="font-size:13px;font-weight:500;margin:2px 0;">{e["title"]}</div>'
                f'<div style="font-size:11px;color:{cc};">{e["date"]} · {e["cat"]}</div>{note}</div>')
        ev_html = (f'<h1 style="margin-top:1.5rem;">다가오는 이벤트 <span style="font-size:13px;color:#888780;font-weight:400;">D-7 이내 노란 강조 · 클릭하면 설명</span></h1>'
                   f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:1rem 0;">{"".join(cards)}</div>')

    kr = data.get("korea", {})
    kr_cards = []
    if "error" not in kr:
        for label, v in kr.items():
            if "error" in v:
                kr_cards.append(f'<div class="card"><p class="lbl">{label}</p><p class="val">-</p></div>')
                continue
            pct = v.get("pct_prev")
            color = "#3B6D11" if (pct or 0) > 0 else ("#A32D2D" if (pct or 0) < 0 else "#888780")
            arrow = "▲" if (pct or 0) > 0 else ("▼" if (pct or 0) < 0 else "-")
            r = rs.get(label, {})
            rs_val = r.get("rs")
            extra = ""
            if rs_val is not None:
                rc = "#3B6D11" if rs_val > 0 else "#A32D2D"
                extra += f'<span style="color:{rc};font-size:11px;">RS {rs_val:+.1f}%p</span>'
            p52 = v.get("pos52")
            if p52 is not None:
                extra += f'<span style="font-size:11px;color:#888780;"> · 52주 {p52:.0f}%</span>'
            extra_html = f'<p class="date">{extra}</p>' if extra else ""
            kr_cards.append(f'''<div class="card clickable" data-chart="{_spark_data(label)}">
                <p class="lbl">{label}</p>
                <p class="val">{_fmt_num(v['value'])}</p>
                <p class="date" style="color:{color};">{arrow}{abs(pct):.2f}% · {v['as_of']}</p>
                {extra_html}
                {_spark(label)}
            </div>''')
    kr_html = f'<h1 style="margin-top:1.5rem;">오늘 한국 시장 <span style="font-size:13px;color:#888780;font-weight:400;">↑ 간밤 미국 흐름 참고</span></h1><div class="grid">{"".join(kr_cards)}</div>' if kr_cards else ""

    return f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, "Malgun Gothic", sans-serif; max-width: 680px;
         margin: 2rem auto; padding: 0 1rem; background: #fcfcfb; color: #0b0b0b; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1a1a19; color: #ffffff; }}
    .card, .gauge-box {{ background: #232322 !important; }}
  }}
  h1 {{ font-size: 20px; font-weight: 500; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 1rem 0; }}
  .card {{ background: #f1efe8; border-radius: 8px; padding: 1rem; }}
  .clickable {{ cursor: pointer; transition: transform .08s ease, box-shadow .15s ease, background .15s ease; }}
  .clickable:hover {{ background: #e7e4d9; box-shadow: 0 2px 10px rgba(0,0,0,.08); }}
  .clickable:active {{ transform: scale(.97); }}
  @media (prefers-color-scheme: dark) {{ .clickable:hover {{ background: #2e2e2c; }} }}
  .lbl {{ font-size: 13px; color: #5F5E5A; margin: 0 0 4px; }}
  .val {{ font-size: 24px; font-weight: 500; margin: 0; }}
  .date {{ font-size: 12px; color: #888780; margin: 4px 0 0; }}
  .gauge-box {{ background: #fff; border: 0.5px solid #d3d1c7; border-radius: 12px; padding: 1.25rem; margin-top: 1rem; }}
  .gauge-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }}
  .gauge-num {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 12px; }}
  .gauge-track {{ position: relative; height: 8px; border-radius: 4px;
                  background: linear-gradient(90deg,#A32D2D 0%,#EF9F27 50%,#3B6D11 100%); margin-bottom: 6px; }}
  .gauge-marker {{ position: absolute; top: -4px; width: 3px; height: 16px; background: #0b0b0b; border-radius: 2px; }}
  .gauge-legend {{ display: flex; justify-content: space-between; font-size: 11px; color: #888780; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 0.5rem; }}
  td {{ padding: 6px 4px; border-bottom: 0.5px solid #e1e0d9; }}
</style></head>
<body>
  <h1>📊 매크로 데일리 · {data['생성시각']}</h1>
  {flags_html}
  {ev_html}
  {idx_html}
  {semi_html}
  {kr_html}
  <h1 style="margin-top:1.5rem;">미국 매크로</h1>
  <div class="grid">{cards_html}</div>
  {fng_html}
  <h1 style="margin-top:1.5rem;">미국 최신 공시 <span style="font-size:13px;color:#888780;font-weight:400;">SEC EDGAR · 반도체+실적임박 대형주</span></h1>
  <table>{edg_html}</table>
  {"<h1 style='margin-top:1.5rem;'>한국 최신 공시 <span style=\"font-size:13px;color:#888780;font-weight:400;\">DART</span></h1><table>" + dart_html + "</table>" if dart_html else ""}
  <div id="spark-tip" style="position:fixed;display:none;z-index:999;background:#232322;color:#fff;
       font-size:12px;line-height:1.6;padding:8px 12px;border-radius:8px;white-space:pre;
       pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.3);"></div>

  <div id="modal-bg" style="position:fixed;inset:0;display:none;z-index:1000;
       background:rgba(0,0,0,.5);align-items:center;justify-content:center;padding:1rem;">
    <div id="modal" style="background:#fff;max-width:560px;width:100%;border-radius:12px;
         padding:1.5rem;max-height:85vh;overflow:auto;"></div>
  </div>

  <script>
    (function(){{
      var tip = document.getElementById('spark-tip');
      document.querySelectorAll('.spark').forEach(function(el){{
        el.addEventListener('mouseenter', function(){{ tip.textContent = el.getAttribute('data-tip'); tip.style.display='block'; }});
        el.addEventListener('mousemove', function(e){{ tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+14)+'px'; }});
        el.addEventListener('mouseleave', function(){{ tip.style.display='none'; }});
      }});

      var bg = document.getElementById('modal-bg'), modal = document.getElementById('modal');
      function fmt(v){{ return Math.abs(v)>=1000 ? Math.round(v).toLocaleString() : (Math.round(v*100)/100).toLocaleString(); }}

      function bigChart(dates, values, w, h){{
        var pad=68, iw=w-pad-12, ih=h-30;
        var lo=Math.min.apply(0,values), hi=Math.max.apply(0,values), rng=(hi-lo)||1;
        var n=values.length;
        var pts=values.map(function(v,i){{ return (pad+i/(n-1)*iw).toFixed(1)+','+(10+ih-(v-lo)/rng*ih).toFixed(1); }}).join(' ');
        var up=values[n-1]>=values[0], col=up?'#3B6D11':'#A32D2D';
        var yt='';
        for(var k=0;k<=2;k++){{ var yv=lo+rng*k/2, yy=10+ih-(k/2)*ih;
          yt+='<line x1="'+pad+'" y1="'+yy+'" x2="'+w+'" y2="'+yy+'" stroke="#e1e0d9"/>'
            +'<text x="'+(pad-8)+'" y="'+(yy+4)+'" text-anchor="end" font-size="10" fill="#888780">'+fmt(yv)+'</text>'; }}
        var xi=[0,Math.floor(n/2),n-1], xt='';
        xi.forEach(function(i){{ var xx=pad+i/(n-1)*iw;
          xt+='<text x="'+xx+'" y="'+(h-4)+'" text-anchor="middle" font-size="10" fill="#888780">'+(dates[i]||'').slice(5)+'</text>'; }});
        return '<svg width="100%" viewBox="0 0 '+w+' '+h+'" style="display:block;margin:8px 0;">'
          +yt+xt+'<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2"/></svg>';
      }}

      document.querySelectorAll('.concept').forEach(function(el){{
        el.addEventListener('click', function(){{
          var d = JSON.parse(el.getAttribute('data-concept'));
          function block(title, body){{
            return '<div style="margin-top:14px;">'
              +'<div style="font-size:14px;font-weight:500;margin-bottom:4px;">'+title+'</div>'
              +'<div style="border-bottom:0.5px solid #d3d1c7;margin-bottom:8px;"></div>'
              +'<div style="font-size:13px;line-height:1.75;color:#2C2C2A;">'+body+'</div></div>';
          }}
          modal.innerHTML = '<h2 style="margin:0;font-size:19px;">'+d.metric+'</h2>'
            +block('정의', d.info['정의'])+block('어떻게 보나', d.info['해석'])+block('예시', d.info['예시'])
            +'<p style="text-align:right;margin-top:16px;"><button onclick="document.getElementById(\\'modal-bg\\').style.display=\\'none\\'">닫기</button></p>';
          bg.style.display='flex';
        }});
      }});

      document.querySelectorAll('.clickable').forEach(function(el){{
        el.addEventListener('click', function(){{
          var raw = el.getAttribute('data-chart');
          if(!raw) return;
          var d = JSON.parse(raw);
          var v=d.values, dt=d.dates, n=v.length, cur=v[n-1];
          var rows='';
          [['어제',1],['5일전',5],['20일전',20],['60일전',60]].forEach(function(p){{
            if(n>p[1]){{ var past=v[n-1-p[1]], pct=(cur/past-1)*100, s=pct>0?'▲':(pct<0?'▼':'-');
              var c=pct>0?'#3B6D11':(pct<0?'#A32D2D':'#888780');
              rows+='<tr><td style="color:#5F5E5A;padding:3px 0;">'+p[0]+' ('+fmt(past)+')</td>'
                  +'<td style="text-align:right;color:'+c+';">'+s+Math.abs(pct).toFixed(2)+'%</td></tr>'; }}
          }});
          function block(title, body){{
            return '<div style="margin-top:14px;">'
              +'<div style="font-size:14px;font-weight:500;margin-bottom:4px;">'+title+'</div>'
              +'<div style="border-bottom:0.5px solid #d3d1c7;margin-bottom:8px;"></div>'
              +'<div style="font-size:13px;line-height:1.75;color:#2C2C2A;">'+body+'</div></div>';
          }}
          modal.innerHTML =
            '<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            +'<h2 style="margin:0;font-size:19px;">'+d.metric+'</h2>'
            +'<span style="font-size:22px;font-weight:500;">'+fmt(cur)+'</span></div>'
            +bigChart(dt, v, 520, 200)
            +'<table style="width:100%;font-size:13px;margin:8px 0 4px;">'+rows+'</table>'
            +block('정의', d.info['정의'])
            +block('어떻게 보나', d.info['해석'])
            +block('예시', d.info['예시'])
            +'<p style="text-align:right;margin-top:16px;"><button onclick="document.getElementById(\\'modal-bg\\').style.display=\\'none\\'">닫기</button></p>';
          bg.style.display='flex';
        }});
      }});
      bg.addEventListener('click', function(e){{ if(e.target===bg) bg.style.display='none'; }});
    }})();
  </script>
</body></html>'''
def send_telegram(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return {"skipped": "텔레그램 env 없음"}
    q = urllib.parse.urlencode({"chat_id": chat, "text": text})
    return _get_json(f"https://api.telegram.org/bot{tok}/sendMessage?{q}")


if __name__ == "__main__":
    import sys
    result = collect_all()
    report = to_report(result)
    print(report)
    html = to_html(result)
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(html)
    # GitHub Pages는 index.html을 기본 페이지로 씀 → 같이 저장
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\n→ report.html / index.html 생성됨")

    # --push 옵션 주면 자동으로 GitHub에 배포 (git 세팅 완료 후)
    if "--push" in sys.argv:
        import subprocess
        try:
            subprocess.run(["git", "add", "index.html", "report.html"], check=True)
            subprocess.run(["git", "commit", "-m",
                            f"update {result['생성시각']}"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("→ GitHub 배포 완료")
        except subprocess.CalledProcessError as e:
            print(f"[push 실패] {e} — git 세팅 확인 필요")
    # send_telegram(report)  # 텔레그램 env 넣고 주석 해제
