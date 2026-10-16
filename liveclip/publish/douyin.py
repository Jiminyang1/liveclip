"""Douyin (抖音) web upload with Playwright driving the system Chrome on a dedicated profile.

Disabled unless config publish.enabled and publish.douyin.enabled are true, and publish.douyin.account_id is set:
the script reads the logged-in account's Douyin ID on the creator center and refuses to publish to any other account.
Only upload content you have the rights to; you are responsible for complying with the platform's terms.

Requires: pip install 'liveclip[publish]' (playwright). channel="chrome" uses the installed Google Chrome
(config publish.douyin.chrome_channel), no browser download.

  liveclip douyin login [--timeout 300]       open a visible Chrome on data/douyin_pw_profile/ (separate from your normal
                                               Chrome) at the creator center; scan the QR code with the target account
  liveclip douyin check                       verify login + account; exit 0 = ready, 1 = not (reason in the JSON output)
  liveclip douyin upload <dir> [--video 视频_精剪.mp4] [--at "YYYY-MM-DD HH:MM"] --dry-run | --go
        --dry-run  fill in everything and stop before「发布」: full-page screenshot + report.json
        --go       click「发布」, confirm the redirect to content management with this clip on top, record publish_log
        --headless no window (default is headed: fewer risk-control prompts); --hold N keeps the dry-run window N s

Verification / slider / SMS / QR prompts: the script stops immediately and asks the human; it never fills them.
Screenshots and reports of every run: logs/douyin_pw/<time>_<cmd>/. Single instance (data/.douyin_pw.lock).

Exit codes: 0 ok; 1 failed (every check failure is 1); 2 bad args; 3 already published; 4 wrong account; 5 not logged in;
        6 verification / risk control; 7 another instance running; 8 a page step failed (see screenshots);
        9 clicked publish but could not verify (do NOT retry blindly — check content management); 10 bad input;
        11 publishing disabled in config

Selectors (✓ verified on the 2026 creator center; ? inferred, the dry-run report includes page snippets to check):
  upload input   input[type=file][accept*=video]                              ✓
  title          input[placeholder*="填写作品标题"] (fallback 添加作品标题)      ✓
  description    div.zone-container[contenteditable=true]                     ✓
  topic dropdown .mention-suggest-mount-dom [class*=tag-hash-view-name]       ?
  topic chips    [data-mention="#"] (fallback: class/text)                    ?
  upload done    text「重新上传」                                              ✓
  cover entry    「选择封面」 inside the「竖封面」slot                          ✓
  cover modal    div.dy-creator-content-modal / semi modal containing「设置竖封面」 ✓
  cover input    file input in the modal, excluding the「生成参考图」one        ✓
  done           role=button「完成」                                           ✓
  check passed   text「封面效果检测通过」                                      ✓
  visibility     「公开」 in the「谁可以看」section                             ✓
  schedule       「定时发布」 radio + input[placeholder*=日期] (browser local TZ) ✓
  publish        role=button name=发布 exact                                   ✓
  success        redirect to /creator-micro/content/manage                   ✓
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import tags as T
from ..config import C
from . import common as PS  # info() / published() / done()

ROOT = C.ROOT
PROFILE = C.DATA / 'douyin_pw_profile'     # Chrome 登录目录（cookie 在这里），.gitignore
LOGDIR = C.LOGS / 'douyin_pw'
LOCK = C.DATA / '.douyin_pw.lock'
ACCOUNT_ID = str(C.get('publish.douyin.account_id') or '').strip()   # 要发布的账号的抖音号；不配置就不发
HOME = 'https://creator.douyin.com/creator-micro/home'
UPLOAD = 'https://creator.douyin.com/creator-micro/content/upload'
BJ = C.audience_tz                          # --at 按观众时区（config timezones.audience）
TZL = C.audience_tz_label

OK, FAIL, BAD_ARGS, DUP, WRONG_ACCOUNT, NOT_LOGGED_IN, BLOCKED, BUSY, STEP, UNVERIFIED, BAD_INPUT, DISABLED = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11

LOGIN_TEXT = ['扫码登录', '手机号登录', '验证码登录', '密码登录', '二维码失效']
# 验证 / 风控：看到就停（只认可见的）。「验证码」单独一词不算（登录页有「验证码登录」）
BLOCK_SEL = ['#captcha_container', '[id*="captcha"]', '[class*="captcha"]', 'iframe[src*="captcha"]',
             'iframe[src*="verifycenter"]', 'iframe[src*="verify"]', '[class*="uc-ui-verify"]', '[class*="verify-bar"]',
             '[class*="secsdk"]', 'input[placeholder*="验证码"]', 'input[placeholder*="短信"]']
BLOCK_TEXT = ['安全验证', '请完成下列验证', '拖动滑块', '按住左边按钮拖动', '完成拼图', '短信验证', '身份验证',
              '获取验证码', '操作频繁', '发布频繁', '账号存在风险', '请输入验证码']

SEL = {
    'video_input': 'input[type=file][accept*="video"]',
    'title': ['input[placeholder*="填写作品标题"]', 'input[placeholder*="添加作品标题"]', 'input[placeholder*="作品标题"]'],
    'editor': ['div.zone-container[contenteditable="true"]', '[class*="editor"] [contenteditable="true"]'],
    'cover_modal': 'div.dy-creator-content-modal, .semi-modal-content, .semi-modal, [role="dialog"]',
}


class Fail(Exception):
    def __init__(self, code, msg, step=''):
        super().__init__(msg)
        self.code, self.msg, self.step = code, msg, step


def say(*a):
    print('[douyin_pw]', *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 运行目录、锁

RUN = None   # 当前这次运行（出错时 main 也要拿到截图目录）


class Run:
    def __init__(self, cmd, label=''):
        global RUN
        RUN = self
        self.dir = LOGDIR / (datetime.now().strftime('%Y%m%d_%H%M%S') + f'_{cmd}' + (f'_{label}' if label else ''))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report = {'cmd': cmd, 'started': datetime.now().astimezone().isoformat(timespec='seconds'),
                       'run_dir': C.rel(self.dir), 'warnings': [], 'steps': []}
        self.page = None

    def step(self, name, **kw):
        self.report['steps'].append({'t': round(time.time() - T0, 1), 'step': name, **kw})
        say(f'{name}', json.dumps(kw, ensure_ascii=False) if kw else '')

    def warn(self, msg):
        self.report['warnings'].append(msg)
        say('注意：' + msg)

    def shot(self, name, full=True):
        if not self.page:
            return None
        p = self.dir / f'{name}.png'
        try:
            self.page.screenshot(path=str(p), full_page=full, timeout=15000)
            return C.rel(p)
        except Exception as e:  # noqa: BLE001
            say(f'截图失败 {name}: {e}')
            return None

    def dump(self, name, text):
        (self.dir / name).write_text(text, encoding='utf-8')

    def save(self):
        self.report['finished'] = datetime.now().astimezone().isoformat(timespec='seconds')
        (self.dir / 'report.json').write_text(json.dumps(self.report, ensure_ascii=False, indent=1), encoding='utf-8')


_LOCK_FH = None


def lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Fail(BUSY, '已有一个抖音发布脚本在跑（data/.douyin_pw.lock），等它结束')
    fh.write(str(os.getpid()))
    fh.flush()
    global _LOCK_FH
    _LOCK_FH = fh          # 进程活着就一直锁着，退出自动释放


# ---------------------------------------------------------------- 浏览器

def open_browser(pw, headless=False):
    PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        str(PROFILE), channel=C.get('publish.douyin.chrome_channel') or 'chrome', headless=headless, locale='zh-CN',
        viewport={'width': 1440, 'height': 900},
        args=['--disable-blink-features=AutomationControlled', '--no-first-run', '--no-default-browser-check',
              '--window-size=1460,1000'],
        # Playwright 默认关 Chrome 沙箱（加 --no-sandbox），窗口顶上会黄条警告、也更不安全：打开沙箱
        chromium_sandbox=True,
        ignore_default_args=['--enable-automation'],
    )
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    ctx.set_default_timeout(15000)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    for p in ctx.pages[1:]:          # 上次留下的标签页关掉，只用一个
        p.close()
    return ctx, page


def visible_text(page, text):
    loc = page.get_by_text(text, exact=True)
    try:
        for i in range(min(loc.count(), 5)):
            if loc.nth(i).is_visible():
                return True
    except Exception:  # noqa: BLE001  页面跳转中
        pass
    return False


def on_login_page(page):
    if 'creator-micro' not in page.url:
        return True
    return any(visible_text(page, t) for t in LOGIN_TEXT)


LOGGED_IN_TEXT = ['抖音号', '内容管理', '数据中心', '发布视频']   # 登录后侧栏/首页才有；登录页一个都没有


def logged_in(page):
    """没登录时抖音会把 URL 留在 /creator-micro/home、过几秒才渲染出扫码卡（2026-10-16 实测），
    所以只看 URL 会把空白页当成已登录：要 URL 对 + 没有登录字样 + 有登录后才有的字。"""
    if 'creator-micro' not in page.url:
        return False
    # 侧栏「内容管理」「数据中心」都看得见 = 已登录。扫码登录后登录弹窗的字可能还留在页面里（曾因此等满 300 秒才报
    # 没登录上，其实早登录好了），所以先认正面信号
    if visible_text(page, '内容管理') and visible_text(page, '数据中心'):
        return True
    if any(visible_text(page, t) for t in LOGIN_TEXT):
        return False
    try:
        body = page.evaluate('document.body.innerText')
    except Exception:  # noqa: BLE001
        return False
    return any(t in body for t in LOGGED_IN_TEXT)


JS_BLOCK = """([sels, texts]) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 20 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  for (const s of sels) { for (const el of document.querySelectorAll(s)) { if (vis(el)) return 'sel:' + s; } }
  const body = document.body ? document.body.innerText : '';
  for (const t of texts) { if (body.includes(t)) return 'text:' + t; }
  return null; }"""


def blocked(page):
    try:
        return page.evaluate(JS_BLOCK, [BLOCK_SEL, BLOCK_TEXT])
    except Exception:  # noqa: BLE001
        return None


def guard(run, page, where):
    """每步之间调用：掉到登录页 / 弹验证就停。"""
    if on_login_page(page):
        run.shot(f'fail_{where}_login')
        raise Fail(NOT_LOGGED_IN, '抖音登录失效（页面是扫码/登录页）。跑 `liveclip douyin login` 用要发布的账号扫码', where)
    b = blocked(page)
    if b:
        run.shot(f'fail_{where}_verify')
        raise Fail(BLOCKED, f'抖音弹了验证/风控（{b}），已停。请人在窗口里自己处理，脚本不代填', where)


def read_account(page, sniffed, tries=25):
    """在创作者中心首页读抖音号（页面文字「抖音号：xxx」；读不到再看接口返回里的 unique_id）。"""
    name = None
    for _ in range(tries):
        try:
            txt = page.evaluate('document.body.innerText')
        except Exception:  # noqa: BLE001
            txt = ''
        m = re.search(r'抖音号\s*[:：]?\s*([A-Za-z0-9_.\-]{4,})', txt)
        if m:
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            for i, x in enumerate(lines):
                if '抖音号' in x:
                    name = lines[i - 1] if i else None
                    break
            return m.group(1), name, 'page'
        if sniffed:
            return sniffed[-1], None, 'api'
        if tries > 1:
            time.sleep(1)
    return None, None, None


def sniff_ids(page):
    ids = []

    def on_resp(r):
        try:
            if 'creator.douyin.com' in r.url and re.search(r'/(user|account)', r.url) and 'json' in (r.headers.get('content-type') or ''):
                for m in re.finditer(r'"unique_id"\s*:\s*"([^"]+)"', r.text()):
                    ids.append(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    page.on('response', on_resp)
    return ids


def session_expiry(page):
    """登录 cookie（sessionid）标的到期时间（本地时间）。实际可能更早被踢（风控、换 IP、连发），以 check 为准。"""
    try:
        for c in page.context.cookies('https://creator.douyin.com'):
            if c['name'] == 'sessionid' and c.get('expires', -1) > 0:
                return datetime.fromtimestamp(c['expires']).astimezone().strftime('%Y-%m-%d %H:%M %z')
    except Exception:  # noqa: BLE001
        pass
    return None


def verify_account(run, page, sniffed):
    """已登录 → 抖音号必须是 config 里的 account_id。返回 (ok, info)；
    info['reason'] ∈ ok / not_logged_in / verify / wrong_account / account_unknown / not_configured。"""
    if not ACCOUNT_ID:
        return False, {'reason': 'not_configured', 'url': page.url}
    page.goto(HOME, wait_until='domcontentloaded', timeout=60000)
    time.sleep(3)
    login_hits = 0
    for _ in range(30):                       # 没登录会被跳回 creator.douyin.com 根路径的扫码页
        if on_login_page(page):
            login_hits += 1
            if login_hits >= 3:
                return False, {'reason': 'not_logged_in', 'url': page.url}
        else:
            login_hits = 0
            b = blocked(page)
            if b:
                return False, {'reason': 'verify', 'detail': b, 'url': page.url}
            acc, name, how = read_account(page, sniffed, tries=1)
            if acc:
                info = {'account_id': acc, 'name': name, 'how': how, 'url': page.url, 'session_expires': session_expiry(page)}
                info['reason'] = 'ok' if acc == ACCOUNT_ID else 'wrong_account'
                return acc == ACCOUNT_ID, info
        time.sleep(1)
    try:
        run.dump('home_text.txt', page.evaluate('document.body.innerText')[:5000])
    except Exception:  # noqa: BLE001
        pass
    return False, {'reason': 'account_unknown', 'url': page.url}


# ---------------------------------------------------------------- login / check

def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Fail(FAIL, "需要 Playwright：pip install 'liveclip[publish]'（用系统里装的 Google Chrome，不用另下浏览器）") from None
    return sync_playwright


def cmd_login(a):
    sync_playwright = _playwright()
    run = Run('login')
    lock()
    with sync_playwright() as pw:
        ctx, page = open_browser(pw, headless=False)       # 登录一定有头
        run.page = page
        try:
            sniffed = sniff_ids(page)
            page.goto('https://creator.douyin.com/', wait_until='domcontentloaded', timeout=60000)
            try:                                              # 登录卡要 5–20 秒才渲染出二维码
                page.get_by_text('扫码登录', exact=True).first.wait_for(state='visible', timeout=30000)
                time.sleep(2)
            except Exception:  # noqa: BLE001  已登录（直接跳进 creator-micro）或页面慢
                pass
            run.report['login_page'] = run.shot('login_page', full=False)
            say(f'浏览器已打开创作者中心。请用抖音 App（要发布的账号）扫码；'
                f'要短信/身份验证就在这个窗口里自己完成。最多等 {a.timeout} 秒。')
            deadline, hits = time.time() + a.timeout, 0
            while time.time() < deadline:
                hits = hits + 1 if logged_in(page) else 0
                if hits >= 2:
                    break
                time.sleep(2)
            else:
                run.shot('login_timeout', full=False)
                raise Fail(NOT_LOGGED_IN, f'{a.timeout} 秒内没登录上（截图在 {run.report["run_dir"]}）')
            run.step('扫码登录成功', url=page.url)
            time.sleep(3)
            ok, info = verify_account(run, page, sniffed)
            run.report['account'] = info
            run.shot('home', full=False)
            if not ok:
                if info['reason'] == 'wrong_account':
                    raise Fail(WRONG_ACCOUNT, f'登录的是抖音号 {info["account_id"]}，不是配置的 {ACCOUNT_ID}。'
                               '请在这个窗口里退出登录，再用正确的账号扫码重跑 login')
                if info['reason'] == 'not_configured':
                    raise Fail(FAIL, '登录好了，但 config publish.douyin.account_id 没填：填上这个账号的抖音号再发布')
                raise Fail(FAIL, f'登录了，但没读到抖音号（{info["reason"]}），看 {run.report["run_dir"]}')
            say(f'登录好了：抖音号 {info["account_id"]} {info.get("name") or ""}')
            return OK, run
        finally:
            run.save()
            ctx.close()


def cmd_check(a):
    sync_playwright = _playwright()
    run = Run('check')
    lock()
    with sync_playwright() as pw:
        ctx, page = open_browser(pw, headless=a.headless)
        run.page = page
        try:
            sniffed = sniff_ids(page)
            ok, info = verify_account(run, page, sniffed)
            run.report['account'] = info
            if not ok:
                run.shot('check_fail', full=False)
            return (OK if ok else FAIL), run
        finally:
            run.save()
            ctx.close()


# ---------------------------------------------------------------- upload 各步

def first_visible(page, sels, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in sels:
            loc = page.locator(s)
            try:
                for i in range(min(loc.count(), 4)):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
    return None


def discard_draft(run, page):
    """上传页顶部问「上次未发布的视频是否继续编辑」→ 放弃（已知坑）。"""
    for _ in range(6):
        btn = page.get_by_role('button', name='放弃', exact=True)
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click()
                run.step('放弃上次草稿')
                time.sleep(1.5)
                return
        except Exception:  # noqa: BLE001
            pass
        if visible_text(page, '放弃'):
            page.get_by_text('放弃', exact=True).first.click()
            run.step('放弃上次草稿')
            time.sleep(1.5)
            return
        time.sleep(0.5)


def put_video(run, page, video):
    page.goto(UPLOAD, wait_until='domcontentloaded', timeout=90000)
    time.sleep(3)
    guard(run, page, 'upload_page')
    discard_draft(run, page)
    inp = page.locator(SEL['video_input']).first
    try:
        inp.wait_for(state='attached', timeout=30000)
    except Exception:  # noqa: BLE001
        run.shot('fail_video_input')
        raise Fail(STEP, '上传页 30 秒没出现视频上传框', 'video')
    inp.set_input_files(str(video))
    run.step('视频已塞进上传框', mb=round(Path(video).stat().st_size / 1e6, 1))
    try:
        page.wait_for_url(re.compile(r'/content/(post/video|publish)'), timeout=90000, wait_until='domcontentloaded')
    except Exception:  # noqa: BLE001
        guard(run, page, 'video')
        run.shot('fail_post_page')
        raise Fail(STEP, '塞了视频 90 秒没跳到发布表单页', 'video')
    run.step('进了发布表单', url=page.url)


def fill_title(run, page, title):
    loc = first_visible(page, SEL['title'], timeout=120)      # 表单「加载中」可能 20 秒+（#39）
    if not loc:
        guard(run, page, 'title')
        run.shot('fail_title')
        raise Fail(STEP, '120 秒没出现标题输入框', 'title')
    loc.click()
    loc.fill(title)
    time.sleep(0.5)
    got = loc.input_value()
    run.report['title_read'] = got
    if got != title:
        run.shot('fail_title_mismatch')
        raise Fail(STEP, f'标题读回来不一样：{got!r}', 'title')
    run.step('标题', title=got)


JS_CARET_END = """el => { el.focus(); const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
  const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  return (el.innerText || '').slice(-1); }"""

# 编辑器里已经变成话题块的（data-mention="#"；没有就退回看 class 里带 topic/hashtag 的、文字以 # 开头的元素）
JS_TOPICS = """el => {
  let nodes = [...el.querySelectorAll('[data-mention="#"]')]; let how = 'data-mention';
  if (!nodes.length) { nodes = [...el.querySelectorAll('[class*="topic"], [class*="hashtag"], [class*="mention"]')]
      .filter(n => (n.innerText || '').trim().startsWith('#')); how = 'class';
    nodes = nodes.filter(n => !nodes.some(o => o !== n && o.contains(n)) ); }
  return {how, names: nodes.map(n => (n.innerText || '').trim().replace(/^#/, '').trim()), text: el.innerText || ''}; }"""

# 话题下拉里的候选（.mention-suggest-mount-dom 里 tag-hash-view-name；没有就退回下拉里每一行的第一行字）
JS_CANDS = """() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-pw-cand]').forEach(e => e.removeAttribute('data-pw-cand'));
  const roots = [...document.querySelectorAll('.mention-suggest-mount-dom, [class*="mention-suggest"]')].filter(vis);
  let items = [];
  for (const r of roots) { items = [...r.querySelectorAll('[class*="tag-hash-view-name"]')].filter(vis); if (items.length) break; }
  let how = 'tag-hash-view-name';
  if (!items.length && roots.length) { how = 'row';
    const rows = [...roots[0].querySelectorAll('[role="option"], li, [class*="item"]')].filter(vis);
    items = rows.filter(n => !rows.some(o => o !== n && o.contains(n))); }
  const loading = roots.some(r => /加载中|loading/i.test(r.innerText || '') || r.querySelector('[class*="spin"], [class*="loading"]'));
  return {how, loading, roots: roots.length, items: items.slice(0, 8).map((n, i) => { n.setAttribute('data-pw-cand', i);
    return ((n.innerText || '').split('\\n')[0] || '').trim().replace(/^#/, '').trim(); })}; }"""


def norm(t):
    return re.sub(r'\s+', '', str(t)).lstrip('#').lower()


def fill_topics(run, page, tags):
    ed = first_visible(page, SEL['editor'], timeout=30)
    if not ed:
        run.shot('fail_editor')
        raise Fail(STEP, '没找到作品简介编辑器', 'topics')
    results = []
    for tag in tags:
        before = ed.evaluate(JS_TOPICS)['names']
        if len(before) >= T.LIMIT['douyin']:       # 第 6 个起选不上，打的字还会插乱（#15）
            run.warn(f'已有 {len(before)} 个话题，#{tag} 不加了')
            continue
        res = {'tag': tag, 'ok': False, 'tries': []}
        for attempt in range(3):                 # 下拉没出来 / 没有同名项 → 删掉重打，最多 3 次
            last = ed.evaluate(JS_CARET_END)
            typed = ('' if (not last or last.isspace()) else ' ') + '#' + tag
            page.keyboard.type(typed, delay=70)
            time.sleep(3)                           # 下拉要加载，转圈时操作会失效（#16）
            cands, chosen = {'items': []}, None
            for _ in range(10):
                cands = page.evaluate(JS_CANDS)
                if cands['items'] and not cands['loading']:
                    break
                time.sleep(0.6)
            items = cands['items']
            exact = [i for i, n in enumerate(items) if norm(n) == norm(tag)]
            if exact:
                chosen = exact[0]
            elif items and attempt == 2 and (norm(items[0]).startswith(norm(tag)) or norm(tag).startswith(norm(items[0]))):
                chosen = 0                          # 最后一次：第一项跟要的话题前缀一致也收（如「abc」显示成「ABC」）
            res['tries'].append({'items': items, 'how': cands.get('how'), 'chosen': chosen})
            if chosen is not None:
                page.locator(f'[data-pw-cand="{chosen}"]').first.click()   # 点项，不按回车（#40：回车会选偏）
                time.sleep(1)
                after = ed.evaluate(JS_TOPICS)
                if len(after['names']) == len(before) + 1 and norm(after['names'][-1]) == norm(items[chosen]):
                    res.update(ok=True, picked=items[chosen], verify=after['how'])
                    break
                if after['how'] == 'class' and not after['names'] and ('#' + items[chosen]) in after['text']:
                    res.update(ok=True, picked=items[chosen], verify='text-only(未认出话题块，干跑时人工核对)')
                    break
                # 选偏了 / 没变成话题块：删掉最后一个话题块或残字
                res['tries'][-1]['after'] = after['names']
                _undo_last(page, ed, before, typed)
            else:
                page.keyboard.press('Escape')
                for _ in range(len(typed)):
                    page.keyboard.press('Backspace')
                time.sleep(1)
        results.append(res)
        run.step('话题', tag=tag, ok=res['ok'], picked=res.get('picked'))
        if not res['ok'] and tag in T.MUST:
            run.report['topics'] = results
            run.shot('fail_topic')
            raise Fail(STEP, f'必带话题 #{tag} 没选上（下拉候选：{res["tries"][-1]["items"]}）', 'topics')
        if not res['ok']:
            run.warn(f'话题 #{tag} 下拉里没有同名项，跳过了（候选：{res["tries"][-1]["items"]}）')
    page.keyboard.press('Escape')
    final = ed.evaluate(JS_TOPICS)
    run.report['topics'] = results
    run.report['topics_read'] = final['names']
    run.report['editor_text'] = final['text']
    run.report['editor_html'] = ed.evaluate('el => el.outerHTML')[:3000]
    if len(final['names']) > T.LIMIT['douyin']:
        raise Fail(STEP, f'编辑器里有 {len(final["names"])} 个话题，超过抖音上限 5 个', 'topics')
    return final['names']


def _undo_last(page, ed, before, typed):
    """选偏了（多了个不对的话题块）→ 退格删掉这个块；没变成块、还是纯文字 → 正好删掉刚打的字。别多删前面的话题（#15）。"""
    page.keyboard.press('Escape')
    ed.evaluate(JS_CARET_END)
    now = ed.evaluate(JS_TOPICS)
    if len(now['names']) > len(before):
        for _ in range(4):
            page.keyboard.press('Backspace')
            time.sleep(0.3)
            now = ed.evaluate(JS_TOPICS)
            if len(now['names']) <= len(before):
                break
    elif now['text'].rstrip().endswith(typed.strip()):
        for _ in range(len(typed)):
            page.keyboard.press('Backspace')
    time.sleep(0.5)


def wait_uploaded(run, page, timeout=600):
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        if visible_text(page, '重新上传'):
            run.step('视频传完')
            return
        if page.get_by_text('上传失败').count():
            run.shot('fail_upload')
            raise Fail(STEP, '抖音显示视频「上传失败」', 'upload')
        try:
            m = re.search(r'(\d{1,3}(?:\.\d+)?)\s*%', page.evaluate('document.body.innerText'))
            if m and m.group(1) != last:
                last = m.group(1)
        except Exception:  # noqa: BLE001
            pass
        guard(run, page, 'upload')
        time.sleep(2)
    run.shot('fail_upload_timeout')
    raise Fail(STEP, f'{timeout} 秒视频还没传完（最后进度 {last}%）', 'upload')


# ---- 封面

# 表单里的两格封面：含「竖封面」不含「横封面」的最大块（及反过来），取里面图片 src / 背景图 / 文字做签名
JS_SLOTS = """() => {
  const out = {};
  const inModal = el => el.closest('.semi-modal, .dy-creator-content-modal, .semi-portal, [role="dialog"]');
  for (const [k, lab, other] of [['v', '竖封面', '横封面'], ['h', '横封面', '竖封面']]) {
    let best = null, area = 0;
    for (const el of document.querySelectorAll('div')) {
      if (inModal(el)) continue;
      const t = el.innerText || '';
      if (!t.includes(lab) || t.includes(other) || t.length > 200) continue;
      const r = el.getBoundingClientRect(); const a = r.width * r.height;
      if (a > area) { area = a; best = el; }
    }
    if (!best) { out[k] = null; continue; }
    best.setAttribute('data-pw-slot', k);
    const imgs = [...best.querySelectorAll('img')].map(i => i.currentSrc || i.src);
    const bgs = [...best.querySelectorAll('*')].map(e => getComputedStyle(e).backgroundImage).filter(b => b && b !== 'none');
    out[k] = {sig: JSON.stringify([imgs, bgs]), text: (best.innerText || '').slice(0, 80), choose: (best.innerText || '').includes('选择封面')};
  }
  return out; }"""

JS_MODAL = """(sel) => {
  document.querySelectorAll('[data-pw-modal]').forEach(e => e.removeAttribute('data-pw-modal'));
  let best = null, area = 0;
  for (const el of document.querySelectorAll(sel)) {
    const t = el.innerText || ''; const r = el.getBoundingClientRect();
    if (!/设置(竖|横)封面/.test(t) || r.width < 50) continue;
    if (r.width * r.height > area) { area = r.width * r.height; best = el; }
  }
  if (best) best.setAttribute('data-pw-modal', '1');
  return !!best; }"""

# 弹窗顶部 tab（不在 button 里、文字正好是「设置竖封面」/「设置横封面」、最靠上的那个；右下角同名按钮不能点，#36 会关弹窗）
JS_TAB = """([label]) => {
  const m = document.querySelector('[data-pw-modal]'); if (!m) return null;
  document.querySelectorAll('[data-pw-tab]').forEach(e => e.removeAttribute('data-pw-tab'));
  const c = [...m.querySelectorAll('*')].filter(e => (e.innerText || '').trim() === label && !e.closest('button'))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (!c.length) return null;
  const inner = c.filter(e => !c.some(o => o !== e && e.contains(o)));
  inner.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  const t = inner[0]; t.setAttribute('data-pw-tab', '1');
  const cls = [t.className, t.parentElement && t.parentElement.className].join(' ');
  return {cls: String(cls).slice(0, 200), active: /active|selected|checked|current/i.test(cls)}; }"""

# 弹窗里的 file input：排除「生成参考图」（#10/#34），优先带「上传封面/点击上传」拖拽区的、可见的
JS_INPUTS = """() => {
  const m = document.querySelector('[data-pw-modal]'); if (!m) return [];
  document.querySelectorAll('[data-pw-in]').forEach(e => e.removeAttribute('data-pw-in'));
  return [...m.querySelectorAll('input[type=file]')].map((inp, i) => {
    inp.setAttribute('data-pw-in', i);
    const box = inp.closest('.semi-upload') || inp.parentElement;
    const r = box.getBoundingClientRect();
    let ctx = '', p = inp;
    for (let k = 0; k < 4 && p; k++) { p = p.parentElement; if (p && (p.innerText || '').trim()) { ctx = p.innerText.trim(); break; } }
    return {i, accept: inp.accept, visible: r.width > 0 && r.height > 0,
            main: !!box.querySelector('.semi-upload-drag-area-main-text') || /上传封面|点击上传/.test(ctx),
            ref: /参考图/.test(ctx), ctx: ctx.slice(0, 60)};
  }); }"""

# 弹窗主预览（最大的 img/canvas）签名 + 宽高比：传对了它会变，竖封面应是竖的、横封面应是横的
JS_PREVIEW = """() => {
  const m = document.querySelector('[data-pw-modal]'); if (!m) return null;
  let best = null, area = 0;
  for (const el of m.querySelectorAll('img, canvas')) {
    const r = el.getBoundingClientRect(); if (r.width * r.height > area) { area = r.width * r.height; best = el; }
  }
  if (!best) return null;
  const r = best.getBoundingClientRect(); let s = best.currentSrc || best.src || '';
  if (best.tagName === 'CANVAS') { try { s = 'canvas:' + best.toDataURL('image/jpeg', 0.3).length; } catch (e) { s = 'canvas-tainted'; } }
  return {sig: best.tagName + '|' + s.slice(0, 120) + '|' + s.length + '|' + Math.round(r.width) + 'x' + Math.round(r.height),
          ratio: r.height ? r.width / r.height : 0}; }"""


def open_cover_modal(run, page):
    page.evaluate("() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e => e.remove())")
    page.keyboard.press('Escape')
    for attempt in range(4):
        slots = page.evaluate(JS_SLOTS)
        slot = page.locator('[data-pw-slot="v"]') if slots.get('v') else None
        target = None
        if slot is not None and slot.get_by_text('选择封面').count():
            target = slot.get_by_text('选择封面').first
        elif slot is not None:
            target = slot.first
        else:
            loc = page.get_by_text('选择封面', exact=True)
            target = loc.first if loc.count() else None
        if target is None:
            break
        try:
            target.scroll_into_view_if_needed(timeout=5000)
            target.hover()
            time.sleep(0.5)
            target.click(timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):
            time.sleep(0.6)
            if page.evaluate(JS_MODAL, SEL['cover_modal']):
                run.step('封面弹窗打开', attempt=attempt + 1)
                time.sleep(1.5)
                return page.locator('[data-pw-modal="1"]')
    run.shot('fail_cover_open')
    raise Fail(STEP, '封面弹窗打不开', 'cover')


def cover_one(run, page, kind, path):
    label = '设置竖封面' if kind == 'v' else '设置横封面'
    page.evaluate(JS_MODAL, SEL['cover_modal'])
    tab = page.evaluate(JS_TAB, [label])
    if tab is None:
        run.dump(f'cover_modal_{kind}.html', page.locator('[data-pw-modal="1"]').evaluate('el => el.outerHTML')[:200000])
        run.shot(f'fail_cover_tab_{kind}')
        raise Fail(STEP, f'封面弹窗里没找到顶部「{label}」tab（弹窗 HTML 已存）', 'cover')
    page.locator('[data-pw-tab="1"]').first.click()
    time.sleep(1.5)
    if not page.evaluate(JS_MODAL, SEL['cover_modal']):      # #36：切 tab 把弹窗关了
        run.shot(f'fail_cover_closed_{kind}')
        raise Fail(STEP, f'点「{label}」后封面弹窗关了', 'cover')
    inputs = page.evaluate(JS_INPUTS)
    order = sorted([x for x in inputs if not x['ref']], key=lambda x: (not x['visible'], not x['main'], -x['i']))
    info = {'kind': kind, 'tab': tab, 'inputs': inputs, 'tries': []}
    for cand in order[:3]:
        before = page.evaluate(JS_PREVIEW)
        page.locator(f'[data-pw-in="{cand["i"]}"]').set_input_files(str(path))
        changed, after = False, before
        for _ in range(20):                      # 最多 ~15 秒等画布换成我们的图
            time.sleep(0.75)
            after = page.evaluate(JS_PREVIEW)
            if after and (not before or after['sig'] != before['sig']):
                changed = True
                break
        info['tries'].append({'input': cand['i'], 'changed': changed, 'ratio': after and round(after['ratio'], 2)})
        if changed:
            time.sleep(2)
            ratio = after['ratio']
            if (kind == 'v' and ratio > 1.05) or (kind == 'h' and ratio < 0.95):
                run.warn(f'{label}后主画布宽高比 {ratio:.2f}，不像{"竖" if kind == "v" else "横"}图，看截图核对是不是传错页（#9）')
            info['shot'] = run.shot(f'cover_modal_{kind}', full=False)
            run.step('封面已传', kind=kind, input=cand['i'], ratio=round(ratio, 2))
            return info
        page.evaluate(JS_MODAL, SEL['cover_modal'])
        page.evaluate(JS_INPUTS)                  # 重新编号，下一轮换另一个 input（#34：第一个可能是参考图）
    run.dump(f'cover_modal_{kind}.html', page.locator('[data-pw-modal="1"]').evaluate('el => el.outerHTML')[:200000])
    run.shot(f'fail_cover_{kind}')
    raise Fail(STEP, f'{label}：传了 {len(info["tries"])} 个上传框，画布都没变（弹窗 HTML 已存）', 'cover')


def set_covers(run, page, v_path, h_path):
    before = page.evaluate(JS_SLOTS)
    run.report['cover_slots_before'] = before
    modal = open_cover_modal(run, page)
    infos = [cover_one(run, page, 'v', v_path), cover_one(run, page, 'h', h_path)]
    run.report['cover_modal'] = infos
    done_btn = modal.get_by_role('button', name='完成', exact=True)
    for _ in range(60):                          # 封面处理完前「完成」是灰的
        try:
            if done_btn.count() and done_btn.first.is_enabled() and 'disabled' not in (done_btn.first.get_attribute('class') or ''):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    # 点「完成」后弹窗会整体变灰、转圈保存横封面，要等它自己关（2026-10-16 第一次干跑：20 秒还在转圈，被误判关不掉）。
    # 最多等 90 秒，每 15 秒还开着就再点一次
    deadline, last_click = time.time() + 90, 0
    while time.time() < deadline:
        if time.time() - last_click > 15:
            try:
                done_btn.first.click(timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            last_click = time.time()
        time.sleep(2)
        for name in ['确定', '确认']:
            c = page.locator('.semi-modal-content').get_by_role('button', name=name, exact=True)
            if c.count() and c.first.is_visible():
                c.first.click()
                time.sleep(1)
        if not page.evaluate(JS_MODAL, SEL['cover_modal']):
            break
    else:
        run.shot('fail_cover_done')
        raise Fail(STEP, '封面弹窗点「完成」关不掉', 'cover')
    run.step('封面弹窗已完成，等缩略图换上 + 检测通过（最多 45 秒，#41/#42 见过 28 秒）')
    deadline, now, v_ok, h_ok, passed = time.time() + 45, {}, False, False, False
    while time.time() < deadline:
        time.sleep(2)
        now = page.evaluate(JS_SLOTS)
        v_ok = bool(now.get('v')) and (not before.get('v') or now['v']['sig'] != before['v']['sig']) and not now['v']['choose']
        h_ok = bool(now.get('h')) and (not before.get('h') or now['h']['sig'] != before['h']['sig']) and not now['h']['choose']
        passed = page.get_by_text('封面效果检测通过').count() > 0
        if v_ok and h_ok and passed:
            break
    res = {'v_changed': v_ok, 'h_changed': h_ok, 'detect_passed': passed, 'slots_after': now}
    run.report['covers'] = res
    run.step('封面结果', v=v_ok, h=h_ok, passed=passed)
    if not (v_ok and h_ok):
        run.shot('fail_cover_thumbs')
        raise Fail(STEP, f'封面缩略图没都换上（竖 {v_ok} / 横 {h_ok}）', 'cover')
    if not passed:
        run.warn('45 秒内没看到「封面效果检测通过」')
    return res


# ---- 设置

JS_RADIO = """([section, label]) => {
  document.querySelectorAll('[data-pw-radio]').forEach(e => e.removeAttribute('data-pw-radio'));
  const all = [...document.querySelectorAll('label, [class*="radio"]')]
    .filter(e => (e.innerText || '').trim() === label && !e.closest('.semi-modal, [role="dialog"]'));
  let pick = all.find(e => { let p = e; for (let k = 0; k < 6 && p; k++) { p = p.parentElement;
    if (p && (p.innerText || '').includes(section)) return true; } return false; }) || all[0];
  if (!pick) return null;
  const lab = pick.closest('label') || pick; lab.setAttribute('data-pw-radio', '1');
  const inp = lab.querySelector('input'); const cls = String(lab.className);
  return {checked: (inp && inp.checked) || /checked/.test(cls)}; }"""


def radio(run, page, section, label, click=True):
    st = page.evaluate(JS_RADIO, [section, label])
    if st is None:
        return None
    if click and not st['checked']:
        page.locator('[data-pw-radio="1"]').first.click()
        time.sleep(0.8)
        st = page.evaluate(JS_RADIO, [section, label])
    return st['checked']


def set_publish_options(run, page, at_bj):
    out = {}
    out['公开'] = radio(run, page, '谁可以看', '公开')
    out['允许保存'] = radio(run, page, '保存', '允许', click=False)        # 只读，不改（平台默认允许）
    if at_bj:
        out['定时发布'] = radio(run, page, '发布时间', '定时发布')
        tzname = page.evaluate('Intl.DateTimeFormat().resolvedOptions().timeZone')
        local = at_bj.astimezone(ZoneInfo(tzname)).strftime('%Y-%m-%d %H:%M')     # 日期框按浏览器本地时区
        inp = first_visible(page, ['input[placeholder*="日期"]', '.semi-datepicker input'], timeout=10)
        if not inp:
            run.shot('fail_schedule')
            raise Fail(STEP, '选了定时发布但没出现日期框', 'schedule')
        inp.click()
        page.keyboard.press('ControlOrMeta+A')
        page.keyboard.type(local, delay=40)
        page.keyboard.press('Enter')
        time.sleep(1)
        got = inp.input_value()
        out['定时'] = {TZL: at_bj.strftime('%Y-%m-%d %H:%M'), '浏览器时区': tzname, '填的': local, '读回': got}
        if got.strip()[:16] != local:
            run.shot('fail_schedule_value')
            raise Fail(STEP, f'定时时间读回来是 {got!r}，应为 {local}（{tzname}）', 'schedule')
    else:
        out['立即发布'] = radio(run, page, '发布时间', '立即发布')
    run.report['options'] = out
    run.step('发布设置', **{k: v for k, v in out.items() if k != '定时'})
    if out['公开'] is not True:
        run.shot('fail_visibility')
        raise Fail(STEP, f'「谁可以看=公开」没选上（{out["公开"]}）', 'options')
    if (at_bj and out.get('定时发布') is not True) or (not at_bj and out.get('立即发布') is not True):
        run.shot('fail_publish_time')
        raise Fail(STEP, '发布时间单选没选上', 'options')
    return out


JS_ASSIST = """() => {
  const h = [...document.querySelectorAll('*')].find(e => (e.innerText || '').trim().startsWith('发文助手') && e.children.length < 6
      && (e.innerText || '').length < 600);
  let box = h;   // 只拿到标题字时往上一层拿整块
  if (box && (box.innerText || '').trim() === '发文助手' && box.parentElement && (box.parentElement.innerText || '').length < 600) box = box.parentElement;
  return box ? box.innerText.trim() : null; }"""


def assistant(run, page):
    txt = page.evaluate(JS_ASSIST)
    body = page.evaluate('document.body.innerText')
    flags = {k: (k in body) for k in ['作品未见异常', '封面效果检测通过', '封面检测通过', '双封面缺失', '封面不佳', '检测中']}
    run.report['assistant'] = {'text': txt, 'flags': flags}
    return flags


# ---------------------------------------------------------------- upload 主流程

def parse_at(s):
    try:
        t = datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=BJ)
    except ValueError:
        raise Fail(BAD_ARGS, f'--at 要写成 "YYYY-MM-DD HH:MM"（{TZL}时间），收到 {s!r}')
    now = datetime.now(BJ)
    if t - now < timedelta(hours=2, minutes=5) or t - now > timedelta(days=14):
        raise Fail(BAD_ARGS, f'抖音定时发布要在 2 小时后、14 天内；现在{TZL}时间 {now:%m-%d %H:%M}')
    return t


def cmd_upload(a):
    if not C.publish_enabled('douyin'):
        raise Fail(DISABLED, '抖音发布没打开：config 里 publish.enabled 和 publish.douyin.enabled 都要是 true'
                             '（只发布你有权发布的内容，见 README）')
    sync_playwright = _playwright()
    d = Path(a.dir).resolve()
    if not (d / '文案.txt').exists():
        raise Fail(BAD_INPUT, f'不是交付目录（没有 文案.txt）：{d}')
    at_bj = parse_at(a.at) if a.at else None
    meta = PS.info(d, a.video)
    title, tags = meta['douyin']['title'], meta['douyin']['tags'][:T.LIMIT['douyin']]
    run = Run('go' if a.go else 'dry', d.name.split('_')[0])
    run.report.update(dir=C.rel(d), video=meta['video'], video_mb=meta['video_mb'], title=title, tags=tags,
                      at_bj=a.at, mode='go' if a.go else 'dry-run', notes=meta['notes'])
    # ---- 素材检查（不开浏览器）
    problems = []
    if not Path(meta['video']).exists():
        problems.append(f'没有视频 {meta["video"]}')
    if not title:
        problems.append('文案里没有抖音标题')
    if len(title) > 30:
        problems.append(f'标题 {len(title)} 字，抖音限 30 字：{title}')
    if not meta['cover_3x4'] or not meta['cover_4x3']:
        problems.append('竖封面 封面.jpg / 横封面 封面_横4比3.jpg 缺一张（抖音要竖横都设，不然报双封面缺失）')
    if problems:
        run.save()
        raise Fail(BAD_INPUT, '；'.join(problems))
    if 'douyin' in PS.published(d):                # 同 publish_serve.py --check douyin
        run.save()
        raise Fail(DUP, f'这条抖音已经发过（data/publish_log.json）：{d.name}')
    for n in meta['notes']:
        run.warn(f'文案首行提示：{n}')
    lock()
    with sync_playwright() as pw:
        ctx, page = open_browser(pw, headless=a.headless)
        run.page = page
        item_id = []
        page.on('response', lambda r: _grab_item(r, item_id))
        try:
            sniffed = sniff_ids(page)
            ok, acc = verify_account(run, page, sniffed)
            run.report['account'] = acc
            if not ok:
                run.shot('fail_account', full=False)
                code = {'not_logged_in': NOT_LOGGED_IN, 'wrong_account': WRONG_ACCOUNT, 'verify': BLOCKED}.get(acc['reason'], WRONG_ACCOUNT)
                raise Fail(code, {'not_logged_in': '没登录：先跑 `liveclip douyin login` 用要发布的账号扫码',
                                  'wrong_account': f'登录的是抖音号 {acc.get("account_id")}，不是配置的 {ACCOUNT_ID}，绝不往别的号发',
                                  'verify': f'创作者中心弹了验证（{acc.get("detail")}），请人自己处理',
                                  'not_configured': '没配置 publish.douyin.account_id，没法核对账号，不发'}.get(
                                      acc['reason'], f'读不到抖音号，没法确认账号，停（{acc["reason"]}）'), 'account')
            run.step('账号核对通过', account=acc['account_id'], nickname=acc.get('name'))
            put_video(run, page, meta['video'])
            fill_title(run, page, title)
            guard(run, page, 'title')
            fill_topics(run, page, tags)
            guard(run, page, 'topics')
            wait_uploaded(run, page)
            set_covers(run, page, meta['cover_3x4'], meta['cover_4x3'])
            guard(run, page, 'cover')
            set_publish_options(run, page, at_bj)
            flags = assistant(run, page)
            run.report['screenshot'] = run.shot('form_full')
            _shot_bottom(run, page)
            # ---- 发布前总核对
            missing = [t for t in T.MUST if norm(t) not in {norm(x) for x in run.report['topics_read']}]
            stop = []
            if missing:
                stop.append(f'必带话题没在编辑器里：{missing}')
            if flags.get('双封面缺失'):
                stop.append('发文助手报「双封面缺失」')
            run.report['ready'] = not stop
            if stop:
                raise Fail(STEP, '；'.join(stop), 'precheck')
            if not a.go:
                run.step('干跑完成：没点发布')
                _hold(a, run)
                return OK, run
            return publish(run, page, d, title, item_id), run
        except Fail:
            run.shot('fail_final')
            raise
        except Exception as e:  # noqa: BLE001
            run.shot('fail_exception')
            raise Fail(STEP, f'{type(e).__name__}: {str(e)[:300]}', 'exception')
        finally:
            run.save()
            ctx.close()


def _grab_item(r, box):
    try:
        if '/aweme/create' in r.url and 'json' in (r.headers.get('content-type') or ''):
            j = r.json()
            if j.get('item_id'):
                box.append(str(j['item_id']))
    except Exception:  # noqa: BLE001
        pass


def _shot_bottom(run, page):
    try:
        btn = page.get_by_role('button', name='发布', exact=True)
        if btn.count():
            btn.first.scroll_into_view_if_needed(timeout=5000)     # 只滚动，不点（#47：别用 find 之类碰发布按钮）
            time.sleep(0.8)
            run.report['screenshot_bottom'] = run.shot('form_bottom', full=False)
    except Exception:  # noqa: BLE001
        pass


def _hold(a, run):
    if a.headless:
        return
    if sys.stdin and sys.stdin.isatty():
        say(f'干跑结束，表单停在发布前。截图/报告：{run.report["run_dir"]}。看完按回车关浏览器（不会发布）')
        try:
            input()
        except EOFError:
            pass
    elif a.hold:
        say(f'窗口再留 {a.hold} 秒给人看（不会发布）')
        time.sleep(a.hold)


def publish(run, page, d, title, item_id):
    btns = page.get_by_role('button', name='发布', exact=True)
    vis = [btns.nth(i) for i in range(btns.count()) if btns.nth(i).is_visible()]
    if len(vis) != 1:
        run.shot('fail_publish_button')
        raise Fail(STEP, f'可见的「发布」按钮有 {len(vis)} 个，不点', 'publish')
    btn = vis[0]
    if not btn.is_enabled():
        run.shot('fail_publish_disabled')
        raise Fail(STEP, '「发布」按钮是灰的', 'publish')
    guard(run, page, 'before_publish')
    btn.scroll_into_view_if_needed()
    btn.click()
    run.step('已点发布')
    run.report['clicked_publish'] = datetime.now().astimezone().isoformat(timespec='seconds')
    # 只点一次：不管后面怎样都不重点（SAU PR#294 同样原则）
    deadline = time.time() + 90
    while time.time() < deadline:
        if '/content/manage' in page.url:
            break
        b = blocked(page)
        if b:
            run.shot('fail_after_publish_verify')
            raise Fail(BLOCKED, f'点了发布后弹验证（{b}）。没记 publish_log。请人在窗口里处理，然后到「内容管理」核对这条有没有发出去，别重发', 'publish')
        time.sleep(1.5)
    else:
        run.shot('fail_no_redirect')
        raise Fail(UNVERIFIED, '点了发布 90 秒没跳到内容管理。没记 publish_log。到抖音「内容管理」核对，别直接重发', 'publish')
    run.step('跳到内容管理', url=page.url, item_id=item_id[-1] if item_id else None)
    # 防重复：一跳转就记（跳到内容管理 = 已提交）；第一条标题核对结果另外报
    PS.done(d, 'douyin', title)
    run.report['logged'] = True
    others = [r['title'] for r in PS.load_log() if r['platform'] == 'douyin' and r['title'] and r['title'] != title]
    found, first = False, None
    for _ in range(20):
        time.sleep(1.5)
        body = page.evaluate('document.body.innerText')
        pos = body.find(title[:14])
        if pos >= 0:
            found = True
            before = [o for o in others if 0 <= body.find(o[:14]) < pos]
            first = not before
            break
    run.report['verify'] = {'title_found': found, 'is_first': first, 'item_id': item_id[-1] if item_id else None}
    run.report['screenshot_manage'] = run.shot('manage', full=False)
    if found and first:
        say(f'发布成功：内容管理第一条是「{title}」，已记 publish_log')
        return OK
    raise Fail(UNVERIFIED, f'已跳到内容管理、已记 publish_log，但列表第一条没核对上（找到 {found}，第一条 {first}）。人工看一眼截图', 'verify')


# ---------------------------------------------------------------- main

T0 = time.time()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='liveclip douyin', description='抖音网页发布（Playwright + 系统 Chrome，独立登录目录）')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('login', help='开有头浏览器，用要发布的账号扫码')
    p.add_argument('--timeout', type=int, default=300, help='最多等几秒（默认 300）')
    p = sub.add_parser('check', help='核对登录 + 账号，0=可发 1=不行')
    p.add_argument('--headless', action='store_true')
    p = sub.add_parser('upload', help='填表（--dry-run）或发布（--go）')
    p.add_argument('dir')
    p.add_argument('--video', help='默认 视频.mp4；精剪版 视频_精剪.mp4')
    p.add_argument('--at', help='定时发布，观众时区（config timezones.audience）"YYYY-MM-DD HH:MM"（2 小时后、14 天内）')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='填完停在发布前')
    g.add_argument('--go', action='store_true', help='真发布')
    p.add_argument('--headless', action='store_true')
    p.add_argument('--hold', type=int, default=0, help='干跑完窗口多留几秒（非终端时）')
    a = ap.parse_args(argv)
    try:
        code, _ = {'login': cmd_login, 'check': cmd_check, 'upload': cmd_upload}[a.cmd](a)
        out = {'ok': code == OK, 'code': code}
    except Fail as e:
        say('停：' + e.msg)
        code, out = e.code, {'ok': False, 'code': e.code, 'step': e.step, 'error': e.msg}
    except KeyboardInterrupt:
        code, out = FAIL, {'ok': False, 'code': FAIL, 'error': '手动中断'}
    run = RUN
    if run is not None:
        out.update({k: run.report.get(k) for k in ['run_dir', 'account', 'title', 'topics_read', 'covers', 'options', 'assistant',
                                                     'screenshot', 'verify', 'warnings'] if run.report.get(k) is not None})
        if a.cmd == 'check':
            out['reason'] = (run.report.get('account') or {}).get('reason')
    print(json.dumps(out, ensure_ascii=False))
    if a.cmd == 'check':
        sys.exit(OK if code == OK else FAIL)
    sys.exit(code)


if __name__ == '__main__':
    main()
