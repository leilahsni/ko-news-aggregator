import feedparser
import requests
import trafilatura


def get_news(keyword="AI", limit=20):
    url = (
        f"https://news.google.com/rss/search"
        f"?q={keyword}"
        f"&hl=ko"
        f"&gl=KR"
        f"&ceid=KR:ko"
    )

    feed = feedparser.parse(url)

    articles = []

    for entry in feed.entries[:limit]:

        try:
            page = requests.get(
                entry.link,
                timeout=10,
                allow_redirects=True,
            )

            article_url = page.url

            downloaded = trafilatura.fetch_url(article_url)
            content = trafilatura.extract(downloaded)

            summary = (
                content[:250] + "..."
                if content else "No summary available"
            )

        except Exception:
            article_url = entry.link
            summary = "Could not extract article"

        articles.append({
            "title": entry.title,
            "link": article_url,
            "summary": summary,
            "published": entry.get("published", ""),
            "thumbnail": f"https://picsum.photos/600/400?random={len(articles)}"
        })

    return articles