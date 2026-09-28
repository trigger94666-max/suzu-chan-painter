#!/usr/bin/env python3
"""铃酱跑图台 · ComfyUI 薄前端（纯标准库，零依赖）

启动：  python server.py           → http://127.0.0.1:8199
两种用法：浏览器点网页；或脚本 POST /api/run（同一套后端、同一个队列、同一个图墙）
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import CFG                 # 配置：内置默认 < config.json < 环境变量
import config as cfgmod                # 网页改设置时用它写回 config.json

COMFY = CFG["comfy_url"]
OUT = CFG["output_dir"]
DL = CFG["download_dir"]
STORE = os.path.join(HERE, "prompts.json")
PORT = int(CFG["port"])

try:
    import comfy_batch as cb          # 线定义已内收进本项目（原先在 skill 目录里）
except Exception as e:                 # 缺文件也不崩，只是没线可选
    cb = None
    CB_ERR = str(e)

# 进度监听：ComfyUI 的采样进度只走 WebSocket，这里用标准库自己收（见 comfy_ws.py）
# 懒启动——第一次跑图时才连，没跑图就完全静默，不占资源
try:
    import comfy_ws
    _mon = comfy_ws.ProgressMonitor(
        host=urllib.parse.urlparse(CFG["comfy_url"]).hostname or "127.0.0.1",
        port=urllib.parse.urlparse(CFG["comfy_url"]).port or 8188,
    )
except Exception:
    comfy_ws = None
    _mon = None

# 人话 → tag：可插拔 Provider（默认关闭，见 config.json 的 tag_translator 段）
try:
    import tagtr
    _TAGTR_CFG = CFG.get("tag_translator") or {}
except Exception:
    tagtr = None
    _TAGTR_CFG = {}

_VOCAB = None


def vocab():
    """字典词表缓存（第一次用到才建）。"""
    global _VOCAB
    if _VOCAB is None and tagtr:
        try:
            _VOCAB = tagtr.Vocabulary(load_tags())
        except Exception:
            _VOCAB = tagtr.Vocabulary([])
    return _VOCAB


def monitor():
    """拿进度监听器（没有就返回 None）；第一次调用时启动后台线程。"""
    if _mon and not _mon._thread:
        _mon.start()
    return _mon

# 负面词覆盖：comfy_batch.py 里的默认值是**开源干净版**（不含露骨解剖词），
# 本机要完整防崩坏词就写在自己的 config.json 的 negatives 段里，这里盖上去。
_NEGS = CFG.get("negatives") or {}
if cb and _NEGS:
    for _line in cb.LINES.values():
        if _NEGS.get("combo") and _line.get("neg") is cb.NEG_COMBO:
            _line["neg"] = _NEGS["combo"]
        if _NEGS.get("real") and _line.get("neg") is cb.NEG_REAL:
            _line["neg"] = _NEGS["real"]

LINE_NAMES = list(cb.LINES.keys()) if cb else []
_lock = threading.Lock()


# ────────────────────────── ComfyUI ──────────────────────────
def comfy_get(path, timeout=20):
    return json.loads(urllib.request.urlopen(COMFY + path, timeout=timeout).read())


def comfy_alive():
    try:
        comfy_get("/system_stats", timeout=8)
        return True
    except Exception:
        return False


# ────────────────────────── ComfyUI 重启（卡死自救）──────────────────────────
COMFY_DIR = CFG["comfy_dir"]
COMFY_PY = os.path.join(COMFY_DIR, ".venv/Scripts/python.exe")
COMFY_LOG = CFG["comfy_log"]


def comfy_pids():
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            if ":8188" in line and "LISTENING" in line.upper():
                pids.add(line.split()[-1])
    except Exception:
        pass
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
            try:
                nm = (p.info.get("name") or "").lower()
                if not nm.startswith("python"):      # 别误伤 bash/cmd 包装进程
                    continue
                cl = " ".join(p.info.get("cmdline") or [])
                cwd = (p.info.get("cwd") or "").replace("\\", "/").lower()
                if "main.py" in cl and "comfyui" in (cwd + cl.lower()):
                    pids.add(str(p.info["pid"]))
            except Exception:
                continue
    except ImportError:
        pass
    return pids


def comfy_restart(wait=150):
    killed = []
    for pid in comfy_pids():
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=20)
            killed.append(pid)
        except Exception:
            pass
    time.sleep(3)
    try:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        log = open(COMFY_LOG, "ab", buffering=0)
        subprocess.Popen([COMFY_PY, "main.py", "--port", "8188", "--disable-dynamic-vram"],
                         cwd=COMFY_DIR, stdout=log, stderr=log,
                         creationflags=flags, close_fds=True)
    except Exception as e:
        return {"ok": False, "killed": killed, "error": str(e)}
    t0 = time.time()
    while time.time() - t0 < wait:
        if comfy_alive():
            # 端口通了不代表起飞完成（ComfyUI 在加载自定义节点前就监听），
            # 再稳 25 秒复查一次，避免报"ready"之后自己崩掉
            time.sleep(25)
            if comfy_alive():
                return {"ok": True, "killed": killed, "ready": True, "waited": int(time.time() - t0)}
            t0 = time.time()
        time.sleep(5)
    return {"ok": True, "killed": killed, "ready": False, "error": "启动后 %ds 内没稳住" % wait}


# ────────────────────────── LoRA 归类（按线过滤，省得 44 个混在一起）──────────────────────────
LORA_META = {k.lower(): (v[0], v[1], bool(v[2]))
            for k, v in (CFG.get("lora_meta") or {}).items()}
# ↑ 本机私有表（模型名 → 线/别名/星标），放在 config.json 的 lora_meta 段里。
# 开源默认是空表：LoRA 靠文件名规则自动归线（见 lora_line()），要手动指定再自己填。


def lora_line(name):
    n = name.lower()
    if n in LORA_META:
        return LORA_META[n][0]
    if n.startswith("z-") or "z_image" in n or n.startswith("gp_zimage") or "zimage" in n:
        return "zimage"
    if n.startswith("krea2"):
        return "krea2"
    if n.startswith("deepseekchan"):
        return "v23"
    return "anima"


USAGE_FILE = os.path.join(HERE, "lora_usage.json")


def lora_use_get():
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def lora_use_bump(name):
    if not name:
        return
    with _lock:
        d = lora_use_get()
        d[name] = int(d.get(name, 0)) + 1
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, USAGE_FILE)


_watchdog = {"on": False, "restarts": 0, "last": 0.0, "down_since": 0.0, "busy": False}
_idle = {"last_activity": time.time()}


def ensure_comfy(wait=150):
    """按需唤醒：ComfyUI 不在就拉起来（休眠态 → 在线）"""
    if comfy_alive():
        return {"ok": True, "already": True}
    _watchdog["busy"] = True
    try:
        return comfy_restart(wait=wait)
    finally:
        _watchdog["busy"] = False


def comfy_stop():
    """关掉 ComfyUI 进程，把显存全吐出来"""
    killed = []
    for pid in comfy_pids():
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=20)
            killed.append(pid)
        except Exception:
            pass
    return {"ok": True, "killed": killed}


# ────────────────────────── 提示词助手（说人话 → tags）──────────────────────────
_PH = CFG["prompt_help"]
LLM_ENV = _PH.get("env_file") or ""            # 你自己的 .env（可选，用来放 key）
PH_KEY_ENV = _PH.get("key_env") or "OPENCODE_GO_API_KEY"
OLLAMA = "http://127.0.0.1:11434"      # 本地兜底，可选
# 优先 opencode-go（Hermes 订阅，不走官方计费）；失败回落官方 DeepSeek → 本地 Ollama
GO_URL = _PH.get("endpoint") or "https://opencode.ai/zen/go/v1/chat/completions"
GO_MODEL = _PH.get("model") or "deepseek-v4.1-flash"          # 实测 2.4s 出 tag；mimo-v2.5 是 reasoning 模型（24s，且 content 常为 None）
GO_PROXY = CFG["proxy"]                  # 境外端点需要代理时填，例 http://127.0.0.1:7890
# ⚠️ 两个必需头（2026-09-19 实测）：缺 UA → Cloudflare 403 error 1010；缺 x-opencode-session → 400 MissingSessionID
GO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

SYS_TAGS = """你是动漫绘图模型的提示词工程师。把用户的中文描述翻译成 danbooru 风格的英文 tag 串。
规则：
1. 只输出一行 tag 串，逗号分隔，全小写，短语用下划线连接。不要解释、不要引号、不要编号、不要中文。
2. 顺序：人数/性别（1girl, solo）→ 角色名 → 外观（发色发型、瞳色、身材）→ 服装 → 姿势动作 → 表情 → 场景背景 → 光线氛围 → 画质词。
3. 具体角色用 danbooru 标准写法，如 `changli \\(wuthering waves\\)`、`ruan mei \\(honkai: star rail\\)`。
4. 用户要成人内容时，用规范准确的英文标签，不回避也不加戏。
5. 硬红线：绝不输出 gore、blood、断肢、内脏、猎奇内容。
6. 只用真实存在的 danbooru 概念，不要自造词。控制在 20-40 个 tag。"""

SYS_REAL = """你是写实摄影出图的提示词工程师。把用户的中文描述翻译成一段英文提示词（不是 danbooru tag）。
规则：
1. 只输出一行英文提示词，逗号和短句混合，自然流畅，像摄影描述。
2. 顺序：主体（年龄气质、人种、五官）→ 发型 → 身材 → 服装 → 动作姿势 → 表情 → 场景环境 → 光线 → 镜头感（shallow depth of field / film grain）。
3. 强调真实人像质感：photorealistic, natural skin texture, realistic asian face。避免 anime/cartoon/illustration 之类的词。
4. 用户要成人内容时用准确的自然语言描述，不回避也不加戏。
5. 硬红线：绝不输出 gore、blood、断肢、猎奇内容。
6. 控制在 40-70 个词。"""


def _read_env_key(name, path=None):
    """key 来源：环境变量 → config.json 里 prompt_help.env_file"""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    for p in ([path] if path else ([LLM_ENV] if LLM_ENV else [])):
        try:
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith(name + "="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except Exception:
            continue
    return ""


def prompt_help(zh, line="anima", n=1):
    zh = (zh or "").strip()
    if not zh:
        return {"ok": False, "error": "中文描述是空的"}
    sysmsg = SYS_REAL if line == "zimage" else SYS_TAGS
    user = zh
    if n > 1:
        user += "\n\n请给 %d 个不同的方案，每行一个。" % n

    err = ""
    msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]

    # 1) opencode-go（Hermes 订阅额度，不走官方计费）
    key = _read_env_key(PH_KEY_ENV)
    if key:
        try:
            body = json.dumps({"model": GO_MODEL, "messages": msgs,
                               "temperature": 1.0, "max_tokens": 900, "stream": False}).encode()
            req = urllib.request.Request(GO_URL, data=body, headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + key,
                "User-Agent": GO_UA, "x-opencode-session": "suzune-imgui-" + uuid.uuid4().hex[:12]})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": GO_PROXY, "https": GO_PROXY}))
            r = json.loads(op.open(req, timeout=120).read())
            m = r["choices"][0]["message"]
            txt = (m.get("content") or m.get("reasoning") or "").strip()
            if not txt:
                raise RuntimeError("empty completion: %s" % json.dumps(m, ensure_ascii=False)[:200])
            return {"ok": True, "provider": "opencode-go/%s" % GO_MODEL, "text": txt}
        except Exception as e:
            err = "opencode-go: %s" % e
            print("[prompt-help] opencode-go FAILED: %s" % e, flush=True)
    else:
        err = "opencode-go: no OPENCODE_GO_API_KEY"
        print("[prompt-help] no OPENCODE_GO_API_KEY found", flush=True)

    # 2) 官方 DeepSeek 兜底
    key = _read_env_key("DEEPSEEK_API_KEY")
    if key:
        try:
            body = json.dumps({"model": "deepseek-chat", "messages": msgs,
                               "temperature": 1.0, "max_tokens": 900, "stream": False}).encode()
            req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + key})
            r = json.loads(urllib.request.urlopen(req, timeout=120).read())
            return {"ok": True, "provider": "deepseek(官方兜底)",
                    "text": r["choices"][0]["message"]["content"].strip()}
        except Exception as e:
            err += " | deepseek: %s" % e
            print("[prompt-help] deepseek FAILED: %s" % e, flush=True)

    try:
        body = json.dumps({
            "model": "qwen25-ablit:latest",
            "prompt": sysmsg + "\n\n用户描述：\n" + user + "\n\n输出：",
            "stream": False, "options": {"temperature": 0.9, "num_predict": 800},
        }).encode()
        req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=300).read())
        return {"ok": True, "provider": "ollama(本地)", "text": (r.get("response") or "").strip()}
    except Exception as e:
        return {"ok": False, "error": "%s | ollama: %s" % (err, e)}


OPT_CACHE = os.path.join(HERE, "options_cache.json")


def _cache_get():
    try:
        with open(OPT_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_put(d):
    try:
        tmp = OPT_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, OPT_CACHE)
    except Exception:
        pass


def options():
    data = {"lines": [], "loras": [], "unets": [], "ckpts": [], "stale": False}
    if cb:
        for name, spec in cb.LINES.items():
            data["lines"].append({
                "name": name,
                "defaults": {k: spec.get(k) for k in ("steps", "cfg", "width", "height", "sampler", "scheduler")},
                "negative": spec.get("neg", ""),
                "unet": spec.get("unet") or spec.get("ckpt"),
            })
    try:
        oi = comfy_get("/object_info", timeout=60)
        raw = oi.get("LoraLoader", {}).get("input", {}).get("required", {}).get("lora_name", [[]])[0]
        use = lora_use_get()
        for name in raw:
            alias, star = "", False
            if name.lower() in LORA_META:
                alias, star = LORA_META[name.lower()][1], LORA_META[name.lower()][2]
            extra = bool(re.search(r"_\d{9}\.safetensors$", name)) or name.endswith(".bin")
            data["loras"].append({"name": name, "line": lora_line(name), "alias": alias,
                                  "star": star, "extra": extra, "used": int(use.get(name, 0))})
        data["unets"] = oi.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [[]])[0]
        data["ckpts"] = oi.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
        if data["loras"]:
            _cache_put({"loras": data["loras"], "unets": data["unets"], "ckpts": data["ckpts"]})
    except Exception:
        # ComfyUI 忙/挂了：退回上次拉的列表，别让下拉变空
        c = _cache_get()
        if c.get("loras"):
            use = lora_use_get()
            data["loras"] = []
            for x in c["loras"]:
                x = dict(x)
                x["used"] = int(use.get(x["name"], x.get("used", 0)))
                data["loras"].append(x)
            data["unets"] = c.get("unets", [])
            data["ckpts"] = c.get("ckpts", [])
            data["stale"] = True
    return data


def submit(batch):
    line = batch.get("line", "anima")
    if not cb or line not in cb.LINES:
        raise RuntimeError("unknown line: %s" % line)
    spec = cb.LINES[line]
    neg = batch.get("negative") or spec["neg"]
    w = int(batch.get("width") or spec["width"])
    h = int(batch.get("height") or spec["height"])
    steps = int(batch.get("steps") or spec["steps"])
    cfg = float(batch.get("cfg") or spec["cfg"])
    # ── LoRA 链 ──────────────────────────────────────────────
    # 优先 loras=[{name, strength}, ...]（跑图台 UI 最多 4 槽，任意个数）；
    # 没传则退回 lora/lora2 老写法——老调用行为完全不变。
    _raw = batch.get("loras")
    chain = None
    if isinstance(_raw, list) and _raw:
        chain = []
        for it in _raw:
            if not isinstance(it, dict):
                continue
            nm = it.get("name")
            if not nm:
                continue
            st = it.get("strength")
            chain.append((nm, float(st) if st is not None else 0.8))
        if not chain:
            chain = None
    if chain:
        lora, strength = chain[0]
        lora2, strength2 = (chain[1] if len(chain) > 1 else (None, 0.8))
    else:
        user_lora = batch.get("lora") or None
        if user_lora:
            lora = user_lora
            strength = float(batch.get("lora_strength") if batch.get("lora_strength") is not None else 0.8)
        else:
            lora = spec.get("default_lora")
            strength = float(spec.get("default_lora_strength", 0.8))
        # 第二 LoRA（可选，叠加）：2026-09-22 加，老调用不传则行为不变
        lora2 = batch.get("lora2") or None
        strength2 = float(batch.get("lora2_strength") if batch.get("lora2_strength") is not None else 0.8)
    # 每个 prompt 出几张：2026-09-22 加（一行一张抽卡太慢）
    per = max(1, min(int(batch.get("per_prompt") or 1), 16))
    prefix = re.sub(r"[^A-Za-z0-9_\-]", "_", str(batch.get("prefix") or "ui"))[:40]
    if chain:
        for _nm, _ in chain:
            lora_use_bump(_nm)
    else:
        lora_use_bump(lora)
        lora_use_bump(lora2)

    # 按需唤醒：休眠态（省显存）自动拉起来
    if not comfy_alive():
        ensure_comfy(wait=150)
    _idle["last_activity"] = time.time()
    _idle["freed_at"] = 0.0

    if batch.get("free", True):
        try:
            cb.post("/free", {"unload_models": True, "free_memory": True})
            time.sleep(2)
        except Exception:
            pass

    ids = []
    for i, j in enumerate(batch.get("jobs", [])):
        base = re.sub(r"[^A-Za-z0-9_\-]", "_", str(j.get("suffix") or ("j%d" % i)))[:20]
        seed0 = j.get("seed")
        if seed0 in (None, "", "random"):
            seed0 = int(time.time() * 1000) % 2_147_483_647
        seed0 = int(seed0)
        for k in range(per):
            suffix = base if per == 1 else "%s%d" % (base, k + 1)
            seed = seed0 + k        # 固定 seed 时逐张递增，否则 N 张会一模一样
            g = cb.build_graph(spec, "%s_%s" % (prefix, suffix), j.get("prompt", ""), neg,
                               seed, lora, strength, w, h, steps, cfg,
                               lora2=lora2, strength2=strength2, loras=chain)
            r = cb.post("/prompt", {"prompt": g, "client_id": "suzune-ui"})
            pid = r.get("prompt_id")
            if pid:
                m = monitor()
                if m:
                    m.track(pid, suffix="%s_%s" % (prefix, suffix), seed=seed)
            ids.append({"suffix": suffix, "seed": seed, "prompt_id": pid})
            time.sleep(1)
    return ids


# ────────────────────────── 配置档（整套参数一键套用）──────────────────────────
# 「跑一套参数」的完整快照：线 + LoRA + 强度 + 尺寸 + 采样。点一下就全填上，
# 省得每次在 UI 里挨个改下拉。数据层通用，不预设内容。
PROFILE_STORE = os.path.join(HERE, "profiles.json")
_PROFILE_FIELDS = ("name", "note", "line", "lora", "lora_strength", "lora2", "lora2_strength",
                   "width", "height", "steps", "cfg", "sampler", "scheduler", "prompt", "negative",
                   "prefix", "per_prompt")


def profile_load():
    try:
        with open(PROFILE_STORE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def profile_save(d):
    tmp = PROFILE_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROFILE_STORE)


def profile_upsert(b):
    name = (b.get("name") or "").strip()
    if not name:
        raise ValueError("name 不能空")
    with _lock:
        d = profile_load()
        pid = b.get("id")
        now = time.time()
        item = {}
        for k in _PROFILE_FIELDS:
            if k in b:
                v = b.get(k)
                if k in ("lora_strength", "lora2_strength", "cfg"):
                    v = float(v) if v not in (None, "") else None
                elif k in ("width", "height", "steps", "per_prompt"):
                    v = int(v) if v not in (None, "") else None
                elif isinstance(v, str):
                    v = v.strip()
                item[k] = v
        for it in d["items"]:
            if it.get("id") == pid:
                it.update(item)
                it["updated"] = now
                profile_save(d)
                return pid
        pid = "p%d" % int(now * 1000)
        item["id"] = pid
        item["used"] = 0
        item["created"] = now
        item["updated"] = now
        d["items"].append(item)
        profile_save(d)
        return pid


def profile_delete(pid):
    with _lock:
        d = profile_load()
        n = len(d["items"])
        d["items"] = [x for x in d["items"] if x.get("id") != pid]
        profile_save(d)
        return n - len(d["items"])


def profile_touch(ids, bump=1):
    with _lock:
        d = profile_load()
        n = 0
        for it in d["items"]:
            if it.get("id") in ids:
                it["used"] = int(it.get("used", 0)) + bump
                it["updated"] = time.time()
                n += 1          # 只数真正改到的，别把不存在的 id 也算上
        profile_save(d)
        return n


# ────────────────────────── 中文标签库（本地 CSV，零云端零 LLM）──────────────────────────
TAG_DIR = CFG["tag_dir"]
_TAGS = None
_TAG_LOCK = threading.Lock()


def load_tags():
    """懒加载中文标签库（8.6MB CSV，只读一次，全内存）。来源 amenorira/danbooru-tags-data-zh (MIT)"""
    global _TAGS
    if _TAGS:                      # 空列表也重试：词库可能刚用 fetch_tags.py 下好
        return _TAGS
    with _TAG_LOCK:
        if _TAGS:
            return _TAGS
        import csv
        rows = []
        for fn, cat in (("general.csv", "general"), ("character.csv", "character"),
                        ("copyright.csv", "copyright"), ("artist.csv", "artist"), ("meta.csv", "meta")):
            p = os.path.join(TAG_DIR, fn)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8-sig") as f:
                    rd = csv.reader(f)
                    next(rd, None)
                    for r in rd:
                        if len(r) < 6:
                            continue
                        try:
                            cnt = int(r[4])
                        except ValueError:
                            cnt = 0
                        rows.append({"tag": r[0], "cat": cat, "aliases": r[2],
                                     "zh": r[3], "count": cnt, "notes": r[5],
                                     "_hay": (r[0] + " " + r[2] + " " + r[3]).lower()})
            except Exception:
                continue
        rows.sort(key=lambda x: -x["count"])
        _TAGS = rows
    return _TAGS


def tag_dir_hint():
    """词库没配好时给一句人话提示（前端显示在面板里，而不是给个空白页）"""
    if not TAG_DIR:
        return "还没配词库目录：在 config.json 里设置 tag_dir，或直接跑 python fetch_tags.py 下载"
    if not os.path.isdir(TAG_DIR):
        return "词库目录不存在（%s）。跑 python fetch_tags.py 下载，或改 config.json 的 tag_dir" % TAG_DIR
    if not any(os.path.isfile(os.path.join(TAG_DIR, f)) for f in
               ("general.csv", "character.csv", "copyright.csv", "artist.csv", "meta.csv")):
        return "词库目录是空的（%s）。跑 python fetch_tags.py 下载" % TAG_DIR
    return ""


def search_tags(q, limit=20):
    """中文/英文关键词 → 标签候选。纯本地字符串匹配，不联网。
    排序：精确命中(tag/译名/别名) > 前缀命中 > 包含；同级内按图片量降序。"""
    q = (q or "").strip().lower()
    if not q:
        return []
    hits = [t for t in load_tags() if q in t["_hay"]]

    def key(t):
        tg = t["tag"].lower()
        zh = (t["zh"] or "").lower()
        al = [a.strip().lower() for a in (t["aliases"] or "").split("|") if a.strip()]
        if tg == q or zh == q or q in al:
            g = 0
        elif tg.startswith(q) or zh.startswith(q) or any(a.startswith(q) for a in al):
            g = 1
        else:
            g = 2
        return (g, -t["count"])

    hits.sort(key=key)
    return [{k: v for k, v in t.items() if k != "_hay"} for t in hits[:limit]]


# ─────────────────── 角色参考（AnimaDex 代理）───────────────────
# AnimaDex（github.com/zetaneko/AnimaDex，MIT）是 Anima 生成的角色/LoRA 索引站，
# 它的 /api/characters/search 直接给「角色名 + Danbooru tags + CivitAI LoRA 链接」，
# 正好补上跑图台缺的一环：中文角色 → tags → LoRA。
# 这里只做代理 + 短缓存，不复制它的本地库（免得跟着它的数据更新跑）。
ANIMADEX = (CFG.get("animadex_url") or "https://animadex.net").rstrip("/")
_CHAR_CACHE = {}
_CHAR_TTL = 300.0


_CJK = None


def _has_cjk(s):
    global _CJK
    if _CJK is None:
        import re as _re
        _CJK = _re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
    return bool(_CJK.search(s or ""))


def _raw_char_search(q, page, size, has_lora):
    url = "%s/api/characters/search?%s" % (ANIMADEX, urllib.parse.urlencode(
        {"q": q, "page": page, "page_size": size, **({"has_lora": 1} if has_lora else {})}))
    req = urllib.request.Request(url, headers={"User-Agent": "suzu-chan-painter", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return {"total": d.get("total", 0), "page": d.get("page", page),
            "results": [{"slug": x.get("slug"), "name": x.get("name"),
                         "copyright": x.get("copyright_name") or x.get("copyright"),
                         "trigger": x.get("trigger"), "tags": x.get("tags") or [],
                         "count": x.get("count"),
                         "thumb": x.get("thumb_url") or x.get("img_url"), "url": x.get("url"),
                         "loras": x.get("loras") or []}
                        for x in (d.get("results") or [])]}


def char_search(q, page=1, size=12, has_lora=False):
    """AnimaDex 的库是纯英文的。中文名 → 先用 LLM 译成英文名再搜。

    译名这一步复用 tag_translator 那份 provider 配置（用户只接一个 key）。
    没接 key 也不报错，就是搜不到中文名而已。
    """
    q = (q or "").strip()
    if not q:
        return {"ok": True, "total": 0, "results": []}
    key = "%s|%s|%s|%s" % (q.lower(), page, size, int(bool(has_lora)))
    hit = _CHAR_CACHE.get(key)
    if hit and time.time() - hit[0] < _CHAR_TTL:
        return hit[1]

    translated_from = None
    try:
        out = _raw_char_search(q, page, size, has_lora)
    except Exception as e:
        return {"ok": False, "err": "AnimaDex 连不上：%s" % e, "results": []}

    # 中文（或搜不到）→ 借 LLM 译成英文名再试一次
    if out["total"] == 0 and _has_cjk(q):
        if tagtr and tagtr.available(_TAGTR_CFG):
            tr = tagtr.char_name_to_english(q, _TAGTR_CFG)
            if tr.get("ok") and tr.get("name") and tr["name"].lower() != q.lower():
                try:
                    out2 = _raw_char_search(tr["name"], page, size, has_lora)
                    if out2["total"]:
                        out, translated_from = out2, {"input": q, "english": tr["name"]}
                except Exception:
                    pass
        else:
            out["hint"] = "中文名需要接一个 LLM（config.json 的 tag_translator）才能翻译；或直接用英文名搜"

    out["ok"] = True
    if translated_from:
        out["translated_from"] = translated_from
    _CHAR_CACHE[key] = (time.time(), out)
    if len(_CHAR_CACHE) > 200:
        for k in sorted(_CHAR_CACHE, key=lambda k: _CHAR_CACHE[k][0])[:100]:
            _CHAR_CACHE.pop(k, None)
    return out


# ────────────────────────── 点选面板（8 类一级 · 二级 filter）──────────────────────────
# 分类逻辑在 tagcats.py（同目录）：一级 8 类，原 22 类降级为二级，规则自动分、多归属。
sys.path.insert(0, HERE)
try:
    from tagcats import classify as _tagcls, PRIMARY as _PRIMARY
except Exception as e:                      # 缺文件也不崩，只是面板空
    _tagcls, _PRIMARY = (lambda _t: []), []
    PANEL_ERR = str(e)

_PANEL = None
_PANEL_LOCK = threading.Lock()


def my_tag_usage():
    """本机词频（个人常用）：prompts.json 里每条 prompt 的 tag，逗号切分。
        权重 = 1 + ★2 + 用过次数(上限5) —— 自己点过/收藏过的排前面，而不是全世界图量。"""
    import collections
    cnt = collections.Counter()
    try:
        with open(STORE, encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    except Exception:
        items = []
    for it in items:
        w = 1 + (2 if it.get("star") else 0) + min(int(it.get("used") or 0), 5)
        for seg in (it.get("text") or "").split(","):
            seg = seg.strip().lower().replace(" ", "_")
            if not seg or len(seg) > 60:
                continue
            seg = re.sub(r"^[\(\[]+|[\)\]]+$", "", seg).split(":")[0].strip("(): ").strip()
            if seg:
                cnt[seg] += w
    return cnt


def panel_index():
    """构建 {二级名: [词...]}（按图量降序）＋ 个人常用榜。懒加载只做一次。"""
    global _PANEL
    # 判空要看 by_sub：_PANEL 里还有 mine 字段，光判 _PANEL 会在词库为空时误认为已加载
    if _PANEL and _PANEL.get("by_sub"):
        return _PANEL
    with _PANEL_LOCK:
        if _PANEL and _PANEL.get("by_sub"):
            return _PANEL
        usage = my_tag_usage()
        by_sub, seen = {}, {}
        for t in load_tags():
            if t["cat"] != "general":
                continue
            subs = _tagcls(t["tag"])
            if not subs:
                continue
            key = t["tag"].lower().replace(" ", "_")
            item = {"tag": t["tag"], "zh": t["zh"], "count": t["count"], "mine": usage.get(key, 0)}
            seen[key] = item
            for s in subs:
                by_sub.setdefault(s, []).append(item)
        mine = sorted((v for v in seen.values() if v["mine"]), key=lambda x: -x["mine"])[:150]
        _PANEL = {"by_sub": by_sub, "mine": mine}
    return _PANEL


def panel_tree(top=24):
    """首屏：分类树 + 每个二级的高频 Top N（词量小，一次给全，点击零延迟）"""
    idx = panel_index()
    cats = []
    for name, subs in _PRIMARY:
        ss = []
        for s in subs:
            lst = idx["by_sub"].get(s, [])
            ss.append({"name": s, "total": len(lst), "top": lst[:top]})
        cats.append({"name": name, "subs": ss})
    out = {"ok": True, "cats": cats, "mine": idx["mine"]}
    h = tag_dir_hint()
    if h:
        out["hint"] = h
    return out


# ────────────────────────── 图墙 ──────────────────────────
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def gallery(limit=60):
    items = []
    for d, tag in ((DL, "dl"), (OUT, "out")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(IMG_EXT):
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items.append({"name": f, "dir": tag, "mtime": st.st_mtime, "size": st.st_size})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    seen, uniq = set(), []
    for it in items:                      # 同名的图 downloads/output 各有一份，只留一份
        if it["name"] in seen:
            continue
        seen.add(it["name"])
        uniq.append(it)
    return uniq[:limit]


def find_img(name):
    name = os.path.basename(name or "")
    for d in (DL, OUT):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


# ────────────────────────── 图片元数据（PNG tEXt）──────────────────────────
def png_chunks(path):
    out = {}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return out
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return out
    i, n = 8, len(data)
    while i + 8 <= n:
        ln = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + ln]
        if typ == b"tEXt":
            k, _, v = body.partition(b"\x00")
            out[k.decode("latin-1", "ignore")] = v.decode("latin-1", "ignore")
        elif typ == b"iTXt":
            parts = body.split(b"\x00", 5)
            if len(parts) == 6:
                out[parts[0].decode("latin-1", "ignore")] = parts[5].decode("utf-8", "ignore")
        i += 12 + ln
        if typ == b"IEND":
            break
    return out


def png_meta(path):
    """从 ComfyUI 出图的元数据里反查提示词与参数"""
    raw = png_chunks(path).get("prompt")
    if not raw:
        return {}
    try:
        g = json.loads(raw)
    except Exception:
        return {}
    info = {"positive": "", "negative": "", "seed": None, "steps": None, "cfg": None,
            "model": "", "sampler": ""}
    ks = [v for v in g.values() if isinstance(v, dict) and v.get("class_type") == "KSampler"]
    if ks:
        k = ks[0].get("inputs", {})
        info["seed"] = k.get("seed")
        info["steps"] = k.get("steps")
        info["cfg"] = k.get("cfg")
        info["sampler"] = "%s/%s" % (k.get("sampler_name"), k.get("scheduler"))
        pos = (k.get("positive") or [None])[0]
        neg = (k.get("negative") or [None])[0]
        for nid, node in g.items():
            ins = node.get("inputs", {}) if isinstance(node, dict) else {}
            if nid == pos:
                info["positive"] = ins.get("text") or ins.get("prompt") or ""
            elif nid == neg:
                info["negative"] = ins.get("text") or ins.get("prompt") or ""
        for node in g.values():
            if isinstance(node, dict) and node.get("class_type") in ("UNETLoader", "CheckpointLoaderSimple",
                                                                     "UnetLoaderGGUF"):
                ins = node.get("inputs", {})
                info["model"] = ins.get("unet_name") or ins.get("ckpt_name") or ins.get("unet_name_gguf") or ""
                if info["model"]:
                    break
    return info


# ────────────────────────── 提示词库 ──────────────────────────
def store_load():
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def store_save(d):
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


# ────────────────────────── 预设 ──────────────────────────
# 用户自己维护的 prompt 片段（角色特征 / 画风 / 固定负面词 / 构图套路）。
# 点一下拼进提示词框「光标所在的那一行」，跟已有 tag 逗号接上。
# 数据层是通用的，不预设内容。旧文件名 characters.json 仍可读（首次保存自动迁到 presets.json）。
PRESET_STORE = os.path.join(HERE, "presets.json")
PRESET_STORE_LEGACY = os.path.join(HERE, "characters.json")


def char_load():
    for p in (PRESET_STORE, PRESET_STORE_LEGACY):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                return d
        except Exception:
            continue
    return {"items": []}


def char_save(d):
    tmp = PRESET_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PRESET_STORE)


def char_upsert(b):
    name = (b.get("name") or "").strip()
    tags = (b.get("tags") or "").strip()
    if not name or not tags:
        raise ValueError("name 和 tags 都不能空")
    with _lock:
        d = char_load()
        cid = b.get("id")
        now = time.time()
        for it in d["items"]:
            if it.get("id") == cid:
                it.update({"name": name, "tags": tags, "note": (b.get("note") or "").strip(),
                           "neg": (b.get("neg") or "").strip(), "updated": now})
                char_save(d)
                return cid
        cid = "c%d" % int(now * 1000)
        d["items"].append({"id": cid, "name": name, "tags": tags,
                           "note": (b.get("note") or "").strip(), "neg": (b.get("neg") or "").strip(),
                           "used": 0, "created": now, "updated": now})
        char_save(d)
        return cid


def char_delete(cid):
    with _lock:
        d = char_load()
        d["items"] = [x for x in d["items"] if x.get("id") != cid]
        char_save(d)


def char_touch(cid):
    with _lock:
        d = char_load()
        for it in d["items"]:
            if it.get("id") == cid:
                it["used"] = int(it.get("used") or 0) + 1
                it["last_used"] = time.time()
                break
        char_save(d)


def chars_list():
    with _lock:
        d = char_load()
    items = sorted(d["items"], key=lambda x: (x.get("used", 0), x.get("created", 0)), reverse=True)
    return {"ok": True, "items": items}


def prompts_list():
    with _lock:
        d = store_load()
    items = sorted(d["items"],
                   key=lambda x: (x.get("star", False), x.get("used", 0), x.get("last_used", 0)),
                   reverse=True)
    for it in items:
        it["preview"] = (it.get("text", "")[:90] + ("…" if len(it.get("text", "")) > 90 else ""))
    return {"items": items,
            "groups": sorted({i.get("group", "") for i in items if i.get("group")})}


def prompt_upsert(b):
    with _lock:
        d = store_load()
        now = time.time()
        pid = b.get("id")
        if pid:
            for it in d["items"]:
                if it["id"] == pid:
                    for k in ("name", "text", "group", "line", "lora", "star"):
                        if k in b:
                            it[k] = b[k]
                    it["updated"] = now
                    break
            else:
                pid = None
        if not pid:
            pid = "p%d" % int(now * 1000)
            text = b.get("text", "")
            d["items"].append({
                "id": pid, "name": b.get("name") or (text[:24] or "未命名"),
                "text": text, "group": b.get("group", ""), "line": b.get("line", ""),
                "lora": b.get("lora", ""), "star": bool(b.get("star", False)),
                "used": 0, "created": now, "updated": now, "last_used": 0})
        store_save(d)
    return pid


def prompt_delete(ids):
    with _lock:
        d = store_load()
        d["items"] = [i for i in d["items"] if i["id"] not in ids]
        store_save(d)


def prompt_touch(ids):
    with _lock:
        d = store_load()
        now = time.time()
        for it in d["items"]:
            if it["id"] in ids:
                it["used"] = it.get("used", 0) + 1
                it["last_used"] = now
        store_save(d)


def prompts_import_gallery(n=30):
    """把图墙里最近 n 张图的提示词收进库里（按文件名前缀分组）"""
    added = 0
    for it in gallery(n):
        p = find_img(it["name"])
        if not p:
            continue
        m = png_meta(p)
        if not m.get("positive"):
            continue
        prefix = re.sub(r"_\d{5}_?.*$", "", it["name"]).replace(".png", "")
        with _lock:
            d = store_load()
            if any(x.get("text") == m["positive"] for x in d["items"]):
                continue
            now = time.time()
            d["items"].append({
                "id": "p%d" % int(now * 1000) + str(added), "name": "%s · %s" % (prefix, it["name"][:18]),
                "text": m["positive"], "group": prefix, "line": "", "lora": "", "star": False,
                "used": 0, "created": now, "updated": now, "last_used": 0,
                "meta": {"seed": m.get("seed"), "steps": m.get("steps"), "cfg": m.get("cfg"),
                         "model": m.get("model")}})
            store_save(d)
        added += 1
    return added


# ────────────────────────── HTTP ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, qs = u.path, urllib.parse.parse_qs(u.query)

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as e:
                return self._send(500, str(e).encode(), "text/plain; charset=utf-8")

        if path == "/api/options":
            return self._json(options())

        if path == "/api/profiles":
            return self._json({"ok": True, "items": profile_load()["items"]})

        if path == "/api/tags":
            q = (qs.get("q") or [""])[0]
            try:
                lim = int((qs.get("limit") or ["20"])[0])
            except ValueError:
                lim = 20
            return self._json({"ok": True, "q": q, "items": search_tags(q, max(1, min(lim, 60)))})

        if path == "/api/panel":
            sub = (qs.get("sub") or [""])[0]
            if sub:                                   # 某个二级的全部词（「显示全部」用）
                lst = panel_index()["by_sub"].get(sub, [])
                try:
                    n = int((qs.get("n") or ["300"])[0])
                except ValueError:
                    n = 300
                if (qs.get("all") or [""])[0]:
                    n = len(lst)
                return self._json({"ok": True, "sub": sub, "total": len(lst),
                                   "items": lst[:max(1, min(n, 1500))]})
            try:
                top = int((qs.get("top") or ["24"])[0])
            except ValueError:
                top = 24
            return self._json(panel_tree(max(6, min(top, 80))))

        if path == "/api/state":
            try:
                q = comfy_get("/queue")
                running, pending = len(q.get("queue_running", [])), len(q.get("queue_pending", []))
            except Exception:
                running, pending = -1, -1
            return self._json({"alive": comfy_alive(), "running": running, "pending": pending,
                               "gallery": gallery(int(qs.get("n", ["40"])[0]))})

        if path == "/api/prompts":
            return self._json(prompts_list())

        # ── 进度：任务状态（JSON，一次性快照）──────────────────────────
        if path == "/api/jobs":
            m = monitor()
            if not m:
                return self._json({"ok": False, "err": "progress monitor unavailable", "jobs": {}})
            m2 = m.snapshot()
            return self._json({"ok": True, "connected": m.connected, "jobs": m2,
                               "active": m.active()})

        # ── 进度：SSE 流（服务端推送，前端用 EventSource 接）──────────────
        # 为什么用 SSE 而不是 WebSocket：浏览器端 EventSource 是原生 API，
        # 后端只要按 text/event-stream 吐文本，零依赖下比手写 WS server 省事得多。
        if path == "/api/progress":
            m = monitor()
            if not m:
                return self._send(503, b"progress monitor unavailable", "text/plain; charset=utf-8")
            want = [x for x in (qs.get("ids") or [""])[0].split(",") if x]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_sig = None
            try:
                # 单次连接最多活 10 分钟，防止半死的连接一直挂着
                deadline = time.time() + 600
                while time.time() < deadline:
                    snap = m.snapshot()
                    if want:
                        snap = {k: v for k, v in snap.items() if k in want}
                    sig = json.dumps(snap, sort_keys=True, ensure_ascii=False)
                    if sig != last_sig:                     # 只在变化时推，省流量
                        payload = {"connected": m.connected, "jobs": snap,
                                   "active": any(v["status"] not in ("success", "failed", "cancelled")
                                                 for v in snap.values())}
                        self.wfile.write(("data: %s\n\n" % json.dumps(payload, ensure_ascii=False)).encode("utf-8"))
                        self.wfile.flush()
                        last_sig = sig
                    else:
                        self.wfile.write(b": ping\n\n")     # 心跳，防中间层断连
                        self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass                                        # 浏览器关了页面，正常
            except Exception:
                pass
            return

        # ── 分类：把一组 tag 映射到分类（已选词分组显示用）──────────────
        # 返回 {一级分类: [二级名...]} 与 {tag: [二级名...]}，前端据此分组。
        # 词表里查不到的 tag（用户手写、LoRA 触发词等）归到 "其他"。
        if path == "/api/classify":
            raw = (qs.get("tags") or [""])[0]
            tags = [t.strip() for t in raw.split(",") if t.strip()]
            detail, groups = {}, {}
            for t in tags:
                key = t.lower().replace(" ", "_")
                subs = []
                try:
                    subs = list(_tagcls(key)) or []
                except Exception:
                    subs = []
                detail[t] = subs
                # 二级 -> 一级；一个 tag 可多归属
                prims = []
                for name, ss in _PRIMARY:
                    for s in subs:
                        if s in ss:
                            prims.append(name)
                            break
                if not prims:
                    prims = ["其他"]
                for p in prims:
                    groups.setdefault(p, []).append(t)
            return self._json({"ok": True, "groups": groups, "detail": detail,
                               "order": [n for n, _ in _PRIMARY] + ["其他"]})

        # ── LLM 设置：网页上直接配，不用手改 config.json ──────────────
        if path == "/api/llm-config":
            tc = CFG.get("tag_translator") or {}
            return self._json({
                "ok": True,
                "provider": tc.get("provider") or "",
                "endpoint": tc.get("endpoint") or "",
                "model": tc.get("model") or "",
                "has_key": bool(tc.get("api_key")),
                "available": bool(tagtr and tagtr.available(tc)),
                "presets": cfgmod.PROVIDER_PRESETS,
                "config_path": os.path.join(HERE, "config.json"),
            })

        # ── 人话转 tag 的状态（前端用它决定要不要显示翻译框）──────────
        if path == "/api/translate-tags":
            ok = bool(tagtr and tagtr.available(_TAGTR_CFG))
            return self._json({"ok": True, "available": ok,
                               "provider": (_TAGTR_CFG or {}).get("provider") or None,
                               "vocab": (len(vocab()) if tagtr else 0)})

        # ── 角色参考：代理 AnimaDex（角色 → tags → LoRA）───────────────
        if path == "/api/charsearch":
            try:
                page = int((qs.get("page") or ["1"])[0])
                size = int((qs.get("n") or ["12"])[0])
            except ValueError:
                page, size = 1, 12
            return self._json(char_search((qs.get("q") or [""])[0],
                                          page=max(1, page), size=max(1, min(size, 40)),
                                          has_lora=bool((qs.get("lora") or [""])[0])))

        if path in ("/api/presets", "/api/chars"):
            return self._json(chars_list())

        if path == "/api/pnginfo":
            p = find_img(qs.get("name", [""])[0])
            return self._json(png_meta(p) if p else {})

        if path == "/img":
            name = os.path.basename(qs.get("name", [""])[0])
            p = find_img(name)
            if not p:
                return self._send(404, b"not found", "text/plain")
            ext = os.path.splitext(name)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)

        return self._send(404, b"{}")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad json"}, 400)

        try:
            if u.path == "/api/run":
                return self._json({"ok": True, "queued": submit(body)})
            # 人话 → tag：完整 pipeline（LLM 出候选 → 词表校验 → fuzzy 纠错）
            if u.path == "/api/translate-tags":
                if not tagtr:
                    return self._json({"ok": False, "err": "tagtr 模块缺失"})
                r = tagtr.translate(body.get("text") or "", _TAGTR_CFG, vocab(),
                                    cutoff=float(body.get("cutoff") or 0.86))
                return self._json(r)

            # 保存 LLM 设置（写回 config.json，带备份；改完立刻热生效）
            if u.path == "/api/llm-config":
                patch = {"tag_translator": {
                    "provider": (body.get("provider") or "").strip(),
                    "endpoint": (body.get("endpoint") or "").strip(),
                    "model": (body.get("model") or "").strip(),
                    "timeout": int(body.get("timeout") or 60),
                }}
                if (body.get("api_key") or "").strip():
                    patch["tag_translator"]["api_key"] = body["api_key"].strip()
                try:
                    cfgmod.save_patch(patch)
                except Exception as e:
                    return self._json({"ok": False, "err": str(e)})
                # 热更新：不用重启服务，下一个请求就用新配置
                globals()["_TAGTR_CFG"] = cfgmod.load().get("tag_translator") or {}
                return self._json({"ok": True,
                                   "available": bool(tagtr and tagtr.available(_TAGTR_CFG))})

            # 真测一次：拿一句话去问配好的模型，验证 key/endpoint 是通的
            if u.path == "/api/llm-test":
                if not tagtr:
                    return self._json({"ok": False, "err": "tagtr 模块缺失"})
                cfg = dict(body.get("cfg") or _TAGTR_CFG)
                if (body.get("api_key") or "").strip():
                    cfg["api_key"] = body["api_key"].strip()
                r = tagtr.ask("初音未来", cfg, system=tagtr.NAME_PROMPT, temperature=0.1)
                if not r.get("ok"):
                    return self._json({"ok": False, "err": r.get("err")})
                sample = (r.get("raw") or "").strip().split("\n")[0][:40]
                return self._json({"ok": True, "provider": r.get("provider"), "sample": sample,
                                   "note": "连接正常（试译「初音未来」→ %s）" % sample})
            if u.path == "/api/prompts":
                pid = prompt_upsert(body)
                return self._json({"ok": True, "id": pid})
            if u.path == "/api/prompts/delete":
                prompt_delete(body.get("ids", []))
                return self._json({"ok": True})
            if u.path == "/api/prompts/touch":
                prompt_touch(body.get("ids", []))
                return self._json({"ok": True})
            if u.path == "/api/prompts/import-gallery":
                return self._json({"ok": True, "added": prompts_import_gallery(int(body.get("n", 30)))})
            if u.path in ("/api/presets", "/api/chars"):
                return self._json({"ok": True, "id": char_upsert(body)})
            if u.path in ("/api/presets/delete", "/api/chars/delete"):
                char_delete(body.get("id"))
                return self._json({"ok": True})
            if u.path in ("/api/presets/touch", "/api/chars/touch"):
                char_touch(body.get("id"))
                return self._json({"ok": True})
            if u.path == "/api/comfy-restart":
                return self._json(comfy_restart(int(body.get("wait", 150))))
            if u.path == "/api/watchdog":
                _watchdog["on"] = bool(body.get("on", True))
                return self._json({"ok": True, "watchdog": _watchdog["on"]})
            if u.path == "/api/comfy-power":
                on = bool(body.get("on", True))
                if on:
                    return self._json(ensure_comfy(wait=int(body.get("wait", 150))))
                return self._json(comfy_stop())
            if u.path == "/api/prompt-help":
                return self._json(prompt_help(body.get("zh", ""), body.get("line", "anima"),
                                              int(body.get("n", 1))))
            if u.path == "/api/profiles":
                if self.command == "GET":
                    return self._json({"ok": True, "items": profile_load()["items"]})
                return self._json({"ok": True, "id": profile_upsert(body)})
            if u.path == "/api/profiles/delete":
                return self._json({"ok": True, "removed": profile_delete(body.get("id"))})
            if u.path == "/api/profiles/touch":
                return self._json({"ok": True, "n": profile_touch(body.get("ids", []))})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

        return self._json({"ok": False, "error": "unknown endpoint"}, 404)


if __name__ == "__main__":
    print("铃酱跑图台 →  http://127.0.0.1:%d" % PORT, flush=True)
    print("ComfyUI:", COMFY, "alive=", comfy_alive(), "| lines:", LINE_NAMES, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
