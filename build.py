"""매일 아침 뉴스 요약 정적 페이지 생성. 표준 라이브러리만 사용 (Gemini 호출용 urllib 제외)."""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
LAT, LON = 37.0078, 127.2797  # 안성
# 기본 48시간. 다만 월요일엔 주말에 있었던 일까지 거슬러 올라간다.
MAX_AGE_HOURS = 88 if datetime.now(KST).weekday() == 0 else 48

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              f"gemini-2.0-flash:generateContent?key={GEMINI_KEY}")
YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY")

WEATHER_CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    56: "어는 이슬비", 57: "강한 어는 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "어는 비", 67: "강한 어는 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    85: "약한 눈소나기", 86: "강한 눈소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "강한 우박 동반 뇌우",
}

WEATHER_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌧️", 56: "🌧️", 57: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️", 77: "❄️",
    80: "🌦️", 81: "🌧️", 82: "⛈️", 85: "🌨️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}

GN_SOLAR = ("https://news.google.com/rss/search?"
            "q=%ED%83%9C%EC%96%91%EA%B4%91+OR+%EC%9E%AC%EC%83%9D%EC%97%90%EB%84%88%EC%A7%80"
            "&hl=ko&gl=KR&ceid=KR:ko")

# 대분류는 날씨 / 기사 / 태양광 셋뿐이다. 기사 안에서는 소제목 없이 한 덩어리로 보여준다.
# (묶음, RSS 주소, 최대 개수, 출처명(None이면 제목의 "- 언론사"에서 추출),
#  원문이 한국어인가, 쓸모없는 기사 AI 필터를 적용할 것인가)
# 원문이 한국어가 아닌 소스는 AI 요약(번역)에 성공한 기사만 싣는다 — 번역 없이 영어 제목을 보여주지 않는다.
# 태양광은 업무용이라 폭넓게 보려고 "실용성" 필터를 걸지 않는다.
FEEDS = [
    ("기사", "https://www.yna.co.kr/rss/news.xml", 5, "연합뉴스", True, True),
    ("기사", "https://www.yna.co.kr/rss/international.xml", 3, "연합뉴스", True, True),
    ("기사", "https://www.yna.co.kr/rss/market.xml", 3, "연합뉴스", True, True),
    ("기사", "https://www.hellodd.com/rss/allArticle.xml", 2, "대덕넷", True, True),
    ("기사", "https://www.sciencedaily.com/rss/strange_offbeat.xml", 2, "ScienceDaily", False, False),
    ("태양광", GN_SOLAR, 6, None, True, False),
]

GROUPS = [("📰", "기사"), ("☀️", "태양광")]

# 제목만 봐도 읽을 가치가 없는 기사를 거른다. AI 키가 없어도 이 목록은 항상 동작한다.
BLOCK = [
    # 자극적이기만 하고 실익 없는 사건사고
    "학대", "성폭행", "성추행", "성범죄", "몰카", "불법촬영", "강간",
    "살해", "살인", "시신", "흉기", "칼부림", "음주운전", "만취",
    "투신", "자살", "극단적 선택", "몹쓸 짓", "엽기",
    # 사실 전달이 아닌 의견성 글
    "칼럼", "사설", "기고", "오피니언", "기자수첩", "취재후기",
    # 부고·인사·동정 등 나와 무관한 사람 소식
    "[부고]", "별세", "부친상", "모친상", "장인상", "장모상", "빙부상", "빙모상",
    "[인사]", "[동정]", "[신간]", "[게시판]", "[알림]", "[프로필]",
    # 사진·영상만 있는 기사, 스포츠 점수 단신
    "[포토]", "[영상]", "[사진]", "전적]",
    # 지역별로 쏟아지는 기상특보 해제 알림 (날씨는 상단 예보 카드로 충분)
    "주의보 해제", "경보 해제",
]

# 제목은 화면에 그대로 보여주고 그 아래에 이 요약을 붙인다. 그래서 제목 반복이 특히 치명적이다.
SUMMARY_PROMPT = """너는 뉴스 브리핑 편집자다. 아래 기사의 핵심을 한국어 한 문장으로 요약해라.

이 요약문은 화면에서 제목 바로 아래에 붙는다. 따라서:
- 제목에 이미 있는 말을 되풀이하지 마라. 제목만 봐서는 알 수 없는
  핵심 사실(숫자, 이유, 결과, 다음 절차)을 채워 넣어라.
- 기사에 없는 내용은 절대 쓰지 마라 (추측 금지).
- 한 문장, 최대 60자 정도로 짧게. 문장 끝에 마침표를 찍어라.
- 종부세·양도세 같은 전문용어는 그대로 쓰지 말고 평소 쓰는 쉬운 말로 풀어써라.

제목: {title}
본문: {body}"""

# 요약 전에 "읽을 가치가 있는 기사인가"부터 판단시킨다. 판단 기준은 주관적 취향이 아니라
# 기사 유형이다 — 취향으로 거르게 하면 멀쩡한 뉴스까지 잘려나간다.
FILTER_RULE = """
먼저 이 기사가 읽을 가치가 있는지 판단해라. 아래 중 하나라도 해당하면
요약하지 말고 오직 SKIP 이라는 단어 하나만 출력해라.
- 부고, 인사이동, 동정, 수상·임명 소식 등 특정 개인의 신상 소식
- 기업·기관의 홍보성 보도자료, 협약 체결, 캠페인·행사 안내
- 특정 개인이나 소수만 관계있는 소송·재판의 절차적 진행 상황
- 배경 설명 없이 사실 한 줄만 전하는 단신, 스포츠 경기 결과
- 읽어도 일상의 판단이나 행동이 전혀 달라지지 않는 내용
"""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def get_weather():
    """어제부터 7일 뒤까지 총 8일치 예보."""
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%2FSeoul&past_days=1&forecast_days=7")
    d = json.loads(fetch(url))["daily"]
    today = datetime.now(KST).date()
    days = []
    for i, iso in enumerate(d["time"]):
        date = datetime.strptime(iso, "%Y-%m-%d").date()
        delta = (date - today).days
        label = {-1: "어제", 0: "오늘", 1: "내일"}.get(delta, "")
        weekday = "월화수목금토일"[date.weekday()]
        code = d["weathercode"][i]
        days.append({
            "date": f"{date.month}/{date.day}({weekday})",
            "label": label,
            "today": delta == 0,
            "desc": WEATHER_CODES.get(code, "-"),
            "emoji": WEATHER_EMOJI.get(code, "🌡️"),
            "tmax": round(d["temperature_2m_max"][i]),
            "tmin": round(d["temperature_2m_min"][i]),
            "rain": d["precipitation_probability_max"][i],
        })
    return days


def yt_api(endpoint, **params):
    params["key"] = YOUTUBE_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(params)}"
    return json.loads(fetch(url))


def get_youtube(limit, seen):
    """최근 48시간 안에 올라온 한국 뉴스 영상을 조회수 높은 순으로."""
    if not YOUTUBE_KEY:
        return []
    after = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = yt_api("search", part="snippet", type="video", order="viewCount",
                   publishedAfter=after, regionCode="KR", relevanceLanguage="ko",
                   videoCategoryId="25", maxResults=limit + 5)  # 25 = 뉴스/정치
    ids = [it["id"]["videoId"] for it in found.get("items", [])]
    if not ids:
        return []
    stats = yt_api("videos", part="snippet,statistics", id=",".join(ids))
    by_views = sorted(stats.get("items", []),
                       key=lambda v: int(v["statistics"].get("viewCount", 0)), reverse=True)
    videos = []
    for v in by_views:
        title = v["snippet"]["title"].strip()
        if any(w in title for w in BLOCK) or is_dup(title, seen):
            continue
        videos.append({
            "title": title,
            "link": f"https://www.youtube.com/watch?v={v['id']}",
            "source": v["snippet"]["channelTitle"],
            "body": v["snippet"].get("description", ""),
        })
        if len(videos) >= limit:
            break
    return videos


def bigrams(text):
    s = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def is_dup(title, seen):
    """제목 글자 2개씩 겹치는 비율로 중복 판단. 한국어 조사 변형에 강하다."""
    b = bigrams(title)
    if not b:
        return True
    for other in seen:
        if len(b & other) / min(len(b), len(other)) >= 0.45:
            return True
    seen.append(b)
    return False


def parse_pubdate(text):
    """RFC822 형식(대부분 RSS)과 'YYYY-MM-DD HH:MM:SS'(일부 언론사, KST 가정) 둘 다 지원."""
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def get_items(url, limit, seen, source_label):
    root = ET.fromstring(fetch(url))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    items = []
    for item in root.iter("item"):
        title = re.sub(r"<[^>]+>", " ", item.findtext("title") or "").strip()
        title = re.sub(r"\s+", " ", title)
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        dt = parse_pubdate(item.findtext("pubDate"))
        if dt is None or dt < cutoff:
            continue
        if any(w in title for w in BLOCK) or is_dup(title, seen):
            continue
        if source_label is None:
            # 구글 뉴스 제목은 "제목 - 언론사" 형식
            title, _, source = title.rpartition(" - ")
            title = title or source
        else:
            source = source_label
        items.append((title, link, source))
        if len(items) >= limit:
            break
    return items


class _ArticleExtractor(HTMLParser):
    """본문 컨테이너를 우선 읽고, 없으면 <p> 태그를 모은다.

    <p>만 긁으면 언론사에 따라 본문 대신 댓글·광고 문구가 잡힌다.
    국내 언론사가 널리 쓰는 CMS는 본문을 아래 표시로 감싸므로 그걸 먼저 찾는다.
    """
    BODY_MARKS = {("itemprop", "articlebody"), ("id", "article-view-content-div")}
    NOISE_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.in_p = 0
        self.p_parts = []
        self.body_tag = None
        self.body_depth = 0
        self.body_parts = []
        self.noise = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.NOISE_TAGS:
            self.noise += 1
            return
        if self.body_tag is None:
            a = {k.lower(): (v or "").lower() for k, v in attrs}
            if any(a.get(k) == v for k, v in self.BODY_MARKS):
                self.body_tag, self.body_depth = tag, 1
        elif tag == self.body_tag and self.body_depth:
            self.body_depth += 1  # 같은 이름의 태그가 안에 또 있으면 깊이를 센다
        if tag == "p":
            self.in_p += 1

    def handle_endtag(self, tag):
        if tag in self.NOISE_TAGS and self.noise:
            self.noise -= 1
            return
        if tag == self.body_tag and self.body_depth:
            self.body_depth -= 1
        if tag == "p" and self.in_p:
            self.in_p -= 1

    def handle_data(self, data):
        if self.noise:
            return
        if self.body_depth:
            self.body_parts.append(data)
        elif self.in_p:
            self.p_parts.append(data)


def extract_article_text(url, max_chars=3000):
    p = _ArticleExtractor()
    p.feed(fetch(url).decode("utf-8", errors="ignore"))
    clean = lambda parts: re.sub(r"\s+", " ", "".join(parts)).strip()
    body = clean(p.body_parts)
    return (body if len(body) >= 100 else clean(p.p_parts))[:max_chars]


def call_gemini(prompt):
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


SKIP = object()  # AI가 "읽을 가치 없음"으로 판단한 기사


def get_summary(title, link, strict, body=None):
    """기사 본문을 읽어 한 줄 요약. body를 넘기면 그걸 쓰고, 아니면 링크에서 받아온다.
    반환값: 요약문 / None(요약 실패, 제목만 표시) / SKIP(실을 가치 없는 기사).
    """
    if not GEMINI_KEY:
        return None
    try:
        if body is None:
            body = extract_article_text(link)
        if len(body) < 100:  # 본문을 못 가져온 경우 (예: 구글 뉴스 중계 페이지)
            return None
        prompt = SUMMARY_PROMPT.format(title=title, body=body)
        if strict:
            prompt = FILTER_RULE + "\n" + prompt
        out = call_gemini(prompt)
        return SKIP if out.strip().upper().startswith("SKIP") else out
    except Exception:
        return None


def weather_card_html(d):
    label = f'<span>{d["label"]}</span>' if d["label"] else ""
    cls = "wday today" if d["today"] else "wday"
    return (f'<div class="{cls}">'
            f'<div class="wd-date">{d["date"]}{label}</div>'
            f'<div class="wd-emoji">{d["emoji"]}</div>'
            f'<div class="wd-desc">{escape(d["desc"])}</div>'
            f'<div class="wd-temp"><b>{d["tmax"]}°</b> / {d["tmin"]}°</div>'
            f'<div class="wd-rain">💧{d["rain"]}%</div>'
            f'</div>')


def build_html():
    now = datetime.now(KST)
    date_str = f"{now.year}년 {now.month}월 {now.day}일 ({'월화수목금토일'[now.weekday()]})"

    weather_days = None
    try:
        weather_days = get_weather()
        cards = "".join(weather_card_html(d) for d in weather_days)
        weather_html = f'<div class="wstrip">{cards}</div>'
    except Exception as e:
        weather_html = f"<p>날씨 정보를 가져오지 못했습니다. ({escape(str(e))})</p>"

    seen = []
    top_headline = None

    def render(title, link, source, summary):
        nonlocal top_headline
        if top_headline is None:
            top_headline = summary or title
        sum_html = f'<p class="sum">{escape(summary)}</p>' if summary else ""
        return (f'<li><a href="{escape(link)}" target="_blank" rel="noopener">{escape(title)}</a>'
                f'{sum_html}<span class="src">{escape(source)}</span></li>')

    # 조회수 높은 유튜브 뉴스 영상을 기사 묶음 맨 앞에 놓는다 — 다른 기사와 똑같은 모양으로 낸다.
    # (조회수 자체는 get_youtube가 이미 정렬 기준으로만 쓰고, 화면엔 안 보여준다)
    lists = {name: [] for _, name in GROUPS}
    try:
        for v in get_youtube(4, seen):
            summary = get_summary(v["title"], v["link"], strict=True, body=v["body"])
            if GEMINI_KEY:
                time.sleep(1.5)
            if summary is SKIP:
                continue
            lists["기사"].append(render(v["title"], v["link"], v["source"], summary))
    except Exception:
        pass  # 유튜브가 막혀도 글 기사는 정상적으로 나와야 한다

    for group, url, limit, source_label, is_korean, strict in FEEDS:
        try:
            # 걸러낼 기사를 감안해 넉넉히 받아온 뒤, 쓸 만한 게 limit개 모이면 멈춘다.
            items = get_items(url, limit + 5, seen, source_label)
        except Exception:
            items = []
        kept = 0
        for t, link, src in items:
            if kept >= limit:
                break
            summary = get_summary(t, link, strict)
            if GEMINI_KEY:
                time.sleep(1.5)  # 무료 API 요청 한도 보호
            if summary is SKIP:
                continue  # AI가 읽을 가치 없다고 판단한 기사
            if not is_korean and not summary:
                continue  # 번역(요약)에 실패한 외국어 기사는 영어 제목 그대로 노출하지 않는다
            lists[group].append(render(t, link, src, summary))
            kept += 1

    sections = [
        f'<section class="card"><h2><span class="ic">{icon}</span>{escape(name)}</h2>'
        f'<ul>{"".join(lists[name])}</ul></section>'
        for icon, name in GROUPS if lists[name]  # 실을 게 없으면 통째로 띄우지 않는다
    ]

    today_weather = next((d for d in (weather_days or []) if d["today"]), None)
    if today_weather:
        notify_weather = f"오늘 안성 {today_weather['desc']}, {today_weather['tmin']}~{today_weather['tmax']}도, 강수 {today_weather['rain']}%"
    else:
        notify_weather = "날씨 정보 없음"
    with open("notify.txt", "w", encoding="utf-8") as f:
        f.write(notify_weather + "\n")
        f.write((top_headline or "오늘의 뉴스를 확인하세요") + "\n")

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>오늘의 뉴스 요약 - {date_str}</title>
<link rel="manifest" href="manifest.json">
<link rel="icon" href="icon.svg">
<meta name="theme-color" content="#4f7cff">
<style>
  :root {{
    --bg: #f4f6fb; --card: #ffffff; --text: #1a1a1a; --sub: #6b7280;
    --border: #ecedf2; --accent: #4f7cff; --accent2: #6ba3ff; --shadow: 0 1px 3px rgba(20,20,40,.06);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #121317; --card: #1c1e24; --text: #eceef2; --sub: #9199a8;
      --border: #2a2d35; --accent: #7aa2ff; --accent2: #5c86e6; --shadow: 0 1px 4px rgba(0,0,0,.4);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 640px;
         margin: 0 auto; padding: 16px; line-height: 1.5; color: var(--text); background: var(--bg); }}
  h1 {{ font-size: 1.3em; margin: 6px 2px 2px; }}
  .date {{ margin: 0 2px 16px; color: var(--sub); font-size: .92em; }}
  .card {{ background: var(--card); border-radius: 16px; padding: 14px 16px; margin-bottom: 12px;
          box-shadow: var(--shadow); }}
  h2 {{ font-size: 1.02em; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }}
  .ic {{ font-size: 1.15em; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  li {{ padding: 10px 0; border-top: 1px solid var(--border); }}
  li:first-child {{ border-top: none; padding-top: 0; }}
  a {{ color: var(--text); text-decoration: none; font-size: .96em; font-weight: 600; }}
  a:hover {{ color: var(--accent); }}
  .sum {{ margin: 4px 0 0; font-size: .88em; color: var(--sub); line-height: 1.45; }}
  .src {{ display: block; font-size: .76em; color: var(--sub); margin-top: 4px; opacity: .85; }}
  .none {{ color: var(--sub); font-size: .92em; }}

  .weather-card {{ background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff;
                   box-shadow: 0 4px 14px rgba(79,124,255,.35); }}
  .weather-card h2 {{ color: #fff; }}
  .wstrip {{ display: flex; gap: 8px; overflow-x: auto; padding-bottom: 2px; margin: 0 -4px; }}
  .wday {{ flex: 0 0 auto; min-width: 76px; text-align: center; background: rgba(255,255,255,.14);
          border-radius: 12px; padding: 10px 6px; }}
  .wday.today {{ background: rgba(255,255,255,.32); }}
  .wd-date {{ font-size: .78em; opacity: .9; }}
  .wd-date span {{ display: block; font-size: .9em; font-weight: 700; }}
  .wd-emoji {{ font-size: 1.5em; margin: 4px 0; }}
  .wd-desc {{ font-size: .72em; opacity: .9; min-height: 1.6em; }}
  .wd-temp {{ font-size: .85em; margin-top: 4px; }}
  .wd-rain {{ font-size: .72em; opacity: .9; margin-top: 2px; }}
</style>
</head>
<body>
<h1>📰 오늘의 뉴스 요약</h1>
<p class="date">{date_str}</p>
<section class="card weather-card"><h2><span class="ic">📍</span>안성 날씨</h2>{weather_html}</section>
{"".join(sections)}
</body>
</html>"""


if __name__ == "__main__":
    html = build_html()
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("done: docs/index.html")
