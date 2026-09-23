# -*- coding: utf-8 -*-
"""配置加载 —— 优先级：内置默认值 < config.example.json < config.json < 环境变量

config.json 是**本机私有**的（已在 .gitignore 里），放你自己的路径；
config.example.json 是给别人的模板。两者都缺时用内置默认值，服务照样起得来。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.join(HERE, "config.example.json")
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
    "prompt_help": {
        "enabled": False,
        "env_file": "",
        "endpoint": "https://opencode.ai/zen/go/v1/chat/completions",
        "model": "deepseek-v4.1-flash",
        "key_env": "OPENCODE_GO_API_KEY",
    },
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


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    cfg = dict(DEFAULTS)
    for p in (_EXAMPLE, _USER):
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = _merge(cfg, json.load(f))
            except Exception as e:
                print("[config] 读取失败 %s: %s" % (p, e), flush=True)
    for env, (key, cast) in ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            try:
                cfg[key] = cast(val)
            except Exception:
                pass
    return cfg


CFG = load()
