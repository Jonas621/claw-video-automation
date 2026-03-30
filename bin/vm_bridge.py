#!/usr/bin/env python3
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, parse, request
from xml.etree import ElementTree as ET

ENV_FILE = Path(os.environ.get("VM_BRIDGE_ENV_FILE", str(Path.home() / ".openclaw" / "vm_bridge.env")))
STATE_FILE = Path(os.environ.get("VM_BRIDGE_STATE_FILE", str(Path.home() / ".openclaw" / "vm_bridge_state.json")))
LOG_FILE = Path(os.environ.get("VM_BRIDGE_LOG_FILE", str(Path.home() / ".openclaw" / "logs" / "vm_bridge.log")))
DISCORD_API_BASE = "https://discord.com/api/v10"
AUTO_THEMES_BEGIN = "### AUTO_CURRENT_THEMES_BEGIN"
AUTO_THEMES_END = "### AUTO_CURRENT_THEMES_END"
AUTO_MANUAL_IDEA_BEGIN = "### AUTO_MANUAL_IDEA_BEGIN"
AUTO_MANUAL_IDEA_END = "### AUTO_MANUAL_IDEA_END"
AUTO_OUTPUT_RULES_BEGIN = "### AUTO_OUTPUT_FORMAT_RULES_BEGIN"
AUTO_OUTPUT_RULES_END = "### AUTO_OUTPUT_FORMAT_RULES_END"
BGM_TRACK_CHOICES = [
    "ambient.mp3",
    "lofi_soft_chill.mp3",
    "tense_dark_drone.mp3",
    "uplift_epic_hopeful_cinematic.mp3",
    "zen_man-background-loop-chill-techno-04-2485.mp3",
]
ALLOWED_CONTENT_MODES = [
    "Story Drama",
    "Fact Explainer",
    "Current-News Brief",
    "Mega-Build/Engineering Showcase",
    "Myth-busting",
]
VOICE_TRACK_CHOICES = [
    "en-US-AvaMultilingualNeural",
    "en-US-GuyNeural",
    "de-DE-FlorianMultilingualNeural",
    "de-DE-SeraphinaMultilingualNeural",
    "de-DE-KillianNeural",
    "de-DE-AmalaNeural",
]

DEFAULT_RANDOM_SEED_WORDS = [
    "clock", "rain", "mirror", "station", "receipt", "neon", "suitcase", "ticket", "stairwell", "window",
    "lighthouse", "elevator", "ring", "shadow", "garden", "bridge", "notebook", "helmet", "siren", "map",
    "backpack", "lantern", "factory", "beach", "helmet", "violin", "taxi", "library", "letter", "beacon",
    "alley", "market", "train", "airport", "desk", "sunrise", "sunset", "camera", "barcode", "snow",
    "storm", "harbor", "hospital", "museum", "workshop", "garage", "highway", "campfire", "portrait", "locker",
]

STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "their", "they", "you", "his", "her", "she", "he",
    "into", "after", "before", "when", "where", "then", "just", "over", "under", "still", "never", "last", "first",
    "story", "title", "hook", "voiceover", "script", "today", "night", "time", "life",
    "tiktok", "trend", "trends", "viral", "challenge", "hashtag", "video", "shorts", "reels",
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run_cmd(cmd, timeout=120):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr}")
    return p.stdout


def persist_job_message_via_openclaw(job_id: str, message: str) -> bool:
    msg = str(message or "").strip()
    if not job_id or not msg:
        return False
    try:
        run_cmd(["openclaw", "cron", "edit", str(job_id), "--message", msg], timeout=180)
        return True
    except Exception as e:
        log(f"Failed to persist cron message for {job_id} via openclaw cron edit: {e}")
        return False


def parse_csv_list(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,\n;|]+", raw)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if re.sub(r"\s+", " ", p).strip()]


def should_log_idle(state: Dict[str, Any], cfg: Dict[str, str]) -> bool:
    interval = max(30, min(1800, int(cfg.get("IDLE_LOG_EVERY_SEC", "300"))))
    now = time.time()
    last = float(state.get("last_idle_log_ts", 0) or 0)
    if now - last >= interval:
        state["last_idle_log_ts"] = now
        return True
    return False


def dedupe_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def parse_cron_job_ids(cfg: Dict[str, str]) -> List[str]:
    raw = str(cfg.get("CRON_JOB_IDS", "") or "")
    ids: List[str] = []
    if raw.strip():
        ids.extend([x.strip() for x in raw.split(",") if x.strip()])
    single = str(cfg.get("CRON_JOB_ID", "") or "").strip()
    if single:
        ids.append(single)
    out: List[str] = []
    seen = set()
    for jid in ids:
        if jid in seen:
            continue
        seen.add(jid)
        out.append(jid)
    return out


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_ts(s: str) -> float:
    if not s:
        return 0.0
    t = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def msg_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content") or []
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts).strip()


def command_from_text(text: str) -> str:
    if not text:
        return ""
    cleaned_raw = re.sub(r"<@!?\d+>", " ", text).strip()
    cleaned_raw = re.sub(r"\s+", " ", cleaned_raw)
    lower = cleaned_raw.lower()

    m = re.match(r"^(theme|idee|idea)\s*:\s*(.+)$", cleaned_raw, flags=re.IGNORECASE)
    if m:
        val = re.sub(r"\s+", " ", m.group(2)).strip()
        return f"THEME:{val}" if val else ""

    m_pick = re.match(r"^(?:pick|select|auswahl|wahl)\s*[:#-]?\s*([1-9][0-9]?)\.?$", lower)
    if m_pick:
        return f"TREND_PICK:{m_pick.group(1)}"
    m_num = re.match(r"^([1-9][0-9]?)\.?$", lower)
    if m_num:
        return f"TREND_PICK:{m_num.group(1)}"

    if re.fullmatch(r"(trend|trends|new trend|new trends)[!. ]*", lower):
        return "TREND"
    if re.fullmatch(r"no[!. ]*", lower):
        return "NO"
    if re.fullmatch(r"go[!. ]*", lower):
        return "GO"
    if re.fullmatch(r"(post|publish|gopost)[!. ]*", lower):
        return "POST"
    return ""


def latest_reply(session_jsonl: str, source: str = "session", id_prefix: str = "") -> Dict[str, Any]:
    best = {"ts": 0.0, "value": "", "id": "", "source": source, "raw": ""}
    for line in session_jsonl.splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "message":
            continue
        m = obj.get("message", {})
        if m.get("role") != "user":
            continue
        txt = msg_text(m)
        ts = parse_ts(obj.get("timestamp", "")) or parse_ts(m.get("timestamp", ""))
        val = command_from_text(txt)

        if val and ts > best["ts"]:
            mid = str(obj.get("id") or m.get("id") or f"{id_prefix}{ts}")
            best = {"ts": ts, "value": val, "id": mid, "source": source, "raw": txt}
    return best


def snowflake_ts(msg_id: str) -> float:
    if not msg_id:
        return 0.0
    try:
        # Discord snowflake: timestamp in ms since 2015-01-01 UTC.
        ms = (int(msg_id) >> 22) + 1420070400000
        return ms / 1000.0
    except Exception:
        return 0.0


def load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_url_text(url: str, timeout: int = 20) -> str:
    req = request.Request(url, method="GET")
    # Browser-like UA improves compatibility with TikTok Creative Center pages.
    req.add_header(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 claw-vm-bridge/1.0",
    )
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    with request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def fetch_rss_titles(url: str, timeout: int = 20, limit: int = 10) -> List[str]:
    raw = fetch_url_text(url, timeout=timeout)
    root = ET.fromstring(raw)
    out: List[str] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        title = html.unescape(title)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        out.append(title)
        if len(out) >= limit:
            break
    return out


def fetch_duckduckgo_titles(query: str, timeout: int = 20, limit: int = 6) -> List[str]:
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    if not q:
        return []
    url = f"https://duckduckgo.com/html/?{parse.urlencode({'q': q})}"
    raw = fetch_url_text(url, timeout=timeout)
    out: List[str] = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', raw, flags=re.IGNORECASE | re.DOTALL):
        txt = re.sub(r"<[^>]+>", " ", m.group(1))
        txt = html.unescape(txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        out.append(txt)
        if len(out) >= limit:
            break
    return out


def _signal_core_query(signal: str) -> str:
    s = normalize_signal_text(signal)
    if not s:
        return ""
    s = re.sub(r"(?i)^tiktok\s+(?:hashtag|inspiration|song)\s+trend\s*\([^)]+\)\s*:\s*", "", s).strip()
    tags = re.findall(r"#([A-Za-z0-9_]{2,60})", s)
    if tags:
        return f"#{tags[0]}"
    s = re.sub(r'(?i)^track\s*:\s*', "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def _extract_keywords(texts: List[str], query: str, limit: int = 5) -> List[str]:
    q_words = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query.lower()))
    counts: Dict[str, int] = {}
    for t in texts:
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", t.lower())
        uniq = set()
        for w in words:
            if w in STOP_WORDS or w in q_words:
                continue
            uniq.add(w)
        for w in uniq:
            counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.keys(), key=lambda k: (-counts[k], k))
    return ranked[: max(1, min(10, int(limit)))]


def build_trend_research_contexts(signals: List[str], cfg: Dict[str, str]) -> Dict[str, str]:
    enabled = str(cfg.get("TREND_WEB_RESEARCH_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {}

    max_signals = max(1, min(12, int(cfg.get("TREND_WEB_RESEARCH_MAX_SIGNALS", "6"))))
    max_titles_per_source = max(1, min(8, int(cfg.get("TREND_WEB_RESEARCH_MAX_TITLES_PER_SOURCE", "3"))))
    timeout_sec = max(4, min(30, int(cfg.get("TREND_WEB_RESEARCH_TIMEOUT_SEC", "12"))))
    geo = (cfg.get("TREND_WEB_RESEARCH_GEO", cfg.get("THEME_DISCOVERY_GEO", "US")) or "US").strip().upper()
    use_news = str(cfg.get("TREND_WEB_RESEARCH_USE_GOOGLE_NEWS", "true")).strip().lower() in {"1", "true", "yes", "on"}
    use_ddg = str(cfg.get("TREND_WEB_RESEARCH_USE_DUCKDUCKGO", "true")).strip().lower() in {"1", "true", "yes", "on"}

    out: Dict[str, str] = {}
    for signal in signals[:max_signals]:
        core = _signal_core_query(signal)
        if not core:
            continue
        if core.startswith("#"):
            q1 = f"{core} TikTok trend meaning"
            q2 = f"{core} challenge"
        else:
            q1 = f"{core} TikTok trend"
            q2 = f"{core} explainer"

        titles: List[str] = []
        seen = set()
        if use_news:
            for q in (q1, q2):
                try:
                    url = (
                        "https://news.google.com/rss/search?"
                        + parse.urlencode(
                            {
                                "q": q,
                                "hl": "en-US",
                                "gl": geo,
                                "ceid": f"{geo}:en",
                            }
                        )
                    )
                    for t in fetch_rss_titles(url, timeout=timeout_sec, limit=max_titles_per_source):
                        k = t.lower()
                        if k in seen:
                            continue
                        seen.add(k)
                        titles.append(t)
                except Exception as e:
                    log(f"Trend web research (Google News) failed for '{core}': {e}")
                    break

        if use_ddg:
            for q in (q1,):
                try:
                    for t in fetch_duckduckgo_titles(q, timeout=timeout_sec, limit=max_titles_per_source):
                        k = t.lower()
                        if k in seen:
                            continue
                        seen.add(k)
                        titles.append(t)
                except Exception as e:
                    log(f"Trend web research (DuckDuckGo) failed for '{core}': {e}")
                    break

        if not titles:
            continue

        kws = _extract_keywords(titles, core, limit=4)
        samples = titles[:2]
        parts: List[str] = []
        if kws:
            parts.append("topics: " + ", ".join(kws))
        if samples:
            parts.append("signals: " + " / ".join(samples))
        brief = "; ".join(parts).strip()
        if brief:
            out[str(signal)] = brief[:240]
    return out


def normalize_signal_text(raw: Any) -> str:
    txt = html.unescape(str(raw or ""))
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def extract_next_data_json(raw_html: str) -> Dict[str, Any]:
    if not raw_html:
        return {}
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        raw_html,
        flags=re.DOTALL,
    )
    if not m:
        return {}
    payload = m.group(1).strip()
    if not payload:
        return {}
    try:
        obj = json.loads(payload)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def normalize_tiktok_country_code(raw: str, default: str = "ALL") -> str:
    country = (raw or "").strip().upper()
    if not country:
        return default
    if country in {"GLOBAL", "WORLD", "WW"}:
        return "ALL"
    return country


def fetch_tiktok_creative_center_page(path_kind: str, locale: str, country: str, period: int) -> str:
    clean_locale = (locale or "en").strip() or "en"
    kind_prefix = f"{path_kind.strip().strip('/')}/" if str(path_kind or "").strip() else ""
    url = (
        f"https://ads.tiktok.com/business/creativecenter/inspiration/popular/{kind_prefix}pc/"
        f"{parse.quote(clean_locale)}?countryCode={parse.quote(country)}&period={period}"
    )
    try:
        return fetch_url_text(url, timeout=25)
    except error.HTTPError as e:
        # Creative Center often returns 404 for non-English locale paths.
        if e.code == 404 and clean_locale.lower() != "en":
            fallback_url = (
                f"https://ads.tiktok.com/business/creativecenter/inspiration/popular/{kind_prefix}pc/"
                f"en?countryCode={parse.quote(country)}&period={period}"
            )
            return fetch_url_text(fallback_url, timeout=25)
        raise


def discover_tiktok_music_themes(cfg: Dict[str, str], limit: int = 4) -> List[str]:
    locale = (cfg.get("THEME_DISCOVERY_TIKTOK_LOCALE", "en") or "en").strip() or "en"
    country = normalize_tiktok_country_code(cfg.get("THEME_DISCOVERY_TIKTOK_COUNTRY", "ALL"), default="ALL")
    period = max(1, min(30, int(cfg.get("THEME_DISCOVERY_TIKTOK_PERIOD_DAYS", "7"))))
    limit = max(0, min(20, int(limit)))
    if limit <= 0:
        return []
    raw_html = fetch_tiktok_creative_center_page("music", locale, country, period)
    next_data = extract_next_data_json(raw_html)
    sound_list = (
        (next_data.get("props") or {}).get("pageProps", {}).get("data", {}).get("soundList", [])
        if isinstance(next_data, dict)
        else []
    )
    out: List[str] = []
    seen = set()
    target_country = country
    for item in sound_list:
        if not isinstance(item, dict):
            continue
        title = normalize_signal_text(item.get("title", ""))
        author = normalize_signal_text(item.get("author", ""))
        ccode = normalize_signal_text(item.get("countryCode", country)).upper() or country
        if not title:
            continue
        label_region = target_country if target_country and target_country != "ALL" else (ccode or "GLOBAL")
        signal = f'TikTok song trend ({label_region}): "{title}"'
        if author:
            signal += f" by {author}"
        key = signal.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
        if len(out) >= limit:
            break
    return out


def discover_tiktok_hashtag_themes(cfg: Dict[str, str], limit: int = 8) -> List[str]:
    locale = (cfg.get("THEME_DISCOVERY_TIKTOK_LOCALE", "en") or "en").strip() or "en"
    country = normalize_tiktok_country_code(cfg.get("THEME_DISCOVERY_TIKTOK_COUNTRY", "ALL"), default="ALL")
    period = max(1, min(30, int(cfg.get("THEME_DISCOVERY_TIKTOK_PERIOD_DAYS", "7"))))
    limit = max(0, min(40, int(limit)))
    if limit <= 0:
        return []
    raw_html = fetch_tiktok_creative_center_page("hashtag", locale, country, period)
    next_data = extract_next_data_json(raw_html)

    entries: List[tuple[str, str]] = []
    queries = (
        (next_data.get("props") or {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        if isinstance(next_data, dict)
        else []
    )
    for query in queries:
        if not isinstance(query, dict):
            continue
        qk = query.get("queryKey")
        if not (isinstance(qk, list) and len(qk) >= 3 and qk[0] == "trend" and qk[1] == "hashtag" and qk[2] == "list"):
            continue
        pages = (query.get("state") or {}).get("data", {}).get("pages", [])
        for page in pages:
            if not isinstance(page, dict):
                continue
            for item in page.get("list", []):
                if not isinstance(item, dict):
                    continue
                name = normalize_signal_text(item.get("hashtagName", ""))
                region = normalize_signal_text((item.get("countryInfo") or {}).get("id", "GLOBAL")).upper() or "GLOBAL"
                if not name:
                    continue
                entries.append((name.lstrip("#"), region))

    if not entries:
        # Fallback when structured query payload is unavailable.
        for raw_tag in re.findall(r'"hashtagName":"([^"]+)"', raw_html):
            tag = raw_tag
            try:
                tag = json.loads(f"\"{raw_tag}\"")
            except Exception:
                pass
            tag = normalize_signal_text(tag).lstrip("#")
            if tag:
                entries.append((tag, "GLOBAL"))

    out: List[str] = []
    seen = set()
    target_country = country
    for tag, region in entries:
        if not tag:
            continue
        if target_country and target_country != "ALL":
            label_region = target_country
        else:
            label_region = region if region and region != "ALL" else "GLOBAL"
        signal = f"TikTok hashtag trend ({label_region}): #{tag}"
        key = signal.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
        if len(out) >= limit:
            break
    return out


def discover_tiktok_inspiration_themes(cfg: Dict[str, str], limit: int = 6) -> List[str]:
    locale = (cfg.get("THEME_DISCOVERY_TIKTOK_LOCALE", "en") or "en").strip() or "en"
    country = normalize_tiktok_country_code(cfg.get("THEME_DISCOVERY_TIKTOK_COUNTRY", "ALL"), default="ALL")
    period = max(1, min(30, int(cfg.get("THEME_DISCOVERY_TIKTOK_PERIOD_DAYS", "7"))))
    limit = max(0, min(40, int(limit)))
    if limit <= 0:
        return []

    raw_html = fetch_tiktok_creative_center_page("", locale, country, period)
    next_data = extract_next_data_json(raw_html)
    videos = (
        (next_data.get("props") or {}).get("pageProps", {}).get("data", {}).get("videos", [])
        if isinstance(next_data, dict)
        else []
    )

    out: List[str] = []
    seen = set()
    target_country = country
    for item in videos:
        if not isinstance(item, dict):
            continue
        title = normalize_signal_text(item.get("title", ""))
        ccode = normalize_signal_text(item.get("countryCode", country)).upper() or country
        if not title:
            continue
        label_region = target_country if target_country and target_country != "ALL" else (ccode if ccode and ccode != "ALL" else "GLOBAL")
        signal = f"TikTok inspiration trend ({label_region}): {title}"
        key = signal.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(signal)
        if len(out) >= limit:
            break
    return out


def discover_tiktok_themes(cfg: Dict[str, str], max_topics: int = 8) -> List[str]:
    max_topics = max(1, min(20, int(max_topics)))
    max_hashtags = max(0, min(max_topics, int(cfg.get("THEME_DISCOVERY_TIKTOK_MAX_HASHTAGS", "6"))))
    max_inspiration = max(0, min(max_topics, int(cfg.get("THEME_DISCOVERY_TIKTOK_MAX_INSPIRATION", "6"))))
    max_songs = max(0, min(max_topics, int(cfg.get("THEME_DISCOVERY_TIKTOK_MAX_SONGS", "0"))))
    out: List[str] = []
    seen = set()

    try:
        for t in discover_tiktok_hashtag_themes(cfg, limit=max_hashtags):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= max_topics:
                return out[:max_topics]
    except Exception as e:
        log(f"TikTok hashtag trend discovery failed: {e}")

    try:
        for t in discover_tiktok_inspiration_themes(cfg, limit=max_inspiration):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= max_topics:
                return out[:max_topics]
    except Exception as e:
        log(f"TikTok inspiration trend discovery failed: {e}")

    try:
        for t in discover_tiktok_music_themes(cfg, limit=max_songs):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= max_topics:
                return out[:max_topics]
    except Exception as e:
        log(f"TikTok music trend discovery failed: {e}")

    return out[:max_topics]


def discover_current_themes(cfg: Dict[str, str]) -> List[str]:
    if str(cfg.get("THEME_DISCOVERY_ENABLED", "true")).strip().lower() in {"0", "false", "no", "off"}:
        return []
    max_topics = max(3, min(15, int(cfg.get("THEME_DISCOVERY_MAX_TOPICS", "8"))))
    seen = set()
    themes: List[str] = []

    tiktok_enabled = str(cfg.get("THEME_DISCOVERY_TIKTOK_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    tiktok_only = str(cfg.get("THEME_DISCOVERY_TIKTOK_ONLY", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if tiktok_enabled:
        for t in discover_tiktok_themes(cfg, max_topics=max_topics):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            themes.append(t)
            if len(themes) >= max_topics:
                return themes[:max_topics]
        if tiktok_only:
            if themes:
                return themes[:max_topics]
            log("TikTok-only theme discovery returned no themes; falling back to generic sources")

    geo = (cfg.get("THEME_DISCOVERY_GEO", "US") or "US").strip().upper()
    urls = [
        f"https://trends.google.com/trending/rss?geo={parse.quote(geo)}",
        f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={parse.quote(geo)}",
        f"https://news.google.com/rss?hl=en-US&gl={parse.quote(geo)}&ceid={parse.quote(geo)}:en",
    ]
    for url in urls:
        try:
            titles = fetch_rss_titles(url, limit=max_topics * 2)
        except Exception as e:
            log(f"Theme discovery source failed: {url} -> {e}")
            continue
        for t in titles:
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            themes.append(t)
            if len(themes) >= max_topics:
                return themes
    return themes[:max_topics]


def _extract_story_title(summary: str) -> str:
    if not summary:
        return ""
    patterns = [
        r"\*\*Title\*\*\s*(.+)",
        r"(?im)^Title\s*:?\s*$\s*(.+)$",
        r"(?im)^Title\s*:\s*(.+)$",
    ]
    for patt in patterns:
        m = re.search(patt, summary, flags=re.IGNORECASE)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _extract_content_mode(summary: str) -> str:
    raw = _extract_section(summary, r"Content\s+mode(?:\s*\(.*?\))?")
    if not raw:
        return ""
    val = re.sub(r"\s+", " ", raw).strip()
    for mode in ALLOWED_CONTENT_MODES:
        if val.lower() == mode.lower():
            return mode
    for mode in ALLOWED_CONTENT_MODES:
        if mode.lower() in val.lower():
            return mode
    return val[:80]


def recent_content_modes(oc_home: str, job_id: str, max_modes: int = 8) -> List[str]:
    runs_file = Path(oc_home) / "cron" / "runs" / f"{job_id}.jsonl"
    if not runs_file.exists():
        return []
    out: List[str] = []
    seen = set()
    for line in reversed(runs_file.read_text(encoding="utf-8").splitlines()):
        if not line.strip().startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("action") != "finished" or obj.get("status") != "ok":
            continue
        mode = _extract_content_mode(str(obj.get("summary") or ""))
        if not mode:
            continue
        key = mode.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(mode)
        if len(out) >= max_modes:
            break
    return out


def pick_preferred_content_mode(cfg: Dict[str, str], recent_modes: List[str]) -> str:
    configured = parse_csv_list(str(cfg.get("CONTENT_MODE_ROTATION", "") or ""))
    pool = [m for m in configured if m] or list(ALLOWED_CONTENT_MODES)
    pool = dedupe_keep_order(pool)
    if not pool:
        pool = list(ALLOWED_CONTENT_MODES)
    avoid_window = max(0, min(5, int(cfg.get("CONTENT_MODE_AVOID_REPEAT_WINDOW", "2"))))
    recent_lower = [str(m).strip().lower() for m in recent_modes[:avoid_window]]
    candidates = [m for m in pool if m.lower() not in recent_lower]
    if candidates:
        return random.SystemRandom().choice(candidates)
    return random.SystemRandom().choice(pool)


def recent_story_titles(oc_home: str, job_id: str, max_titles: int = 8) -> List[str]:
    runs_file = Path(oc_home) / "cron" / "runs" / f"{job_id}.jsonl"
    if not runs_file.exists():
        return []
    titles: List[str] = []
    seen = set()
    for line in reversed(runs_file.read_text(encoding="utf-8").splitlines()):
        if not line.strip().startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("action") != "finished" or obj.get("status") != "ok":
            continue
        title = _extract_story_title(str(obj.get("summary") or ""))
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= max_titles:
            break
    return titles


def extract_frequent_title_terms(titles: List[str], min_hits: int = 2, max_terms: int = 8) -> List[str]:
    counts: Dict[str, int] = {}
    for title in titles:
        words = re.findall(r"[A-Za-z][A-Za-z\-']{3,}", title.lower())
        uniq = set()
        for w in words:
            if w in STOP_WORDS:
                continue
            uniq.add(w)
        for w in uniq:
            counts[w] = counts.get(w, 0) + 1
    ranked = sorted(
        [k for k, v in counts.items() if v >= min_hits],
        key=lambda k: (-counts[k], k),
    )
    return ranked[:max_terms]


def pick_random_seed_words(cfg: Dict[str, str]) -> List[str]:
    enabled = str(cfg.get("RANDOM_SEEDS_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return []
    count = max(1, min(6, int(cfg.get("RANDOM_SEED_WORD_COUNT", "3"))))
    configured = parse_csv_list(str(cfg.get("RANDOM_SEED_WORDS", "") or ""))
    pool = dedupe_keep_order(configured or DEFAULT_RANDOM_SEED_WORDS)
    if not pool:
        return []
    if len(pool) <= count:
        return pool
    return random.SystemRandom().sample(pool, count)


def build_diversity_context(cfg: Dict[str, str], oc_home: str, job_id: str) -> Dict[str, List[str]]:
    recent_max = max(3, min(20, int(cfg.get("THEME_AVOID_RECENT_TITLES", "10"))))
    recent_titles = recent_story_titles(oc_home, job_id, max_titles=recent_max)
    frequent_terms = extract_frequent_title_terms(recent_titles, min_hits=2, max_terms=8)
    hard_ban = parse_csv_list(
        str(cfg.get("THEME_HARD_BAN_TERMS", "voicemail,last voicemail,voice note,missed call") or "")
    )
    avoid_terms = dedupe_keep_order(hard_ban + frequent_terms)
    seed_words = pick_random_seed_words(cfg)
    return {
        "recent_titles": recent_titles,
        "avoid_terms": avoid_terms,
        "seed_words": seed_words,
    }


def strip_auto_block(text: str, begin_marker: str, end_marker: str) -> str:
    patt = re.compile(
        rf"\n?{re.escape(begin_marker)}.*?{re.escape(end_marker)}\n?",
        flags=re.DOTALL,
    )
    return re.sub(patt, "", text).strip()


def inject_themes_into_job_prompt(
    oc_home: str,
    job_id: str,
    themes: List[str],
    trend_contexts: Optional[Dict[str, str]] = None,
    avoid_recent_titles: List[str] | None = None,
    avoid_terms: List[str] | None = None,
    seed_words: List[str] | None = None,
) -> bool:
    if not themes:
        return False
    jobs_path = Path(oc_home) / "cron" / "jobs.json"
    data = load_json_file(jobs_path)
    if not isinstance(data, dict):
        return False
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return False

    updated = False
    final_message = ""
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        AUTO_THEMES_BEGIN,
        f"Use one of these current {len(themes)} trend signals as inspiration (captured {now_utc} UTC):",
    ] + [f"- {t}" for t in themes] + [
        "First infer the exact current intent behind hashtag/signal from the web-research context below.",
        "Rules: Pick exactly one trend signal and transform it into a high-retention vertical short concept.",
        "NON-NEGOTIABLE: The final output must clearly and specifically map to the chosen signal.",
        "Validation: if a reviewer cannot identify which listed signal was used, output is invalid.",
        "Include a concrete keyword from the chosen signal in Title or Hook.",
        "Avoid direct references to private persons or unverifiable claims.",
        "Choose exactly one content angle that best fits trend intent: Story Drama, Fact Explainer, Current-News Brief, Mega-Build/Engineering Showcase, Myth-busting.",
        "Do not force fictional character drama when trend intent is factual/news/engineering.",
    ]
    if trend_contexts:
        lines += [
            "Trend research context (web lookup):",
        ]
        for t in themes:
            brief = str((trend_contexts or {}).get(str(t), "")).strip()
            if brief:
                lines.append(f"- {t} => {brief}")
    if avoid_recent_titles:
        lines += [
            "Hard constraint: Avoid repeating these recent story titles/themes:",
        ] + [f"- {t}" for t in avoid_recent_titles]
    if avoid_terms:
        lines += [
            "Hard ban terms/motifs (do not use these as central premise):",
        ] + [f"- {t}" for t in avoid_terms]
    if seed_words:
        lines += [
            f"Creativity constraint: Use exactly these {len(seed_words)} seed words naturally in the story context:",
        ] + [f"- {w}" for w in seed_words]
    lines += [
        AUTO_THEMES_END,
    ]
    injection = "\n".join(lines)

    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id") or "") != job_id:
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        msg = str(payload.get("message") or "")
        # Keep base prompt, but replace any auto-injected theme/manual override blocks.
        base = strip_auto_block(msg, AUTO_THEMES_BEGIN, AUTO_THEMES_END)
        base = strip_auto_block(base, AUTO_MANUAL_IDEA_BEGIN, AUTO_MANUAL_IDEA_END)
        merged = f"{injection}\n\n{base}".strip()
        payload["message"] = merged
        job["updatedAtMs"] = int(time.time() * 1000)
        final_message = merged
        updated = True
        break

    if not updated:
        return False
    jobs_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if final_message:
        persist_job_message_via_openclaw(job_id, final_message)
    return True


def inject_manual_idea_into_job_prompt(
    oc_home: str,
    job_id: str,
    manual_idea: str,
    idea_context: str = "",
) -> bool:
    idea = re.sub(r"\s+", " ", str(manual_idea or "")).strip()
    if not idea:
        return False

    jobs_path = Path(oc_home) / "cron" / "jobs.json"
    data = load_json_file(jobs_path)
    if not isinstance(data, dict):
        return False
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return False

    updated = False
    final_message = ""
    block_lines = [
        AUTO_MANUAL_IDEA_BEGIN,
        "Manual idea override (highest priority):",
        f"- Use this exact user prompt as PRIMARY generation direction: {idea}",
        "- Build the full story package directly from this idea.",
        "- NON-NEGOTIABLE: Do not switch to unrelated themes.",
        "- The central conflict, setting, and reveal must directly reflect this idea.",
        "- Include a concrete keyword from the user idea in Title or Hook.",
        "- Do NOT fallback to default generic motifs (train/container/locker/heist) unless the idea itself asks for it.",
        "- If output drifts away from the idea, regenerate before returning final output.",
        "- Choose exactly one content angle: Story Drama, Fact Explainer, Current-News Brief, Mega-Build/Engineering Showcase, Myth-busting.",
    ]
    ctx = re.sub(r"\s+", " ", str(idea_context or "")).strip()
    if ctx:
        block_lines.append(f"- Web trend context for this idea: {ctx}")
    block_lines.append(AUTO_MANUAL_IDEA_END)
    block = "\n".join(block_lines)

    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id") or "") != job_id:
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        msg = str(payload.get("message") or "")
        # Replace previous auto blocks to avoid conflicting constraints.
        base = strip_auto_block(msg, AUTO_THEMES_BEGIN, AUTO_THEMES_END)
        base = strip_auto_block(base, AUTO_MANUAL_IDEA_BEGIN, AUTO_MANUAL_IDEA_END)
        merged = f"{block}\n\n{base}".strip()
        if merged != msg:
            payload["message"] = merged
            job["updatedAtMs"] = int(time.time() * 1000)
            final_message = merged
            updated = True
        break

    if not updated:
        return False
    jobs_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if final_message:
        persist_job_message_via_openclaw(job_id, final_message)
    return True


def inject_output_format_rules_into_job_prompt(
    oc_home: str,
    job_id: str,
    preferred_mode: str = "",
    recent_modes: Optional[List[str]] = None,
) -> bool:
    jobs_path = Path(oc_home) / "cron" / "jobs.json"
    data = load_json_file(jobs_path)
    if not isinstance(data, dict):
        return False
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return False

    updated = False
    final_message = ""
    patt = re.compile(
        rf"\n?{re.escape(AUTO_OUTPUT_RULES_BEGIN)}.*?{re.escape(AUTO_OUTPUT_RULES_END)}\n?",
        flags=re.DOTALL,
    )
    mode_lines = [f"- {x}" for x in ALLOWED_CONTENT_MODES]
    preferred = re.sub(r"\s+", " ", str(preferred_mode or "")).strip()
    recent = [re.sub(r"\s+", " ", str(x)).strip() for x in (recent_modes or []) if str(x).strip()][:4]
    rules_lines = [
        AUTO_OUTPUT_RULES_BEGIN,
        "Strict output format rules (non-negotiable):",
        "- Output can be narrative OR factual/explainer; do not force fictional story every run.",
        "- Pick exactly one content mode and write it as a section: 'Content mode'.",
        "- Allowed Content mode values:",
    ] + mode_lines + [
        "- Keep all required sections used by the render pipeline (Title, Hook, Voiceover Script, On-screen text beats, Visual plan, Voice choice, Music direction, Caption, Hashtags).",
        "- The Hook is the first sentence the viewer hears. It MUST also appear as the opening line of the Voiceover Script.",
        "- In section 'Visual plan', write EXACTLY 2 shot descriptions (not 3, not 1). The render pipeline generates exactly 2 video clips.",
        "- Each Visual plan shot description must be highly specific and detailed (2-3 sentences each).",
        "- Include in each shot: exact setting/location, lighting conditions, subject appearance/clothing, specific action/movement, camera angle, and dominant color palette or mood.",
        "- Example good shot: 'Dimly lit subway car at night, warm yellow overhead lights, young woman in dark hoodie gripping metal pole, eyes darting nervously, rain streaks on window behind her, handheld close-up slowly pulling back.'",
        "- Example bad shot: 'Person in city, dramatic scene.' (too vague, will produce generic video)",
    ]
    if preferred:
        rules_lines.append(f"- Preferred mode for this run: {preferred} (unless trend intent strongly requires another mode).")
    if recent:
        rules_lines.append("- Avoid repeating recent modes if possible:")
        rules_lines += [f"- {m}" for m in recent]
    rules_lines += [
        "- In section 'On-screen text beats', EVERY beat line must include a time range prefix.",
        "- Total number of on-screen text beats must be 3 to 4 lines (inclusive), never more than 4.",
        "- Accepted examples: 0-3s: TEXT, 3-7s: TEXT, 00:03-00:07: TEXT.",
        "- Each beat time range should be SHORT: around 2-3 seconds (never longer than 3 seconds).",
        "- Each beat text must be SHORT: 2 or 3 words only (maximum 3 words).",
        "- Distribute beats across the full narration timeline, not only the opening.",
        "- Ensure the final beat appears in the last third of the narration timeline.",
        "- Place each beat only at its exact moment; avoid subtitle-like continuous coverage.",
        "- Do not write full sentences in beat text.",
        "- Do not output untimed beat lines.",
        "- In section 'Music direction', choose EXACTLY ONE BGM track from the allowed list below.",
        "- Write the first line exactly as: Track: <filename>",
        "- Allowed track filenames:",
    ] + [f"- {x}" for x in BGM_TRACK_CHOICES] + [
        "- Then write one short sentence why this exact track fits the mood.",
        "- Never mention tracks outside the allowed list.",
        "- Add a dedicated section: 'Voice choice'.",
        "- In section 'Voice choice', choose EXACTLY ONE voice from the allowed list below.",
        "- Write the first line exactly as: Voice: <voice_name>",
        "- Allowed voice names:",
    ] + [f"- {x}" for x in VOICE_TRACK_CHOICES] + [
        "- Then write one short sentence why this voice best matches narrator language and tone.",
        "- Never mention voices outside the allowed list.",
        "- Output only the final Discord-ready package body with the requested sections.",
        AUTO_OUTPUT_RULES_END,
    ]
    rules_block = "\n".join(rules_lines)

    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id") or "") != job_id:
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        msg = str(payload.get("message") or "")
        base = re.sub(patt, "", msg).strip()
        merged = f"{base}\n\n{rules_block}".strip()
        if merged != msg:
            payload["message"] = merged
            job["updatedAtMs"] = int(time.time() * 1000)
            final_message = merged
            updated = True
        break

    if not updated:
        return False
    jobs_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if final_message:
        persist_job_message_via_openclaw(job_id, final_message)
    return True


def clear_temporary_overrides_from_job_prompt(oc_home: str, job_id: str) -> bool:
    jobs_path = Path(oc_home) / "cron" / "jobs.json"
    data = load_json_file(jobs_path)
    if not isinstance(data, dict):
        return False
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return False

    updated = False
    final_message = ""
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("id") or "") != job_id:
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        msg = str(payload.get("message") or "")
        cleaned = strip_auto_block(msg, AUTO_THEMES_BEGIN, AUTO_THEMES_END)
        cleaned = strip_auto_block(cleaned, AUTO_MANUAL_IDEA_BEGIN, AUTO_MANUAL_IDEA_END)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if cleaned != msg:
            payload["message"] = cleaned
            job["updatedAtMs"] = int(time.time() * 1000)
            final_message = cleaned
            updated = True
        break

    if not updated:
        return False
    jobs_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if final_message:
        persist_job_message_via_openclaw(job_id, final_message)
    return True


def run_openclaw_cron_with_temporary_prompt_reset(oc_home: str, job_id: str, timeout_ms: int) -> None:
    try:
        run_openclaw_cron(job_id, timeout_ms)
    finally:
        if clear_temporary_overrides_from_job_prompt(oc_home, job_id):
            log(f"Cleared temporary theme/manual overrides on {job_id}")


def pick_latest_reply(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [c for c in candidates if c.get("value")]
    if not valid:
        return {"ts": 0.0, "value": "", "id": "", "source": "", "raw": ""}
    valid.sort(key=lambda c: (float(c.get("ts") or 0), 1 if c.get("source") == "discord" else 0), reverse=True)
    return valid[0]


def latest_reply_from_session_files(sessions_dir: Path, preferred_file: str = "") -> Dict[str, Any]:
    files: List[Path] = []
    if preferred_file:
        p = Path(preferred_file)
        if p.exists():
            files.append(p)
    files.extend(sorted(sessions_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True))

    seen = set()
    best = {"ts": 0.0, "value": "", "id": "", "source": "session", "raw": ""}
    for p in files[:40]:
        if p in seen:
            continue
        seen.add(p)
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        cand = latest_reply(text, source="session", id_prefix=f"{p.name}:")
        if cand.get("value") and cand["ts"] > best["ts"]:
            best = cand
    return best


def detect_discord_token(cfg: Dict[str, str], oc_home: str) -> str:
    if cfg.get("DISCORD_BOT_TOKEN"):
        return cfg["DISCORD_BOT_TOKEN"]
    oc_cfg = load_json_file(Path(oc_home) / "openclaw.json")
    return str((((oc_cfg.get("channels") or {}).get("discord") or {}).get("token") or "")).strip()


def discord_post_json(url: str, token: str, payload: Dict[str, Any]) -> Any:
    req = request.Request(url, method="POST", data=json.dumps(payload).encode("utf-8"))
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "claw-vm-bridge/1.0")
    with request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="ignore")
    return json.loads(body)


def discord_send_message(channel_id: str, token: str, content: str) -> None:
    if not channel_id or not token:
        return
    msg = str(content or "").strip()
    if not msg:
        return
    if len(msg) > 1900:
        msg = msg[:1900] + "..."
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    discord_post_json(url, token, {"content": msg})


def discord_get_json(url: str, token: str) -> Any:
    req = request.Request(url, method="GET")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "claw-vm-bridge/1.0")
    with request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="ignore")
    return json.loads(body)


def latest_reply_from_discord_api(
    channel_id: str,
    token: str,
    last_seen_id: str = "",
) -> Dict[str, Any]:
    if not token:
        return {"best": {"ts": 0.0, "value": "", "id": "", "source": "discord", "raw": ""}, "latest_seen_id": last_seen_id}
    query = {"limit": "100" if last_seen_id else "50"}
    if last_seen_id:
        query["after"] = last_seen_id
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages?{parse.urlencode(query)}"

    try:
        data = discord_get_json(url, token)
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Discord API error {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"Discord API request failed: {e}") from e

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Discord API response: {data}")

    best = {"ts": 0.0, "value": "", "id": "", "source": "discord", "raw": ""}
    latest_seen = last_seen_id
    empty_user_content = 0
    for msg in data:
        msg_id = str(msg.get("id") or "")
        if msg_id:
            if not latest_seen:
                latest_seen = msg_id
            else:
                try:
                    if int(msg_id) > int(latest_seen):
                        latest_seen = msg_id
                except Exception:
                    pass
        author = msg.get("author") or {}
        if author.get("bot"):
            continue
        txt = str(msg.get("content") or "")
        if not txt.strip():
            empty_user_content += 1
        val = command_from_text(txt)
        if not val:
            continue
        ts = parse_ts(str(msg.get("timestamp") or "")) or snowflake_ts(msg_id)
        if ts > best["ts"]:
            best = {"ts": ts, "value": val, "id": msg_id or f"discord:{ts}", "source": "discord", "raw": txt}

    return {"best": best, "latest_seen_id": latest_seen, "empty_user_content": empty_user_content}


def format_trend_choices_message(
    themes: List[str],
    max_items: int = 6,
    trend_contexts: Optional[Dict[str, str]] = None,
) -> str:
    items = [t for t in themes if str(t).strip()][: max(1, min(10, int(max_items)))]
    lines = [
        "Aktuelle Trend-Auswahl:",
    ]
    for idx, theme in enumerate(items, start=1):
        lines.append(f"{idx}. {theme}")
        brief = str((trend_contexts or {}).get(str(theme), "")).strip()
        if brief:
            lines.append(f"   Kontext: {brief[:220]}")
    lines += [
        "",
        "Antworte mit der Zahl (z.B. `1`) oder `PICK:1`, um genau diesen Trend zu nutzen.",
    ]
    return "\n".join(lines)


def latest_story_data(runs_jsonl: str) -> Dict[str, Any]:
    latest_ts = 0.0
    latest_summary = ""
    for line in runs_jsonl.splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("action") != "finished" or obj.get("status") != "ok":
            continue
        summary = obj.get("summary", "")
        if not isinstance(summary, str) or len(summary.strip()) < 40:
            continue
        ts = float(obj.get("ts") or 0)
        if ts > latest_ts:
            latest_ts = ts
            latest_summary = summary
    return {"summary": latest_summary, "ts": latest_ts}


def latest_story_data_from_jobs(oc_home: str, job_ids: List[str]) -> Dict[str, Any]:
    best = {"summary": "", "ts": 0.0, "job_id": ""}
    for job_id in job_ids:
        runs_file = Path(oc_home) / "cron" / "runs" / f"{job_id}.jsonl"
        if not runs_file.exists():
            continue
        data = latest_story_data(runs_file.read_text(encoding="utf-8"))
        if float(data.get("ts") or 0) > float(best.get("ts") or 0):
            best = {"summary": data.get("summary", ""), "ts": float(data.get("ts") or 0), "job_id": job_id}
    return best


def _extract_section(story: str, label_pattern: str) -> str:
    text = str(story or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    section_stop = (
        r"(?:Title|Hook(?:\s*\([^\)\n]*\))?|Voiceover(?:\s+Script)?(?:\s*\([^\)\n]*\))?|"
        r"On[-\s]*screen\s+text\s+beats?(?:\s*\([^\)\n]*\))?|Visual(?:\s+Plan)?(?:\s*\([^\)\n]*\))?|"
        r"Music(?:\s+direction)?(?:\s*\([^\)\n]*\))?|Voice(?:\s+choice)?(?:\s*\([^\)\n]*\))?|"
        r"Content\s+mode(?:\s*\([^\)\n]*\))?|Caption|Hashtags?)"
    )

    start_patterns = [
        re.compile(rf"(?im)^\s*\*\*{label_pattern}\s*:?\s*\*\*\s*(?P<inline>[^\n]*)$"),
        re.compile(rf"(?im)^\s*{label_pattern}\s*:?\s*(?P<inline>[^\n]*)$"),
    ]
    start_match: Optional[re.Match[str]] = None
    for patt in start_patterns:
        m = patt.search(text)
        if m and (start_match is None or m.start() < start_match.start()):
            start_match = m
    if not start_match:
        return ""

    inline = str(start_match.groupdict().get("inline") or "").strip()
    stop_re = re.compile(
        rf"(?im)^\s*(?:\*\*(?:{section_stop})\s*:?\s*\*\*|(?:{section_stop})\s*:?).*$",
    )
    next_match = stop_re.search(text, start_match.end())
    body = text[start_match.end() : (next_match.start() if next_match else len(text))].strip()
    out = f"{inline}\n{body}".strip() if (inline and body) else (inline or body)
    out = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def extract_prompt(story: str) -> str:
    title = _extract_section(story, r"Title")
    hook = _extract_section(story, r"Hook(?:\s*\(.*?\))?")
    visual = _extract_section(story, r"Visual(?:\s+Plan)?(?:\s*\(.*?\))?")
    content_mode = _extract_section(story, r"Content\s+mode(?:\s*\(.*?\))?")

    if visual:
        # Keep only first visual item to avoid overloading the base prompt.
        visual = re.sub(r"^\s*[-*•]\s*", "", visual)
        visual = re.split(r"\s*(?:;|\||\n)\s*", visual)[0].strip()

    parts = [
        "Cinematic vertical video, 9:16, high visual clarity, no embedded text",
    ]
    if content_mode:
        parts.append(f"Content mode: {content_mode}")
    if title:
        parts.append(f"Topic: {title}")
    if hook:
        parts.append(f"Hook intent: {hook}")
    if visual:
        parts.append(f"Primary visual anchor: {visual}")
    parts.append("Prioritize trend relevance, clear subject identity, and concise storytelling beats")
    return ". ".join([p for p in parts if p]).strip()


def post_json(
    url: str,
    payload: Dict[str, Any],
    token: str = "",
    timeout_sec: int = 240,
    retries: int = 0,
    retry_backoff_sec: float = 4.0,
) -> Dict[str, Any]:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Api-Token", token)
    last_exc: Exception | None = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            with request.urlopen(req, timeout=max(10, int(timeout_sec))) as r:
                body = r.read().decode("utf-8", errors="ignore")
            try:
                return json.loads(body)
            except Exception:
                return {"raw": body}
        except error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {e.code} from {url}: {body[:600]}") from e
        except Exception as e:
            last_exc = e
            if attempt >= attempts:
                break
            wait_s = retry_backoff_sec * attempt
            log(f"POST {url} failed (attempt {attempt}/{attempts}): {e}; retrying in {wait_s:.1f}s")
            time.sleep(wait_s)
    raise RuntimeError(str(last_exc) if last_exc else f"POST {url} failed")


def mac_api_status(
    api_url: str,
    token: str = "",
    timeout_sec: int = 20,
) -> Dict[str, Any]:
    return post_json(
        f"{api_url.rstrip('/')}/status",
        {},
        token,
        timeout_sec=max(10, int(timeout_sec)),
        retries=0,
        retry_backoff_sec=1.0,
    )


def _mac_api_story_matches_status(status_obj: Dict[str, Any], story_id: str) -> bool:
    sid = str(story_id or "").strip()
    if not sid or not isinstance(status_obj, dict):
        return False
    active = status_obj.get("active_status")
    if isinstance(active, dict):
        active_story = str(active.get("story_id") or "").strip()
        stage = str(active.get("stage") or "").strip().lower()
        if active_story == sid and stage in {"preparing", "generating", "ready", "publishing"}:
            return True
    last_story = status_obj.get("last_story")
    if isinstance(last_story, dict):
        if str(last_story.get("story_id") or "").strip() == sid:
            return True
    return False


def post_generate_with_status_recovery(
    *,
    api_url: str,
    api_token: str,
    payload: Dict[str, Any],
    story_id: str,
    generate_timeout: int,
    api_retries: int,
    api_retry_backoff: float,
    status_timeout: int,
    log_prefix: str,
) -> Dict[str, Any]:
    try:
        return post_json(
            f"{api_url}/generate",
            payload,
            api_token,
            timeout_sec=generate_timeout,
            retries=api_retries,
            retry_backoff_sec=api_retry_backoff,
        )
    except Exception as e:
        try:
            status_obj = mac_api_status(api_url, api_token, timeout_sec=status_timeout)
        except Exception as se:
            log(f"{log_prefix} /generate failed and /status check failed: {e} (status error: {se})")
            raise
        if _mac_api_story_matches_status(status_obj, story_id):
            active = status_obj.get("active_status") if isinstance(status_obj, dict) else {}
            stage = str((active or {}).get("stage") or "unknown")
            log(
                f"{log_prefix} /generate transport error: {e}; "
                f"mac_api reports story_id={story_id} stage={stage}, treating as accepted"
            )
            return {
                "ok": True,
                "assumed_ok": True,
                "transport_error": str(e),
                "status_after_error": status_obj,
            }
        raise


def run_openclaw_cron(job_id: str, timeout_ms: int) -> None:
    timeout_ms = max(30000, min(1800000, int(timeout_ms)))
    cmd = ["openclaw", "cron", "run", str(job_id), "--timeout", str(timeout_ms)]
    # Add headroom above OpenClaw's own timeout.
    run_cmd(cmd, timeout=max(120, int(timeout_ms / 1000) + 120))


def main() -> int:
    cfg = load_env(ENV_FILE)
    required = ["DISCORD_CHANNEL_ID", "MAC_API_URL"]
    missing = [k for k in required if not cfg.get(k)]
    cron_job_ids = parse_cron_job_ids(cfg)
    if not cron_job_ids:
        missing.append("CRON_JOB_ID or CRON_JOB_IDS")
    if missing:
        log(f"Missing env keys: {', '.join(missing)}")
        return 2

    oc_home = cfg.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))
    sessions_file = Path(oc_home) / "agents" / "main" / "sessions" / "sessions.json"
    sessions = load_json_file(sessions_file)
    dkey = f"agent:main:discord:channel:{cfg['DISCORD_CHANNEL_ID']}"
    sentry = sessions.get(dkey) if isinstance(sessions, dict) else None
    preferred_session_file = (sentry or {}).get("sessionFile", "")

    sessions_dir = Path(oc_home) / "agents" / "main" / "sessions"
    session_reply = latest_reply_from_session_files(sessions_dir, preferred_file=preferred_session_file)

    state = load_state()
    state_dirty = False

    discord_reply = {"ts": 0.0, "value": "", "id": "", "source": "discord", "raw": ""}
    last_seen_discord_id = str(state.get("last_seen_discord_message_id") or "")
    discord_token = detect_discord_token(cfg, oc_home)
    if discord_token:
        api_res = latest_reply_from_discord_api(cfg["DISCORD_CHANNEL_ID"], discord_token, last_seen_discord_id)
        discord_reply = api_res["best"]
        newest_seen = str(api_res.get("latest_seen_id") or "")
        if newest_seen and newest_seen != last_seen_discord_id:
            state["last_seen_discord_message_id"] = newest_seen
            state_dirty = True
        if int(api_res.get("empty_user_content") or 0) > 0:
            log(
                "Discord messages from users have empty content. "
                "Enable Message Content Intent for the bot (Discord Developer Portal) "
                "or reply with a direct bot mention (e.g. '@OpenClaw GO')."
            )

    reply = pick_latest_reply([session_reply, discord_reply])
    # Use consumed-reply markers for de-duplication (at-most-once command handling).
    # Fallback to legacy processed markers for backward compatibility with older state files.
    last_ts = float(
        state.get(
            "last_consumed_reply_ts",
            state.get("last_processed_reply_ts", 0),
        )
        or 0
    )
    last_id = str(
        state.get("last_consumed_reply_id")
        or state.get("last_processed_reply_id")
        or ""
    )
    if not reply.get("value"):
        if state_dirty:
            save_state(state)
        if should_log_idle(state, cfg):
            save_state(state)
            log("No new GO/NO/POST")
        return 0
    if reply["ts"] < last_ts or (reply["ts"] == last_ts and str(reply.get("id") or "") == last_id):
        if state_dirty:
            save_state(state)
        if should_log_idle(state, cfg):
            save_state(state)
            log("No new GO/NO/POST")
        return 0

    # On fresh state files (e.g. after service migration), ignore stale commands once.
    replay_protect = str(cfg.get("COMMAND_REPLAY_PROTECTION_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    is_command = (
        reply.get("value") in {"GO", "POST", "NO", "TREND"}
        or str(reply.get("value", "")).startswith("THEME:")
        or str(reply.get("value", "")).startswith("TREND_PICK:")
    )
    if replay_protect and last_ts <= 0 and is_command:
        max_age = max(120, min(86400, int(cfg.get("COMMAND_MAX_AGE_SEC", "900"))))
        now_ts = time.time()
        if float(reply.get("ts") or 0) < (now_ts - max_age):
            state["last_consumed_reply_ts"] = float(reply.get("ts") or 0)
            state["last_consumed_reply_id"] = str(reply.get("id") or "")
            state["last_processed_reply_ts"] = float(reply.get("ts") or 0)
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            state["last_action"] = "bootstrap_ignore_stale_command"
            save_state(state)
            log(
                f"Bootstrap ignored stale command {reply.get('value')} from {reply.get('source')}:{reply.get('id')} "
                f"(older than {max_age}s)"
            )
            return 0

    # Persist command consumption immediately so a crash/timeout does not replay
    # the same GO/NO/TREND/THEME command over and over.
    state["last_consumed_reply_ts"] = float(reply.get("ts") or 0)
    state["last_consumed_reply_id"] = str(reply.get("id") or "")
    save_state(state)
    state_dirty = False

    story_data = latest_story_data_from_jobs(oc_home, cron_job_ids)
    story = story_data["summary"]
    story_id = hashlib.sha1((story or "").encode("utf-8")).hexdigest() if story else ""
    active_job_id = str(story_data.get("job_id") or cron_job_ids[0])

    for jid in cron_job_ids:
        recent_modes = recent_content_modes(oc_home, jid, max_modes=8)
        preferred_mode = pick_preferred_content_mode(cfg, recent_modes)
        if inject_output_format_rules_into_job_prompt(
            oc_home,
            jid,
            preferred_mode=preferred_mode,
            recent_modes=recent_modes,
        ):
            log(
                "Ensured strict output-format + dynamic content-mode rules "
                f"in cron prompt on {jid} (preferred_mode={preferred_mode})"
            )

    api_url = cfg["MAC_API_URL"].rstrip("/")
    api_token = cfg.get("MAC_API_TOKEN", "")
    publish_enabled = str(cfg.get("PUBLISH_COMMAND_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
    generate_timeout = max(120, int(cfg.get("MAC_API_GENERATE_TIMEOUT_SEC", "1800")))
    publish_timeout = max(60, int(cfg.get("MAC_API_PUBLISH_TIMEOUT_SEC", "300")))
    status_timeout = max(10, min(120, int(cfg.get("MAC_API_STATUS_TIMEOUT_SEC", "20"))))
    api_retries = max(0, min(5, int(cfg.get("MAC_API_RETRIES", "2"))))
    api_retry_backoff = max(1.0, min(30.0, float(cfg.get("MAC_API_RETRY_BACKOFF_SEC", "4"))))
    cron_run_timeout_ms = max(30000, min(1800000, int(cfg.get("OPENCLAW_CRON_RUN_TIMEOUT_MS", "600000"))))
    trend_selection_enabled = str(cfg.get("TREND_SELECTION_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    trend_selection_max_options = max(3, min(10, int(cfg.get("TREND_SELECTION_MAX_OPTIONS", "6"))))

    if str(reply["value"]).startswith("THEME:"):
        manual_theme = str(reply["value"]).split(":", 1)[1].strip()
        manual_theme = re.sub(r"\s+", " ", manual_theme).strip()
        idea_as_prompt = str(cfg.get("IDEA_COMMAND_AS_PROMPT_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
        if not manual_theme:
            log(f"{reply['source']}:{reply.get('id')} THEME command ignored (empty)")
        else:
            log(f"{reply['source']}:{reply.get('id')} THEME -> rerun cron job {active_job_id} with manual idea")
            manual_ctx_map = build_trend_research_contexts([manual_theme], cfg)
            manual_ctx = str(manual_ctx_map.get(manual_theme, "")).strip()
            if idea_as_prompt:
                if inject_manual_idea_into_job_prompt(oc_home, active_job_id, manual_theme, idea_context=manual_ctx):
                    log(f"Injected manual IDEE prompt override on {active_job_id}: {manual_theme}")
                else:
                    log("Manual IDEE prompt override did not update job prompt")
            else:
                themed = [manual_theme]
                ctx = build_diversity_context(cfg, oc_home, active_job_id)
                if inject_themes_into_job_prompt(
                    oc_home,
                    active_job_id,
                    themed,
                    trend_contexts=manual_ctx_map,
                    avoid_recent_titles=ctx["recent_titles"],
                    avoid_terms=ctx["avoid_terms"],
                    seed_words=ctx["seed_words"],
                ):
                    log(f"Injected manual theme into cron prompt on {active_job_id}: {manual_theme}")
                else:
                    log("Manual theme injection did not update job prompt")
            run_openclaw_cron_with_temporary_prompt_reset(oc_home, active_job_id, cron_run_timeout_ms)
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            state["last_action"] = "THEME_rerun"
            state["last_manual_theme"] = manual_theme
            state.pop("pending_trend_choices", None)
            state.pop("pending_trend_job_id", None)
            state.pop("pending_trend_contexts", None)
            save_state(state)
            return 0

    if str(reply["value"]).startswith("TREND_PICK:"):
        pick_raw = str(reply["value"]).split(":", 1)[1].strip()
        try:
            pick_idx = int(pick_raw)
        except Exception:
            pick_idx = 0
        choices = state.get("pending_trend_choices")
        pending_job_id = str(state.get("pending_trend_job_id") or active_job_id)
        if not isinstance(choices, list) or not choices:
            log(f"{reply['source']}:{reply.get('id')} TREND_PICK ignored: no pending trend list")
            try:
                discord_send_message(
                    cfg["DISCORD_CHANNEL_ID"],
                    discord_token,
                    "Keine offene Trend-Liste gefunden. Bitte zuerst `TREND` senden.",
                )
            except Exception as e:
                log(f"Discord send failed for TREND_PICK without pending list: {e}")
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            state["last_action"] = "TREND_pick_no_pending"
            state.pop("pending_trend_contexts", None)
            save_state(state)
            return 0
        if pick_idx < 1 or pick_idx > len(choices):
            log(f"{reply['source']}:{reply.get('id')} TREND_PICK invalid index={pick_idx}")
            try:
                discord_send_message(
                    cfg["DISCORD_CHANNEL_ID"],
                    discord_token,
                    f"Ungültige Auswahl `{pick_idx}`. Bitte eine Zahl von 1 bis {len(choices)} senden.",
                )
            except Exception as e:
                log(f"Discord send failed for invalid TREND_PICK: {e}")
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            state["last_action"] = "TREND_pick_invalid_index"
            save_state(state)
            return 0

        chosen = str(choices[pick_idx - 1]).strip()
        choice_contexts = state.get("pending_trend_contexts")
        selected_ctx = {}
        if isinstance(choice_contexts, dict):
            ctx_val = str(choice_contexts.get(chosen, "")).strip()
            if ctx_val:
                selected_ctx[chosen] = ctx_val
        elif not selected_ctx:
            selected_ctx = build_trend_research_contexts([chosen], cfg)
        ctx = build_diversity_context(cfg, oc_home, pending_job_id)
        if inject_themes_into_job_prompt(
            oc_home,
            pending_job_id,
            [chosen],
            trend_contexts=selected_ctx,
            avoid_recent_titles=ctx["recent_titles"],
            avoid_terms=ctx["avoid_terms"],
            seed_words=ctx["seed_words"],
        ):
            log(
                f"{reply['source']}:{reply.get('id')} TREND_PICK -> selected {pick_idx}/{len(choices)} "
                f"on {pending_job_id}: {chosen}"
            )
        else:
            log(f"{reply['source']}:{reply.get('id')} TREND_PICK prompt update failed on {pending_job_id}")
        run_openclaw_cron_with_temporary_prompt_reset(oc_home, pending_job_id, cron_run_timeout_ms)
        state["last_processed_reply_ts"] = reply["ts"]
        state["last_processed_reply_id"] = str(reply.get("id") or "")
        state["last_action"] = "TREND_pick_rerun"
        state["last_selected_trend"] = chosen
        state.pop("pending_trend_choices", None)
        state.pop("pending_trend_job_id", None)
        state.pop("pending_trend_contexts", None)
        save_state(state)
        return 0

    if reply["value"] == "TREND":
        log(f"{reply['source']}:{reply.get('id')} TREND -> rerun cron job {active_job_id} with fresh trends")
        themes = discover_current_themes(cfg)
        trend_contexts = build_trend_research_contexts(themes, cfg) if themes else {}
        ctx = build_diversity_context(cfg, oc_home, active_job_id)
        if themes and trend_selection_enabled and discord_token:
            options = themes[:trend_selection_max_options]
            option_contexts = {k: v for k, v in trend_contexts.items() if k in options}
            state["pending_trend_choices"] = options
            state["pending_trend_job_id"] = active_job_id
            state["pending_trend_contexts"] = option_contexts
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            state["last_action"] = "TREND_listed_choices"
            save_state(state)
            try:
                discord_send_message(
                    cfg["DISCORD_CHANNEL_ID"],
                    discord_token,
                    format_trend_choices_message(
                        options,
                        max_items=trend_selection_max_options,
                        trend_contexts=option_contexts,
                    ),
                )
            except Exception as e:
                log(f"Discord send failed for TREND choice list: {e}; fallback to immediate rerun")
                if inject_themes_into_job_prompt(
                    oc_home,
                    active_job_id,
                    themes,
                    trend_contexts=trend_contexts,
                    avoid_recent_titles=ctx["recent_titles"],
                    avoid_terms=ctx["avoid_terms"],
                    seed_words=ctx["seed_words"],
                ):
                    log(f"Fallback: updated cron job prompt with {len(themes)} current themes on {active_job_id}")
                else:
                    log("Fallback: trend prompt injection did not update job")
                run_openclaw_cron_with_temporary_prompt_reset(oc_home, active_job_id, cron_run_timeout_ms)
            return 0
        if themes:
            if inject_themes_into_job_prompt(
                oc_home,
                active_job_id,
                themes,
                trend_contexts=trend_contexts,
                avoid_recent_titles=ctx["recent_titles"],
                avoid_terms=ctx["avoid_terms"],
                seed_words=ctx["seed_words"],
            ):
                log(f"Updated cron job prompt with {len(themes)} current themes on {active_job_id}")
            else:
                log("Trend discovery succeeded, but prompt injection did not update job")
        else:
            log("No current themes discovered; using existing cron prompt")
        run_openclaw_cron_with_temporary_prompt_reset(oc_home, active_job_id, cron_run_timeout_ms)
        state["last_processed_reply_ts"] = reply["ts"]
        state["last_processed_reply_id"] = str(reply.get("id") or "")
        state["last_action"] = "TREND_rerun"
        state.pop("pending_trend_choices", None)
        state.pop("pending_trend_job_id", None)
        state.pop("pending_trend_contexts", None)
        save_state(state)
        return 0

    if reply["value"] == "NO":
        log(f"{reply['source']}:{reply.get('id')} NO -> rerun cron job {active_job_id}")
        themes = discover_current_themes(cfg)
        trend_contexts = build_trend_research_contexts(themes, cfg) if themes else {}
        ctx = build_diversity_context(cfg, oc_home, active_job_id)
        if themes:
            if inject_themes_into_job_prompt(
                oc_home,
                active_job_id,
                themes,
                trend_contexts=trend_contexts,
                avoid_recent_titles=ctx["recent_titles"],
                avoid_terms=ctx["avoid_terms"],
                seed_words=ctx["seed_words"],
            ):
                log(f"Updated cron job prompt with {len(themes)} current themes on {active_job_id}")
            else:
                log("Theme discovery succeeded, but prompt injection did not update job")
        else:
            log("No current themes discovered; using existing cron prompt")
        run_openclaw_cron_with_temporary_prompt_reset(oc_home, active_job_id, cron_run_timeout_ms)
        state["last_processed_reply_ts"] = reply["ts"]
        state["last_processed_reply_id"] = str(reply.get("id") or "")
        state["last_action"] = "NO_rerun"
        state.pop("pending_trend_choices", None)
        state.pop("pending_trend_job_id", None)
        state.pop("pending_trend_contexts", None)
        save_state(state)
        return 0

    if reply["value"] == "GO":
        generated_story_id = state.get("generated_story_id", "")
        if story and story_id and story_id != generated_story_id:
            payload = {
                "story_id": story_id,
                "story_text": story,
                "prompt": extract_prompt(story),
                "source_channel_id": cfg["DISCORD_CHANNEL_ID"],
                "refresh_overlays_only": (not publish_enabled),
            }
            log(
                f"{reply['source']}:{reply.get('id')} GO -> call /generate "
                f"(timeout={generate_timeout}s, retries={api_retries}) story_id={story_id}"
            )
            res = post_generate_with_status_recovery(
                api_url=api_url,
                api_token=api_token,
                payload=payload,
                story_id=story_id,
                generate_timeout=generate_timeout,
                api_retries=api_retries,
                api_retry_backoff=api_retry_backoff,
                status_timeout=status_timeout,
                log_prefix=f"{reply['source']}:{reply.get('id')} GO ->",
            )
            if res.get("busy"):
                active_story_id = str(res.get("active_story_id") or "")
                log(
                    f"{reply['source']}:{reply.get('id')} GO -> skipped (mac_api busy) "
                    f"requested_story_id={story_id} active_story_id={active_story_id or 'unknown'}"
                )
                state["last_action"] = "GO_generate_busy_skip"
                state["last_generate_response"] = res
                state["last_processed_reply_ts"] = reply["ts"]
                state["last_processed_reply_id"] = str(reply.get("id") or "")
                save_state(state)
                return 0
            if not res.get("ok"):
                raise RuntimeError(f"/generate failed: {res}")
            state["generated_story_id"] = story_id
            state["last_action"] = "GO_generate"
            state["last_generate_response"] = res
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            save_state(state)
            log(f"{reply['source']}:{reply.get('id')} GO -> generated story_id={story_id}")
            return 0

        if story_id and state.get("generated_story_id") == story_id and state.get("published_story_id") != story_id:
            if not publish_enabled:
                if not story:
                    raise RuntimeError("GO refresh requested but no story package text available")
                payload = {
                    "story_id": story_id,
                    "story_text": story,
                    "prompt": extract_prompt(story),
                    "source_channel_id": cfg["DISCORD_CHANNEL_ID"],
                    "refresh_overlays_only": True,
                }
                log(
                    f"{reply['source']}:{reply.get('id')} GO -> call /generate "
                    f"(refresh_overlays_only=true, timeout={generate_timeout}s, retries={api_retries}) "
                    f"story_id={story_id}"
                )
                res = post_generate_with_status_recovery(
                    api_url=api_url,
                    api_token=api_token,
                    payload=payload,
                    story_id=story_id,
                    generate_timeout=generate_timeout,
                    api_retries=api_retries,
                    api_retry_backoff=api_retry_backoff,
                    status_timeout=status_timeout,
                    log_prefix=f"{reply['source']}:{reply.get('id')} GO refresh ->",
                )
                if res.get("busy"):
                    active_story_id = str(res.get("active_story_id") or "")
                    log(
                        f"{reply['source']}:{reply.get('id')} GO -> refresh skipped (mac_api busy) "
                        f"requested_story_id={story_id} active_story_id={active_story_id or 'unknown'}"
                    )
                    state["last_action"] = "GO_refresh_busy_skip"
                    state["last_generate_response"] = res
                    state["last_processed_reply_ts"] = reply["ts"]
                    state["last_processed_reply_id"] = str(reply.get("id") or "")
                    save_state(state)
                    return 0
                if not res.get("ok"):
                    raise RuntimeError(f"/generate refresh failed: {res}")
                state["generated_story_id"] = story_id
                state["last_action"] = "GO_refresh_overlays"
                state["last_generate_response"] = res
                state["last_processed_reply_ts"] = reply["ts"]
                state["last_processed_reply_id"] = str(reply.get("id") or "")
                save_state(state)
                log(f"{reply['source']}:{reply.get('id')} GO -> refreshed overlays for story_id={story_id}")
                return 0
            log(
                f"{reply['source']}:{reply.get('id')} GO -> call /publish "
                f"(timeout={publish_timeout}s, retries={api_retries}) story_id={story_id}"
            )
            res = post_json(
                f"{api_url}/publish",
                {"story_id": story_id, "source_channel_id": cfg["DISCORD_CHANNEL_ID"]},
                api_token,
                timeout_sec=publish_timeout,
                retries=api_retries,
                retry_backoff_sec=api_retry_backoff,
            )
            if not res.get("ok"):
                raise RuntimeError(f"/publish failed: {res}")
            state["published_story_id"] = story_id
            state["last_action"] = "GO_publish"
            state["last_publish_response"] = res
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            save_state(state)
            log(f"{reply['source']}:{reply.get('id')} GO -> published story_id={story_id}")
            return 0

    if reply["value"] == "POST":
        if not publish_enabled:
            log(
                f"{reply['source']}:{reply.get('id')} POST ignored: publish disabled "
                "(set PUBLISH_COMMAND_ENABLED=true to enable)"
            )
            state["last_action"] = "POST_disabled"
            state["last_processed_reply_ts"] = reply["ts"]
            state["last_processed_reply_id"] = str(reply.get("id") or "")
            save_state(state)
            return 0
        if not story_id:
            raise RuntimeError("No story available for POST")
        log(
            f"{reply['source']}:{reply.get('id')} POST -> call /publish "
            f"(timeout={publish_timeout}s, retries={api_retries}) story_id={story_id}"
        )
        res = post_json(
            f"{api_url}/publish",
            {"story_id": story_id, "source_channel_id": cfg["DISCORD_CHANNEL_ID"]},
            api_token,
            timeout_sec=publish_timeout,
            retries=api_retries,
            retry_backoff_sec=api_retry_backoff,
        )
        if not res.get("ok"):
            raise RuntimeError(f"/publish failed: {res}")
        state["published_story_id"] = story_id
        state["last_action"] = "POST_publish"
        state["last_publish_response"] = res
        state["last_processed_reply_ts"] = reply["ts"]
        state["last_processed_reply_id"] = str(reply.get("id") or "")
        save_state(state)
        log(f"{reply['source']}:{reply.get('id')} POST -> published story_id={story_id}")
        return 0

    if state_dirty:
        save_state(state)
    log("No action matched")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        raise
