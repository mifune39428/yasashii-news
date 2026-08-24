#!/usr/bin/env python3
"""政治・経済・国際のニュースを集め、「週刊こどもニュース」のようなやさしい解説を付けて
docs/articles.json に書き出す。

1本の記事につき用意するのは次の4つ。原文の本文はそのまま載せない
（独自の解説・出典名・原文へのリンクだけを持たせて、引用の範囲に収める）。
  ひとことで言うと … 30〜45字の要約
  何があったの     … 起きたことの説明
  そもそも         … 前提になる仕組みや背景（これが「こどもニュース」の肝）
  わたしたちへの影響 … 暮らしとの関わり
それに加えて、記事に出てくる難しい言葉を「ことば」として最大3つ、やさしい定義付きで持たせる。

GitHub Actions から6時間ごとに実行され、差分がコミットされると
GitHub Pages 側のサイトが更新される。ローカルでも同じスクリプトが動く。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import llm_providers  # noqa: E402  （.env 読み込みより前でよい。キーは呼び出し時に参照される）

FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "articles.json")

USER_AGENT = "yasashii-news/1.0 (+https://github.com/mifune39428)"
FETCH_TIMEOUT = 25

# 新しく取り込む記事の対象期間。ニュースは足が速いので短くする。
INTAKE_DAYS = 2
# サイトに残す期間と件数の上限。解説は少し寝かせても読めるので2週間残す。
KEEP_DAYS = 14
KEEP_MAX = 300
# 1回のLLM呼び出しでまとめて処理する記事数。
# 1件あたりの出力が長い（解説4本＋ことば3つ）ので、要約系のサイトより小さくする。
BATCH_SIZE = 4
# 1回の実行で解説を書く上限。無料枠の1日あたり回数を使い切らないための蓋。
# 溢れた分は次の実行（6時間後）に回る。
MAX_NEW_PER_RUN = 24
# そのうち各ジャンルのために空けておく枠。
# 政治のフィードは本数が多いので、放っておくと経済と国際が埋まらない。
GENRE_QUOTA = {"経済": 7, "国際": 7}
# 1回の実行で、過去の記事のサムネイルを取りに行く件数の上限。
BACKFILL_PER_RUN = 30

# サイト上部のタブ。
GENRES = ["政治", "経済", "国際"]

CATEGORIES = [
    "選挙・政党",
    "国会・法律",
    "予算・税金",
    "内閣・行政",
    "景気・物価",
    "円安・株・金利",
    "企業・産業",
    "働く・くらしのお金",
    "貿易・関税",
    "アメリカ",
    "中国・アジア",
    "ヨーロッパ",
    "中東",
    "ウクライナ・ロシア",
    "その他の地域",
    "安全保障・防衛",
    "エネルギー・環境",
    "その他",
]

REGIONS = ["国内", "海外"]

# 総合フィードに混ざってくる、このサイトの守備範囲でない見出し。
# LLMに渡す前に落とす（1回の解説枠をスポーツと料理で食い潰さないため）。
NOISE_RE = re.compile(
    r"プロ野球|高校野球|甲子園|大リーグ|MLB|NBA|NFL|Jリーグ|サッカー|なでしこ|"
    r"大相撲|力士|ゴルフ|テニス|マラソン|駅伝|五輪|オリンピック|パラリンピック|W杯|ワールドカップ|"
    r"ドラマ|映画|アイドル|歌手|お笑い|芸能|タレント|グラビア|"
    r"レシピ|作り方|難読漢字|占い|星座|運勢|"
    r"猛暑日|熱中症|台風\d|大雨|天気予報",
    re.I,
)

# 出典名にこの文字列を含むものは載せない。
BLOCK_SOURCES = ["PR TIMES", "prtimes", "GameWith", "Game8", "アルテマ", "神ゲー攻略"]

# Googleニュースの <source> がドメインのまま入ってくる媒体を、読める名前に直す。
DOMAIN_NAMES = {
    "news.yahoo.co.jp": "Yahoo!ニュース",
    "www3.nhk.or.jp": "NHK",
    "www.nhk.or.jp": "NHK",
    "news.ntv.co.jp": "日テレNEWS",
}

JST = dt.timezone(dt.timedelta(hours=9))

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
RSS10 = "{http://purl.org/rss/1.0/}"


# --------------------------------------------------------------------------
# 下ごしらえ
# --------------------------------------------------------------------------

def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入るので不要）。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """トラッキング用のクエリを落として、同じ記事が別URLに見えないようにする。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "at_"))
    ]
    # Google ニュースはクエリに記事のIDが載るので触らない。
    if parts.netloc == "news.google.com":
        query = urllib.parse.parse_qsl(parts.query)
    cleaned = parts._replace(query=urllib.parse.urlencode(query), fragment="")
    return urllib.parse.urlunsplit(cleaned).rstrip("/")


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def parse_date(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# RSS / Atom / RDF の取得
# --------------------------------------------------------------------------

def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None:
            if found.text:
                return found.text
            # Atom の <link href="..."> のように属性側に入っている場合。
            href = found.get("href")
            if href:
                return href
    return ""


# 記事のサムネイルとして使わない画像（配信計測用の透明画像やアイコンなど）。
IMAGE_BLOCKLIST = ("feedburner", "gravatar", "/pixel", "1x1", "blank.gif", "spacer",
                   "doubleclick", "profile_images")
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']'
    r'[^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
    re.I,
)


def usable_image(url: str, base: str) -> str:
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    url = urllib.parse.urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return ""
    if any(word in url.lower() for word in IMAGE_BLOCKLIST):
        return ""
    return url


def image_from_entry(entry, base: str) -> str:
    """RSSの中に入っている画像を探す。媒体ごとに置き場所が違うので順に当たる。"""
    for node in list(entry.iter(f"{MEDIA}thumbnail")) + list(entry.iter(f"{MEDIA}content")):
        medium = (node.get("medium") or node.get("type") or "").lower()
        if medium and "image" not in medium:
            continue
        found = usable_image(node.get("url", ""), base)
        if found:
            return found

    for node in entry.findall("enclosure") + entry.findall(f"{ATOM}link"):
        if "image" in (node.get("type") or "").lower():
            found = usable_image(node.get("url") or node.get("href") or "", base)
            if found:
                return found

    # 本文HTMLの最初の <img>。多くの媒体はここにアイキャッチが入っている。
    raw_body = " ".join(
        node.text or ""
        for tag in ("description", f"{CONTENT}encoded", f"{RSS10}description",
                    f"{ATOM}summary", f"{ATOM}content")
        for node in entry.findall(tag)
    )
    for candidate in IMG_TAG_RE.findall(raw_body):
        found = usable_image(candidate, base)
        if found:
            return found
    return ""


# --------------------------------------------------------------------------
# Google ニュースのリンクを元媒体のURLに戻す
# --------------------------------------------------------------------------

GOOGLE_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
TIMESTAMP_RE = re.compile(r'data-n-a-ts="([^"]+)"')


def resolve_google_url(url: str) -> str:
    """news.google.com の転送URLから、元媒体の記事URLを取り出す。

    転送ページはJavaScriptで飛ぶ作りなので、HTTPを追うだけでは元URLが分からない。
    ページに埋まっている署名（sg）と時刻（ts）を Google の batchexecute に投げると
    元URLが返る。取れなければ転送URLのまま使う（リンクとしては機能する）。
    """
    if "news.google.com" not in url:
        return url
    try:
        gid = url.split("/articles/")[1].split("?")[0]
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            # 署名はページのかなり後ろに入っているので、途中で切らずに全部読む。
            page = response.read().decode("utf-8", errors="ignore")
        signature, timestamp = SIGNATURE_RE.search(page), TIMESTAMP_RE.search(page)
        if not signature or not timestamp:
            return url

        payload = [[
            "Fbv4je",
            json.dumps([
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                  None, None, None, None, None, 0, 1],
                 "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                gid, int(timestamp.group(1)), signature.group(1),
            ]),
            None, "1",
        ]]
        data = urllib.parse.urlencode({"f.req": json.dumps([payload])}).encode()
        request = urllib.request.Request(
            GOOGLE_BATCH_URL,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001  取れなくても転送URLで記事は読める
        return url
    return parse_garturlres(body) or url


def parse_garturlres(body: str) -> str:
    """batchexecute の返事から元URLを取り出す。URLは二重にJSONエスケープされている。"""
    for line in body.splitlines():
        if "garturlres" not in line:
            continue
        try:
            for part in json.loads(line):
                if isinstance(part, list) and len(part) > 2 and part[0] == "wrb.fr":
                    inner = json.loads(part[2])
                    if len(inner) > 1 and str(inner[1]).startswith("http"):
                        return canonical_url(inner[1])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return ""


def resolve_google_urls(items: list[dict]) -> None:
    targets = [item for item in items if "news.google.com" in item["url"]]
    if not targets:
        return
    print(f"  Googleニュースのリンク {len(targets)}件を元媒体のURLに変換中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, resolved in zip(targets, pool.map(lambda i: resolve_google_url(i["url"]), targets)):
            item["url"] = resolved
    remaining = sum(1 for item in targets if "news.google.com" in item["url"])
    print(f"  変換できたもの {len(targets) - remaining}件")


def fetch_og_image(url: str) -> str:
    """RSSに画像が無い記事は、元ページの og:image を見に行く。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=12) as response:
            head = response.read(200_000).decode("utf-8", errors="ignore")
            final_url = response.geturl()
    except Exception:  # noqa: BLE001  取れなくても記事自体は載せる
        return ""
    match = OG_IMAGE_RE.search(head)
    if not match:
        return ""
    return usable_image(match.group(1) or match.group(2) or "", final_url)


def fill_missing_images(items: list[dict], limit: int = 0) -> None:
    """画像がまだ無い記事について、元ページの og:image を取りに行く。"""
    targets = [item for item in items if not item.get("image")]
    if limit:
        targets = targets[:limit]
    if not targets:
        return
    print(f"  サムネイル未取得 {len(targets)}件をページから取得中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, image in zip(targets, pool.map(lambda i: fetch_og_image(i["url"]), targets)):
            item["image"] = image
    print(f"  取得できたもの {sum(1 for item in targets if item['image'])}件")


def entry_body(entry) -> str:
    """解説の材料になる本文を取り出す。"""
    raw = _text(
        entry,
        "description",
        f"{CONTENT}encoded",
        f"{RSS10}description",
        f"{ATOM}summary",
        f"{ATOM}content",
    )
    return strip_html(raw)


def fetch_feed(feed: dict) -> list[dict]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()

    root = ET.fromstring(raw)
    entries = (
        root.findall(".//item")
        or root.findall(f".//{RSS10}item")
        or root.findall(f".//{ATOM}entry")
    )

    items = []
    for entry in entries:
        title = strip_html(_text(entry, "title", f"{ATOM}title", f"{RSS10}title"))
        link = _text(entry, "link", f"{RSS10}link", f"{ATOM}link").strip()
        if not link:
            # Atom は複数の <link> を持つので rel="alternate" を拾う。
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break
        if not title or not link:
            continue

        source = feed["name"]
        if feed.get("google_news"):
            # Google ニュースの見出しは「本文の見出し - 媒体名」の形。
            actual = strip_html(_text(entry, "source"))
            if actual:
                source = DOMAIN_NAMES.get(actual, actual)
                if title.endswith(f" - {actual}"):
                    title = title[: -len(actual) - 3].strip()
            else:
                title = re.sub(r"\s+-\s+[^-]{2,30}$", "", title).strip()
        if any(blocked.lower() in source.lower() for blocked in BLOCK_SOURCES):
            continue

        body = entry_body(entry)
        # 総合フィードはスポーツ・芸能・料理が混ざる。見出しで粗く落としておく
        # （最終的な線引きはLLMの relevant 判定だが、そこまで運ぶと解説の枠を食う）。
        if feed.get("mixed") and NOISE_RE.search(f"{title} {body[:120]}"):
            continue
        # Google ニュースの description は他媒体へのリンク集なので材料にならない。
        if feed.get("google_news"):
            body = ""

        published = parse_date(
            _text(entry, "pubDate", f"{DC}date", f"{ATOM}published", f"{ATOM}updated", "date")
        )

        items.append(
            {
                "id": article_id(link),
                "url": canonical_url(link),
                "title_original": title,
                "excerpt": body[:800],
                "source": source,
                "image": image_from_entry(entry, link),
                "genre_hint": feed.get("genre", ""),
                "region": feed.get("region", "国内"),
                "published": (published or dt.datetime.now(dt.timezone.utc)).isoformat(),
            }
        )

    limit = int(feed.get("max_items", 0) or 0)
    if limit:
        items.sort(key=lambda item: item["published"], reverse=True)
        items = items[:limit]
    return items


def collect_feed_items(feeds: list[dict]) -> list[dict]:
    collected: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, feed): feed for feed in feeds}
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                items = future.result()
            except Exception as exc:  # 1本落ちても全体は続ける
                print(f"  × {feed['name']}: {type(exc).__name__}: {exc}")
                continue
            print(f"  ○ {feed['name']}: {len(items)}件")
            collected.extend(items)
    return collected


# --------------------------------------------------------------------------
# 重複の除去
# --------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = re.sub(r"[\s　]+", "", title.lower())
    return re.sub(r"[!-/:-@\[-`{-~、。「」・…—–\-]", "", title)


TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[0-9]+|[ァ-ヶー]{2,}|[一-龥]{2,}")


def title_tokens(title: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(title)}


def same_story(a: dict, b: dict) -> bool:
    """やさしくした見出しで、同じ出来事かどうかを見る。

    同じ会見・同じ発表を各社が書くので、ニュースサイトでは重複の除去が要になる。
    海外記事と国内記事は原題が別物なので、日本語の見出しになって初めて重なりが分かる。
    """
    published_a, published_b = parse_date(a["published"]), parse_date(b["published"])
    if published_a and published_b and abs((published_a - published_b).total_seconds()) > 36 * 3600:
        return False

    left, right = normalize_title(a["title_ja"]), normalize_title(b["title_ja"])
    if SequenceMatcher(None, left, right).ratio() >= 0.72:
        return True

    tokens_a, tokens_b = title_tokens(a["title_ja"]), title_tokens(b["title_ja"])
    if len(tokens_a) >= 3 and len(tokens_b) >= 3:
        if len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= 0.6:
            return True
    return False


def dedupe_stories(new_items: list[dict], existing_items: list[dict]) -> list[dict]:
    """同じ出来事は1本に絞る。各社が同じ会見を書くので、ここが効かないと同じ話が並ぶ。"""
    recent = existing_items[:120]
    kept: list[dict] = []
    for item in new_items:
        older = next((o for o in recent if same_story(item, o)), None)
        if older is not None:
            print(f"  ・既出のため除外: {item['title_ja']}（{item['source']}）")
            continue
        if any(same_story(item, other) for other in kept):
            print(f"  ・重複のため除外: {item['title_ja']}（{item['source']}）")
            continue
        kept.append(item)
    return kept


DEDUPE_PROMPT = """次に並べるのは、ニュースサイトに載せる記事の見出しです。
このうち「同じ出来事を伝えている記事」の組を見つけてください。

同じ出来事とみなすもの:
- 同じ発表・同じ会見・同じ事件について、別の新聞社が書いた記事。
- 日本語の記事と外国語の記事が、同じ出来事を伝えている場合。
- 続報と第一報のように、同じ出来事の同じ局面を伝えている場合。

同じ出来事とみなさないもの:
- 同じ国・同じ人物・同じテーマというだけで、出来事が別のもの
  （例: 同じ大統領の別々の発言、同じ戦争の別々の戦闘、同じ会社の別々の発表）。
- 一方が個別の出来事、もう一方がその分野全体の解説記事である場合。

出力はJSON配列のみ。前置き・説明・コードフェンスを付けない。
同じ出来事の記号を1つの配列にまとめ、それを並べる。組が無ければ [] とだけ書く。
例: [["N1","N5"],["N3","E12"]]

見出し:
{articles}
"""


def parse_groups(text: str, valid: set[str]) -> list[list[str]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list):
        raise llm_providers.ResponseInvalid("配列ではありません")
    groups = []
    for group in data:
        if not isinstance(group, list):
            raise llm_providers.ResponseInvalid("組が配列になっていません")
        # 存在しない記号を返してくることがある。返事ごと捨てると解説の枠を無駄にするので、
        # 知らない記号だけ落として残りを使う。
        labels = [label for label in (str(v).strip().upper() for v in group) if label in valid]
        if len(set(labels)) >= 2:
            groups.append(sorted(set(labels)))
    return groups


def drop_duplicate_stories(new_items: list[dict], existing_items: list[dict]) -> list[dict]:
    """見出しを突き合わせて、同じ出来事の記事をLLMに1本へまとめさせる。

    各社が同じ会見を書くので、ニュースサイトでは重複の除去が要になる。
    語の重なりを見る same_story() だけでは、
    「服の通販シーインが香港で上場へ」と「洋服通販のシーインが株式を売り出す計画」のように
    書き方が違う同じ話を取りこぼす。ここを外すと同じ話が2本並ぶので、判断はLLMに任せる。
    失敗したときは何も落とさない（重複が1回残るだけで、記事が消えるよりはよい）。
    """
    if len(new_items) < 2:
        return new_items
    # 見比べる相手は直近3日の既存記事だけ。それより古いものは同じ出来事になりにくい。
    fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    recent = [
        item for item in existing_items[:80]
        if (parse_date(item.get("published", "")) or fresh) >= fresh
    ][:60]

    labels: dict[str, dict] = {}
    lines: list[str] = []
    for prefix, items in (("N", new_items), ("E", recent)):
        for index, item in enumerate(items, start=1):
            label = f"{prefix}{index}"
            labels[label] = item
            published = parse_date(item["published"])
            when = published.astimezone(JST).strftime("%m/%d %H:%M") if published else "??"
            lines.append(f"{label} [{when} {item['genre']}] {item['title_ja']}（{item['source']}）")

    try:
        text = llm_providers.generate_text(
            DEDUPE_PROMPT.format(articles="\n".join(lines)),
            validate=lambda t: parse_groups(t, set(labels)),
        )
        groups = parse_groups(text, set(labels))
    except llm_providers.LLMError as exc:
        print(f"  × 重複のまとめに失敗（今回はそのまま載せます）: {exc}")
        return new_items

    dropped: set[str] = set()
    for group in groups:
        members = [label for label in group if label not in dropped]
        if len(members) < 2:
            continue
        # 既に載っている記事があるなら、そちらを残して新着を落とす（リンクを差し替えない）。
        keep = next((label for label in members if label.startswith("E")), members[0])
        for label in members:
            if label == keep or label.startswith("E"):
                continue
            dropped.add(label)
            print(f"  ・同じ出来事のため除外: {labels[label]['title_ja']}"
                  f"（{labels[label]['source']}）← {labels[keep]['title_ja']}")
    return [item for label, item in labels.items()
            if label.startswith("N") and label not in dropped]


def is_duplicate(title: str, known_titles: list[str]) -> bool:
    target = normalize_title(title)
    if not target:
        return False
    for known in known_titles:
        if not known:
            continue
        if target == known:
            return True
        if abs(len(target) - len(known)) <= max(6, len(target) * 0.3):
            if SequenceMatcher(None, target, known).ratio() >= 0.86:
                return True
    return False


def interleave_by_source(items: list[dict], limit: int) -> list[dict]:
    """出典ごとに1件ずつ順番に取って上限まで詰める。

    記事数の多い媒体（NHK・日経）が枠を独占すると、他紙が1本も載らなくなる。
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["source"], []).append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: item["published"], reverse=True)

    picked: list[dict] = []
    while len(picked) < limit:
        added = False
        for bucket in buckets.values():
            if not bucket:
                continue
            picked.append(bucket.pop(0))
            added = True
            if len(picked) >= limit:
                break
        if not added:
            break
    return picked


# --------------------------------------------------------------------------
# やさしい解説を書く
# --------------------------------------------------------------------------

PROMPT_TEMPLATE = """あなたは、テレビの「週刊こどもニュース」のような番組で、
政治・経済・国際のニュースを子どもにも分かる言葉で説明する解説者です。
新聞社・通信社の見出しと抜粋を渡すので、1本ずつ「やさしい解説」を書いてください。

読者は、ニュースは気になるけれど専門用語で挫折してきた大人と、その家族です。
新聞を読み上げるのではなく、「そもそもこれは何の話か」から説明してください。

厳守すること:
- 原文を写さない。翻訳文をそのまま載せるのでもない。事実を踏まえて自分の言葉で書く。
- 英語の記事も、すべて日本語で書く。
- 抜粋に書かれていない事実（数字・金額・日付・発言）を作らない。
  抜粋が無く見出しだけの場合は、見出しから確実に言えることだけを書く。
  分からないことは「〜と伝えられています」ではなく、書かない。
- 「そもそも」に書いてよいのは、教科書に載っているような一般的な仕組み・制度・経緯だけ。
  今回の記事の細かい事実を、確認できないまま背景として書き足さない。
- 中立に書く。ある政党・国・企業を持ち上げたり、けなしたりしない。
  「〜すべきだ」という主張や、書き手の意見を入れない。賛成・反対の両論があるなら両方に触れる。
- 難しい言葉は本文中で言い換えるか、terms（ことば）で説明する。
  かぎかっこ付きの専門用語を説明なしに使わない。

各項目の書き方:
- title_ja: やさしい見出し。日本語40文字以内。何の話かが一読で分かるようにする。煽らない。
  新聞の見出しのように短く言い切る（「〜しました」で終える必要はない）。
  役職や国の名前は略さずに書く（「英首相」より「イギリスの首相」）。
- one_line: 「ひとことで言うと」。30〜50字の1文。です・ます調で言い切る。
- what: 「何があったの？」。90〜140字。いつ・だれが・何をしたかを、易しい言葉で。
- background: 「そもそも」。120〜200字。この話を理解するのに必要な前提
  （その組織は何をする所か、その制度はなぜあるのか、これまでの経緯）を説明する。
  ここが一番大事な部分。前提が要らないほど単純な話でも、必ず何かしら足がかりを書く。
- impact: 「わたしたちへの影響」。80〜130字。暮らし・家計・仕事・将来にどう関わるか。
  影響がすぐには出ない話なら「今すぐ変わるものではないが、〜」と正直に書く。
- terms: 記事に出てくる難しい言葉を0〜3つ。それぞれ
  {{"word":"言葉","meaning":"40〜80字のやさしい説明"}}。
  小学生でも知っている言葉（会社・お金・選挙など）は入れない。抜粋に出てこない言葉も入れない。
- relevant: 政治・経済・国際のニュースなら true。
  制度・法律・予算・税金・選挙・外交・安全保障・景気・物価・株・企業・貿易・
  エネルギーに関わる話は、小さな動きでも、過去を振り返る解説記事でも true。
  政治家や経済人の訃報・人事も true。
  false にするのはこれだけ: スポーツ、芸能、料理・生活情報、占い、
  広告・通販・セール告知、ランキングや「〜が話題」だけの記事、有料会員向けの宣伝、
  そして政治・経済の動きにつながらない単発の事件・事故・災害。
- genre: 政治 / 経済 / 国際 のどれか1つ。
  日本の国内政治なら政治、お金・景気・企業・物価なら経済、外国が主役なら国際。
  日本の外交・安全保障は国際にする。
- category は次から必ず1つ選ぶ: {categories}
- importance は1〜5の整数。5=暮らしに直結する大きな出来事、3=普通、1=小さな動き。
- 出力はJSON配列のみ。前置き・説明・コードフェンスを付けない。

出力形式（要素数は入力と同じ{count}件、iは入力の番号）:
[{{"i":1,"relevant":true,"genre":"経済","category":"景気・物価","title_ja":"...","one_line":"...","what":"...","background":"...","impact":"...","terms":[{{"word":"...","meaning":"..."}}],"importance":3}}]

入力記事:
{articles}
"""


def build_prompt(batch: list[dict]) -> str:
    lines = []
    today = dt.datetime.now(JST).strftime("%Y年%m月%d日")
    for index, item in enumerate(batch, start=1):
        published = parse_date(item["published"])
        when = published.astimezone(JST).strftime("%m月%d日") if published else "不明"
        lines.append(
            f"[{index}] 出典: {item['source']}（{item['region']}） / 配信日: {when}\n"
            f"見出し: {item['title_original']}\n"
            f"抜粋: {item['excerpt'][:600] or '(抜粋なし)'}\n"
        )
    return PROMPT_TEMPLATE.format(
        categories=" / ".join(CATEGORIES),
        count=len(batch),
        articles="\n".join(lines),
    ) + f"\n（きょうは{today}です）\n"


REQUIRED_FIELDS = ("title_ja", "one_line", "what", "background", "impact")


def parse_llm_json(text: str, expected: int) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise llm_providers.ResponseInvalid("空の配列です")
    if len(data) != expected:
        raise llm_providers.ResponseInvalid(f"{expected}件のはずが{len(data)}件です")
    for entry in data:
        if not isinstance(entry, dict):
            raise llm_providers.ResponseInvalid("配列の中身がオブジェクトではありません")
        # 対象外と判定された記事は解説が空でよい。
        if entry.get("relevant") is False:
            continue
        missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field, "")).strip()]
        if missing:
            raise llm_providers.ResponseInvalid(f"{'、'.join(missing)} が欠けています")
    return data


def clean_terms(raw) -> list[dict]:
    """「ことば」を整える。説明の無い語や長すぎる語は落とす。"""
    if not isinstance(raw, list):
        return []
    terms: list[dict] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, dict):
            continue
        word = re.sub(r"\s+", " ", str(value.get("word", ""))).strip(" 　「」『』・,、")
        meaning = re.sub(r"\s+", " ", str(value.get("meaning", ""))).strip()
        if not word or not meaning or len(word) > 20 or word in seen:
            continue
        seen.add(word)
        terms.append({"word": word, "meaning": meaning[:160]})
    return terms[:3]


def enrich(items: list[dict]) -> tuple[list[dict], set[str]]:
    """LLMでやさしい見出しと解説を付ける。失敗した分は捨てて次回に回す。

    載せる記事と、「守備範囲外」と判断した記事のIDを返す。
    後者を覚えておかないと、同じ記事をRSSに残っている間ずっと解説し直してしまう。
    """
    results: list[dict] = []
    rejected: set[str] = set()
    for offset in range(0, len(items), BATCH_SIZE):
        batch = items[offset : offset + BATCH_SIZE]
        print(f"  解説 {offset + 1}〜{offset + len(batch)}件目 …")
        try:
            text = llm_providers.generate_text(
                build_prompt(batch),
                validate=lambda t, n=len(batch): parse_llm_json(t, n),
            )
            entries = parse_llm_json(text, len(batch))
        except llm_providers.LLMError as exc:
            # 生煮えの解説をサイトに出すより、今回は見送って次の実行で拾い直す。
            print(f"  × 解説に失敗（この{len(batch)}件は次回に回します）: {exc}")
            continue

        by_index = {}
        for entry in entries:
            try:
                by_index[int(entry.get("i", 0))] = entry
            except (TypeError, ValueError):
                continue

        for index, item in enumerate(batch, start=1):
            entry = by_index.get(index) or entries[index - 1]
            if entry.get("relevant") is False:
                print(f"  ・守備範囲外のため除外: {item['title_original'][:40]}")
                rejected.add(item["id"])
                continue
            genre = str(entry.get("genre", "")).strip()
            category = str(entry.get("category", "")).strip()
            item["title_ja"] = str(entry["title_ja"]).strip()
            item["one_line"] = str(entry["one_line"]).strip()
            item["what"] = str(entry["what"]).strip()
            item["background"] = str(entry["background"]).strip()
            item["impact"] = str(entry["impact"]).strip()
            item["genre"] = genre if genre in GENRES else (
                item["genre_hint"] if item["genre_hint"] in GENRES else "政治"
            )
            item["category"] = category if category in CATEGORIES else "その他"
            item["terms"] = clean_terms(entry.get("terms"))
            try:
                item["importance"] = max(1, min(5, int(entry.get("importance", 3))))
            except (TypeError, ValueError):
                item["importance"] = 3
            results.append(item)
    return results, rejected


def to_public(item: dict) -> dict:
    """サイトに出す形に整える。原文の抜粋は公開データに残さない。"""
    return {key: value for key, value in item.items()
            if key not in ("excerpt", "genre_hint")}


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

# 一度「載せない」と決めた記事を覚えておく日数。
# 取り込み期間（INTAKE_DAYS）より長くしないと、同じ記事がまた解説の枠を食う。
SKIP_MEMORY_DAYS = 5


def load_existing() -> dict:
    if not os.path.exists(OUTPUT_PATH):
        return {"updated_at": None, "items": []}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "items": []}
    data.setdefault("items", [])
    data.setdefault("skipped", [])
    return data


def pick_for_run(new_items: list[dict]) -> list[dict]:
    """今回解説する記事を選ぶ。ジャンルごとの枠を先に確保してから、残りを出典で回す。"""
    if len(new_items) <= MAX_NEW_PER_RUN:
        return new_items
    picked: list[dict] = []
    taken: set[str] = set()
    for genre, quota in GENRE_QUOTA.items():
        share = interleave_by_source(
            [item for item in new_items if item["genre_hint"] == genre], quota
        )
        picked.extend(share)
        taken.update(item["id"] for item in share)
    rest = interleave_by_source(
        [item for item in new_items if item["id"] not in taken],
        max(0, MAX_NEW_PER_RUN - len(picked)),
    )
    picked.extend(rest)
    picked.sort(key=lambda item: item["published"], reverse=True)
    return picked


def main() -> int:
    load_env()

    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = [feed for feed in json.load(f)["feeds"] if feed.get("enabled", True)]

    print(f"■ フィード取得（{len(feeds)}本）")
    fetched = collect_feed_items(feeds)
    print(f"  合計 {len(fetched)}件")

    existing = load_existing()
    existing_items = existing["items"]
    # 期限切れの見送り記録は捨てる（RSSから消えた記事をいつまでも覚えていても仕方がない）。
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    skipped_log = [
        record for record in existing.get("skipped", [])
        if isinstance(record, dict) and str(record.get("until", "")) > now_iso
    ]
    skipped_ids = {record["id"] for record in skipped_log}
    known_ids = {item["id"] for item in existing_items}
    known_urls = {canonical_url(item["url"]) for item in existing_items}
    known_titles = [normalize_title(item.get("title_original", "")) for item in existing_items]

    now = dt.datetime.now(dt.timezone.utc)

    fetched.sort(key=lambda item: item["published"], reverse=True)

    new_items: list[dict] = []
    for item in fetched:
        published = parse_date(item["published"])
        cutoff = now - dt.timedelta(days=INTAKE_DAYS)
        if published is None or published < cutoff or published > now + dt.timedelta(hours=12):
            continue
        if item["id"] in known_ids or item["url"] in known_urls:
            continue
        if item["id"] in skipped_ids:
            continue
        if is_duplicate(item["title_original"], known_titles):
            continue
        known_ids.add(item["id"])
        known_urls.add(item["url"])
        known_titles.append(normalize_title(item["title_original"]))
        new_items.append(item)

    new_items.sort(key=lambda item: item["published"], reverse=True)

    print(f"■ 新着 {len(new_items)}件（重複と期間外を除外）")
    if len(new_items) > MAX_NEW_PER_RUN:
        print(f"  うち{MAX_NEW_PER_RUN}件を今回処理（残りは次回）")
        new_items = pick_for_run(new_items)

    enriched: list[dict] = []
    if new_items:
        enriched, rejected = enrich(new_items)
        print(f"  解説 {len(enriched)}件（守備範囲外と判定された分は除外）")
        written = {item["id"] for item in enriched}
        enriched = dedupe_stories(enriched, existing_items)
        enriched = drop_duplicate_stories(enriched, existing_items)
        print(f"  掲載対象 {len(enriched)}件")
        # 守備範囲外と重複は、次の実行でまた解説し直さないように覚えておく。
        until = (now + dt.timedelta(days=SKIP_MEMORY_DAYS)).isoformat()
        for article_key in rejected | (written - {item["id"] for item in enriched}):
            skipped_log.append({"id": article_key, "until": until})
        # 実際に載せる記事だけ元ページを見に行く（無駄なアクセスを増やさないため）。
        resolve_google_urls(enriched)
        fill_missing_images(enriched)

    # 既に載っている記事にも、あとから足した出典名の変換とブロックを後追いで効かせる。
    kept_existing = [
        {**item, "source": DOMAIN_NAMES.get(item["source"], item["source"])}
        for item in existing_items
        if item.get("genre") in GENRES
        and item.get("category") in CATEGORIES
        and item.get("one_line")
        and not any(blocked.lower() in item["source"].lower() for blocked in BLOCK_SOURCES)
    ]

    merged = enriched + kept_existing
    merged = [
        item
        for item in merged
        if (parse_date(item.get("published", "")) or now) >= now - dt.timedelta(days=KEEP_DAYS)
    ]
    merged.sort(key=lambda item: item["published"], reverse=True)
    merged = merged[:KEEP_MAX]

    # 以前の実行で画像が付かなかった記事を、少しずつ埋めていく。
    stale = [item for item in merged if not item.get("image")][:BACKFILL_PER_RUN]
    if stale:
        print("■ 既存記事のサムネイル補完")
        resolve_google_urls(stale)
        fill_missing_images(stale)

    merged = [to_public(item) for item in merged]

    payload = {
        "updated_at": now.astimezone(JST).isoformat(),
        "genres": GENRES,
        "categories": CATEGORIES,
        "regions": REGIONS,
        "sources": sorted({item["source"] for item in merged}),
        "skipped": skipped_log[-400:],
        "count": len(merged),
        "items": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    counts = {genre: sum(1 for item in merged if item["genre"] == genre) for genre in GENRES}
    print(f"■ 書き出し: {OUTPUT_PATH}（掲載 {len(merged)}件 / "
          + " ・ ".join(f"{genre} {count}" for genre, count in counts.items()) + "）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
