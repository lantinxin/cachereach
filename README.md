# cachereach · 浏览器缓存数据导出工具

从浏览器的 **localStorage** 中导出网页游戏的本机数据（对局记录、干员预设、皮肤、设置等）。
用于服务器停服后抢救保存在浏览器里的存档。

> **⚠️ 隐私提示**
> 本工具通用读取 localStorage，其中可能包含你访问过的站点的**登录令牌与会话标识**。
> 导出文件请视为敏感文件妥善保管。
> 工具**只读**（绝不修改浏览器文件）、**无网络请求**、**不含第三方代码**。
> 请仅处理属于你自己的浏览器与数据 —— 分析他人数据在许多地区可能违法。
> 详见 [LICENSE](LICENSE)。

## 一、安装（首次使用）

**Windows —— 双击 `一键安装.bat` 即可**，无需任何命令行操作。

也可以用 PowerShell：

```powershell
.\setup-ps1.ps1              # 完整安装
.\setup-ps1.ps1 -Check       # 只自检，不改动文件
```

macOS / Linux：

```bash
python3 setup.py
```

安装脚本会自动完成：

1. 检测 Python（需 3.8+）
2. 创建虚拟环境 `.venv`（本工具零第三方依赖，venv 用于隔离与版本固定）
3. 检测 Node.js 与 npm
4. 安装 / 复用 `classic-level`（LevelDB 解析后端）
5. 环境自检 —— **真实扫描一次浏览器数据**，确认端到端可用
6. 生成启动器 `cachereach.bat`（Windows）/ `cachereach`（Unix）

其他选项：

```bash
python setup.py --check       # 只自检，不改动任何文件
python setup.py --force       # 重建 venv 与 node_modules
python setup.py --skip-venv   # 跳过虚拟环境，直接用当前 Python
```

安装脚本**可重复运行**，已完成的步骤会自动跳过。

> **关于中文显示**：如果你的控制台使用传统代码页（GBK/CP936），
> 脚本会自动把提示符号降级为 ASCII（`[OK]` / `[*]` / `[X]`）并切换英文文案，
> 避免乱码。终端本身支持 UTF-8 时则显示完整中文。

## 二、使用

```bash
cachereach.bat                 # 交互式：列出所有站点，选择导出
cachereach.bat --list          # 只列出，不导出
cachereach.bat --all           # 导出全部站点
cachereach.bat --site ark-proto  # 只导出匹配关键字的站点
```

macOS / Linux 用 `./cachereach`，参数完全相同。

常用参数：

| 参数 | 说明 |
|---|---|
| `--out DIR` | 输出目录，默认为脚本所在目录 |
| `--site KEYWORD` | 按站点关键字筛选 |
| `--index N` | 按列表序号导出 |
| `--no-firefox` | 跳过 Firefox 扫描（更快） |
| `--no-game-report` | 不生成游戏可视化报告 |
| `--ref-data DIR` | 指定游戏数据表目录，用于中文名映射 |
| `-y` | 跳过确认 |

## 三、文件构成

| 文件 | 作用 |
|---|---|
| `一键安装.bat` | **Windows 一键安装**（双击运行，GBK 编码） |
| `setup-ps1.ps1` | PowerShell 版安装脚本（兼容 5.1 与 7） |
| `setup.py` | 跨平台安装脚本（bat / ps1 最终都调用它） |
| `cachereach.py` | 主程序（CLI） |
| `_backend.py` | 解析层：Node 优先，纯 Python 回退 |
| `_read_ls.js` | Node 侧的 LevelDB 读取器 |
| `cachereach.bat` / `cachereach` | 日常使用的启动器（由 setup.py 生成） |

## 四、特点

- **多浏览器**：自动扫描 Chrome、Edge、Chromium、Brave、Vivaldi、Opera、Opera GX、
  Yandex、QQ 浏览器、360、CentBrowser、Firefox
- **多站点**：一次列出所有存过 localStorage 的网站，**游戏站点自动置顶并标 ★**
- **默认导出到脚本所在目录**，无需指定路径
- **零 pip 依赖**：只用 Python 标准库（Node 后端为可选加速项）


## 五、导出内容

**通用导出**（任何站点都适用）：

```
<站点名>/
  <站点名>_<时间戳>-localstorage.json   完整原始数据（可直接用于恢复）
  <站点名>_<时间戳>-可读.txt            逐项可读文本
  <站点名>_<时间戳>-索引.csv            键名/编码/大小/预览
  <站点名>_<时间戳>-json/               每个 JSON 值单独成文件
```

**游戏站点额外生成**（检测到 `sp.pref.*` 这类游戏数据键时）：

```
  卫戍协议-数据导出.html    可视化报告：对局明细、战绩、干员/装备统计
  对局记录.csv              对局明细，可用 Excel 打开
  干员预设.csv              干员技能预设 + 皮肤 + DIY 配置
  stats-原始存档.json       可在游戏内「导入」恢复
  loadout-干员预设.json     可在游戏内「导入」恢复
```

## 六、运行原理

网页游戏把本机数据存在 localStorage，落在：

```
%LOCALAPPDATA%\<浏览器>\User Data\<配置>\Local Storage\leveldb\
```

这是 LevelDB 格式，**内部数据块经 Snappy 压缩**，直接用文本工具 grep 只能得到乱码。

### 解析后端（重要）

工具会自动选择后端，并在启动时显示当前用的是哪种：

| 后端 | 状态 | 说明 |
|---|---|---|
| **Node + classic-level** | ✅ **推荐** | 完整可靠、速度快。需本机有 Node.js |
| 纯 Python 回退 | ⚠️ 尽力而为 | 无需 Node，但**可能读不全**，仅作应急 |

Node 后端会自动定位 Node 并设置 `NODE_PATH`，无需额外配置。若启动时显示
「纯 Python 回退模式」，建议装一个 Node.js 以获得完整数据：

```bash
npm install classic-level    # 在任意目录安装即可，工具会自动找到
```

### 关键处理

- **浏览器锁定**：浏览器运行时数据库被占用，工具会自动复制快照到临时目录再读，
  **无需关闭浏览器**（已实测 Edge 占用时仍能读出全部数据）。
- **编码判定**：值首字节标识编码（`0x00`=单字节，`0x01`=UTF-16LE），
  工具结合内容特征自动选择正确解码，避免中文乱码。

## 七、注意事项

- 若数据在**无痕窗口**中使用，关闭后不会落盘，无法导出。
- 清理过浏览器缓存（清除站点数据）会导致数据丢失。
- 部分站点数据量大（数 MB），全量导出需要一些时间。
- 工具只读取浏览器数据，不会修改或删除任何原始文件。

## 八、常见问题

**Q: 某个站点没出现在列表里？**
A: 先用 `--list` 确认；若确实缺失，可能是该浏览器配置不同，或数据在 IndexedDB
（本工具暂未覆盖 IndexedDB）。

**Q: 提示"纯 Python 回退模式"？**
A: 说明未找到 Node + classic-level。装个 Node.js 即可自动启用完整能力；
或 `npm install classic-level` 到任意目录后重新运行 setup.py。

**Q: 导出后中文是乱码？**
A: 用 `--out` 换个目录试试；若内容本身正常，说明是查看工具的编码问题
（CSV 为 UTF-8-BOM，Excel 可直接打开）。

**Q: 需要关闭浏览器吗？**
A: 不需要。工具会在数据库被锁定时自动复制快照再读。

## 九、与其他工具的区别

同类项目大致两条路线：**启动浏览器抓**（Playwright / 扩展）或 **离线读磁盘 LevelDB**。
以下是与几个代表性项目的对比（数据截至 2026-10）：

| 能力 | 本工具 | [chromium-reader](https://github.com/DJRHails/chromium-reader) | [CookieWrangler](https://github.com/s0meguy1/CookieWrangler) | [hindsight](https://github.com/ObsidianForensics/hindsight) |
|---|:---:|:---:|:---:|:---:|
| **浏览器运行时可读**（无需关闭） | ✅ | 未说明 | ❌ 需关闭 | ❌ 需关闭 |
| 多浏览器自动发现 | ✅ 11 种 | ❌ 单 profile | ⚠️ Chrome/Firefox | ⚠️ Chrome/Brave |
| 零 pip 依赖 | ✅ | ✅ | ❌ | ❌ |
| 需要 Node | ⚠️ 推荐 | ❌ 不需要 | ❌ | ❌ |
| 编码自动判定（UTF-16LE / Latin-1） | ✅ | ✅ | ⚠️ | ⚠️ |
| 游戏数据可视化报告 | ✅ | ❌ | ❌ | ❌ |
| 支持 IndexedDB | ❌ | ✅ | ❌ | ✅ |

**主要差异**：浏览器运行时数据库被 LOCK 锁定时，本工具自动复制快照再读取，
无需关闭浏览器（实测 Edge 运行中可读出全部 9667 条，直读则为 0 条）。

**已知短板**（诚实列出）：

- 纯 Python 回退后端经过实测**可能读不全**，完整能力依赖 Node + classic-level
- 不支持 IndexedDB（部分游戏用 IndexedDB 而非 localStorage）
- 只读 localStorage，不涉及 cookie / 密码 / 历史记录

## 十、参考与许可证

同类项目：

- [chromium-reader](https://pypi.org/project/chromium-reader/) — 纯 Python 读 Chromium 存储格式
- [CookieWrangler](https://github.com/s0meguy1/CookieWrangler) — 导出导入 cookie 与 localStorage
- [hindsight](https://github.com/ObsidianForensics/hindsight) — Chrome 取证工具
- [session-capture](https://github.com/super-rick/session-capture) — Playwright 抓登录态

许可证：MIT（见 [LICENSE](LICENSE)）
