"""
cachereach — 从浏览器缓存中导出网页游戏的本机数据（localStorage）。

支持 Chrome / Edge / Chromium / Brave / Opera / Vivaldi / Firefox 等，
自动发现所有存有数据的站点，交互式选择后导出为可读报告 + 原始存档。
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _backend import read_leveldb_dirs, read_leveldb_dir, backend_info, SnappyError  # noqa: E402

# Windows 控制台强制 UTF-8，避免中文输出乱码
for _stream in ("stdout", "stderr"):
    _s = getattr(sys, _stream, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

APP = "cachereach"
VERSION = "1.1.0"


# ─────────────────────────────────────────────────────────────
# 3. localStorage 编解码
#    key   = "_" + origin + "\x00\x01" + <键名, UTF-8>
#    value = [类型字节] + 内容
#            0x00 -> 单字节 (Latin-1)
#            0x01 -> UTF-16LE
# ─────────────────────────────────────────────────────────────


def _latin1(b: bytes) -> str:
    return b.decode("latin-1", errors="replace")


def _score(s: str) -> float:
    """启发式评分：越像合法 JSON / 可打印文本，分数越高。"""
    if not s:
        return 0.0
    t = s.strip()
    sc = 0.0
    if t[:1] in "{[":
        sc += 100
    if t[:1] == '"' and t[-1:] == '"':
        sc += 60
    try:
        json.loads(s)
        sc += 200
    except Exception:
        pass
    printable = sum(1 for c in s if 0x20 <= ord(c) < 0x7F)
    sc += printable / len(s) * 120
    cjk = sum(1 for c in s if 0x4E00 <= ord(c) <= 0x9FFF)
    sc += cjk / len(s) * 40
    return sc


def decode_value(v: bytes) -> tuple[str, object]:
    """解码 localStorage value，返回 (编码方式, 值)。"""
    if not v:
        return "empty", ""
    t = v[0]
    body = v[1:]
    cands: list[tuple[str, str]] = []
    if t == 0x00:
        cands = [("latin1", _latin1(body)), ("utf16", _safe_utf16(body))]
    elif t == 0x01:
        cands = [("utf16", _safe_utf16(body)), ("latin1", _latin1(body))]
    else:
        cands = [
            ("latin1", _latin1(v)),
            ("utf16", _safe_utf16(v)),
            ("utf8", v.decode("utf-8", errors="replace")),
        ]
    best = max(cands, key=lambda c: _score(c[1]))
    return best[0], best[1]


def _safe_utf16(b: bytes) -> str:
    n = len(b) - (len(b) % 2)
    return b[:n].decode("utf-16-le", errors="replace")


ORIGIN_RE = re.compile(rb"(?:^|[\x00\x01])(https?://[^\x00\x01]{3,200})[\x00\x01]")


def extract_origin(key: bytes) -> str | None:
    m = ORIGIN_RE.search(key)
    if m:
        return m.group(1).decode("utf-8", errors="replace")
    # 兜底：键中直接含 http
    m2 = re.search(rb"(https?://[A-Za-z0-9\.\-:/]+)", key)
    if m2:
        return m2.group(1).decode("utf-8", errors="replace")
    return None


def storage_key_name(key: bytes, origin: str) -> str:
    ob = origin.encode("utf-8", errors="replace")
    i = key.find(ob)
    rest = key[i + len(ob):] if i >= 0 else key
    if rest[:1] == b"\x00":
        rest = rest[1:]
    if rest[:1] == b"\x01":
        rest = rest[1:]
    return rest.decode("utf-8", errors="replace")



# ─────────────────────────────────────────────────────────────
# 4. 浏览器发现
# ─────────────────────────────────────────────────────────────

def _home() -> str:
    return os.path.expanduser("~")


def discover_localstorage_sources() -> list[dict]:
    """扫描所有已知浏览器的 localStorage 目录，返回数据源列表。"""
    home = _home()
    appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    localapp = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))

    candidates: list[tuple[str, str]] = []
    # (浏览器名, profile 根目录)
    chromium_roots = [
        ("Chrome", os.path.join(localapp, "Google", "Chrome", "User Data")),
        ("Edge", os.path.join(localapp, "Microsoft", "Edge", "User Data")),
        ("Chromium", os.path.join(localapp, "Chromium", "User Data")),
        ("Brave", os.path.join(localapp, "BraveSoftware", "Brave-Browser", "User Data")),
        ("Vivaldi", os.path.join(localapp, "Vivaldi", "User Data")),
        ("Opera", os.path.join(appdata, "Opera Software", "Opera Stable")),
        ("Opera GX", os.path.join(appdata, "Opera Software", "Opera GX Stable")),
        ("Yandex", os.path.join(localapp, "Yandex", "YandexBrowser", "User Data")),
        ("QQ浏览器", os.path.join(appdata, "Tencent", "QQBrowser", "User Data")),
        ("360浏览器", os.path.join(appdata, "360se6", "User Data")),
        ("CentBrowser", os.path.join(localapp, "CentBrowser", "User Data")),
    ]
    for name, root in chromium_roots:
        if not os.path.isdir(root):
            continue
        profiles = []
        try:
            for e in sorted(os.listdir(root)):
                if e == "Default" or e.startswith("Profile "):
                    profiles.append(e)
        except OSError:
            continue
        for prof in profiles:
            ls_dir = os.path.join(root, prof, "Local Storage", "leveldb")
            if os.path.isdir(ls_dir):
                candidates.append((f"{name}/{prof}", ls_dir))
            # Opera GX 结构不同
            if name.startswith("Opera"):
                alt = os.path.join(root, prof, "Local Storage", "leveldb")
                if os.path.isdir(alt) and alt != ls_dir:
                    candidates.append((f"{name}/{prof}", alt))

    # 批量读取所有候选目录（一次 Node 调用，避免反复启动）
    dirs = sorted({d for _, d in candidates})
    stores = read_leveldb_dirs(dirs)
    norm = {os.path.normpath(k): v for k, v in stores.items()}

    sources = []
    for label, d in candidates:
        store = norm.get(os.path.normpath(d), [])
        if not store:
            continue
        # 按 origin 聚合（后端返回 [{o, k(b64), v(b64)}]）
        by_origin: dict[str, list[tuple[bytes, bytes]]] = defaultdict(list)
        for rec in store:
            kb = base64.b64decode(rec["k"])
            vb = base64.b64decode(rec["v"])
            by_origin[rec["o"]].append((kb, vb))
        for origin, items in by_origin.items():
            if not items:
                continue
            total = sum(len(v) for _, v in items)
            sources.append({
                "browser": label,
                "origin": origin,
                "path": d,
                "items": items,
                "count": len(items),
                "bytes": total,
            })
    sources.sort(key=lambda s: -s["bytes"])
    return sources


def discover_firefox_sources() -> list[dict]:
    """Firefox 的 localStorage 存在 webappsstore.sqlite。"""
    home = _home()
    appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    roots = [
        os.path.join(appdata, "Mozilla", "Firefox", "Profiles"),
        os.path.join(home, ".mozilla", "firefox"),
        os.path.join(home, "Library", "Application Support", "Firefox", "Profiles"),
    ]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            profs = sorted(os.listdir(root))
        except OSError:
            continue
        for prof in profs:
            db = os.path.join(root, prof, "webappsstore.sqlite")
            if not os.path.isfile(db):
                continue
            items = _read_firefox_db(db)
            if not items:
                continue
            by_origin: dict[str, list[tuple[bytes, bytes]]] = defaultdict(list)
            for origin, key, val in items:
                by_origin[origin].append((key.encode("utf-8"), val))
            for origin, kv in by_origin.items():
                out.append({
                    "browser": f"Firefox/{prof}",
                    "origin": origin,
                    "path": db,
                    "items": kv,
                    "count": len(kv),
                    "bytes": sum(len(v) for _, v in kv),
                })
    out.sort(key=lambda s: -s["bytes"])
    return out


def _read_firefox_db(db: str) -> list[tuple[str, str, bytes]]:
    """从 Firefox webappsstore.sqlite 读取 localStorage。"""
    tmp = None
    try:
        # 复制一份避免锁库
        fd, tmp = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        shutil.copy2(db, tmp)
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        cur.execute(
            "SELECT origin, key, value FROM webappsstore2 "
            "WHERE origin LIKE 'http%'"
        )
        rows = cur.fetchall()
        con.close()
    except (sqlite3.Error, OSError):
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return []
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    out = []
    for origin, key, value in rows:
        if not key:
            continue
        # Firefox 的 value 存储为 UTF-16LE（首字节为编码标记）
        vb = value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")
        out.append((origin, key, bytes(vb)))
    return out


# ─────────────────────────────────────────────────────────────
# 5. 通用导出
# ─────────────────────────────────────────────────────────────


def export_source(src: dict, outdir: str, pretty_names: bool = True) -> dict:
    """把一个数据源导出到目录，返回导出摘要。"""
    os.makedirs(outdir, exist_ok=True)
    data: dict[str, str] = {}
    encodings: dict[str, str] = {}
    for k, v in src["items"]:
        origin = src["origin"]
        name = storage_key_name(k, origin)
        if name.startswith("META"):
            continue
        enc, val = decode_value(v)
        data[name] = val
        encodings[name] = enc

    host = re.sub(r"^https?://", "", src["origin"]).replace("/", "_")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(outdir, f"{host}_{stamp}")

    written = []

    # 1) 完整原始快照
    p = base + "-localstorage.json"
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    written.append(p)

    # 2) 可读文本
    p = base + "-可读.txt"
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"来源: {src['origin']}\n")
        f.write(f"浏览器: {src['browser']}\n")
        f.write(f"导出时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"条目数: {len(data)}\n")
        f.write("=" * 60 + "\n\n")
        for k, v in data.items():
            f.write(f"### {k}   [编码: {encodings.get(k, '?')}, {len(v)} 字符]\n")
            f.write(v[:4000])
            if len(v) > 4000:
                f.write(f"\n...（截断，共 {len(v)} 字符）")
            f.write("\n\n")
    written.append(p)

    # 3) 逐项 JSON 文件（如果内容本身是 JSON）
    parsed = 0
    jsondir = base + "-json"
    json_items = []
    for k, v in data.items():
        if not isinstance(v, str) or not v.strip():
            continue
        try:
            obj = json.loads(v)
        except Exception:
            continue
        parsed += 1
        json_items.append((k, obj))
    if json_items:
        os.makedirs(jsondir, exist_ok=True)
        for k, obj in json_items:
            safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", k)[:80]
            with open(os.path.join(jsondir, f"{safe}.json"), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        written.append(jsondir)

    # 4) 索引表
    p = base + "-索引.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["键名", "编码", "字符数", "是否JSON", "预览"])
        for k, v in data.items():
            v = v if isinstance(v, str) else str(v)
            isj = ""
            if v.strip():
                try:
                    json.loads(v)
                    isj = "是"
                except Exception:
                    isj = "否"
            prev = v[:100].replace("\n", " ").replace("\r", " ")
            w.writerow([k, encodings.get(k, "?"), len(v), isj, prev])
    written.append(p)

    return {
        "base": base,
        "items": len(data),
        "json_items": parsed,
        "files": written,
        "data": data,
    }


# ─────────────────────────────────────────────────────────────
# 6. 卫戍协议专用报告（若检测到该游戏数据）
# ─────────────────────────────────────────────────────────────

GAME_KEY_STATS = "sp.pref.stats"
GAME_KEY_LOADOUT = "sp.pref.loadout"


def detect_game(data: dict) -> str | None:
    if any(k.startswith("sp.") for k in data):
        return "stronghold-protocol"
    return None


def _load_ref(src_root: str, name: str) -> dict:
    p = os.path.join(src_root, "data", f"{name}.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build_game_report(data: dict, outdir: str, ref_root: str | None) -> str | None:
    """生成《卫戍协议》专用 HTML 报告。"""
    stats_raw = data.get(GAME_KEY_STATS)
    if not stats_raw:
        return None
    try:
        stats = json.loads(stats_raw)
    except Exception:
        return None
    records = stats.get("records") or []
    if not records:
        return None

    ref_root = ref_root or ""
    chess = _load_ref(ref_root, "chess")
    items_t = _load_ref(ref_root, "items")
    stages_t = _load_ref(ref_root, "stages")
    bands_t = _load_ref(ref_root, "bands")

    CH = {k: (v.get("name") or k) for k, v in chess.items()}
    CH_T = {k: v.get("tier") for k, v in chess.items()}
    CH_G = {k: v.get("isGolden") for k, v in chess.items()}
    CH_P = {k: (v.get("subProfessionName") or v.get("profession") or "") for k, v in chess.items()}
    IT = {k: (v.get("name") or k) for k, v in items_t.items()}
    IT_T = {k: v.get("tier") for k, v in items_t.items()}
    ST = {k: (v.get("name") or k) for k, v in stages_t.items()}
    BD = {k: (v.get("name") or k) for k, v in bands_t.items()}

    def parse(k):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            try:
                return json.loads(v)
            except Exception:
                return None
        return None

    loadout = parse(GAME_KEY_LOADOUT) or {}
    skins = parse("sp.pref.skins") or {}
    diy = parse("sp.pref.diy") or {}
    own = parse("sp.pref.ownership") or {}
    settings = parse("sp.pref.settings") or {}
    recent = parse("sp.pref.recentRooms") or []
    player = data.get("sp.name") or data.get("ark.callsign") or "未知"

    def ts(ms):
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else "—"

    def dur(ms):
        if not ms:
            return "—"
        s = ms // 1000
        return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60}分{s%60:02d}秒"

    esc = html.escape
    total = len(records)
    wins = sum(1 for r in records if r.get("victory"))
    wr = (wins / total * 100) if total else 0

    char_ct, char_win, item_ct = Counter(), Counter(), Counter()
    for r in records:
        for p in r.get("players") or []:
            if p.get("left"):
                continue
            v = bool(p.get("victory")) if r.get("vs") else bool(r.get("victory"))
            for u in p.get("lineup") or []:
                cid = u.get("id")
                if not cid:
                    continue
                char_ct[cid] += 1
                if v:
                    char_win[cid] += 1
                for it in u.get("items") or []:
                    item_ct[it] += 1

    stage_stat = defaultdict(lambda: [0, 0])
    for r in records:
        nm = ST.get(r.get("stageId"), r.get("stageId") or "—")
        stage_stat[nm][0] += 1
        if r.get("victory"):
            stage_stat[nm][1] += 1

    rows = []
    for i, r in enumerate(sorted(records, key=lambda x: x.get("t") or 0), 1):
        res = "胜利" if r.get("victory") else ("战败" if r.get("end") == "settled" else "未结算")
        players = r.get("players") or []
        me = next((p for p in players if p.get("playerId") == r.get("selfId")), players[0] if players else {})
        lu = me.get("lineup") or []
        lu_txt = "、".join(f"{CH.get(u.get('id'), u.get('id'))}{'★' if u.get('golden') else ''}" for u in lu) or "—"
        eq = [x for u in lu for x in (u.get("items") or [])]
        eq_txt = "、".join(IT.get(x, x) for x in eq) or "—"
        bn = [b.get("bandId") for b in (me.get("bonds") or []) if b.get("active") and b.get("bandId")]
        rows.append(
            f"<tr><td class=num>{i}</td><td>{esc(ts(r.get('t')))}</td>"
            f"<td><span class='tag {'ok' if r.get('victory') else 'no'}'>{res}</span></td>"
            f"<td>{esc(ST.get(r.get('stageId'), r.get('stageId') or '—'))}</td>"
            f"<td>{esc(str(r.get('difficulty') or '—'))}</td>"
            f"<td>{esc(str(r.get('reason') or '—'))}</td>"
            f"<td>{r.get('roundsPassed') if r.get('roundsPassed') is not None else '—'}</td>"
            f"<td>{esc(dur(r.get('durationMs')))}</td>"
            f"<td>{esc(str(me.get('name') or player))}</td><td>{len(lu)}</td>"
            f"<td class=lu>{esc(lu_txt)}</td><td class=lu>{esc(eq_txt)}</td>"
            f"<td>{esc(str(me.get('lp') if me.get('lp') is not None else '—'))}</td>"
            f"<td>{esc(str(me.get('reward') if me.get('reward') is not None else '—'))}</td>"
            f"<td>{esc(', '.join(BD.get(x, x) for x in bn) or '—')}</td></tr>")

    def char_rows():
        return "\n".join(
            f"<tr><td>{esc(CH.get(c, c))}{'★' if CH_G.get(c) else ''}</td><td class=id>{esc(c)}</td>"
            f"<td class=num>{CH_T.get(c) or '—'}</td><td>{esc(str(CH_P.get(c) or '—'))}</td>"
            f"<td class=num>{n}</td><td class=num>{char_win.get(c, 0)}</td></tr>"
            for c, n in char_ct.most_common())

    def item_rows():
        return "\n".join(
            f"<tr><td>{esc(IT.get(i, i))}</td><td class=id>{esc(i)}</td>"
            f"<td class=num>{IT_T.get(i) or '—'}</td><td class=num>{n}</td></tr>"
            for i, n in item_ct.most_common())

    def stage_rows():
        return "\n".join(
            f"<tr><td>{esc(k)}</td><td class=num>{v[0]}</td><td class=num>{v[1]}</td>"
            f"<td class=num>{v[1]/v[0]*100:.0f}%</td></tr>"
            for k, v in sorted(stage_stat.items(), key=lambda x: -x[1][0]))

    entries = loadout.get("entries") or {}
    loadout_rows = "\n".join(
        f"<tr><td>{esc(CH.get(c, c))}{'★' if CH_G.get(c) else ''}</td><td class=id>{esc(c)}</td>"
        f"<td class=num>{v.get('skill') if v.get('skill') is not None else '—'}</td></tr>"
        for c, v in entries.items())
    skin_rows = "\n".join(
        f"<tr><td>{esc(CH.get(k, k))}</td><td class=id>{esc(k)}</td><td class=id>{esc(str(v))}</td></tr>"
        for k, v in (skins.get("choices") or {}).items())
    diy_rows = "\n".join(
        f"<tr><td class=id>{esc(k)}</td><td class=id>{esc(json.dumps(v, ensure_ascii=False))}</td></tr>"
        for k, v in (diy.get("picks") or {}).items())

    ref_note = "（未找到官方数据表，显示原始 ID）" if not chess else ""

    REPORT_CSS = """<style>
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 80px;background:#f6f7f9;color:#1c1e21;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:1500px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px}
.sub{color:#6b7280;font-size:13px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:28px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px 18px}
.card .k{color:#6b7280;font-size:12px;margin-bottom:4px}
.card .v{font-size:24px;font-weight:600}
.card .v small{font-size:13px;font-weight:400;color:#9ca3af}
h2{font-size:18px;margin:32px 0 12px;padding-bottom:8px;border-bottom:2px solid #e5e7eb}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-size:13px}
th{background:#f9fafb;text-align:left;padding:10px 11px;font-weight:600;border-bottom:1px solid #e5e7eb;white-space:nowrap;color:#374151}
td{padding:9px 11px;border-bottom:1px solid #f3f4f6;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.num{text-align:right;white-space:nowrap}
.id{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;word-break:break-all;max-width:260px}
.lu{max-width:420px;font-size:12px;color:#4b5563}
.tag{padding:2px 9px;border-radius:5px;font-size:12px;font-weight:600}
.tag.ok{background:#dcfce7;color:#166534}
.tag.no{background:#fee2e2;color:#991b1b}
.scroll{overflow-x:auto}
.note{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 15px;font-size:12.5px;color:#78350f;margin-bottom:24px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:12px}
</style>"""

    # 用列表 + % 拼接组装，避免 f-string 嵌套引号（兼容 Python 3.8+）
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    skin_choices = skins.get("choices") or {}
    skin_count = len(skin_choices)
    not_owned_txt = esc(", ".join(CH.get(x, x) for x in (own.get("notOwned") or [])) or "无")
    recent_txt = esc(", ".join(map(str, recent)) or "无")
    settings_txt = esc(json.dumps(settings, ensure_ascii=False))

    diy_block = ""
    if diy.get("picks"):
        diy_block = ("<h2>DIY 配置</h2><div class=scroll><table>"
                     "<tr><th>槽位</th><th>配置</th></tr>"
                     + diy_rows + "</table></div>")

    parts = []
    A = parts.append
    A('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">')
    A("<title>卫戍协议 · 数据导出</title>")
    A(REPORT_CSS)
    A("</head><body><div class=wrap>")
    A("<h1>卫戍协议 · 游玩数据导出</h1>")
    A("<div class=sub>玩家 <b>%s</b> · 导出于 %s</div>" % (esc(str(player)), now_str))
    A("<div class=note>⚠️ 本文件由 %s v%s 从浏览器缓存离线解析生成。名称映射%s。</div>"
      % (APP, VERSION, ref_note))

    A("<div class=cards>")
    A("<div class=card><div class=k>总对局数</div><div class=v>%d</div></div>" % total)
    A("<div class=card><div class=k>胜场</div><div class=v>%d</div></div>" % wins)
    A("<div class=card><div class=k>胜率</div><div class=v>%.1f<small>%%</small></div></div>" % wr)
    A("<div class=card><div class=k>干员预设</div><div class=v>%d</div></div>" % len(entries))
    A("<div class=card><div class=k>皮肤选择</div><div class=v>%d</div></div>" % skin_count)
    A("<div class=card><div class=k>使用干员</div><div class=v>%d</div></div>" % len(char_ct))
    A("</div>")

    A("<h2>对局明细</h2>")
    A("<div class=scroll><table><tr><th>#</th><th>时间</th><th>结果</th><th>关卡</th>"
      "<th>难度</th><th>原因</th><th>回合</th><th>时长</th><th>玩家</th><th>上场</th>"
      "<th>阵容</th><th>装备</th><th>LP</th><th>奖励</th><th>盟约</th></tr>")
    A("".join(rows))
    A("</table></div>")

    A("<h2>关卡战绩</h2><table><tr><th>关卡</th><th>场次</th><th>胜场</th><th>胜率</th></tr>")
    A(stage_rows())
    A("</table>")

    A("<h2>干员使用统计</h2><div class=scroll><table><tr><th>干员</th><th>ID</th>"
      "<th>星级</th><th>职业</th><th>次数</th><th>胜场</th></tr>")
    A(char_rows())
    A("</table></div>")

    A("<h2>装备使用统计</h2><div class=scroll><table><tr><th>装备</th><th>ID</th>"
      "<th>星级</th><th>次数</th></tr>")
    A(item_rows())
    A("</table></div>")

    A("<h2>干员技能预设（%d）</h2><div class=scroll><table><tr><th>干员</th>"
      "<th>ID</th><th>技能序号</th></tr>" % len(entries))
    A(loadout_rows)
    A("</table></div>")

    A("<h2>皮肤选择（%d）</h2><div class=scroll><table><tr><th>干员</th><th>ID</th>"
      "<th>皮肤</th></tr>" % skin_count)
    A(skin_rows)
    A("</table></div>")

    if diy_block:
        A(diy_block)

    A("<h2>其他设置</h2><table><tr><th>项目</th><th>内容</th></tr>")
    A("<tr><td>未拥有干员</td><td class=id>%s</td></tr>" % not_owned_txt)
    A("<tr><td>最近房间</td><td>%s</td></tr>" % recent_txt)
    A("<tr><td>设置</td><td class=id>%s</td></tr></table></div>" % settings_txt)
    A("<footer>%s v%s · 数据完整性与原存档一致</footer>" % (APP, VERSION))
    A("</div></body></html>")

    HTML = "".join(parts)

    p = os.path.join(outdir, "卫戍协议-数据导出.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(HTML)

    # 可再导入的原始存档
    with open(os.path.join(outdir, "stats-原始存档.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "loadout-干员预设.json"), "w", encoding="utf-8") as f:
        json.dump(loadout, f, ensure_ascii=False, indent=2)

    with open(os.path.join(outdir, "对局记录.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "时间", "结果", "关卡", "难度", "原因", "回合数", "时长",
                    "玩家", "上场干员数", "阵容", "装备", "LP", "奖励", "盟约"])
        for i, r in enumerate(sorted(records, key=lambda x: x.get("t") or 0), 1):
            res = "胜利" if r.get("victory") else ("战败" if r.get("end") == "settled" else "未结算")
            players = r.get("players") or []
            me = next((p for p in players if p.get("playerId") == r.get("selfId")),
                      players[0] if players else {})
            lu = me.get("lineup") or []
            eq = [x for u in lu for x in (u.get("items") or [])]
            bn = [BD.get(b.get("bandId"), b.get("bandId")) for b in (me.get("bonds") or [])
                  if b.get("active") and b.get("bandId")]
            w.writerow([i, ts(r.get("t")), res, ST.get(r.get("stageId"), r.get("stageId") or ""),
                        r.get("difficulty") or "", r.get("reason") or "",
                        r.get("roundsPassed") if r.get("roundsPassed") is not None else "",
                        dur(r.get("durationMs")), me.get("name") or player, len(lu),
                        "、".join(f"{CH.get(u.get('id'), u.get('id'))}{'★' if u.get('golden') else ''}" for u in lu),
                        "、".join(IT.get(x, x) for x in eq),
                        me.get("lp") if me.get("lp") is not None else "",
                        me.get("reward") if me.get("reward") is not None else "",
                        "、".join(map(str, bn))])

    with open(os.path.join(outdir, "干员预设.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["类型", "名称", "ID", "详情"])
        for c, v in entries.items():
            w.writerow(["技能预设", CH.get(c, c), c, f"技能序号 {v.get('skill')}"])
        for k, v in (skins.get("choices") or {}).items():
            w.writerow(["皮肤选择", CH.get(k, k), k, v])
        for k, v in (diy.get("picks") or {}).items():
            w.writerow(["DIY", CH.get(k, k), k, json.dumps(v, ensure_ascii=False)])

    return p


# ─────────────────────────────────────────────────────────────
# 7. CLI
# ─────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def order_sources(sources: list[dict]) -> list[dict]:
    """统一展示顺序：游戏站点优先，其余按数据量降序。

    返回的顺序即为列表中的编号顺序，供 --index / 交互选择使用。
    """
    games = [s for s in sources if detect_game_generic(s)]
    others = [s for s in sources if not detect_game_generic(s)]
    games.sort(key=lambda s: -s["bytes"])
    others.sort(key=lambda s: -s["bytes"])
    return games + others


def print_sources(sources: list[dict], limit: int = 30) -> None:
    if not sources:
        print("未发现任何 localStorage 数据。")
        return
    games = [s for s in sources if detect_game_generic(s)]
    print(f"\n发现 {len(sources)} 个数据源"
          f"（其中 {len(games)} 个检测到网页游戏数据 ★）\n")
    print(f"{'#':<5}{'浏览器':<21}{'站点':<46}{'条目':>6}  {'大小':>9}")
    print("─" * 92)
    for i, s in enumerate(sources, 1):
        name = s["browser"][:20]
        host = s["origin"].replace("https://", "").replace("http://", "")
        mark = "★" if detect_game_generic(s) else " "
        print(f"{i:<5}{name:<21}{host[:45]:<46}{s['count']:>6}  {_fmt_size(s['bytes']):>9}{mark}")
    if len(sources) > limit:
        print("─" * 92)
        print(f"（仅显示前 {limit} / {len(sources)} 行；用 --site <关键字> 精确筛选，"
              f"--all 导出全部）")
    print("\n★ = 检测到网页游戏数据（会额外生成可视化报告）")


def detect_game_generic(src: dict) -> bool:
    for k, _ in src["items"]:
        if b"sp.pref." in k or b"sp.matches" in k:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        prog=APP,
        description="从浏览器缓存中导出网页游戏的本机数据（对局记录 / 干员预设等）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  cachereach                      交互式：列出所有站点并选择导出
  cachereach --list               只列出，不导出
  cachereach --all                导出全部站点
  cachereach --site ark-proto    只导出匹配 "ark-proto" 的站点
  cachereach --all --out D:\\bak  指定输出目录（默认为脚本所在目录）
""")
    ap.add_argument("--list", action="store_true", help="仅列出发现的数据源")
    ap.add_argument("--all", action="store_true", help="导出全部数据源")
    ap.add_argument("--site", metavar="KEYWORD", help="只处理 origin 含该关键字的站点")
    ap.add_argument("--index", type=int, metavar="N", help="按列表序号直接导出")
    ap.add_argument("--out", metavar="DIR",
                    help="输出目录（默认：脚本所在目录）")
    ap.add_argument("--no-game-report", action="store_true", help="不生成游戏专用 HTML 报告")
    ap.add_argument("--ref-data", metavar="DIR",
                    help="游戏数据表目录（含 chess.json 等），用于中文名映射")
    ap.add_argument("--no-firefox", action="store_true", help="跳过 Firefox（较慢）")
    ap.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    ap.add_argument("-V", "--version", action="version", version=f"{APP} {VERSION}")
    args = ap.parse_args()

    # 默认输出 = 脚本所在目录
    outdir = args.out or os.path.dirname(os.path.abspath(sys.argv[0])) or os.getcwd()

    print(f"{APP} v{VERSION}  ·  解析后端: {backend_info()}")
    print("正在扫描浏览器 localStorage …")
    sources = discover_localstorage_sources()
    if not args.no_firefox:
        sources += discover_firefox_sources()
    sources = order_sources(sources)

    if not sources:
        print("\n未找到任何数据。\n"
              "请确认：\n"
              "  1) 浏览器确实玩过该网页游戏\n"
              "  2) 没有清理过浏览器缓存\n"
              "  3) 若游戏在无痕窗口中，数据不会落盘\n")
        return 1

    print_sources(sources)

    if args.list:
        return 0

    # 选定目标
    if args.site:
        targets = [s for s in sources if args.site.lower() in s["origin"].lower()]
        if not targets:
            print(f"\n没有匹配 “{args.site}” 的站点。")
            return 1
    elif args.all:
        targets = sources
    elif args.index:
        if args.index < 1 or args.index > len(sources):
            print(f"\n序号超出范围（1-{len(sources)}）。")
            return 1
        targets = [sources[args.index - 1]]
    else:
        # 交互选择
        print("\n输入要导出的序号（多个用逗号分隔，如 1,3；输入 q 退出）:")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0
        if raw.lower() in ("q", "quit", "exit", ""):
            print("已取消。")
            return 0
        try:
            idxs = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            print("输入无效。")
            return 1
        targets = [sources[i - 1] for i in idxs if 1 <= i <= len(sources)]
        if not targets:
            print("没有有效序号。")
            return 1

    print(f"\n将导出 {len(targets)} 个数据源到：{outdir}")
    if not args.yes:
        try:
            ok = input("确认继续？(Y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 0
        if ok not in ("", "y", "yes"):
            print("已取消。")
            return 0

    os.makedirs(outdir, exist_ok=True)
    print()
    for src in targets:
        host = re.sub(r"^https?://", "", src["origin"]).replace("/", "_")
        sub = os.path.join(outdir, host)
        print(f"── {src['origin']}  ({src['browser']})")
        try:
            res = export_source(src, sub)
        except Exception as ex:
            print(f"   ✗ 失败：{ex}")
            continue
        print(f"   ✓ {res['items']} 项数据（{res['json_items']} 项为 JSON）")
        for f in res["files"]:
            print(f"     - {os.path.relpath(f, outdir)}")

        if not args.no_game_report:
            ref = args.ref_data
            if not ref:
                # 自动探测常见位置
                for cand in (os.path.join(os.getcwd(), "sp-src"),
                             os.path.join(os.path.dirname(outdir), "sp-src"),
                             os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "sp-src")):
                    if os.path.isdir(os.path.join(cand, "data")):
                        ref = cand
                        break
            try:
                p = build_game_report(res["data"], sub, ref)
                if p:
                    extra = [
                        "卫戍协议-数据导出.html", "对局记录.csv",
                        "干员预设.csv", "stats-原始存档.json", "loadout-干员预设.json",
                    ]
                    shown = [x for x in extra if os.path.isfile(os.path.join(sub, x))]
                    print(f"   ✓ 游戏报告：{os.path.relpath(p, outdir)}")
                    for x in shown[1:]:
                        print(f"     - {os.path.relpath(os.path.join(sub, x), outdir)}")
            except Exception as ex:
                print(f"   ! 游戏报告生成失败：{ex}")

    print(f"\n全部完成。输出目录：{outdir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
