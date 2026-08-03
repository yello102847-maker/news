"""매일 아침 뉴스 요약 정적 페이지 생성. 표준 라이브러리만 사용 (Gemini 호출용 urllib 제외)."""
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
LAT, LON = 37.0078, 127.2797  # 안성
# 평일 아침마다 실행되므로 보통은 전날 아침 이후 기사만. 월요일엔 주말치까지 거슬러 올라간다.
MAX_AGE_HOURS = 88 if datetime.now(KST).weekday() == 0 else 40

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              f"gemini-2.0-flash:generateContent?key={GEMINI_KEY}")

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

GN_SOLAR = ("https://news.google.com/rss/search?"
            "q=%ED%83%9C%EC%96%91%EA%B4%91+OR+%EC%9E%AC%EC%83%9D%EC%97%90%EB%84%88%EC%A7%80"
            "&hl=ko&gl=KR&ceid=KR:ko")

# (섹션 제목, RSS 주소, 최대 개수, 출처명(None이면 제목의 "- 언론사"에서 추출), 원문이 한국어인가)
# 원문이 한국어가 아닌 소스는 AI 요약(번역)에 성공한 기사만 싣는다 — 번역 없이 영어 제목을 보여주지 않는다.
FEEDS = [
    ("주식", "https://www.yna.co.kr/rss/market.xml", 5, "연합뉴스", True),
    ("경제", "https://www.yna.co.kr/rss/economy.xml", 4, "연합뉴스", True),
    ("태양광 · 재생에너지", GN_SOLAR, 5, None, True),
    ("국내 주요뉴스", "https://www.yna.co.kr/rss/society.xml", 6, "연합뉴스", True),
    ("세계 주요뉴스", "https://www.yna.co.kr/rss/international.xml", 5, "연합뉴스", True),
    ("과학 뉴스", "https://www.hellodd.com/rss/allArticle.xml", 3, "대덕넷", True),
    ("재밌는 과학 상식", "https://www.sciencedaily.com/rss/strange_offbeat.xml", 4, "ScienceDaily", False),
]

# 자극적이기만 하고 읽을 실익이 없는 사건사고, 그리고 사실 전달이 아닌 의견성 글을 걸러낸다.
BLOCK = ["학대", "성폭행", "성추행", "성범죄", "몰카", "불법촬영", "강간",
         "살해", "살인", "시신", "흉기", "칼부림", "음주운전", "만취",
         "투신", "자살", "극단적 선택", "몹쓸 짓", "엽기",
         "칼럼", "사설", "기고", "오피니언", "기자수첩", "취재후기"]

SUMMARY_PROMPT = """너는 뉴스 브리핑 편집자다. 아래 기사를 읽고 핵심 내용을 한국어로 요약해라.
- 제목을 그대로 반복하지 말고, 기사에 없는 내용은 절대 쓰지 마라 (추측 금지).
- 1~2문장으로, 읽자마자 무슨 일인지 파악되게 써라. 길이보다 이해가 우선이다.
- 종부세·양도세·세제개편 같은 전문용어나 정책 명칭은 그대로 쓰지 말고,
  평소 대화에서 쓰는 쉬운 말로 무슨 뜻이고 나한테 뭐가 달라지는지 풀어써라.

제목: {title}
본문: {body}"""


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
        days.append({
            "date": f"{date.month}/{date.day}({weekday})",
            "label": label,
            "today": delta == 0,
            "desc": WEATHER_CODES.get(d["weathercode"][i], "-"),
            "tmax": round(d["temperature_2m_max"][i]),
            "tmin": round(d["temperature_2m_min"][i]),
            "rain": d["precipitation_probability_max"][i],
        })
    return days


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


class _ParagraphExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_p = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.in_p += 1

    def handle_endtag(self, tag):
        if tag == "p" and self.in_p:
            self.in_p -= 1

    def handle_data(self, data):
        if self.in_p:
            self.parts.append(data)


def extract_article_text(url, max_chars=3000):
    html = fetch(url).decode("utf-8", errors="ignore")
    p = _ParagraphExtractor()
    p.feed(html)
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()[:max_chars]


def call_gemini(prompt):
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(GEMINI_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def get_summary(title, link):
    """기사 본문을 읽어 한 줄 요약. 실패하면 None (제목만 표시)."""
    if not GEMINI_KEY:
        return None
    try:
        body = extract_article_text(link)
        if len(body) < 100:  # 본문을 못 가져온 경우 (예: 구글 뉴스 중계 페이지)
            return None
        return call_gemini(SUMMARY_PROMPT.format(title=title, body=body))
    except Exception:
        return None


def build_html():
    now = datetime.now(KST)
    date_str = f"{now.year}년 {now.month}월 {now.day}일 ({'월화수목금토일'[now.weekday()]})"

    weather_days = None
    try:
        weather_days = get_weather()
        rows = "".join(
            f'<tr{" class=today" if d["today"] else ""}>'
            f'<td>{d["date"]}<span class=lbl>{d["label"]}</span></td>'
            f'<td>{escape(d["desc"])}</td>'
            f'<td>{d["tmin"]}° / {d["tmax"]}°</td>'
            f'<td>{d["rain"]}%</td></tr>'
            for d in weather_days
        )
        weather_html = ("<table><tr><th>날짜</th><th>날씨</th><th>최저/최고</th><th>강수</th></tr>"
                         f"{rows}</table>")
    except Exception as e:
        weather_html = f"<p>날씨 정보를 가져오지 못했습니다. ({escape(str(e))})</p>"

    seen = []
    sections = []
    top_headline = None
    for name, url, limit, source_label, is_korean in FEEDS:
        try:
            items = get_items(url, limit, seen, source_label)
        except Exception:
            items = []
        lis = []
        for t, link, src in items:
            summary = get_summary(t, link)
            if GEMINI_KEY:
                time.sleep(1.5)  # 무료 API 요청 한도 보호
            if not is_korean and not summary:
                continue  # 번역(요약)에 실패한 외국어 기사는 영어 제목 그대로 노출하지 않는다
            headline = summary or t
            if top_headline is None:
                top_headline = headline
            lis.append(
                f'<li><a href="{escape(link)}" target="_blank" rel="noopener">{escape(headline)}</a>'
                f'<span class=src>{escape(src)}</span></li>'
            )
        body = "<ul>" + "".join(lis) + "</ul>" if lis else '<p class="none">오늘은 새로 전할 소식이 없습니다.</p>'
        sections.append(f"<section><h2>{escape(name)}</h2>{body}</section>")

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
<meta name="theme-color" content="#0645ad">
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 640px;
         margin: 0 auto; padding: 20px; line-height: 1.5; color: #1a1a1a; }}
  h1 {{ font-size: 1.25em; margin-bottom: 4px; }}
  h2 {{ font-size: 1.05em; border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 30px; }}
  ul {{ padding-left: 1.1em; }}
  li {{ margin: 10px 0; }}
  a {{ color: #0645ad; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .src {{ display: block; font-size: .8em; color: #777; }}
  .none {{ color: #777; font-size: .92em; }}
  .weather {{ background: #eef5ff; padding: 12px 14px; border-radius: 8px; }}
  .weather h2 {{ margin-top: 0; border-color: #b7cdf0; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9em; }}
  th {{ text-align: left; font-weight: 600; color: #555; padding: 4px 2px; }}
  td {{ padding: 5px 2px; border-top: 1px solid #d5e2f5; }}
  tr.today td {{ font-weight: 700; }}
  .lbl {{ color: #0645ad; font-size: .85em; margin-left: 5px; }}
</style>
</head>
<body>
<h1>오늘의 뉴스 요약</h1>
<p>{date_str}</p>
<section class="weather"><h2>날씨 (안성)</h2>{weather_html}</section>
{"".join(sections)}
</body>
</html>"""


if __name__ == "__main__":
    html = build_html()
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("done: docs/index.html")
