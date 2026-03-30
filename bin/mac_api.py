#!/usr/bin/env python3
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib import parse, request

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "config" / "mac_api.env"
STATE_FILE = ROOT / "state" / "mac_api_state.json"
LOG_FILE = ROOT / "logs" / "mac_api.log"
OUT_DIR = ROOT / "output"

FINAL_CLIP_CANDIDATES = [
    "final_text.mp4",
    "final_vo_text.mp4",
    "final_vo_bgm.mp4",
    "final_vo.mp4",
    "final_bgm.mp4",
    "final_motion.mp4",
    "final_looped.mp4",
    "base.mp4",
]

GENERATE_LOCK = Lock()
GENERATE_ACTIVE_STORY_ID = ""
GENERATE_ACTIVE_SINCE = ""


class ComfyInterruptedError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now().isoformat()


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
    # OS environment variables override file values (e.g. docker-compose env).
    for k in list(out.keys()):
        env_val = os.environ.get(k)
        if env_val is not None:
            out[k] = env_val
    return out


def run_cmd(cmd, timeout=300):
    env = os.environ.copy()
    path = env.get("PATH", "")
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + ([path] if path else []))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr}")
    return p.stdout


def _is_timeout_like_error(err: Exception) -> bool:
    t = str(err or "").lower()
    return "timed out" in t or "timeout" in t


def restart_comfyui_service(reason: str = "") -> None:
    label = f"gui/{os.getuid()}/com.jonas.comfyui"
    try:
        run_cmd(["/bin/launchctl", "kickstart", "-k", label], timeout=30)
        if reason:
            log(f"Restarted ComfyUI service ({reason})")
        else:
            log("Restarted ComfyUI service")
    except Exception as e:
        if reason:
            log(f"ComfyUI restart failed ({reason}): {e}")
        else:
            log(f"ComfyUI restart failed: {e}")


def cleanup_failed_run_intermediates(run_dir: Optional[Path]) -> None:
    if not run_dir or not run_dir.exists() or not run_dir.is_dir():
        return

    keep_files: set[Path] = set()
    for name in FINAL_CLIP_CANDIDATES:
        p = run_dir / name
        if p.exists():
            keep_files.add(p.resolve())
    failed_preview = run_dir / "failed_preview.webm"
    if failed_preview.exists():
        keep_files.add(failed_preview.resolve())

    keep_text_suffixes = {".txt", ".json", ".vtt", ".md"}
    removed_files = 0
    removed_dirs = 0
    removed_bytes = 0

    for p in sorted(run_dir.iterdir(), key=lambda x: x.name):
        rp = p.resolve()
        if rp in keep_files:
            continue
        try:
            if p.is_dir():
                try:
                    removed_bytes += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                except Exception:
                    pass
                shutil.rmtree(p, ignore_errors=True)
                removed_dirs += 1
                continue
            if p.is_file():
                if p.suffix.lower() in keep_text_suffixes:
                    continue
                try:
                    removed_bytes += p.stat().st_size
                except Exception:
                    pass
                p.unlink(missing_ok=True)
                removed_files += 1
        except Exception as e:
            log(f"Failed-run cleanup skipped for {p}: {e}")

    if removed_files or removed_dirs:
        log(
            f"Cleaned failed run_dir={run_dir} removed_files={removed_files} "
            f"removed_dirs={removed_dirs} freed_mb={removed_bytes / (1024 * 1024):.2f}"
        )


def preserve_latest_comfy_preview(
    run_dir: Optional[Path],
    cfg: Dict[str, str],
    min_mtime_ts: float = 0.0,
) -> Optional[Path]:
    if not run_dir:
        return None
    run_dir.mkdir(parents=True, exist_ok=True)

    preview_dir_raw = str(cfg.get("COMFYUI_OUTPUT_DIR", "")).strip()
    if preview_dir_raw:
        preview_dir = Path(os.path.expanduser(preview_dir_raw))
    else:
        comfy_dir = Path(os.path.expanduser(cfg.get("COMFYUI_DIR", "~/ComfyUI")))
        preview_dir = comfy_dir / "output"
    if not preview_dir.exists() or not preview_dir.is_dir():
        return None

    patterns = [p.strip() for p in str(cfg.get("COMFYUI_PREVIEW_PATTERN", "claw_preview*")).split(",") if p.strip()]
    if not patterns:
        patterns = ["claw_preview*"]

    latest: Optional[Path] = None
    latest_mtime = 0.0
    seen: set[str] = set()
    for pat in patterns:
        for f in preview_dir.glob(pat):
            if not f.is_file():
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                mt = f.stat().st_mtime
            except Exception:
                continue
            if min_mtime_ts > 0 and mt < min_mtime_ts:
                continue
            if mt > latest_mtime:
                latest = f
                latest_mtime = mt

    if not latest:
        return None

    target = run_dir / "failed_preview.webm"
    try:
        shutil.copy2(latest, target)
        log(f"Preserved latest ComfyUI preview for failed run: {latest} -> {target}")
        return target
    except Exception as e:
        log(f"Failed to preserve ComfyUI preview from {latest} to {target}: {e}")
        return None


def _recover_clip_from_comfy_preview(
    out_file: Path,
    cfg: Dict[str, str],
    min_mtime_ts: float,
    skip_paths: Optional[set] = None,
) -> bool:
    """Convert the newest ComfyUI preview webm (newer than min_mtime_ts) to mp4.

    Returns True if a usable preview was found and converted successfully.
    skip_paths is a set of resolved path strings already claimed by earlier variants.
    """
    preview_dir_raw = str(cfg.get("COMFYUI_OUTPUT_DIR", "")).strip()
    if preview_dir_raw:
        preview_dir = Path(os.path.expanduser(preview_dir_raw))
    else:
        comfy_dir = Path(os.path.expanduser(cfg.get("COMFYUI_DIR", "~/ComfyUI")))
        preview_dir = comfy_dir / "output"
    if not preview_dir.exists() or not preview_dir.is_dir():
        return False

    patterns = [p.strip() for p in str(cfg.get("COMFYUI_PREVIEW_PATTERN", "claw_preview*")).split(",") if p.strip()]
    if not patterns:
        patterns = ["claw_preview*"]

    candidates: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for pat in patterns:
        for f in preview_dir.glob(pat):
            if not f.is_file():
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            if skip_paths and key in skip_paths:
                continue
            seen.add(key)
            try:
                mt = f.stat().st_mtime
            except Exception:
                continue
            if mt < min_mtime_ts:
                continue
            candidates.append((mt, f))

    if not candidates:
        return False

    # Use the oldest unassigned preview (first one generated for this job)
    candidates.sort(key=lambda x: x[0])
    _, src = candidates[0]

    log(f"Network-error recovery: converting {src.name} -> {out_file.name}")
    try:
        run_cmd(
            ["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out_file)],
            timeout=300,
        )
        if out_file.exists() and out_file.stat().st_size > 0:
            log(f"Recovery succeeded: {out_file.name} from {src.name}")
            if skip_paths is not None:
                skip_paths.add(str(src.resolve()))
            return True
    except Exception as e:
        log(f"Recovery conversion failed: {e}")
    return False


def _tail_text_file(path: Path, max_lines: int, max_chars: int) -> str:
    max_lines = max(1, min(max_lines, 200))
    max_chars = max(200, min(max_chars, 20000))
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    text = text.replace("\r", "\n")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def http_json(url: str, payload: Dict[str, Any], timeout: int = 240) -> Dict[str, Any]:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    return json.loads(raw or "{}")


def maybe_enhance_prompt_with_ollama(prompt: str, cfg: Dict[str, str]) -> str:
    model = cfg.get("OLLAMA_PROMPT_MODEL", "").strip()
    if not model:
        return prompt
    base = cfg.get("OLLAMA_API_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        payload = {
            "model": model,
            "stream": False,
            "prompt": (
                "Rewrite this for text-to-video generation, concise and visual, no markdown:\n\n"
                + prompt
            ),
        }
        resp = http_json(f"{base}/api/generate", payload, timeout=120)
        out = str(resp.get("response", "")).strip()
        return out if out else prompt
    except Exception as e:
        log(f"Ollama prompt enhancement failed: {e}")
        return prompt


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {"stories": {}, "last_story_id": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"stories": {}, "last_story_id": None}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _default_active_status() -> Dict[str, Any]:
    return {
        "stage": "idle",
        "detail": "",
        "story_id": "",
        "started_at": None,
        "updated_at": _now_iso(),
    }


def load_active_status(state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if state is None:
        state = load_state()
    active = state.get("active_status") if isinstance(state, dict) else None
    if not isinstance(active, dict):
        active = _default_active_status()
    # Ensure required keys exist
    for k, v in _default_active_status().items():
        active.setdefault(k, v)
    return active


def update_active_status(stage: str, detail: str = "", story_id: str = "") -> Dict[str, Any]:
    state = load_state()
    active = load_active_status(state)
    if stage == "idle":
        active = _default_active_status()
    else:
        if story_id:
            previous_story_id = str(active.get("story_id") or "")
            # Reset start time when a new story becomes active; keep it for
            # same-story status updates.
            if previous_story_id != story_id:
                active["started_at"] = _now_iso()
            elif not active.get("started_at"):
                active["started_at"] = _now_iso()
            active["story_id"] = story_id
        active["stage"] = stage
        active["detail"] = detail
        active["updated_at"] = _now_iso()
    state["active_status"] = active
    save_state(state)
    return active


def format_status_message(status: Dict[str, Any], state: Dict[str, Any], lang: str = "en") -> str:
    stories = state.get("stories", {}) if isinstance(state, dict) else {}
    story_id = status.get("story_id") or state.get("last_story_id")
    info = stories.get(str(story_id), {}) if story_id else {}

    title = info.get("title") or "Story"
    stage = status.get("stage", "idle")
    detail = status.get("detail", "")
    started = status.get("started_at") or "n/a"
    updated = status.get("updated_at") or "n/a"

    if lang.lower().startswith("de"):
        heading = "Status"
        lines = [
            f"{heading}: {stage}",
            f"Story: {title} ({story_id or 'keine'})",
            f"Detail: {detail or '-'}",
            f"Gestartet: {started}",
            f"Aktualisiert: {updated}",
        ]
    else:
        heading = "Status"
        lines = [
            f"{heading}: {stage}",
            f"Story: {title} ({story_id or 'none'})",
            f"Detail: {detail or '-'}",
            f"Started: {started}",
            f"Updated: {updated}",
        ]

    if info and info.get("clip_path"):
        clip_name = Path(info["clip_path"]).name
        if lang.lower().startswith("de"):
            lines.append(f"Letzter Clip: {clip_name}")
        else:
            lines.append(f"Last Clip: {clip_name}")
    return "\n".join(lines)


def status_payload(state: Dict[str, Any], story_id: str = "") -> Dict[str, Any]:
    active = load_active_status(state)
    sid = story_id or active.get("story_id") or state.get("last_story_id")
    stories = state.get("stories", {}) if isinstance(state, dict) else {}
    info = stories.get(str(sid), {}) if sid else {}
    summary = {
        "story_id": sid,
        "title": info.get("title"),
        "clip_path": info.get("clip_path"),
        "generated_at": info.get("generated_at"),
        "published": info.get("published", False),
        "published_at": info.get("published_at"),
    } if info else {}
    return {"ok": True, "active_status": active, "last_story": summary}


def pick_story_clip(run_dir: Path) -> Optional[Path]:
    if not run_dir.exists():
        return None
    for name in FINAL_CLIP_CANDIDATES:
        p = run_dir / name
        if p.exists():
            return p
    return None


def legacy_story_dirs(story_id: str) -> List[Path]:
    if not story_id:
        return []
    short = story_id[:8]
    dirs = [p for p in OUT_DIR.glob(f"*_{short}") if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs


def find_existing_story_clip(story_id: str, run_dir: Path, state_clip_path: str = "") -> Optional[Path]:
    if state_clip_path:
        p = Path(state_clip_path)
        if p.exists():
            return p

    direct = pick_story_clip(run_dir)
    if direct:
        return direct

    for legacy in legacy_story_dirs(story_id):
        found = pick_story_clip(legacy)
        if found:
            return found
    return None


def cleanup_legacy_story_dirs(story_id: str, keep_dir: Path) -> None:
    keep = str(keep_dir.resolve())
    for p in legacy_story_dirs(story_id):
        try:
            if str(p.resolve()) == keep:
                continue
            shutil.rmtree(p)
            log(f"Removed legacy run dir for story_id={story_id}: {p}")
        except Exception as e:
            log(f"Legacy cleanup skipped for {p}: {e}")


def cleanup_old_output_dirs(cfg: Dict[str, str]) -> None:
    # 1) Remove old story output directories.
    output_enabled = str(cfg.get("OUTPUT_CLEANUP_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if output_enabled:
        days = max(1, min(90, int(cfg.get("OUTPUT_RETENTION_DAYS", "7"))))
        cutoff = datetime.now() - timedelta(days=days)
        for p in OUT_DIR.iterdir():
            if not p.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime > cutoff:
                    continue
                shutil.rmtree(p)
                log(f"Removed old output dir (> {days}d): {p}")
            except Exception as e:
                log(f"Old output cleanup skipped for {p}: {e}")

    # 2) Remove stale ComfyUI preview renders (claw_preview* by default).
    preview_enabled = str(cfg.get("COMFYUI_PREVIEW_CLEANUP_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if not preview_enabled:
        return

    preview_days = max(1, min(90, int(cfg.get("COMFYUI_PREVIEW_RETENTION_DAYS", "2"))))
    preview_cutoff = datetime.now() - timedelta(days=preview_days)
    preview_dir_raw = str(cfg.get("COMFYUI_OUTPUT_DIR", "")).strip()
    if preview_dir_raw:
        preview_dir = Path(os.path.expanduser(preview_dir_raw))
    else:
        comfy_dir = Path(os.path.expanduser(cfg.get("COMFYUI_DIR", "~/ComfyUI")))
        preview_dir = comfy_dir / "output"
    if not preview_dir.exists() or not preview_dir.is_dir():
        return

    patterns = [p.strip() for p in str(cfg.get("COMFYUI_PREVIEW_PATTERN", "claw_preview*")).split(",") if p.strip()]
    if not patterns:
        patterns = ["claw_preview*"]

    removed_files = 0
    removed_bytes = 0
    seen: set[str] = set()
    for pat in patterns:
        for f in preview_dir.glob(pat):
            if not f.is_file():
                continue
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime > preview_cutoff:
                    continue
                removed_bytes += f.stat().st_size
                f.unlink(missing_ok=True)
                removed_files += 1
            except Exception as e:
                log(f"ComfyUI preview cleanup skipped for {f}: {e}")

    if removed_files > 0:
        log(
            f"Removed old ComfyUI previews (> {preview_days}d): files={removed_files} "
            f"freed_mb={removed_bytes / (1024 * 1024):.2f} dir={preview_dir}"
        )


def prune_story_intermediates(run_dir: Path, final_clip: Path, cfg: Dict[str, str]) -> None:
    enabled = str(cfg.get("OUTPUT_PRUNE_INTERMEDIATES", "false")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return
    if not run_dir.exists() or not run_dir.is_dir():
        return
    if not final_clip.exists():
        return

    keep_text = str(cfg.get("OUTPUT_PRUNE_KEEP_TEXT_FILES", "true")).strip().lower() in {"1", "true", "yes", "on"}
    keep: set[Path] = {final_clip.resolve()}
    if keep_text:
        for name in ("story.txt", "prompt.txt"):
            p = run_dir / name
            if p.exists():
                keep.add(p.resolve())

    removed_files = 0
    removed_dirs = 0
    removed_bytes = 0

    for p in sorted(run_dir.iterdir(), key=lambda x: x.name):
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp in keep:
            continue
        try:
            if p.is_file():
                try:
                    removed_bytes += p.stat().st_size
                except Exception:
                    pass
                p.unlink(missing_ok=True)
                removed_files += 1
                continue
            if p.is_dir():
                try:
                    removed_bytes += sum(
                        f.stat().st_size for f in p.rglob("*") if f.is_file()
                    )
                except Exception:
                    pass
                shutil.rmtree(p, ignore_errors=True)
                removed_dirs += 1
        except Exception as e:
            log(f"Intermediate prune skipped for {p}: {e}")

    if removed_files or removed_dirs:
        log(
            f"Pruned run_dir={run_dir} kept={final_clip.name} "
            f"removed_files={removed_files} removed_dirs={removed_dirs} "
            f"freed_mb={removed_bytes / (1024 * 1024):.2f}"
        )


def extract_title(story_text: str) -> str:
    raw = _extract_section(story_text, r"Title")
    if raw:
        first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        if first:
            return _clean_spoken_text(first)
    return "Story Preview"


def extract_caption_hashtags(story_text: str) -> Dict[str, str]:
    caption = ""
    hashtags = ""
    cm = _extract_section(story_text, r"Caption")
    if cm:
        caption = re.sub(r"\s+", " ", cm).strip()
    hm = _extract_section(story_text, r"Hashtags?")
    if hm:
        hashtags = re.sub(r"\s+", " ", hm).strip()
    return {"caption": caption, "hashtags": hashtags}


def _extract_section(story_text: str, label_pattern: str) -> str:
    # Supports markdown/plain headings and inline/multiline section bodies.
    text = story_text.replace("\r\n", "\n").replace("\r", "\n")
    section_stop = (
        r"(?:Title|Hook(?:\s*\([^\)\n]*\))?|Voiceover(?:\s+Script)?(?:\s*\([^\)\n]*\))?|"
        r"On[-\s]*screen\s+text\s+beats?(?:\s*\([^\)\n]*\))?|Visual(?:\s+Plan)?(?:\s*\([^\)\n]*\))?|"
        r"Music(?:\s+direction)?(?:\s*\([^\)\n]*\))?|Voice(?:\s+choice)?(?:\s*\([^\)\n]*\))?|Caption|Hashtags?)"
    )

    start_patterns = [
        re.compile(
            rf"(?im)^\s*\*\*{label_pattern}\s*:?\s*\*\*\s*(?P<inline>[^\n]*)$",
        ),
        re.compile(
            rf"(?im)^\s*{label_pattern}\s*:?\s*(?P<inline>[^\n]*)$",
        ),
    ]

    start_match: Optional[re.Match[str]] = None
    for pattern in start_patterns:
        m = pattern.search(text)
        if m and (start_match is None or m.start() < start_match.start()):
            start_match = m
    if not start_match:
        return ""

    inline = (start_match.groupdict().get("inline") or "").strip()
    stop_re = re.compile(
        rf"(?im)^\s*(?:\*\*(?:{section_stop})\s*:?\s*\*\*|(?:{section_stop})\s*:?).*$",
    )
    next_match = stop_re.search(text, start_match.end())
    body = text[start_match.end() : (next_match.start() if next_match else len(text))].strip()

    if inline and body:
        return f"{inline}\n{body}".strip()
    if inline:
        return inline
    return body


def _clean_spoken_text(text: str) -> str:
    t = text.replace("\r", "\n")
    t = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"#[A-Za-z0-9_]+", " ", t)
    t = re.sub(r"[>*_]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _trim_voiceover_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sentence_cut = re.sub(r"[^.!?]+$", "", cut).strip()
    if sentence_cut:
        return sentence_cut
    return cut.rstrip(" ,;:") + "."


def extract_voiceover_text(story_text: str) -> str:
    # Extract the dedicated voiceover block and prepend the hook so it is
    # spoken as the opening line of the narration.
    raw = _extract_section(story_text, r"Voiceover(?:\s+Script)?(?:\s*\(.*?\))?")
    if not raw:
        return ""
    hook_raw = _extract_section(story_text, r"Hook(?:\s*\(.*?\))?")
    text = _clean_spoken_text(raw)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = text.strip()
    if hook_raw:
        hook = _clean_spoken_text(hook_raw)
        hook = re.sub(r"\s+([,.;:!?])", r"\1", hook).strip()
        if hook and not text.startswith(hook):
            text = f"{hook} {text}"
    return text


def extract_visual_plan_text(story_text: str) -> str:
    items = extract_visual_plan_items(story_text, max_items=5)
    return "; ".join(items).strip()


def extract_visual_plan_items(story_text: str, max_items: int = 5) -> List[str]:
    max_items = max(1, min(12, int(max_items)))
    raw = _extract_section(story_text, r"Visual(?:\s+Plan)?(?:\s*\(.*?\))?")
    if not raw:
        return []
    lines: List[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^\s*[-*•]\s*", "", s)
        # Strip numeric bullets like "1. text" or "2) text", but do not
        # touch decimal timestamps like "1.5-3.0s: ...".
        s = re.sub(r"^\s*\d+[\.\)]\s+", "", s)
        s = _clean_spoken_text(s)
        if s:
            lines.append(s)
    if not lines:
        lines = [_clean_spoken_text(raw)]
    return [ln for ln in lines[:max_items] if ln]


def extract_music_direction_text(story_text: str) -> str:
    raw = _extract_section(story_text, r"Music(?:\s+direction)?(?:\s*\(.*?\))?")
    if not raw:
        return ""
    text = _clean_spoken_text(raw)
    return re.sub(r"\s+", " ", text).strip()


def extract_voice_choice_text(story_text: str) -> str:
    raw = _extract_section(story_text, r"Voice\s+choice(?:\s*\(.*?\))?")
    if not raw:
        return ""
    return re.sub(r"\s+", " ", _clean_spoken_text(raw)).strip()


def _sanitize_prompt_text(text: str) -> str:
    t = str(text or "").replace("\r", "\n")
    # Remove empty heading residues like "Theme:" that add noise for T2V.
    t = re.sub(r"\b(?:theme|visual\s+plan)\s*:\s*(?=[,.;:\n]|$)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:!?]){2,}", r"\1", t)
    return t.strip(" .")


def _truncate_prompt(text: str, cfg: Dict[str, str], key: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    max_chars = max(0, min(2000, int(cfg.get(key, "0"))))
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw
    cut = raw[:max_chars]
    # Prefer finishing at a natural separator near the end.
    sep_pos = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(", "), cut.rfind(" "))
    min_good = int(max_chars * 0.65)
    if sep_pos >= min_good:
        cut = cut[:sep_pos]
    return cut.strip(" ,.;:") + "."


def _stability_hint(cfg: Dict[str, str]) -> str:
    enabled = str(cfg.get("PROMPT_STABILITY_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return ""
    raw = cfg.get(
        "PROMPT_STABILITY_HINT",
        (
            "single primary subject, consistent identity across frames, same face and clothing, "
            "same key props, no morphing or transformation, physically plausible motion, clean anatomy"
        ),
    )
    return _sanitize_prompt_text(raw)


def merge_prompt_with_story_plan(prompt: str, story_text: str, cfg: Dict[str, str]) -> str:
    style_hint = _sanitize_prompt_text(
        cfg.get(
        "PROMPT_STYLE_HINT",
        "thrilling cinematic tension, dramatic lighting, realistic details, dynamic framing",
    ).strip()
    )
    visual_items = extract_visual_plan_items(story_text, max_items=8)
    visual_mode = str(cfg.get("PROMPT_VISUAL_PLAN_MODE", "anchor")).strip().lower()
    visual_max_items = max(1, min(8, int(cfg.get("PROMPT_VISUAL_PLAN_MAX_ITEMS", "2"))))
    subject_lock = _sanitize_prompt_text(cfg.get("PROMPT_SUBJECT_LOCK", "").strip())
    if not subject_lock and visual_items:
        subject_lock = visual_items[0]

    parts = [_sanitize_prompt_text(prompt)]
    if visual_mode in {"full", "all"} and visual_items:
        merged_visual = "; ".join(visual_items[:visual_max_items]).strip()
        if merged_visual:
            parts.append(f"Visual context: {merged_visual}")
    elif visual_mode not in {"none", "off"} and subject_lock:
        parts.append(f"Subject lock: {subject_lock}")
    stability_hint = _stability_hint(cfg)
    if stability_hint:
        parts.append(stability_hint)
    if style_hint:
        parts.append(style_hint)
    merged = ". ".join([p for p in parts if p]).strip()
    merged = _sanitize_prompt_text(merged)
    return _truncate_prompt(merged, cfg, "PROMPT_MAX_CHARS")


def _parse_inline_time_range(raw: str) -> tuple[Optional[float], Optional[float], str]:
    # Accept flexible beat timing prefixes, e.g.:
    # 0-3s text
    # 0s-3s: text
    # 00:00-00:03 - text
    m = re.match(
        r"^\s*([0-9:.]+(?:[.,][0-9]+)?s?)\s*-\s*([0-9:.]+(?:[.,][0-9]+)?s?)\s*(?:[:\-–]\s*|\s+)(.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        # Best effort: strip a timing prefix even when we cannot produce valid windows.
        stripped = re.sub(
            r"^\s*[0-9:.]+(?:[.,][0-9]+)?s?\s*-\s*[0-9:.]+(?:[.,][0-9]+)?s?\s*(?:[:\-–]\s*|\s+)",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        return None, None, stripped or raw
    start = _vtt_time_to_seconds(m.group(1))
    end = _vtt_time_to_seconds(m.group(2))
    if start is None or end is None or end <= start:
        return None, None, m.group(3).strip()
    return float(start), float(end), m.group(3).strip()


def _compress_timed_beats(
    beats: List[tuple[str, Optional[float], Optional[float]]],
    max_beats: int,
) -> List[tuple[str, Optional[float], Optional[float]]]:
    if max_beats <= 0:
        return []
    if len(beats) <= max_beats:
        return beats
    if max_beats == 1:
        return [beats[0]]

    # Keep beats spread across the timeline instead of taking only the first N.
    raw_idxs = [round(i * (len(beats) - 1) / (max_beats - 1)) for i in range(max_beats)]
    seen: set[int] = set()
    idxs: List[int] = []
    for idx in raw_idxs:
        idx = max(0, min(len(beats) - 1, idx))
        if idx not in seen:
            seen.add(idx)
            idxs.append(idx)

    if len(idxs) < max_beats:
        for idx in range(len(beats)):
            if idx in seen:
                continue
            seen.add(idx)
            idxs.append(idx)
            if len(idxs) >= max_beats:
                break

    idxs.sort()
    return [beats[i] for i in idxs[:max_beats]]


def extract_on_screen_text_beats_timed(story_text: str) -> List[tuple[str, Optional[float], Optional[float]]]:
    raw = _extract_section(story_text, r"On[-\s]*screen\s+text\s+beats?(?:\s*\(.*?\))?")
    if not raw:
        return []
    beats: List[tuple[str, Optional[float], Optional[float]]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^\s*[-*•]\s*", "", s)
        # Strip numeric bullets like "1. text" or "2) text", but do not
        # touch decimal timestamps like "1.5-3.0s: ...".
        s = re.sub(r"^\s*\d+[\.\)]\s+", "", s)
        start, end, txt = _parse_inline_time_range(s)
        txt = _clean_spoken_text(txt)
        if txt:
            beats.append((txt, start, end))
    # Keep on-screen beats concise: max 4 total.
    return _compress_timed_beats(beats, max_beats=4)


def extract_on_screen_text_beats(story_text: str) -> List[str]:
    return [t for t, _, _ in extract_on_screen_text_beats_timed(story_text)]


def scene_hints_from_story(story_text: str, cfg: Optional[Dict[str, str]] = None) -> List[str]:
    hints: List[str] = []
    visual_max_items = 6
    include_text_beats = False
    if cfg:
        visual_max_items = max(1, min(8, int(cfg.get("PROMPT_SCENE_HINT_MAX_ITEMS", "6"))))
        include_text_beats = str(cfg.get("PROMPT_INCLUDE_TEXT_BEATS_IN_SCENE_HINTS", "false")).strip().lower() in {
            "1", "true", "yes", "on"
        }
    for part in extract_visual_plan_items(story_text, max_items=visual_max_items):
        s = _sanitize_prompt_text(part)
        if s and s not in hints:
            hints.append(s)
    if include_text_beats:
        for beat in extract_on_screen_text_beats(story_text):
            s = _sanitize_prompt_text(beat)
            if s and s not in hints:
                hints.append(s)
    return hints[:12]


def _config_list(value: str) -> List[str]:
    raw_val = str(value or "")
    sep = "|" if "|" in raw_val else ","
    out: List[str] = []
    for raw in raw_val.split(sep):
        s = _sanitize_prompt_text(raw)
        if s:
            out.append(s)
    return out


def compose_scene_prompt(
    merged_prompt: str,
    scene_hint: str,
    shot_index: int,
    total_shots: int,
    cfg: Dict[str, str],
) -> str:
    auto_wrap = str(cfg.get("PROMPT_AUTO_SHOT_WRAPPER_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    base = _sanitize_prompt_text(merged_prompt)
    base = _truncate_prompt(base, cfg, "PROMPT_BASE_FOR_SHOTS_MAX_CHARS")
    scene = _sanitize_prompt_text(scene_hint)
    if not auto_wrap:
        p = base
        if scene:
            p = f"{p}. Scene detail: {scene}"
        return _truncate_prompt(_sanitize_prompt_text(p), cfg, "PROMPT_SCENE_MAX_CHARS")

    recipes = _config_list(
        cfg.get(
            "PROMPT_SHOT_RECIPES",
            (
                "medium-wide establish, controlled dolly-in, clean silhouette, readable background separation, "
                "tight close-up on face and hands, shallow depth feel, emotional micro-expression focus, "
                "dynamic side tracking, subtle parallax, stable subject lock, no sudden jumps"
            ),
        )
    )
    if not recipes:
        recipes = ["cinematic framing, stable subject, controlled movement"]
    recipe = recipes[(shot_index - 1) % len(recipes)]

    continuity = _sanitize_prompt_text(
        cfg.get(
            "PROMPT_SHOT_CONTINUITY_HINT",
            "same person identity and outfit, same key props, consistent lighting logic, no morphing",
        )
    )
    quality = _sanitize_prompt_text(
        cfg.get(
            "PROMPT_QUALITY_HINT",
            "high detail, clean edges, coherent anatomy, coherent geometry, consistent temporal motion",
        )
    )
    motion = _sanitize_prompt_text(
        cfg.get(
            "PROMPT_MOTION_HINT",
            "natural motion cadence, no flicker, no warping, no abrupt camera direction changes",
        )
    )

    parts: List[str] = [base]
    parts.append(f"Shot {shot_index}/{max(1, total_shots)}")
    if scene:
        parts.append(f"Scene: {scene}")
    if recipe:
        parts.append(f"Camera: {recipe}")
    if continuity:
        parts.append(continuity)
    if quality:
        parts.append(quality)
    if motion:
        parts.append(motion)
    prompt = ". ".join([p for p in parts if p]).strip()
    return _truncate_prompt(_sanitize_prompt_text(prompt), cfg, "PROMPT_SCENE_MAX_CHARS")


def resolve_comfy_negative_prompt(cfg: Dict[str, str]) -> str:
    base = str(
        cfg.get(
            "COMFYUI_NEGATIVE_PROMPT",
            "text, watermark, logo, subtitles, static frame, blur, low quality, ugly, deformed face",
        )
    ).strip()
    enabled = str(cfg.get("COMFYUI_NEGATIVE_PROMPT_STABILITY_ENABLED", "true")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    if not enabled:
        return base
    extra = str(
        cfg.get(
            "COMFYUI_NEGATIVE_PROMPT_STABILITY_TERMS",
            (
                "morphing, mutation, transformation, identity drift, face swap, duplicate person, "
                "floating limbs, fused fingers, warped geometry, melted objects, circular blob artifact"
            ),
        )
    ).strip()
    merged_parts: List[str] = []
    seen: set[str] = set()
    for chunk in f"{base},{extra}".split(","):
        term = chunk.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        merged_parts.append(term)
    return ", ".join(merged_parts)


def _caption_chunk_limits(cfg: Dict[str, str]) -> tuple[int, int, int, int]:
    min_words = int(cfg.get("VOICEOVER_CAPTIONS_MIN_WORDS", "2"))
    max_words = int(cfg.get("VOICEOVER_CAPTIONS_MAX_WORDS", "4"))
    min_words = max(1, min(4, min_words))
    max_words = max(2, min(4, max_words))
    if min_words > max_words:
        min_words = max_words
    max_chars = max(10, min(90, int(cfg.get("VOICEOVER_CAPTIONS_MAX_CHARS", "32"))))
    max_chunks = max(6, min(120, int(cfg.get("VOICEOVER_CAPTIONS_MAX_CHUNKS", "60"))))
    return min_words, max_words, max_chars, max_chunks


def build_voiceover_caption_chunks(text: str, cfg: Dict[str, str]) -> List[str]:
    min_words, max_words, max_chars, max_chunks = _caption_chunk_limits(cfg)

    cleaned = _clean_spoken_text(text)
    if not cleaned:
        return []

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if not sentences:
        sentences = [cleaned]

    chunks: List[str] = []
    for sentence in sentences:
        chunks.extend(
            _split_caption_text(
                sentence,
                min_words=min_words,
                max_words=max_words,
                max_chars=max_chars,
            )
        )

    chunks = [c for c in chunks if c][:max_chunks]
    return chunks[:max_chunks]


def _caption_speech_weight(chunk: str) -> float:
    words = re.findall(r"[A-Za-z0-9']+", chunk)
    if not words:
        return 1.0
    chars = sum(len(w) for w in words)
    punct_pause = (
        chunk.count(",") * 0.45
        + chunk.count(";") * 0.55
        + chunk.count(":") * 0.55
        + chunk.count(".") * 0.9
        + chunk.count("?") * 1.0
        + chunk.count("!") * 1.0
    )
    return max(1.0, float(len(words)) + (chars * 0.08) + punct_pause)


def _build_weighted_caption_windows(chunks: List[str], speech_duration: float, cfg: Dict[str, str]) -> List[tuple[float, float]]:
    if not chunks or speech_duration <= 0:
        return []
    lead_in = max(0.0, min(1.2, float(cfg.get("VOICEOVER_CAPTIONS_LEAD_IN_SEC", "0.03"))))
    tail_hold = max(0.0, min(1.2, float(cfg.get("VOICEOVER_CAPTIONS_TAIL_HOLD_SEC", "0.10"))))
    gap = max(0.0, min(0.5, float(cfg.get("VOICEOVER_CAPTIONS_GAP_SEC", "0.05"))))
    usable = speech_duration - lead_in - tail_hold - (gap * max(0, len(chunks) - 1))
    usable = max(0.4, usable)
    min_seg = max(0.08, min(0.40, float(cfg.get("VOICEOVER_CAPTIONS_MIN_SEG_SEC", "0.16"))))

    weights = [_caption_speech_weight(c) for c in chunks]
    total_w = sum(weights) or 1.0
    segs = [usable * (w / total_w) for w in weights]
    floor = min(min_seg, usable / max(1, len(chunks)))
    segs = [max(floor, s) for s in segs]
    seg_total = sum(segs) or 1.0
    scale = usable / seg_total
    segs = [s * scale for s in segs]

    windows: List[tuple[float, float]] = []
    cursor = lead_in
    for i, seg in enumerate(segs):
        start = max(0.0, min(speech_duration, cursor))
        end = start + max(0.08, seg)
        if i == len(segs) - 1:
            end = max(end, speech_duration - (tail_hold * 0.2))
        end = max(start + 0.10, min(speech_duration, end))
        windows.append((start, end))
        cursor = end + gap
    return windows


def _vtt_time_to_seconds(raw: str) -> Optional[float]:
    s = raw.strip().lower().replace(",", ".")
    if s.endswith("s"):
        s = s[:-1].strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        return float(s)
    except Exception:
        return None


def _parse_webvtt_cues(vtt_file: Path) -> List[tuple[str, float, float]]:
    try:
        lines = vtt_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    cues: List[tuple[str, float, float]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue
        m = re.match(r"\s*([0-9:.,]+)\s*-->\s*([0-9:.,]+)", line)
        if not m:
            i += 1
            continue
        start = _vtt_time_to_seconds(m.group(1))
        end = _vtt_time_to_seconds(m.group(2))
        i += 1

        parts: List[str] = []
        while i < len(lines) and lines[i].strip():
            parts.append(lines[i].strip())
            i += 1
        i += 1

        txt = re.sub(r"<[^>]+>", " ", " ".join(parts))
        txt = _clean_spoken_text(txt)
        txt = re.sub(r"\s+([,.;:!?])", r"\1", txt)
        if txt and start is not None and end is not None and end > start:
            cues.append((txt, float(start), float(end)))
    return cues


def _split_caption_text(text: str, min_words: int, max_words: int, max_chars: int) -> List[str]:
    cleaned = _clean_spoken_text(text)
    if not cleaned:
        return []
    words = cleaned.split()
    if not words:
        return []
    groups: List[List[str]] = []
    cur: List[str] = []
    for w in words:
        candidate = " ".join(cur + [w]).strip()
        if cur and (len(cur) >= max_words or len(candidate) > max_chars):
            groups.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        groups.append(cur)

    i = 0
    while i < len(groups):
        g = groups[i]
        if len(g) >= min_words or len(groups) == 1:
            i += 1
            continue
        borrowed = False
        if i + 1 < len(groups) and len(groups[i + 1]) > min_words:
            g.append(groups[i + 1].pop(0))
            borrowed = True
        elif i > 0 and len(groups[i - 1]) > min_words:
            g.insert(0, groups[i - 1].pop())
            borrowed = True
        if borrowed and len(g) >= min_words:
            i += 1
            continue
        if i + 1 < len(groups):
            groups[i + 1] = g + groups[i + 1]
            groups.pop(i)
        elif i > 0:
            groups[i - 1].extend(g)
            groups.pop(i)
            i = max(0, i - 1)
        else:
            i += 1

    normalized: List[List[str]] = []
    for g in groups:
        if len(g) <= max_words:
            normalized.append(g)
            continue
        j = 0
        while j < len(g):
            normalized.append(g[j : j + max_words])
            j += max_words
    groups = [g for g in normalized if g]

    if len(groups) >= 2 and len(groups[-1]) < min_words:
        need = min_words - len(groups[-1])
        movable = max(0, len(groups[-2]) - min_words)
        take = min(need, movable)
        if take > 0:
            groups[-1] = groups[-2][-take:] + groups[-1]
            groups[-2] = groups[-2][:-take]
        if len(groups[-1]) < min_words:
            groups[-2].extend(groups[-1])
            groups.pop()

    parts = [" ".join(g).strip(" ,;:") for g in groups if g]
    return [p for p in parts if p]


def _limit_timed_cues(cues: List[tuple[str, float, float]], cfg: Dict[str, str], max_end: float = 0.0) -> List[tuple[str, float, float]]:
    if not cues:
        return []

    min_words, max_words, max_chars, max_chunks = _caption_chunk_limits(cfg)
    min_seg = max(0.08, min(0.40, float(cfg.get("VOICEOVER_CAPTIONS_MIN_SEG_SEC", "0.16"))))

    normalized: List[tuple[str, float, float]] = []
    for text, start, end in cues:
        s = max(0.0, float(start))
        e = max(s + 0.10, float(end))
        if max_end > 0:
            s = min(max_end, s)
            e = min(max_end, e)
            if e <= s:
                continue
        parts = _split_caption_text(
            text,
            min_words=min_words,
            max_words=max_words,
            max_chars=max_chars,
        )
        if not parts:
            continue
        if len(parts) == 1:
            normalized.append((parts[0], s, e))
            continue
        while len(parts) > 1 and ((e - s) / len(parts)) < min_seg:
            parts[-2] = _clean_spoken_text(parts[-2] + " " + parts[-1])
            parts.pop()
        span = max(0.10, e - s)
        seg = span / len(parts)
        for i, part in enumerate(parts):
            ps = s + (i * seg)
            pe = e if i == len(parts) - 1 else min(e, s + ((i + 1) * seg))
            normalized.append((part, ps, max(ps + 0.10, pe)))

    if len(normalized) <= max_chunks:
        return normalized

    step = int(math.ceil(len(normalized) / max_chunks))
    merged: List[tuple[str, float, float]] = []
    for i in range(0, len(normalized), step):
        group = normalized[i : i + step]
        start = group[0][1]
        end = group[-1][2]
        text = _clean_spoken_text(" ".join(g[0] for g in group))
        if text:
            merged.append((text, start, end))
    return merged


def build_voiceover_timed_cues(
    voiceover_text: str,
    voice_duration: float,
    cfg: Dict[str, str],
    subtitle_file: Optional[Path] = None,
) -> List[tuple[str, float, float]]:
    # 1) Best case: use subtitle timing emitted by backend (e.g. edge-tts .vtt).
    if subtitle_file and subtitle_file.exists():
        parsed = _parse_webvtt_cues(subtitle_file)
        limited = _limit_timed_cues(parsed, cfg, max_end=max(0.0, voice_duration))
        if limited:
            return limited

    # 2) Fallback: estimate timings by chunk speech weight over measured VO duration.
    chunks = build_voiceover_caption_chunks(voiceover_text, cfg)
    if not chunks:
        return []

    windows = _build_weighted_caption_windows(chunks, voice_duration, cfg)
    if windows and len(windows) == len(chunks):
        return [(chunks[i], windows[i][0], windows[i][1]) for i in range(len(chunks))]

    # 3) Final fallback: evenly distributed over audio.
    if voice_duration <= 0:
        return []
    seg = max(0.3, voice_duration / max(1, len(chunks)))
    out: List[tuple[str, float, float]] = []
    for i, c in enumerate(chunks):
        start = i * seg
        if start >= voice_duration:
            break
        end = min(voice_duration, start + seg * 0.92)
        out.append((c, start, max(start + 0.12, end)))
    return out


def _format_beat_text(text: str, width: int = 32, max_lines: int = 3) -> str:
    wrapped = textwrap.wrap(text, width=width) or [text]
    if len(wrapped) > max_lines:
        head = wrapped[: max_lines - 1]
        tail = " ".join(wrapped[max_lines - 1 :])
        wrapped = head + [textwrap.shorten(tail, width=width, placeholder="...")]
    return "\n".join(wrapped)


def _video_size(path: Path) -> tuple[int, int]:
    out = run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ]
    ).strip()
    try:
        w, h = out.split("x", 1)
        return int(w), int(h)
    except Exception as e:
        raise RuntimeError(f"Could not parse video size '{out}': {e}") from e


def _clamp_byte(v: int) -> int:
    return max(0, min(255, int(v)))


def _parse_rgba_color(raw: str, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    s = (raw or "").strip()
    if not s:
        return default
    if s.startswith("#"):
        h = s[1:]
        try:
            if len(h) == 6:
                return (_clamp_byte(int(h[0:2], 16)), _clamp_byte(int(h[2:4], 16)), _clamp_byte(int(h[4:6], 16)), default[3])
            if len(h) == 8:
                return (
                    _clamp_byte(int(h[0:2], 16)),
                    _clamp_byte(int(h[2:4], 16)),
                    _clamp_byte(int(h[4:6], 16)),
                    _clamp_byte(int(h[6:8], 16)),
                )
        except Exception:
            return default
        return default
    parts = [p.strip() for p in s.split(",")]
    try:
        if len(parts) == 3:
            return (_clamp_byte(int(parts[0])), _clamp_byte(int(parts[1])), _clamp_byte(int(parts[2])), default[3])
        if len(parts) == 4:
            return (
                _clamp_byte(int(parts[0])),
                _clamp_byte(int(parts[1])),
                _clamp_byte(int(parts[2])),
                _clamp_byte(int(parts[3])),
            )
    except Exception:
        return default
    return default


def _render_text_overlay_png(
    text: str,
    width: int,
    height: int,
    font_file: str,
    font_size: int,
    out_png: Path,
    *,
    text_color: tuple[int, int, int, int] = (255, 255, 255, 255),
    text_stroke_color: tuple[int, int, int, int] = (0, 0, 0, 230),
    text_stroke_width: int = 2,
    card_color: tuple[int, int, int, int] = (0, 0, 0, 150),
    card_border_color: tuple[int, int, int, int] = (255, 255, 255, 0),
    card_border_width: int = 0,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        raise RuntimeError(f"Pillow not available for text overlay: {e}") from e

    # Use a tiny probe canvas only for text measurement. The final PNG is card-sized,
    # so ffmpeg overlay coordinates map directly to visible content.
    probe_img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe_img)

    max_card_w = max(160, int(width * 0.92))
    max_card_h = max(100, int(height * 0.42))
    min_font_size = max(16, int(font_size * 0.55))
    probe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    chosen: Dict[str, Any] = {}

    for cur_size in range(font_size, min_font_size - 1, -2):
        try:
            font = ImageFont.truetype(font_file, cur_size)
        except Exception:
            font = ImageFont.load_default()

        avg_char_w = max(1.0, float(draw.textlength(probe, font=font)) / len(probe))
        wrap_chars = max(14, min(56, int((max_card_w * 0.78) / avg_char_w)))
        txt = _format_beat_text(text, width=wrap_chars, max_lines=4)
        spacing = max(6, int(cur_size * 0.24))
        bbox = draw.multiline_textbbox((0, 0), txt, font=font, spacing=spacing, align="center",
                                       stroke_width=max(0, int(text_stroke_width)))
        text_w = max(1, int(math.ceil(bbox[2] - bbox[0])))
        text_h = max(1, int(math.ceil(bbox[3] - bbox[1])))
        pad_x = int(max(18, cur_size * 0.65)) + max(0, int(text_stroke_width))
        pad_y = int(max(14, cur_size * 0.45)) + max(0, int(text_stroke_width))
        box_w = text_w + pad_x * 2
        box_h = text_h + pad_y * 2

        chosen = {
            "font": font,
            "txt": txt,
            "spacing": spacing,
            "text_w": text_w,
            "text_h": text_h,
            "pad_x": pad_x,
            "pad_y": pad_y,
            "box_w": box_w,
            "box_h": box_h,
        }
        if box_w <= max_card_w and box_h <= max_card_h:
            break

    if not chosen:
        raise RuntimeError("Unable to build text overlay card")

    font = chosen["font"]
    txt = chosen["txt"]
    spacing = int(chosen["spacing"])
    text_w = int(chosen["text_w"])
    text_h = int(chosen["text_h"])
    pad_x = int(chosen["pad_x"])
    pad_y = int(chosen["pad_y"])
    box_w = min(width, int(chosen["box_w"]))
    box_h = min(height, int(chosen["box_h"]))

    # Draw only the visible card (no full-frame transparent canvas).
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(img)
    radius = int(min(box_w, box_h) * 0.14)
    card_draw.rounded_rectangle(
        (0, 0, box_w, box_h),
        radius=radius,
        fill=card_color,
        outline=card_border_color if card_border_width > 0 else None,
        width=max(0, int(card_border_width)),
    )
    text_x = max(pad_x, int((box_w - text_w) / 2))
    card_draw.multiline_text(
        (text_x, pad_y),
        txt,
        font=font,
        fill=text_color,
        spacing=spacing,
        align="center",
        stroke_width=max(0, int(text_stroke_width)),
        stroke_fill=text_stroke_color,
    )

    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)


def _normalize_text_beat_window(
    start: float,
    end: float,
    *,
    duration: float,
    next_start: Optional[float],
    min_sec: float,
    max_sec: float,
    target_sec: float,
    guard_sec: float,
) -> tuple[float, float]:
    start = max(0.0, min(duration, float(start)))
    end = max(start + 0.10, min(duration, float(end)))

    preferred_end = start + target_sec
    upper = min(duration, end, start + max_sec)
    if next_start is not None:
        upper = min(upper, max(start + 0.10, float(next_start) - guard_sec))

    lower = min(duration, start + min_sec)
    if next_start is not None:
        lower = min(lower, max(start + 0.10, float(next_start) - guard_sec))

    if upper < start + 0.10:
        return start, min(duration, start + 0.10)

    final_end = min(upper, preferred_end)
    if final_end < lower:
        final_end = upper

    final_end = max(start + 0.10, min(duration, final_end))
    return start, final_end


def add_text_beats_overlay(
    video_file: Path,
    beats: List[str],
    out_file: Path,
    cfg: Dict[str, str],
    *,
    font_file: Optional[str] = None,
    font_size: Optional[int] = None,
    y_frac: Optional[float] = None,
    display_sec: Optional[float] = None,
    cue_windows: Optional[List[Optional[tuple[float, float]]]] = None,
    text_color: Optional[str] = None,
    text_stroke_color: Optional[str] = None,
    text_stroke_width: Optional[int] = None,
    card_color: Optional[str] = None,
    card_border_color: Optional[str] = None,
    card_border_width: Optional[int] = None,
    tmp_prefix: str = "beats",
) -> None:
    if not beats:
        if out_file != video_file:
            run_cmd(["cp", str(video_file), str(out_file)])
        return

    duration = ffprobe_duration(video_file)
    if duration <= 0:
        raise RuntimeError("Cannot add text beats: invalid video duration")
    width, height = _video_size(video_file)

    if font_file is None:
        font_file = cfg.get("TEXT_BEATS_FONT_FILE", "/System/Library/Fonts/Helvetica.ttc").strip() or "/System/Library/Fonts/Helvetica.ttc"
    if font_size is None:
        font_size = int(cfg.get("TEXT_BEATS_FONT_SIZE", "56"))
    if y_frac is None:
        y_frac = float(cfg.get("TEXT_BEATS_Y_FRAC", "0.80"))
    y_frac = max(0.05, min(0.95, y_frac))
    if display_sec is None:
        display_sec = float(cfg.get("TEXT_BEATS_DISPLAY_SEC", "4.5"))
    requested_display = display_sec
    timed_min_sec = max(0.2, min(5.0, float(cfg.get("TEXT_BEATS_TIMED_MIN_SEC", "2.0"))))
    timed_max_sec = max(timed_min_sec, min(8.0, float(cfg.get("TEXT_BEATS_TIMED_MAX_SEC", "3.0"))))
    timed_target_sec = max(
        timed_min_sec,
        min(
            timed_max_sec,
            float(cfg.get("TEXT_BEATS_TIMED_TARGET_SEC", str((timed_min_sec + timed_max_sec) / 2.0))),
        ),
    )
    timed_guard_sec = max(0.02, min(0.5, float(cfg.get("TEXT_BEATS_TIMED_GAP_SEC", "0.08"))))
    if text_color is None:
        text_color = cfg.get("TEXT_BEATS_TEXT_COLOR", "#FFFFFF")
    if text_stroke_color is None:
        text_stroke_color = cfg.get("TEXT_BEATS_STROKE_COLOR", "#000000E6")
    if text_stroke_width is None:
        text_stroke_width = int(cfg.get("TEXT_BEATS_STROKE_WIDTH", "2"))
    if card_color is None:
        card_color = cfg.get("TEXT_BEATS_CARD_COLOR", "#00000096")
    if card_border_color is None:
        card_border_color = cfg.get("TEXT_BEATS_CARD_BORDER_COLOR", "#FFFFFF00")
    if card_border_width is None:
        card_border_width = int(cfg.get("TEXT_BEATS_CARD_BORDER_WIDTH", "0"))
    text_color_rgba = _parse_rgba_color(text_color, (255, 255, 255, 255))
    stroke_color_rgba = _parse_rgba_color(text_stroke_color, (0, 0, 0, 230))
    card_color_rgba = _parse_rgba_color(card_color, (0, 0, 0, 150))
    card_border_rgba = _parse_rgba_color(card_border_color, (255, 255, 255, 0))
    text_stroke_width = max(0, min(12, int(text_stroke_width)))
    card_border_width = max(0, min(12, int(card_border_width)))

    n = len(beats)
    display_len = max(0.8, min(requested_display, timed_max_sec))
    spread_step = max(display_len + 0.05, duration / max(1, n))

    effective_cue_windows = cue_windows
    if cue_windows:
        valid_cues = [cw for cw in cue_windows if cw and float(cw[1]) > float(cw[0])]
        if valid_cues:
            latest_end = max(float(cw[1]) for cw in valid_cues)
            # If all timed beats are packed very early, ignore timings and spread
            # across the full clip instead of stacking in the first seconds.
            if n >= 3 and latest_end < max(10.0, duration * 0.45):
                effective_cue_windows = None
                log(
                    "Text beat cue windows are front-loaded; "
                    f"redistributing across clip (latest_end={latest_end:.2f}s, duration={duration:.2f}s)"
                )

    overlay_specs: List[tuple[int, float, float, Path]] = []
    for i, beat in enumerate(beats):
        if effective_cue_windows and i < len(effective_cue_windows) and effective_cue_windows[i]:
            cw = effective_cue_windows[i]
            assert cw is not None
            start_raw = max(0.0, min(duration, float(cw[0])))
            end_raw = max(start_raw + 0.10, min(duration, float(cw[1])))
            next_start: Optional[float] = None
            if i + 1 < len(beats) and effective_cue_windows and i + 1 < len(effective_cue_windows) and effective_cue_windows[i + 1]:
                next_cw = effective_cue_windows[i + 1]
                assert next_cw is not None
                next_start = max(0.0, min(duration, float(next_cw[0])))
            start, end = _normalize_text_beat_window(
                start_raw,
                end_raw,
                duration=duration,
                next_start=next_start,
                min_sec=timed_min_sec,
                max_sec=timed_max_sec,
                target_sec=timed_target_sec,
                guard_sec=timed_guard_sec,
            )
        else:
            if n <= 1:
                start = max(0.0, min(duration - display_len, duration * 0.45))
            else:
                start = i * spread_step
            if start >= duration:
                break
            end = min(duration, start + display_len)
        png = Path(tempfile.mkdtemp(prefix=f"{tmp_prefix}_", dir=str(video_file.parent))) / f"beat_{i:02d}.png"
        _render_text_overlay_png(
            beat,
            width,
            height,
            font_file,
            font_size,
            png,
            text_color=text_color_rgba,
            text_stroke_color=stroke_color_rgba,
            text_stroke_width=text_stroke_width,
            card_color=card_color_rgba,
            card_border_color=card_border_rgba,
            card_border_width=card_border_width,
        )
        overlay_specs.append((i, start, end, png))

    if not overlay_specs:
        if out_file != video_file:
            run_cmd(["cp", str(video_file), str(out_file)])
        return

    cmd: List[str] = ["ffmpeg", "-y", "-i", str(video_file)]
    for _, _, _, png in overlay_specs:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(png)]

    filters: List[str] = []
    prev = "[0:v]"
    y_expr = f"(H*{y_frac})-(h/2)"
    for idx, start, end, _ in overlay_specs:
        in_label = prev
        ov_label = f"[{idx + 1}:v]"
        out_label = f"[v{idx + 1}]"
        filters.append(
            f"{in_label}{ov_label}overlay=(W-w)/2:{y_expr}:eof_action=pass:shortest=1:enable='between(t,{start:.2f},{end:.2f})'{out_label}"
        )
        prev = out_label

    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        prev,
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(out_file),
    ]
    run_cmd(cmd, timeout=1200)

    for _, _, _, png in overlay_specs:
        png.unlink(missing_ok=True)
        try:
            png.parent.rmdir()
        except OSError:
            pass


def synth_voiceover_macos_say(text: str, out_file: Path, cfg: Dict[str, str]) -> None:
    say_bin = cfg.get("VOICEOVER_SAY_BIN", "/usr/bin/say").strip() or "/usr/bin/say"
    voice = cfg.get("VOICEOVER_VOICE", "Samantha").strip() or "Samantha"
    rate = int(cfg.get("VOICEOVER_RATE_WPM", "178"))
    raw_file = out_file.with_suffix(".aiff")
    run_cmd([say_bin, "-v", voice, "-r", str(rate), "-o", str(raw_file), text], timeout=300)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw_file),
            "-ar",
            "44100",
            "-ac",
            "1",
            str(out_file),
        ],
        timeout=300,
    )
    raw_file.unlink(missing_ok=True)


def _resolve_edge_tts_bin(cfg: Dict[str, str]) -> str:
    explicit = cfg.get("VOICEOVER_EDGE_TTS_BIN", "edge-tts").strip() or "edge-tts"
    if explicit != "edge-tts":
        return explicit

    candidates = [
        "/opt/homebrew/bin/edge-tts",
        "/usr/local/bin/edge-tts",
        str(Path.home() / ".local" / "bin" / "edge-tts"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c

    for p in sorted(Path.home().glob("Library/Python/*/bin/edge-tts"), reverse=True):
        if p.exists():
            return str(p)
    return "edge-tts"


def _voice_mode_bool(cfg: Dict[str, str], key: str, default: str = "true") -> bool:
    return str(cfg.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}


def detect_voiceover_language(text: str, cfg: Dict[str, str], source_channel_id: str = "") -> str:
    mode = str(cfg.get("VOICEOVER_LANGUAGE_MODE", "auto")).strip().lower()
    if mode in {"de", "en"}:
        return mode

    if source_channel_id:
        per_channel = str(cfg.get(f"VOICEOVER_LANGUAGE_CHANNEL_{source_channel_id}", "")).strip().lower()
        if per_channel in {"de", "en"}:
            return per_channel

    t = f" {str(text or '').lower()} "
    de_words = [
        " der ",
        " die ",
        " das ",
        " und ",
        " nicht ",
        " ich ",
        " wir ",
        " ist ",
        " mit ",
        " fuer ",
        " eine ",
        " einen ",
        " sie ",
        " ihm ",
    ]
    en_words = [
        " the ",
        " and ",
        " not ",
        " i ",
        " we ",
        " is ",
        " with ",
        " a ",
        " an ",
        " you ",
        " they ",
        " he ",
        " she ",
    ]
    de_score = sum(1 for w in de_words if w in t)
    en_score = sum(1 for w in en_words if w in t)
    return "de" if de_score > en_score else "en"


def choose_voiceover_gender(text: str, cfg: Dict[str, str], story_seed: str = "") -> str:
    mode = str(cfg.get("VOICEOVER_GENDER_MODE", "auto")).strip().lower()
    if mode in {"female", "male"}:
        return mode

    t = f" {str(text or '').lower()} "
    female_hints = [
        " she ",
        " her ",
        " frau ",
        " mutter ",
        " tochter ",
        " sister ",
        " girl ",
        " woman ",
        " actress ",
    ]
    male_hints = [
        " he ",
        " his ",
        " mann ",
        " vater ",
        " sohn ",
        " brother ",
        " boy ",
        " man ",
        " actor ",
    ]
    female_score = sum(1 for w in female_hints if w in t)
    male_score = sum(1 for w in male_hints if w in t)
    if female_score > male_score:
        return "female"
    if male_score > female_score:
        return "male"

    if mode == "random":
        return random.choice(["female", "male"])

    seed = story_seed.strip() or str(text or "").strip() or str(time.time())
    h = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return "female" if (int(h[-1], 16) % 2 == 0) else "male"


def _normalize_edge_voice_choice(explicit_choice: str, cfg: Dict[str, str]) -> str:
    raw = str(explicit_choice or "").strip()
    if not raw:
        return ""
    line = raw.splitlines()[0].strip()
    m = re.search(r"(?i)\bvoice\s*:\s*([A-Za-z0-9._-]+)", line)
    if m:
        line = m.group(1).strip()
    else:
        m2 = re.search(r"([A-Za-z]{2}-[A-Za-z]{2}-[A-Za-z0-9._-]+(?:Neural)?)", line)
        if m2:
            line = m2.group(1).strip()

    choices_cfg = str(
        cfg.get(
            "VOICEOVER_EDGE_ALLOWED_CHOICES",
            "en-US-AvaMultilingualNeural,en-US-GuyNeural,de-DE-KatjaNeural,de-DE-ConradNeural",
        )
        or ""
    ).strip()
    allowed = [x.strip() for x in choices_cfg.split(",") if x.strip()]
    if not allowed:
        return line
    for v in allowed:
        if v.lower() == line.lower():
            return v
    return ""


def select_edge_tts_voice(
    text: str,
    cfg: Dict[str, str],
    story_seed: str = "",
    source_channel_id: str = "",
    explicit_choice: str = "",
) -> Dict[str, str]:
    explicit_voice = _normalize_edge_voice_choice(explicit_choice, cfg)
    if explicit_voice:
        return {"voice": explicit_voice, "lang": "explicit", "gender": "explicit"}

    if not _voice_mode_bool(cfg, "VOICEOVER_EDGE_VARIANTS_ENABLED", "true"):
        voice = (
            cfg.get("VOICEOVER_EDGE_VOICE", "").strip()
            or cfg.get("VOICEOVER_VOICE", "en-US-JennyNeural").strip()
            or "en-US-JennyNeural"
        )
        return {"voice": voice, "lang": "", "gender": ""}

    lang = detect_voiceover_language(text, cfg, source_channel_id=source_channel_id)
    gender = choose_voiceover_gender(text, cfg, story_seed=story_seed)

    defaults = {
        ("en", "female"): "en-US-JennyNeural",
        ("en", "male"): "en-US-GuyNeural",
        ("de", "female"): "de-DE-SeraphinaMultilingualNeural",
        ("de", "male"): "de-DE-FlorianMultilingualNeural",
    }
    exact_key = f"VOICEOVER_EDGE_VOICE_{lang.upper()}_{gender.upper()}"
    opposite = "male" if gender == "female" else "female"
    opposite_key = f"VOICEOVER_EDGE_VOICE_{lang.upper()}_{opposite.upper()}"
    fallback_key = f"VOICEOVER_EDGE_VOICE_{lang.upper()}"
    voice = (
        str(cfg.get(exact_key, "")).strip()
        or str(cfg.get(opposite_key, "")).strip()
        or str(cfg.get(fallback_key, "")).strip()
        or str(cfg.get("VOICEOVER_EDGE_VOICE", "")).strip()
        or defaults.get((lang, gender), "en-US-JennyNeural")
    )
    return {"voice": voice, "lang": lang, "gender": gender}


def synth_voiceover_edge_tts(text: str, out_file: Path, cfg: Dict[str, str], subtitle_out: Optional[Path] = None) -> None:
    edge_bin = _resolve_edge_tts_bin(cfg)
    voice = cfg.get("VOICEOVER_EDGE_VOICE", "").strip() or cfg.get("VOICEOVER_VOICE", "en-US-JennyNeural").strip() or "en-US-JennyNeural"
    rate = cfg.get("VOICEOVER_EDGE_RATE", "+0%").strip() or "+0%"
    pitch = cfg.get("VOICEOVER_EDGE_PITCH", "").strip()
    volume = cfg.get("VOICEOVER_EDGE_VOLUME", "").strip()

    media_file = out_file.with_suffix(".edge.mp3")
    cmd = [
        edge_bin,
        "--voice",
        voice,
        "--text",
        text,
        "--rate",
        rate,
        "--write-media",
        str(media_file),
    ]
    if pitch:
        cmd += ["--pitch", pitch]
    if volume:
        cmd += ["--volume", volume]
    if subtitle_out:
        cmd += ["--write-subtitles", str(subtitle_out)]

    edge_timeout_sec = max(30, min(1800, int(cfg.get("VOICEOVER_EDGE_TIMEOUT_SEC", "240"))))
    try:
        run_cmd(cmd, timeout=edge_timeout_sec)
    except FileNotFoundError as e:
        raise RuntimeError(
            "edge-tts binary not found. Install with: python3 -m pip install edge-tts"
        ) from e

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(media_file),
            "-ar",
            "44100",
            "-ac",
            "1",
            str(out_file),
        ],
        timeout=300,
    )
    media_file.unlink(missing_ok=True)


def generate_voiceover_audio(
    text: str,
    out_file: Path,
    cfg: Dict[str, str],
    subtitle_out: Optional[Path] = None,
) -> None:
    backend = cfg.get("VOICEOVER_BACKEND", "macos_say").strip().lower()
    if backend == "macos_say":
        synth_voiceover_macos_say(text, out_file, cfg)
        return
    if backend == "edge_tts":
        retries = max(0, min(5, int(cfg.get("VOICEOVER_EDGE_RETRIES", "1"))))
        retry_delay = max(0.2, min(10.0, float(cfg.get("VOICEOVER_EDGE_RETRY_DELAY_SEC", "2.0"))))
        fallback_enabled = cfg.get("VOICEOVER_EDGE_FALLBACK_ENABLED", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        fallback_backend = cfg.get("VOICEOVER_EDGE_FALLBACK_BACKEND", "macos_say").strip().lower() or "macos_say"
        fallback_on_timeout = cfg.get("VOICEOVER_EDGE_FALLBACK_ON_TIMEOUT", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        last_err: Optional[Exception] = None
        attempts = retries + 1
        for idx in range(attempts):
            try:
                synth_voiceover_edge_tts(text, out_file, cfg, subtitle_out=subtitle_out)
                return
            except Exception as e:
                last_err = e
                if (
                    fallback_enabled
                    and fallback_backend == "macos_say"
                    and fallback_on_timeout
                    and _is_timeout_like_error(e)
                ):
                    log(f"Edge TTS timeout detected; immediate fallback to macOS say: {e}")
                    if subtitle_out:
                        subtitle_out.unlink(missing_ok=True)
                    synth_voiceover_macos_say(text, out_file, cfg)
                    return
                if idx < attempts - 1:
                    log(
                        f"Edge TTS attempt {idx + 1}/{attempts} failed: {e}; "
                        f"retrying in {retry_delay:.1f}s"
                    )
                    time.sleep(retry_delay)

        if fallback_enabled and fallback_backend == "macos_say":
            log("Edge TTS failed after retries; falling back to macOS say")
            if subtitle_out:
                subtitle_out.unlink(missing_ok=True)
            synth_voiceover_macos_say(text, out_file, cfg)
            return

        if last_err:
            raise RuntimeError(f"Edge TTS failed after {attempts} attempts: {last_err}") from last_err
        raise RuntimeError("Edge TTS failed with unknown error")
    raise RuntimeError(f"Unsupported VOICEOVER_BACKEND={backend}")


def add_voiceover_to_clip(video_file: Path, voice_file: Path, out_file: Path, cfg: Dict[str, str]) -> None:
    gain_db = float(cfg.get("VOICEOVER_GAIN_DB", "2.5"))
    filters = (
        "[1:a]highpass=f=80,lowpass=f=8500,"
        "acompressor=threshold=-18dB:ratio=2.5:attack=5:release=60,"
        f"volume={gain_db}dB,apad[a]"
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(voice_file),
            "-filter_complex",
            filters,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out_file),
        ],
        timeout=900,
    )


def _bgm_profile_from_direction(direction_text: str) -> Dict[str, float]:
    txt = (direction_text or "").lower()
    profile = {
        "f1": 82.0,
        "f2": 164.0,
        "f3": 246.0,
        "lp": 2200.0,
        "hp": 45.0,
        "lfo_hz": 0.55,
        "v1": 0.22,
        "v2": 0.09,
        "v3": 0.05,
    }

    if any(k in txt for k in ["tension", "thriller", "dark", "suspense", "noir", "crime", "heist", "betray"]):
        profile.update({"f1": 55.0, "f2": 110.0, "f3": 165.0, "lp": 1300.0, "lfo_hz": 0.85, "v1": 0.24})
    elif any(k in txt for k in ["warm", "hope", "hopeful", "uplift", "inspiring", "heart", "family", "love"]):
        profile.update({"f1": 131.0, "f2": 262.0, "f3": 392.0, "lp": 3600.0, "lfo_hz": 0.40, "v1": 0.18})
    elif any(k in txt for k in ["industrial", "metal", "pulse", "percussion", "cinematic tension"]):
        profile.update({"f1": 65.0, "f2": 97.0, "f3": 130.0, "lp": 1800.0, "lfo_hz": 1.20, "v2": 0.12})

    return profile


def synth_background_music(
    direction_text: str,
    duration_sec: float,
    out_file: Path,
    cfg: Dict[str, str],
) -> None:
    duration = max(2.0, min(900.0, float(duration_sec)))
    fade_in = max(0.0, min(6.0, float(cfg.get("BGM_FADE_IN_SEC", "0.5"))))
    fade_out = max(0.0, min(8.0, float(cfg.get("BGM_FADE_OUT_SEC", "0.8"))))
    fade_out_start = max(0.0, duration - fade_out)
    synth_gain_db = float(cfg.get("BGM_SYNTH_GAIN_DB", "-8.0"))
    synth_gain = max(0.02, min(1.0, math.pow(10.0, synth_gain_db / 20.0)))

    p = _bgm_profile_from_direction(direction_text)

    chain: List[str] = [
        f"[0:a]lowpass=f={p['lp']:.1f},highpass=f={p['hp']:.1f},volume={p['v1']:.4f}[b1]",
        f"[1:a]lowpass=f={p['lp'] * 1.35:.1f},highpass=f={max(25.0, p['hp'] * 1.7):.1f},volume={p['v2']:.4f}[b2]",
        f"[2:a]lowpass=f={p['lp'] * 1.7:.1f},highpass=f={max(35.0, p['hp'] * 2.4):.1f},volume={p['v3']:.4f}[b3]",
    ]
    tail = [f"[b1][b2][b3]amix=inputs=3:normalize=0,volume={synth_gain:.5f}"]
    if fade_in > 0:
        tail.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0 and fade_out_start > 0:
        tail.append(f"afade=t=out:st={fade_out_start:.2f}:d={fade_out:.2f}")
    tail.append("alimiter=limit=0.92")
    chain.append(",".join(tail) + "[outa]")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            f"sine=frequency={p['f1']:.2f}:sample_rate=44100",
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            f"sine=frequency={p['f2']:.2f}:sample_rate=44100",
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            f"sine=frequency={p['f3']:.2f}:sample_rate=44100",
            "-filter_complex",
            ";".join(chain),
            "-map",
            "[outa]",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(out_file),
        ],
        timeout=max(180, int(duration * 6)),
    )


def classify_bgm_tag(direction_text: str, tags: List[str]) -> str:
    txt = (direction_text or "").lower()
    kw = {
        "chill": ["calm", "chill", "lofi", "ambient", "soft", "relax"],
        "tense": ["tension", "thriller", "dark", "suspense", "noir", "crime", "heist", "war", "militar", "militär", "spannung"],
        "uplift": ["epic", "hope", "uplift", "hero", "inspire", "anthem", "victory", "episch", "hoffnung"],
        "beat": ["beat", "club", "techno", "edm", "drum", "pulse"],
        "ambient": ["space", "drone", "atmos", "wash", "background"],
    }
    for tag in tags:
        for needle in kw.get(tag, []):
            if needle in txt:
                return tag
    # fallback heuristics
    if "dark" in txt or "war" in txt:
        return "tense" if "tense" in tags else tags[0] if tags else ""
    if "happy" in txt or "hope" in txt or "inspire" in txt:
        return "uplift" if "uplift" in tags else tags[0] if tags else ""
    if "club" in txt or "beat" in txt:
        return "beat" if "beat" in tags else tags[0] if tags else ""
    return tags[0] if tags else ""


def pick_bgm_sample(cfg: Dict[str, str], direction_text: str = "") -> Optional[Path]:
    sample_dir = Path(os.path.expanduser(cfg.get("BGM_SAMPLE_DIR", "~/Music/bgm_samples")))
    patterns = [p.strip() for p in cfg.get("BGM_SAMPLE_PATTERN", "*.wav,*.mp3").split(",") if p.strip()]
    tags = [t.strip().lower() for t in cfg.get("BGM_SAMPLE_TAGS", "chill,tense,uplift,beat,ambient").split(",") if t.strip()]
    tag = classify_bgm_tag(direction_text, tags)

    files: List[Path] = []
    for pat in patterns:
        files.extend(sample_dir.glob(pat))
    files = [p for p in files if p.is_file()]
    if not files:
        return None

    # If the story explicitly names a track filename/stem, use that exact sample.
    direction_l = (direction_text or "").lower()
    compact_direction = re.sub(r"[^a-z0-9]+", "", direction_l)
    if compact_direction:
        for p in files:
            name_c = re.sub(r"[^a-z0-9]+", "", p.name.lower())
            stem_c = re.sub(r"[^a-z0-9]+", "", p.stem.lower())
            if name_c and name_c in compact_direction:
                return p
            if stem_c and stem_c in compact_direction:
                return p

    if tag:
        tagged = [p for p in files if tag in p.stem.lower() or p.parent.name.lower() == tag]
        if tagged:
            files = tagged
    return random.choice(files) if files else None


def prep_bgm_sample(sample_file: Path, duration: float, out_file: Path, cfg: Dict[str, str]) -> None:
    """Trim/loop a real BGM sample to target duration and loudness-normalize."""
    duration = max(2.0, min(900.0, float(duration)))
    target_lufs = float(cfg.get("BGM_TARGET_LUFS", "-14.0"))
    fade_in = max(0.0, min(4.0, float(cfg.get("BGM_FADE_IN_SEC", "0.3"))))
    fade_out = max(0.0, min(8.0, float(cfg.get("BGM_FADE_OUT_SEC", "0.8"))))
    fade_out_start = max(0.0, duration - fade_out)

    filters = [
        f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11.0",
    ]
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0:
        filters.append(f"afade=t=out:st={fade_out_start:.2f}:d={fade_out:.2f}")
    filters.append("alimiter=limit=0.95")

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(sample_file),
            "-t",
            f"{duration:.3f}",
            "-af",
            ",".join(filters),
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(out_file),
        ],
        timeout=max(180, int(duration * 5)),
    )


def add_bgm_to_clip(video_file: Path, bgm_file: Path, out_file: Path, cfg: Dict[str, str]) -> None:
    bgm_db = float(cfg.get("BGM_LEVEL_DB", "-20.0"))
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(bgm_file),
            "-filter_complex",
            f"[1:a]volume={bgm_db:.2f}dB,apad[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out_file),
        ],
        timeout=900,
    )


def add_voiceover_and_bgm_to_clip(
    video_file: Path,
    voice_file: Path,
    bgm_file: Path,
    out_file: Path,
    cfg: Dict[str, str],
) -> None:
    voice_gain_db = float(cfg.get("VOICEOVER_GAIN_DB", "2.5"))
    bgm_db = float(cfg.get("BGM_LEVEL_DB", "-20.0"))
    # Keep voice intelligibility high even with energetic music.
    voice_mix_weight = max(0.10, min(3.00, float(cfg.get("VOICEOVER_MIX_WEIGHT", "1.35"))))
    bgm_mix_weight = max(0.05, min(2.00, float(cfg.get("BGM_MIX_WEIGHT", "0.45"))))
    duck_enabled = str(cfg.get("BGM_DUCKING_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
    duck_threshold = max(0.005, min(1.0, float(cfg.get("BGM_DUCK_THRESHOLD", "0.03"))))
    duck_ratio = max(1.5, min(20.0, float(cfg.get("BGM_DUCK_RATIO", "10.0"))))
    duck_attack = max(1.0, min(400.0, float(cfg.get("BGM_DUCK_ATTACK_MS", "18"))))
    duck_release = max(10.0, min(1500.0, float(cfg.get("BGM_DUCK_RELEASE_MS", "280"))))

    voice_base = (
        f"[1:a]highpass=f=80,lowpass=f=8500,"
        f"acompressor=threshold=-18dB:ratio=2.5:attack=5:release=60,"
        f"volume={voice_gain_db:.2f}dB,apad"
    )
    if duck_enabled:
        voice_chain = f"{voice_base},asplit=2[vo_main][vo_sc]"
    else:
        voice_chain = f"{voice_base}[vo_main]"
    bgm_chain = f"[2:a]highpass=f=30,lowpass=f=9500,volume={bgm_db:.2f}dB,apad[bgm]"
    if duck_enabled:
        mix_chain = (
            f"[bgm][vo_sc]sidechaincompress=threshold={duck_threshold:.4f}:ratio={duck_ratio:.2f}:"
            f"attack={duck_attack:.1f}:release={duck_release:.1f}[duck];"
            f"[duck][vo_main]amix=inputs=2:normalize=0:weights='{bgm_mix_weight:.2f} {voice_mix_weight:.2f}',"
            f"alimiter=limit=0.95[a]"
        )
    else:
        mix_chain = (
            f"[bgm][vo_main]amix=inputs=2:normalize=0:weights='{bgm_mix_weight:.2f} {voice_mix_weight:.2f}',"
            f"alimiter=limit=0.95[a]"
        )

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(voice_file),
            "-i",
            str(bgm_file),
            "-filter_complex",
            ";".join([voice_chain, bgm_chain, mix_chain]),
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out_file),
        ],
        timeout=1200,
    )


def synth_base_clip(prompt: str, seconds: int, out_file: Path) -> None:
    hue = 180
    p = prompt.lower()
    if any(k in p for k in ["horror", "fear", "dark", "night"]):
        hue = 300
    elif any(k in p for k in ["sad", "lonely", "lost", "old"]):
        hue = 210
    elif any(k in p for k in ["love", "hope", "family", "warm"]):
        hue = 35
    elif any(k in p for k in ["city", "neon", "future", "ai"]):
        hue = 250

    vf = (
        f"hue=h={hue}+18*t:s=1.2,"
        "eq=contrast=1.18:brightness=-0.04:saturation=1.08,"
        "gblur=sigma=6,"
        "vignette=PI/5,"
        "noise=alls=10:allf=t+u,"
        "fps=30,format=yuv420p"
    )
    run_cmd([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30",
        "-t", str(seconds),
        "-vf", vf,
        str(out_file),
    ], timeout=600)


def _replace_workflow_tokens(obj: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        out = obj
        for k, v in mapping.items():
            out = out.replace(k, v)
        return out
    if isinstance(obj, list):
        return [_replace_workflow_tokens(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _replace_workflow_tokens(v, mapping) for k, v in obj.items()}
    return obj


def _coerce_comfy_types(obj: Any) -> Any:
    numeric_keys_int = {"width", "height", "batch_size", "seed", "steps", "frames", "length"}
    numeric_keys_float = {"cfg", "denoise", "shift", "fps"}

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            vv = _coerce_comfy_types(v)
            if isinstance(vv, str):
                if k in numeric_keys_int:
                    try:
                        vv = int(float(vv))
                    except Exception:
                        pass
                elif k in numeric_keys_float:
                    try:
                        vv = float(vv)
                    except Exception:
                        pass
            out[k] = vv
        return out
    if isinstance(obj, list):
        return [_coerce_comfy_types(x) for x in obj]
    return obj


def _find_comfy_file_ref(history_obj: Dict[str, Any]) -> Optional[Dict[str, str]]:
    def _extract_item(item: Any) -> Optional[Dict[str, str]]:
        if isinstance(item, dict):
            if item.get("filename"):
                return {
                    "filename": str(item.get("filename", "")),
                    "subfolder": str(item.get("subfolder", "")),
                    "type": str(item.get("type", "output")),
                }
            for v in item.values():
                got = _extract_item(v)
                if got:
                    return got
        elif isinstance(item, list):
            for v in item:
                got = _extract_item(v)
                if got:
                    return got
        return None

    for node_out in history_obj.get("outputs", {}).values():
        for key in ("videos", "video", "gifs", "images"):
            arr = node_out.get(key)
            got = _extract_item(arr)
            if got:
                return got
        got = _extract_item(node_out)
        if got:
            return got
    return None


def _comfy_available_checkpoints(base_url: str) -> list[str]:
    with request.urlopen(f"{base_url}/object_info/CheckpointLoaderSimple", timeout=60) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    obj = json.loads(raw or "{}")
    names = (
        ((obj.get("CheckpointLoaderSimple") or {}).get("input") or {})
        .get("required", {})
        .get("ckpt_name", [])
    )
    if isinstance(names, list) and names and isinstance(names[0], list):
        names = names[0]
    if not isinstance(names, list):
        return []
    out = [str(x) for x in names if isinstance(x, str) and x.strip()]
    out.sort()
    return out


def _comfy_queue_contains_prompt(base_url: str, prompt_id: str) -> Dict[str, bool]:
    with request.urlopen(f"{base_url}/queue", timeout=60) as r:
        raw = r.read().decode("utf-8", errors="ignore")
    obj = json.loads(raw or "{}")

    def _contains(items: Any) -> bool:
        if not isinstance(items, list):
            return False
        for item in items:
            if isinstance(item, list) and len(item) > 1 and str(item[1]) == prompt_id:
                return True
        return False

    def _index(items: Any) -> int:
        if not isinstance(items, list):
            return -1
        for i, item in enumerate(items):
            if isinstance(item, list) and len(item) > 1 and str(item[1]) == prompt_id:
                return i
        return -1

    running = obj.get("queue_running")
    pending = obj.get("queue_pending")
    return {
        "running": _contains(running),
        "pending": _contains(pending),
        "running_count": len(running) if isinstance(running, list) else 0,
        "pending_count": len(pending) if isinstance(pending, list) else 0,
        "running_index": _index(running),
        "pending_index": _index(pending),
    }


def _comfy_interrupt_prompt(base_url: str, prompt_id: str) -> None:
    req = request.Request(
        f"{base_url}/interrupt",
        data=json.dumps({"prompt_id": prompt_id}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=30) as r:
        r.read()


def _comfy_delete_pending_prompt(base_url: str, prompt_id: str) -> None:
    req = request.Request(
        f"{base_url}/queue",
        data=json.dumps({"delete": [prompt_id]}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=30) as r:
        r.read()


def _comfy_wait_until_ready(base_url: str, timeout_sec: int = 90, poll_sec: float = 2.0) -> bool:
    timeout_sec = max(1, min(600, int(timeout_sec)))
    poll_sec = max(0.2, min(10.0, float(poll_sec)))
    deadline = time.time() + timeout_sec
    last_err = ""
    while time.time() < deadline:
        try:
            with request.urlopen(f"{base_url}/queue", timeout=10) as r:
                raw = r.read().decode("utf-8", errors="ignore")
            obj = json.loads(raw or "{}")
            if isinstance(obj, dict):
                return True
        except Exception as e:
            last_err = str(e)
        time.sleep(poll_sec)
    if last_err:
        log(f"ComfyUI ready-wait timed out after {timeout_sec}s: {last_err}")
    else:
        log(f"ComfyUI ready-wait timed out after {timeout_sec}s")
    return False


def _comfy_history_message_summary(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(item, dict):
        return out
    status = item.get("status")
    if not isinstance(status, dict):
        return out
    out["status_str"] = str(status.get("status_str", ""))
    out["completed"] = bool(status.get("completed", False))
    msgs = status.get("messages")
    if not isinstance(msgs, list):
        out["message_count"] = 0
        return out
    out["message_count"] = len(msgs)
    if not msgs:
        return out
    last = msgs[-1]
    if isinstance(last, list) and last:
        out["last_message_type"] = str(last[0])
        if len(last) > 1 and isinstance(last[1], dict):
            payload = last[1]
            for key in ("node_id", "node_type", "prompt_id", "timestamp"):
                if key in payload:
                    out[f"last_{key}"] = payload.get(key)
            if "executed" in payload and isinstance(payload["executed"], list):
                out["last_executed_count"] = len(payload["executed"])
    return out


def _log_comfy_timeout_diagnostics(
    *,
    cfg: Dict[str, str],
    base_url: str,
    prompt_id: str,
    elapsed: float,
    item: Optional[Dict[str, Any]],
    queue_state: Dict[str, Any],
    stage: str,
) -> None:
    diag_enabled = str(cfg.get("COMFYUI_TIMEOUT_DIAG_ENABLED", "true")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    if not diag_enabled:
        return
    summary: Dict[str, Any] = {
        "stage": stage,
        "prompt_id": prompt_id,
        "elapsed_sec": int(elapsed),
        "base_url": base_url,
        "queue": queue_state,
        "history": _comfy_history_message_summary(item),
    }
    try:
        log(f"ComfyUI timeout diagnostics: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    except Exception:
        log(f"ComfyUI timeout diagnostics: {summary}")

    tail_lines = max(0, min(200, int(cfg.get("COMFYUI_TIMEOUT_LOG_TAIL_LINES", "30"))))
    if tail_lines <= 0:
        return
    tail_chars = max(200, min(20000, int(cfg.get("COMFYUI_TIMEOUT_LOG_TAIL_CHARS", "2200"))))
    default_err = ROOT / "logs" / "comfyui.err.log"
    err_file = Path(os.path.expanduser(str(cfg.get("COMFYUI_ERR_LOG_FILE", str(default_err))).strip()))
    tail = _tail_text_file(err_file, max_lines=tail_lines, max_chars=tail_chars)
    if tail:
        log(f"ComfyUI stderr tail ({err_file}, last {tail_lines} lines):\n{tail}")
    else:
        log(f"ComfyUI stderr tail unavailable: {err_file}")


def _comfy_latest_sec_per_it(cfg: Dict[str, str]) -> Optional[float]:
    default_err = ROOT / "logs" / "comfyui.err.log"
    err_file = Path(os.path.expanduser(str(cfg.get("COMFYUI_ERR_LOG_FILE", str(default_err))).strip()))
    tail = _tail_text_file(err_file, max_lines=25, max_chars=2400)
    if not tail:
        return None
    matches = re.findall(r"([0-9]+(?:\.[0-9]+)?)s/it", tail)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except Exception:
        return None


def comfyui_base_clip(prompt: str, seconds: int, out_file: Path, cfg: Dict[str, str]) -> None:
    base_url = cfg.get("COMFYUI_API_URL", "http://127.0.0.1:8188").rstrip("/")
    wf_file = cfg.get("COMFYUI_WORKFLOW_FILE", "").strip()
    if not wf_file:
        raise RuntimeError("COMFYUI_WORKFLOW_FILE missing for VIDEO_BACKEND=comfyui")

    wf_path = Path(wf_file).expanduser()
    if not wf_path.exists():
        raise RuntimeError(f"ComfyUI workflow file not found: {wf_path}")

    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    workflow_raw = json.dumps(workflow, ensure_ascii=False)
    uses_ckpt_name = "__CKPT_NAME__" in workflow_raw

    ckpt_name = cfg.get("COMFYUI_CHECKPOINT", "").strip()
    if uses_ckpt_name:
        available_ckpts = _comfy_available_checkpoints(base_url)
        if available_ckpts:
            if not ckpt_name:
                ckpt_name = available_ckpts[0]
                log(f"COMFYUI_CHECKPOINT empty, using '{ckpt_name}'")
            elif ckpt_name not in available_ckpts:
                chosen = available_ckpts[0]
                log(f"Configured COMFYUI_CHECKPOINT '{ckpt_name}' not found; using '{chosen}'")
                ckpt_name = chosen
        else:
            raise RuntimeError("No ComfyUI checkpoints found in ComfyUI/models/checkpoints")

    fps = int(cfg.get("COMFYUI_FPS", "24"))
    raw_frames = seconds * fps
    max_frames = int(cfg.get("COMFYUI_MAX_FRAMES", "81"))
    frames = max(8, min(raw_frames, max_frames))

    workflow = _replace_workflow_tokens(
        workflow,
        {
            "__PROMPT__": prompt,
            "__NEGATIVE_PROMPT__": resolve_comfy_negative_prompt(cfg),
            "__SECONDS__": str(seconds),
            "__FPS__": str(fps),
            "__FRAMES__": str(frames),
            "__WIDTH__": str(int(cfg.get("COMFYUI_WIDTH", "720"))),
            "__HEIGHT__": str(int(cfg.get("COMFYUI_HEIGHT", "1280"))),
            "__SEED__": str(int(datetime.now().timestamp()) % 2147483647),
            "__STEPS__": str(int(cfg.get("COMFYUI_STEPS", "28"))),
            "__CFG__": str(float(cfg.get("COMFYUI_CFG", "6.5"))),
            "__SAMPLER_NAME__": cfg.get("COMFYUI_SAMPLER_NAME", "euler"),
            "__SCHEDULER__": cfg.get("COMFYUI_SCHEDULER", "normal"),
            "__CKPT_NAME__": ckpt_name,
            "__DIFFUSION_MODEL__": cfg.get("COMFYUI_DIFFUSION_MODEL", "").strip(),
            "__TEXT_ENCODER__": cfg.get("COMFYUI_TEXT_ENCODER", "").strip(),
            "__VAE_NAME__": cfg.get("COMFYUI_VAE_NAME", "").strip(),
            "__MODEL_SHIFT__": str(float(cfg.get("COMFYUI_MODEL_SHIFT", "8.0"))),
        },
    )
    workflow = _coerce_comfy_types(workflow)

    start = http_json(f"{base_url}/prompt", {"prompt": workflow}, timeout=180)
    prompt_id = start.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {start}")

    timeout_s = max(0, int(cfg.get("COMFYUI_TIMEOUT_SEC", "600")))
    # Guardrail: a single Comfy prompt running longer than ~45 minutes is
    # treated as unhealthy and is interrupted.
    hard_timeout_s = max(0, int(cfg.get("COMFYUI_HARD_TIMEOUT_SEC", "2700")))
    if hard_timeout_s and timeout_s and hard_timeout_s < timeout_s:
        hard_timeout_s = timeout_s
        log(f"COMFYUI_HARD_TIMEOUT_SEC < COMFYUI_TIMEOUT_SEC; using hard timeout {hard_timeout_s}s")

    timeout_interrupt_on_hard = cfg.get("COMFYUI_TIMEOUT_INTERRUPT_ON_HARD", "true").lower() in {
        "1", "true", "yes", "on"
    }
    timeout_clear_pending_on_hard = cfg.get("COMFYUI_TIMEOUT_CLEAR_PENDING_ON_HARD", "true").lower() in {
        "1", "true", "yes", "on"
    }
    step_watchdog_enabled = cfg.get("COMFYUI_STEP_WATCHDOG_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    step_watchdog_max_sec = max(60.0, min(1800.0, float(cfg.get("COMFYUI_STEP_WATCHDOG_MAX_SEC_PER_IT", "360"))))
    step_watchdog_hits_needed = max(1, min(10, int(cfg.get("COMFYUI_STEP_WATCHDOG_HITS", "2"))))
    step_watchdog_min_elapsed = max(0, min(3600, int(cfg.get("COMFYUI_STEP_WATCHDOG_MIN_ELAPSED_SEC", "240"))))
    stall_watchdog_enabled = cfg.get("COMFYUI_STALL_WATCHDOG_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    stall_watchdog_idle_sec = max(60, min(1800, int(cfg.get("COMFYUI_STALL_WATCHDOG_IDLE_SEC", "240"))))
    stall_watchdog_min_elapsed = max(0, min(7200, int(cfg.get("COMFYUI_STALL_WATCHDOG_MIN_ELAPSED_SEC", "600"))))
    stall_watchdog_probe_sec = max(10.0, min(120.0, float(cfg.get("COMFYUI_STALL_WATCHDOG_PROBE_SEC", "30"))))
    default_err = ROOT / "logs" / "comfyui.err.log"
    stall_err_file = Path(os.path.expanduser(str(cfg.get("COMFYUI_ERR_LOG_FILE", str(default_err))).strip()))

    poll_s = float(cfg.get("COMFYUI_POLL_SEC", "2"))
    t0 = datetime.now().timestamp()
    soft_timeout_logged = False
    file_ref: Optional[Dict[str, str]] = None
    slow_step_hits = 0
    last_slow_sec_per_it: Optional[float] = None
    next_step_watchdog_probe_ts = t0
    next_stall_watchdog_probe_ts = t0
    last_stall_progress_ts = t0
    last_stall_probe_key = ""

    while True:
        with request.urlopen(f"{base_url}/history/{prompt_id}", timeout=60) as r:
            hist_raw = r.read().decode("utf-8", errors="ignore")
        hist = json.loads(hist_raw or "{}")
        item = hist.get(prompt_id) if isinstance(hist, dict) else None
        if item and item.get("status", {}).get("completed"):
            file_ref = _find_comfy_file_ref(item)
            if not file_ref:
                raise RuntimeError("ComfyUI completed but output file reference is missing")
            break
        if item and item.get("status", {}).get("status_str") == "error":
            msgs = item.get("status", {}).get("messages", [])
            interrupted = False
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, list) and m:
                        if str(m[0]) == "execution_interrupted":
                            interrupted = True
                            break
            if interrupted:
                raise ComfyInterruptedError(f"ComfyUI execution_interrupted: {item}")
            raise RuntimeError(f"ComfyUI job failed: {item}")

        elapsed = datetime.now().timestamp() - t0
        if (
            step_watchdog_enabled
            and elapsed >= step_watchdog_min_elapsed
            and datetime.now().timestamp() >= next_step_watchdog_probe_ts
        ):
            next_step_watchdog_probe_ts = datetime.now().timestamp() + max(15.0, min(90.0, poll_s * 5.0))
            sec_per_it = _comfy_latest_sec_per_it(cfg)
            if sec_per_it is not None:
                if sec_per_it >= step_watchdog_max_sec:
                    # Do not count repeated probes of the same stale sample as new hits.
                    if last_slow_sec_per_it is None or abs(last_slow_sec_per_it - sec_per_it) > 0.05:
                        slow_step_hits += 1
                        last_slow_sec_per_it = sec_per_it
                        log(
                            f"ComfyUI step watchdog hit {slow_step_hits}/{step_watchdog_hits_needed} "
                            f"for prompt_id={prompt_id}: observed {sec_per_it:.2f}s/it (threshold {step_watchdog_max_sec:.2f}s/it)"
                        )
                else:
                    slow_step_hits = 0
                    last_slow_sec_per_it = None
            if slow_step_hits >= step_watchdog_hits_needed:
                queue_state = _comfy_queue_contains_prompt(base_url, prompt_id)
                _log_comfy_timeout_diagnostics(
                    cfg=cfg,
                    base_url=base_url,
                    prompt_id=prompt_id,
                    elapsed=elapsed,
                    item=item,
                    queue_state=queue_state,
                    stage="step_watchdog",
                )
                try:
                    _comfy_interrupt_prompt(base_url, prompt_id)
                except Exception as e:
                    log(f"ComfyUI step-watchdog interrupt failed for prompt_id={prompt_id}: {e}")
                try:
                    _comfy_delete_pending_prompt(base_url, prompt_id)
                except Exception as e:
                    log(f"ComfyUI step-watchdog queue delete failed for prompt_id={prompt_id}: {e}")
                restart_comfyui_service(reason=f"step watchdog prompt_id={prompt_id}")
                restart_ready_wait = max(0, min(600, int(cfg.get("COMFYUI_RESTART_READY_WAIT_SEC", "90"))))
                if restart_ready_wait > 0:
                    if _comfy_wait_until_ready(base_url, timeout_sec=restart_ready_wait, poll_sec=max(0.5, poll_s)):
                        log(
                            f"ComfyUI became ready after step-watchdog restart for prompt_id={prompt_id} "
                            f"(waited <= {restart_ready_wait}s)"
                        )
                    else:
                        log(
                            f"ComfyUI not ready within {restart_ready_wait}s after step-watchdog restart "
                            f"for prompt_id={prompt_id}"
                        )
                raise RuntimeError(
                    f"ComfyUI step watchdog aborted prompt_id={prompt_id} after {int(elapsed)}s "
                    f"(observed >= {step_watchdog_max_sec:.2f}s/it)"
                )

        if (
            stall_watchdog_enabled
            and elapsed >= stall_watchdog_min_elapsed
            and datetime.now().timestamp() >= next_stall_watchdog_probe_ts
        ):
            next_stall_watchdog_probe_ts = datetime.now().timestamp() + stall_watchdog_probe_sec
            queue_state = _comfy_queue_contains_prompt(base_url, prompt_id)
            prompt_active = queue_state.get("running", False) or queue_state.get("pending", False)
            err_tail = _tail_text_file(stall_err_file, max_lines=8, max_chars=1200)
            probe_key = (
                f"r={int(queue_state.get('running', False))}"
                f"|p={int(queue_state.get('pending', False))}"
                f"|ri={int(queue_state.get('running_index', -1))}"
                f"|pi={int(queue_state.get('pending_index', -1))}"
                f"|tail={err_tail[-400:]}"
            )
            if probe_key != last_stall_probe_key:
                last_stall_probe_key = probe_key
                last_stall_progress_ts = datetime.now().timestamp()
            idle_for = datetime.now().timestamp() - last_stall_progress_ts
            sec_per_it_for_stall = _comfy_latest_sec_per_it(cfg)
            dynamic_idle_limit = stall_watchdog_idle_sec
            if sec_per_it_for_stall is not None and sec_per_it_for_stall > 0:
                dynamic_idle_limit = max(
                    stall_watchdog_idle_sec,
                    max(60, min(1800, int(sec_per_it_for_stall * 2.0 + 90.0))),
                )
            if prompt_active and idle_for >= dynamic_idle_limit:
                _log_comfy_timeout_diagnostics(
                    cfg=cfg,
                    base_url=base_url,
                    prompt_id=prompt_id,
                    elapsed=elapsed,
                    item=item,
                    queue_state=queue_state,
                    stage="stall_watchdog",
                )
                try:
                    _comfy_interrupt_prompt(base_url, prompt_id)
                except Exception as e:
                    log(f"ComfyUI stall-watchdog interrupt failed for prompt_id={prompt_id}: {e}")
                try:
                    _comfy_delete_pending_prompt(base_url, prompt_id)
                except Exception as e:
                    log(f"ComfyUI stall-watchdog queue delete failed for prompt_id={prompt_id}: {e}")
                restart_comfyui_service(reason=f"stall watchdog prompt_id={prompt_id}")
                restart_ready_wait = max(0, min(600, int(cfg.get("COMFYUI_RESTART_READY_WAIT_SEC", "90"))))
                if restart_ready_wait > 0:
                    if _comfy_wait_until_ready(base_url, timeout_sec=restart_ready_wait, poll_sec=max(0.5, poll_s)):
                        log(
                            f"ComfyUI became ready after stall-watchdog restart for prompt_id={prompt_id} "
                            f"(waited <= {restart_ready_wait}s)"
                        )
                    else:
                        log(
                            f"ComfyUI not ready within {restart_ready_wait}s after stall-watchdog restart "
                            f"for prompt_id={prompt_id}"
                        )
                raise RuntimeError(
                    f"ComfyUI stall watchdog aborted prompt_id={prompt_id} after {int(elapsed)}s "
                    f"(no progress for {int(idle_for)}s; limit={int(dynamic_idle_limit)}s)"
                )

        if timeout_s > 0 and elapsed >= timeout_s:
            queue_state = _comfy_queue_contains_prompt(base_url, prompt_id)
            prompt_active = queue_state.get("running", False) or queue_state.get("pending", False)
            if prompt_active:
                if hard_timeout_s > 0 and elapsed >= hard_timeout_s:
                    _log_comfy_timeout_diagnostics(
                        cfg=cfg,
                        base_url=base_url,
                        prompt_id=prompt_id,
                        elapsed=elapsed,
                        item=item,
                        queue_state=queue_state,
                        stage="hard_timeout",
                    )
                    if timeout_interrupt_on_hard:
                        try:
                            _comfy_interrupt_prompt(base_url, prompt_id)
                        except Exception as e:
                            log(f"ComfyUI hard-timeout interrupt failed for prompt_id={prompt_id}: {e}")
                    if timeout_clear_pending_on_hard:
                        try:
                            _comfy_delete_pending_prompt(base_url, prompt_id)
                        except Exception as e:
                            log(f"ComfyUI hard-timeout queue delete failed for prompt_id={prompt_id}: {e}")
                    restart_comfyui_service(reason=f"hard timeout prompt_id={prompt_id}")
                    restart_ready_wait = max(0, min(600, int(cfg.get("COMFYUI_RESTART_READY_WAIT_SEC", "90"))))
                    if restart_ready_wait > 0:
                        if _comfy_wait_until_ready(base_url, timeout_sec=restart_ready_wait, poll_sec=max(0.5, poll_s)):
                            log(
                                f"ComfyUI became ready after restart for prompt_id={prompt_id} "
                                f"(waited <= {restart_ready_wait}s)"
                            )
                        else:
                            log(
                                f"ComfyUI not ready within {restart_ready_wait}s after restart "
                                f"for prompt_id={prompt_id}"
                            )
                    raise RuntimeError(
                        f"ComfyUI hard timeout after {int(elapsed)}s for prompt_id={prompt_id}"
                    )
                if not soft_timeout_logged:
                    _log_comfy_timeout_diagnostics(
                        cfg=cfg,
                        base_url=base_url,
                        prompt_id=prompt_id,
                        elapsed=elapsed,
                        item=item,
                        queue_state=queue_state,
                        stage="soft_timeout",
                    )
                    if hard_timeout_s > 0:
                        log(
                            f"ComfyUI soft timeout after {timeout_s}s for prompt_id={prompt_id}, "
                            f"but prompt is still active; waiting until hard timeout {hard_timeout_s}s"
                        )
                    else:
                        log(
                            f"ComfyUI soft timeout after {timeout_s}s for prompt_id={prompt_id}, "
                            "but prompt is still active; continuing to wait"
                        )
                    soft_timeout_logged = True
            else:
                raise RuntimeError(
                    f"ComfyUI timeout after {int(elapsed)}s for prompt_id={prompt_id} "
                    "(prompt is no longer active and no output is available)"
                )
        time.sleep(poll_s)

    if not file_ref:
        raise RuntimeError("ComfyUI finished without output file reference")

    q = parse.urlencode(
        {
            "filename": file_ref["filename"],
            "subfolder": file_ref["subfolder"],
            "type": file_ref["type"] or "output",
        }
    )
    view_url = f"{base_url}/view?{q}"
    downloaded = out_file.with_suffix(Path(file_ref["filename"]).suffix or ".bin")
    with request.urlopen(view_url, timeout=300) as r:
        downloaded.write_bytes(r.read())

    if downloaded.suffix.lower() in (".mp4", ".mov", ".webm"):
        if downloaded != out_file:
            out_file.write_bytes(downloaded.read_bytes())
            downloaded.unlink(missing_ok=True)
        return

    if downloaded.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        # Animate a still image so base clip has a real duration.
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-t",
                str(seconds),
                "-i",
                str(downloaded),
                "-vf",
                (
                    "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,"
                    "zoompan=z='min(zoom+0.0007,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920,"
                    "fps=30,format=yuv420p"
                ),
                "-an",
                str(out_file),
            ],
            timeout=300,
        )
        downloaded.unlink(missing_ok=True)
        return

    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(downloaded),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out_file),
        ],
        timeout=300,
    )
    downloaded.unlink(missing_ok=True)


def runpod_base_clip(prompt: str, seconds: int, out_file: Path, cfg: Dict[str, str]) -> None:
    """Generate a base clip via RunPod Serverless ComfyUI endpoint."""
    api_key = cfg.get("RUNPOD_API_KEY", "").strip()
    endpoint_id = cfg.get("RUNPOD_ENDPOINT_ID", "").strip()
    if not api_key or not endpoint_id:
        raise RuntimeError("RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID required for VIDEO_BACKEND=runpod")

    wf_file = cfg.get("RUNPOD_WORKFLOW_FILE", "").strip() or cfg.get("COMFYUI_WORKFLOW_FILE", "").strip()
    if not wf_file:
        raise RuntimeError("RUNPOD_WORKFLOW_FILE (or COMFYUI_WORKFLOW_FILE) missing for VIDEO_BACKEND=runpod")
    wf_path = Path(wf_file).expanduser()
    if not wf_path.exists():
        raise RuntimeError(f"RunPod workflow file not found: {wf_path}")

    workflow = json.loads(wf_path.read_text(encoding="utf-8"))

    fps = int(cfg.get("RUNPOD_FPS", cfg.get("COMFYUI_FPS", "24")))
    raw_frames = seconds * fps
    max_frames = int(cfg.get("RUNPOD_MAX_FRAMES", cfg.get("COMFYUI_MAX_FRAMES", "81")))
    frames = max(8, min(raw_frames, max_frames))

    workflow = _replace_workflow_tokens(
        workflow,
        {
            "__PROMPT__": prompt,
            "__NEGATIVE_PROMPT__": resolve_comfy_negative_prompt(cfg),
            "__SECONDS__": str(seconds),
            "__FPS__": str(fps),
            "__FRAMES__": str(frames),
            "__WIDTH__": str(int(cfg.get("RUNPOD_WIDTH", cfg.get("COMFYUI_WIDTH", "720")))),
            "__HEIGHT__": str(int(cfg.get("RUNPOD_HEIGHT", cfg.get("COMFYUI_HEIGHT", "1280")))),
            "__SEED__": str(int(datetime.now().timestamp()) % 2147483647),
            "__STEPS__": str(int(cfg.get("RUNPOD_STEPS", cfg.get("COMFYUI_STEPS", "25")))),
            "__CFG__": str(float(cfg.get("RUNPOD_CFG", cfg.get("COMFYUI_CFG", "6.5")))),
            "__SAMPLER_NAME__": cfg.get("RUNPOD_SAMPLER_NAME", cfg.get("COMFYUI_SAMPLER_NAME", "euler")),
            "__SCHEDULER__": cfg.get("RUNPOD_SCHEDULER", cfg.get("COMFYUI_SCHEDULER", "normal")),
            "__DIFFUSION_MODEL__": cfg.get("RUNPOD_DIFFUSION_MODEL", "wan2.2_t2v_14B_fp8.safetensors"),
            "__TEXT_ENCODER__": cfg.get("RUNPOD_TEXT_ENCODER", "umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
            "__VAE_NAME__": cfg.get("RUNPOD_VAE_NAME", "wan_2.2_vae.safetensors"),
            "__MODEL_SHIFT__": str(float(cfg.get("RUNPOD_MODEL_SHIFT", cfg.get("COMFYUI_MODEL_SHIFT", "8.0")))),
            "__CKPT_NAME__": cfg.get("RUNPOD_CHECKPOINT", cfg.get("COMFYUI_CHECKPOINT", "")),
        },
    )
    workflow = _coerce_comfy_types(workflow)

    run_url = f"https://api.runpod.ai/v2/{endpoint_id}/run"
    status_base = f"https://api.runpod.ai/v2/{endpoint_id}/status"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = json.dumps({"input": {"workflow": workflow}}).encode("utf-8")

    req = request.Request(run_url, data=payload, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with request.urlopen(req, timeout=60) as r:
        run_resp = json.loads(r.read().decode("utf-8", errors="ignore"))

    job_id = run_resp.get("id")
    if not job_id:
        raise RuntimeError(f"RunPod did not return job id: {run_resp}")
    log(f"RunPod job submitted: {job_id}")

    timeout_s = max(0, int(cfg.get("RUNPOD_TIMEOUT_SEC", "600")))
    poll_s = float(cfg.get("RUNPOD_POLL_SEC", "5"))
    t0 = datetime.now().timestamp()

    while True:
        elapsed = datetime.now().timestamp() - t0
        if timeout_s > 0 and elapsed >= timeout_s:
            # Try to cancel the job
            try:
                cancel_url = f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}"
                cancel_req = request.Request(cancel_url, method="POST")
                for k, v in headers.items():
                    cancel_req.add_header(k, v)
                request.urlopen(cancel_req, timeout=30)
            except Exception:
                pass
            raise RuntimeError(f"RunPod timeout after {int(elapsed)}s for job {job_id}")

        status_url = f"{status_base}/{job_id}"
        status_req = request.Request(status_url, method="GET")
        for k, v in headers.items():
            status_req.add_header(k, v)
        with request.urlopen(status_req, timeout=60) as r:
            status_resp = json.loads(r.read().decode("utf-8", errors="ignore"))

        status = status_resp.get("status", "")
        if status == "COMPLETED":
            break
        if status == "FAILED":
            error_msg = status_resp.get("error", "unknown error")
            raise RuntimeError(f"RunPod job {job_id} failed: {error_msg}")
        if status == "CANCELLED":
            raise RuntimeError(f"RunPod job {job_id} was cancelled")

        if int(elapsed) % 30 == 0 and int(elapsed) > 0:
            log(f"RunPod job {job_id}: status={status}, elapsed={int(elapsed)}s")
        time.sleep(poll_s)

    output = status_resp.get("output", {})

    # The RunPod ComfyUI worker returns images as base64 or as a URL
    # Try URL first (S3 or direct), then base64
    video_url = None
    if isinstance(output, dict):
        # worker-comfyui returns {"images": [{"image": "base64...", "type": "..."}]}
        # or {"images": [{"url": "https://..."}]}
        images = output.get("images") or output.get("output") or []
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                video_url = first.get("url") or first.get("image_url")
                if not video_url and first.get("image"):
                    # base64 encoded
                    import base64
                    video_data = base64.b64decode(first["image"])
                    tmp_path = out_file.with_suffix(".webm")
                    tmp_path.write_bytes(video_data)
                    if tmp_path.suffix.lower() != out_file.suffix.lower():
                        run_cmd(["ffmpeg", "-y", "-i", str(tmp_path), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_file)], timeout=300)
                        tmp_path.unlink(missing_ok=True)
                    else:
                        tmp_path.rename(out_file)
                    log(f"RunPod job {job_id} completed (base64), saved to {out_file}")
                    return
            elif isinstance(first, str) and first.startswith("http"):
                video_url = first
    elif isinstance(output, str) and output.startswith("http"):
        video_url = output

    if not video_url:
        raise RuntimeError(f"RunPod job {job_id} completed but no video URL/data found in output: {output}")

    # Download the video
    dl_req = request.Request(video_url, method="GET")
    with request.urlopen(dl_req, timeout=300) as r:
        video_data = r.read()

    # Determine extension from URL or default to webm
    from pathlib import PurePosixPath
    url_path = PurePosixPath(parse.urlparse(video_url).path)
    dl_ext = url_path.suffix.lower() if url_path.suffix else ".webm"
    tmp_path = out_file.with_suffix(dl_ext)
    tmp_path.write_bytes(video_data)

    if dl_ext in (".mp4", ".mov"):
        if tmp_path != out_file:
            out_file.write_bytes(tmp_path.read_bytes())
            tmp_path.unlink(missing_ok=True)
    else:
        run_cmd(["ffmpeg", "-y", "-i", str(tmp_path), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_file)], timeout=300)
        tmp_path.unlink(missing_ok=True)

    log(f"RunPod job {job_id} completed, saved to {out_file}")


def generate_base_clip(prompt: str, seconds: int, out_file: Path, cfg: Dict[str, str]) -> None:
    backend = cfg.get("VIDEO_BACKEND", "local_synth").strip().lower()
    prompt2 = maybe_enhance_prompt_with_ollama(prompt, cfg)
    if backend == "local_synth":
        synth_base_clip(prompt2, seconds, out_file)
        return
    if backend == "runpod":
        runpod_base_clip(prompt2, seconds, out_file, cfg)
        return
    if backend == "comfyui":
        base_url = cfg.get("COMFYUI_API_URL", "http://127.0.0.1:8188").rstrip("/")
        ready_wait = max(0, min(600, int(cfg.get("COMFYUI_RESTART_READY_WAIT_SEC", "90"))))
        if ready_wait > 0 and not _comfy_wait_until_ready(base_url, timeout_sec=ready_wait, poll_sec=1.0):
            raise RuntimeError(f"ComfyUI not ready after {ready_wait}s at {base_url}")
        fallback = cfg.get("COMFYUI_FALLBACK_LOCAL_SYNTH", "true").lower() in {"1", "true", "yes", "on"}
        try:
            comfyui_base_clip(prompt2, seconds, out_file, cfg)
        except Exception as e:
            if not fallback:
                raise
            log(f"ComfyUI backend failed ({e}); falling back to local_synth")
            synth_base_clip(prompt2, seconds, out_file)
        return
    raise RuntimeError(f"Unsupported VIDEO_BACKEND={backend}. Use local_synth, comfyui, or runpod")


def ffprobe_duration(path: Path) -> float:
    out = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    return float(out.strip())


def loop_to_target(base_file: Path, final_file: Path, min_s: int, max_s: int, target_s: int) -> None:
    dur = ffprobe_duration(base_file)
    if dur <= 0:
        raise RuntimeError("Invalid base clip duration")
    t = max(min(target_s, max_s), min_s)
    loops = max(0, math.ceil(t / dur) - 1)
    run_cmd([
        "ffmpeg", "-y",
        "-stream_loop", str(loops),
        "-i", str(base_file),
        "-t", str(t),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",
        str(final_file),
    ], timeout=900)


def concat_video_clips(clips: List[Path], out_file: Path) -> None:
    if not clips:
        raise RuntimeError("concat_video_clips requires at least one input clip")
    if len(clips) == 1:
        if clips[0] != out_file:
            run_cmd(["cp", str(clips[0]), str(out_file)])
        return

    target_w, target_h = _video_size(clips[0])
    target_w = target_w if target_w % 2 == 0 else target_w - 1
    target_h = target_h if target_h % 2 == 0 else target_h - 1

    cmd: List[str] = ["ffmpeg", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip)]

    norm_filters: List[str] = []
    norm_inputs: List[str] = []
    for i in range(len(clips)):
        out_label = f"[v{i}]"
        norm_filters.append(
            (
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS{out_label}"
            )
        )
        norm_inputs.append(out_label)

    concat_inputs = "".join(norm_inputs)
    filter_complex = ";".join(norm_filters + [f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[v]"])

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out_file),
    ]
    run_cmd(cmd, timeout=1200)


def add_motion_effects(video_file: Path, out_file: Path, cfg: Dict[str, str]) -> None:
    zoom = max(1.02, min(1.25, float(cfg.get("MOTION_ZOOM", "1.09"))))
    sway_x = max(2.0, min(64.0, float(cfg.get("MOTION_SWAY_X", "16"))))
    sway_y = max(2.0, min(96.0, float(cfg.get("MOTION_SWAY_Y", "24"))))
    freq_x = max(0.05, min(2.50, float(cfg.get("MOTION_FREQ_X", "0.42"))))
    freq_y = max(0.05, min(2.50, float(cfg.get("MOTION_FREQ_Y", "0.34"))))
    noise = max(0.0, min(20.0, float(cfg.get("MOTION_NOISE", "4"))))
    contrast = max(0.80, min(1.30, float(cfg.get("MOTION_CONTRAST", "1.06"))))
    saturation = max(0.60, min(1.60, float(cfg.get("MOTION_SATURATION", "1.09"))))
    width, height = _video_size(video_file)
    width = width if width % 2 == 0 else width - 1
    height = height if height % 2 == 0 else height - 1

    vf = (
        f"scale=iw*{zoom}:ih*{zoom}:flags=lanczos,"
        f"crop={width}:{height}:"
        f"x='(in_w-{width})/2 + sin(t*{freq_x})*{sway_x}':"
        f"y='(in_h-{height})/2 + cos(t*{freq_y})*{sway_y}',"
        f"eq=contrast={contrast}:saturation={saturation},"
        f"noise=alls={noise}:allf=t+u,"
        "fps=30,format=yuv420p"
    )
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_file),
            "-vf",
            vf,
            "-an",
            str(out_file),
        ],
        timeout=1200,
    )


def discord_upload(webhook: str, content: str, video_file: Optional[Path] = None, text_file: Optional[Path] = None) -> None:
    if not webhook:
        return

    def _redact_discord_webhook_refs(text: str) -> str:
        return re.sub(
            r"https://discord\.com/api/webhooks/\d+/[A-Za-z0-9._-]+",
            "https://discord.com/api/webhooks/<redacted>",
            text,
        )

    webhook_url = webhook
    if "wait=" not in webhook_url:
        webhook_url += ("&" if "?" in webhook_url else "?") + "wait=true"

    cfg = load_env(ENV_FILE)
    max_upload_mb = max(1.0, float(cfg.get("DISCORD_PREVIEW_MAX_MB", "7.8")))
    max_upload_bytes = int(max_upload_mb * 1024 * 1024)
    upload_video: Optional[Path] = video_file if video_file and video_file.exists() else None
    temp_preview_dir: Optional[Path] = None

    def _prepare_discord_video(src: Path) -> Optional[Path]:
        nonlocal temp_preview_dir
        if src.stat().st_size <= max_upload_bytes:
            return src
        temp_preview_dir = Path(tempfile.mkdtemp(prefix="claw-discord-preview-"))
        profiles = [
            (900, 80, 33),
            (700, 64, 35),
            (520, 48, 37),
        ]
        for i, (video_k, audio_k, crf) in enumerate(profiles, start=1):
            out = temp_preview_dir / f"preview_{i}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-vf",
                "fps=24,scale='if(gt(iw,540),540,iw)':-2:flags=lanczos,format=yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(crf),
                "-maxrate",
                f"{video_k}k",
                "-bufsize",
                f"{video_k * 2}k",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_k}k",
                "-movflags",
                "+faststart",
                str(out),
            ]
            try:
                run_cmd(cmd, timeout=300)
            except Exception:
                continue
            if out.exists() and out.stat().st_size <= max_upload_bytes:
                out_mb = out.stat().st_size / (1024.0 * 1024.0)
                log(
                    f"Discord preview compressed: {src.name} -> {out.name} "
                    f"({out_mb:.2f} MB <= {max_upload_mb:.2f} MB)"
                )
                return out
        return None

    if upload_video and upload_video.stat().st_size > max_upload_bytes:
        try:
            compressed = _prepare_discord_video(upload_video)
            if compressed and compressed.exists():
                upload_video = compressed
        except Exception as e:
            log(f"Discord preview compression failed: {_redact_discord_webhook_refs(str(e))}")

    def _post(include_video: bool, extra_note: str = "") -> None:
        body = f"{content}{extra_note}" if extra_note else content
        cmd = [
            "curl",
            "-sS",
            "--fail-with-body",
            "--retry",
            "2",
            "--retry-delay",
            "1",
            "--retry-all-errors",
            "-X",
            "POST",
            webhook_url,
            "-F",
            f"content={body}",
        ]
        if include_video and upload_video and upload_video.exists():
            cmd += ["-F", f"file1=@{upload_video}"]
        if text_file and text_file.exists():
            cmd += ["-F", f"file2=@{text_file}"]
        try:
            raw = run_cmd(cmd, timeout=240).strip()
        except Exception as e:
            raise RuntimeError(_redact_discord_webhook_refs(str(e))) from None
        if raw:
            try:
                obj = json.loads(raw)
                msg_id = str(obj.get("id", "")).strip()
                channel_id = str(obj.get("channel_id", "")).strip()
                if msg_id:
                    log(f"Discord webhook sent message_id={msg_id} channel_id={channel_id or 'unknown'}")
            except Exception:
                log(f"Discord webhook response (non-json): {raw[:180]}")

    has_video = bool(upload_video and upload_video.exists())
    try:
        _post(include_video=has_video)
    except Exception as e:
        if has_video and upload_video:
            size_mb = float(upload_video.stat().st_size) / (1024.0 * 1024.0)
            source_name = video_file.name if video_file else upload_video.name
            log(f"Discord upload with video failed ({upload_video.name}, {size_mb:.2f} MB): {e}")
            note = (
                f"\n(Preview video omitted: {source_name}, {size_mb:.1f} MB."
                " Upload failed due to Discord limits or network.)"
            )
            _post(include_video=False, extra_note=note)
            return
        raise
    finally:
        if temp_preview_dir and temp_preview_dir.exists():
            shutil.rmtree(temp_preview_dir, ignore_errors=True)


def _categorize_pipeline_error(e: Exception) -> str:
    err = str(e).lower()
    if "network is unreachable" in err or "errno 101" in err or "errno 61" in err or "enetunreach" in err:
        return "Network error: Docker->ComfyUI connection failed (host.docker.internal unreachable)"
    if "connection refused" in err:
        return "ComfyUI not running (connection refused)"
    if "comfyui not ready" in err:
        return "ComfyUI nicht erreichbar nach Wartezeit (evtl. Neustart noetig)"
    if "stall watchdog" in err:
        return "ComfyUI stall watchdog - kein Fortschritt (MPS ueberlastet?)"
    if "step watchdog" in err or "sec/it" in err:
        return "ComfyUI step watchdog - Generation zu langsam (MPS ueberlastet?)"
    if "hard timeout" in err:
        return "ComfyUI hard timeout - Generation hat zu lange gedauert"
    if "execution_interrupted" in err or "comfyui_execution_interrupted" in err:
        return "ComfyUI Execution interrupted (manuell oder OOM)"
    if "edge tts" in err or "voiceover" in err:
        return f"Voiceover-Fehler: {str(e)[:120]}"
    if "pillow" in err or "no module named 'pil'" in err:
        return "Pillow nicht installiert - Text-Overlay uebersprungen"
    if "pipeline" in err and ("busy" in err or "active" in err):
        return "Pipeline belegt - anderer Job laueft noch"
    return f"Pipeline-Fehler: {str(e)[:200]}"


def _discord_notify_error(webhook: str, e: Exception, story_id: str = "", title: str = "") -> None:
    if not webhook:
        return
    summary = _categorize_pipeline_error(e)
    parts = ["Generation failed"]
    if title:
        parts.append(f"Story: {title}")
    if story_id:
        parts.append(f"ID: {story_id[:12]}")
    parts.append(summary)
    try:
        discord_upload(webhook, "\n".join(parts))
    except Exception as ne:
        log(f"Discord error notify failed: {ne}")


def resolve_preview_webhook(cfg: Dict[str, str], source_channel_id: str = "") -> str:
    sid = str(source_channel_id or "").strip()
    if sid:
        keyed = cfg.get(f"PREVIEW_DISCORD_WEBHOOK_URL_{sid}", "").strip()
        if keyed:
            return keyed
    return cfg.get("PREVIEW_DISCORD_WEBHOOK_URL", "").strip() or cfg.get("DISCORD_WEBHOOK_URL", "").strip()


def _resolve_tiktok_account(cfg: Dict[str, str], lang: str) -> str:
    """Pick the TikTok account name based on story language."""
    lang_lc = (lang or "en").strip().lower()[:2]
    # Try language-specific account first, then default
    account = cfg.get(f"TIKTOK_ACCOUNT_{lang_lc.upper()}", "").strip()
    if account:
        return account
    return cfg.get("TIKTOK_ACCOUNT", "").strip()


def tiktok_publish(cfg: Dict[str, str], story_id: str, clip_file: Path, caption: str, hashtags: str,
                   lang: str = "en") -> Dict[str, Any]:
    dry_run = cfg.get("TIKTOK_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}

    description = caption.strip()
    if hashtags:
        tag_str = " ".join(t if t.startswith("#") else f"#{t}" for t in hashtags.split(",") if t.strip())
        if tag_str:
            description = f"{description}\n{tag_str}" if description else tag_str

    account = _resolve_tiktok_account(cfg, lang)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "message": "TikTok publish simulated (TIKTOK_DRY_RUN=true)",
            "story_id": story_id,
            "clip": str(clip_file),
            "description": description,
            "lang": lang,
            "account": account or "(none)",
        }

    if not account:
        raise RuntimeError(
            f"No TikTok account configured for lang={lang}. "
            f"Set TIKTOK_ACCOUNT_{lang.upper()[:2]} or TIKTOK_ACCOUNT in mac_api.env"
        )

    venv_python = str(ROOT / ".venv-tiktok" / "bin" / "python3.12")
    upload_script = str(ROOT / "bin" / "tiktok_upload.py")
    timeout_sec = int(cfg.get("TIKTOK_UPLOAD_TIMEOUT_SEC", "300"))

    cmd = [
        venv_python, upload_script,
        "--video", str(clip_file),
        "--description", description,
        "--account", account,
        "--headless",
    ]
    log(f"TikTok upload: lang={lang} account={account} clip={clip_file}")

    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"TikTok upload timed out after {timeout_sec}s")

    stdout = (p.stdout or "").strip()
    stderr = (p.stderr or "").strip()

    try:
        result = json.loads(stdout)
    except Exception:
        result = {"raw_stdout": stdout}

    if p.returncode != 0:
        err_msg = result.get("error", stderr or stdout or "unknown error")
        raise RuntimeError(f"TikTok upload failed (exit {p.returncode}): {err_msg}")

    result["dry_run"] = False
    result["lang"] = lang
    result["account"] = account
    return result


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        cfg = load_env(ENV_FILE)
        auth = cfg.get("MAC_API_TOKEN", "").strip()
        if auth:
            got = self.headers.get("X-Api-Token", "")
            if got != auth:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return

        try:
            size = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(size) if size > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"invalid_json: {e}"})
            return

        try:
            current_story_id = ""
            current_run_dir: Optional[Path] = None
            current_generate_started_ts = 0.0
            source_channel_id = str(payload.get("source_channel_id", "")).strip()

            if self.path == "/health":
                self._json(200, {"ok": True, "service": "mac_api"})
                return

            if self.path == "/status":
                lang = str(payload.get("lang", "en")).strip().lower()
                story_id = str(payload.get("story_id", "")).strip()
                state = load_state()
                resp = status_payload(state, story_id)
                if payload.get("notify", False) or str(payload.get("notify", "")).lower() in {"1", "true", "yes", "on"}:
                    webhook = str(payload.get("webhook", "")).strip() or resolve_preview_webhook(cfg, source_channel_id)
                    if webhook:
                        msg = format_status_message(resp.get("active_status", {}), state, lang)
                        try:
                            discord_upload(webhook, msg)
                        except Exception as e:
                            log(f"Status notify failed: {e}")
                self._json(200, resp)
                return

            if self.path == "/generate":
                cleanup_old_output_dirs(cfg)
                story_text = str(payload.get("story_text", "")).strip()
                refresh_overlays_only = str(payload.get("refresh_overlays_only", "false")).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if not story_text:
                    self._json(400, {"ok": False, "error": "story_text_required"})
                    return

                base_prompt = str(payload.get("prompt", "")).strip() or "cinematic emotional vertical story"
                prompt = merge_prompt_with_story_plan(base_prompt, story_text, cfg)
                story_id = str(payload.get("story_id", "")).strip() or hashlib.sha1(story_text.encode("utf-8")).hexdigest()
                current_story_id = story_id
                force_retry = str(payload.get("force_retry", "false")).strip().lower() in {"1", "true", "yes", "on"}
                interrupted_cooldown_s = max(0, int(cfg.get("COMFYUI_INTERRUPTED_COOLDOWN_SEC", "300")))
                if interrupted_cooldown_s > 0 and not force_retry:
                    state = load_state()
                    interrupted_stories = state.get("interrupted_stories", {}) if isinstance(state, dict) else {}
                    if isinstance(interrupted_stories, dict):
                        entry = interrupted_stories.get(story_id)
                        if isinstance(entry, dict):
                            ts = float(entry.get("ts", 0.0) or 0.0)
                            if ts > 0:
                                elapsed = max(0.0, time.time() - ts)
                                if elapsed < interrupted_cooldown_s:
                                    retry_after = int(math.ceil(interrupted_cooldown_s - elapsed))
                                    log(
                                        f"/generate blocked after manual interrupt: story_id={story_id} "
                                        f"retry_after_sec={retry_after}"
                                    )
                                    self._json(
                                        409,
                                        {
                                            "ok": False,
                                            "error": "story_recently_interrupted",
                                            "story_id": story_id,
                                            "retry_after_sec": retry_after,
                                        },
                                    )
                                    return
                global GENERATE_ACTIVE_STORY_ID, GENERATE_ACTIVE_SINCE
                if not GENERATE_LOCK.acquire(blocking=False):
                    active_story_id = str(GENERATE_ACTIVE_STORY_ID or "")
                    log(
                        f"/generate busy: active_story_id={active_story_id or 'unknown'} "
                        f"requested_story_id={story_id}"
                    )
                    self._json(
                        202,
                        {
                            "ok": True,
                            "busy": True,
                            "story_id": story_id,
                            "active_story_id": active_story_id,
                            "active_since": GENERATE_ACTIVE_SINCE,
                        },
                    )
                    return

                GENERATE_ACTIVE_STORY_ID = story_id
                GENERATE_ACTIVE_SINCE = datetime.now().isoformat()
                current_generate_started_ts = time.time()
                preview_webhook = resolve_preview_webhook(cfg, source_channel_id)
                update_active_status("starting", "preparing story", story_id)
                try:
                    title_preview = extract_title(story_text)
                except Exception:
                    title_preview = "Story"
                if preview_webhook:
                    try:
                        discord_upload(
                            preview_webhook,
                            f"Generation started: {title_preview}\nStory ID: {story_id}\nStage: preparing",
                        )
                    except Exception as e:
                        log(f"Discord start notify failed: {e}")
                try:
                    state = load_state()
                    stories = state.setdefault("stories", {})
                    story_state = stories.get(story_id, {}) if isinstance(stories, dict) else {}

                    # Canonical per-story output directory avoids duplicates on retries.
                    run_dir = OUT_DIR / story_id
                    run_dir.mkdir(parents=True, exist_ok=True)
                    current_run_dir = run_dir

                    existing_clip = find_existing_story_clip(
                        story_id,
                        run_dir,
                        str(story_state.get("clip_path", "")) if isinstance(story_state, dict) else "",
                    )
                    refresh_source_clip: Optional[Path] = None
                    refresh_source_tmp: Optional[Path] = None
                    if existing_clip and existing_clip.exists():
                        if refresh_overlays_only:
                            candidate_paths: List[Path] = []
                            state_base = ""
                            if isinstance(story_state, dict):
                                state_base = str(story_state.get("base_clip_path", "")).strip()
                            if state_base:
                                candidate_paths.append(Path(state_base))
                            candidate_paths.extend(
                                [
                                    run_dir / "final_looped.mp4",
                                    run_dir / "final_motion.mp4",
                                    run_dir / "final_vo.mp4",
                                    existing_clip,
                                ]
                            )
                            for cand in candidate_paths:
                                if cand.exists():
                                    refresh_source_clip = cand
                                    break
                            if refresh_source_clip:
                                refresh_source_tmp = run_dir / "__refresh_source.mp4"
                                if refresh_source_tmp.exists():
                                    refresh_source_tmp.unlink(missing_ok=True)
                                if refresh_source_clip.resolve() != refresh_source_tmp.resolve():
                                    shutil.copy2(refresh_source_clip, refresh_source_tmp)
                                else:
                                    refresh_source_tmp = refresh_source_clip
                                log(
                                    f"Refresh overlays only for story_id={story_id} "
                                    f"using source={refresh_source_clip}"
                                )
                            else:
                                log(
                                    f"Refresh overlays requested but no source clip found for story_id={story_id}; "
                                    "falling back to cached clip"
                                )
                        else:
                            title = extract_title(story_text)
                            info = extract_caption_hashtags(story_text)
                            story_file = run_dir / "story.txt"
                            story_file.write_text(story_text, encoding="utf-8")

                            existing_info: Dict[str, Any] = dict(story_state) if isinstance(story_state, dict) else {}
                            base_looped = existing_clip.parent / "final_looped.mp4"
                            voiceover_text = str(existing_info.get("voiceover_text", "")).strip() or extract_voiceover_text(story_text)
                            voice_choice = str(existing_info.get("voice_choice", "")).strip() or extract_voice_choice_text(story_text)
                            music_direction = str(existing_info.get("music_direction", "")).strip() or extract_music_direction_text(story_text)
                            text_beats = existing_info.get("text_beats")
                            if not isinstance(text_beats, list):
                                text_beats = extract_on_screen_text_beats(story_text)

                            existing_info.update(
                                {
                                    "story_id": story_id,
                                    "title": title or existing_info.get("title", "Story Preview"),
                                    "story_text": story_text,
                                    "caption": info["caption"] or existing_info.get("caption", ""),
                                    "hashtags": info["hashtags"] or existing_info.get("hashtags", ""),
                                    "clip_path": str(existing_clip),
                                    "base_clip_path": str(base_looped if base_looped.exists() else existing_clip),
                                    "run_dir": str(existing_clip.parent),
                                    "voiceover_text": voiceover_text,
                                    "voice_choice": voice_choice,
                                    "music_direction": music_direction,
                                    "text_beats": text_beats,
                                    "generated_at": existing_info.get("generated_at") or datetime.now().isoformat(),
                                    "published": bool(existing_info.get("published", False)),
                                    "source_channel_id": source_channel_id or str(existing_info.get("source_channel_id", "")),
                                }
                            )
                            stories[story_id] = existing_info
                            state["stories"] = stories
                            state["last_story_id"] = story_id
                            save_state(state)
                            cleanup_legacy_story_dirs(story_id, existing_clip.parent if existing_clip.parent.exists() else run_dir)
                            preview_webhook = resolve_preview_webhook(cfg, str(existing_info.get("source_channel_id", "")))
                            update_active_status("ready", "preview cached", story_id)
                            try:
                                discord_upload(
                                    preview_webhook,
                                    f"Preview ready: {title}\nStory ID: {story_id}\nReply GO again for refresh, or NO/TREND/THEME for a new story.",
                                    existing_clip,
                                    story_file,
                                )
                            except Exception as e:
                                log(f"Discord cached preview notify failed: {e}")
                            log(f"Reusing existing clip for story_id={story_id} -> {existing_clip}")
                            self._json(200, {"ok": True, "story_id": story_id, "clip_path": str(existing_clip), "cached": True})
                            return

                    update_active_status("generating", "rendering", story_id)
                    base_seconds = int(cfg.get("BASE_CLIP_SECONDS", "8"))
                    min_s = int(cfg.get("MIN_SECONDS", "40"))
                    max_s = int(cfg.get("MAX_SECONDS", "60"))
                    target_s = int(cfg.get("TARGET_SECONDS", "50"))
                    base_clip_variants = max(1, min(6, int(cfg.get("BASE_CLIP_VARIANTS", "2"))))
                    motion_enabled = cfg.get("MOTION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
                    voiceover_enabled = cfg.get("VOICEOVER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
                    voiceover_max_chars = int(cfg.get("VOICEOVER_MAX_CHARS", "900"))
                    voiceover_captions_enabled = cfg.get("VOICEOVER_CAPTIONS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
                    bgm_enabled = cfg.get("BGM_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
                    text_beats_enabled = cfg.get("TEXT_BEATS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
                    text_beats_extra_enabled = cfg.get("TEXT_BEATS_EXTRA_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
                    prune_intermediates = cfg.get("OUTPUT_PRUNE_INTERMEDIATES", "false").lower() in {"1", "true", "yes", "on"}
                    text_beats_timed = extract_on_screen_text_beats_timed(story_text) if text_beats_enabled else []
                    text_beats = [t for t, _, _ in text_beats_timed]
                    text_beats_windows: Optional[List[Optional[tuple[float, float]]]] = None
                    if text_beats_timed:
                        parsed_windows: List[Optional[tuple[float, float]]] = []
                        for _, s0, e0 in text_beats_timed:
                            if s0 is not None and e0 is not None and e0 > s0:
                                parsed_windows.append((float(s0), float(e0)))
                            else:
                                parsed_windows.append(None)
                        if any(w is not None for w in parsed_windows):
                            text_beats_windows = parsed_windows

                    voiceover_text = str(payload.get("voiceover_text", "")).strip() or extract_voiceover_text(story_text)
                    voiceover_text = _trim_voiceover_text(voiceover_text, voiceover_max_chars)
                    voice_choice_text = str(payload.get("voice_choice", "")).strip() or extract_voice_choice_text(story_text)
                    music_direction = str(payload.get("music_direction", "")).strip() or extract_music_direction_text(story_text)
                    voice_cfg = cfg
                    voice_meta: Dict[str, str] = {}
                    if voiceover_enabled and voiceover_text and cfg.get("VOICEOVER_BACKEND", "macos_say").strip().lower() == "edge_tts":
                        voice_meta = select_edge_tts_voice(
                            voiceover_text,
                            cfg,
                            story_seed=story_id,
                            source_channel_id=source_channel_id,
                            explicit_choice=voice_choice_text,
                        )
                        selected_voice = str(voice_meta.get("voice", "")).strip()
                        if selected_voice:
                            voice_cfg = dict(cfg)
                            voice_cfg["VOICEOVER_EDGE_VOICE"] = selected_voice
                            log(
                                "Voiceover edge voice selected: "
                                f"{selected_voice}"
                                f" (lang={voice_meta.get('lang', 'auto') or 'auto'}, "
                                f"gender={voice_meta.get('gender', 'auto') or 'auto'})"
                            )
                    if voice_choice_text and voice_meta.get("lang") != "explicit":
                        log(f"Voice choice in story not recognized, fallback to auto-selection: {voice_choice_text}")

                    base = run_dir / "base.mp4"
                    final = run_dir / "final_looped.mp4"
                    voice_file: Optional[Path] = None
                    voice_subtitle_file: Optional[Path] = None
                    bgm_file: Optional[Path] = None
                    voice_secs = 0.0

                    if voiceover_enabled and voiceover_text:
                        try:
                            voice_file = run_dir / "voiceover.wav"
                            voice_subtitle_file = run_dir / "voiceover.vtt"
                            voice_subtitle_file.unlink(missing_ok=True)
                            generate_voiceover_audio(voiceover_text, voice_file, voice_cfg, subtitle_out=voice_subtitle_file)
                            voice_secs = ffprobe_duration(voice_file)
                            target_s = max(min_s, min(max_s, max(target_s, int(math.ceil(voice_secs)) + 1)))
                            (run_dir / "voiceover.txt").write_text(voiceover_text, encoding="utf-8")
                            if voice_meta and voice_meta.get("voice"):
                                (run_dir / "voice_choice.txt").write_text(
                                    (
                                        f"requested={voice_choice_text}\n"
                                        f"lang={voice_meta.get('lang', '')}\n"
                                        f"gender={voice_meta.get('gender', '')}\n"
                                        f"voice={voice_meta.get('voice', '')}\n"
                                    ),
                                    encoding="utf-8",
                                )
                        except Exception as e:
                            voice_file = None
                            voice_subtitle_file = None
                            log(f"Voiceover generation failed: {e}")

                    if refresh_source_tmp and refresh_source_tmp.exists():
                        final_clip = refresh_source_tmp
                    else:
                        scene_hints = scene_hints_from_story(story_text, cfg)
                        scene_prompts: List[str] = []
                        base_parts: List[Path] = []
                        variant_retries = max(0, min(4, int(cfg.get("BASE_VARIANT_RETRIES", "1"))))
                        recovered_previews: set = set()
                        for i in range(base_clip_variants):
                            scene_hint = scene_hints[i % len(scene_hints)] if scene_hints else ""
                            p = compose_scene_prompt(
                                merged_prompt=prompt,
                                scene_hint=scene_hint,
                                shot_index=i + 1,
                                total_shots=base_clip_variants,
                                cfg=cfg,
                            )
                            part = run_dir / f"base_{i + 1:02d}.mp4"

                            last_err: Optional[Exception] = None
                            ok = False
                            attempts = variant_retries + 1
                            for attempt in range(1, attempts + 1):
                                part.unlink(missing_ok=True)
                                try:
                                    generate_base_clip(p, base_seconds, part, cfg)
                                    ok = True
                                    break
                                except ComfyInterruptedError:
                                    # Preserve explicit interrupted semantics for upstream cooldown handling.
                                    raise
                                except Exception as e:
                                    last_err = e
                                    log(
                                        f"Base variant {i + 1}/{base_clip_variants} failed "
                                        f"(attempt {attempt}/{attempts}): {e}"
                                    )
                                    if attempt < attempts:
                                        err_s = str(e).lower()
                                        if (
                                            "connection refused" in err_s
                                            or "timed out" in err_s
                                            or "timeout" in err_s
                                            or "unreachable" in err_s
                                            or "network" in err_s
                                        ):
                                            retry_wait = max(0, min(600, int(cfg.get("COMFYUI_RESTART_READY_WAIT_SEC", "300"))))
                                            log(f"Connection/network error; waiting {retry_wait}s before retry")
                                            _comfy_wait_until_ready(
                                                cfg.get("COMFYUI_API_URL", "http://127.0.0.1:8188").rstrip("/"),
                                                timeout_sec=retry_wait,
                                                poll_sec=5.0,
                                            )
                                        else:
                                            time.sleep(1.0)
                            if not ok:
                                # Network errors: ComfyUI may have completed the job even though the
                                # pipeline lost connectivity. Check for a new preview webm and recover.
                                err_s = str(last_err).lower()
                                is_network_err = (
                                    "unreachable" in err_s
                                    or "network" in err_s
                                    or "connection refused" in err_s
                                    or "timed out" in err_s
                                    or "timeout" in err_s
                                    or "errno 101" in err_s
                                    or "errno 61" in err_s
                                )
                                if is_network_err and _recover_clip_from_comfy_preview(
                                    part, cfg, current_generate_started_ts, skip_paths=recovered_previews
                                ):
                                    log(
                                        f"Base variant {i + 1}/{base_clip_variants} recovered from "
                                        f"ComfyUI preview after network error"
                                    )
                                    ok = True
                            if not ok:
                                raise RuntimeError(
                                    f"Base variant {i + 1}/{base_clip_variants} failed after {attempts} attempts: {last_err}"
                                )
                            scene_prompts.append(p)
                            base_parts.append(part)

                        if base_parts:
                            if len(base_parts) == 1:
                                base = base_parts[0]
                            else:
                                concat_video_clips(base_parts, base)
                        (run_dir / "scene_prompts.txt").write_text("\n\n".join(scene_prompts), encoding="utf-8")

                        loop_to_target(base, final, min_s, max_s, target_s)
                        final_clip = final
                        if motion_enabled:
                            try:
                                motioned = run_dir / "final_motion.mp4"
                                add_motion_effects(final_clip, motioned, cfg)
                                final_clip = motioned
                            except Exception as e:
                                log(f"Motion effects failed: {e}")

                    if bgm_enabled:
                        try:
                            bgm_file = run_dir / "bgm.wav"
                            bgm_secs = ffprobe_duration(final_clip)
                            if bgm_secs <= 0:
                                bgm_secs = float(target_s)
                            sample = pick_bgm_sample(cfg, music_direction)
                            allow_synth = str(cfg.get("BGM_ALLOW_SYNTH", "true")).lower() in {"1", "true", "yes", "on"}
                            if sample:
                                log(f"BGM sample selected: {sample}")
                                prep_bgm_sample(sample, bgm_secs, bgm_file, cfg)
                            elif allow_synth:
                                log("BGM sample not found; using synth fallback")
                                synth_background_music(music_direction, bgm_secs, bgm_file, cfg)
                            else:
                                log("BGM sample not found and synth fallback disabled; keeping original clip audio")
                                bgm_file = None
                            if music_direction:
                                (run_dir / "music_direction.txt").write_text(music_direction, encoding="utf-8")
                        except Exception as e:
                            if bgm_file:
                                bgm_file.unlink(missing_ok=True)
                            bgm_file = None
                            log(f"BGM generation failed: {e}")

                    audio_applied = False
                    if voice_file and voice_file.exists() and bgm_file and bgm_file.exists():
                        try:
                            voiced_bgm = run_dir / "final_vo_bgm.mp4"
                            add_voiceover_and_bgm_to_clip(final_clip, voice_file, bgm_file, voiced_bgm, cfg)
                            final_clip = voiced_bgm
                            audio_applied = True
                        except Exception as e:
                            log(f"Voiceover+BGM mux failed: {e}")

                    if (not audio_applied) and voice_file and voice_file.exists():
                        try:
                            voiced = run_dir / "final_vo.mp4"
                            add_voiceover_to_clip(final_clip, voice_file, voiced, cfg)
                            final_clip = voiced
                            audio_applied = True
                        except Exception as e:
                            log(f"Voiceover mux failed: {e}")

                    if (not audio_applied) and bgm_file and bgm_file.exists():
                        try:
                            bgm_out = run_dir / "final_bgm.mp4"
                            add_bgm_to_clip(final_clip, bgm_file, bgm_out, cfg)
                            final_clip = bgm_out
                        except Exception as e:
                            log(f"BGM mux failed: {e}")

                    if voiceover_captions_enabled and voiceover_text:
                        try:
                            cue_source_file = voice_subtitle_file if voice_subtitle_file and voice_subtitle_file.exists() else None
                            cue_duration = voice_secs if voice_secs > 0 else ffprobe_duration(final_clip)
                            timed_cues = build_voiceover_timed_cues(
                                voiceover_text,
                                cue_duration,
                                cfg,
                                subtitle_file=cue_source_file,
                            )
                            if timed_cues:
                                cue_lines = [f"{s:.2f}-{e:.2f} {t}" for t, s, e in timed_cues]
                                (run_dir / "voiceover_captions.txt").write_text("\n".join(cue_lines), encoding="utf-8")
                                voiced_captioned = run_dir / "final_vo_text.mp4"
                                vo_text = [t for t, _, _ in timed_cues]
                                vo_windows = [(s, e) for _, s, e in timed_cues]
                                add_text_beats_overlay(
                                    final_clip,
                                    vo_text,
                                    voiced_captioned,
                                    cfg,
                                    font_file=(
                                        cfg.get("VOICEOVER_CAPTIONS_FONT_FILE", "").strip()
                                        or cfg.get("TEXT_BEATS_FONT_FILE", "/System/Library/Fonts/Helvetica.ttc").strip()
                                        or "/System/Library/Fonts/Helvetica.ttc"
                                    ),
                                    font_size=int(cfg.get("VOICEOVER_CAPTIONS_FONT_SIZE", "44")),
                                    y_frac=float(cfg.get("VOICEOVER_CAPTIONS_Y_FRAC", "0.90")),
                                    display_sec=float(cfg.get("VOICEOVER_CAPTIONS_DISPLAY_SEC", "2.8")),
                                    cue_windows=vo_windows,
                                    text_color=cfg.get("VOICEOVER_CAPTIONS_TEXT_COLOR", ""),
                                    text_stroke_color=cfg.get("VOICEOVER_CAPTIONS_STROKE_COLOR", ""),
                                    text_stroke_width=int(cfg.get("VOICEOVER_CAPTIONS_STROKE_WIDTH", "2")),
                                    card_color=cfg.get("VOICEOVER_CAPTIONS_CARD_COLOR", ""),
                                    card_border_color=cfg.get("VOICEOVER_CAPTIONS_CARD_BORDER_COLOR", ""),
                                    card_border_width=int(cfg.get("VOICEOVER_CAPTIONS_CARD_BORDER_WIDTH", "0")),
                                    tmp_prefix="vocap",
                                )
                                final_clip = voiced_captioned
                        except Exception as e:
                            log(f"Voiceover captions failed: {e}")

                    if text_beats:
                        try:
                            (run_dir / "text_beats.txt").write_text("\n".join(text_beats), encoding="utf-8")
                            captioned = run_dir / "final_text.mp4"
                            add_text_beats_overlay(
                                final_clip,
                                text_beats,
                                captioned,
                                cfg,
                                cue_windows=text_beats_windows,
                                tmp_prefix="beats",
                            )
                            final_clip = captioned
                        except Exception as e:
                            log(f"Text beats overlay failed: {e}")

                    if text_beats and text_beats_extra_enabled:
                        try:
                            extra = run_dir / "final_text_extra.mp4"
                            add_text_beats_overlay(
                                final_clip,
                                text_beats,
                                extra,
                                cfg,
                                font_file=(
                                    cfg.get("TEXT_BEATS_EXTRA_FONT_FILE", "").strip()
                                    or cfg.get("TEXT_BEATS_FONT_FILE", "/System/Library/Fonts/Helvetica.ttc").strip()
                                    or "/System/Library/Fonts/Helvetica.ttc"
                                ),
                                font_size=int(cfg.get("TEXT_BEATS_EXTRA_FONT_SIZE", str(int(cfg.get("TEXT_BEATS_FONT_SIZE", "56")) + 18))),
                                y_frac=float(cfg.get("TEXT_BEATS_EXTRA_Y_FRAC", "0.12")),
                                display_sec=float(cfg.get("TEXT_BEATS_EXTRA_DISPLAY_SEC", cfg.get("TEXT_BEATS_DISPLAY_SEC", "4.5"))),
                                cue_windows=text_beats_windows,
                                text_color=cfg.get("TEXT_BEATS_EXTRA_TEXT_COLOR", cfg.get("TEXT_BEATS_TEXT_COLOR", "#FFFFFF")),
                                text_stroke_color=cfg.get("TEXT_BEATS_EXTRA_STROKE_COLOR", cfg.get("TEXT_BEATS_STROKE_COLOR", "#000000E6")),
                                text_stroke_width=int(cfg.get("TEXT_BEATS_EXTRA_STROKE_WIDTH", cfg.get("TEXT_BEATS_STROKE_WIDTH", "2"))),
                                card_color=cfg.get("TEXT_BEATS_EXTRA_CARD_COLOR", cfg.get("TEXT_BEATS_CARD_COLOR", "#00000096")),
                                card_border_color=cfg.get("TEXT_BEATS_EXTRA_CARD_BORDER_COLOR", cfg.get("TEXT_BEATS_CARD_BORDER_COLOR", "#FFFFFF00")),
                                card_border_width=int(cfg.get("TEXT_BEATS_EXTRA_CARD_BORDER_WIDTH", cfg.get("TEXT_BEATS_CARD_BORDER_WIDTH", "0"))),
                                tmp_prefix="beatsx",
                            )
                            final_clip = extra
                        except Exception as e:
                            log(f"Extra text beats overlay failed: {e}")

                    info = extract_caption_hashtags(story_text)
                    title = extract_title(story_text)
                    story_file = run_dir / "story.txt"
                    story_file.write_text(story_text, encoding="utf-8")
                    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

                    state = load_state()
                    stories = state.setdefault("stories", {})
                    interrupted_stories = state.get("interrupted_stories", {})
                    if isinstance(interrupted_stories, dict) and story_id in interrupted_stories:
                        interrupted_stories.pop(story_id, None)
                        state["interrupted_stories"] = interrupted_stories
                    base_clip_for_state = final if (not prune_intermediates or final == final_clip) else final_clip
                    stories[story_id] = {
                        "story_id": story_id,
                        "title": title,
                        "story_text": story_text,
                        "caption": info["caption"],
                        "hashtags": info["hashtags"],
                        "clip_path": str(final_clip),
                        "base_clip_path": str(base_clip_for_state),
                        "run_dir": str(run_dir),
                        "voiceover_text": voiceover_text,
                        "voice_choice": voice_choice_text,
                        "voiceover_voice": str(voice_meta.get("voice", "")).strip(),
                        "voiceover_lang": str(voice_meta.get("lang", "")).strip(),
                        "voiceover_gender": str(voice_meta.get("gender", "")).strip(),
                        "music_direction": music_direction,
                        "text_beats": text_beats,
                        "generated_at": datetime.now().isoformat(),
                        "published": False,
                        "source_channel_id": source_channel_id,
                    }
                    state["last_story_id"] = story_id
                    save_state(state)
                    cleanup_legacy_story_dirs(story_id, run_dir)

                    preview_webhook = resolve_preview_webhook(cfg, source_channel_id)
                    update_active_status("ready", "preview ready", story_id)
                    try:
                        discord_upload(
                            preview_webhook,
                            f"Preview ready: {title}\nStory ID: {story_id}\nReply GO again for refresh, or NO/TREND/THEME for a new story.",
                            final_clip,
                            story_file,
                        )
                    except Exception as e:
                        log(f"Discord preview notify failed: {e}")
                    prune_story_intermediates(run_dir, final_clip, cfg)

                    # Clean up stale failed_preview from a previous failed run for the same story.
                    stale_fp = run_dir / "failed_preview.webm"
                    if stale_fp.exists():
                        stale_fp.unlink(missing_ok=True)

                    log(f"Generated clip for story_id={story_id} -> {final_clip}")
                    self._json(200, {"ok": True, "story_id": story_id, "clip_path": str(final_clip)})
                    return
                finally:
                    GENERATE_ACTIVE_STORY_ID = ""
                    GENERATE_ACTIVE_SINCE = ""
                    GENERATE_LOCK.release()

            if self.path == "/publish":
                cleanup_old_output_dirs(cfg)
                state = load_state()
                stories = state.get("stories", {})
                story_id = str(payload.get("story_id", "")).strip() or state.get("last_story_id")
                if not story_id or story_id not in stories:
                    self._json(404, {"ok": False, "error": "story_not_found"})
                    return

                info = stories[story_id]
                clip = Path(info["clip_path"])
                if not clip.exists():
                    self._json(404, {"ok": False, "error": "clip_not_found"})
                    return

                result = tiktok_publish(
                    cfg,
                    story_id,
                    clip,
                    info.get("caption", ""),
                    info.get("hashtags", ""),
                    lang=str(info.get("voiceover_lang", "en")).strip() or "en",
                )

                info["published"] = True
                info["published_at"] = datetime.now().isoformat()
                info["publish_result"] = result
                stories[story_id] = info
                state["stories"] = stories
                save_state(state)

                source_channel_id = str(payload.get("source_channel_id", "")).strip() or str(info.get("source_channel_id", ""))
                notify_webhook = resolve_preview_webhook(cfg, source_channel_id)
                update_active_status("published", f"publish done (dry_run={result.get('dry_run', False)})", story_id)
                try:
                    discord_upload(
                        notify_webhook,
                        f"Publish done for story_id={story_id}. dry_run={result.get('dry_run', False)}",
                    )
                except Exception as e:
                    log(f"Discord publish notify failed: {e}")

                log(f"Published story_id={story_id}")
                self._json(200, {"ok": True, "story_id": story_id, "result": result})
                return

            self._json(404, {"ok": False, "error": "not_found"})
        except Exception as e:
            if self.path == "/generate":
                try:
                    preserve_latest_comfy_preview(current_run_dir, cfg, min_mtime_ts=current_generate_started_ts)
                except Exception as pe:
                    log(f"Failed preview-preserve error: {pe}")
                if current_story_id and isinstance(e, ComfyInterruptedError):
                    try:
                        state = load_state()
                        interrupted_stories = state.get("interrupted_stories", {})
                        if not isinstance(interrupted_stories, dict):
                            interrupted_stories = {}
                        interrupted_stories[current_story_id] = {
                            "ts": time.time(),
                            "at": _now_iso(),
                            "error": str(e),
                        }
                        state["interrupted_stories"] = interrupted_stories
                        save_state(state)
                    except Exception as se:
                        log(f"Interrupted-state save failed: {se}")
                try:
                    cleanup_failed_run_intermediates(current_run_dir)
                except Exception as ce:
                    log(f"Failed-run cleanup error: {ce}")
            log(f"ERROR {self.path}: {e}")
            if self.path == "/generate":
                try:
                    err_webhook = resolve_preview_webhook(cfg, source_channel_id)
                    if err_webhook:
                        _discord_notify_error(err_webhook, e, current_story_id)
                except Exception as ne:
                    log(f"Discord error notify failed: {ne}")
            try:
                if current_story_id:
                    update_active_status("error", str(e), current_story_id)
                else:
                    update_active_status("error", str(e))
            except Exception:
                pass
            if self.path == "/generate" and isinstance(e, ComfyInterruptedError):
                self._json(
                    409,
                    {
                        "ok": False,
                        "error": "comfyui_execution_interrupted",
                        "story_id": current_story_id,
                        "retry_after_sec": max(0, int(cfg.get("COMFYUI_INTERRUPTED_COOLDOWN_SEC", "300"))),
                    },
                )
                return
            self._json(500, {"ok": False, "error": str(e)})


def main() -> int:
    try:
        state = load_state()
        active = load_active_status(state)
        stage = str(active.get("stage") or "").strip().lower()
        if stage in {"preparing", "generating", "publishing"}:
            update_active_status("idle")
            log(f"Reset stale active status on startup (previous stage={stage})")
    except Exception as e:
        log(f"Startup status reset skipped: {e}")

    cfg = load_env(ENV_FILE)
    host = cfg.get("MAC_API_HOST", "127.0.0.1")
    port = int(cfg.get("MAC_API_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    log(f"mac_api listening on {host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
