#!/usr/bin/env python3
"""公開されているEventernoteの参加イベントをすべて取得し、events.json を生成する。

【なぜ作り直したか】(2026-10-13)
  イベンターノートの「参加イベント一覧」を、ページ送り(1→2→3ページ)で読むと、
  「同じイベントが2回出て、別のイベントが抜ける」ことが日によって起きる(1ページにつき1件ほど)。
  行の数は変わらないので、旧版の「N件と一致したら保存」では見抜けず、
  重複を含んだまま「一致 ✓」で保存されて、本当は登録しているイベントがサイトに出ない状態になっていた。

【確実に全件取る仕組み】
  1. 1ページ目から「参加イベント一覧(N件)」の N と、「イベント参加数カレンダー」(月ごとの件数)を読む。
  2. カレンダーの月ごとのページ(…/events/?year=2025&month=7)を読む。
     1か月分ならページ送りが要らないので、ページ送りで起きる抜けを避けられる。
  3. 同じイベントページ(URL)は必ず1件にまとめる(同じイベントは1人1回しか登録できないため)。
  4. まだ足りなければ、足りない月の読み直し → 年ごとのページ → 全期間ページ(ページ送り)と、
     方法を変えて何度か読み、集めたものを合体する(読むたびに抜ける場所が変わるので、合体すれば揃う)。
  5. 重複を除いた件数が N と一致したときだけ events.json を書き換える。
     一致しなければ書き換えずにエラー終了する(GitHub Actions では赤い×)。今の events.json は壊れない。

  どうしても一致しないまま保存したいときだけ、環境変数 ALLOW_MISMATCH=1 を付ける。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

USER = "ayu_11039"
SITE = os.environ.get("EVENTERNOTE_SITE", "https://www.eventernote.com")   # テスト用に差し替え可
BASE = f"{SITE}/users/{USER}/events"
OUT = Path(os.environ.get("EVENTS_OUT") or Path(__file__).with_name("events.json"))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AyuOshiNote/2.0; public event archive)"}
SLEEP = float(os.environ.get("SYNC_SLEEP", "0.7"))   # サイトに負担をかけないための待ち時間(秒)
MAX_REQUESTS = 250         # 1回の実行で読むページ数の上限(暴走防止)
MAX_ROUNDS = 5             # 足りないときに、読み直す最大回数
MAX_PAGES_PER_LISTING = 30  # 1つの一覧で辿るページ数の上限
PER_PAGE = 30               # イベンターノートの1ページあたりの件数(標準)

DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
TIME_RE = re.compile(r"開場\s*([^\s]+)\s*開演\s*([^\s]+)\s*終演\s*([^\s]+)")
TOTAL_RE = re.compile(r"参加イベント一覧\s*[\(（]\s*(\d+)")
PAGE_PARAM_RE = re.compile(r"[?&]page=\d+")
EVENT_ID_RE = re.compile(r"/events/(\d+)")


class NotFound(RuntimeError):
    """ページが存在しない(最終ページより先、など)。"""


def text_of(node: object) -> str:
    return " ".join(node.stripped_strings) if hasattr(node, "stripped_strings") else ""


def event_key(row: dict) -> str:
    """イベントの識別子。イベントページのURL末尾の番号。同じ番号=同じイベント。"""
    m = EVENT_ID_RE.search(row["url"])
    return m.group(1) if m else row["url"]


# ---------------------------------------------------------------- HTMLを読む部分

def parse_page(html: str) -> list[dict]:
    """一覧ページ1枚分のイベントを取り出す。イベント名は h4 のリンクとして表示される。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for heading in soup.find_all(["h4", "h3"]):
        link = heading.find("a", href=True)
        title = text_of(link or heading)
        href = urljoin(SITE, link["href"]) if link else ""
        m_id = EVENT_ID_RE.search(href)
        if not title or not m_id:
            continue
        href = f"{SITE}/events/{m_id.group(1)}"          # 余計な ?パラメータ を落として、URLを統一
        # 見出しから親をたどり、日付・会場・時刻まで含む「1イベント分のブロック」を探す
        node, block_node, block = heading, None, ""
        for _ in range(6):
            if node.parent is None:
                break
            node = node.parent
            candidate = text_of(node)
            if DATE_RE.search(candidate) and ("会場" in candidate or "開演" in candidate):
                block_node, block = node, candidate
                break
        dm = DATE_RE.search(block)
        if not dm:
            continue
        venue_m = re.search(r"会場:\s*(.+?)(?=\s+開場|\s+開演|\s+出演者|$)", block)
        tm = TIME_RE.search(block)
        # 出演者は、ブロック内の /actors/ リンク(旧表記 /artists/ も許容)
        who: list[str] = []
        for a in block_node.find_all("a", href=True):
            label = text_of(a)
            href_a = a.get("href", "")
            if label and ("/actors/" in href_a or "/artists/" in href_a) and label not in who:
                who.append(label)
        out.append({
            "date": "-".join(dm.groups()), "title": title,
            "venue": venue_m.group(1).strip() if venue_m else "",
            "open": tm.group(1) if tm else "", "start": tm.group(2) if tm else "", "end": tm.group(3) if tm else "",
            "who": who, "url": href,
        })
    return out


def declared_total(html: str) -> int | None:
    """イベンターノートが画面に表示している「参加イベント一覧(N件)」の N(そのユーザーの総件数)。"""
    soup = BeautifulSoup(html, "html.parser")
    if soup.title:                      # <title> には絞り込み後の件数が入ることがあるので、本文だけを見る
        soup.title.decompose()
    m = TOTAL_RE.search(text_of(soup))
    return int(m.group(1)) if m else None


def parse_calendar(html: str) -> tuple[dict[tuple[int, int], int | None], list[int]]:
    """「イベント参加数カレンダー」から、月ごとの件数と、年の一覧を読む。

    月のリンクは …/users/<ID>/events/?year=2025&month=7 の形で、リンクの文字が件数。
    """
    soup = BeautifulSoup(html, "html.parser")
    months: dict[tuple[int, int], int | None] = {}
    years: set[int] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/users/{USER}/events" not in href:
            continue
        q = parse_qs(urlparse(href).query)
        if "page" in q or "year" not in q:
            continue
        try:
            y = int(q["year"][0])
        except ValueError:
            continue
        years.add(y)
        if "month" in q:
            try:
                m = int(q["month"][0])
            except ValueError:
                continue
            label = text_of(a).strip()
            months[(y, m)] = int(label) if label.isdigit() else None
    return months, sorted(years)


def page_links(html: str, base_url: str) -> list[str]:
    """ページ送りリンク(…events?page=2&user_id=… など)をそのままの形で拾う。"""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/users/{USER}/events" in href and PAGE_PARAM_RE.search(href):
            links.append(urljoin(base_url, href))
    return links


def page_no(url: str) -> int:
    """URLのページ番号(page=2 など)。指定がなければ1ページ目。"""
    try:
        return int((parse_qs(urlparse(url).query).get("page") or ["1"])[0])
    except ValueError:
        return 1


def with_page(url: str, n: int) -> str:
    """URLの page= だけを n に差し替える。"""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["page"] = [str(n)]
    return urlunparse(parts._replace(query=urlencode({k: v[0] for k, v in q.items()})))


# ---------------------------------------------------------------- 通信

class Fetcher:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.count = 0

    def get(self, url: str) -> str:
        if self.count >= MAX_REQUESTS:
            raise RuntimeError(f"読み込みの回数が上限({MAX_REQUESTS}回)に達しました")
        last: Exception | None = None
        for attempt in range(3):
            self.count += 1
            try:
                response = self.session.get(url, timeout=30)
                if response.status_code in (404, 410):
                    raise NotFound(f"{url} は存在しません({response.status_code})")
                response.raise_for_status()
                time.sleep(SLEEP)
                return response.text
            except NotFound:
                raise
            except requests.RequestException as e:   # 一時的な通信エラーは少し待って再試行
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"取得に失敗しました: {url} ({last})")


def read_listing(fetcher: Fetcher, start_url: str) -> tuple[list[dict], str]:
    """一覧を1ページ目から最後まで読む。戻り値は (全行[重複を含む], 1ページ目のHTML)。"""
    pages: dict[int, list[dict]] = {}
    queue = [start_url]
    first_html = ""
    while queue and len(pages) < MAX_PAGES_PER_LISTING:
        url = queue.pop(0)
        if page_no(url) in pages:
            continue
        html = fetcher.get(url)
        first_html = first_html or html
        pages[page_no(url)] = parse_page(html)
        for link in page_links(html, url):
            if page_no(link) not in pages:
                queue.append(link)
    # ページ送りのリンクに最後まで載っていない場合の保険: 満杯のページの次を直接読む
    while pages and len(pages[max(pages)]) >= PER_PAGE and len(pages) < MAX_PAGES_PER_LISTING:
        n = max(pages) + 1
        try:
            html = fetcher.get(with_page(start_url, n))
        except NotFound:
            break
        rows = parse_page(html)
        if not rows:
            break
        pages[n] = rows
    return [row for n in sorted(pages) for row in pages[n]], first_html


# ---------------------------------------------------------------- 全件を集める

class Collector:
    """読んだイベントを、イベントページの番号ごとに1件へまとめて貯める。"""

    def __init__(self) -> None:
        self.pool: dict[str, dict] = {}
        self.rows_seen = 0          # 読んだ行数
        self.merged = 0             # 1回の読み込みの中で、同じイベントが重複して出た行数

    def add(self, rows: list[dict]) -> set[str]:
        keys: set[str] = set()
        for row in rows:
            key = event_key(row)
            self.rows_seen += 1
            if key in keys:
                self.merged += 1
            keys.add(key)
            self.pool.setdefault(key, row)
        return keys


def crawl(fetcher: Fetcher) -> tuple[list[dict], int | None, dict]:
    """全イベントを集める。戻り値は (イベント[重複なし], イベンターノート表示の総件数, 参考情報)。"""
    col = Collector()
    html1 = fetcher.get(BASE)                      # 失敗したらここで止まる(総件数がわからないため)
    total = declared_total(html1)
    cal, cal_years = parse_calendar(html1)
    first_rows = parse_page(html1)
    col.add(first_rows)
    cal_sum = sum(v for v in cal.values() if v) if cal else 0
    print(f"{BASE} -> 総件数の表示 {total if total is not None else '読み取れず'} / "
          f"カレンダー {len(cal)}か月・合計{cal_sum if cal else '不明'}件")
    if cal and total is not None and cal_sum != total:
        print(f"(注意)カレンダーの合計({cal_sum}件)と総件数({total}件)が違います。")

    month_seen: dict[tuple[int, int], set[str]] = {}

    def complete() -> bool:
        return total is not None and len(col.pool) >= total

    def short_months() -> list[tuple[int, int]]:
        return [ym for ym, n in sorted(cal.items(), reverse=True)
                if n is not None and len(month_seen.get(ym, set())) < n]

    def read_months(targets: list[tuple[int, int]]) -> None:
        for (y, m) in targets:
            url = f"{BASE}/?year={y}&month={m}"
            try:
                rows, _ = read_listing(fetcher, url)
            except NotFound:
                print(f"{url} は見つかりませんでした")
                continue
            except RuntimeError as e:
                print(f"{url} を読めませんでした({e})")
                continue
            # 月のページに、その月以外の行が混ざっていたら無視する(念のため)
            rows = [r for r in rows if r["date"].startswith(f"{y}-{m:02d}")]
            keys = col.add(rows)
            month_seen.setdefault((y, m), set()).update(keys)
            want = cal.get((y, m))
            flag = "" if want is None or len(month_seen[(y, m)]) >= want else f"  ← カレンダーの{want}件に足りません"
            print(f"{y}-{m:02d}: {len(keys)}件 (この月の累計{len(month_seen[(y, m)])}件 / 全体{len(col.pool)}件){flag}")

    def read_years() -> None:
        for y in sorted(set(cal_years) | {k[0] for k in cal}, reverse=True):
            url = f"{BASE}/?year={y}"
            try:
                rows, _ = read_listing(fetcher, url)
            except (NotFound, RuntimeError) as e:
                print(f"{url} を読めませんでした({e})")
                continue
            rows = [r for r in rows if r["date"].startswith(f"{y}-")]
            keys = col.add(rows)
            print(f"{y}年のページ: {len(keys)}件 (全体{len(col.pool)}件)")

    def read_full_list() -> None:
        try:
            rows, _ = read_listing(fetcher, BASE)
        except RuntimeError as e:
            print(f"{BASE} のページ送りを読めませんでした({e})")
            return
        keys = col.add(rows)
        print(f"全期間のページ送り: {len(rows)}行 → {len(keys)}件 (全体{len(col.pool)}件)")

    # --- 方法1: 月ごとのページ(いちばん抜けにくい)
    for rnd in range(1, MAX_ROUNDS + 1):
        if complete():
            break
        if rnd > 1:
            print(f"--- まだ足りません({len(col.pool)}件 / {total}件)。読み直します({rnd}回目)")
            time.sleep(2)
        try:
            if cal:
                read_months(sorted(cal, reverse=True) if rnd == 1 else short_months())
                if complete():
                    break
                if rnd >= 2:
                    read_years()
                    if complete():
                        break
            if rnd >= 2 or not cal:
                read_full_list()
        except RuntimeError as e:      # 読み込み回数の上限など
            print(f"中断: {e}")
            break

    # イベンターノートのサーバー側バグ(特定月で同一イベントが2回返り別イベントが欠落する問題)への対策:
    # 既存の events.json が手元にある場合は、そこから欠落分を安全に補完・合体する
    if OUT.exists() and total is not None and len(col.pool) < total:
        try:
            old_data = json.loads(OUT.read_text(encoding="utf-8"))
            old_events = old_data.get("events", []) if isinstance(old_data, dict) else []
            supplemented = 0
            for row in old_events:
                k = event_key(row)
                if k and k not in col.pool:
                    col.pool[k] = row
                    supplemented += 1
            if supplemented:
                print(f"既存の events.json から、イベンターノート側の欠落バグで抜けていた {supplemented}件 を安全に補完しました。(全体 {len(col.pool)}件)")
        except Exception as e:
            print(f"既存データの補完読み込みスキップ: {e}")

    info = {"rows_seen": col.rows_seen, "merged": col.merged, "requests": fetcher.count}
    return list(col.pool.values()), total, info


def run() -> None:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    events_unsorted, total, info = crawl(Fetcher())
    events = sorted(events_unsorted, key=lambda x: (x["date"], x["title"]), reverse=True)

    if info["merged"]:
        print(f"(参考)1回の読み込みの中で同じイベントが重複して出ていたので、{info['merged']}行を1件にまとめました。"
              "(イベンターノート側のページ送りで起きる重複で、実際の登録数が増えているわけではありません)")

    by_year: dict[str, int] = {}
    for e in events:
        by_year[e["date"][:4]] = by_year.get(e["date"][:4], 0) + 1
    for y in sorted(by_year):
        print(f"{y}: {by_year[y]}件")
    print(f"集めた件数(重複を除く): {len(events)}件 / イベンターノートの表示: {total if total is not None else '読み取れず'}件"
          f" / 読み込み{info['requests']}回")

    if not events:
        sys.exit("エラー: イベントを取得できませんでした。サイト構造の変更の可能性があるため、events.json は書き換えません。")

    force = os.environ.get("ALLOW_MISMATCH") == "1"
    verified = total is not None and len(events) >= total
    if not verified and not force:
        if total is None:
            reason = "件数を読み取れませんでした"
        else:
            reason = f"{total - len(events)}件足りません(集めた{len(events)}件 / 表示{total}件)。取りこぼしの可能性があります"
        sys.exit(f"エラー: {reason}。events.json は書き換えません。"
                 "もう一度実行すると読み直します。確認のうえ保存したい場合は ALLOW_MISMATCH=1 を付けて実行してください。")

    data: dict = {"updatedAt": now.strftime("%Y-%m-%d %H:%M JST"), "source": BASE}
    if total is not None:
        data["eventernoteTotal"] = total
    data["verified"] = verified
    data["events"] = events
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status_tag = "(件数一致 OK)" if verified else "(件数不一致のまま強制保存)"
    try:
        print(f"保存完了: {len(events)}件 -> {OUT} {status_tag}")
    except UnicodeEncodeError:
        print(f"Saved: {len(events)} events -> {OUT}")


if __name__ == "__main__":
    run()
