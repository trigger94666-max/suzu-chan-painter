#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tagtr.py —— 人话 → Danbooru tag（可插拔 Provider + 词表校验）

设计要点（来自 GPT 评审，照抄它的判断）：
    真正的难点不是"接哪个模型"，而是 **LLM 吐出来的东西对不对得上你的词表**。
    模型天然爱说 beautiful girl / pretty eyes，而这些词在 Danbooru 词表里不存在。
    所以 pipeline 必须是：

        自然语言 → LLM → 候选 tag → **词表校验** → fuzzy match → 最终 tag

    没有这一层校验，接了模型也是白接。

    Provider 是可插拔的：ollama / openai 兼容 / 任意 HTTP。
    **默认不配 provider = 功能关闭**——不破坏"零依赖、装上就能跑"这个卖点。

配置（写在 config.json 的 tag_translator 段，全部可选）：
    {
      "tag_translator": {
        "provider": "ollama",                       // ollama | openai | http
        "endpoint": "http://127.0.0.1:11434",       // 不填走 provider 默认
        "model": "qwen2.5:7b",
        "api_key": "",                              // 云服务才需要（也可用环境变量）
        "timeout": 60
      }
    }
"""

import difflib
import json
import re
import urllib.error
import urllib.request

# ───────────────────────── prompt ─────────────────────────

SYSTEM_PROMPT = (
    "You convert a Chinese/English description into Danbooru-style image tags.\n"
    "Rules:\n"
    "- Output ONLY a comma-separated list of lowercase english tags. No prose, no numbering, no quotes.\n"
    "- Use underscores instead of spaces inside a tag (e.g. long_hair, blue_eyes).\n"
    "- Prefer common Danbooru tags over descriptive phrases.\n"
    "- Do NOT invent words like 'beautiful', 'pretty', 'cute outfit'.\n"
    "- Order: subject count first (1girl/1boy), then appearance, clothing, pose, scene.\n"
    "Example input: 一个穿白裙子的蓝眼睛长发少女站在海边\n"
    "Example output: 1girl, long_hair, blue_eyes, white_dress, standing, ocean, beach"
)

# 这些"垃圾词"模型爱吐但词表里没有，直接扔
_JUNK = {
    "beautiful", "pretty", "cute", "nice", "good", "high quality", "masterpiece",
    "best quality", "detailed", "realistic", "photo", "image", "picture", "art",
    "anime style", "anime", "girl", "woman", "female", "male", "person",
}

_SPLIT = re.compile(r"[,，、\n;；]+")
# 模型爱加序号（"1. xxx 2. yyy"）——先把它当分隔符拆掉
_NUM = re.compile(r"(?:^|\s)\d+\s*[\.\)、]\s+")
_CLEAN = re.compile(r"^[\s\(\)\[\]<>\"'`]+|[\s\(\)\[\]<>\"'`]+$")


def _clean_tag(t):
    """规范化一个候选 tag：去括号引号、空格转下划线、去权重语法。"""
    t = (t or "").strip().lower()
    t = _CLEAN.sub("", t)
    t = re.sub(r"[:：]\s*[\d.]+$", "", t)      # (tag:1.2) -> tag
    t = t.replace(" ", "_")
    t = re.sub(r"_+", "_", t).strip("_")
    return t


def parse_tags(raw):
    """把模型输出切成候选列表（容忍它加序号、加引号、加解释）。"""
    raw = _NUM.sub(", ", raw or "")          # "1. a 2. b" -> "a, b"
    out = []
    for seg in _SPLIT.split(raw):
        seg = seg.strip()
        if not seg:
            continue
        seg = re.sub(r"^\d+[\.\)、]\s*", "", seg)     # 去掉残留下的开头序号
        t = _clean_tag(seg)
        if not t or t in _JUNK or len(t) > 60:
            continue
        out.append(t)
    # 去重保序
    seen, res = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res


# ───────────────────────── 词表校验 ─────────────────────────


class Vocabulary:
    """本地 tag 词表。校验 + fuzzy 纠错（这是整条链里最关键的一环）。"""

    def __init__(self, tags):
        self.exact = {}          # 规范化 tag -> 原 tag
        for t in tags or []:
            k = _clean_tag(t if isinstance(t, str) else (t.get("tag") or ""))
            if k and k not in self.exact:
                self.exact[k] = t if isinstance(t, str) else t.get("tag")

    def __len__(self):
        return len(self.exact)

    def check(self, cand, cutoff=0.86):
        """返回 (命中词, 是否 fuzzy)。命中不了返回 (None, False)。"""
        if cand in self.exact:
            return self.exact[cand], False
        near = difflib.get_close_matches(cand, self.exact.keys(), n=1, cutoff=cutoff)
        if near:
            return self.exact[near[0]], True
        return None, False


def verify(candidates, vocab, cutoff=0.86):
    """把候选对齐到词表：命中保留，近似纠错，剩下的丢掉。"""
    kept, fixed, dropped = [], [], []
    seen = set()
    for c in candidates:
        hit, was_fuzzy = vocab.check(c, cutoff) if len(vocab) else (c, False)
        if hit is None:
            dropped.append(c)
            continue
        low = str(hit).lower()
        if low in seen:
            continue
        seen.add(low)
        kept.append(hit)
        if was_fuzzy:
            fixed.append({"from": c, "to": hit})
    return kept, fixed, dropped


# ───────────────────────── Provider ─────────────────────────


def _post_json(url, payload, headers=None, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ask_ollama(text, cfg, system=None, temperature=0.2):
    base = (cfg.get("endpoint") or "http://127.0.0.1:11434").rstrip("/")
    model = cfg.get("model") or "qwen2.5:7b"
    d = _post_json(base + "/api/generate",
                   {"model": model,
                    "prompt": (system or SYSTEM_PROMPT) + "\n\nInput: " + text + "\nOutput:",
                    "stream": False, "options": {"temperature": temperature}},
                   timeout=int(cfg.get("timeout") or 60))
    return d.get("response") or ""


def _ask_openai(text, cfg, system=None, temperature=0.2, base_default="https://api.openai.com/v1"):
    """OpenAI 兼容（也覆盖 DeepSeek / 本地 vLLM / LM Studio 等）。"""
    base = (cfg.get("endpoint") or base_default).rstrip("/")
    key = cfg.get("api_key") or ""
    d = _post_json(base + "/chat/completions",
                   {"model": cfg.get("model") or "gpt-4o-mini",
                    "messages": [{"role": "system", "content": system or SYSTEM_PROMPT},
                                 {"role": "user", "content": text}],
                    "temperature": temperature},
                   headers={"Authorization": "Bearer " + key} if key else {},
                   timeout=int(cfg.get("timeout") or 60))
    ch = d.get("choices") or []
    return (ch[0].get("message") or {}).get("content") if ch else ""


def _ask_http(text, cfg, system=None, temperature=0.2):
    """自定义 HTTP：POST {text} 到 endpoint，期待 {tags:[...]} 或 {text: "..."}"""
    base = (cfg.get("endpoint") or "").rstrip("/")
    if not base:
        raise RuntimeError("http provider 需要 endpoint")
    headers = {"Authorization": "Bearer " + cfg["api_key"]} if cfg.get("api_key") else {}
    d = _post_json(base, {"text": text, "system": system or SYSTEM_PROMPT},
                   headers=headers, timeout=int(cfg.get("timeout") or 60))
    if isinstance(d.get("tags"), list):
        return ", ".join(str(x) for x in d["tags"])
    return d.get("text") or d.get("result") or ""


PROVIDERS = {
    "ollama": _ask_ollama,
    "openai": _ask_openai,
    "deepseek": lambda t, c, **kw: _ask_openai(t, {**c, "endpoint": c.get("endpoint") or "https://api.deepseek.com/v1"}, **kw),
    "http": _ask_http,
}


def available(cfg):
    """这个配置能不能用（默认没配 = 关闭）。"""
    p = (cfg or {}).get("provider")
    return bool(p) and p in PROVIDERS


def ask(text, cfg, system=None, temperature=0.2):
    """通用入口：拿一段文本去问配好的模型，返回它的原始字符串。

    人话转 tag、中文角色名转英文名都走这里——共用一份 provider 配置，
    用户只接一个 key 就两件事都能用。
    """
    cfg = cfg or {}
    p = cfg.get("provider")
    if not p:
        return {"ok": False, "err": "no provider configured（去 config.json 配 tag_translator）"}
    if p not in PROVIDERS:
        return {"ok": False, "err": "unknown provider: %s" % p}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "err": "empty text"}
    try:
        raw = PROVIDERS[p](text, cfg, system=system, temperature=temperature)
    except urllib.error.URLError as e:
        return {"ok": False, "err": "provider 连不上：%s" % e}
    except Exception as e:
        return {"ok": False, "err": "provider 出错：%s" % e}
    return {"ok": True, "provider": p, "raw": raw or ""}


# ─────────────── 中文角色名 → 英文名（给角色搜索用） ───────────────

NAME_PROMPT = (
    "You convert an anime/game character name (often Chinese) into the canonical "
    "English name used by Danbooru, so it can be looked up.\n"
    "Rules:\n"
    "- Output ONLY the english name, nothing else. No quotes, no explanation.\n"
    "- Use the Danbooru spelling, including the series in parentheses when ambiguous "
    "(e.g. 'Rem (Re:zero)', 'Artoria Pendragon (Fate)').\n"
    "- If it is already an english name, output it unchanged.\n"
    "- If you don't know the character, output the best literal romanization.\n"
    "Example input: 初音未来\nExample output: Hatsune Miku\n"
    "Example input: 蕾姆\nExample output: Rem (Re:zero)"
)


def char_name_to_english(name, cfg):
    """把中文角色名译成英文（复用同一个 provider）。"""
    r = ask(name, cfg, system=NAME_PROMPT, temperature=0.1)
    if not r.get("ok"):
        return r
    out = (r.get("raw") or "").strip().strip("\"'`。. ")
    out = out.split("\n")[0].strip()          # 只取第一行，防止模型啰嗦
    out = re.sub(r"^(answer|output|english name)\s*[:：]\s*", "", out, flags=re.I)
    if not out:
        return {"ok": False, "err": "模型没给出名字"}
    return {"ok": True, "name": out, "provider": r.get("provider")}


def translate(text, cfg, vocab, cutoff=0.86):
    """完整 pipeline：人话 → LLM → 候选 → 词表校验 → 最终 tag。"""
    cfg = cfg or {}
    p = cfg.get("provider")
    if not p:
        return {"ok": False, "err": "no provider configured（功能默认关闭，去 config.json 配 tag_translator）"}
    if p not in PROVIDERS:
        return {"ok": False, "err": "unknown provider: %s" % p}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "err": "empty text"}

    try:
        raw = PROVIDERS[p](text, cfg)
    except urllib.error.URLError as e:
        return {"ok": False, "err": "provider 连不上：%s" % e}
    except Exception as e:
        return {"ok": False, "err": "provider 出错：%s" % e}

    cands = parse_tags(raw)
    kept, fixed, dropped = verify(cands, vocab, cutoff)
    return {"ok": True, "provider": p, "tags": kept, "text": ", ".join(kept),
            "candidates": cands, "fixed": fixed, "dropped": dropped}


# ───────────────────────── 自测 ─────────────────────────

if __name__ == "__main__":
    import sys
    # 只用本机已有的几个词当词表试试校验逻辑
    v = Vocabulary(["1girl", "long_hair", "blue_eyes", "white_dress", "standing",
                    "ocean", "bloomers"])
    for s in ["1girl, long hair, blue eyes, white dress, standing, ocean, beach",
              "1. beautiful girl 2. pretty eyes 3. white drees"]:
        print("in :", s)
        print("out:", parse_tags(s))
        print("chk:", verify(parse_tags(s), v))
        print()
