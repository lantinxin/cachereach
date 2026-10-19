"""
解析后端：优先使用 Node + classic-level（可靠、Windows 有预编译二进制），
若不可用则回退到纯 Python 的 Snappy/LevelDB 解析。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_READER = os.path.join(SCRIPT_DIR, "_read_ls.js")


class SnappyError(Exception):
    pass


def snappy_decompress(data: bytes) -> bytes:
    """纯 Python 实现 Snappy 解压（回退路径使用）。"""
    n = len(data)
    if n == 0:
        return b""
    pos = 0
    length = 0
    shift = 0
    while True:
        if pos >= n:
            raise SnappyError("varint 截断")
        b = data[pos]
        pos += 1
        length |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 32:
            raise SnappyError("varint 过长")

    out = bytearray()
    while pos < n and len(out) < length:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                if pos + extra > n:
                    raise SnappyError("literal 长度越界")
                ln = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            if pos + ln > n:
                raise SnappyError("literal 数据越界")
            out += data[pos:pos + ln]
            pos += ln
        else:
            if kind == 1:
                ln = 4 + ((tag >> 2) & 0x07)
                offset = ((tag >> 5) & 0x07) << 8 | data[pos]
                pos += 1
            elif kind == 2:
                ln = (tag >> 2) + 1
                offset = int.from_bytes(data[pos:pos + 2], "little")
                pos += 2
            else:
                ln = (tag >> 2) + 1
                offset = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
            if offset == 0 or offset > len(out):
                raise SnappyError("copy 偏移非法")
            start = len(out) - offset
            for i in range(ln):
                out.append(out[start + i])
    return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")
    return result, pos


def _parse_entries(block: bytes, limit: int) -> list[tuple[bytes, bytes]]:
    """解析 varint 编码的条目序列（前缀压缩）。"""
    entries: list[tuple[bytes, bytes]] = []
    pos = 0
    last_key = b""
    while pos < limit:
        try:
            shared, pos = _read_varint(block, pos)
            unshared, pos = _read_varint(block, pos)
            vlen, pos = _read_varint(block, pos)
        except (ValueError, IndexError):
            break
        if shared > len(last_key) or unshared > limit - pos or vlen > limit - pos - unshared:
            break
        key = last_key[:shared] + block[pos:pos + unshared]
        pos += unshared
        value = block[pos:pos + vlen]
        pos += vlen
        last_key = key
        entries.append((key, value))
    return entries


def _py_read_leveldb_dir(path: str, time_budget: float = 20.0) -> list[tuple[bytes, bytes]]:
    """纯 Python 回退：扫描 leveldb 目录，尽力取出条目。

    带时间预算，避免超大目录耗时过久（浏览器数据动辄几十 MB）。
    """
    import time as _time
    t0 = _time.time()
    store: dict[bytes, bytes] = {}
    if not os.path.isdir(path):
        return []
    try:
        files = sorted(os.listdir(path))
    except OSError:
        return []
    for fn in files:
        if _time.time() - t0 > time_budget:
            break
        if not fn.endswith(".ldb"):
            continue
        full = os.path.join(path, fn)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            continue
        n = len(data)
        i = 0
        while i < n - 6:
            if _time.time() - t0 > time_budget:
                break
            if data[i] == 1:
                r = _py_try_snappy_block(data, i)
                if r:
                    ents, nxt = r
                    for k, v in ents:
                        store[k] = v
                    i = max(nxt, i + 1)
                    continue
            i += 1
    return list(store.items())


def _py_try_snappy_block(data: bytes, type_pos: int):
    """尝试把 type_pos(压缩标记) 处当作一个 snappy 压缩块解析。"""
    n = len(data)
    pos = type_pos + 1
    shift = 0
    out_len = 0
    try:
        while True:
            if pos >= n:
                return None
            b = data[pos]
            pos += 1
            out_len |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift > 32:
                return None
        consumed = _py_snappy_consumed(data, pos, out_len)
        if not consumed:
            return None
        body = snappy_decompress(data[pos:pos + consumed])
        if len(body) != out_len:
            return None
        ents = _parse_entries(body, len(body))
        if not ents:
            return None
        # 合理性校验：至少有 key 含 http 或 META，否则视为误判
        if not any(b"http" in k or b"META" in k for k, _ in ents):
            return None
        return ents, pos + consumed
    except (SnappyError, ValueError, IndexError):
        return None


def _py_snappy_consumed(data: bytes, pos: int, out_len: int) -> int | None:
    n = len(data)
    out = 0
    start = pos
    while pos < n and out < out_len:
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                if pos + extra > n:
                    return None
                ln = int.from_bytes(data[pos:pos + extra], "little") + 1
                pos += extra
            if ln > n - pos:
                return None
            pos += ln
            out += ln
        else:
            if kind == 1:
                ln = 4 + ((tag >> 2) & 0x07)
                pos += 1
            elif kind == 2:
                ln = (tag >> 2) + 1
                pos += 2
            else:
                ln = (tag >> 2) + 1
                pos += 4
            if pos > n:
                return None
            out += ln
    if out < out_len:
        return None
    return pos - start


def _find_node() -> str | None:
    """定位可用的 node 可执行文件。"""
    cands = []
    node = shutil.which("node")
    if node:
        cands.append(node)
    # WorkBuddy 自带 Node
    node_root = os.path.expanduser("~/.workbuddy/binaries/node/versions")
    if os.path.isdir(node_root):
        try:
            for v in sorted(os.listdir(node_root), reverse=True):
                for exe in ("node.exe", "node"):
                    p = os.path.join(node_root, v, exe)
                    if os.path.isfile(p):
                        cands.append(p)
        except OSError:
            pass
    for c in cands:
        try:
            subprocess.run([c, "--version"], capture_output=True, timeout=10)
            return c
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _find_module_dir(mod: str = "classic-level") -> str | None:
    """查找包含该模块的 node_modules 目录。"""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".workbuddy", "binaries", "node", "workspace", "node_modules"),
        os.path.join(SCRIPT_DIR, "node_modules"),
        os.path.join(os.getcwd(), "node_modules"),
    ]
    # 也搜索用户目录下常见的全局位置
    roots.append(os.path.join(home, "node_modules"))
    for r in roots:
        if os.path.isdir(os.path.join(r, mod)):
            return r
    return None


_NODE_CACHE: str | None | bool = False
_CLASSIC_LEVEL_OK: bool | None = None
_MODULE_DIR: str | None = None


def _node_env() -> dict:
    """构造带 NODE_PATH 的环境变量，让 require 能找到 classic-level。"""
    env = dict(os.environ)
    paths = [p for p in (_MODULE_DIR, SCRIPT_DIR) if p]
    if env.get("NODE_PATH"):
        paths.append(env["NODE_PATH"])
    if paths:
        env["NODE_PATH"] = os.pathsep.join(paths)
    return env


def _node_backend_ready() -> bool:
    """检查 Node + classic-level 是否可用。"""
    global _NODE_CACHE, _CLASSIC_LEVEL_OK, _MODULE_DIR
    if _NODE_CACHE is False:
        _NODE_CACHE = _find_node()
    if not _NODE_CACHE:
        return False
    if _CLASSIC_LEVEL_OK is None:
        if not os.path.isfile(NODE_READER):
            _CLASSIC_LEVEL_OK = False
        else:
            _MODULE_DIR = _find_module_dir()
            probe = (
                "try{require('classic-level');process.stdout.write('ok')}"
                "catch(e){process.stdout.write('no')}"
            )
            try:
                r = subprocess.run(
                    [_NODE_CACHE, "-e", probe],
                    capture_output=True, timeout=60,
                    cwd=SCRIPT_DIR, env=_node_env(),
                )
                _CLASSIC_LEVEL_OK = r.stdout.decode("utf-8", "replace").strip() == "ok"
            except (OSError, subprocess.SubprocessError):
                _CLASSIC_LEVEL_OK = False
    return bool(_CLASSIC_LEVEL_OK)


def _snapshot_leveldb(path: str) -> str | None:
    """把 leveldb 目录复制一份，避免浏览器运行时文件被锁定。

    返回临时目录路径；失败返回 None。
    """
    if not os.path.isdir(path):
        return None
    tmp = tempfile.mkdtemp(prefix="cachereach_ls_")
    dst = os.path.join(tmp, "db")
    try:
        shutil.copytree(path, dst)
        # 移除锁文件，避免 LevelDB 认为目录被占用
        for junk in ("LOCK", "LOG.old"):
            jp = os.path.join(dst, junk)
            if os.path.exists(jp):
                try:
                    os.remove(jp)
                except OSError:
                    pass
        return dst
    except (OSError, shutil.Error):
        shutil.rmtree(tmp, ignore_errors=True)
        return None


def _run_node_read(paths: list[str]) -> dict | None:
    """调用 Node 读取给定目录，返回结果字典。失败返回 None。"""
    if not _node_backend_ready():
        return None
    try:
        r = subprocess.run(
            [_NODE_CACHE, NODE_READER] + paths,
            capture_output=True, timeout=900, cwd=SCRIPT_DIR, env=_node_env(),
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout.decode("utf-8"))
        return {os.path.normpath(k): v for k, v in data.items()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def read_leveldb_dirs(paths: list[str]) -> dict[str, list[dict]]:
    """批量读取多个 localStorage leveldb 目录，返回 {目录: [{o,k,v}...]}。

    先尝试直接读取；浏览器运行中被锁定的目录会自动改读快照副本。
    """
    if not paths:
        return {}

    result = _run_node_read(paths) or {}
    norm = {os.path.normpath(p): result.get(os.path.normpath(p), []) for p in paths}

    # 找出读不到内容的目录 → 改用快照重试
    empty = [p for p, v in norm.items() if not v]
    if empty:
        snaps: list[tuple[str, str]] = []
        tmpdirs: list[str] = []
        for p in empty:
            snap = _snapshot_leveldb(p)
            if snap:
                snaps.append((snap, p))
                tmpdirs.append(os.path.dirname(snap))
        if snaps:
            r2 = _run_node_read([s for s, _ in snaps])
            if r2:
                for snap, orig in snaps:
                    got = r2.get(os.path.normpath(snap), [])
                    if got:
                        norm[os.path.normpath(orig)] = got
            for t in tmpdirs:
                shutil.rmtree(t, ignore_errors=True)

    # 仍为空的走纯 Python 回退
    out: dict[str, list[dict]] = {}
    for p in paths:
        k = os.path.normpath(p)
        if norm.get(k):
            out[p] = norm[k]
        else:
            out[p] = _py_read_leveldb_dir_records(p)
    return out


def read_leveldb_dir(path: str, origin_filter: str = "") -> list[dict]:
    """读取单个 localStorage leveldb 目录。"""
    recs = read_leveldb_dirs([path]).get(os.path.normpath(path), [])
    if origin_filter:
        f = origin_filter.lower()
        recs = [r for r in recs if f in r["o"].lower()]
    return recs


def _py_read_leveldb_dir_records(path: str) -> list[dict]:
    """纯 Python 回退：把解析结果转成统一记录格式。"""
    import re as _re
    ORIGIN_RE = _re.compile(rb"(?:^|[\x00\x01])(https?://[^\x00\x01]{3,200})[\x00\x01]")
    out = []
    for k, v in _py_read_leveldb_dir(path):
        m = ORIGIN_RE.search(k) or _re.search(rb"(https?://[A-Za-z0-9\.\-:/]+)", k)
        if not m:
            continue
        out.append({
            "o": m.group(1).decode("utf-8", "replace"),
            "k": base64.b64encode(k).decode(),
            "v": base64.b64encode(v).decode(),
        })
    return out


def backend_info() -> str:
    if _node_backend_ready():
        return "Node + classic-level"
    return "纯 Python 回退模式"
