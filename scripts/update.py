#!/usr/bin/env python3
"""Mirror public Telegram channels (via their public web preview, t.me/s/<name>)
into Markdown files.

Output (written into --out):
  FEED.md                       one list-style file with all channels, newest posts first
  archive/<channel>/<date>.md   text-only archive, one file per day, kept forever
  media/<channel>/<id>_<n>.jpg  photos, deleted 24 hours after the post was published
  state.json                    bookkeeping (last seen post id, recent posts, photo ages)
  README.md                     short pointer to FEED.md

Videos are never downloaded: the post text plus a link to the post is shown.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jdatetime
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# ---- settings you may want to change -------------------------------------
FEED_MAX = 20            # newest posts per channel shown in FEED.md
FEED_TEXT_LIMIT = 1200   # characters of a post shown in FEED.md (archive keeps all)
PHOTO_TTL_HOURS = 24     # photos are deleted this long after the post was published
MAX_PAGES = 6            # max pages (about 20 posts each) fetched per channel per run
MAX_PHOTO_BYTES = 8 * 1024 * 1024
# ---------------------------------------------------------------------------

TEHRAN = timezone(timedelta(hours=3, minutes=30))
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


# ============================ small helpers ================================

def log(msg: str) -> None:
    print(msg, flush=True)


def fa(s) -> str:
    return str(s).translate(FA_DIGITS)


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_local(dt_utc: datetime) -> str:
    """Jalali date + time, Tehran time zone, Persian digits."""
    d = dt_utc.astimezone(TEHRAN)
    j = jdatetime.date.fromgregorian(date=d.date())
    return fa(f"{j.year:04d}/{j.month:02d}/{j.day:02d} {d:%H:%M}")


def parse_channels(path: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = re.sub(r"^https?://", "", line)
        line = re.sub(r"^(t\.me/s/|t\.me/|telegram\.me/|@)", "", line).strip("/")
        if not CHANNEL_RE.match(line):
            log(f"warning: skipping invalid channel name: {raw!r}")
            continue
        if line.lower() in seen:
            continue
        seen.add(line.lower())
        names.append(line)
    return names


# ======================= Telegram HTML -> Markdown =========================

def md_escape(s: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", s)


def _wrap(marker: str, inner: str) -> str:
    core = inner.strip()
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()):]
    return f"{lead}{marker}{core}{marker}{trail}"


def to_md(node: Tag) -> str:
    out: list[str] = []
    for ch in node.children:
        if isinstance(ch, NavigableString):
            out.append(md_escape(str(ch)))
            continue
        if not isinstance(ch, Tag):
            continue
        name = ch.name
        classes = ch.get("class") or []
        if name == "br":
            out.append("  \n")
        elif name == "a":
            href = (ch.get("href") or "").strip()
            label = to_md(ch)
            if href.startswith(("http://", "https://")):
                href = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
                out.append(f"[{label}]({href})")
            else:  # hashtag / relative links -> plain text
                out.append(label)
        elif name in ("b", "strong") and "emoji" not in classes:
            out.append(_wrap("**", to_md(ch)))
        elif name in ("i", "em") and "emoji" not in classes:
            out.append(_wrap("*", to_md(ch)))
        elif name in ("i", "b") and "emoji" in classes:
            out.append(ch.get_text())
        elif name == "tg-emoji":
            out.append(ch.get_text())
        elif name in ("s", "del", "strike"):
            out.append(_wrap("~~", to_md(ch)))
        elif name == "code":
            txt = ch.get_text().replace("`", "'")
            out.append(f"`{txt}`" if txt.strip() else "")
        elif name == "pre":
            for br in ch.find_all("br"):
                br.replace_with("\n")
            body = ch.get_text().strip("\n")
            out.append(f"\n\n```\n{body}\n```\n\n")
        else:  # spans, spoilers, blockquotes, unknown tags: keep their text
            out.append(to_md(ch))
    return "".join(out)


def clean_md(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"(?m)^[ \t]+$", "", text)       # whitespace-only lines -> empty
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(?m)^(\s*)(#{1,6}\s)", r"\1\\\2", text)   # '# x' must not become a heading
    return text.strip()


def shorten(text: str, limit: int) -> tuple[str, bool]:
    """Cut text at a line/space boundary without leaving a half-open link or bold."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    nl = cut.rfind("\n")
    sp = cut.rfind(" ")
    boundary = nl if nl > limit * 0.5 else sp
    if boundary > limit * 0.5:
        cut = cut[:boundary]
    for pat in (r"\[[^\]]*$", r"\]\([^)]*$"):
        m = re.search(pat, cut)
        if m:
            cut = cut[: m.start()]
            break
    cut = cut.rstrip()
    if cut.count("**") % 2:
        cut += "**"
    return cut, True


# ============================= page parsing ================================

def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts: list[dict] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue

        date_a = msg.select_one("a.tgme_widget_message_date")
        time_tag = date_a.select_one("time[datetime]") if date_a else None
        link = (date_a.get("href") if date_a else None) or f"https://t.me/{msg['data-post']}"
        ts = parse_iso(time_tag["datetime"]) if time_tag else datetime.now(timezone.utc)

        text_el = msg.select_one("div.tgme_widget_message_text.js-message_text")
        text = clean_md(to_md(text_el)) if text_el else ""

        photos: list[str] = []
        for a in msg.select("a.tgme_widget_message_photo_wrap"):
            m = re.search(r"url\(['\"]?([^'\")]+)", a.get("style", ""))
            if m:
                photos.append(m.group(1))

        video_links: list[str] = []
        for el in msg.select(
            '[class*="tgme_widget_message_video_player"],'
            '[class*="tgme_widget_message_roundvideo_player"]'
        ):
            href = el.get("href") or ""
            href = href if href.startswith("http") else link
            if href not in video_links:
                video_links.append(href)

        others: list[str] = []
        if msg.select_one('[class*="tgme_widget_message_document"]'):
            others.append("📎 فایل")
        if msg.select_one('[class*="tgme_widget_message_voice"]'):
            others.append("🎙 پیام صوتی")
        if msg.select_one('[class*="tgme_widget_message_poll"]'):
            others.append("📊 نظرسنجی")
        if msg.select_one('[class*="tgme_widget_message_sticker"]'):
            others.append("🎭 استیکر")

        fwd_el = msg.select_one(".tgme_widget_message_forwarded_from_name")
        fwd = fwd_el.get_text(strip=True) if fwd_el else ""

        posts.append({
            "id": post_id,
            "ts": ts.isoformat(),
            "link": link,
            "text": text,
            "photo_urls": photos,
            "photo_count": len(photos),
            "photo_files": [],
            "videos": video_links,
            "others": others,
            "fwd": fwd,
        })
    posts.sort(key=lambda p: p["id"])
    return posts


# ============================== networking =================================

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
    return s


def fetch_text(session: requests.Session, url: str) -> str:
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            last_err = RuntimeError(f"HTTP {r.status_code}")
            if r.status_code in (429, 500, 502, 503, 504):
                try:
                    wait = min(int(r.headers.get("Retry-After", "")), 60)
                except ValueError:
                    wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            break
        except requests.RequestException as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}: {last_err}")


def download_photo(session: requests.Session, url: str, dest: Path) -> bool:
    try:
        r = session.get(url, timeout=30, stream=True)
        if r.status_code != 200:
            return False
        data = bytearray()
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > MAX_PHOTO_BYTES:
                return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bytes(data))
        return True
    except requests.RequestException:
        return False


def fetch_new_posts(session: requests.Session, name: str, last_id: int | None) -> list[dict]:
    collected: dict[int, dict] = {}
    before: int | None = None
    for page in range(MAX_PAGES):
        url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
        posts = parse_page(fetch_text(session, url))
        if not posts:
            if page == 0:
                raise RuntimeError(
                    "no posts visible (wrong name, private channel, or web preview disabled)"
                )
            break
        for p in posts:
            collected[p["id"]] = p
        min_id = min(p["id"] for p in posts)
        if last_id is None or min_id <= last_id or min_id <= 1:
            break
        before = min_id
        time.sleep(1)
    new = [p for p in collected.values() if last_id is None or p["id"] > last_id]
    new.sort(key=lambda p: p["id"])
    return new


# ============================== state / files ==============================

def load_state(out: Path) -> dict:
    f = out / "state.json"
    if f.exists():
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
            st.setdefault("channels", {})
            st.setdefault("media", {})
            return st
        except json.JSONDecodeError:
            log("warning: state.json is corrupt, starting fresh")
    return {"channels": {}, "media": {}}


def save_state(out: Path, state: dict) -> None:
    (out / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def cleanup_media(out: Path, state: dict, now: datetime) -> int:
    """Delete photos older than PHOTO_TTL_HOURS (age = hours since the post, from state)."""
    cutoff = now - timedelta(hours=PHOTO_TTL_HOURS)
    media: dict[str, str] = state["media"]
    removed = 0
    for rel, ts in list(media.items()):
        if parse_iso(ts) < cutoff:
            (out / rel).unlink(missing_ok=True)
            del media[rel]
            removed += 1
    root = out / "media"
    if root.exists():
        for f in root.rglob("*"):
            if f.is_file() and f.relative_to(out).as_posix() not in media:
                f.unlink()
                removed += 1
        for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    return removed


# ============================== rendering ==================================

def photo_lines(p: dict, out: Path | None) -> list[str]:
    """Photos are embedded only while the file still exists."""
    lines: list[str] = []
    if out is not None:
        present = [f for f in p.get("photo_files", []) if (out / f).exists()]
        for f in present:
            lines.append(f"![عکس]({f})")
        if p.get("photo_count", 0) and not present:
            lines.append("🖼 *(عکس بعد از ۲۴ ساعت حذف شده)*")
    elif p.get("photo_count", 0):
        lines.append(f"🖼 {fa(p['photo_count'])} عکس")
    return lines


def other_media_lines(p: dict) -> list[str]:
    """Videos and other media are never downloaded: only a link to the post."""
    lines = [f"🎬 ویدیو: [مشاهده در تلگرام]({v})" for v in p.get("videos", [])]
    lines += [f"{o}: [مشاهده در تلگرام]({p['link']})" for o in p.get("others", [])]
    return lines


def render_post(p: dict, out: Path | None, *, limit: int | None, with_date: bool) -> str:
    when = fmt_local(parse_iso(p["ts"]))
    head = f"**🕒 {when}** · [پست {p['id']}]({p['link']})" if with_date else \
        f"**🕒 {when[-5:]}** · [پست {p['id']}]({p['link']})"
    parts = [head]
    if p.get("fwd"):
        parts.append(f"↪️ فوروارد از {md_escape(p['fwd'])}")
    parts.extend(photo_lines(p, out))
    text = p.get("text", "")
    if text:
        if limit:
            text, cut = shorten(text, limit)
            if cut:
                text += f"\n\n… [ادامه در تلگرام]({p['link']})"
        parts.append(text)
    parts.extend(other_media_lines(p))
    if not text and len(parts) == 1:
        parts.append(f"*(پست بدون متن)* [مشاهده در تلگرام]({p['link']})")
    return "\n\n".join(parts)


def append_archive(out: Path, key: str, posts: list[dict]) -> None:
    for p in posts:
        d = parse_iso(p["ts"]).astimezone(TEHRAN)
        f = out / "archive" / key / f"{d:%Y-%m-%d}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            j = jdatetime.date.fromgregorian(date=d.date())
            f.write_text(
                f"# {key} — {d:%Y-%m-%d} ({fa(f'{j.year}/{j.month:02d}/{j.day:02d}')})\n\n",
                encoding="utf-8",
            )
        with f.open("a", encoding="utf-8") as fh:
            fh.write(render_post(p, None, limit=None, with_date=False) + "\n\n---\n\n")


def write_feed(out: Path, state: dict, channels: list[str], now: datetime) -> None:
    lines = [
        "# 📡 فید کانال‌های تلگرام",
        "",
        f"آخرین بررسی: {fmt_local(now)} (به وقت تهران)",
        "",
        f"هر ساعت به‌روز می‌شود. از هر کانال {fa(FEED_MAX)} پست آخر نمایش داده می‌شود؛ "
        f"عکس‌ها {fa(PHOTO_TTL_HOURS)} ساعت می‌مانند، برای ویدیو فقط متن و لینک پست آمده است. "
        "متن همه‌ی پست‌ها در پوشه‌ی [archive](archive/) نگه‌داری می‌شود.",
        "",
        "## فهرست کانال‌ها",
        "",
    ]
    for name in channels:
        cs = state["channels"].get(name.lower(), {})
        recent = cs.get("recent", [])
        latest = f" · آخرین پست: {fmt_local(parse_iso(recent[0]['ts']))}" if recent else ""
        lines.append(f"- [{name}](#{name.lower()}){latest}")
    lines.append("")

    for name in channels:
        key = name.lower()
        cs = state["channels"].get(key, {})
        recent = cs.get("recent", [])
        links = f"[کانال در تلگرام](https://t.me/{name})"
        if (out / "archive" / key).is_dir():
            links += f" · [آرشیو متن](archive/{key}/)"
        lines += ["---", "", f"## {name}", "", links, ""]
        if not recent:
            lines += ["هنوز پستی دریافت نشده.", ""]
            continue
        for p in recent:
            lines += [render_post(p, out, limit=FEED_TEXT_LIMIT, with_date=True), "", "· · ·", ""]
    (out / "FEED.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_readme(out: Path) -> None:
    (out / "README.md").write_text(
        "# داده‌ی فید تلگرام\n\n"
        "این شاخه را ربات به‌صورت خودکار می‌سازد و دستی ویرایش نکنید.\n\n"
        "- [FEED.md](FEED.md) — آخرین پست‌های همه‌ی کانال‌ها\n"
        "- [archive/](archive/) — آرشیو متن پست‌ها به تفکیک کانال و روز\n",
        encoding="utf-8",
    )


# ================================== main ===================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channels", default="channels.txt")
    ap.add_argument("--out", default="out")
    args = ap.parse_args(argv)

    channels = parse_channels(args.channels)
    if not channels:
        log("error: no channels listed in " + args.channels)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    state = load_state(out)
    now = datetime.now(timezone.utc)
    session = make_session()
    ok = 0

    for name in channels:
        key = name.lower()
        cs = state["channels"].setdefault(key, {"last_id": None, "recent": []})
        cs["name"] = name
        try:
            new = fetch_new_posts(session, name, cs.get("last_id"))
        except Exception as e:  # one broken channel must not stop the others
            log(f"[{name}] FAILED: {e}")
            continue
        ok += 1

        photo_cutoff = now - timedelta(hours=PHOTO_TTL_HOURS)
        for p in new:
            ts = parse_iso(p["ts"])
            if ts >= photo_cutoff:
                for i, url in enumerate(p["photo_urls"], 1):
                    m = re.search(r"\.(jpe?g|png|webp)(?:\?|$)", url, re.I)
                    ext = m.group(1).lower() if m else "jpg"
                    rel = f"media/{key}/{p['id']}_{i}.{ext}"
                    if download_photo(session, url, out / rel):
                        p["photo_files"].append(rel)
                        state["media"][rel] = p["ts"]
            p.pop("photo_urls", None)

        if new:
            append_archive(out, key, new)
            merged = {p["id"]: p for p in cs.get("recent", [])}
            merged.update({p["id"]: p for p in new})
            cs["recent"] = sorted(merged.values(), key=lambda p: p["id"], reverse=True)[:FEED_MAX]
            cs["last_id"] = max(p["id"] for p in new)
        log(f"[{name}] {len(new)} new post(s)")

    if ok == 0:
        log("error: every channel failed; nothing written")
        return 1

    removed = cleanup_media(out, state, now)
    if removed:
        log(f"removed {removed} expired photo(s)")
    write_feed(out, state, channels, now)
    write_readme(out)
    save_state(out, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
