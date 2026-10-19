#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — cachereach 一键环境安装

做什么：
  1. 检测 Python（要求 3.8+）
  2. 创建 Python 虚拟环境 .venv（本工具零第三方依赖，venv 用于隔离与版本固定）
  3. 检测 Node.js；没有则提示安装方式
  4. 安装/复用 classic-level（LevelDB 解析后端，Windows 有预编译二进制）
  5. 自检：验证后端可加载、能真正读出浏览器数据
  6. 生成启动器 cachereach.bat（Windows）/ cachereach（Unix）

用法：
  python setup.py            # 完整安装
  python setup.py --check    # 只做环境自检，不改动任何文件
  python setup.py --force    # 重建 venv 与 node_modules
  python setup.py --skip-venv  # 跳过 venv，直接用当前 Python
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(HERE, ".venv")
NODE_MODULES = os.path.join(HERE, "node_modules")
IS_WIN = os.name == "nt"

MIN_PY = (3, 8)

def _configure_stdio() -> None:
    """Make output safe on legacy consoles (GBK/CP936) and piped hosts.

    A child process may inherit a code page that cannot round-trip the text
    we print. Try to move stdout to UTF-8 first; if that is impossible,
    degrade to ASCII markers so the output stays legible instead of mojibake.

    CACHEREACH_ASCII=1 forces the ASCII markers. Use it when the parent
    process captures our output through a pipe and may decode it with a
    different code page than we encode with.
    """
    global _ASCII_MODE
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is not None and hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    _ASCII_MODE = bool(os.environ.get("CACHEREACH_ASCII")) or (
        ("utf" not in enc) and enc != "")


_ASCII_MODE = False
_configure_stdio()

# In ASCII mode we also avoid non-ASCII text: a host that decodes our bytes
# with a different code page would render Chinese as mojibake too.
_T = {}


def t(key: str, default: str) -> str:
    """Return an ASCII-safe string when running in ASCII mode."""
    if _ASCII_MODE:
        return _T.get(key, default)
    return default


C = {
    "r": "\033[0m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
    "red": "\033[31m", "c": "\033[36m", "dim": "\033[2m",
}

# Non-ASCII markers cannot be encoded on a legacy console code page
# (GBK/CP936) and would turn into mojibake. Fall back to ASCII markers so
# the output stays readable everywhere.
MARK = {
    "step": "[*]" if _ASCII_MODE else "\u25b6",   # right triangle
    "ok": "[OK]" if _ASCII_MODE else "\u2713",    # check mark
    "warn": "[!]",
    "err": "[X]" if _ASCII_MODE else "\u2717",    # ballot X
}


def color(txt: str, key: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return txt
    return f"{C.get(key,'')}{txt}{C['r']}"


def step(msg: str) -> None:
    print(color(f"\n{MARK['step']} {msg}", "b"))


def ok(msg: str) -> None:
    print(color(f"  {MARK['ok']} {msg}", "g"))


def warn(msg: str) -> None:
    print(color(f"  {MARK['warn']} {msg}", "y"))


def err(msg: str) -> None:
    print(color(f"  {MARK['err']} {msg}", "red"))


def info(msg: str) -> None:
    print(color(f"    {msg}", "dim"))


# Compact English equivalents for the messages that matter, used when the
# host may decode our bytes with a code page that cannot represent Chinese.
E = {
    "banner": "cachereach setup",
    "banner2": "Export local game data from browser cache",
    "check_py": "Checking Python",
    "py_low": "Python is too old",
    "check_venv": "Preparing Python virtual environment",
    "venv_skip": "Already present, skipped",
    "venv_fail": "Failed to create venv",
    "venv_made": "Created",
    "check_node": "Checking Node.js",
    "no_node": "Node.js not found",
    "npm_ok": "npm available",
    "no_npm": "npm not found",
    "check_mod": "Preparing LevelDB backend (classic-level)",
    "mod_ok": "classic-level is available",
    "mod_missing": "classic-level is not available",
    "mod_done": "classic-level installed",
    "selfcheck": "Environment self-check",
    "backend": "Parsing backend",
    "found_src": "Found {} data sources, {} game sites",
    "no_src": "No data sources found",
    "launchers": "Generating launchers",
    "done": "Setup finished successfully",
    "problem": "Setup reported a problem",
    "next": "Next:",
    "how_list": "list all sites",
    "how_run": "interactive export",
    "how_all": "export all",
}


def L(key: str, zh: str) -> str:
    """English text in ASCII mode, the Chinese original otherwise."""
    return E.get(key, zh) if _ASCII_MODE else zh


# ── 1. Python 检测 ────────────────────────────────────────────
def check_python() -> bool:
    step(L("check_py", "检查 Python"))
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v[:2] < MIN_PY:
        err(L("py_low", f"Python {ver} 过低") + f" (need {MIN_PY[0]}.{MIN_PY[1]}+)")
        info("请到 https://www.python.org/downloads/ 安装新版 Python")
        return False
    ok(f"Python {ver}  ({sys.executable})")
    return True


# ── 2. venv 创建 ──────────────────────────────────────────────
def venv_python(root: str = VENV_DIR) -> str:
    if IS_WIN:
        return os.path.join(root, "Scripts", "python.exe")
    return os.path.join(root, "bin", "python")


def setup_venv(force: bool = False) -> str | None:
    step(L("check_venv", "准备 Python 虚拟环境"))
    py = venv_python()
    if force and os.path.isdir(VENV_DIR):
        info("删除旧的 .venv …")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    if os.path.isfile(py):
        ok(L("venv_skip", "已存在，跳过") + f": {VENV_DIR}")
        return py

    info("创建 .venv …")
    try:
        r = subprocess.run([sys.executable, "-m", "venv", VENV_DIR],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        err(f"创建失败：{e}")
        return None
    if r.returncode != 0 or not os.path.isfile(py):
        err(L("venv_fail", "创建 venv 失败"))
        if r.stderr:
            info(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
        warn("部分精简版 Python 缺少 venv 模块，将直接使用当前解释器")
        return None
    ok(L("venv_made", "已创建") + f": {VENV_DIR}")
    return py


# ── 3. Node 检测 ──────────────────────────────────────────────
def find_node() -> str | None:
    """定位 node 可执行文件：PATH 优先，再找常见安装位置。"""
    n = shutil.which("node")
    if n:
        return n
    home = os.path.expanduser("~")
    cands = []
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        cands += [
            os.path.join(prog, "nodejs", "node.exe"),
            os.path.join(local, "Programs", "nodejs", "node.exe"),
            os.path.join(prog, "nodejs", "node.exe"),
        ]
        # nvm / fnm 等版本管理器
        for root in (os.path.join(local, "nvm"), os.path.join(home, ".nvm"),
                     os.path.join(local, "Volta", "bin")):
            if os.path.isdir(root):
                for dirpath, _dirs, files in os.walk(root):
                    for f in files:
                        if f == "node.exe":
                            cands.append(os.path.join(dirpath, f))
                            break
                    if len(cands) > 40:
                        break
    else:
        cands += ["/usr/local/bin/node", "/usr/bin/node",
                  "/opt/homebrew/bin/node", os.path.join(home, ".nvm/current/bin/node")]
        nvm = os.path.join(home, ".nvm", "versions", "node")
        if os.path.isdir(nvm):
            for d in sorted(os.listdir(nvm), reverse=True):
                cands.append(os.path.join(nvm, d, "bin", "node"))
    for c in cands:
        if c and os.path.isfile(c) and not c.endswith(".cmd"):
            return c
    return None


def find_npm(node: str) -> str | None:
    """定位 npm。Node 安装目录下通常与 node 同级。"""
    n = shutil.which("npm")
    if n:
        return n
    d = os.path.dirname(node)
    for name in ("npm.cmd", "npm") if IS_WIN else ("npm",):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def setup_node() -> tuple[str | None, str | None]:
    step(L("check_node", "检查 Node.js"))
    node = find_node()
    if not node:
        err(L("no_node", "未找到 Node.js"))
        info("classic-level 需要 Node.js 才能加载（它是原生模块，含预编译二进制）")
        print()
        print(color("    安装方式：", "b"))
        print("      Windows : https://nodejs.org  -> 下载 LTS 安装包，一路下一步")
        print("      macOS   : brew install node")
        print("      Linux   : sudo apt install nodejs  或使用 nvm")
        print()
        print(color("    不装 Node 也能用：", "b"))
        print("      工具会自动降级为「纯 Python 回退模式」，但可能读不全数据。")
        print("      若只是临时导出一份，建议还是装上 Node。")
        return None, None

    try:
        r = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=30)
        ver = r.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        ver = "?"
    ok(f"Node {ver}  ({node})")

    npm = find_npm(node)
    if npm:
        ok(L("npm_ok", "npm 可用") + f" ({npm})")
    else:
        warn(L("no_npm", "未找到 npm"))
        info("可手动安装 classic-level，或让工具使用纯 Python 回退模式")
    return node, npm


# ── 4. classic-level 安装 ─────────────────────────────────────
def check_module_local() -> bool:
    return os.path.isdir(os.path.join(NODE_MODULES, "classic-level"))


def module_available() -> bool:
    """classic-level 是否已能在本机被 Node 加载（不限于本目录）。"""
    node = find_node()
    if not node:
        return False
    env = dict(os.environ)
    paths = [p for p in (NODE_MODULES, os.path.dirname(node)) if p]
    if env.get("NODE_PATH"):
        paths.append(env["NODE_PATH"])
    if paths:
        env["NODE_PATH"] = os.pathsep.join(paths)
    probe = ("try{require('classic-level');process.stdout.write('ok')}"
             "catch(e){process.stdout.write('no')}")
    try:
        r = subprocess.run([node, "-e", probe], capture_output=True,
                           timeout=60, cwd=HERE, env=env)
        return r.stdout.decode("utf-8", "replace").strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


def setup_classic_level(node: str | None, npm: str | None, force: bool,
                        check_only: bool = False) -> bool:
    step(L("check_mod", "准备 LevelDB 解析后端（classic-level）"))
    if check_only:
        if module_available():
            ok(L("mod_ok", "classic-level 已可用（无需改动）"))
        else:
            warn(L("mod_missing", "classic-level 不可用"))
            info("运行 python setup.py 可自动安装")
        return module_available()

    if force and os.path.isdir(NODE_MODULES):
        info("删除旧的 node_modules …")
        shutil.rmtree(NODE_MODULES, ignore_errors=True)

    if check_module_local():
        ok(L("mod_ok", "已安装于本目录 node_modules，跳过"))
        return True

    # 本机其他位置已可用时，不必重复安装
    if not force and module_available():
        ok(L("mod_ok", "classic-level 已在本机可用，无需重复安装"))
        info("若想让它随工具目录一起分发，可加 --force 强制装到 node_modules")
        return True

    if not (node and npm):
        warn("缺少 Node 或 npm，无法安装")
        info("稍后装好 Node 后重新运行本脚本即可；或直接使用纯 Python 回退模式")
        return False

    info("安装 classic-level（首次约需 10-30 秒）…")
    env = dict(os.environ)
    try:
        r = subprocess.run(
            [npm, "install", "classic-level", "--no-audit", "--no-fund", "--loglevel=error"],
            cwd=HERE, capture_output=True, text=True, timeout=900, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        err(f"安装失败：{e}")
        return False

    if check_module_local():
        ok(L("mod_done", "classic-level 安装完成"))
        return True
    err("安装未成功")
    if r.stderr:
        for line in r.stderr.strip().splitlines()[-4:]:
            info(line)
    info("可尝试手动执行：" + f"{npm} install classic-level")
    return False


# ── 5. 后端自检 ───────────────────────────────────────────────
def run_selfcheck(py: str) -> bool:
    step(L("selfcheck", "环境自检"))
    code = textwrap.dedent("""
        import sys, os
        sys.path.insert(0, %r)
        for s in ('stdout','stderr'):
            st = getattr(sys, s, None)
            if st and hasattr(st,'reconfigure'):
                try: st.reconfigure(encoding='utf-8', errors='replace')
                except Exception: pass
        import _backend as B
        print('BACKEND=' + B.backend_info())
        print('NODE=' + ('yes' if B._NODE_CACHE else 'no'))
        print('MODULE=' + ('yes' if B._CLASSIC_LEVEL_OK else 'no'))
        # 真正读一次浏览器数据，确认端到端可用
        srcs = []
        try:
            import cachereach as C
            srcs = C.discover_localstorage_sources()
        except Exception as e:
            print('SCAN_ERR=' + str(e)[:120])
        print('SOURCES=' + str(len(srcs)))
        games = [s for s in srcs if C.detect_game_generic(s)] if srcs else []
        print('GAMES=' + str(len(games)))
    """) % HERE
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True,
                           timeout=900, cwd=HERE)
    except (OSError, subprocess.SubprocessError) as e:
        err(f"自检执行失败：{e}")
        return False

    out = r.stdout or ""
    kv = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    backend = kv.get("BACKEND", "?")
    if backend.startswith("Node"):
        ok(L("backend", "解析后端") + f": {backend}")
    else:
        warn(L("backend", "解析后端") + f": {backend}")
        info("数据可能读不全。建议安装 Node.js 后重新运行本脚本。")

    if kv.get("SOURCES", "0").isdigit():
        n = int(kv["SOURCES"])
        if n > 0:
            games = kv.get("GAMES", "0")
            if _ASCII_MODE:
                ok(f"Found {n} data sources, {games} game sites")
            else:
                ok(f"扫描到 {n} 个数据源，{games} 个游戏站点")
        else:
            warn(L("no_src", "未扫描到任何数据源"))
            info("若你确实在浏览器玩过网页游戏，请确认数据未被清理")
    if kv.get("SCAN_ERR"):
        err("扫描出错：" + kv["SCAN_ERR"])
    return backend.startswith("Node")


# ── 6. 启动器 ────────────────────────────────────────────────
def write_launchers() -> None:
    step(L("launchers", "生成启动器"))

    # Windows 批处理
    bat = os.path.join(HERE, "cachereach.bat")
    with open(bat, "w", encoding="gbk", errors="replace") as f:
        f.write(
            "@echo off\r\n"
            "rem cachereach 启动器（由 setup.py 生成）\r\n"
            "cd /d \"%~dp0\"\r\n"
            f"set \"PY={venv_python()}\"\r\n"
            "if not exist \"%PY%\" set \"PY=python\"\r\n"
            "\"%PY%\" cachereach.py %*\r\n"
        )
    ok(L("done", "已生成 cachereach.bat（双击或命令行运行）"))

    # Unix 脚本
    sh = os.path.join(HERE, "cachereach")
    with open(sh, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/sh\n"
            "# cachereach 启动器（由 setup.py 生成）\n"
            'cd "$(dirname "$0")" || exit 1\n'
            f'PY="{venv_python()}"\n'
            '[ -x "$PY" ] || PY=python3\n'
            'exec "$PY" cachereach.py "$@"\n'
        )
    try:
        os.chmod(sh, 0o755)
    except OSError:
        pass
    if not IS_WIN:
        ok("已生成 cachereach（Unix）")


# ── 主流程 ───────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="cachereach 一键环境安装",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只自检，不改动文件")
    ap.add_argument("--force", action="store_true", help="重建 venv 与 node_modules")
    ap.add_argument("--skip-venv", action="store_true", help="跳过虚拟环境")
    args = ap.parse_args()

    print(color("=" * 62, "dim"))
    print(color("  " + L("banner", "cachereach 环境安装"), "b"))
    print(color("  " + L("banner2", "从浏览器缓存导出网页游戏本机数据"), "dim"))
    print(color("=" * 62, "dim"))

    if not check_python():
        return 1

    if args.check:
        node, npm = setup_node()
        setup_classic_level(node, npm, force=False, check_only=True)
        run_selfcheck(sys.executable)
        print()
        return 0

    py = sys.executable
    if not args.skip_venv:
        created = setup_venv(args.force)
        if created:
            py = created
        else:
            warn("使用当前 Python 解释器")
    else:
        info("按要求跳过虚拟环境")

    node, npm = setup_node()
    has_mod = setup_classic_level(node, npm, args.force)
    write_launchers()
    good = run_selfcheck(py)

    # 收尾
    print(color("\n" + "=" * 62, "dim"))
    if good:
        print(color("  安装完成 — 解析后端工作正常", "g"))
    else:
        print(color("  安装完成，但使用了降级后端", "y"))
    print(color("=" * 62, "dim"))
    print()
    print("接下来：")
    if IS_WIN:
        print('  列出所有站点      ' + color('cachereach.bat --list', 'c'))
        print('  交互式导出        ' + color('cachereach.bat', 'c'))
    else:
        print('  列出所有站点      ' + color('./cachereach --list', 'c'))
        print('  交互式导出        ' + color('./cachereach', 'c'))
    print(f'  导出全部          ' + color(
        'cachereach.bat --all' if IS_WIN else './cachereach --all', 'c'))
    print()
    if not good:
        print(color("  提示：", "y"))
        print("  当前为纯 Python 回退模式，可能读不全 LevelDB 数据。")
        print("  装好 Node.js 后重新运行 python setup.py 即可获得完整能力。")
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
