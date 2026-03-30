# Text-to-Video AI Models Research for Mac Apple Silicon + ComfyUI

**Date:** 2026-03-11
**Context:** Currently using Wan 2.2 5B through ComfyUI on Mac. Looking for less restricted models for storytelling/drama content (true crime, violence, mature themes).

---

## Executive Summary

For your Mac + ComfyUI setup focused on less-restricted content generation:

1. **Best current option: Wan 2.2 (what you already have)** -- It is already one of the least restricted open-source video models. The "Remix" variant and community builds strip remaining filters. The 5B model runs on Mac with GGUF quantization.

2. **Best alternative: HunyuanVideo 1.5** -- Community "cosy" builds remove all safety filters. Has an MLX port specifically for Apple Silicon. Quality rivals Wan 2.2 but generation is very slow on Mac.

3. **Most promising newcomer: LTX-2.3 (released March 2026)** -- Open source, permissive license, 4K output, native audio. Has a native macOS app optimized for Apple Silicon. Content restrictions are minimal in the open-source weights.

4. **Best for low VRAM / fast iteration: AnimateDiff** -- Runs on 8GB VRAM, fully compatible with Apple Silicon. Uses SD checkpoints so you can use any uncensored SD model. Lower quality than dedicated video models but very flexible.

---

## Detailed Model Comparison

### 1. Wan 2.2 (Current Setup - Baseline)

| Attribute | Details |
|---|---|
| **Developer** | Alibaba (Wan-Video) |
| **Architecture** | Mixture-of-Experts (MoE) diffusion |
| **Sizes** | 5B (T2V/I2V), 14B (T2V/I2V) |
| **Resolution** | 720p @ 24fps |
| **VRAM/Memory** | 5B: ~8GB minimum (GGUF), 14B: ~12GB+ (GGUF) |
| **ComfyUI Support** | Native official support + Kijai community nodes |
| **Apple Silicon** | Works via MPS; GGUF quantization recommended; M2 Max 64GB+ ideal |
| **Content Restrictions** | Minimal built-in safeguards; open-source weights have no enforced filters |
| **Uncensored Versions** | Wan2.2-Remix (no LoRA needed for mature content); community builds strip any remaining checks |

**Key Notes:**
- Already one of the least restricted open-source video models
- The Wan2.2 Remix variant is specifically designed for unrestricted generation without additional LoRA
- MoE architecture: high-noise and low-noise expert models divided by denoising timestep
- GGUF versions work well on Apple Silicon with ComfyUI
- Known issue: default workflow template may need adjustment for Apple Silicon
- Wan 2.5 adds audio sync, 10s videos, 1080p; Wan 2.6 adds reference-to-video and lip-sync

**Practical for your use case:** YES - you already have this. The Remix variant or simply running without safety-checker nodes gives full creative freedom. For true crime / drama storytelling, Wan 2.2 5B is already capable of generating violence, crime scenes, and mature themes.

---

### 2. HunyuanVideo 1.5

| Attribute | Details |
|---|---|
| **Developer** | Tencent |
| **Parameters** | 8.3B |
| **Resolution** | 720p (1280x720) @ 24fps, up to 15 seconds |
| **VRAM/Memory** | Full: 60GB+; GGUF Q8_0: ~12-16GB; with CPU offload: ~8GB GPU + system RAM |
| **ComfyUI Support** | Official node pack (ComfyUI-HunyuanVideo); GGUF/FP16/BF16 support |
| **Apple Silicon** | HunyuanVideo_MLX port exists; must use FP16 (bf16 not supported on MPS); Q8_0 recommended (Q4_K_M produces noise) |
| **Content Restrictions** | Official release has safety filters; enable_safety_checker defaults to false in many implementations |
| **Uncensored Versions** | Community "cosy" builds remove prompt blocking, NSFW filters, and safety classifiers; core weights unchanged |

**Key Notes:**
- Very high quality output, competitive with Wan 2.2
- VERY slow on Mac: M3 Pro 36GB takes ~16 minutes per clip; described as "one day to generate a single video" for full-quality
- MLX port exists for native Apple Silicon performance
- GGUF quantization is essential for Mac use
- Community versions just remove the filter layer -- the model itself was not explicitly trained on restricted content, but it can generate it when unfiltered
- CPU offloading for text encoder frees 4-5GB VRAM

**Practical for your use case:** PARTIALLY -- Quality is excellent and uncensored builds exist, but generation speed on Mac is painfully slow. Best if you have 128GB+ unified memory and patience, or use it selectively for hero shots.

---

### 3. LTX-2 / LTX-2.3

| Attribute | Details |
|---|---|
| **Developer** | Lightricks |
| **Latest Version** | LTX-2.3 (March 2026) |
| **Resolution** | Up to 4K @ 50fps with audio; portrait 9:16 support |
| **Duration** | Up to 20 seconds |
| **VRAM/Memory** | Runs on single RTX 4090 (24GB); GGUF versions for lower VRAM |
| **ComfyUI Support** | Official ComfyUI-LTXVideo nodes; native ComfyUI integration |
| **Apple Silicon** | Partial MPS support (some issues with Float8/output channels); native macOS app exists (ltx-video-mac); LTX Desktop runs on M1+; ~4-6 min for 10s 1080p on M3 Max |
| **Content Restrictions** | Open-source weights with permissive license; no prominent built-in content filters in open weights |
| **License** | Free for commercial use under $10M ARR |

**Key Notes:**
- LTX-2.3 (just released) adds redesigned VAE, better prompt adherence, cleaner audio, portrait video
- Quality described as "on par with Google Veo 3"
- Some MPS compatibility issues: "Output channels > 65536" not supported; use FP16 T5 encoder instead of FP8
- torch 2.5+ on Mac reported to produce noise -- may need specific torch version
- Native macOS app uses MLX for Apple Silicon optimization
- Open weights = full control over filtering

**Practical for your use case:** PROMISING -- Fast generation, high quality, good Mac support trajectory. The 4K capability and audio sync are significant advantages. MPS issues are being actively worked on. Worth testing alongside Wan 2.2.

---

### 4. CogVideoX

| Attribute | Details |
|---|---|
| **Developer** | Tsinghua University / Zhipu AI (via SiliconFlow) |
| **Sizes** | 2B and 5B |
| **Resolution** | 720x480 (5B), 6-second clips |
| **VRAM/Memory** | 2B: 12GB; 5B: 13-14GB |
| **ComfyUI Support** | ComfyUI-CogVideoXWrapper community nodes |
| **Apple Silicon** | Technically works via MPS fallback (`PYTORCH_ENABLE_MPS_FALLBACK=1`); ~20x slower than RTX 4090 |
| **Content Restrictions** | No prominent built-in content filter in open weights |
| **License** | Apache 2.0 |

**Key Notes:**
- Lower resolution and shorter clips than Wan 2.2 or HunyuanVideo
- CogVideoX1.5-5B supports 10-second videos at higher resolution
- MPS fallback works but performance is very poor on Mac
- Apache 2.0 license is very permissive
- Older model, largely superseded by Wan 2.2 and HunyuanVideo for quality

**Practical for your use case:** LIMITED -- Lower quality than what you already have with Wan 2.2, and very slow on Mac via MPS fallback. Not recommended as a replacement.

---

### 5. AnimateDiff / AnimateLCM / AnimateDiff-Lightning

| Attribute | Details |
|---|---|
| **Developer** | Various (Shanghai AI Lab, ByteDance for Lightning) |
| **Architecture** | Motion module plugin for Stable Diffusion |
| **Resolution** | Depends on base SD model (typically 512x512 to 768x768) |
| **VRAM/Memory** | 8GB minimum; AnimateDiff-Lightning designed for speed |
| **ComfyUI Support** | ComfyUI-AnimateDiff-Evolved -- excellent, mature integration |
| **Apple Silicon** | Fully compatible with M1/M2/M3/M4 |
| **Content Restrictions** | None inherent -- uses whatever SD checkpoint you load |
| **Uncensored Versions** | Use any uncensored SD 1.5 checkpoint (e.g., from CivitAI) |

**Key Notes:**
- This is a fundamentally different approach: it adds motion to Stable Diffusion image models
- No built-in content filter -- the base SD model determines content capability
- Huge ecosystem of SD checkpoints, LoRAs, and ControlNets
- AnimateDiff-Lightning (ByteDance) is dramatically faster
- Lower visual quality and shorter clips than dedicated video models
- Context/view options extend animation length while managing VRAM
- Very mature ComfyUI integration with scheduling, ControlNet, FaceDetailer support
- Works well for stylized / anime content; less ideal for photorealistic video

**Practical for your use case:** SUPPLEMENTARY -- Best for quick iteration, stylized content, or when you need specific SD checkpoints for character consistency. Not a replacement for Wan 2.2 for realistic video, but a good complement. The complete lack of content restrictions is an advantage.

---

### 6. Open-Sora 2.0

| Attribute | Details |
|---|---|
| **Developer** | HPC-AI Tech (ColossalAI team) |
| **Parameters** | 11B |
| **Resolution** | Up to 720p, 2-15 seconds |
| **VRAM/Memory** | 40GB+ (reports of 60GB+ actual usage) |
| **ComfyUI Support** | Community nodes (ComfyUI-Open-Sora-I2V) for V2 and V3 models |
| **Apple Silicon** | No confirmed MPS support; primarily CUDA-only |
| **Content Restrictions** | Open-source weights; no prominent built-in filters |

**Key Notes:**
- Performance "nearly at parity" with OpenAI's Sora (0.69% gap)
- Very high VRAM requirements make it impractical for most local setups
- CUDA-focused -- no evidence of Apple Silicon / MPS support
- Supports T2V, I2V, V2V, and infinite time generation
- Quality is high but resource demands are prohibitive for Mac

**Practical for your use case:** NO -- Too resource-intensive and no Apple Silicon support. Skip this one for local Mac generation.

---

### 7. Mochi 1

| Attribute | Details |
|---|---|
| **Developer** | Genmo |
| **Parameters** | 10B |
| **Resolution** | 480p, ~5.4 seconds |
| **VRAM/Memory** | ~60GB single GPU; ComfyUI optimized: ~20GB; multi-GPU: 4x H100 recommended |
| **ComfyUI Support** | Community implementations exist |
| **Apple Silicon** | No confirmed support |
| **Content Restrictions** | Open-source (Apache 2.0); no prominent built-in filters |
| **License** | Apache 2.0 |

**Key Notes:**
- Uses novel Asymmetric Diffusion Transformer architecture
- High VRAM requirements even with ComfyUI optimization
- 480p output is significantly lower than competitors
- No evidence of Apple Silicon / MPS support
- The Apache 2.0 license is very permissive for commercial use
- Has been largely overtaken by Wan 2.2 and HunyuanVideo in quality

**Practical for your use case:** NO -- Too resource-intensive for Mac, lower resolution than alternatives, no Apple Silicon support.

---

## Content Restrictions Summary

| Model | Built-in Safety Filter | Easy to Remove? | Training Data Restrictions | Notes |
|---|---|---|---|---|
| **Wan 2.2** | Minimal / Optional | Yes - just don't include safety-checker node | Minimal | Remix variant has no restrictions at all |
| **Wan 2.2 Remix** | None | N/A | None | Purpose-built for unrestricted generation |
| **HunyuanVideo 1.5** | Yes (default off in many impls) | Yes - community "cosy" builds | Core weights not trained on explicit content | Unfiltered version generates mature content despite training |
| **LTX-2.3** | None prominent | N/A | Open weights, full transparency | Permissive license, full code access |
| **CogVideoX** | None prominent | N/A | Open weights | Apache 2.0 license |
| **AnimateDiff** | None (uses base SD model) | N/A | Depends on SD checkpoint | Use any uncensored SD checkpoint |
| **Open-Sora 2.0** | None prominent | N/A | Open weights | Not practical for Mac |
| **Mochi 1** | None prominent | N/A | Open weights, Apache 2.0 | Not practical for Mac |

**Key insight:** Most open-source video models have minimal or no built-in content restrictions in their downloadable weights. The censorship typically comes from:
1. Hosted API services (not applicable when running locally)
2. Optional safety-checker nodes in ComfyUI (just don't add them)
3. Training data curation (models may be less skilled at content they weren't trained on, but won't actively refuse)

---

## Apple Silicon Compatibility Ranking

| Rank | Model | Mac Viability | Notes |
|---|---|---|---|
| 1 | **Wan 2.2 5B** | Good | GGUF works, you already have it running |
| 2 | **AnimateDiff** | Good | 8GB VRAM, fully compatible M1-M4 |
| 3 | **LTX-2.3** | Decent | Native macOS app exists; some MPS issues in ComfyUI; ~4-6 min per clip on M3 Max |
| 4 | **HunyuanVideo 1.5** | Slow but works | MLX port exists; GGUF Q8_0 required; very slow generation |
| 5 | **CogVideoX** | Poor | MPS fallback only, ~20x slower than GPU |
| 6 | **Open-Sora 2.0** | No | CUDA only, 40-60GB+ VRAM |
| 7 | **Mochi 1** | No | Requires H100-class GPUs |

---

## Recommendations for Your Workflow

### Immediate Actions (No New Setup Needed)
1. **Ensure you're using Wan 2.2 without safety-checker nodes** in your ComfyUI workflow. ComfyUI itself has no censorship -- just don't add the optional safety-checker node.
2. **Try the Wan2.2-Remix model** (available on HuggingFace at FX-FeiHou/wan2.2-Remix) -- drop-in replacement, specifically built for unrestricted content.

### Worth Testing
3. **LTX-2.3** -- Just released (March 2026). Try the native macOS app first (github.com/james-see/ltx-video-mac) to evaluate quality. If quality suits your needs, integrate into ComfyUI. 4K output with audio is a major advantage for production work.
4. **AnimateDiff-Lightning** -- For quick iteration and storyboarding. Pair with an uncensored SD 1.5 checkpoint. Very fast on Mac, good for previsualization before rendering final clips in Wan 2.2.

### Consider for Future
5. **HunyuanVideo 1.5** -- Monitor the MLX port for speed improvements. If generation time drops to reasonable levels, the uncensored community builds make it a strong alternative.
6. **Wan 2.5 / 2.6** -- These newer versions add audio sync, 1080p, and 10-second clips. Check if open-source weights become available for local ComfyUI use (currently some features are API-only).

### Skip
- **CogVideoX** -- Superseded by Wan 2.2 in quality, poor Mac performance
- **Open-Sora 2.0** -- No Mac support, extreme VRAM requirements
- **Mochi 1** -- No Mac support, lower resolution, extreme VRAM requirements

---

## Sources

- [Wan 2.2 GitHub](https://github.com/Wan-Video/Wan2.2)
- [Wan2.2 Remix T2V (Next Diffusion)](https://www.nextdiffusion.ai/tutorials/create-uncensored-videos-with-wan22-remix-in-comfyui-t2v)
- [Wan2.2 Remix I2V (Next Diffusion)](https://www.nextdiffusion.ai/tutorials/creating-uncensored-videos-with-wan22-remix-in-comfyui-i2v)
- [Wan 2.2 ComfyUI Official Workflow](https://docs.comfy.org/tutorials/video/wan/wan2_2)
- [Wan 2.2 Unrestricted (Republic Labs)](https://blog.republiclabs.ai/2025/12/wan-22-unrestricted-ai-video-generator.html)
- [HunyuanVideo 1.5 GitHub](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
- [HunyuanVideo MLX Port](https://github.com/gaurav-nelson/HunyuanVideo_MLX)
- [HunyuanVideo 1.5 Uncensored (Promptus)](https://www.promptus.ai/blog/hunyuan-video-1-5)
- [HunyuanVideo M4 MacBook Guide](https://gist.github.com/mdbecker/be0c1730e4a9a8830e46c72812f18a6e)
- [HunyuanVideo 1.5 Low VRAM GGUF Guide](https://apatero.com/blog/hunyuanvideo-15-low-vram-gguf-5g-complete-guide-2025)
- [LTX-2.3 Launch Guide](https://www.genaintel.com/guides/ltx-2-3-launch-guide)
- [LTX-2 Open Source](https://ltx.io/model/open-source)
- [LTX-Video Mac Native App](https://github.com/james-see/ltx-video-mac)
- [LTX-2 Apple Silicon Issues](https://github.com/Lightricks/ComfyUI-LTXVideo/issues/386)
- [LTX MPS Support Issue](https://github.com/Lightricks/ComfyUI-LTXVideo/issues/302)
- [CogVideoX GitHub](https://github.com/siliconflow/CogVideo-P)
- [CogVideoX ComfyUI Wrapper](https://comfyuiweb.com/posts/comfyui-cog-video-x-wrapper)
- [AnimateDiff Evolved (ComfyUI)](https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved)
- [Open-Sora 2.0 Release](https://comfyui-wiki.com/en/news/2025-03-13-open-sora-2-release)
- [Mochi 1 GitHub](https://github.com/genmoai/mochi)
- [ComfyUI Without Censorship (Promptus)](https://www.promptus.ai/blog/comfyui-without-censorship)
- [ComfyUI MLX Extension](https://apatero.com/blog/comfyui-mlx-extension-70-faster-apple-silicon-guide-2025)
- [ComfyUI Mac M4 Max Setup Guide](https://apatero.com/blog/comfyui-mac-m4-max-complete-setup-guide-2025)
- [Best Open Source Video Models 2026 (Pixazo)](https://www.pixazo.ai/blog/best-open-source-ai-video-generation-models)
- [Best Open Source Video Models 2026 (Hyperstack)](https://www.hyperstack.cloud/blog/case-study/best-open-source-video-generation-models)
- [31 Open-Source AI Video Models (AI Free Forever)](https://aifreeforever.com/blog/open-source-ai-video-models-free-tools-to-make-videos)
- [WAN 2.6 ComfyUI (ComfyUI Blog)](https://blog.comfy.org/p/wan26-reference-to-video)
- [Wan 2.5 ComfyUI (ComfyUI Blog)](https://blog.comfy.org/p/wan-25-preview-api-nodes-in-comfyui)
- [MacBook Run Wan 2.2 (Medium)](https://medium.com/@ttio2tech_28094/macbook-macmini-run-wan-2-2-generating-videos-dd0e32eb91b3)
- [Wan2.2 Memory Optimization (ComfyUI Blog)](https://blog.comfy.org/p/wan22-memory-optimization)
