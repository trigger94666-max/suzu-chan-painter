#!/usr/bin/env python3
"""通用 ComfyUI 批次跑图器 —— 一份 JSON 描述一批图，省去每次重写整个 python 脚本。

用法：
  python comfy_batch.py batch.json
  python comfy_batch.py batch.json --dry-run     # 只检查参数、不提交
  python comfy_batch.py batch.json --no-free     # 同线连跑时跳过 /free

batch.json：
{
  "line": "<线名，见 config.json 的 lines 段；默认只有示例线 example>",
  "lora": "<LoRA 文件名>" | null,
  "lora_strength": 0.8,
  "negative": "（可选，覆盖该线默认负面）",
  "width": 832, "height": 1216,
  "steps": null, "cfg": null,
  "prefix": "mybatch",
  "jobs": [ {"prompt": "...", "seed": 1234, "suffix": "a"}, ... ]
}

设计原则：负面模板里已经装了红线（gore/blood/injury）与常见崩坏防线，红线不要删。
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request

BASE = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
_HERE = os.path.dirname(os.path.abspath(__file__))
# 出图/下载目录：server.py 会用 config.json 的值注入环境变量；
# 直接命令行跑这个脚本时落到项目下的 outputs/（不想要就自己设 COMFY_OUT）。
OUT = os.environ.get("COMFY_OUT") or os.path.join(_HERE, "outputs")
DL = os.environ.get("COMFY_DL") or os.path.join(_HERE, "outputs")

# ── 默认负面词 ──
# ⚠️ 开源版刻意**不含露骨解剖词**：这类词会给仓库贴上 NSFW 标签、招来不必要的麻烦。
#    要恢复完整防崩坏词，写在你自己的 config.json 的 negatives 段里（server.py 会覆盖）。
#    红线词（gore/blood/child/loli）**必须保留**，这是安全底线不是口味问题。
NEG_COMBO = ("worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, "
             "bad anatomy, bad hands, bad feet, extra fingers, extra toes, missing fingers, fused fingers, "
             "extra arms, extra legs, deformed hands, deformed feet, "
             "gore, blood, injury, exposed bone, "
             "text, watermark, signature, child, loli, hazy, foggy")

NEG_REAL = ("worst quality, low quality, blurry, bad anatomy, bad hands, bad feet, extra fingers, "
            "missing fingers, fused fingers, extra arms, extra legs, deformed, "
            "cartoon, anime, illustration, painting, 3d render, plastic skin, doll, "
            "text, watermark, signature, child, loli, kid, young")

# ── 线的定义 ──
# ⚠️ 这里是**示例**，模型名是占位的，照抄会报「模型不存在」。
#
# 你自己常用的线写在 config.json 的 "lines" 段里（格式跟下面一样），
# 启动时会自动合并进来（同名的覆盖示例）。好处：不用改本文件，
# 也不会把「你机器上装了什么模型」提交进仓库。
#
# 单个 line 支持的字段：
#   ckpt / unet               模型文件名（二选一：ckpt = checkpoint，unet = 分离式）
#   unet_node                 默认 UNETLoader；GGUF 用 UnetLoaderGGUF
#   clip / clip_type / vae    分离式模型才需要
#   pos_node                  正向文本编码节点名（各家模型不一样）
#   steps / cfg / sampler / scheduler / width / height
#   neg                       该线的默认负面词
#   default_lora / default_lora_strength   该线默认挂的 LoRA（可选）
LINES = {
    "example": dict(
        ckpt="your_model.safetensors",          # ← 换成你 ComfyUI 里真实存在的文件名
        vae=None,
        pos_node="CLIPTextEncode",
        steps=20, cfg=7.0, sampler="euler", scheduler="normal",
        width=512, height=512,
        neg=NEG_COMBO),
}


# 把 config.json 里 "lines" 段的自定义线合并进来（同名的覆盖上面的示例）。
# 放在文件末尾：保证 LINES 已经有内容；只有标准库，读不到也不影响跑。
try:
    from config import CFG as _CFG
    for _n, _spec in (_CFG.get("lines") or {}).items():
        if isinstance(_spec, dict) and _spec:
            LINES[_n] = _spec
except Exception:
    pass


def post(path, data, timeout=120):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw) if raw else {}


def build_graph(spec, prefix, prompt, negative, seed, lora, strength, w, h, steps, cfg,
                lora2=None, strength2=0.8, loras=None):
    """LoRA 链：按顺序串 LoraLoader。

    优先用 loras=[(名字, 强度), ...]（任意个数，2026-09-27 加，配合跑图台的多 LoRA 槽）；
    没传 loras 时退回 lora/lora2 两槽的老写法——老调用行为完全不变。
    """
    line = spec
    lora_chain = loras if loras is not None else [(lora, strength), (lora2, strength2)]
    lora_chain = [(n, s) for (n, s) in lora_chain if n]
    if "ckpt" in line:
        g = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": line["ckpt"]}}}
        model_src, clip_src, vae_src = ["1", 0], ["1", 1], ["1", 2]
        for _i, (_ln, _st) in enumerate(lora_chain):
            _nid = str(11 + _i)
            g[_nid] = {"class_type": "LoraLoader", "inputs": {
                "lora_name": _ln, "strength_model": _st, "strength_clip": _st,
                "model": model_src, "clip": clip_src}}
            model_src, clip_src = [_nid, 0], [_nid, 1]
        if line.get("auraflow"):
            g["10"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": model_src, "shift": 3.0}}
            model_src = ["10", 0]
    else:
        g = {
            "1": {"class_type": line["unet_node"], "inputs": {"unet_name": line["unet"],
                                                              "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": line["clip"], "type": line["clip_type"]}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": line["vae"]}},
        }
        model_src, clip_src, vae_src = ["1", 0], ["2", 0], ["3", 0]
        for _i, (_ln, _st) in enumerate(lora_chain):
            _nid = str(11 + _i)
            g[_nid] = {"class_type": "LoraLoader", "inputs": {
                "lora_name": _ln, "strength_model": _st, "strength_clip": _st,
                "model": model_src, "clip": clip_src}}
            model_src, clip_src = [_nid, 0], [_nid, 1]
        if line.get("auraflow"):
            g["10"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": model_src, "shift": 3.0}}
            model_src = ["10", 0]

    if line["pos_node"] == "TextEncodeZImageOmni":
        # ⚠️ clip + auto_resize_images 两个 required 字段一个都不能少
        g["4"] = {"class_type": "TextEncodeZImageOmni",
                  "inputs": {"clip": clip_src, "prompt": prompt, "auto_resize_images": True}}
    else:
        g["4"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": prompt}}
    g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip_src, "text": negative}}
    g["6"] = {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}}
    g["7"] = {"class_type": "KSampler", "inputs": {
        "model": model_src, "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0],
        "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": line["sampler"], "scheduler": line["scheduler"], "denoise": 1.0}}
    g["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": vae_src}}
    g["9"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}}
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-free", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()

    b = json.load(open(a.batch, encoding="utf-8"))
    line_name = b.get("line") or (list(LINES)[0] if LINES else "")
    if line_name not in LINES:
        sys.exit("unknown line: %s (options: %s)" % (line_name, "/".join(LINES)))
    spec = LINES[line_name]
    neg = b.get("negative") or spec["neg"]
    w = b.get("width") or spec["width"]
    h = b.get("height") or spec["height"]
    steps = b.get("steps") or spec["steps"]
    cfg = b.get("cfg") or spec["cfg"]
    lora = b.get("lora") or spec.get("default_lora")
    strength = float(b.get("lora_strength") or spec.get("default_lora_strength", 0.8))
    prefix = b.get("prefix", "batch")
    jobs = b["jobs"]

    print("[config] line=%s lora=%s steps=%s cfg=%s %sx%s jobs=%d"
          % (line_name, lora, steps, cfg, w, h, len(jobs)), flush=True)
    if a.dry_run:
        for j in jobs:
            g = build_graph(spec, prefix + "_" + j.get("suffix", "x"), j["prompt"], neg,
                            j.get("seed", 0), lora, strength, w, h, steps, cfg)
            print("  [ok] suffix=%s seed=%s nodes=%d prompt_len=%d"
                  % (j.get("suffix"), j.get("seed"), len(g), len(j["prompt"])), flush=True)
        print("[dry-run] 参数与节点图均构建成功，未提交")
        return

    if b.get("free", True) and not a.no_free:
        try:
            post("/free", {"unload_models": True, "free_memory": True})
            print("[free] sent", flush=True)
        except Exception as e:
            print("[free] note:", e, flush=True)
        time.sleep(2)

    for j in jobs:
        p = prefix + "_" + str(j.get("suffix", "x"))
        g = build_graph(spec, p, j["prompt"], neg, j.get("seed", 0), lora, strength, w, h, steps, cfg)
        try:
            r = post("/prompt", {"prompt": g, "client_id": "suzune-batch"})
            print("[queued]", p, r.get("prompt_id"), flush=True)
        except urllib.error.HTTPError as e:
            print("[FAIL]", p, e.code, e.read()[:400], flush=True)
            sys.exit(1)
        time.sleep(1)

    t0 = time.time()
    while time.time() - t0 < a.timeout:
        q = json.loads(urllib.request.urlopen(BASE + "/queue", timeout=15).read())
        r_, p_ = len(q.get("queue_running", [])), len(q.get("queue_pending", []))
        done = [f for f in os.listdir(OUT) if f.startswith(prefix + "_") and f.endswith(".png")]
        print("[poll] %ds running=%s pending=%s files=%d" % (int(time.time() - t0), r_, p_, len(done)), flush=True)
        if r_ == 0 and p_ == 0:
            break
        time.sleep(15)

    copied = 0
    for f in sorted(os.listdir(OUT)):
        if f.startswith(prefix + "_") and f.endswith(".png"):
            shutil.copy2(os.path.join(OUT, f), os.path.join(DL, f))
            copied += 1
            print("    copied", f, flush=True)
    print("[DONE] copied =", copied, flush=True)


if __name__ == "__main__":
    main()
