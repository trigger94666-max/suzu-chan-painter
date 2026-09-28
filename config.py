# -*- coding: utf-8 -*-
"""配置加载 —— 优先级：内置默认值 < config.json < 环境变量

- `config.json` 是**本机私有**的（已在 .gitignore 里），放你自己的路径。
- `config.example.json` 只是给人看的模板，**不参与加载**——照抄一份改名成 `config.json` 即可。
- 两者都缺时用内置默认值，服务照样起得来（只是还没出图目录和词库）。
- 相对路径一律相对**项目目录**（不是当前工作目录），所以从哪儿启动都一样。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_USER = os.path.join(HERE, "config.json")

DEFAULTS = {
    "port": 8199,
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_dir": "",
    "comfy_log": "",
    "output_dir": "",
    "download_dir": "",
    "tag_dir": "",
    "proxy": "",
    "animadex_url": "https://animadex.net",
    # 可选 LLM：人话转 tag、中文角色名翻译都走这里。provider 留空 = 功能关闭。
    "tag_translator": {
        "provider": "",
        "endpoint": "",
        "model": "",
        "api_key": "",
        "timeout": 60,
    },
    "prompt_help": {
        "enabled": False,
        "env_file": "",
        "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
        "model": "deepseek-v4.1-flash",
        "key_env": "OPENCODE_GO_API_KEY",
    },
}

# 这几个 provider 只是给前端下拉框用的"预设"，用户也可以手填 endpoint
PROVIDER_PRESETS = {
    "deepseek": {"endpoint": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "signup": "https://platform.deepseek.com/api_keys",
                 "label": "DeepSeek（便宜、国内直连、支付宝充值）"},
    "openai":   {"endpoint": "https://api.openai.com/v1", "model": "gpt-4o-mini",
                 "signup": "https://platform.openai.com/api-keys",
                 "label": "OpenAI（质量好，需要海外支付）"},
    "ollama":   {"endpoint": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
                 "signup": "https://ollama.com/download",
                 "label": "Ollama（本机跑、免费、不联网）"},
    "custom":   {"endpoint": "", "model": "", "signup": "",
                 "label": "自定义（任何 OpenAI 兼容接口）"},
}

# 环境变量覆盖表：环境变量 > config.json（方便 Docker / 脚本临时改）
ENV_MAP = {
    "IMGUI_PORT": ("port", int),
    "COMFY_URL": ("comfy_url", str),
    "COMFY_DIR": ("comfy_dir", str),
    "COMFY_LOG": ("comfy_log", str),
    "COMFY_OUT": ("output_dir", str),
    "COMFY_DL": ("download_dir", str),
    "TAG_DIR": ("tag_dir", str),
    "HTTP_PROXY_LOCAL": ("proxy", str),
}

# 这些键是路径：相对路径要相对项目目录解析
_PATH_KEYS = ("comfy_dir", "comfy_log", "output_dir", "download_dir", "tag_dir")


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise SystemExit("读不了 %s：%s\n（八成是 JSON 格式问题，检查有没有多余的逗号 / 引号）" % (path, e))


def _check(cfg):
    """把配置错误说成人话，而不是甩一坨 traceback。"""
    errs = []
    try:
        cfg["port"] = int(cfg.get("port"))
    except (TypeError, ValueError):
        errs.append("port 必须是整数（现在写的是 %r）" % (cfg.get("port"),))
    else:
        if not (1 <= cfg["port"] <= 65535):
            errs.append("port 要在 1-65535 之间（现在是 %d）" % cfg["port"])

    url = str(cfg.get("comfy_url") or "")
    if not url.startswith(("http://", "https://")):
        errs.append("comfy_url 要以 http:// 或 https:// 开头（现在写的是 %r）" % (cfg.get("comfy_url"),))

    if errs:
        raise SystemExit("配置有问题，改一下 config.json：\n  - " + "\n  - ".join(errs))
    return cfg


def load():
    cfg = _merge(DEFAULTS, _read(_USER))
    for env, (key, cast) in ENV_MAP.items():
        v = os.environ.get(env)
        if v not in (None, ""):
            try:
                cfg[key] = cast(v)
            except (TypeError, ValueError):
                raise SystemExit("环境变量 %s 的值不合法：%r" % (env, v))
    cfg = _check(cfg)
    for k in _PATH_KEYS:                      # 相对路径 → 相对项目目录
        v = cfg.get(k)
        if v and not os.path.isabs(v):
            cfg[k] = os.path.abspath(os.path.join(HERE, v))
    return cfg


CFG = load()


def save_patch(patch):
    """把一小段配置合并写回 config.json（网页上改设置用）。

    只动传进来的那几个键，其余原样保留；写之前先备份一份 .bak。
    返回合并后的完整用户配置。
    """
    cur = _read(_USER)
    if not isinstance(cur, dict):
        cur = {}
    bak = _USER + ".bak"
    try:
        if os.path.isfile(_USER):
            with open(_USER, encoding="utf-8") as f:
                old = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
        merged = _merge(cur, patch or {})
        tmp = _USER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, _USER)          # 原子替换，写一半断电也不会留半个文件
        return merged
    except Exception as e:
        raise RuntimeError("写 config.json 失败：%s" % e)

