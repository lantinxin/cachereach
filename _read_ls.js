// 批量 LevelDB 读取后端 —— 由 cachereach.py 调用
// 用法: node _read_ls.js <dir1> [dir2 ...]
// 输出: JSON 对象 { "<dir>": [{o,k,v}...] }
const { ClassicLevel } = require('classic-level');

async function readOne(dir) {
  const db = new ClassicLevel(dir, { keyEncoding: 'buffer', valueEncoding: 'buffer' });
  const out = [];
  try {
    await db.open();
    for await (const [k, v] of db.iterator()) {
      const s = k.toString('latin1');
      // key 形如 "_https://host\x00\x01name" 或 "META:https://host"
      const m = s.match(/_?(https?:\/\/[A-Za-z0-9._\-]+(?::\d+)?)/);
      if (!m) continue;
      const origin = m[1];
      out.push({ o: origin, k: k.toString("base64"), v: v.toString("base64") });
    }
    await db.close();
  } catch (e) {
    // 被浏览器锁定或损坏：返回已读到的部分
    try { await db.close(); } catch (e2) {}
  }
  return out;
}

(async () => {
  const dirs = process.argv.slice(2);
  const result = {};
  for (const d of dirs) {
    result[d] = await readOne(d);
  }
  process.stdout.write(JSON.stringify(result));
})().catch(e => { process.stderr.write(String(e && e.message || e)); process.exit(1); });
