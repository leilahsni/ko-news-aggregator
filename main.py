import time
import json
import requests
import feedparser
import threading
import trafilatura

from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from urllib.parse import quote, urlparse
from deep_translator import GoogleTranslator, MyMemoryTranslator

app = FastAPI()

templates = Jinja2Templates(directory="templates")

def get_news(keyword="삼성전자", limit=10):

    rss_url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(keyword)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )

    feed = feedparser.parse(rss_url)

    articles = []

    for entry in feed.entries[:limit]:

        raw_summary = entry.get("summary", "")

        summary = BeautifulSoup(
            raw_summary,
            "html.parser"
        ).get_text(
            separator=" ",
            strip=True
        )

        source = entry.get("source", {})

        articles.append({
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
            "summary": summary,
            "source": source.get("title", ""),
        })

    return articles

@app.get("/")
def homepage(request: Request, q: str = "삼성전자 AI"):

    q = q.strip()[:100] or "삼성전자 AI"

    try:
        articles = get_news(keyword=q, limit=30)
    except Exception as e:
        print("News fetch error:", e)
        articles = []

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "articles": articles,
            "q": q,
            "trending": get_trending(),   # <-- add this
        }
    )

translation_cache = {}
translate_lock = threading.Lock()
last_translation_time = 0.0
MIN_INTERVAL = 0.5   # seconds between calls to the translation service

@app.get("/translate")
def translate(word: str):
    global last_translation_time

    word = word.strip()
    if not word or len(word) > 500:
        return {"translation": ""}

    if word in translation_cache:
        return {"translation": translation_cache[word]}

    # Only one outgoing request at a time
    with translate_lock:
        # Another click may have just fetched the same word
        if word in translation_cache:
            return {"translation": translation_cache[word]}

        wait = MIN_INTERVAL - (time.time() - last_translation_time)
        if wait > 0:
            time.sleep(wait)

        result = None

        # Try Google up to 3 times, backing off between attempts
        for attempt in range(3):
            try:
                result = GoogleTranslator(source="ko", target="en").translate(word)
                if result:
                    break
            except Exception as e:
                print(f"Google translate error (try {attempt + 1}):", e)
                time.sleep(1.5 * (attempt + 1))

        # Fallback to a different free service
        if not result:
            try:
                result = MyMemoryTranslator(source="ko-KR", target="en-US").translate(word)
            except Exception as e:
                print("MyMemory fallback error:", e)

        last_translation_time = time.time()

        if result:
            translation_cache[word] = result
            return {"translation": result}

    # Don't cache failures; tell the browser to try again later
    return JSONResponse({"translation": ""}, status_code=429)


def extract_article(url):

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            )
        },
        timeout=15
    )

    response.raise_for_status()

    text = trafilatura.extract(
        response.text,
        include_comments=False,
        include_tables=False
    )

    return {
        "url": response.url,
        "article": text or ""
    }

url_cache = {}

def resolve_google_news_url(url):
    if "news.google.com" not in url:
        return url
    if url in url_cache:
        return url_cache[url]

    article_id = urlparse(url).path.rstrip("/").split("/")[-1]

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        )
    })
    # Skip the EU consent wall
    session.cookies.set("CONSENT", "YES+cb", domain=".google.com")
    session.cookies.set("SOCS", "CAI", domain=".google.com")

    # Step 1: get signature + timestamp from the article page
    page = session.get(
        f"https://news.google.com/rss/articles/{article_id}",
        timeout=15
    )
    soup = BeautifulSoup(page.text, "html.parser")
    node = soup.select_one("c-wiz > div[jscontroller]")
    if not node:
        raise ValueError("Could not read Google News page (consent wall or layout change)")

    signature = node.get("data-n-a-sg")
    timestamp = node.get("data-n-a-ts")

    # Step 2: ask Google's batchexecute endpoint for the real URL
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
        'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id}",{timestamp},"{signature}"]'
    )
    payload = [[["Fbv4je", inner]]]

    resp = session.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data="f.req=" + quote(json.dumps(payload)),
        timeout=15
    )
    resp.raise_for_status()

    parsed = json.loads(resp.text.split("\n\n")[1])[:-2]
    decoded = json.loads(parsed[0][2])[1]

    url_cache[url] = decoded
    return decoded

@app.get("/article")
def article_endpoint(url: str):

    try:

        # 1. Try to resolve Google News URL
        publisher_url = resolve_google_news_url(url)

        print("PUBLISHER:", publisher_url)

        # 2. Fetch publisher
        article = extract_article(publisher_url)

        return article

    except Exception as e:

        print("Article extraction error:", e)

        return {
            "article": "",
            "error": str(e)
        }

trending_cache = {"time": 0.0, "items": []}

def traffic_value(label):
    """'20000+' -> 20000; missing -> 0"""
    digits = "".join(ch for ch in label if ch.isdigit())
    return int(digits) if digits else 0

def get_trending(limit=10):
    """Trending searches in Korea, sorted by approximate volume, cached 10 min."""
    now = time.time()
    if trending_cache["items"] and now - trending_cache["time"] < 600:
        return trending_cache["items"]

    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=KR",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)

        items = []
        for entry in feed.entries[:limit]:
            query = entry.get("title", "").strip()
            if query:
                items.append({
                    "query": query,
                    "traffic": entry.get("ht_approx_traffic", ""),
                })

        # Highest volume first; ties keep Google's order (newest first)
        items.sort(key=lambda i: traffic_value(i["traffic"]), reverse=True)

        if items:
            trending_cache["items"] = items
            trending_cache["time"] = now
    except Exception as e:
        print("Trending fetch error:", e)

    return trending_cache["items"]