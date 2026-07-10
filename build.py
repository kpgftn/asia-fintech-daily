"""Asia Fintech Daily — one page per requirement sheet.

Reads every configs/*.yml (a person's transcribed requirement sheet), pulls
their jurisdictions' news from Google News RSS + direct feeds, tags items with
keyword topic chips, clusters same-story-different-outlet duplicates, and asks
Claude Haiku (one call/day, ~cents) to pick the day's 8-12 signal items with a
one-line "why". The firehose stays on the page below the signal — triage, not
truncation. GitHub Actions runs this every morning; GitHub Pages serves it.

Run: pip install feedparser pyyaml && python build.py
(ANTHROPIC_API_KEY optional — without it the page builds signal-less.)
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
SGT = timezone(timedelta(hours=8))
MAX_AGE_H = 48
ARCHIVE_DAYS_SHOWN = 14

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"

feedparser.USER_AGENT = "asia-fintech-daily/0.1 (GFTN internal POC)"


# ---------------------------------------------------------------- fetch

_feed_cache: dict[str, list] = {}


def fetch(url: str) -> list:
    if url not in _feed_cache:
        try:
            _feed_cache[url] = feedparser.parse(url).entries or []
        except Exception:
            _feed_cache[url] = []
        time.sleep(0.3)
    return _feed_cache[url]


def gnews_query(q: str) -> str:
    return GNEWS.format(q=urllib.parse.quote(q))


def entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def entry_source(e) -> str:
    src = e.get("source", {})
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    m = re.search(r" - ([^-]+)$", e.get("title", ""))
    return m.group(1).strip() if m else ""


def clean_title(e) -> str:
    title = e.get("title", "").strip()
    src = entry_source(e)
    if src and title.endswith(f" - {src}"):
        title = title[: -len(src) - 3].strip()
    return title


# ---------------------------------------------------------------- assemble

def norm_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())[:80]


# Social platforms syndicate raw posts into Google News results — never news.
BLOCKED_HOSTS = ("facebook.com", "instagram.com", "youtube.com", "reddit.com",
                 "tiktok.com", "x.com", "twitter.com")

# Leadership items must actually be about the financial world, not cabinet
# reshuffles or fashion weeks that happen to contain "steps down".
FINANCE_WORDS = ("bank", "regulat", "fintech", "financ", "monetary", "securities",
                 "exchange", "insurance", "payment", "capital market")


def blocked(e) -> bool:
    host = urllib.parse.urlparse(e.get("link", "")).netloc.lower()
    src = entry_source(e).lower()
    return any(b in host or b in src for b in BLOCKED_HOSTS)


def marker_match(text_low: str, markers: list[str]) -> bool:
    """Word-boundary match for short acronyms ("MAS" must not hit "Christmas"),
    plain substring for longer names."""
    for m in markers:
        if len(m) <= 4:
            if re.search(rf"\b{re.escape(m)}\b", text_low):
                return True
        elif m in text_low:
            return True
    return False


def tag_topics(title: str, topics: dict) -> list[str]:
    low = f" {title.lower()} "
    return [name for name, kws in topics.items()
            if any(str(k).lower() in low for k in kws)]


def collect_for(cfg: dict) -> dict:
    """Fetch + tag + dedupe one person's items. Returns render-ready data."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
    window = cfg.get("window", "when:2d")

    def quoted(terms):  # multi-word terms must be quoted or Google News splits them
        return " OR ".join(f'"{t}"' if " " in str(t) else str(t) for t in terms)

    fintech_terms = " OR ".join(cfg["fintech_terms"])
    lead_terms = quoted(cfg["leadership_terms"])

    seen: set[str] = set()
    by_jur: dict[str, list[dict]] = {j: [] for j in cfg["jurisdictions"]}
    leadership: list[dict] = []
    regional: list[dict] = []
    jur_markers = {j: [j.lower()] + [r.lower() for r in s.get("regulators", [])]
                   for j, s in cfg["jurisdictions"].items()}
    all_markers = [m for marks in jur_markers.values() for m in marks]

    def add(e, jurisdiction: str | None, is_leadership: bool):
        t = entry_time(e)
        if not t or t < cutoff or blocked(e):
            return
        title = clean_title(e)
        key = norm_key(title)
        if not title or key in seen:
            return
        seen.add(key)
        if len(title) > 180:
            title = title[:177].rstrip() + "…"
        item = {
            "title": title,
            "url": e.get("link", ""),
            "source": entry_source(e),
            "time": t.astimezone(SGT).strftime("%d %b, %H:%M"),
            "ts": t,
            "topics": tag_topics(title, cfg["topics"]),
        }
        low = title.lower()
        is_financial = any(w in low for w in FINANCE_WORDS)
        on_patch = marker_match(low, all_markers)  # google treats "Jurisdiction" as a hint, we enforce it
        if (is_leadership or "Leadership" in item["topics"]) and is_financial and on_patch:
            leadership.append(item)
        elif is_leadership:
            return  # off-patch or non-finance leadership story — drop it
        elif jurisdiction:
            by_jur[jurisdiction].append(item)
        else:
            regional.append(item)

    for jur, spec in cfg["jurisdictions"].items():
        regs = " OR ".join(f'"{r}"' for r in spec.get("regulators", []))
        general = f'({fintech_terms}) "{jur}" {window}'
        for e in fetch(gnews_query(general)):
            add(e, jur, False)
        lead_q = (f'({lead_terms}) (bank OR regulator OR fintech OR "financial services"'
                  f' OR {regs or "monetary"}) "{jur}" {window}')
        for e in fetch(gnews_query(lead_q)):
            add(e, jur, True)

    for feed in cfg.get("feeds", []):
        for e in fetch(feed["url"]):
            title_low = clean_title(e).lower()
            hit = next((j for j, marks in jur_markers.items()
                        if marker_match(title_low, marks)), None)
            add(e, hit, False)

    cap = cfg.get("max_per_jurisdiction", 40)
    data = {
        "leadership": cluster(leadership)[:cap],
        "by_jur": {j: cluster(items)[:cap] for j, items in by_jur.items()},
        "regional": cluster(regional)[:cap],
    }
    data["signal"] = pick_signal(data)
    return data


# ---------------------------------------------------------------- cluster

def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3}


def cluster(items: list[dict]) -> list[dict]:
    """Collapse same-story-different-outlet items. Canonical = longest title;
    cluster size becomes a ranking signal (three outlets > one outlet)."""
    clusters: list[dict] = []
    for it in sorted(items, key=lambda x: x["ts"], reverse=True):
        toks = _tokens(it["title"])
        home = None
        for c in clusters:
            inter = len(toks & c["toks"])
            union = len(toks | c["toks"]) or 1
            if inter / union > 0.5:
                home = c
                break
        if home:
            home["sources"] += 1
            home["toks"] |= toks
            if len(it["title"]) > len(home["item"]["title"]):
                it["sources"], it["toks"] = home["sources"], home["toks"]
                home["item"] = it
        else:
            it["sources"] = 1
            clusters.append({"item": it, "toks": toks, "sources": 1})
    for c in clusters:
        c["item"]["sources"] = c["sources"]
    # multi-outlet stories first, then recency
    return sorted((c["item"] for c in clusters),
                  key=lambda x: (-x["sources"], -x["ts"].timestamp()))


# ---------------------------------------------------------------- signal (the one LLM call)

SIGNAL_MODEL = os.environ.get("SIGNAL_MODEL", "claude-haiku-4-5-20251001")


def pick_signal(data: dict) -> list[dict]:
    """One Haiku call over the day's headlines -> 8-12 items with a one-line why.
    Fails soft: no key or a bad response just means no signal section today."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("  (no ANTHROPIC_API_KEY — skipping signal section)")
        return []

    pool: list[dict] = []
    for section, items in [("Leadership", data["leadership"]), ("Regional", data["regional"])]:
        pool += [{**i, "_sec": section} for i in items]
    for jur, items in data["by_jur"].items():
        pool += [{**i, "_sec": jur} for i in items]
    lines = [f'{n}|{i["_sec"]}|{i["source"] or "?"}|{i["title"]}' for n, i in enumerate(pool)]

    prompt = (
        "You triage a daily fintech news digest for GFTN, a Singapore-based fintech-events "
        "and advisory organisation working with central banks and regulators across Asia "
        "and Central Asia. From the numbered headlines below, pick the 8-12 items a GFTN "
        "analyst most needs to know today. Prefer: regulator/central-bank actions, licensing, "
        "CBDC and payments-infrastructure moves, major funding, leadership changes. Avoid: "
        "consumer promos, market-price chatter, opinion listicles. NEVER pick two items about "
        "the same underlying event, even from different angles. Spread picks across "
        "jurisdictions where the news genuinely merits it — don't let one market dominate.\n"
        "Return ONLY a JSON array: [{\"id\": <number>, \"why\": \"<one line, max 18 words, "
        "why this matters to GFTN's world>\"}]\n\n" + "\n".join(lines)
    )
    body = json.dumps({
        "model": SIGNAL_MODEL, "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = json.load(resp)["content"][0]["text"]
        picks = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
        out, picked_toks = [], []
        for p in picks:
            i = pool[int(p["id"])]
            toks = _tokens(i["title"])
            # safety net: the model still sometimes picks the same event twice
            if any(len(toks & pt) / (len(toks | pt) or 1) > 0.3 for pt in picked_toks):
                continue
            picked_toks.append(toks)
            out.append({**i, "why": str(p.get("why", ""))[:160], "jur": i["_sec"]})
            if len(out) == 12:
                break
        print(f"  signal: {len(out)} items picked by {SIGNAL_MODEL}")
        return out
    except Exception as e:
        print(f"  signal failed softly: {e}")
        return []


# ---------------------------------------------------------------- render

CSS = """
:root { --navy:#0f1f3d; --gold:#c9a84c; --bg:#f4f6fa; --card:#fff; --ink:#1a1a2e;
        --muted:#5b6474; --line:#e3e7ef; --chipbg:#eef2f9; --chipink:#2c3e63; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#10141d; --card:#181e2b; --ink:#e8eaf0; --muted:#98a0b3;
          --line:#262e40; --chipbg:#232b3d; --chipink:#b9c4dd; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
header { background:var(--navy); color:#fff; padding:26px 20px; }
header .inner, main { max-width:860px; margin:0 auto; }
header h1 { margin:0; font-size:24px; } header h1 span { color:var(--gold); }
header p { margin:6px 0 0; color:#cdd6ea; font-size:13px; }
main { padding:20px; }
h2 { font-size:17px; margin:28px 0 10px; padding-bottom:6px;
     border-bottom:2px solid var(--gold); display:flex; justify-content:space-between; align-items:baseline; }
h2 .count { font-size:12px; font-weight:400; color:var(--muted); }
.item { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:10px 14px; margin-bottom:8px; }
.item a { color:inherit; text-decoration:none; font-weight:600; font-size:14.5px; }
.item a:hover { text-decoration:underline; }
.meta { font-size:12px; color:var(--muted); margin-top:4px; }
.chip { display:inline-block; background:var(--chipbg); color:var(--chipink);
        font-size:11px; border-radius:9px; padding:1px 8px; margin-left:6px; }
.lead .item { border-left:3px solid var(--gold); }
.signal .item { border-left:3px solid var(--gold); background:linear-gradient(0deg, var(--card), var(--card)); }
.why { font-size:13px; color:var(--muted); margin-top:3px; font-style:italic; }
.chip.multi { background:var(--gold); color:#2b230a; }
.chip.jur { border:1px solid var(--line); background:transparent; }
details { margin-top:4px; }
details summary { cursor:pointer; color:var(--muted); font-size:13px; padding:6px 2px; }
.empty { color:var(--muted); font-size:13.5px; font-style:italic; }
footer { max-width:860px; margin:30px auto; padding:0 20px 40px; font-size:12px;
         color:var(--muted); border-top:1px solid var(--line); padding-top:14px; }
footer a { color:var(--muted); }
nav.arch { font-size:12px; margin-top:8px; } nav.arch a { margin-right:10px; }
"""


FOLD_AT = 8  # jurisdiction sections show this many; the rest folds into "show all"


def _item_html(it: dict, why: bool = False) -> str:
    chips = "".join(f'<span class="chip">{html.escape(t)}</span>'
                    for t in it["topics"] if t != "Leadership")
    if it.get("sources", 1) > 1:
        chips += f'<span class="chip multi">+{it["sources"] - 1} more source{"s" if it["sources"] > 2 else ""}</span>'
    if why and it.get("jur"):
        chips += f'<span class="chip jur">{html.escape(it["jur"])}</span>'
    src = f'{html.escape(it["source"])} · ' if it["source"] else ""
    why_html = (f'<div class="why">{html.escape(it["why"])}</div>'
                if why and it.get("why") else "")
    return (f'<div class="item"><a href="{html.escape(it["url"])}" target="_blank" '
            f'rel="noopener">{html.escape(it["title"])}</a>{why_html}'
            f'<div class="meta">{src}{it["time"]} SGT{chips}</div></div>')


def _section(title: str, items: list, cls: str = "", fold: bool = False) -> str:
    if not items:
        return (f'<h2>{html.escape(title)}</h2>'
                f'<p class="empty">Nothing in the last 48 hours.</p>')
    head = (f'<h2>{html.escape(title)} <span class="count">{len(items)} items</span></h2>')
    if not fold or len(items) <= FOLD_AT:
        return head + f'<div class="{cls}">{"".join(_item_html(i) for i in items)}</div>'
    top = "".join(_item_html(i) for i in items[:FOLD_AT])
    rest = "".join(_item_html(i) for i in items[FOLD_AT:])
    return (head + f'<div class="{cls}">{top}'
            f'<details><summary>show all {len(items)}</summary>{rest}</details></div>')


def render(cfg: dict, data: dict, archive_links: list[str]) -> str:
    now = datetime.now(SGT)
    total = (len(data["leadership"]) + len(data["regional"])
             + sum(len(v) for v in data["by_jur"].values()))
    sections = []
    if data.get("signal"):
        sections.append('<h2>Today\'s signal <span class="count">picked for you, one line of why each</span></h2>'
                        '<div class="signal">'
                        + "".join(_item_html(i, why=True) for i in data["signal"]) + "</div>")
    sections.append(_section("Leadership changes", data["leadership"], "lead"))
    sections += [_section(j, items, fold=True) for j, items in data["by_jur"].items()]
    sections.append(_section("Regional & global", data["regional"], fold=True))
    arch = "".join(f'<a href="archive/{d}.html">{d}</a>' for d in archive_links)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asia Fintech Daily — {html.escape(cfg["name"])}</title>
<style>{CSS}</style></head><body>
<header><div class="inner">
  <h1>Asia Fintech <span>Daily</span></h1>
  <p>{now.strftime("%A %d %B %Y")} · {total} items · updated {now.strftime("%H:%M")} SGT ·
     built from {html.escape(cfg["name"])}'s requirement sheet</p>
</div></header>
<main>{"".join(sections)}</main>
<footer>
  Raw links, full firehose, no AI summaries — by request. Sources: Google News queries
  per jurisdiction + direct industry feeds. Refreshes every morning ~6:15am SGT.
  Want a feed built to your own requirements? Fill in the same requirement sheet.
  <nav class="arch">Past editions: {arch or "—"}</nav>
</footer>
</body></html>"""


# ---------------------------------------------------------------- main

def build_user(path: Path) -> str:
    cfg = yaml.safe_load(path.read_text())
    slug = cfg.get("slug", path.stem)
    out_dir = DOCS / slug
    (out_dir / "archive").mkdir(parents=True, exist_ok=True)

    data = collect_for(cfg)
    stamp = datetime.now(SGT).strftime("%Y-%m-%d")
    existing = sorted((out_dir / "archive").glob("*.html"), reverse=True)
    archive_links = [p.stem for p in existing if p.stem != stamp][:ARCHIVE_DAYS_SHOWN]

    page = render(cfg, data, archive_links)
    (out_dir / "index.html").write_text(page)
    (out_dir / "archive" / f"{stamp}.html").write_text(page)
    total = (len(data["leadership"]) + len(data["regional"])
             + sum(len(v) for v in data["by_jur"].values()))
    print(f"  {slug}: {total} items -> docs/{slug}/index.html")
    return slug


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    slugs = [build_user(p) for p in sorted((ROOT / "configs").glob("*.yml"))]
    links = "".join(f'<li><a href="{s}/">{s}</a></li>' for s in slugs)
    (DOCS / "index.html").write_text(
        f"<!doctype html><meta charset='utf-8'><title>Asia Fintech Daily</title>"
        f"<body style='font-family:sans-serif;padding:40px'><h1>Asia Fintech Daily</h1>"
        f"<p>One feed per requirement sheet.</p><ul>{links}</ul></body>")


if __name__ == "__main__":
    main()
