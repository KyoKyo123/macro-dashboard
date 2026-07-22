"""
deepdive.py — 특정 종목의 최근 8-K를 본문까지 뽑는 온디맨드 도구 (B안: 요약은 클로드에게).
사용법:
    python deepdive.py NVDA              최근 8-K 3건 본문 발췌(콘솔)
    python deepdive.py AMD --n 5         최근 5건
    python deepdive.py NVDA --full       본문 전체를 deepdive_NVDA.txt로 저장 (클로드에 붙여 요약용)

daily_macro는 "무슨 일 있었나"만, 이건 "그래서 뭔 내용인지" 원문까지.
한글 해석이 필요하면 --full로 뽑은 txt를 클로드에게 붙여넣으면 됨.
"""
import sys
import re
import json
import urllib.request

UA = {"User-Agent": "gyobeom-research bee@example.com"}
_CIK_CACHE = {}

EIGHTK_ITEMS = {
    "1.01": "중대계약 체결", "1.02": "중대계약 종료", "1.03": "파산·법정관리",
    "2.01": "자산 인수/매각", "2.02": "실적 발표", "2.03": "채무 발생",
    "2.05": "구조조정", "2.06": "자산 손상차손", "3.01": "상장폐지 경고",
    "4.01": "회계법인 교체", "4.02": "재무제표 재작성", "5.01": "경영권 변동",
    "5.02": "임원 선임/사임/보수변경", "5.07": "주주총회 표결", "7.01": "공정공시",
    "8.01": "기타 중요사건", "9.01": "재무제표·첨부",
}


def _get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20)


def ticker_to_cik(ticker):
    if not _CIK_CACHE:
        data = json.load(_get("https://www.sec.gov/files/company_tickers.json"))
        for row in data.values():
            _CIK_CACHE[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return _CIK_CACHE.get(ticker.upper())


def clean_text(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&#8217;|&#8220;|&#8221;|&nbsp;|&#160;|&amp;|&#8201;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def deepdive(ticker, n=3, full=False):
    cik = ticker_to_cik(ticker)
    if not cik:
        print(f"{ticker}: CIK 못 찾음")
        return
    data = json.load(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    r = data["filings"]["recent"]
    cik_no = cik.lstrip("0")

    blocks = []
    header = f"{ticker} 최근 8-K 딥다이브 (본문)"
    print(f"\n{'='*60}\n{header}\n{'='*60}")

    count = 0
    for i, f in enumerate(r["form"]):
        if f != "8-K":
            continue
        items = r.get("items", [""])[i]
        codes = [c.strip() for c in items.split(",")]
        desc = ", ".join(EIGHTK_ITEMS.get(c, c) for c in codes)
        acc = r["accessionNumber"][i]
        doc = r["primaryDocument"][i]
        date = r["filingDate"][i]
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_no}/{acc.replace('-','')}/{doc}"
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_no}/{acc.replace('-','')}/{acc}-index.htm"

        print(f"\n[{date}] {desc}")
        print(f"원문: {idx_url}")
        try:
            text = clean_text(_get(url).read().decode("utf-8", "ignore"))
            m = re.search(r"Item\s+\d\.\d\d", text)
            body = text[m.start():] if m else text
            print("본문 발췌:")
            print("  " + body[:600])
            blocks.append(f"[{date}] {desc}\n원문: {idx_url}\n{body[:8000]}\n")
        except Exception as e:
            print(f"  본문 읽기 실패: {e}")
        count += 1
        if count >= n:
            break

    if full and blocks:
        fname = f"deepdive_{ticker}.txt"
        with open(fname, "w", encoding="utf-8") as fp:
            fp.write(header + "\n\n" + "\n\n".join(blocks))
        print(f"\n→ 본문 전체 저장: {fname}")
        print("  (이 파일을 클로드에게 붙여넣으면 한글 요약·해석해줌)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python deepdive.py <종목> [--n 건수] [--full]")
        sys.exit(1)
    tk = sys.argv[1].upper()
    n = 3
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    deepdive(tk, n, full=("--full" in sys.argv))
