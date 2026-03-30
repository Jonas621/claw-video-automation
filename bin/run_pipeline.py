#!/usr/bin/env python3
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "config" / ".env"
STATE_FILE = ROOT / "state" / "state.json"
LOG_FILE = ROOT / "logs" / "pipeline.log"
OUT_DIR = ROOT / "output"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
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


def tcl_quote_literal(value: str) -> str:
    # Keep each argument as one Tcl word without invoking shell parsing.
    return "{" + value.replace("{", "\\{").replace("}", "\\}") + "}"


def run_cmd(cmd: List[str], timeout: int = 120) -> str:
    env = os.environ.copy()
    path = env.get("PATH", "")
    extra = ["/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = ":".join(extra + ([path] if path else []))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr}")
    return p.stdout


def parse_json_object_from_text(raw: str) -> Dict[str, Any]:
    text = raw.strip()
    if not text:
        raise ValueError("Empty JSON payload")
    if text.startswith("{"):
        return json.loads(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in text payload")
    return json.loads(text[start:end + 1])


def ssh_cmd(cfg: Dict[str, str], remote_cmd: str, timeout: int = 120) -> str:
    base_cmd = [
        "ssh", "-p", cfg.get("VM_SSH_PORT", "22"),
        f"{cfg['VM_USER']}@{cfg['VM_HOST']}",
        remote_cmd,
    ]
    password = cfg.get("VM_SSH_PASSWORD", "").strip()
    if not password:
        return run_cmd(base_cmd, timeout=timeout)

    if shutil.which("sshpass"):
        return run_cmd(["sshpass", "-p", password] + base_cmd, timeout=timeout)

    if shutil.which("expect"):
        # Fallback for macOS where sshpass is often missing.
        # Uses password-based SSH non-interactively for launchd jobs.
        pw = password.replace("\\", "\\\\").replace('"', '\\"')
        spawn_args = " ".join(tcl_quote_literal(part) for part in base_cmd)
        script = f"""
set timeout {int(timeout)}
spawn -noecho {spawn_args}
expect {{
  -re {{.*yes/no.*}} {{ send "yes\\r"; exp_continue }}
  -re {{.*[Pp]assword:.*}} {{ send "{pw}\\r"; exp_continue }}
  eof
}}
catch wait result
set exit_status [lindex $result 3]
exit $exit_status
"""
        return run_cmd(["expect", "-c", script], timeout=timeout + 10)

    raise RuntimeError("VM_SSH_PASSWORD is set, but neither sshpass nor expect is available")


def ssh_cat(cfg: Dict[str, str], remote_path: str, tail: Optional[int] = None) -> str:
    q = shlex.quote(remote_path)
    remote_cmd = f"tail -n {int(tail)} {q}" if tail else f"cat {q}"
    return ssh_cmd(cfg, remote_cmd)


def parse_ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    t = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def msg_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content") or []
    if not isinstance(content, list):
        return ""
    chunks = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            chunks.append(c.get("text", ""))
    return "\n".join(chunks).strip()


def latest_reply_go_no(session_jsonl: str) -> Dict[str, Any]:
    best = {"ts": 0.0, "value": ""}
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
        if re.fullmatch(r"\s*go\s*[!.]*\s*", txt, flags=re.IGNORECASE):
            ts = parse_ts(obj.get("timestamp")) or parse_ts(m.get("timestamp"))
            if ts > best["ts"]:
                best = {"ts": ts, "value": "GO"}
        if re.fullmatch(r"\s*no\s*[!.]*\s*", txt, flags=re.IGNORECASE):
            ts = parse_ts(obj.get("timestamp")) or parse_ts(m.get("timestamp"))
            if ts > best["ts"]:
                best = {"ts": ts, "value": "NO"}
    return best


def latest_story_summary(runs_jsonl: str) -> str:
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
    return latest_summary


def extract_prompt(story: str) -> str:
    title = ""
    m = re.search(r"\*\*Title\*\*\s*(.+)", story, flags=re.IGNORECASE)
    if m:
        title = m.group(1).strip()
    return (
        "Create a cinematic vertical short video (9:16), realistic, emotional, smooth camera movement, "
        "no text, no watermark, high detail, dramatic lighting. "
        + (f"Theme/title: {title}. " if title else "Theme: emotional story arc. ")
        + "Visual style suitable for TikTok storytelling background footage."
    )


def find_url(obj: Any) -> Optional[str]:
    if isinstance(obj, str) and obj.startswith(("http://", "https://")):
        return obj
    if isinstance(obj, dict):
        for k in ["video_url", "url", "output_url", "download_url", "result_url"]:
            u = find_url(obj.get(k))
            if u:
                return u
        for v in obj.values():
            u = find_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for v in obj:
            u = find_url(v)
            if u:
                return u
    return None


def call_video_api(cfg: Dict[str, str], prompt: str, out_dir: Path) -> Path:
    endpoint = cfg.get("VIDEO_API_ENDPOINT", "").strip()
    if not endpoint:
        out = out_dir / "base.mp4"
        run_cmd([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=8",
            "-vf", "format=yuv420p", str(out)
        ])
        log("No VIDEO_API_ENDPOINT configured, generated fallback base clip")
        return out

    payload = {
        "prompt": prompt,
        "duration_seconds": int(cfg.get("BASE_CLIP_SECONDS", "8")),
        "aspect_ratio": cfg.get("ASPECT_RATIO", "9:16"),
    }
    req = request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    key = cfg.get("VIDEO_API_KEY", "").strip()
    if key:
        req.add_header(cfg.get("VIDEO_API_KEY_HEADER", "Authorization"), f"Bearer {key}")

    with request.urlopen(req, timeout=180) as r:
        body = r.read().decode("utf-8", errors="ignore")
    resp = json.loads(body)
    url = find_url(resp)
    if not url:
        raise RuntimeError(f"No video URL in API response: {resp}")

    base = out_dir / "base.mp4"
    with request.urlopen(url, timeout=300) as r:
        base.write_bytes(r.read())
    return base


def local_synth_clip(cfg: Dict[str, str], prompt: str, out_dir: Path) -> Path:
    """
    Generates a local synthetic cinematic clip (no external API, no billing).
    Not full diffusion, but theme-tinted animated background for VO overlays.
    """
    duration = int(cfg.get("BASE_CLIP_SECONDS", "8"))
    prompt_l = prompt.lower()

    hue = 180
    if any(k in prompt_l for k in ["horror", "dark", "fear", "night", "death"]):
        hue = 300
    elif any(k in prompt_l for k in ["sad", "lonely", "melanch", "lost"]):
        hue = 210
    elif any(k in prompt_l for k in ["love", "warm", "hope", "sun", "family"]):
        hue = 35
    elif any(k in prompt_l for k in ["city", "neon", "future", "tech", "ai"]):
        hue = 250

    out = out_dir / "base.mp4"
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
        "-t", str(duration),
        "-vf", vf,
        str(out),
    ], timeout=300)
    return out


def generate_base_clip(cfg: Dict[str, str], prompt: str, out_dir: Path) -> Path:
    backend = cfg.get("VIDEO_BACKEND", "local_synth").strip().lower()
    if backend == "api":
        return call_video_api(cfg, prompt, out_dir)
    if backend == "local_synth":
        return local_synth_clip(cfg, prompt, out_dir)

    raise RuntimeError(f"Unsupported VIDEO_BACKEND={backend}. Use local_synth or api")


def ffprobe_duration(path: Path) -> float:
    out = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    return float(out.strip())


def loop_to_target(base: Path, dst: Path, target: int, min_s: int, max_s: int) -> None:
    d = ffprobe_duration(base)
    if d <= 0:
        raise RuntimeError("Base clip duration invalid")
    t = max(min(target, max_s), min_s)
    loops = max(0, math.ceil(t / d) - 1)
    run_cmd([
        "ffmpeg", "-y", "-stream_loop", str(loops), "-i", str(base),
        "-t", str(t), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)
    ], timeout=900)


def post_to_discord(cfg: Dict[str, str], video: Path, story_file: Path) -> None:
    webhook = cfg.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    run_cmd([
        "curl", "-sS", "-X", "POST", webhook,
        "-F", "content=GO received. Local video generated.",
        "-F", f"file1=@{video}",
        "-F", f"file2=@{story_file}",
    ], timeout=240)


def main() -> int:
    cfg = load_env(ENV_FILE)
    required = ["VM_USER", "VM_HOST", "DISCORD_CHANNEL_ID", "CRON_JOB_ID"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log(f"Missing required .env keys: {', '.join(missing)}")
        return 2

    state = load_state()
    last_reply_ts = float(state.get("last_processed_reply_ts", 0))
    vm_home = cfg.get("VM_OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))

    sessions_raw = ssh_cat(cfg, f"{vm_home}/agents/main/sessions/sessions.json")
    sessions = parse_json_object_from_text(sessions_raw)
    dkey = f"agent:main:discord:channel:{cfg['DISCORD_CHANNEL_ID']}"
    sentry = sessions.get(dkey)
    if not sentry:
        log(f"Discord session not found: {dkey}")
        return 1

    session_file = sentry.get("sessionFile")
    if not session_file:
        log("sessionFile missing in sessions.json")
        return 1

    session_jsonl = ssh_cat(cfg, session_file, tail=1000)
    reply = latest_reply_go_no(session_jsonl)
    if reply["ts"] <= last_reply_ts:
        log("No new GO/NO reply")
        return 0

    if reply["value"] == "NO":
        log("NO detected -> requesting new story on VM")
        cron_cmd = f"export PATH=$HOME/.npm-global/bin:$PATH; openclaw cron run {shlex.quote(cfg['CRON_JOB_ID'])}"
        ssh_cmd(cfg, f"bash -lc {shlex.quote(cron_cmd)}")
        state["last_processed_reply_ts"] = reply["ts"]
        state["last_action"] = "NO_rerun"
        save_state(state)
        log("Story rerun triggered")
        return 0

    if reply["value"] == "GO":
        log("GO detected -> generating local video")
        runs = ssh_cat(cfg, f"{vm_home}/cron/runs/{cfg['CRON_JOB_ID']}.jsonl", tail=300)
        story = latest_story_summary(runs)
        if not story:
            raise RuntimeError("No story summary found in runs jsonl")

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = OUT_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        prompt = extract_prompt(story)
        base = generate_base_clip(cfg, prompt, run_dir)
        final = run_dir / "final_looped.mp4"
        target = int(cfg.get("TARGET_SECONDS", "50"))
        min_s = int(cfg.get("MIN_SECONDS", "40"))
        max_s = int(cfg.get("MAX_SECONDS", "60"))
        loop_to_target(base, final, target, min_s, max_s)

        story_file = run_dir / "story.txt"
        prompt_file = run_dir / "prompt.txt"
        story_file.write_text(story, encoding="utf-8")
        prompt_file.write_text(prompt, encoding="utf-8")

        post_to_discord(cfg, final, story_file)

        state["last_processed_reply_ts"] = reply["ts"]
        state["last_action"] = "GO_video"
        state["last_output"] = str(final)
        save_state(state)
        log(f"Video generated: {final}")
        return 0

    log("No actionable reply found")
    return 0


if __name__ == "__main__":
    try:
        if "--test-local-video" in sys.argv:
            cfg = load_env(ENV_FILE)
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S_test")
            run_dir = OUT_DIR / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            prompt = "Cinematic emotional vertical story background, no text, moody lighting"
            base = generate_base_clip(cfg, prompt, run_dir)
            final = run_dir / "final_looped.mp4"
            target = int(cfg.get("TARGET_SECONDS", "50"))
            min_s = int(cfg.get("MIN_SECONDS", "40"))
            max_s = int(cfg.get("MAX_SECONDS", "60"))
            loop_to_target(base, final, target, min_s, max_s)
            log(f"Local test clip generated: {final}")
            sys.exit(0)
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
