#!/usr/bin/env python3
"""
RedNote (Xiaohongshu) scraper: posts about a keyword (default "GrabFood")
plus each post's comments and the commenting users.

How it works
------------
It drives a real Chromium window with Playwright. You log in once (QR code
in the RedNote app); the login is kept in ./rednote_profile for later runs.
Instead of forging RedNote's signed API requests, the script listens to the
JSON responses the website itself loads while it scrolls:

  /api/sns/web/v1/search/notes    -> search results (posts)
  /api/sns/web/v1/feed            -> post detail (sometimes)
  /api/sns/web/v2/comment/page    -> top-level comments
  /api/sns/web/v2/comment/sub/page-> replies (with --replies)

Usage
-----
  pip install -r requirements.txt
  playwright install chromium

  # search by keyword
  python rednote_scraper.py --keyword GrabFood --max-posts 50

  # or scrape specific post links
  python rednote_scraper.py --urls "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."

  # or a text file with one link per line
  python rednote_scraper.py --url-file links.txt --replies

Output goes to ./output/<timestamp>/ : posts.csv, comments.csv, raw.json
"""

import argparse
import csv
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

from playwright.sync_api import sync_playwright

BASE = "https://www.xiaohongshu.com"


def sleep(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


class Collector:
    """Accumulates API responses captured from the page."""

    def __init__(self):
        self.search_items = {}  # note_id -> item
        self.search_has_more = True
        self.details = {}  # note_id -> note dict (feed api)
        self.comments = {}  # comment_id -> comment dict
        self.comment_has_more = {}  # note_id -> bool

    def on_response(self, resp):
        url = resp.url
        if "/api/sns/web/" not in url:
            return
        try:
            body = resp.json()
        except Exception:
            return
        if not isinstance(body, dict) or not body.get("success", True):
            return
        data = body.get("data") or {}

        if "/search/notes" in url:
            for it in data.get("items", []) or []:
                if it.get("model_type") == "note" and it.get("id"):
                    self.search_items.setdefault(it["id"], it)
            self.search_has_more = bool(data.get("has_more"))

        elif "/v1/feed" in url:
            for it in data.get("items", []) or []:
                card = it.get("note_card") or {}
                nid = it.get("id") or card.get("note_id")
                if nid:
                    self.details[nid] = card

        elif "/comment/page" in url or "/comment/sub/page" in url:
            for c in data.get("comments", []) or []:
                self._add_comment(c, parent_id="")
            note_id = parse_qs(urlparse(url).query).get("note_id", [""])[0]
            if "/comment/page" in url and note_id:
                self.comment_has_more[note_id] = bool(data.get("has_more"))

    def _add_comment(self, c, parent_id):
        cid = c.get("id")
        if not cid:
            return
        target = c.get("target_comment") or {}
        self.comments[cid] = {
            "comment_id": cid,
            "note_id": c.get("note_id", ""),
            "parent_comment_id": parent_id or target.get("id", ""),
            "content": c.get("content", ""),
            "like_count": c.get("like_count", ""),
            "sub_comment_count": c.get("sub_comment_count", ""),
            "ip_location": c.get("ip_location", ""),
            "create_time": ms_to_iso(c.get("create_time")),
            "user_id": (c.get("user_info") or {}).get("user_id", ""),
            "user_nickname": (c.get("user_info") or {}).get("nickname", ""),
            "user_profile_url": f"{BASE}/user/profile/{(c.get('user_info') or {}).get('user_id', '')}",
        }
        for sc in c.get("sub_comments", []) or []:
            self._add_comment(sc, parent_id=cid)


def wait_for_login(page):
    page.goto(BASE, wait_until="domcontentloaded")
    sleep(3, 4)
    for _ in range(300):  # up to ~10 minutes
        logged_in = page.evaluate(
            """() => {
                try {
                  const u = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user;
                  const v = u && (u.loggedIn && (u.loggedIn.value ?? u.loggedIn._value ?? u.loggedIn));
                  if (v === true) return true;
                } catch (e) {}
                return !!document.querySelector('.user.side-bar-component, .side-bar .user');
            }"""
        )
        if logged_in:
            print("[+] Logged in.")
            return
        if _ == 0:
            print("[!] Please log in in the browser window (scan the QR code with the RedNote app)...")
        time.sleep(2)
    raise SystemExit("Login not detected, aborting.")


def search_posts(page, col, keyword, max_posts):
    url = f"{BASE}/search_result?keyword={quote(keyword)}&source=web_search_result_notes&type=51"
    print(f"[*] Searching: {keyword}")
    page.goto(url, wait_until="domcontentloaded")
    sleep(3, 5)
    stale = 0
    while len(col.search_items) < max_posts and stale < 6:
        before = len(col.search_items)
        page.mouse.wheel(0, random.randint(2500, 4000))
        sleep(2, 3.5)
        if len(col.search_items) == before:
            stale += 1
            if not col.search_has_more:
                break
        else:
            stale = 0
        print(f"    posts found: {len(col.search_items)}")
    items = list(col.search_items.values())[:max_posts]
    return [
        {
            "note_id": it["id"],
            "xsec_token": it.get("xsec_token", ""),
            "url": f"{BASE}/explore/{it['id']}?xsec_token={quote(it.get('xsec_token', ''))}&xsec_source=pc_search",
            "card": it.get("note_card") or {},
        }
        for it in items
    ]


def parse_url(u):
    m = re.search(r"/(?:explore|discovery/item|search_result)/([0-9a-f]{24})", u)
    if not m:
        return None
    token = parse_qs(urlparse(u).query).get("xsec_token", [""])[0]
    return {"note_id": m.group(1), "xsec_token": token, "url": u.strip(), "card": {}}


def read_initial_state_note(page, note_id):
    return page.evaluate(
        """(id) => {
            try {
              const m = window.__INITIAL_STATE__.note.noteDetailMap;
              const map = m.value ?? m._value ?? m;
              const n = map[id] && map[id].note;
              return n ? JSON.parse(JSON.stringify(n)) : null;
            } catch (e) { return null; }
        }""",
        note_id,
    )


def scroll_comments(page, col, note_id, max_comments, replies):
    def count():
        return sum(1 for c in col.comments.values() if c["note_id"] == note_id)

    stale = 0
    while count() < max_comments and stale < 5:
        before = count()
        page.evaluate(
            """() => {
                const s = document.querySelector('.note-scroller') ||
                          document.querySelector('.interaction-container') ||
                          document.scrollingElement;
                s.scrollTop = s.scrollHeight;
            }"""
        )
        if replies:
            for el in page.query_selector_all(".show-more"):
                try:
                    if "展开" in (el.inner_text() or ""):
                        el.click(timeout=1500)
                        sleep(0.8, 1.5)
                except Exception:
                    pass
        sleep(1.8, 3)
        if page.query_selector(".end-container") and count() == before:
            break  # "THE END" marker shown
        if count() == before:
            stale += 1
            if col.comment_has_more.get(note_id) is False and not replies:
                break
        else:
            stale = 0
    return count()


def normalise_post(note_id, url, detail, card):
    d = detail or {}
    user = d.get("user") or card.get("user") or {}
    inter = d.get("interactInfo") or d.get("interact_info") or card.get("interact_info") or {}
    g = lambda *keys: next((inter.get(k) for k in keys if inter.get(k) not in (None, "")), "")
    return {
        "note_id": note_id,
        "url": url,
        "type": d.get("type") or card.get("type", ""),
        "title": d.get("title") or card.get("display_title", ""),
        "description": d.get("desc", ""),
        "tags": ", ".join(t.get("name", "") for t in d.get("tagList", d.get("tag_list", [])) or []),
        "publish_time": ms_to_iso(d.get("time")),
        "ip_location": d.get("ipLocation") or d.get("ip_location", ""),
        "author_id": user.get("userId") or user.get("user_id", ""),
        "author_nickname": user.get("nickname") or user.get("nick_name", ""),
        "liked_count": g("likedCount", "liked_count"),
        "collected_count": g("collectedCount", "collected_count"),
        "comment_count": g("commentCount", "comment_count"),
        "shared_count": g("shareCount", "shared_count"),
    }


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel shows Chinese correctly
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Scrape RedNote posts + comments")
    ap.add_argument("--keyword", default="GrabFood")
    ap.add_argument("--urls", nargs="*", default=[], help="specific post links")
    ap.add_argument("--url-file", help="text file, one post link per line")
    ap.add_argument("--max-posts", type=int, default=30)
    ap.add_argument("--max-comments", type=int, default=200, help="per post")
    ap.add_argument("--replies", action="store_true", help="also expand replies to comments")
    ap.add_argument("--headless", action="store_true", help="only after you've logged in once")
    ap.add_argument("--profile", default="rednote_profile")
    args = ap.parse_args()

    out = Path("output") / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    col = Collector()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            args.profile, headless=args.headless, viewport={"width": 1366, "height": 900}, locale="zh-CN"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", col.on_response)

        wait_for_login(page)

        links = list(args.urls)
        if args.url_file:
            links += [l for l in Path(args.url_file).read_text(encoding="utf-8").splitlines() if l.strip()]
        targets = [t for t in (parse_url(u) for u in links) if t]
        if not targets:
            targets = search_posts(page, col, args.keyword, args.max_posts)
        print(f"[*] {len(targets)} posts to scrape")

        posts = []
        for i, t in enumerate(targets, 1):
            nid = t["note_id"]
            print(f"[{i}/{len(targets)}] {t['url']}")
            try:
                page.goto(t["url"], wait_until="domcontentloaded")
                sleep(2.5, 4)
                if "404" in page.url or "website-login" in page.url:
                    print("    ! post unavailable (deleted, private, or missing xsec_token)")
                    continue
                detail = col.details.get(nid) or read_initial_state_note(page, nid)
                n = scroll_comments(page, col, nid, args.max_comments, args.replies)
                posts.append(normalise_post(nid, t["url"], detail, t["card"]))
                print(f"    title: {posts[-1]['title'][:50]!r}  comments captured: {n}")
            except Exception as e:
                print(f"    ! error: {e}")
            sleep(3, 6)  # be polite; going faster risks the account being rate-limited

        ctx.close()

    comments = list(col.comments.values())
    write_csv(out / "posts.csv", posts)
    write_csv(out / "comments.csv", comments)
    (out / "raw.json").write_text(
        json.dumps({"posts": posts, "comments": comments}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    users = {c["user_id"] for c in comments if c["user_id"]}
    print(f"\n[+] Done: {len(posts)} posts, {len(comments)} comments from {len(users)} users -> {out}/")


if __name__ == "__main__":
    main()
