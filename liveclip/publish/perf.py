"""Performance snapshot: pull stats for every published clip (Douyin + Bilibili) and join them with the plan features.

Read-only: GET queries only (Bilibili creator center, with the biliup login cookie) and, for Douyin, a script you run
yourself in your logged-in browser tab — nothing is published, edited, deleted or commented.

Usage
  liveclip perf                     Bilibili + the latest Douyin raw dump (if any) → data/perf/YYYY-MM-DD.json
  liveclip perf --douyin-receive    start a local receiver on 127.0.0.1:8766 and wait for the browser to POST Douyin data
  liveclip perf --douyin-js         print the JS to run in the Douyin creator center (DevTools console)
  liveclip perf --no-bili           rebuild from existing Douyin raw data only (offline)

Douyin steps (the creator-center APIs need your login session and the page context):
  1. terminal: `liveclip perf --douyin-receive` (exits after one POST)
  2. browser: open https://creator.douyin.com/creator-micro/content/manage, wait for the list, open DevTools → Console,
     paste the output of `liveclip perf --douyin-js` (work list + traffic sources + retention curves, ~0.3 s per item,
     then POST to 127.0.0.1:8766). It saves data/perf/raw/douyin_YYYY-MM-DD.json.
  3. run `liveclip perf` again to build the snapshot. Stop at any login / verification prompt.

Douyin (per item): views, completion, 5 s completion, 2 s bounce, average watch time/ratio, likes, comments, shares,
  favourites, follows gained/lost, profile visits, fan share, danmaku; traffic-source shares; per-second retention +
  peer curve + the platform's "valley" ranges.
Bilibili (per item): views, likes, coins, favourites, shares, comments, danmaku; average watch time / progress,
  3 s bounce, cover CTR, interaction rate and peer medians, fan / guest views.

Each row is one clip × platform with plan features: score, length tier, category, why, point/theme (data/points.json),
round, risky, hook score, tight version available, which version was published (matched by duration), title and tags
used, publish hour (audience timezone), snapshot age. Items not in publish_log (posted by hand) are matched by title (via=manual).
"""
import argparse
import difflib
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ..config import C

ROOT = C.ROOT
PUBLOG = C.DATA / 'publish_log.json'
COOKIE = C.DATA / 'biliup' / 'cookies.json'
OUT = C.DATA / 'perf'
RAW = OUT / 'raw'
BJ = C.audience_tz
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
PORT = 8766
# 交付目录对应的计划文件（跟播目录 直播跟播_<VID> 自动对 clips/<VID>/follow.json；手动上传 手动上传_<ID> 对 auto.json；
# 其余按标题在 clips/ 里找）。特殊对应写在 data/perf/plan_hints.json：{"交付批次目录名": "clips/<ID>/<plan>.json"}
_hints_f = C.DATA / 'perf' / 'plan_hints.json'
PLAN_HINTS = json.loads(_hints_f.read_text(encoding='utf-8')) if _hints_f.exists() else {}

DOUYIN_JS = r"""
const K=['view_count','completion_rate','completion_rate_5s','bounce_rate_2s','avg_view_second','avg_view_proportion','like_count','comment_count','share_count','favorite_count','subscribe_count','unsubscribe_count','homepage_visit_count','fan_view_proportion','danmaku_count','dislike_count','download_count'];
const get=async u=>(await fetch(u,{credentials:'include'})).json();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let cur=0, works=[];
for(let p=0;p<20;p++){
  const wl=await get('/janus/douyin/creator/pc/work_list?status=0&count=50&max_cursor='+cur+'&scene=star_atlas&device_platform=android&aid=1128');
  (wl.items||[]).forEach((it,i)=>{const a=(wl.aweme_list||[])[i]||{}; const m={}; K.forEach(k=>m[k]=it.metrics?+it.metrics[k]:null);
    const desc=it.description||a.desc||'';
    works.push({aweme_id:a.aweme_id||String(it.id), create_time:it.create_time, duration_ms:a.duration, title:desc.split(/\s#/)[0].trim(),
      item_title:a.item_title||'', desc, tags:(desc.match(/#[^\s#]+/g)||[]).map(x=>x.slice(1)), metrics:m});});
  if(!wl.has_more) break; cur=wl.max_cursor; await sleep(500);
}
const q='aid=2906&app_name=aweme_creator_platform&device_platform=web';
for(const w of works){
  const s=await get('/janus/douyin/creator/data/item/play/source?'+q+'&item_id='+w.aweme_id);
  w.source=Object.fromEntries((s.play_source||[]).filter(x=>x.app_id==1128).map(x=>[x.key,+x.value.toFixed(4)]));
  const r=await get('/janus/douyin/creator/data/realtime/analysis/data_center?user_id=&item_id='+w.aweme_id+'&analysis_type=1');
  const t=r.analysis_trend||{};
  w.retention=(t.current_item||[]).map(x=>+x.value.toFixed(4));
  w.retention_peer=(t.similar_author||[]).map(x=>+x.value.toFixed(4));
  w.valleys=((r.valley_list||{})['1']||[]).map(x=>[+x.start,+x.end]);
  await sleep(300);
}
const res=await fetch('http://127.0.0.1:8766/douyin',{method:'POST',body:JSON.stringify({captured_at:Math.floor(Date.now()/1000),works})});
(await res.text())+' '+works.length
"""


_NAMES = [n.lower() for n in {C.name, C.host_label, C.canonical_name} if n]


def norm(s):
    """标题比对用：去标点、去主播名（平台标题有时加「名字：」前缀）。"""
    t = re.sub(r'[^0-9A-Za-z一-鿿]', '', s or '').lower()
    for n in _NAMES:
        t = t.replace(n, '')
    return t


def nn_of(d):
    return int(re.search(r'\d+', d.name).group())


def today():
    return datetime.now(BJ).strftime('%Y-%m-%d')


# ---------- 交付目录 / 计划 ----------

def parse_copy(d):
    """文案.txt 里各平台的标题。"""
    f = d / '文案.txt'
    out = {}
    if not f.exists():
        return out
    sec = None
    lines = f.read_text(encoding='utf-8').splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r'【(.+?)】', ln)
        if m:
            sec = m.group(1)
        elif ln.strip().startswith('标题') and sec and i + 1 < len(lines):
            out.setdefault(sec, lines[i + 1].strip())
    return out


def deliver_index():
    idx = {}
    for d in sorted(C.DELIVER.glob('*/*_*')):
        if not d.is_dir() or not re.match(r'(第)?\d+(天)?_', d.name):
            continue
        keys = {norm(d.name.split('_', 1)[1])} | {norm(t) for t in parse_copy(d).values()}
        for k in keys:
            if k:
                idx.setdefault(k, d)
    return idx


_plans = None


def all_plans():
    global _plans
    if _plans is None:
        _plans = []
        for f in sorted(C.CLIPS.glob('*/*.json')):
            try:
                j = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            if isinstance(j, dict) and isinstance(j.get('clips'), list):
                _plans.append((f, j['clips']))
    return _plans


def find_plan(d):
    """交付目录 → (计划文件, 第几条, 计划条目)。"""
    parent, nn = d.parent.name, nn_of(d)
    cands = []
    if parent.startswith('直播跟播_'):
        cands.append(C.CLIPS / parent[len('直播跟播_'):] / 'follow.json')
    if parent.startswith('手动上传_'):
        cands.append(C.CLIPS / parent[len('手动上传_'):] / 'auto.json')
    if parent in PLAN_HINTS:
        cands.append(C.ws(PLAN_HINTS[parent]))
    want = norm(d.name.split('_', 1)[1])
    for f in cands:
        if f.exists():
            cl = json.loads(f.read_text(encoding='utf-8'))['clips']
            if nn <= len(cl):
                return f, nn, cl[nn - 1]
    for f, cl in all_plans():      # 按标题找
        for i, c in enumerate(cl):
            if norm(c.get('title')) == want:
                return f, i + 1, c
    return None, None, None


def tier(sec):
    if sec is None:
        return None
    return 'short' if sec <= 90 else 'medium' if sec <= 180 else 'long'


_points = None


def point_of(rel):
    """观点库 data/points.json（按交付目录 id）里的 point / theme。"""
    global _points
    if _points is None:
        f = C.DATA / 'points.json'
        try:
            j = json.loads(f.read_text(encoding='utf-8'))
            rows = j.get('rows', []) if isinstance(j, dict) else j
            _points = {x.get('id'): x for x in rows if isinstance(x, dict)}
        except Exception:
            _points = {}
    return _points.get(rel) or {}


def plan_features(d):
    feat = {'deliver_dir': C.rel(d) if d else None}
    if not d:
        return feat
    feat['tight_available'] = (d / '视频_精剪.mp4').exists() or (d / '视频_原版.mp4').exists()
    pt = point_of(feat['deliver_dir'])
    feat['point'], feat['theme'] = pt.get('point'), pt.get('theme')
    f, n, c = find_plan(d)
    if not c:
        return feat
    dur = round(c['end'] - c['start'], 1)
    hp = c.get('hook_pass') or {}
    feat.update({
        'plan_file': C.rel(f), 'plan_n': n,
        'score': c.get('score'), 'category': c.get('category'), 'plan_duration': dur, 'tier': tier(dur),
        'round': c.get('round'), 'risky': c.get('risky'), 'layout': c.get('layout'),
        'has_hook': c.get('hook_start') is not None, 'hook_score': hp.get('score'), 'hook_type': hp.get('type'),
        'why': c.get('why'), 'point': c.get('point') or feat['point'], 'plan_tags': c.get('tags'),
    })
    return feat


# ---------- B站 ----------

def bili_get(url, ck):
    r = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://member.bilibili.com/', 'Cookie': ck})
    j = json.loads(urllib.request.urlopen(r, timeout=20).read())
    if j.get('code') != 0:
        raise RuntimeError(f"{url.split('?')[0]} → {j.get('code')} {j.get('message')}")
    return j['data']


def fetch_bili():
    c = json.loads(COOKIE.read_text())
    ck = '; '.join(f"{x['name']}={x['value']}" for x in c['cookie_info']['cookies'])
    arcs, pn = [], 1
    while True:
        d = bili_get('https://member.bilibili.com/x/web/archives?status=is_pubing%2Cpubed%2Cnot_pubed'
                     f'&pn={pn}&ps=50&coop=1&interactive=1', ck)
        arcs += d.get('arc_audits') or []
        if len(arcs) >= d['page']['count'] or not d.get('arc_audits'):
            break
        pn += 1
    rows = []
    for a in arcs:
        A, st = a['Archive'], a['stat']
        bv = A['bvid']
        row = {'platform': 'bili', 'id': bv, 'title': A['title'], 'state': A.get('state_desc'),
               'tags': [t for t in (A.get('tag') or '').split(',') if t],
               'pub_ts': A['ptime'], 'duration': A['duration'],
               'views': st['view'], 'likes': st['like'], 'coins': st['coin'], 'favorites': st['favorite'],
               'shares': st['share'], 'comments': st['reply'], 'danmaku': st['danmaku']}
        try:
            da = bili_get(f'https://member.bilibili.com/x/web/data/archive?bvid={bv}', ck)
            p = da.get('play') or {}
            row.update({'avg_view_sec': p.get('avg_duration'),
                        'avg_view_proportion': p['rate'] / 1e4 if p.get('rate') else None,
                        'fan_views': (da.get('group') or {}).get('fans'), 'guest_views': (da.get('group') or {}).get('guest')})
        except Exception as e:
            row['err_archive'] = str(e)
        try:
            cmp_ = bili_get(f'https://member.bilibili.com/x/web/data/archive_diagnose/compare?bvid={bv}', ck)
            s = next((x['stat'] for x in cmp_.get('list') or [] if x.get('bvid') == bv), None)
            if s:
                r4 = lambda k: s[k] / 1e4 if s.get(k) is not None else None
                row.update({'full_play_ratio': r4('full_play_ratio'),        # B站「完播率」= 平均播放进度
                            'bounce_3s': r4('crash_rate'), 'bounce_3s_peer_med': r4('crash_rate_med'),
                            'cover_ctr': r4('tm_rate'), 'interact_rate': r4('interact_rate'),
                            'interact_rate_peer_med': r4('interact_rate_med'),
                            'guest_play_rate': r4('play_viewer_rate'), 'new_follows': s.get('total_new_attention_cnt'),
                            'unfollows': s.get('unfollow')})
        except Exception as e:
            row['err_compare'] = str(e)
        rows.append(row)
        time.sleep(0.4)
    return rows


# ---------- 抖音 ----------

def latest_douyin_raw():
    fs = sorted(RAW.glob('douyin_*.json'))
    return fs[-1] if fs else None


def douyin_rows(raw):
    j = json.loads(raw.read_text(encoding='utf-8'))
    rows = []
    for w in j['works']:
        m = w['metrics']
        ret = w.get('retention') or []
        pick = lambda s: ret[s] if s < len(ret) else None
        rows.append({
            'platform': 'douyin', 'id': w['aweme_id'], 'title': w.get('item_title') or w['title'],
            'tags': w.get('tags'), 'desc': w.get('desc'), 'pub_ts': w['create_time'],
            'duration': round(w['duration_ms'] / 1000, 1) if w.get('duration_ms') else None,
            'views': m['view_count'], 'completion': m['completion_rate'], 'completion_5s': m['completion_rate_5s'],
            'bounce_2s': m['bounce_rate_2s'], 'avg_view_sec': m['avg_view_second'],
            'avg_view_proportion': m['avg_view_proportion'], 'likes': m['like_count'], 'comments': m['comment_count'],
            'shares': m['share_count'], 'favorites': m['favorite_count'], 'new_follows': m['subscribe_count'],
            'unfollows': m['unsubscribe_count'], 'homepage_visits': m['homepage_visit_count'],
            'fan_view_proportion': m['fan_view_proportion'], 'danmaku': m['danmaku_count'],
            'not_interested': m['dislike_count'], 'downloads': m['download_count'],
            'source': w.get('source'),
            'source_reco': (w.get('source') or {}).get('homepage_hot'),
            'source_search': (w.get('source') or {}).get('search'),
            'ret_3s': pick(3), 'ret_5s': pick(5), 'ret_10s': pick(10), 'ret_30s': pick(30),
            'ret_half': ret[len(ret) // 2] if ret else None,
            'retention': ret, 'retention_peer': w.get('retention_peer'), 'valleys': w.get('valleys'),
            'captured_at': j.get('captured_at'),
        })
    return rows


def receive():
    RAW.mkdir(parents=True, exist_ok=True)

    class H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header('Access-Control-Allow-Origin', 'https://creator.douyin.com')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Access-Control-Allow-Private-Network', 'true')

        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            j = json.loads(body)
            f = RAW / f'douyin_{today()}.json'
            f.write_text(json.dumps(j, ensure_ascii=False), encoding='utf-8')
            self.send_response(200); self._cors(); self.end_headers(); self.wfile.write(b'ok')
            print(f'收到 {len(j.get("works", []))} 条 → {C.rel(f)}')
            self.server.done = True

        def log_message(self, *a):
            pass

    srv = HTTPServer(('127.0.0.1', PORT), H)
    srv.done = False
    print(f'等抖音数据：http://127.0.0.1:{PORT}/douyin（收到一次就退出，Ctrl-C 取消）')
    while not srv.done:
        srv.handle_request()


# ---------- 拼快照 ----------

def bj(ts):
    return datetime.fromtimestamp(ts, BJ)


def file_version(f):
    """交付目录里的视频文件 → original / tight。
    新约定：视频.mp4 正常版、视频_精剪.mp4 精剪；旧约定（auto_clip 不加 --tight-separate）：视频.mp4 是精剪、视频_原版.mp4 是原版。"""
    if f.name == '视频_精剪.mp4':
        return 'tight'
    if f.name == '视频_原版.mp4':
        return 'original'
    if f.name == '视频.mp4':
        return 'tight' if (f.parent / '视频_原版.mp4').exists() else 'original'
    return None


_durs = {}


def media_dur(f):
    if f not in _durs:
        try:
            out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(f)],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            _durs[f] = float(out)
        except Exception:
            _durs[f] = None
    return _durs[f]


def infer_version(d, platform_dur, plan_file=None, nn=None):
    """发的是哪个文件：拿平台时长去对交付目录里各版本视频、以及 clips/<VID>/<计划>/NN_*.mp4 各版剪辑的时长
    （差 ≤1.5 秒算对上；同时对上原版和精剪就不判）。返回 (version, basis, 对上的剪辑目录名)。
    发布工具（publish_serve / bili_upload）默认传 视频.mp4，publish_log、队列、发布会话日志里都没出现过精剪；
    时长都对不上时退回这个默认（basis=default_视频.mp4）；连交付目录都没有就是 unknown。"""
    if not d:
        return 'unknown', 'no_deliver_dir', None
    cands = [(f, file_version(f)) for f in sorted(d.glob('视频*.mp4')) if file_version(f)]
    if plan_file and nn:
        for f in sorted(C.ws(plan_file).parent.glob(f'*/{nn:02d}_*.mp4')):
            cands.append((f, 'tight' if f.parent.name.endswith('_tight') else 'original'))
    if platform_dur:
        hits = [(f, v) for f, v in cands if media_dur(f) and abs(media_dur(f) - platform_dur) <= 1.5]
        if hits and len({v for _, v in hits}) == 1:
            f = min(hits, key=lambda x: abs(media_dur(x[0]) - platform_dur))[0]
            variant = None if f.parent == d else f.parent.name
            return hits[0][1], f'duration_match:{C.rel(f)}', variant
    if (d / '视频.mp4').exists():
        return file_version(d / '视频.mp4'), 'default_视频.mp4', None
    return 'unknown', 'no_match', None


def build(bili, douyin, snap_ts):
    idx = deliver_index()
    log = json.loads(PUBLOG.read_text(encoding='utf-8')) if PUBLOG.exists() else []
    logged = {}
    for e in log:
        d = C.ws(e['dir'])
        logged.setdefault((e['platform'], norm(e['title'])), (d, e['time']))
        logged.setdefault((e['platform'], norm(d.name.split('_', 1)[1])), (d, e['time']))
    rows = []
    for r in bili + douyin:
        k = norm(r['title'])
        hit = logged.get((r['platform'], k))
        if not hit:   # 平台标题可能带「名字：」前缀等，退一步用包含关系
            hit = next((v for (p, t), v in logged.items() if p == r['platform'] and len(t) >= 6 and (t in k or k in t)), None)
        d = hit[0] if hit else idx.get(k) or next((v for t, v in idx.items() if len(k) >= 6 and (t in k or k in t)), None)
        if not d and idx:   # 发布时改过标题：取字面最像的（≥0.6）
            sc, t = max((difflib.SequenceMatcher(None, k, t).ratio(), t) for t in idx)
            d = idx[t] if sc >= 0.6 else None
        r['via'] = 'auto' if hit else 'manual'           # auto = publish_log 里有（面板/小工人发的）
        r['logged_time'] = hit[1] if hit else None
        r.update(plan_features(d))
        r['version'], r['version_basis'], variant = infer_version(d, r.get('duration'), r.get('plan_file'), r.get('plan_n'))
        pf = r.get('plan_file')
        if variant and pf and C.ws(pf).parent.joinpath(variant.removesuffix('_tight') + '.json').exists():
            # 实际发的是另一版剪辑（比如手动发的是改预告之前的版本）→ 计划特征换成那一版
            alt = C.ws(pf).parent / (variant.removesuffix('_tight') + '.json')
            cl = json.loads(alt.read_text(encoding='utf-8'))['clips']
            if r['plan_n'] <= len(cl) and C.rel(alt) != pf:
                c = cl[r['plan_n'] - 1]
                hp = c.get('hook_pass') or {}
                r.update({'plan_file': C.rel(alt), 'score': c.get('score'), 'category': c.get('category'),
                          'plan_duration': round(c['end'] - c['start'], 1), 'tier': tier(c['end'] - c['start']),
                          'has_hook': c.get('hook_start') is not None, 'hook_score': hp.get('score'),
                          'hook_type': hp.get('type'), 'why': c.get('why'), 'point': c.get('point') or r.get('point')})
        if r.get('pub_ts'):
            t = bj(r['pub_ts'])
            r['pub_bj'] = t.strftime('%Y-%m-%d %H:%M')
            r['pub_hour_bj'] = t.hour
            r['age_h'] = round((snap_ts - r['pub_ts']) / 3600, 1)
        rows.append(r)
    rows.sort(key=lambda r: (r.get('pub_ts') or 0, r['platform']))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog='liveclip perf', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--douyin-js', action='store_true', help='打印抖音创作者中心里执行的 JS')
    ap.add_argument('--douyin-receive', action='store_true', help='起本地接收端收抖音数据')
    ap.add_argument('--douyin-raw', help='指定抖音原始数据文件（默认 data/perf/raw/ 里最新的）')
    ap.add_argument('--no-bili', action='store_true')
    a = ap.parse_args(argv)
    if a.douyin_js:
        print(DOUYIN_JS.strip()); return
    if a.douyin_receive:
        receive(); return

    snap_ts = int(time.time())
    bili = []
    if not a.no_bili:
        try:
            bili = fetch_bili()
        except Exception as e:
            print(f'B站没拉到：{e}（cookie 失效就 liveclip bili login）', file=sys.stderr)
    raw = Path(a.douyin_raw).resolve() if a.douyin_raw else latest_douyin_raw()
    douyin = douyin_rows(raw) if raw else []
    if raw:
        cap = json.loads(raw.read_text(encoding='utf-8')).get('captured_at')
        if cap and snap_ts - cap > 6 * 3600:
            print(f'注意：抖音数据是 {bj(cap):%m-%d %H:%M} 抓的，不是现在', file=sys.stderr)
    else:
        print('没有抖音原始数据（data/perf/raw/douyin_*.json），这次只有 B站。步骤见 --help', file=sys.stderr)
    rows = build(bili, douyin, snap_ts)
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f'{today()}.json'
    f.write_text(json.dumps({'snapshot_at': bj(snap_ts).strftime('%Y-%m-%d %H:%M'),
                             'douyin_raw': C.rel(raw) if raw else None,
                             'rows': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    n = {p: sum(r['platform'] == p for r in rows) for p in ('douyin', 'bili')}
    auto = sum(r['via'] == 'auto' for r in rows)
    planned = sum(r.get('score') is not None for r in rows)
    print(f'{C.rel(f)}：抖音 {n["douyin"]} 条、B站 {n["bili"]} 条；publish_log 里的 {auto} 条，对上计划 {planned} 条')


if __name__ == '__main__':
    main()
