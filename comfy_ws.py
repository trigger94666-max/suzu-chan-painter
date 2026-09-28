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
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid
from collections import OrderedDict

# 帧长度上限：ComfyUI 发的都是很小的 JSON，正常远用不到。
# 设上限是为了防止异常/恶意服务端声称一个巨大的长度，把线程卡死在 recv 里。
MAX_FRAME = 2 * 1024 * 1024

# 状态机推导用的哨兵：区分「没传这个字段」和「传了这个字段但值是 None」。
# 因为 current_node=None 是有意义的（表示当前节点跑完了），不能被过滤掉。
_UNSET = object()

# 状态只能往前走，不能倒退（GPT 审出来的：terminal 之间互相覆盖会让页面
# 出现"明明跑完了又变回运行中"这种怪现象）。
_ORDER = {"queued": 0, "running": 1, "success": 2, "failed": 2, "cancelled": 2}


def _can_enter(cur, new):
    """cur -> new 这个状态迁移允不允许？"""
    if new is None or new == cur:
        return True
    if cur is None:
        return True
    a, b = _ORDER.get(cur, -1), _ORDER.get(new, -1)
    if a < 0 or b < 0:
        return True                     # 未知状态不拦，避免误伤
    return b > a                        # 同级别（terminal 之间）不许覆盖

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
            if len(head) > 65536:        # 头部异常大 -> 不是 WebSocket 服务
                raise ConnectionError("WS handshake: header too large")
        head, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest

        lines = head.split(b"\r\n")
        status = lines[0]
        # 严格匹配状态行，避免 body 里随便出现 "101" 就误判
        if not status.startswith(b"HTTP/1.1 101 "):
            raise ConnectionError("WS handshake failed: %s" % status[:80])

        # RFC 6455：必须校验服务端返回的 Accept（客户端按固定 GUID 算）
        want = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        got = None
        for ln in lines[1:]:
            if b":" in ln:
                k, _, v = ln.partition(b":")
                if k.strip().lower() == b"sec-websocket-accept":
                    got = v.strip().decode("latin-1")
                    break
        if got != want:
            raise ConnectionError("WS handshake: bad Sec-WebSocket-Accept (%r)" % (got,))
        return self

    # ---- 底层读写 ----
    def _recv_exact(self, n):
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                # 空闲超时不是错误！ComfyUI 在两个事件之间可能静默很久
                # （VAE 解码、模型换入换出等），把超时当成连接故障会疯狂
                # 重连，结果一整个任务的 progress 全丢。继续等就是了。
                continue
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
        """收一条完整文本消息。

        处理的协议约束（GPT 审出来的）：分片必须按 TEXT/CONT 顺序、
        控制帧不能分片且 ≤125 字节、帧长度有上限。
        """
        parts = []
        started = None                  # 当前分片消息的起始 opcode（None = 不在分片中）
        while True:
            b0, b1 = struct.unpack("!BB", self._recv_exact(2))
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._recv_exact(8))[0]
            if ln > MAX_FRAME:
                raise ConnectionError("WS frame too large: %d" % ln)
            mask = self._recv_exact(4) if masked else None
            data = self._recv_exact(ln) if ln else b""
            if mask:                    # 服务端本不该加掩码，这里宽容处理
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            # ---- 控制帧（0x8/0x9/0xA）：不可分片、payload ≤125 ----
            if opcode >= 0x8:
                if not fin:
                    raise ConnectionError("WS protocol error: fragmented control frame")
                if ln > 125:
                    raise ConnectionError("WS protocol error: control frame too long")
                if opcode == 0x8:
                    try:
                        self._send_frame(0x8, data[:125])   # 按 RFC 回一个 Close
                    except Exception:
                        pass
                    raise ConnectionError("WS closed by peer")
                if opcode == 0x9:
                    self._send_frame(0xA, data)
                    continue
                continue                # 0xA pong

            # ---- 数据帧：校验分片顺序 ----
            if opcode == 0x0:           # continuation
                if started is None:
                    raise ConnectionError("WS protocol error: continuation without start")
            elif opcode in (0x1, 0x2):
                if started is not None:
                    raise ConnectionError("WS protocol error: new message while fragmented")
                started = opcode
            else:
                raise ConnectionError("WS protocol error: unknown opcode 0x%x" % opcode)

            parts.append(data)
            if fin:
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
        self._ws = None                     # 当前连接（stop() 用它打断 recv）
        # ⚠️ client_id 必须**实例唯一且全程不变**，而且 HTTP 提交
        # （POST /prompt 的 client_id）必须用**同一个值**——
        # ComfyUI 里 progress 是广播的，但 execution_success / executed
        # 这些生命周期事件是按 client_id 定向发的。两边对不上，任务跑完
        # 也收不到"完成"，状态会永远停在 running、也拿不到图片名。
        self.client_id = uuid.uuid4().hex

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
        # 线程可能正阻塞在 recv 里，直接 shutdown 把这次读打断，
        # 否则要等下一条事件才醒来（可能几十秒）。
        ws = self._ws
        if ws is not None:
            try:
                if ws.sock:
                    ws.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

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
        """拿状态快照。锁内把可变字段复制出来，避免后台线程边改边被序列化。"""
        with self._lock:
            def _copy(j):
                d = dict(j)
                d["images"] = list(j.get("images") or [])
                return d
            if prompt_id:
                j = self._jobs.get(prompt_id)
                return _copy(j) if j else None
            return {k: _copy(v) for k, v in self._jobs.items()}

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
            ws = None                       # connect() 抛异常时 finally 不会 NameError
            try:
                url = "/ws?clientId=%s" % self.client_id   # 固定 id：必须和 HTTP 提交的一致
                ws = _WS(self.host, self.port, url).connect()
                self._ws = ws
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
                self._stop.wait(backoff)    # 用 wait 而不是 sleep：stop() 能立刻唤醒
                backoff = min(backoff * 2, 15.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._ws = None
        self._connected = False

    def _resync(self):
        """断线重连后：把 /queue 里的标 running/queued，/history 里的标终态。

        注意：这里**不能**直接赋值 status，必须过 _can_enter——
        否则会把 WS 已经收到的新状态（比如 success）倒退回去。
        """
        import urllib.request
        base = "http://%s:%d" % (self.host, self.port)

        def _bump(pid, st, **kw):
            """在锁内做一次"只前进"的状态更新。"""
            j = self._jobs.get(pid)
            if not j:
                return
            if not _can_enter(j.get("status"), st):
                return
            j["status"] = st
            for k, v in kw.items():
                if v is not _UNSET:
                    j[k] = v
            j["updated"] = time.time()

        try:
            with urllib.request.urlopen(base + "/queue", timeout=8) as r:
                q = json.loads(r.read().decode())
            with self._lock:
                for x in q.get("queue_running", []):
                    if len(x) > 1:
                        _bump(x[1], "running")
                for x in q.get("queue_pending", []):
                    if len(x) > 1:
                        _bump(x[1], "queued")
        except Exception:
            return
        try:
            with urllib.request.urlopen(base + "/history?max_items=50", timeout=8) as r:
                hist = json.loads(r.read().decode())
            for pid, rec in hist.items():
                st = (rec.get("status") or {})
                sstr = (st.get("status_str") or "").lower()
                err = self._extract_error(rec)
                images = self._extract_images(rec)
                if sstr == "success" or st.get("completed"):
                    tgt = "success"
                elif sstr == "error" or err:
                    tgt = "failed"
                else:
                    # history 里有记录、但既没成功也没报错——通常是服务重启或任务被
                    # 清掉了。这不能当成"执行失败"（会把中断的活儿冤枉成错误），
                    # 保持现状不动，等 WS 或下一次 resync 给准信。
                    continue
                with self._lock:
                    j = self._jobs.get(pid)
                    if not j or not _can_enter(j.get("status"), tgt):
                        continue
                    if tgt == "success":
                        _bump(pid, "success", progress=1.0, images=images)
                    else:
                        _bump(pid, "failed", error=err or "执行失败")
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

        elif t == "progress_state":
            # 新版 ComfyUI 还会发这个：一个 {node_id: {value, max, state}} 的字典。
            # 注意它的 value/max 是**节点内部**的粒度（很多节点 max=1），
            # 直接拿来当采样进度会把进度条打回 0——所以这里只用来认"当前在哪个
            # 节点"，进度数值仍然只信 "progress" 事件（单一真相源）。
            pid = d.get("prompt_id")
            nodes = d.get("nodes") or {}
            cur = None
            for nid, nd in nodes.items():
                if isinstance(nd, dict) and nd.get("state") == "running":
                    cur = nd.get("node_id") or nid
                    break
            kw = {"status": "running"}
            if cur is not None:
                kw["current_node"] = str(cur)
            self._set(pid, **kw)

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
                        # 断线重连后同一条 executed 可能再次到达：去重保序，
                        # 否则图墙里同一张图会出现两次。
                        cur = list(j.get("images") or [])
                        j["images"] = cur + [x for x in imgs if x not in cur]
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
            # 终态是整个冻结的：任务一旦进入终态，后续**任何**事件都不许再改它的字段。
            # ⚠️ 不能只在 kw 带 status 时才判断——不带 status 的事件（executing 的
            # current_node=None、裸 progress 等）照样能把数据写回去，页面就会出现
            # 「状态已经 success、进度条却又动起来」这种怪现象。
            cur = j.get("status")
            if cur in TERMINAL:
                return
            # 状态只许往前走（terminal 之间不许互相覆盖）。
            new_st = kw.get("status")
            if new_st is not None and not _can_enter(cur, new_st):
                return
            # 用 _UNSET 而不是 None 做过滤：current_node=None 是有意义的（节点跑完）
            j.update({k: v for k, v in kw.items() if v is not _UNSET})
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
