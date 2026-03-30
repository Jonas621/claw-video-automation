# Claw Video Automation

Local TikTok short generation pipeline with OpenClaw on VM and video rendering on Mac.

## What The Pipeline Does

1. OpenClaw cron on VM generates a story package and posts it to Discord.
2. Reply `GO` in Discord:
   - first `GO` for that story -> VM bridge calls Mac `/generate`
   - preview clip is posted back to Discord
3. Reply `GO` again or `POST`:
   - VM bridge calls Mac `/publish`
   - publish runs TikTok endpoint (or dry run)
4. Reply `NO`:
   - VM bridge reruns the OpenClaw cron job for a new story
5. Optional in Discord:
  - `TREND` -> posts a numbered trend list to Discord (TikTok Creative Center hashtags + inspiration first, optional songs only if enabled, then Google fallback), then reruns after your selection (`1`, `2`, `3`, ... or `PICK:2`)
  - `THEME: <deine Idee>` or `IDEE: <deine Idee>` -> rerun with your custom idea as primary prompt override
   - Bridge injects anti-repeat constraints (recent titles + hard-ban motifs) and random seed words for diversity

## Architecture

### Mac (Docker)

The Mac runs two containers via `docker-compose.yml`:

| Container | Image | Port | Purpose |
|---|---|---|---|
| `mac_api` | `claw-video-automation-mac_api` | `127.0.0.1:8787` | HTTP API for generate/publish |
| `tunnel` | `alpine:3.19` | — | Reverse SSH tunnel to Pi (`-R 19090:127.0.0.1:8787`) |

ComfyUI runs **natively** on the Mac (needs direct MPS/GPU access) on port 8188.

```bash
# Start Mac services
cd ~/claw-video-automation
docker compose up -d

# ComfyUI (native, via launchd)
launchctl load ~/Library/LaunchAgents/com.jonas.comfyui.plist
```

> **Note:** `bin/install_*_launchd.sh` scripts exist as an alternative to Docker for running mac_api and tunnel natively via launchd. The production setup uses Docker.

### Raspberry Pi (Debian, systemd)

The Pi runs three systemd user services natively (no Docker):

| Service | Unit file | Purpose |
|---|---|---|
| OpenClaw Gateway | `openclaw-gateway.service` | Discord/OpenClaw gateway (port 18789) |
| VM Bridge EN | `claw-vm-bridge-en.service` | Watches EN Discord channel, calls Mac API |
| VM Bridge DE | `claw-vm-bridge-de.service` | Watches DE Discord channel, calls Mac API |

OpenClaw cron jobs generate story packages on schedule and post to Discord.

All Pi service files and configs are in the `pi/` directory.

## Mac Setup (from scratch)

### 1. Clone and configure

```bash
git clone https://github.com/Jonas621/claw-video-automation.git
cd claw-video-automation
cp config/mac_api.env.example config/mac_api.env
cp config/.env.example config/.env
```

Edit `config/mac_api.env` with your tokens, paths, and backend settings.
Edit `config/.env` with your Pi SSH credentials.

### 2. Start Docker services

```bash
docker compose up -d --build
```

This starts `mac_api` (port 8787) and the reverse SSH tunnel to the Pi.

### 3. Install ComfyUI (optional, for local rendering)

```bash
bash bin/setup_comfyui.sh
bash bin/install_comfyui_launchd.sh
```

Download required models (see "Required ComfyUI Models" section below).

### 4. Install Python dependencies (host, for edge-tts in Docker)

The Docker image includes edge-tts. For native/launchd usage:

```bash
pip3 install edge-tts Pillow
```

### 5. Verify

```bash
docker compose ps
curl -fsS -X POST http://127.0.0.1:8787/health \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: <your-token>" \
  -d '{}'
```

## Pi Setup (from scratch)

### 1. Prerequisites

Tested on Debian 13 (trixie) aarch64. Needs Node.js 22+ and Python 3.12+.

```bash
# Install Node.js 22
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash -
sudo apt install -y nodejs python3 python3-pip git

# Set up npm global dir (no sudo for global installs)
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
echo 'export PATH=$HOME/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

### 2. Install OpenClaw

```bash
npm install -g openclaw
openclaw onboard
```

Follow the onboard wizard. Set up Discord channel integration when prompted.

### 3. Clone repo and install services

```bash
git clone https://github.com/Jonas621/claw-video-automation.git
cd claw-video-automation
bash pi/install_pi_services.sh
```

This copies `vm_bridge.py`, loop scripts, and systemd service files.

### 4. Configure bridge environments

Edit `~/.openclaw/vm_bridge_en.env` and `~/.openclaw/vm_bridge_de.env`:

- Set `DISCORD_CHANNEL_ID` to the correct Discord channel
- Set `MAC_API_TOKEN` to match the token in `config/mac_api.env` on Mac
- Set `CRON_JOB_ID` / `CRON_JOB_IDS` after creating cron jobs (step 5)

See `pi/vm_bridge_en.env.example` and `pi/vm_bridge_de.env.example` for all options.

### 5. Create OpenClaw cron jobs

```bash
export PATH=$HOME/.npm-global/bin:$PATH

# English story — daily at 13:00 CET → #general channel
openclaw cron create \
  --name "Daily TikTok story EN 13:00" \
  --schedule "0 13 * * *" \
  --tz "Europe/Berlin" \
  --channel "discord" \
  --to "channel:<EN_DISCORD_CHANNEL_ID>" \
  --message "Create exactly ONE fresh TikTok-ready story package in ENGLISH..."

# English story — daily at 19:30 CET
openclaw cron create \
  --name "Daily TikTok story EN 19:30" \
  --schedule "30 19 * * *" \
  --tz "Europe/Berlin" \
  --channel "discord" \
  --to "channel:<EN_DISCORD_CHANNEL_ID>" \
  --message "Create exactly ONE fresh TikTok-ready story package in ENGLISH..."

# German story — daily at 13:45 CET → #de channel
openclaw cron create \
  --name "Daily TikTok story DE 13:00" \
  --schedule "45 13 * * *" \
  --tz "Europe/Berlin" \
  --channel "discord" \
  --to "channel:<DE_DISCORD_CHANNEL_ID>" \
  --message "Create exactly ONE fresh TikTok-ready story package in GERMAN..."

# German story — daily at 20:15 CET
openclaw cron create \
  --name "Daily TikTok story DE 20:15" \
  --schedule "15 20 * * *" \
  --tz "Europe/Berlin" \
  --channel "discord" \
  --to "channel:<DE_DISCORD_CHANNEL_ID>" \
  --message "Create exactly ONE fresh TikTok-ready story package in GERMAN..."
```

Copy the job IDs from the output into the bridge env files (`CRON_JOB_ID` / `CRON_JOB_IDS`).

Verify: `openclaw cron list --json`

### 6. Set gateway token

Edit `~/.config/systemd/user/openclaw-gateway.service` and set `OPENCLAW_GATEWAY_TOKEN` to the token from `openclaw onboard`.

### 7. Start services and enable linger

```bash
# Enable linger so services survive logout
sudo loginctl enable-linger $USER

# Start everything
systemctl --user start openclaw-gateway
systemctl --user start claw-vm-bridge-en
systemctl --user start claw-vm-bridge-de

# Verify
systemctl --user status openclaw-gateway claw-vm-bridge-en claw-vm-bridge-de
```

### 8. Set up SSH tunnel (Mac → Pi)

The Mac tunnel container connects to the Pi and forwards port 19090 → Mac 8787.
Make sure the Pi allows SSH and the credentials in `config/.env` on the Mac are correct.

Verify tunnel from Pi: `ss -ltn | grep ':19090'`

## Critical Config Files

- Mac API config: `config/mac_api.env`
- VM bridge config: `~/.openclaw/vm_bridge.env`
  - Optional multi-job mode: `CRON_JOB_IDS=<id1>,<id2>` (latest story across jobs)

Do not commit real tokens/webhook URLs/passwords.

## Required ComfyUI Models (Wan 2.2 5B)

The current workflow expects these files:

- `~/ComfyUI/models/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors`
- `~/ComfyUI/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `~/ComfyUI/models/vae/wan2.2_vae.safetensors`

Workflow file: `comfy/workflow_wan22_t2v_5b_api.json`

Important env keys in `config/mac_api.env` (native paths, not Docker):

- `VIDEO_BACKEND=comfyui`
- `COMFYUI_API_URL=http://127.0.0.1:8188` (native, direct localhost)
- `COMFYUI_WORKFLOW_FILE=/Users/YOUR_USERNAME/claw-video-automation/comfy/workflow_wan22_t2v_5b_api.json`
- `COMFYUI_OUTPUT_DIR=/Users/YOUR_USERNAME/ComfyUI/output`

## Service Management

### Mac: Docker services (mac_api + tunnel)

```bash
cd ~/claw-video-automation

# Start / restart
docker compose up -d

# Restart after code or config change
docker compose up -d --build

# Stop
docker compose down

# View live log
docker compose logs -f mac_api
docker compose logs -f tunnel
```

### Mac: ComfyUI (launchd, native)

ComfyUI runs natively for direct MPS/GPU access.

```bash
# Restart ComfyUI
launchctl kickstart -k "gui/$(id -u)/com.jonas.comfyui"

# Install/reinstall launchd service
bash bin/install_comfyui_launchd.sh
```

### Pi: systemd services

```bash
# Restart all
systemctl --user restart openclaw-gateway claw-vm-bridge-en claw-vm-bridge-de

# View logs
journalctl --user -u openclaw-gateway.service -f
journalctl --user -u claw-vm-bridge-en.service -f
journalctl --user -u claw-vm-bridge-de.service -f
tail -f ~/.openclaw/logs/vm_bridge_en.log
tail -f ~/.openclaw/logs/vm_bridge_de.log
```

Recommended in `~/.openclaw/vm_bridge.env` for long ComfyUI renders:

```bash
MAC_API_GENERATE_TIMEOUT_SEC=5400
MAC_API_PUBLISH_TIMEOUT_SEC=300
MAC_API_STATUS_TIMEOUT_SEC=20
MAC_API_RETRIES=2
MAC_API_RETRY_BACKOFF_SEC=4
TREND_WEB_RESEARCH_ENABLED=true
TREND_WEB_RESEARCH_MAX_SIGNALS=6
TREND_WEB_RESEARCH_MAX_TITLES_PER_SOURCE=3
TREND_WEB_RESEARCH_TIMEOUT_SEC=12
TREND_WEB_RESEARCH_GEO=US
TREND_WEB_RESEARCH_USE_GOOGLE_NEWS=true
TREND_WEB_RESEARCH_USE_DUCKDUCKGO=true
CONTENT_MODE_ROTATION=Story Drama,Fact Explainer,Current-News Brief,Mega-Build/Engineering Showcase,Myth-busting
CONTENT_MODE_AVOID_REPEAT_WINDOW=2
```

`vm_bridge.py` persists each incoming command immediately (at-most-once semantics).
If `/generate` fails or the bridge restarts mid-run, the same `GO` message is not replayed automatically.
Send a fresh `GO` to trigger a new run.

## Health Checks (Copy/Paste)

### Mac checks

```bash
# Check Docker services
docker compose ps

# Check ComfyUI (native)
launchctl list | grep com.jonas.comfyui

# Check ports
lsof -iTCP -sTCP:LISTEN -nP | grep -E '127.0.0.1:(8188|8787)'
```

Mac API health:

```bash
TOKEN=$(grep '^MAC_API_TOKEN=' ~/claw-video-automation/config/mac_api.env | cut -d= -f2-)
curl -fsS -X POST http://127.0.0.1:8787/health \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: $TOKEN" \
  -d '{}'
```

Comfy queue:

```bash
curl -sS http://127.0.0.1:8188/queue
```

Status endpoint:

```bash
TOKEN=$(grep '^MAC_API_TOKEN=' ~/claw-video-automation/config/mac_api.env | cut -d= -f2-)
curl -fsS -X POST http://127.0.0.1:8787/status \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: $TOKEN" \
  -d '{"notify":false,"lang":"en"}'
```

### Background Music (BGM)
- Samples: `~/Music/bgm_samples`
- Tags (im Dateinamen oder Unterordner): `chill`, `tense`, `uplift`, `beat`, `ambient`
- Relevante env Keys (`config/mac_api.env`):
  - `BGM_SAMPLE_DIR=~/Music/bgm_samples` (nativer Pfad)
  - `BGM_SAMPLE_PATTERN=*.wav,*.mp3`
  - `BGM_TARGET_LUFS=-14`
  - `BGM_LEVEL_DB=-3`
  - `BGM_DUCKING_ENABLED=false`
  - `BGM_ALLOW_SYNTH=false` (kein 1-Ton-Fallback)

### Pi checks

```bash
ssh jonas@<PI_IP>
systemctl --user is-active openclaw-gateway.service
systemctl --user is-active claw-vm-bridge-en.service
systemctl --user is-active claw-vm-bridge-de.service
systemctl --user --no-pager status openclaw-gateway.service | head -n 20
systemctl --user --no-pager status claw-vm-bridge-en.service | head -n 20
systemctl --user --no-pager status claw-vm-bridge-de.service | head -n 20
```

OpenClaw is in `~/.npm-global/bin`, so use:

```bash
export PATH=$HOME/.npm-global/bin:$PATH
openclaw cron list --json
tail -n 10 ~/.openclaw/cron/runs/<CRON_JOB_ID>.jsonl
```

Confirm reverse tunnel is listening on VM:

```bash
ss -ltn | grep ':19090'
```

VM -> Mac API through tunnel:

```bash
source ~/.openclaw/vm_bridge.env
curl -fsS -X POST "$MAC_API_URL/health" \
  -H "Content-Type: application/json" \
  -H "X-Api-Token: $MAC_API_TOKEN" \
  -d '{}'
```

## Logs

### Mac logs

- `logs/mac_api.out.log` — structured app log (most useful; stdout from mac_api)
- `logs/mac_api.err.log` — Python stderr from mac_api
- `logs/comfyui.err.log` — all ComfyUI output (stdout + stderr, including tqdm progress)
- `logs/tunnel.out.log` / `logs/tunnel.err.log` — SSH tunnel logs

Note: `logs/comfyui.out.log` is always empty — ComfyUI writes everything to stderr.

Tail quickly:

```bash
tail -f logs/mac_api.out.log
tail -f logs/comfyui.err.log
```

### Pi logs

```bash
journalctl --user -u openclaw-gateway.service -f
journalctl --user -u claw-vm-bridge-en.service -f
journalctl --user -u claw-vm-bridge-de.service -f
tail -f ~/.openclaw/logs/vm_bridge_en.log
tail -f ~/.openclaw/logs/vm_bridge_de.log
```

## Output File Structure

Each story uses one canonical folder: `output/<story_id>/`

Per-story files:

- `story.txt`: full story package input.
- `prompt.txt`: final visual prompt.
- `scene_prompts.txt`: shot-level prompt variants used.
- `base_01.mp4`, `base_02.mp4`, ...: base shot clips.
- `base.mp4`: concatenated base clip (if multiple variants).
- `final_looped.mp4`: looped visual base to target duration.
- `voiceover.wav`: generated VO audio.
- `final_text.mp4`: final publish/preview asset (VO + captions + beats).
- `failed_preview.webm`: last preserved ComfyUI preview when a run fails.

If `OUTPUT_PRUNE_INTERMEDIATES=true`, intermediate files are deleted after successful generate.

Main file to upload: `output/<story_id>/final_text.mp4`

## Key Runtime Behavior

- `BASE_CLIP_SECONDS=8`, `BASE_CLIP_VARIANTS=2`: two 8s base clips generated per story.
- `BASE_VARIANT_RETRIES=1`: retries only the failing variant on failure.
- `COMFYUI_STEPS=10`, `COMFYUI_MAX_FRAMES=84`, `COMFYUI_FPS=12`: 7s clips at 10 sampling steps.
- `VOICEOVER_ENABLED=true`: adds voiceover from story "Voiceover Script".
- `VOICEOVER_BACKEND=edge_tts`: neural voice (free, internet required).
- `VOICEOVER_CAPTIONS_ENABLED=true`: burns timed captions.
- `TEXT_BEATS_ENABLED=true`: burns on-screen beat cards.
- `PROMPT_STYLE_HINT=...`: nudges visuals toward stylized anime/cinematic look.
- `PROMPT_STABILITY_ENABLED=true`: adds anti-morph identity constraints.
- `PROMPT_AUTO_SHOT_WRAPPER_ENABLED=true`: auto-wraps prompts with camera recipe + continuity.
- `OUTPUT_CLEANUP_ENABLED=true`, `OUTPUT_RETENTION_DAYS=7`: auto-deletes old output.
- `COMFYUI_PREVIEW_CLEANUP_ENABLED=true`, `COMFYUI_PREVIEW_RETENTION_DAYS=2`: cleans old previews.
- `TIKTOK_DRY_RUN=true`: publish is simulated only.

### RunPod Serverless Backend (VIDEO_BACKEND=runpod)

Alternative to local ComfyUI: renders clips on RunPod cloud GPUs (RTX 4090, 24GB VRAM).
Massive quality upgrade: 14B model, 25 steps, 720×1280 instead of local 5B/10 steps/512×896.

**RunPod Account:** runpod.io (jonas, $10 balance loaded 2026-03-23)

**Network Volume:** `comfyui_wan` in **EU-RO-1**, 40GB ($2.80/Monat)

**Models on Volume** (`/runpod-volume/ComfyUI/models/`):
- `diffusion_models/wan2.2_t2v_14B_fp8.safetensors` (~14GB, Wan 2.2 14B fp8)
- `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors` (~5GB)
- `vae/wan_2.2_vae.safetensors` (~0.5GB)

**Serverless Endpoint:** NOT YET CREATED — waiting for 24GB GPU availability in EU-RO-1.
When available: Serverless → New Endpoint → ComfyUI (Network Volume) → select `comfyui_wan` volume → 24GB GPU → Min Workers 0 / Max Workers 1.

**Config keys in `config/mac_api.env`:**
- `VIDEO_BACKEND=runpod` (switch back to `comfyui` for local rendering)
- `RUNPOD_API_KEY=` (create under RunPod Settings → API Keys)
- `RUNPOD_ENDPOINT_ID=` (shown on endpoint page after creation)
- `RUNPOD_WORKFLOW_FILE=/Users/YOUR_USERNAME/claw-video-automation/comfy/workflow_wan22_t2v_5b_api.json`
- `RUNPOD_DIFFUSION_MODEL=wan2.2_t2v_14B_fp8.safetensors`
- `RUNPOD_TEXT_ENCODER=umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `RUNPOD_VAE_NAME=wan_2.2_vae.safetensors`
- `RUNPOD_WIDTH=720`, `RUNPOD_HEIGHT=1280` (720p portrait)
- `RUNPOD_FPS=24`, `RUNPOD_MAX_FRAMES=192`
- `RUNPOD_STEPS=25`, `RUNPOD_CFG=6.5`
- `RUNPOD_SAMPLER_NAME=euler`, `RUNPOD_SCHEDULER=normal`
- `RUNPOD_TIMEOUT_SEC=600`, `RUNPOD_POLL_SEC=5`

**Code:** `runpod_base_clip()` in `bin/mac_api.py` — sends workflow to RunPod Serverless API, polls for completion, downloads result video.

**To activate (once endpoint is created):**
1. Fill in `RUNPOD_API_KEY` and `RUNPOD_ENDPOINT_ID` in `config/mac_api.env`
2. Ensure `VIDEO_BACKEND=runpod`
3. Restart mac_api: `launchctl kickstart -k "gui/$(id -u)/com.jonas.claw-mac-api"`

**To switch back to local:** Set `VIDEO_BACKEND=comfyui` and restart mac_api.

**Cost estimate:** ~$0.03/day for 2 clips, ~$1/month + $2.80/month storage = ~$3.80/month total.

### ComfyUI Watchdogs

The mac_api has two watchdogs that abort hanging ComfyUI runs:

**Step watchdog** (`COMFYUI_STEP_WATCHDOG_*`): aborts if a single sampling step takes too long.
- `COMFYUI_STEP_WATCHDOG_MAX_SEC_PER_IT=600`: abort if any step exceeds this.
- `COMFYUI_STEP_WATCHDOG_MIN_ELAPSED_SEC=240`: only active after initial warmup.

**Stall watchdog** (`COMFYUI_STALL_WATCHDOG_*`): aborts if ComfyUI shows no progress for a long window.
- `COMFYUI_STALL_WATCHDOG_IDLE_SEC=600`: **must be >= 300**. The VAE decode phase after sampling (~200s) produces no output to the log. If this is set too low (e.g. 240), the watchdog fires during VAE and kills a completed run.
- `COMFYUI_STALL_WATCHDOG_MIN_ELAPSED_SEC=600`: only starts checking after this many seconds.

Important: the stall watchdog attempts to restart ComfyUI via `launchctl` but this may fail silently if run in a restricted context. ComfyUI continues running anyway, and the next attempt reuses the cached models. This is expected behavior.

### Realistic Voice Options

- `VOICEOVER_BACKEND=edge_tts`: neural voice (internet required).
- Install: `pip3 install edge-tts` (installs to `~/Library/Python/3.12/bin/edge-tts`)
- Binary path must be set in `config/mac_api.env`:
  ```
  VOICEOVER_EDGE_TTS_BIN=/Users/YOUR_USERNAME/Library/Python/3.12/bin/edge-tts
  ```

```bash
VOICEOVER_BACKEND=edge_tts
VOICEOVER_EDGE_VOICE=en-US-AvaMultilingualNeural
```

Voice and gender auto-selection (`VOICEOVER_LANGUAGE_MODE=auto`, `VOICEOVER_GENDER_MODE=auto`) picks the right voice from `VOICEOVER_EDGE_ALLOWED_CHOICES` based on the story language and "Voice choice" field.

## Text Overlays (Captions + Text Beats)

Text overlays are burned into the video using Pillow (PIL) + ffmpeg. Pillow must be installed for `/usr/bin/python3`:

```bash
/usr/bin/python3 -m pip install Pillow
```

### Fonts

Both overlays use **Arial Black** (macOS system font):

```
VOICEOVER_CAPTIONS_FONT_FILE=/System/Library/Fonts/Supplemental/Arial Black.ttf
TEXT_BEATS_FONT_FILE=/System/Library/Fonts/Supplemental/Arial Black.ttf
```

### Layout (512×896 video)

| Layer | Position | Size | Config key |
|---|---|---|---|
| Text beats (top) | `y_frac=0.22` | 58pt | `TEXT_BEATS_Y_FRAC`, `TEXT_BEATS_FONT_SIZE` |
| Subtitles (lower) | `y_frac=0.60` | 38pt | `VOICEOVER_CAPTIONS_Y_FRAC`, `VOICEOVER_CAPTIONS_FONT_SIZE` |

`y_frac` is the center of the card as a fraction of the video height. `0.22` = 22% from top = text beats sit in the upper area, clear of TikTok UI. `0.60` = 60% = subtitles in the lower-center area, above the TikTok bottom UI.

### Stroke clipping fix

`multiline_textbbox()` in PIL does **not** include `stroke_width` in its measurements by default. Without the fix, the bottom stroke of the last line gets clipped at the edge of the card PNG.

The fix in `_render_text_overlay_png()`:
- Pass `stroke_width` to `multiline_textbbox()` so the bbox measurement includes the stroke
- Add `stroke_width` to `pad_x` and `pad_y` to give enough margin around the text

## Error Recovery

### Network errors during ComfyUI generation

If a network error occurs (ENETUNREACH / errno 101 / connection refused) after ComfyUI has already rendered clips, the pipeline automatically recovers:

1. Scans `COMFYUI_OUTPUT_DIR` for `.webm` preview files newer than the job start time
2. Converts the found preview to `.mp4` via ffmpeg
3. Continues the rest of the pipeline (voiceover, BGM, overlays) normally

Config relevant: `COMFYUI_OUTPUT_DIR=/Users/YOUR_USERNAME/ComfyUI/output`

### Discord error notifications

On any `/generate` failure, the pipeline sends a summary to the Discord preview webhook with:
- Error category (network unreachable / ComfyUI not running / stall watchdog / timeout / unknown)
- Story ID and exception message

## Troubleshooting

### Alle Clips schlagen fehl (ComfyUI stall watchdog)

Symptom in `logs/mac_api.log`:
```
Base variant X/Y failed: ComfyUI stall watchdog aborted ... (no progress for 241s; limit=240s)
```

Ursache: `COMFYUI_STALL_WATCHDOG_IDLE_SEC` ist zu niedrig. Der VAE-Decode nach dem Sampling (~200s) schreibt nichts in den Log. Der Watchdog interpretiert das als Stillstand und bricht ab.

Fix: In `config/mac_api.env` setzen:
```
COMFYUI_STALL_WATCHDOG_IDLE_SEC=600
```
Dann `launchctl kickstart -k "gui/$(id -u)/com.jonas.claw-mac-api"`.

### `GO`/`NO` in Discord does nothing

- Check VM bridge: `journalctl --user -u claw-vm-bridge.service -n 100 --no-pager`
- Check gateway: `journalctl --user -u openclaw-gateway.service -n 100 --no-pager`
- If DNS errors (`EAI_AGAIN`) or reconnect loops: VM network/DNS issue, restart gateway and bridge.

### `Connection refused` from VM bridge to Mac API

- Tunnel or mac_api not running.
- On Mac: `launchctl list | grep com.jonas` — check all three services are running.
- On VM: `ss -ltn | grep ':19090'` (tunnel must be listening).

### `unauthorized` from `/generate` or `/publish`

`MAC_API_TOKEN` mismatch between `config/mac_api.env` (Mac) and `~/.openclaw/vm_bridge.env` (VM).

### ComfyUI queue stuck or not responding

```bash
# Restart ComfyUI
launchctl kickstart -k "gui/$(id -u)/com.jonas.comfyui"

# Check ComfyUI is up
curl -sS http://127.0.0.1:8188/queue
```

### Discord preview has no video (only text)

- Check `logs/mac_api.log` for `413 Request entity too large`.
- mac_api auto-compresses a smaller preview MP4 for Discord (`DISCORD_PREVIEW_MAX_MB=7.8`).

### ComfyUI preview cleanup fails

If cleanup logs an error for `~/ComfyUI/output/`, check directory permissions:
```bash
ls -la ~/ComfyUI/output/
```
Currently harmless — previews accumulate but don't affect generation. Auto-cleanup runs based on `COMFYUI_PREVIEW_RETENTION_DAYS=2`.

## Cleanup Commands

Delete old generated runs while keeping newest 6:

```bash
cd ~/claw-video-automation/output
ls -1dt */ | tail -n +7 | xargs -I{} rm -rf "{}"
```

Delete old ComfyUI preview renders:

```bash
find ~/ComfyUI/output -type f -name 'claw_preview*' -mtime +2 -delete
```

Check disk usage:

```bash
du -sh ~/claw-video-automation/output ~/ComfyUI/models ~/ComfyUI/output
```
