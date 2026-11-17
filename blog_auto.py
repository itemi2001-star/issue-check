# -*- coding: utf-8 -*-
"""
블로그 글감 자동화
[수집]
- 구글 트렌드(한국): 실시간 급상승 키워드 + 구글 검색량
- 시그널(signal.bz): 네이버 앱 기준 실시간 검색어 TOP10 (키 없이 동작)
- 구글 뉴스(한국): 주요 뉴스
- 네이버 뉴스: 언론사별 많이 본 뉴스 (키 없이 동작)
- (키 필요) 네이버 검색광고 API: 키워드별 네이버 월간 검색량(PC/모바일) + 연관 키워드
- (키 필요) 네이버 검색 API: 키워드별 블로그 문서 수(경쟁도), 키워드별 최신 네이버 뉴스
[출력]
- 검색량/경쟁도 표 + 뉴스 → GPT/Codex용 블로그 작성 프롬프트를 메일 발송 + 파일 저장

외부 라이브러리 없이 파이썬 기본 모듈만 사용합니다.
사용법:  python blog_auto.py           (실행 + 메일 발송)
         python blog_auto.py --dry-run (메일 안 보내고 프롬프트만 출력/저장)
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
LOG_PATH = os.path.join(BASE_DIR, "blog_auto.log")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=KR"
GOOGLE_NEWS_RSS = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
NAVER_RANKING = "https://news.naver.com/main/ranking/popularDay.naver"
SIGNAL_API = "https://api.signal.bz/news/realtime"   # 시그널: 네이버 앱 기준 실시간 검색어 TOP10
GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q="
HT_NS = "{https://trends.google.com/trending/rss}"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    # 환경변수가 있으면 config.json 값보다 우선 (GitHub Actions Secrets 용)
    env = os.environ.get
    m = cfg.setdefault("mail", {})
    nv = cfg.setdefault("naver", {})
    ad = cfg.setdefault("naver_ad", {})
    if env("MAIL_SENDER"):
        m["sender"] = env("MAIL_SENDER")
    if env("MAIL_APP_PASSWORD"):
        m["app_password"] = env("MAIL_APP_PASSWORD").replace(" ", "")
    if env("MAIL_TO"):
        m["to"] = [x.strip() for x in env("MAIL_TO").split(",") if x.strip()]
    for key, envname in (("client_id", "NAVER_CLIENT_ID"), ("client_secret", "NAVER_CLIENT_SECRET")):
        if env(envname):
            nv[key] = env(envname)
    for key, envname in (("api_key", "NAVER_AD_API_KEY"), ("secret_key", "NAVER_AD_SECRET_KEY"),
                         ("customer_id", "NAVER_AD_CUSTOMER_ID")):
        if env(envname):
            ad[key] = env(envname)
    return cfg


def fetch(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def to_int(v):
    """네이버 검색광고 API는 적은 값을 '< 10' 문자열로 줌"""
    if isinstance(v, (int, float)):
        return int(v)
    return 5 if "<" in str(v) else int(re.sub(r"\D", "", str(v)) or 0)


# ---------------------------------------------------------------- 구글
def parse_traffic(s):
    """'20,000+' / '2만+' 같은 문자열 -> 정렬용 숫자"""
    s = (s or "").replace(",", "").replace("+", "").strip()
    mult = 1
    for unit, m in (("만", 10_000), ("천", 1_000), ("K", 1_000), ("M", 1_000_000)):
        if s.endswith(unit):
            s, mult = s[: -len(unit)], m
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def is_korean_or_ascii(word):
    """태국어 등 한국 블로그와 무관한 키워드 제외"""
    return all(("가" <= ch <= "힣") or ord(ch) < 128 or ch in "·" for ch in word)


def get_google_trends(limit):
    root = ET.fromstring(fetch(TRENDS_RSS))
    items = []
    for it in root.iter("item"):
        kw = it.findtext("title", "").strip()
        if not kw or not is_korean_or_ascii(kw):
            continue
        traffic = it.findtext(f"{HT_NS}approx_traffic", "")
        news = [{
            "title": n.findtext(f"{HT_NS}news_item_title", "").strip(),
            "url": n.findtext(f"{HT_NS}news_item_url", "").strip(),
            "source": n.findtext(f"{HT_NS}news_item_source", "").strip(),
        } for n in it.findall(f"{HT_NS}news_item")][:2]
        items.append({"keyword": kw, "source": "구글트렌드", "google_traffic": traffic,
                      "google_num": parse_traffic(traffic), "news": news})
    items.sort(key=lambda x: x["google_num"], reverse=True)
    return items[:limit]


def search_news(keyword, limit=2):
    """키워드로 구글 뉴스 검색 (키 없이). 제목 끝의 ' - 언론사'를 분리"""
    root = ET.fromstring(fetch(GOOGLE_NEWS_SEARCH + urllib.parse.quote(keyword)))
    out = []
    for it in root.iter("item"):
        title = it.findtext("title", "").strip()
        src = it.findtext("source", "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"title": title, "url": it.findtext("link", "").strip(), "source": src or "구글뉴스",
                    "date": it.findtext("pubDate", "")})
        if len(out) >= limit:
            break
    return out


def get_signal_realtime(limit=10):
    """시그널(signal.bz) 네이버 실시간 검색어. state: n=신규, +=상승, -=하락, s=유지"""
    data = json.loads(fetch(SIGNAL_API, {"Referer": "https://signal.bz/"}))
    out = []
    for r in data.get("top10", [])[:limit]:
        kw = (r.get("keyword") or "").strip()
        if kw:
            out.append({"keyword": kw, "source": "네이버실시간", "naver_rank": int(r.get("rank", 0)),
                        "state": r.get("state", ""), "google_traffic": "", "google_num": 0, "news": []})
    return out


def merge_trends(google, naver):
    """구글 급상승 + 네이버 실시간 합치기 (같은 키워드는 하나로)"""
    norm = lambda s: s.replace(" ", "").lower()
    by = {norm(c["keyword"]): c for c in google}
    out = list(google)
    for n in naver:
        g = by.get(norm(n["keyword"]))
        if g:
            g["naver_rank"], g["state"], g["source"] = n["naver_rank"], n["state"], "구글+네이버"
        else:
            out.append(n)
    return out


def fill_news(cands, per=2):
    """뉴스가 비어 있는 키워드(주로 네이버 실시간)에 구글 뉴스 검색 결과 붙이기"""
    for c in cands:
        if not c["news"]:
            c["news"] = safe(search_news, c["keyword"], per, name=f"뉴스검색({c['keyword']})")


def get_google_news(limit):
    root = ET.fromstring(fetch(GOOGLE_NEWS_RSS))
    out = []
    for it in root.iter("item"):
        out.append({"title": it.findtext("title", "").strip(), "url": it.findtext("link", "").strip()})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 네이버 (키 없이)
def get_naver_ranking_news(limit, per_press=2):
    """네이버 뉴스 '언론사별 많이 본 뉴스'에서 각 언론사 상위 기사"""
    raw = fetch(NAVER_RANKING)
    m = re.search(rb'charset=["\']?([\w-]+)', raw)
    text = raw.decode(m.group(1).decode() if m else "utf-8", "ignore")
    out, seen = [], set()
    for box in re.split(r'<div class="rankingnews_box', text)[1:]:
        press = clean((re.search(r'<strong class="rankingnews_name">(.*?)</strong>', box) or [None, ""])[1])
        arts = re.findall(r'<em class="list_ranking_num">(\d+)<.*?<a href="(https://n\.news\.naver\.com/article/[^"?]+)[^"]*" '
                          r'class="list_title[^"]*"[^>]*>(.*?)</a>', box, re.S)
        for rank, url, title in arts[:per_press]:
            title = clean(title)
            if title in seen:
                continue
            seen.add(title)
            out.append({"title": title, "url": url, "press": press, "rank": int(rank)})
    out.sort(key=lambda x: x["rank"])   # 각 언론사 1위 → 2위 순
    return out[:limit]


# ---------------------------------------------------------------- 네이버 (키 필요)
def naver_search(kind, query, cfg, display=1, sort="sim"):
    nv = cfg["naver"]
    url = (f"https://openapi.naver.com/v1/search/{kind}.json?display={display}&sort={sort}&query="
           + urllib.parse.quote(query))
    return json.loads(fetch(url, {"X-Naver-Client-Id": nv["client_id"],
                                  "X-Naver-Client-Secret": nv["client_secret"]}))


def naver_ad_keywords(keyword, cfg):
    """네이버 검색광고 키워드도구: 입력 키워드 + 연관 키워드의 월간 검색수(PC/모바일)"""
    ad = cfg["naver_ad"]
    ts = str(int(time.time() * 1000))
    uri = "/keywordstool"
    sig = base64.b64encode(hmac.new(ad["secret_key"].encode(), f"{ts}.GET.{uri}".encode(),
                                    hashlib.sha256).digest()).decode()
    hint = keyword.replace(" ", "")
    url = f"https://api.searchad.naver.com{uri}?showDetail=1&hintKeywords=" + urllib.parse.quote(hint)
    data = json.loads(fetch(url, {"X-Timestamp": ts, "X-API-KEY": ad["api_key"],
                                  "X-Customer": str(ad["customer_id"]), "X-Signature": sig}))
    rows = []
    for r in data.get("keywordList", []):
        pc, mo = to_int(r.get("monthlyPcQcCnt", 0)), to_int(r.get("monthlyMobileQcCnt", 0))
        rows.append({"keyword": r["relKeyword"], "pc": pc, "mobile": mo, "total": pc + mo,
                     "is_self": r["relKeyword"].upper() == hint.upper()})
    return rows


def enrich_with_naver(cands, cfg):
    has_ad = all(cfg["naver_ad"].get(k) for k in ("api_key", "secret_key", "customer_id"))
    has_search = cfg["naver"].get("client_id") and cfg["naver"].get("client_secret")
    related_per_kw = cfg.get("related_per_keyword", 2)
    extra = []

    if has_ad:
        known = {c["keyword"].replace(" ", "").upper() for c in cands}
        for c in cands:
            try:
                rows = naver_ad_keywords(c["keyword"], cfg)
            except Exception as e:
                log(f"검색광고 API 실패({c['keyword']}): {e}")
                continue
            self_row = next((r for r in rows if r["is_self"]), None)
            if self_row:
                c.update(naver_pc=self_row["pc"], naver_mobile=self_row["mobile"], naver_total=self_row["total"])
            # 연관 키워드 중 검색량 큰 것 추가 (롱테일 글감)
            for r in sorted((r for r in rows if not r["is_self"]), key=lambda r: -r["total"]):
                if len([x for x in extra if x["parent"] == c["keyword"]]) >= related_per_kw:
                    break
                if r["keyword"].upper() in known or r["total"] < 100:
                    continue
                known.add(r["keyword"].upper())
                extra.append({"keyword": r["keyword"], "source": f"연관({c['keyword']})", "parent": c["keyword"],
                              "google_traffic": "", "google_num": 0, "news": [],
                              "naver_pc": r["pc"], "naver_mobile": r["mobile"], "naver_total": r["total"]})
            time.sleep(0.3)
        log(f"네이버 검색량 조회 완료 (연관 키워드 {len(extra)}개)")

    if has_search:
        for c in cands:
            try:
                c["blog_docs"] = naver_search("blog", c["keyword"], cfg)["total"]
                if not c["news"]:
                    items = naver_search("news", c["keyword"], cfg, display=2, sort="date").get("items", [])
                    c["news"] = [{"title": clean(i["title"]), "url": i.get("originallink") or i["link"],
                                  "source": "네이버뉴스"} for i in items]
            except Exception as e:
                log(f"네이버 검색 API 실패({c['keyword']}): {e}")
        log("네이버 블로그 문서 수 조회 완료")
    extra.sort(key=lambda r: -r["naver_total"])
    return has_ad, has_search, extra


def score(c):
    """정렬 점수: 네이버 검색량 ÷ 블로그 문서 수 (높을수록 '검색은 많고 글은 적은' 황금 키워드)"""
    vol = c.get("naver_total") or c.get("google_num", 0)
    if c.get("naver_rank"):
        # 네이버 실시간 순위에 오른 키워드는 지금 가장 뜨거우니 가산 (1위 +10만 … 10위 +1만)
        vol += 10_000 * (11 - min(c["naver_rank"], 10))
    docs = c.get("blog_docs")
    return vol / (docs + 1) if docs is not None else vol


# ---------------------------------------------------------------- 프롬프트
def fmt(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def build_prompt(cfg, cands, related, gnews, nnews, has_ad, has_search):
    now = datetime.now()
    L = [f"# 블로그 글감 브리핑 ({now:%Y-%m-%d %H시})\n"]

    L.append("## 1. 지금 뜨는 키워드 (네이버 실시간 + 구글 급상승, 화제성·검색량 순)")
    L.append("| 순위 | 키워드 | 네이버 실시간 | 구글 검색량(급상승) | 네이버 월검색(PC/모바일) | 네이버 블로그 문서 수 | 검색량÷문서수 |")
    L.append("|---|---|---|---|---|---|---|")
    for i, c in enumerate(cands, 1):
        nav = f"{fmt(c['naver_total'])} ({fmt(c['naver_pc'])}/{fmt(c['naver_mobile'])})" if "naver_total" in c else "-"
        ratio = f"{c['naver_total'] / (c['blog_docs'] + 1):.2f}" if "naver_total" in c and "blog_docs" in c else "-"
        nrank = f"{c['naver_rank']}위" if c.get("naver_rank") else "-"
        L.append(f"| {i} | {c['keyword']} | {nrank} | {c['google_traffic'] or '-'} | {nav} | "
                 f"{fmt(c.get('blog_docs'))} | {ratio} |")
    if related:
        L.append("\n### 연관 키워드 (소제목·태그용, 단독 주제 아님)")
        for r in related:
            L.append(f"- {r['keyword']} — 네이버 월 {fmt(r['naver_total'])}회 (← {r['parent']})")
    missing = []
    if not has_ad:
        missing.append("네이버 월검색량(검색광고 API 키 없음)")
    if not has_search:
        missing.append("블로그 문서 수(네이버 검색 API 키 없음)")
    if missing:
        L.append(f"\n※ 미수집: {', '.join(missing)}")

    L.append("\n### 키워드별 관련 뉴스")
    for c in cands:
        for n in c["news"]:
            L.append(f"- [{c['keyword']}] {n['title']} ({n['source']}) {n['url']}")

    L.append("\n## 2. 네이버 많이 본 뉴스 (언론사별 랭킹)")
    for i, n in enumerate(nnews, 1):
        L.append(f"{i}. {n['title']} ({n['press']} {n['rank']}위)\n   {n['url']}")

    L.append("\n## 3. 구글 뉴스 주요 기사")
    for i, n in enumerate(gnews, 1):
        L.append(f"{i}. {n['title']}\n   {n['url']}")
    briefing = "\n".join(L)

    instruction = f"""
---
## 요청 (아래를 그대로 GPT/Codex에 붙여넣으세요)

너는 네이버·구글 검색 유입에 강한 한국어 블로그 전문 작가야.
글 주제는 반드시 위 1번 표의 '지금 뜨는 키워드' 중에서 고르고, 네이버 월검색량이 많은(문서 수가 있으면 검색량÷문서수가 높은) 키워드를 우선으로,
2·3번 뉴스에서 네이버와 구글 양쪽에서 동시에 화제인 이슈를 가산점으로 고려해서
블로그 글 {cfg.get('posts_per_run', 3)}개를 작성해줘. 고른 키워드와 선택 이유(수치 근거)를 글 앞에 한 줄로 적어줘.

[블로그 정보]
- 블로그 주제/성격: {cfg.get('blog_topic', '생활 정보, 이슈 정리')}
- 문체: {cfg.get('tone', '친근한 존댓말')}

[각 글 작성 규칙]
1. SEO 제목 3개 후보 (핵심 키워드를 앞쪽에 배치, 30자 내외)
2. 본문 {cfg.get('min_chars', 1500)}자 이상, 소제목(H2/H3) 4~6개, 소제목에도 키워드/연관 키워드 자연스럽게 포함
3. 도입부 3줄 안에 독자가 얻을 정보 요약
4. 사실은 위 뉴스 링크 기준으로만 작성, 추측은 '~로 알려졌다'처럼 구분
5. 마지막에 요약 3줄 + 연관 키워드 태그 10개 (위 '연관 키워드' 목록 중 주제와 맞는 것 활용)
6. 썸네일 문구 1개, 본문 중 이미지 넣을 위치 [이미지: 설명] 표시
7. 저작권 문제 없도록 뉴스 문장을 그대로 베끼지 말고 재구성

결과는 글마다 '=== 글 N ===' 으로 구분해서 마크다운으로 출력해줘.
""".strip()
    return briefing + "\n\n" + instruction


# ---------------------------------------------------------------- 출력
def send_mail(cfg, subject, body):
    m = cfg["mail"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = m["sender"]
    msg["To"] = ", ".join(m["to"])
    with smtplib.SMTP_SSL(m.get("smtp_host", "smtp.gmail.com"), m.get("smtp_port", 465), timeout=30) as s:
        s.login(m["sender"], m["app_password"])
        s.sendmail(m["sender"], m["to"], msg.as_string())


def save_prompt(prompt):
    os.makedirs(PROMPT_DIR, exist_ok=True)
    path = os.path.join(PROMPT_DIR, f"prompt_{datetime.now():%Y%m%d_%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompt)
    # 항상 최신본을 latest.md 로도 저장 → Codex에 "prompts/latest.md 읽고 작업해" 하면 됨
    with open(os.path.join(PROMPT_DIR, "latest.md"), "w", encoding="utf-8") as f:
        f.write(prompt)
    return path


def run_codex(cfg, prompt_path):
    c = cfg.get("codex", {})
    if not c.get("auto_run"):
        return
    exe = shutil.which("codex")
    if not exe:
        log("codex CLI를 찾을 수 없어 자동 실행을 건너뜀 (npm i -g @openai/codex)")
        return
    out_dir = os.path.join(BASE_DIR, c.get("output_dir", "posts"))
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, os.path.basename(prompt_path).replace("prompt_", "post_"))
    task = f"{prompt_path} 파일의 지시대로 블로그 글을 작성해서 결과 전체를 출력해줘."
    log("codex 실행 중...")
    subprocess.run([exe, "exec", "--skip-git-repo-check", "-o", out_file, task],
                   cwd=BASE_DIR, timeout=c.get("timeout_sec", 900))
    log(f"codex 결과 저장: {out_file}")


# ---------------------------------------------------------------- main
def safe(fn, *a, default=None, name=""):
    try:
        return fn(*a)
    except Exception as e:
        log(f"{name} 수집 실패: {e!r}")
        return default if default is not None else []


# 늘 검색량이 많아 새 글로는 노출이 어려운 일반 키워드 (순위에서 제외)
GENERIC_KEYWORDS = {
    "날씨", "오늘 날씨", "내일 날씨", "주말 날씨", "미세먼지", "로또", "로또 당첨번호", "운세", "오늘의 운세",
    "네이버", "다음", "구글", "유튜브", "카카오톡", "인스타그램", "넷플릭스", "쿠팡", "지도", "번역", "파파고",
    "메일", "뉴스", "시간", "환율", "주식", "코스피", "비트코인", "TV", "티비", "영화", "드라마", "게임",
    "노래", "음악", "사전", "계산기", "달력", "쇼핑", "택배", "배달", "맛집", "부동산",
}


def drop_generic(cands, related, cfg):
    """너무 흔한 키워드(목록에 있거나 네이버 월 검색량이 상한 초과)를 순위에서 뺀다."""
    max_monthly = cfg.get("max_naver_monthly", 1_000_000)
    extra = set(cfg.get("exclude_keywords", []))

    def generic(c):
        kw = c["keyword"].strip()
        return (kw in GENERIC_KEYWORDS or kw in extra
                or (c.get("naver_total") or 0) > max_monthly)

    kept = [c for c in cands if not generic(c)]
    dropped = [c["keyword"] for c in cands if generic(c)]
    if dropped:
        log(f"흔한 키워드 제외: {', '.join(dropped)}")
    return kept, [r for r in related if not generic(r)]


def main():
    dry = "--dry-run" in sys.argv
    cfg = load_config()

    cands = safe(get_google_trends, cfg.get("trend_count", 15), name="구글 트렌드")
    log(f"구글 트렌드 키워드 {len(cands)}개")
    signal = safe(get_signal_realtime, 10, name="시그널(네이버 실시간)")
    log(f"네이버 실시간 검색어 {len(signal)}개")
    cands = merge_trends(cands, signal)
    gnews = safe(get_google_news, cfg.get("news_count", 10), name="구글 뉴스")
    log(f"구글 뉴스 {len(gnews)}개")
    nnews = safe(get_naver_ranking_news, cfg.get("naver_news_count", 15), name="네이버 뉴스")
    log(f"네이버 많이 본 뉴스 {len(nnews)}개")

    has_ad, has_search, related = enrich_with_naver(cands, cfg)
    cands, related = drop_generic(cands, related, cfg)
    fill_news(cands)
    cands.sort(key=score, reverse=True)

    prompt = build_prompt(cfg, cands, related, gnews, nnews, has_ad, has_search)
    path = save_prompt(prompt)
    log(f"프롬프트 저장: {path}")

    if dry:
        print("\n" + prompt)
        return

    subject = f"[블로그 글감] {datetime.now():%m/%d %H시} 인기 키워드 & 핫이슈"
    send_mail(cfg, subject, prompt)
    log(f"메일 발송 완료 → {', '.join(cfg['mail']['to'])}")

    run_codex(cfg, path)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"오류: {e!r}")
        raise
