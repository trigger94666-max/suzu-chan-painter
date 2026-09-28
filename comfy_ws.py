#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comfy_ws.py —— 零依赖的 ComfyUI 进度监听（标准库 WebSocket 客户端）

为什么有这个东西：
    ComfyUI 的采样进度（"18/30"）只从 WebSocket 推，HTTP 侧只有
    /queue（队列里有几个）和 /history（完成的）。为了不引入第三方包，
    这里用 socket 手写最小 WebSocket 客户端。

职责边界（单一真相源）：
    本模块只做一件事——**把 ComfyUI 的 WS 事件翻译成任务状态**。
    HTTP 轮询（/queue、/history）只作为断线兜底，不在这里做。

    Job 状态机（对外唯一形态）：
        {"id": prompt_id, "status": "queued|running|success|failed|cancelled",
         "progress": 0..1, "step": n, "total": m,
         "current_node": str|None, "suffix": str, "seed": int,
         "images": [文件名...], "error": str|None, "updated": ts}

用法：
    mon = ProgressMonitor("127.0.0.1", 8188)
    mon.start()                       # 后台线程收听
    mon.track(prompt_id, suffix, seed)  # 提交后登记
    mon.snapshot()                    # 拿当前所有任务状态（dict）
    mon.snapshot(prompt_id)           # 拿单个
"""

import base64
import json
import os
import socket
import struct
import threading
import time
from collections import OrderedDict

# ─────────────────────── 最小 WebSocket 客户端 ───────────────────────


class _WS:
    """够用就好的 WebSocket 客户端：文本帧、ping/pong、分片、close。"""

    def __init__(self, host, port, path="/ws", timeout=10):
        self.host, self.port, self.path = host, port, path
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    # ---- 握手 ----
    def connect(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://%s:%d\r\n"
            "\r\n" % (self.path, self.host, self.port, key, self.host, self.port)
        )
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake: connection closed")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("WS handshake failed: %s" % head.split(b"\r\n")[0][:80])
        return self

    # ---- 底层读写 ----
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload=b""):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(header + mask + masked)

    def recv(self):
        """收一条完整文本消息（处理分片、回复 ping、自动跳过 pong）。"""
        parts = []
        while True:
            b0, b1 = struct.unpack("!BB", self._recv_exact(2))
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            data = self._recv_exact(ln) if ln else b""
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:      # close
                raise ConnectionError("WS closed by peer")
            if opcode == 0x9:      # ping -> pong
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:      # pong
                continue
            parts.append(data)
            if b0 & 0x80:          # FIN
                return b"".join(parts)

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


# ─────────────────────── 进度状态机 ───────────────────────

# 终态：到了就别再动
TERMINAL = ("success", "failed", "cancelled")


class ProgressMonitor:
    """后台线程收听 ComfyUI WS，维护 prompt_id -> job 状态。

    断线自动重连（指数退避，封顶 15s）；重连后用 /queue + /history 校准，
    避免漏掉断线期间的事件。
    """

    def __init__(self, host="127.0.0.1", port=8188, max_jobs=200):
        self.host, self.port = host, port
        self.max_jobs = max_jobs
        self._jobs = OrderedDict()          # prompt_id -> job dict
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._last_event = 0.0

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="comfy-ws", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    @property
    def connected(self):
        return self._connected

    # ---- 对外：登记 / 查询 ----
    def track(self, prompt_id, suffix="", seed=None):
        """提交成功后调用，登记一个任务为 queued。"""
        if not prompt_id:
            return
        with self._lock:
            j = self._jobs.get(prompt_id)
            if not j:
                j = self._new_job(prompt_id, suffix, seed)
                self._jobs[prompt_id] = j
                self._trim()
            else:
                j["suffix"] = suffix or j.get("suffix", "")
                if seed is not None:
                    j["seed"] = seed

    def mark_cancelled(self, prompt_id):
        with self._lock:
            j = self._jobs.get(prompt_id)
            if j and j["status"] not in TERMINAL:
                j["status"] = "cancelled"
                j["updated"] = time.time()

    def snapshot(self, prompt_id=None):
        with self._lock:
            if prompt_id:
                j = self._jobs.get(prompt_id)
                return dict(j) if j else None
            return {k: dict(v) for k, v in self._jobs.items()}

    def active(self):
        """还有没跑完的任务？"""
        with self._lock:
            return any(v["status"] not in TERMINAL for v in self._jobs.values())

    # ---- 内部 ----
    def _new_job(self, pid, suffix, seed):
        return {"id": pid, "status": "queued", "progress": 0.0, "step": 0, "total": 0,
                "current_node": None, "suffix": suffix or "", "seed": seed,
                "images": [], "error": None, "updated": time.time()}

    def _trim(self):
        # 超过上限时，从最老的终态任务开始丢
        if len(self._jobs) <= self.max_jobs:
            return
        for k in list(self._jobs.keys()):
            if len(self._jobs) <= self.max_jobs:
                break
            if self._jobs[k]["status"] in TERMINAL:
                del self._jobs[k]

    def _loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = _WS(self.host, self.port, "/ws?clientId=suzune-ui").connect()
                self._connected = True
                backoff = 1.0
                self._resync()                     # 重连后校准
                while not self._stop.is_set():
                    raw = ws.recv()
                    try:
                        msg = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    self._handle(msg)
            except Exception:
                self._connected = False
                if self._stop.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        self._connected = False

    def _resync(self):
        """断线重连后：把 /queue 里的标 running/queued，/history 里的标终态。"""
        import urllib.request
        base = "http://%s:%d" % (self.host, self.port)
        try:
            with urllib.request.urlopen(base + "/queue", timeout=8) as r:
                q = json.loads(r.read().decode())
            running = {x[1] for x in q.get("queue_running", []) if len(x) > 1}
            with self._lock:
                for x in q.get("queue_running", []):
                    if len(x) > 1 and x[1] in self._jobs:
                        self._jobs[x[1]]["status"] = "running"
                for x in q.get("queue_pending", []):
                    if len(x) > 1 and x[1] in self._jobs:
                        self._jobs[x[1]]["status"] = "queued"
                for pid, j in self._jobs.items():
                    if j["status"] == "queued" and pid in running:
                        j["status"] = "running"
        except Exception:
            return
        try:
            with urllib.request.urlopen(base + "/history?max_items=50", timeout=8) as r:
                hist = json.loads(r.read().decode())
            for pid, rec in hist.items():
                with self._lock:
                    j = self._jobs.get(pid)
                    if not j or j["status"] in TERMINAL:
                        continue
                st = (rec.get("status") or {})
                ok = st.get("status_str") == "success" or st.get("completed")
                images = self._extract_images(rec)
                with self._lock:
                    if ok:
                        j["status"] = "success"
                        j["progress"] = 1.0
                        j["images"] = images
                    else:
                        j["status"] = "failed"
                        j["error"] = self._extract_error(rec) or "执行失败"
                    j["updated"] = time.time()
        except Exception:
            return

    @staticmethod
    def _extract_images(rec):
        out = []
        for node_out in (rec.get("outputs") or {}).values():
            for im in node_out.get("images", []) or []:
                if im.get("filename"):
                    out.append(im["filename"])
        return out

    @staticmethod
    def _extract_error(rec):
        for m in (rec.get("status") or {}).get("messages", []) or []:
            if isinstance(m, list) and len(m) > 1 and m[0] in ("execution_error", "execution_interrupted"):
                d = m[1] if isinstance(m[1], dict) else {}
                return d.get("exception_message") or d.get("node_type") or m[0]
        return None

    def _handle(self, msg):
        t = msg.get("type")
        d = msg.get("data") or {}
        self._last_event = time.time()

        if t == "execution_start":
            self._set(d.get("prompt_id"), status="running")

        elif t == "execution_cached":
            # 部分节点命中缓存，不算进度，只保证任务在跑
            self._set(d.get("prompt_id"), status="running")

        elif t == "progress":
            pid = d.get("prompt_id")
            val, mx = d.get("value") or 0, d.get("max") or 0
            self._set(pid, status="running", step=val, total=mx,
                      progress=(val / mx if mx else 0.0),
                      current_node=d.get("node"))

        elif t == "executing":
            pid = d.get("prompt_id")
            node = d.get("node")
            if node is None:
                # 节点跑完，等 executed/history 落终态；先保持 running
                self._set(pid, current_node=None)
            else:
                self._set(pid, status="running", current_node=node)

        elif t == "executed":
            pid = d.get("prompt_id")
            out = d.get("output") or {}
            imgs = [im["filename"] for im in (out.get("images") or []) if im.get("filename")]
            if imgs:
                with self._lock:
                    j = self._jobs.get(pid)
                    if j:
                        j["images"] = list(j.get("images") or []) + imgs
                        j["updated"] = time.time()

        elif t == "execution_error":
            self._set(d.get("prompt_id"), status="failed",
                      error=d.get("exception_message") or d.get("node_type") or "执行错误")

        elif t == "execution_interrupted":
            self._set(d.get("prompt_id"), status="cancelled")

        elif t == "execution_success":
            self._set(d.get("prompt_id"), status="success", progress=1.0)

        elif t == "status":
            # 队列状态变化：把还在 queued 的按 queue_pending 顺序认领
            pass

    def _set(self, pid, **kw):
        if not pid:
            return
        with self._lock:
            j = self._jobs.get(pid)
            if not j:
                # WS 先于 track 到达（极少）：补一个任务
                j = self._new_job(pid, "", None)
                self._jobs[pid] = j
                self._trim()
            if j["status"] in TERMINAL and kw.get("status") not in TERMINAL:
                return
            j.update({k: v for k, v in kw.items() if v is not None})
            j["updated"] = time.time()


# ─────────────────────── 自测 ───────────────────────

if __name__ == "__main__":
    import sys
    mon = ProgressMonitor().start()
    print("listening on ws://%s:%d/ws" % (mon.host, mon.port))
    try:
        while True:
            time.sleep(1)
            snap = mon.snapshot()
            line = " | ".join(
                "%s %s %.0f%% %s" % (v["suffix"] or v["id"][:6], v["status"],
                                     (v["progress"] or 0) * 100, v["current_node"] or "")
                for v in snap.values()
            )
            sys.stdout.write("\r" + ("conn " if mon.connected else "DISC ") + (line or "(no jobs)"))
            sys.stdout.flush()
    except KeyboardInterrupt:
        mon.stop()
