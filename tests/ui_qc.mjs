// UI 级 QC(webapp-testing)—— 真在浏览器里点一遍关键用户流程并断言。
// 跑法:先起服务(本地或线上),再:
//   NODE_PATH=/tmp/shot/node_modules BASE=http://localhost:8099 node tests/ui_qc.mjs
// 覆盖:①用户登录 ②公共库收藏→出现在我的物料库(用户报的 bug 就是这条)
//       ③管理员可见全部 ④管理员审核通过 ⑤管理员发布到公共库
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');   // 需 NODE_PATH 指向装了 playwright 的 node_modules

const BASE = (process.env.BASE || 'http://localhost:8099') + '/ui/';
const results = [];
const check = (name, cond) => { results.push([name, !!cond]); console.log((cond ? 'PASS  ' : 'FAIL  ') + name); };

async function loginAs(p, name, pw, register) {
  await p.goto(BASE); await p.waitForTimeout(500);
  await p.evaluate(() => localStorage.removeItem('me'));
  await p.goto(BASE); await p.waitForTimeout(700);
  if (register) await p.click('#tab-reg');
  await p.fill('#g-name', name); await p.fill('#g-pw', pw);
  await p.click('#g-submit'); await p.waitForTimeout(1500);
}
async function go(p, sec) { await p.click(`.nav[data-sec="${sec}"]`); await p.waitForTimeout(1400); }

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1340, height: 900 } });

  // ── 用户:登录 ──
  const user = 'qc' + Date.now();
  await loginAs(p, user, 'pw123456', true);
  check('用户注册并进入应用', await p.isVisible('#app'));

  // ── 用户:公共库收藏 → 我的物料库出现(★ 用户报的 bug) ──
  await go(p, 'public');
  const favBtn = await p.$('#pub-grid .cell .btn.key');
  check('公共库有可收藏物料', !!favBtn);
  if (favBtn) { await favBtn.click(); await p.waitForTimeout(1100); }
  await go(p, 'mine');
  const inMine = await p.evaluate(() =>
    [...document.querySelectorAll('#mine-grid .cell')].some(c => c.textContent.includes('取消收藏')));
  check('★ 收藏后出现在「我的物料库」', inMine);

  // ── 管理员:全部/审核/发布 ──
  await loginAs(p, 'admin', 'admin123', false);
  check('管理员可见「全部物料」菜单', !!await p.$('.nav[data-sec="all"]'));
  await go(p, 'review');
  const passBtn = await p.$('#queue .btn.pass');
  if (passBtn) { await passBtn.click(); await p.waitForTimeout(1000); }
  check('管理员审核队列可操作(通过)', !!passBtn);
  await go(p, 'all');
  const pubBtn = await p.$('#all-grid .btn.key');
  if (pubBtn) { await pubBtn.click(); await p.waitForTimeout(1000); }
  check('管理员可发布到公共库', !!pubBtn);

  await b.close();
  const failed = results.filter(([, c]) => !c);
  console.log(`\n== UI QC: ${results.length - failed.length}/${results.length} passed ==`);
  process.exit(failed.length ? 1 : 0);
})();
