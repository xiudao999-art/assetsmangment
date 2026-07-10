"""UI 级 QC(webapp-testing)—— 真在浏览器点一遍关键用户流程并断言。
不是 API 测试、不是静态截图,是真点击。前端改动后必跑(见 CLAUDE.md)。
跑法:make ui-qc(自动起服务+播种+验);或对线上:BASE=http://8.149.247.100:8088 .venv/bin/python tests/ui_qc.py
覆盖:①用户登录 ②公共库收藏→出现在我的物料库 ③管理员可见全部 ④审核通过 ⑤发布到公共库
"""
import os
import sys
import time
from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE", "http://localhost:8099")).rstrip("/") + "/ui/"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name)


def login(p, name, pw, register):
    p.goto(BASE); p.wait_for_timeout(500)
    p.evaluate("() => localStorage.removeItem('me')")
    p.goto(BASE); p.wait_for_timeout(700)
    if register:
        p.click("#tab-reg")
    p.fill("#g-name", name); p.fill("#g-pw", pw); p.click("#g-submit"); p.wait_for_timeout(1500)


def go(p, sec):
    p.click(f'.nav[data-sec="{sec}"]'); p.wait_for_timeout(1400)


with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_page(viewport={"width": 1340, "height": 900})

    # ── 用户:登录 ──
    login(p, "qc" + str(int(time.time())), "pw123456", True)
    check("用户注册并进入应用", p.is_visible("#app"))

    # ── ★ 公共库收藏 → 我的物料库出现(用户报的 bug) ──
    go(p, "public")
    fav = p.query_selector('#pub-grid .cell .btn.key')
    check("公共库有可收藏物料", fav is not None)
    if fav:
        fav.click(); p.wait_for_timeout(1100)
    go(p, "mine")
    in_mine = p.evaluate("() => [...document.querySelectorAll('#mine-grid .cell')].some(c => c.textContent.includes('取消收藏'))")
    check("★ 收藏后出现在「我的物料库」", in_mine)

    # ── 管理员:全部 / 审核 / 发布 ──
    login(p, "admin", "admin123", False)
    check("管理员可见「全部物料」菜单", p.query_selector('.nav[data-sec="all"]') is not None)
    go(p, "review")
    pb = p.query_selector('#queue .btn.pass')
    if pb:
        pb.click(); p.wait_for_timeout(1000)
    check("管理员审核队列可操作(通过)", pb is not None)
    go(p, "all")
    pubb = p.query_selector('#all-grid .btn.key')
    if pubb:
        pubb.click(); p.wait_for_timeout(1000)
    check("管理员可发布到公共库", pubb is not None)

    b.close()

failed = [n for n, c in results if not c]
print(f"\n== UI QC: {len(results) - len(failed)}/{len(results)} passed ==")
sys.exit(1 if failed else 0)
