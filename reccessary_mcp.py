#!/usr/bin/env python3
"""
Reccessary 文章 MCP 連接器：讓 Claude 直接讀取「可閱讀」的新文章並整理重點

安裝:  pip install mcp requests beautifulsoup4

模式一（Claude Desktop 本機，stdio）:
    python reccessary_mcp.py --stdio
模式二（遠端，給 claude.ai / 手機 / Cowork 用，需公開 HTTPS 網址）:
    python reccessary_mcp.py            # 預設監聽 0.0.0.0:8000，端點 /mcp

環境變數: PORT(8000)  MCP_PATH(/mcp)  MIN_BODY_CHARS(500)
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

BASE = "https://www.reccessary.com"
LIST_URLS = [f"{BASE}/zh-tw/news/all", f"{BASE}/zh-tw"]
ARTICLE_RE = re.compile(r"^/zh-tw/(news|insight|interview|feature)/[^/?#]+$")
SKIP_SLUGS = {"all", "index", "list"}
HEADERS = {"User-Agent": "Mozilla/5.0 (personal MCP connector)"}
MIN_BODY = int(os.getenv("MIN_BODY_CHARS", "500"))
PAYWALL_MARKERS = ["會員限定", "登入後閱讀", "登入後繼續", "加入會員", "訂閱後閱讀",
                   "升級會員", "繼續閱讀請", "付費會員", "訂閱即可閱讀"]
SEEN_FILE = Path(os.getenv("SEEN_FILE", "mcp_seen.json"))
CACHE_TTL = 300  # 秒

mcp = FastMCP(
    "reccessary",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
    streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
    instructions=(
        "讀取 Reccessary（全球綠電與碳權市場資訊）的最新文章。"
        "只提供可公開閱讀的文章。回答時請用繁體中文整理重點，並附上原文連結。"
    ),
)
_cache = {}  # url -> (timestamp, parsed dict)


# ---------- 抓取與判斷 ----------
def _get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def _list_urls():
    found = {}
    for list_url in LIST_URLS:
        try:
            soup = BeautifulSoup(_get(list_url), "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            path = a["href"].replace(BASE, "")
            if ARTICLE_RE.match(path) and path.rsplit("/", 1)[-1] not in SKIP_SLUGS:
                found.setdefault(urljoin(BASE, path), True)
        if found:
            break
    return list(found)


def _parse(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    soup = BeautifulSoup(_get(url), "html.parser")
    meta = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    title = meta("og:title") or (soup.title.get_text(strip=True) if soup.title else url)
    node = soup.find("article") or soup.find("main") or soup.body
    date = meta("article:published_time")[:10]
    if not date and node:
        m = re.search(r"20\d{2}-\d{2}-\d{2}", node.get_text(" "))
        date = m.group(0) if m else ""
    paras = [p.get_text(" ", strip=True) for p in node.find_all("p")] if node else []
    body = "\n".join(p for p in paras if len(p) > 15)
    hits = [m for m in PAYWALL_MARKERS if m in body]
    if hits:
        ok, why = False, f"內文含會員牆字樣 {hits}"
    elif len(body) < MIN_BODY:
        ok, why = False, f"內文只有 {len(body)} 字"
    else:
        ok, why = True, ""
    data = {"url": url, "title": title, "date": date, "body": body,
            "readable": ok, "reason": why}
    _cache[url] = (time.time(), data)
    return data


def _load_seen():
    return set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()


def _fetch_many(urls):
    with ThreadPoolExecutor(max_workers=5) as ex:
        return list(ex.map(lambda u: _safe_parse(u), urls))


def _safe_parse(u):
    try:
        return _parse(u)
    except Exception as e:
        return {"url": u, "title": u, "date": "", "body": "", "readable": False,
                "reason": f"抓取失敗：{e}"}


def _format(items, chars):
    out = []
    for a in items:
        text = a["body"][:chars]
        more = "（內文已截斷）" if len(a["body"]) > chars else ""
        out.append(f"## {a['title']}\n日期：{a['date'] or '未知'}\n原文：{a['url']}\n\n{text}{more}")
    return "\n\n---\n\n".join(out) if out else "沒有符合條件的可閱讀文章。"


# ---------- MCP 工具 ----------
@mcp.tool()
def latest_articles(limit: int = 8, chars_per_article: int = 2500) -> str:
    """列出 Reccessary 最新的「可閱讀」文章（含原文連結與內文），供整理重點。
    會員限定或內文過短的文章會被自動排除。

    Args:
        limit: 最多回傳幾篇（1~15）
        chars_per_article: 每篇最多回傳多少字內文
    """
    limit = max(1, min(limit, 15))
    urls = _list_urls()[: limit * 2]  # 多抓一些，補償被排除的
    items = [a for a in _fetch_many(urls) if a["readable"]][:limit]
    return _format(items, chars_per_article)


@mcp.tool()
def new_articles_since_last_check(chars_per_article: int = 2500) -> str:
    """只回傳「上次呼叫之後新出現」的可閱讀文章；第一次呼叫會把現有文章標記為已讀，
    並回覆目前最新幾篇。適合用來詢問『有新文章嗎』。

    Args:
        chars_per_article: 每篇最多回傳多少字內文
    """
    urls = _list_urls()
    seen = _load_seen()
    first_run = not seen
    fresh = [u for u in urls if u not in seen]
    SEEN_FILE.write_text(json.dumps(sorted(seen | set(urls))))
    if first_run:
        items = [a for a in _fetch_many(urls[:10]) if a["readable"]][:5]
        return "（首次使用，已記錄目前所有文章。以下是目前最新幾篇）\n\n" + _format(items, chars_per_article)
    items = [a for a in _fetch_many(fresh) if a["readable"]]
    if not items:
        return "自上次檢查後，沒有新的可閱讀文章。"
    return _format(items, chars_per_article)


@mcp.tool()
def get_article(url: str, max_chars: int = 12000) -> str:
    """讀取單篇 Reccessary 文章全文（僅限 reccessary.com 網址，且須為可閱讀文章）。

    Args:
        url: 文章網址
        max_chars: 最多回傳字數
    """
    host = urlparse(url).netloc
    if host not in ("www.reccessary.com", "reccessary.com"):
        return "只支援 reccessary.com 的文章網址。"
    a = _safe_parse(url)
    if not a["readable"]:
        return f"這篇文章無法閱讀：{a['reason']}\n原文：{url}"
    return _format([a], max_chars)


if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        print(f"MCP 端點：http://0.0.0.0:{mcp.settings.port}{mcp.settings.streamable_http_path}")
        mcp.run(transport="streamable-http")
