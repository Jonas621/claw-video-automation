#!/usr/bin/env bash
set -euo pipefail

COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"
OUTPUT_DIR="$HOME/ComfyUI/output"

# ============================================================================
# STYLE PRESETS
# Each style sets: visual tone, lighting, camera, atmosphere, quality anchors
# Your prompt adds: subject, action, clothing, setting, emotion
# ============================================================================

STYLE_CINEMATIC="cinematic vertical video, 9:16 aspect ratio, \
35mm film grain, anamorphic lens flare, shallow depth of field with bokeh, \
dramatic three-point lighting with warm key and cool fill, \
professional color grading with rich shadows and lifted blacks, \
smooth controlled camera movement, dolly tracking, \
intimate close framing, sensual atmosphere, \
photorealistic skin texture and hair detail, natural body proportions, \
high production value, 8K source downscaled"

STYLE_EDITORIAL="high-fashion editorial video, vertical 9:16, \
studio environment with seamless backdrop, \
strong rim lighting and butterfly key light, \
sharp focus on face and body with clean separation from background, \
professional model poses with confident body language, \
visible skin texture and pore detail, natural skin tone, \
bold contrast between light and shadow, \
magazine cover quality, Vogue aesthetic, \
smooth slow motion movement, elegant transitions"

STYLE_NOIR="neo-noir cinematic vertical video, 9:16, \
extreme chiaroscuro lighting with deep blacks and selective highlights, \
neon color accent reflections on wet surfaces and skin, \
venetian blind shadow patterns, smoke or haze in the air, \
handheld slight movement for tension, tight claustrophobic framing, \
desaturated base with isolated color pops in red or blue, \
gritty film grain, dark sensual atmosphere, \
realistic skin in dramatic light, sweat and texture visible"

STYLE_ARTISTIC="fine art cinematic video portrait, 9:16, \
renaissance Rembrandt lighting with single warm source, \
painterly soft focus with sharp subject and dreamy background, \
classical composition following golden ratio, \
warm amber and golden tones with deep umber shadows, \
expressive body language and emotion in face and hands, \
skin rendered like oil painting with visible warmth and glow, \
slow graceful movement, floating fabric, candle flicker, \
museum gallery quality, timeless and elegant"

STYLE_BEDROOM="intimate boudoir cinematic video, 9:16, \
soft diffused window light mixed with warm practical lamps, \
luxurious interior setting with silk sheets and soft textures, \
shallow depth of field with creamy bokeh background, \
warm skin tones with natural highlights and soft shadows, \
relaxed natural poses, genuine emotion and eye contact, \
slow subtle movement, breathing, hair falling, fabric shifting, \
tasteful sensual framing, partial silhouette, suggestion over exposure, \
high-end photography aesthetic, analog warmth"

STYLE_POOL="summer lifestyle cinematic video, 9:16, \
golden hour sunlight with lens flare and warm highlights, \
outdoor pool or beach setting with turquoise water reflections, \
water droplets and wet skin catching sunlight, \
vibrant saturated color palette with teal and orange tones, \
natural relaxed movement, hair flip, water splash, stretching, \
bright airy atmosphere with heat haze, \
crisp detail on skin and swimwear texture, \
vacation luxury aesthetic, natural beauty"

STYLE_DANCE="performance cinematic video, 9:16, \
dynamic stage lighting with colored spotlights and haze, \
fluid expressive body movement captured in slow motion, \
dramatic shadows revealing and concealing form, \
tight choreographed camera following the dancer, \
high contrast between lit body and dark background, \
visible muscle definition and movement dynamics, \
sweat and exertion adding realism, fabric in motion, \
music video production quality"

# --- Negative prompt (quality control, no content blocking) ---
NEG_PROMPT="text, watermark, logo, subtitle, UI overlay, \
blur, out of focus subject, low quality, jpeg artifacts, noise, \
ugly, deformed face, asymmetric eyes, extra limbs, missing fingers, \
distorted hands, broken anatomy, extra digits, fused limbs, \
cartoon, anime, 3d render, clay, plastic skin, \
censorship bars, mosaic, pixelation, black bars, \
static frame, frozen pose, mannequin, wax figure"

# --- Defaults ---
STYLE="$STYLE_CINEMATIC"
STYLE_NAME="cinematic"
STEPS=10
SEED=$RANDOM
WIDTH=512
HEIGHT=896
FRAMES=84
FPS=12
PREFIX="test_gen"
CFG=5.0
MODEL="wan2.2_ti2v_5B_fp16.safetensors"

usage() {
    cat << 'EOF'
Usage: generate_test.sh [OPTIONS] "your scene description"

You describe WHAT happens — the style wrapper handles HOW it looks.

STYLES:
  -s cinematic    Film look, dramatic lighting, intimate (default)
  -s editorial    Fashion/magazine, studio, sharp
  -s noir         Dark, neon, shadows, gritty
  -s artistic     Painterly, renaissance, golden warmth
  -s bedroom      Boudoir, soft light, silk, intimate
  -s pool         Summer, golden hour, wet skin, vibrant
  -s dance        Performance, stage lighting, movement
  -s none         No wrapper — full manual control

OPTIONS:
  -n NAME         Output filename prefix (default: test_gen)
  -S STEPS        Sampling steps, more=better (default: 10, try 15-20)
  -seed NUM       Fixed seed for reproducibility
  -cfg NUM        CFG scale (default: 5.0, higher=closer to prompt)
  --landscape     896x512 landscape instead of 9:16 portrait
  --model NAME    Model safetensors filename

PROMPT TIPS:
  Be specific about: subject, clothing, pose, action, setting, emotion
  Describe movement: "slowly turns", "wind blows hair", "walks toward camera"
  Include details:   skin, fabric texture, lighting interaction, eye direction

EXAMPLES:
  ./generate_test.sh "young woman with long dark hair in black lace dress, \
    sitting on windowsill, legs crossed, looking out at rainy city, \
    one hand touching the glass, melancholic expression"

  ./generate_test.sh -s noir "woman in wet trench coat under neon sign, \
    slowly removing coat revealing bare shoulders, rain dripping, \
    looking directly at camera with intense eyes"

  ./generate_test.sh -s bedroom "woman lying on her side on white sheets, \
    wearing oversized white shirt unbuttoned, morning light on face, \
    stretching lazily, eyes half closed, peaceful smile"

  ./generate_test.sh -s pool "woman in red bikini at pool edge, \
    slowly emerging from water, pushing wet hair back, \
    water streaming down body, golden sunset behind her"

  ./generate_test.sh -s editorial -S 15 "model in sheer black bodysuit, \
    standing against white wall, one arm raised above head, \
    sharp shadow cast on wall, confident powerful stance"
EOF
    exit 1
}

# --- Parse args ---
PROMPT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -s)
            STYLE_NAME="$2"
            case "$2" in
                cinematic)  STYLE="$STYLE_CINEMATIC" ;;
                editorial)  STYLE="$STYLE_EDITORIAL" ;;
                noir)       STYLE="$STYLE_NOIR" ;;
                artistic)   STYLE="$STYLE_ARTISTIC" ;;
                bedroom)    STYLE="$STYLE_BEDROOM" ;;
                pool)       STYLE="$STYLE_POOL" ;;
                dance)      STYLE="$STYLE_DANCE" ;;
                none)       STYLE="" ;;
                *) echo "Unknown style: $2"; usage ;;
            esac
            shift 2 ;;
        -n)         PREFIX="$2"; shift 2 ;;
        -S)         STEPS="$2"; shift 2 ;;
        -seed)      SEED="$2"; shift 2 ;;
        -cfg)       CFG="$2"; shift 2 ;;
        --landscape) WIDTH=896; HEIGHT=512; shift ;;
        --model)    MODEL="$2"; shift 2 ;;
        -h|--help)  usage ;;
        *)
            if [[ -z "$PROMPT" ]]; then
                PROMPT="$1"
            else
                PROMPT="$PROMPT $1"
            fi
            shift ;;
    esac
done

if [[ -z "$PROMPT" ]]; then
    usage
fi

# --- Build final prompt ---
if [[ -n "$STYLE" ]]; then
    FULL_PROMPT="${STYLE}. Scene: ${PROMPT}. single consistent subject throughout, no morphing, coherent anatomy, natural proportions"
else
    FULL_PROMPT="$PROMPT"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Style:   $STYLE_NAME"
echo "Scene:   $PROMPT"
echo "Config:  ${WIDTH}x${HEIGHT}, ${FRAMES}f@${FPS}fps, ${STEPS} steps, cfg=$CFG, seed=$SEED"
echo "Model:   $MODEL"
echo "Output:  ${OUTPUT_DIR}/${PREFIX}_*.webm"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# --- Build JSON payload via Python for safe escaping ---
PAYLOAD=$(python3 -c "
import json, sys
prompt_text = sys.argv[1]
neg_text = sys.argv[2]
payload = {
    'prompt': {
        '1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': '$MODEL', 'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader', 'inputs': {'clip_name': 'umt5_xxl_fp8_e4m3fn_scaled.safetensors', 'type': 'wan', 'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'wan2.2_vae.safetensors'}},
        '4': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt_text, 'clip': ['2', 0]}},
        '5': {'class_type': 'CLIPTextEncode', 'inputs': {'text': neg_text, 'clip': ['2', 0]}},
        '6': {'class_type': 'Wan22ImageToVideoLatent', 'inputs': {'vae': ['3', 0], 'width': $WIDTH, 'height': $HEIGHT, 'length': $FRAMES, 'batch_size': 1}},
        '7': {'class_type': 'ModelSamplingSD3', 'inputs': {'model': ['1', 0], 'shift': 8.0}},
        '8': {'class_type': 'KSampler', 'inputs': {'model': ['7', 0], 'seed': $SEED, 'steps': $STEPS, 'cfg': $CFG, 'sampler_name': 'uni_pc', 'scheduler': 'simple', 'positive': ['4', 0], 'negative': ['5', 0], 'latent_image': ['6', 0], 'denoise': 1.0}},
        '9': {'class_type': 'VAEDecode', 'inputs': {'samples': ['8', 0], 'vae': ['3', 0]}},
        '10': {'class_type': 'SaveWEBM', 'inputs': {'images': ['9', 0], 'filename_prefix': '$PREFIX', 'codec': 'vp9', 'fps': float($FPS), 'crf': 32.0}}
    }
}
print(json.dumps(payload))
" "$FULL_PROMPT" "$NEG_PROMPT")

# --- Send to ComfyUI ---
RESPONSE=$(curl -s -X POST "$COMFYUI_URL/prompt" -H "Content-Type: application/json" -d "$PAYLOAD")

PROMPT_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('prompt_id',''))" 2>/dev/null || true)

if [[ -n "$PROMPT_ID" ]]; then
    echo "Queued! prompt_id=$PROMPT_ID"
    echo ""
    echo "Commands:"
    echo "  Status:     curl -s $COMFYUI_URL/queue | python3 -c \"import sys,json; d=json.load(sys.stdin); print(f'Running: {len(d[\\\"queue_running\\\"])}, Pending: {len(d[\\\"queue_pending\\\"])}')\""
    echo "  Abbrechen:  curl -s -X POST $COMFYUI_URL/interrupt"
    echo "  Öffnen:     open ${OUTPUT_DIR}/${PREFIX}_*.webm"
else
    echo "ERROR: $RESPONSE"
    exit 1
fi
