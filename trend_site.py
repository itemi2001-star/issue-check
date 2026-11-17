# -*- coding: utf-8 -*-
"""
실시간 이슈 체크 사이트 생성기
- 네이버 실시간 검색어(시그널 signal.bz) + 구글 트렌드(대한민국) 급상승 검색어를 모아
  한국 시간 1시간 단위로 기록하고, 검색어마다 관련 뉴스(왜 떴는지)를 붙여
  정적 웹페이지 site/index.html 하나로 만들어 줍니다. (외부 라이브러리 없음)

사용법
  python trend_site.py            한 번 수집 → site/index.html 갱신
  python trend_site.py --loop     10분마다 수집 + http://localhost:8000 으로 사이트 띄우기
                                  (창을 닫거나 Ctrl+C 하면 멈춤)
"""
import http.server
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone

import blog_auto as b

KST = timezone(timedelta(hours=9))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE, "data", "trend_history.json")
SITE_DIR = os.path.join(BASE, "site")
KEEP_DAYS = 30            # 기록 보관 일수
BRIEF_REFRESH_MIN = 60    # 같은 검색어 뉴스는 60분마다만 다시 검색
SITE_TITLE = "실시간 이슈 체크"


def now_kst():
    return datetime.now(KST)


def load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"days": {}, "briefs": {}}


def save(db):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, DATA_PATH)


def collect(db):
    t = now_kst()
    naver = b.safe(b.get_signal_realtime, 10, name="네이버 실시간")
    google = b.safe(b.get_google_trends, 10, name="구글 트렌드")
    if not naver and not google:
        b.log("수집된 검색어가 없어 이번 회차는 건너뜀")
        return False

    slot = {
        "t": t.strftime("%H:%M"),
        "naver": [{"rank": n["naver_rank"], "keyword": n["keyword"]} for n in naver],
        "google": [{"rank": i, "keyword": g["keyword"], "traffic": g["google_traffic"],
                    "news": g["news"]} for i, g in enumerate(google, 1)],
    }
    db["days"].setdefault(t.strftime("%Y-%m-%d"), {})[t.strftime("%H")] = slot

    # 검색어별 이슈 브리핑(관련 뉴스) 갱신
    briefs = db["briefs"]
    for src, items in (("naver", slot["naver"]), ("google", slot["google"])):
        for it in items:
            kw = it["keyword"]
            br = briefs.setdefault(kw, {"first": t.isoformat(timespec="minutes")})
            br[f"best_{src}"] = min(br.get(f"best_{src}", 99), it["rank"])
            br["last"] = t.isoformat(timespec="minutes")
            fresh = br.get("updated") and (t - datetime.fromisoformat(br["updated"])).total_seconds() < BRIEF_REFRESH_MIN * 60
            if fresh and br.get("news"):
                continue
            news = b.safe(b.search_news, kw, 3, name=f"뉴스검색({kw})") or it.get("news") or []
            if news:
                br["news"] = [{"title": n["title"], "url": n["url"], "source": n.get("source", "")} for n in news[:3]]
                br["updated"] = t.isoformat(timespec="minutes")

    # 오래된 기록 정리
    cutoff = (t - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for d in [d for d in db["days"] if d < cutoff]:
        del db["days"][d]
    alive = {it["keyword"] for day in db["days"].values() for s in day.values()
             for it in s["naver"] + s["google"]}
    for kw in [k for k in briefs if k not in alive]:
        del briefs[kw]
    b.log(f"수집 완료: 네이버 {len(naver)}개, 구글 {len(google)}개 ({slot['t']})")
    return True


def render(db):
    os.makedirs(SITE_DIR, exist_ok=True)
    data = json.dumps({"days": db["days"], "briefs": db["briefs"],
                       "generated": now_kst().isoformat(timespec="seconds")}, ensure_ascii=False)
    # 페이지가 1분마다 이 파일만 다시 받아서 새로고침 없이 화면을 바꿈
    with open(os.path.join(SITE_DIR, "data.json"), "w", encoding="utf-8") as f:
        f.write(data)
    data = data.replace("</", "<\\/")   # <script> 안에 안전하게 넣기
    page = HTML.replace("__TITLE__", SITE_TITLE).replace("__DATA__", data)
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def run_once():
    db = load()
    if collect(db):
        save(db)
    render(db)
    return os.path.join(SITE_DIR, "index.html")


def serve(port=8000):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=SITE_DIR, **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://localhost:{port}/"


def main():
    if "--loop" in sys.argv:
        run_once()
        url = serve()
        b.log(f"사이트 실행 중: {url}  (10분마다 갱신, 멈추려면 Ctrl+C)")
        webbrowser.open(url)
        while True:
            time.sleep(600)
            try:
                run_once()
            except Exception as e:
                b.log(f"갱신 실패: {e!r}")
    else:
        b.log(f"사이트 생성: {run_once()}")


HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ — 지금 뜨는 검색어와 이유</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--sub:#6b7280;--line:#e5e7eb;--accent:#2f6fed;
--naver:#03a94f;--google:#4285f4;--up:#e5484d;--down:#3b82f6;--new:#f59e0b;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e8eaed;--sub:#9aa0a6;
--line:#2a2f3a;--accent:#7aa2ff;--chip:#1f2533}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif;line-height:1.5}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
header h1{margin:0;font-size:26px}header p{margin:6px 0 0;color:var(--sub);font-size:14px}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--accent);margin-right:6px}
.stats{display:flex;gap:10px;margin:16px 0;flex-wrap:wrap}.stat{flex:1;min-width:140px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat b{font-size:22px;display:block}.stat span{color:var(--sub);font-size:13px}
.nav{display:flex;align-items:center;gap:8px;margin:18px 0 8px}.nav button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:4px 10px;cursor:pointer}
.nav .date{font-weight:700}
.hours{display:flex;gap:6px;overflow-x:auto;padding-bottom:6px}.hours button{flex:none;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:999px;padding:4px 10px;cursor:pointer;font-size:13px}
.hours button.on{background:var(--accent);color:#fff;border-color:var(--accent)}.hours button:disabled{opacity:.35;cursor:default}
.hero{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:14px 0}
.hero .k{font-size:22px;font-weight:800;margin:4px 0 8px}.hero li{margin:4px 0;font-size:14px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}@media(max-width:760px){.grid{grid-template-columns:minmax(0,1fr)}}
.wrap>*{min-width:0}.hours{max-width:100%}.briefs{grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))!important}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.card h2{font-size:16px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.dot{width:22px;height:22px;border-radius:6px;color:#fff;font-size:12px;font-weight:800;display:inline-flex;align-items:center;justify-content:center}
.card .when{color:var(--sub);font-size:12px;margin-bottom:6px}
ol{list-style:none;margin:0;padding:0}ol li{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid var(--line);cursor:pointer}
ol li:first-child{border-top:0}.rk{width:22px;font-weight:800;color:var(--accent);flex:none;text-align:right}
.kw{flex:1;min-width:0}.kw b{display:block}.kw small{color:var(--sub);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chg{flex:none;font-size:12px;font-weight:700;min-width:34px;text-align:right}.chg.new{color:var(--new)}.chg.up{color:var(--up)}.chg.down{color:var(--down)}
.src{color:var(--sub);font-size:12px;margin-top:8px}
h3.sec{margin:28px 0 4px;font-size:18px}.secsub{color:var(--sub);font-size:13px;margin:0 0 10px}
.briefs{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.brief{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.brief .tag{font-weight:800;color:var(--accent)}.brief .h{font-weight:700;margin:6px 0}.brief ul{margin:6px 0 0;padding-left:18px;font-size:13px}
.brief .meta{color:var(--sub);font-size:12px;margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}
.long li{cursor:default}footer{color:var(--sub);font-size:12px;margin-top:36px;text-align:center}
.hl{outline:2px solid var(--accent)}
.live{color:#e5484d;background:rgba(229,72,77,.12)}.live i{display:inline-block;width:7px;height:7px;border-radius:50%;background:#e5484d;margin-right:5px;vertical-align:1px;animation:pulse 1.6s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
.hours button[title]{position:relative}.flash{animation:flash 1.2s}@keyframes flash{from{background:rgba(47,111,237,.18)}to{background:transparent}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <span class="badge live"><i></i>실시간</span><span class="badge">10분마다 갱신</span><span class="badge">네이버 · 구글</span>
  <h1>__TITLE__</h1>
  <p>네이버 실시간 검색어(시그널)와 구글 트렌드 급상승 검색어를 1시간 단위로 기록하고, 검색어마다 관련 뉴스를 붙였어요.</p>
</header>
<div class="stats">
  <div class="stat"><b id="sKw">-</b><span>오늘 순위에 오른 검색어</span></div>
  <div class="stat"><b id="sBr">-</b><span>오늘 정리한 이슈</span></div>
  <div class="stat"><b id="sDays">-</b><span>기록한 날</span></div>
</div>
<div class="nav"><button id="prev">‹</button><span class="date" id="date"></span><button id="next">›</button>
  <span style="color:var(--sub);font-size:13px" id="upd"></span></div>
<div class="hours" id="hours"></div>
<div class="hero" id="hero"></div>
<div class="grid">
  <div class="card"><h2><span class="dot" style="background:var(--naver)">N</span>네이버 실시간 검색어</h2><div class="when" id="nWhen"></div><ol id="nList"></ol><div class="src">출처: 시그널(signal.bz)</div></div>
  <div class="card"><h2><span class="dot" style="background:var(--google)">G</span>구글 급상승 검색어</h2><div class="when" id="gWhen"></div><ol id="gList"></ol><div class="src">출처: Google 트렌드(대한민국)</div></div>
</div>
<h3 class="sec">지금 뜨는 이슈 브리핑</h3><p class="secsub">검색어를 누르면 해당 이슈로 이동해요. 관련 기사 제목으로 왜 떴는지 정리했어요.</p>
<div class="briefs" id="briefs"></div>
<h3 class="sec">오늘 가장 오래 머문 검색어</h3><p class="secsub">순위에 오른 시간과 최고 순위를 합산했어요.</p>
<div class="card"><ol class="long" id="long"></ol></div>
<footer id="foot"></footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
let D = JSON.parse(document.getElementById('data').textContent);
let days = Object.keys(D.days).sort();
const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => { const e = document.createElement(tag); Object.assign(e, props); kids.forEach(k => e.append(k)); return e; };
let day = days[days.length - 1], hour = null;
let follow = true;   // 최신 시간대를 보고 있으면 새 데이터가 오면 자동으로 따라감
const latestHour = d => { const hs = Object.keys(D.days[d] || {}).sort(); return hs[hs.length - 1]; };
function ago() {
  const s = Math.max(0, Math.round((Date.now() - new Date(D.generated).getTime()) / 1000));
  const t = s < 60 ? '방금 전' : s < 3600 ? Math.floor(s / 60) + '분 전' : Math.floor(s / 3600) + '시간 전';
  $('upd').textContent = '· 업데이트 ' + D.generated.slice(11, 16) + ' (' + t + ')';
}
async function poll() {
  try {
    const n = await (await fetch('data.json?t=' + Date.now(), {cache: 'no-store'})).json();
    if (n.generated === D.generated) return;
    D = n; days = Object.keys(D.days).sort();
    if (follow) { day = days[days.length - 1]; hour = null; }
    render();
    document.querySelectorAll('.card,.hero').forEach(c => { c.classList.remove('flash'); void c.offsetWidth; c.classList.add('flash'); });
  } catch (e) { /* 파일로 직접 열었을 때 등은 무시 */ }
}

function prevSlot(d, h) {
  const hs = Object.keys(D.days[d] || {}).sort().filter(x => x < h);
  if (hs.length) return D.days[d][hs[hs.length - 1]];
  const i = days.indexOf(d);
  if (i > 0) { const pd = D.days[days[i - 1]], ph = Object.keys(pd).sort(); return pd[ph[ph.length - 1]]; }
  return null;
}
function change(kw, rank, prev, key) {
  if (!prev) return el('span', {className: 'chg'});
  const p = prev[key].find(x => x.keyword === kw);
  if (!p) return el('span', {className: 'chg new', textContent: 'NEW'});
  const d = p.rank - rank;
  if (d > 0) return el('span', {className: 'chg up', textContent: '▲' + d});
  if (d < 0) return el('span', {className: 'chg down', textContent: '▼' + (-d)});
  return el('span', {className: 'chg', textContent: '-'});
}
function headline(kw) { const b = D.briefs[kw]; return b && b.news && b.news[0] ? b.news[0].title : ''; }
function list(id, items, prev, key, extra) {
  const ol = $(id); ol.replaceChildren();
  if (!items.length) { ol.append(el('li', {textContent: '이 시간대 기록이 없어요.'})); return; }
  items.forEach(it => {
    const kw = el('div', {className: 'kw'}, el('b', {textContent: it.keyword}), el('small', {textContent: extra(it) || headline(it.keyword)}));
    const li = el('li', {}, el('span', {className: 'rk', textContent: it.rank}), kw, change(it.keyword, it.rank, prev, key));
    li.onclick = () => { const t = document.querySelector(`[data-kw="${CSS.escape(it.keyword)}"]`); if (t) { t.scrollIntoView({behavior: 'smooth', block: 'center'}); t.classList.add('hl'); setTimeout(() => t.classList.remove('hl'), 1500); } };
    ol.append(li);
  });
}
function briefCard(kw) {
  const b = D.briefs[kw]; if (!b) return null;
  const card = el('div', {className: 'brief'}); card.dataset.kw = kw;
  card.append(el('div', {className: 'tag', textContent: '#' + kw}));
  if (b.news && b.news.length) {
    card.append(el('div', {className: 'h', textContent: b.news[0].title}));
    const ul = el('ul');
    b.news.forEach(n => ul.append(el('li', {}, el('a', {href: n.url, target: '_blank', rel: 'noopener', textContent: n.title + (n.source ? ' · ' + n.source : '')}))));
    card.append(ul);
  } else card.append(el('div', {className: 'h', textContent: '관련 기사를 찾는 중이에요.'}));
  const meta = el('div', {className: 'meta'});
  if (b.best_naver) meta.append(el('span', {textContent: '네이버 최고 ' + b.best_naver + '위'}));
  if (b.best_google) meta.append(el('span', {textContent: '구글 최고 ' + b.best_google + '위'}));
  if (b.updated) meta.append(el('span', {textContent: b.updated.slice(11, 16) + ' 기사 확인'}));
  card.append(meta); return card;
}
function render() {
  const slots = D.days[day] || {}, hs = Object.keys(slots).sort();
  if (hour === null || !slots[hour]) hour = hs[hs.length - 1];
  $('date').textContent = day.replace(/-/g, '. ') + ' (' + '일월화수목금토'[new Date(day + 'T00:00:00+09:00').getDay()] + ')';
  $('prev').disabled = days.indexOf(day) <= 0; $('next').disabled = days.indexOf(day) >= days.length - 1;
  const hb = $('hours'); hb.replaceChildren();
  for (let i = 0; i < 24; i++) { const h = String(i).padStart(2, '0');
    const bt = el('button', {textContent: i + '시', disabled: !slots[h], className: h === hour ? 'on' : '',
      title: slots[h] ? '' : '이 시간대는 기록이 없어요'});
    bt.onclick = () => { hour = h; follow = day === days[days.length - 1] && h === latestHour(day); render(); }; hb.append(bt); }
  const s = slots[hour] || {naver: [], google: [], t: ''}, prev = prevSlot(day, hour);
  $('nWhen').textContent = $('gWhen').textContent = `${day} ${hour}시 기록 (마지막 확인 ${s.t})`;
  list('nList', s.naver, prev, 'naver', () => '');
  list('gList', s.google, prev, 'google', it => (it.traffic ? '검색량 ' + it.traffic + ' · ' : '') + headline(it.keyword));
  // 1위 이슈
  const top = s.naver[0] || s.google[0], hero = $('hero'); hero.replaceChildren();
  if (top) { const b = D.briefs[top.keyword] || {};
    hero.append(el('span', {className: 'badge', textContent: '#1 지금 1위 · ' + (s.naver[0] ? '네이버 실시간' : '구글')}), el('div', {className: 'k', textContent: top.keyword}));
    const ul = el('ul'); (b.news || []).forEach(n => ul.append(el('li', {}, el('a', {href: n.url, target: '_blank', rel: 'noopener', textContent: n.title + (n.source ? ' · ' + n.source : '')}))));
    hero.append(ul); }
  // 브리핑: 이 시간대 검색어 전부
  const seen = new Set(), br = $('briefs'); br.replaceChildren();
  [...s.naver, ...s.google].forEach(it => { if (seen.has(it.keyword)) return; seen.add(it.keyword); const c = briefCard(it.keyword); if (c) br.append(c); });
  // 오늘 오래 머문 검색어
  const agg = {};
  Object.values(slots).forEach(sl => [...sl.naver, ...sl.google].forEach(it => {
    const a = agg[it.keyword] || (agg[it.keyword] = {hours: new Set(), best: 99}); a.hours.add(sl.t.slice(0, 2)); a.best = Math.min(a.best, it.rank); }));
  const ranked = Object.entries(agg).map(([k, a]) => [k, a.hours.size, a.best]).sort((x, y) => y[1] - x[1] || x[2] - y[2]).slice(0, 10);
  const lo = $('long'); lo.replaceChildren();
  ranked.forEach(([k, h, best], i) => lo.append(el('li', {}, el('span', {className: 'rk', textContent: i + 1}), el('div', {className: 'kw'}, el('b', {textContent: k}), el('small', {textContent: `${h}시간 · 최고 ${best}위`})))));
  $('sKw').textContent = Object.keys(agg).length;
  $('sBr').textContent = Object.keys(agg).filter(k => D.briefs[k] && D.briefs[k].news).length;
  $('sDays').textContent = days.length + '일';
  ago();
  $('foot').textContent = '순위 출처: 시그널(네이버 앱 기준), Google 트렌드 · 기사 저작권은 각 언론사에 있어요.';
}
$('prev').onclick = () => { day = days[days.indexOf(day) - 1]; hour = null; follow = false; render(); };
$('next').onclick = () => { day = days[days.indexOf(day) + 1]; hour = null; follow = day === days[days.length - 1]; render(); };
if (days.length) render(); else $('hero').textContent = '아직 수집된 기록이 없어요. python trend_site.py 를 실행해 주세요.';
setInterval(poll, 60 * 1000);   // 1분마다 새 순위가 있는지 확인
setInterval(ago, 20 * 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
