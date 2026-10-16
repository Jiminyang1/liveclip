"""Publish worker: processes the dashboard's publish queue (data/publish_queue.json) one item at a time.

  Bilibili: liveclip.publish.bili --go (biliup API upload)
  Douyin:   liveclip.publish.douyin upload --go --headless (Playwright on a dedicated Chrome profile)

Nothing runs unless config publish.enabled (and the platform's enabled flag) is true. Queue items are only created
when a human clicks "confirm publish" on the dashboard — that click is the authorisation for that one item.
Only publish content you have the rights to.

Robustness:
  - data/publish_log.json is the source of truth: "uploader says success but nothing logged" → failed/needs checking;
    "uploader crashed but it is logged" → done
  - platforms already published are skipped; after a crash, unlogged platforms are marked failed, never auto-retried
  - Douyin pacing: at most publish.douyin.per_day per day (audience timezone) and gap_min minutes apart; when the
    day is full the item waits until next_hour the next day (posting many clips a day starved later ones of reach)
  - Bilibili "posting too frequently" (21566): exponential back-off, 1 h → 12 h
  - single instance (data/.publish_worker.pid); stop flag data/.publish_worker.stop (exit when idle)

  liveclip worker publish            run continuously, poll every 5 s (the dashboard starts it)
  liveclip worker publish --once     process the queue and exit
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from ..config import C, py_mod
from ..platform import notify as _notify

ROOT = C.ROOT
QUEUE = C.DATA / "publish_queue.json"
PUBLOG = C.DATA / "publish_log.json"
LOCK = C.DATA / ".publish_queue.lock"
PIDLOCK = C.DATA / ".publish_worker.pid"
STOPFLAG = C.DATA / ".publish_worker.stop"
LOG = C.LOGS / "publish_worker.log"
RATE_WAIT = 60 * 60          # B站「投稿过于频繁」后等多久再试；连着被拒就翻倍（1、2、4、8 小时…），最多 12 小时
RATE_WAIT_MAX = 12 * 3600   # 一直戳反而可能把限流拖长
DOUYIN_TIMEOUT = 20 * 60
BJ = C.audience_tz
TZL = C.audience_tz_label
NAME = {"douyin": "抖音", "bili": "B站"}
_dy = C.get("publish.douyin") or {}
DY_PER_DAY, DY_GAP, DY_NEXT_HOUR = int(_dy.get("per_day", 5)), int(_dy.get("gap_min", 60)) * 60, int(_dy.get("next_hour", 19))


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(title, text):
    _notify(title, text)


@contextmanager
def locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        q = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        yield q
        tmp = QUEUE.with_suffix(".tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1) + "\n")
        tmp.replace(QUEUE)          # 原子替换，写一半断电也不会坏


def update(job_id, **kw):
    with locked() as q:
        for j in q:
            if j["id"] == job_id:
                for k, v in kw.items():
                    if k == "result":
                        j.setdefault("results", {}).update(v)
                    else:
                        j[k] = v


def logged(rel, platform, since=None):
    """publish_log 里有没有这条这个平台（since：只看某时间之后的记录）。"""
    if not PUBLOG.exists():
        return False
    for r in json.loads(PUBLOG.read_text()):
        if r["dir"] == rel and r["platform"] == platform:
            if since is None or r["time"][:19] >= since:
                return True
    return False


# ---------- B站 ----------
def run_bili(job):
    cmd = py_mod("publish.bili", C.ws(job["dir"]), "--go") + (["--at", job["at"]] if job.get("at") else [])
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        r = None
    out = ((r.stdout + r.stderr) if r else "").strip()
    m = re.search(r'\{"bvid": "(BV\w+)"', out)
    if m:
        return {"state": "done", "note": m.group(1)}
    if logged(job["dir"], "bili"):
        return {"state": "done", "note": "已发（以发布记录为准）"}
    # 完整输出存下来（只看「cookie」字样会误判登录失效：命令行本身就带 cookies.json）
    (C.LOGS / "publish_worker").mkdir(parents=True, exist_ok=True)
    (C.LOGS / "publish_worker" / f"{job['id']}_bili.txt").write_text(out, encoding="utf-8")
    errs = [ln for ln in out.splitlines() if re.search(r"ERROR|Error|code: -?\d+|message|失败", ln) and "--title" not in ln]
    tail = (errs[-1] if errs else (out.splitlines()[-1] if out else ("超时" if r is None else f"退出码 {r.returncode}")))[-240:]
    if re.search(r"code: -101|账号未登录|not logged|登录已过期", out):
        tail = "B站登录失效，要重新扫码：liveclip bili login"
    if re.search(r"code: 21566|投稿过于频繁", out):      # 限流：过一阵自动再试，不算失败
        return {"state": "waiting", "note": "B站说投稿太频繁（21566），过一阵自动重试"}
    return {"state": "failed", "note": tail}


# ---------- 抖音 ----------
def douyin_wait():
    """抖音现在能不能发：能发返回 None，不能发返回 (到点的时间戳, 原因)。按发布记录算（含手动记的）。"""
    ts = []
    for x in json.loads(PUBLOG.read_text(encoding="utf-8")) if PUBLOG.exists() else []:
        if x.get("platform") == "douyin":
            try:
                ts.append(datetime.strptime(x["time"], "%Y-%m-%d %H:%M:%S %z"))
            except (KeyError, ValueError):
                pass
    now = datetime.now(BJ)
    today = [t for t in ts if t.astimezone(BJ).date() == now.date()]
    if len(today) >= DY_PER_DAY:
        nxt = (now + timedelta(days=1)).replace(hour=DY_NEXT_HOUR, minute=0, second=0, microsecond=0)
        return nxt.timestamp(), f"抖音今天（{TZL}时间）已发 {len(today)} 条，每天最多 {DY_PER_DAY} 条"
    if ts and (now - max(ts)).total_seconds() < DY_GAP:
        return max(ts).timestamp() + DY_GAP, f"离上一条抖音不到 {DY_GAP // 60} 分钟"
    return None


PW_CODES = {3: "已发过", 4: "登录的不是配置的账号，停了", 5: "抖音登录失效，要重新扫码：liveclip douyin login",
            6: "碰到验证码/风控，停了，要人在窗口里处理", 7: "已有一个抖音脚本在跑", 8: "页面某一步没做成",
            9: "点了发布但没核实到，先去「内容管理」看，别重发", 10: "素材不全（缺封面或标题超 30 字）",
            11: "抖音发布没在 config 里打开"}


def run_douyin(job):
    started = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    cmd = py_mod("publish.douyin", "upload", C.ws(job["dir"]), "--go", "--headless")
    if job.get("at"):
        cmd += ["--at", job["at"]]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=DOUYIN_TIMEOUT)
        (C.LOGS / "publish_worker").mkdir(parents=True, exist_ok=True)
        (C.LOGS / "publish_worker" / f"{job['id']}_douyin.txt").write_text(r.stdout + r.stderr, encoding="utf-8")
        js = re.findall(r"^\{.*\}$", r.stdout, re.M)
        v = json.loads(js[-1]) if js else {}
        code = v.get("code", r.returncode)
        if v.get("ok"):
            res = {"state": "done", "note": f"脚本发布成功（{v.get('title', '')}），截图在 {v.get('run_dir')}", "cost": 0}
        else:
            res = {"state": "failed", "note": f"{PW_CODES.get(code, '脚本出错')}：{(v.get('error') or r.stderr.strip()[-200:])[:200]}"
                                              f"（截图在 {v.get('run_dir', 'logs/douyin_pw/')}）"}
    except subprocess.TimeoutExpired:
        res = {"state": "failed", "note": f"抖音脚本超过 {DOUYIN_TIMEOUT // 60} 分钟没做完，先去「内容管理」看发没发"}
    did = logged(job["dir"], "douyin", since=started[:19])
    if res["state"] == "done" and not did:
        res = {"state": "failed", "note": "脚本说发了，但发布记录里没有，请到抖音「内容管理」确认：" + res["note"]}
    elif res["state"] != "done" and did:
        res = {"state": "done", "note": "已发（脚本异常结束，但发布记录里有）：" + res["note"]}
    return res


# ---------- 主流程 ----------
def process(job):
    log(f"开始 {job['id']} {job['title']} → {'、'.join(job['platforms'])}{' 定时 ' + job['at'] if job.get('at') else ''}")
    update(job["id"], status="running", started=datetime.now().strftime("%m-%d %H:%M:%S"))
    for p in sorted(job["platforms"], key=lambda x: x != "bili"):   # B站快，先发
        if (job.get("results") or {}).get(p, {}).get("state") == "done":   # 重试/重排：已发的平台跳过
            continue
        if logged(job["dir"], p):
            update(job["id"], result={p: {"state": "done", "note": "已发过（发布记录里有），跳过"}})
            continue
        if not C.publish_enabled(p):
            update(job["id"], result={p: {"state": "failed", "note": f"{NAME.get(p, p)}发布没在 config 里打开（publish.enabled / publish.{p}.enabled）"}})
            continue
        if p == "douyin" and not job.get("at") and (w := douyin_wait()):   # 定时发布由平台排，不管
            until, why = w
            res = {"state": "waiting", "until": until,
                   "note": f"{why}，{datetime.fromtimestamp(until).astimezone(BJ):%m-%d %H:%M} {TZL}时间自动发"}
            update(job["id"], result={p: res})
            log(f"  {p}: waiting {res['note']}")
            continue
        update(job["id"], result={p: {"state": "running"}})
        try:
            res = run_bili(job) if p == "bili" else run_douyin(job)
        except Exception as e:
            res = {"state": "failed", "note": f"出错：{str(e)[-200:]}"}
        update(job["id"], result={p: res})
        log(f"  {p}: {res['state']} {res.get('note', '')}{' $' + str(res['cost']) if res.get('cost') is not None else ''}")
    with locked() as q:
        j = next(x for x in q if x["id"] == job["id"])
        ok = all(j["results"].get(p, {}).get("state") == "done" for p in j["platforms"])
        waiting = any(j["results"].get(p, {}).get("state") == "waiting" for p in j["platforms"])
        bad = any(j["results"].get(p, {}).get("state") == "failed" for p in j["platforms"])
        if waiting and not bad:      # 只剩要等的平台：排回队列，到点再试
            untils = [r["until"] for r in j["results"].values() if r.get("state") == "waiting" and r.get("until")]
            if any(r.get("state") == "waiting" and not r.get("until") for r in j["results"].values()):   # B站限流：翻倍退避
                j["rate_tries"] = j.get("rate_tries", 0) + 1
                untils.append(time.time() + min(RATE_WAIT * 2 ** (j["rate_tries"] - 1), RATE_WAIT_MAX))
            j["status"], j["not_before"] = "pending", max(untils)
            j["retry_at"] = datetime.fromtimestamp(j["not_before"]).strftime("%m-%d %H:%M")
        else:
            j["status"] = "done" if ok else "failed"
        j["finished"] = datetime.now().strftime("%m-%d %H:%M:%S")
    if waiting and not bad:
        log(f"  {job['title']}：要等（B站限流或抖音节奏），{j['retry_at']} 再试")
        return
    summary = "、".join(f"{NAME.get(p, p)}{'✓' if j['results'].get(p, {}).get('state') == 'done' else '✗'}" for p in j["platforms"])
    notify("发布完成" if ok else "发布有失败", f"{job['title']}：{summary}")


def recover():
    """上次进程中断留下的 running：已记进 publish_log 的平台算成功，其余标失败，不自动重发。"""
    with locked() as q:
        for j in q:
            if j["status"] != "running":
                continue
            for p in j["platforms"]:
                if logged(j["dir"], p):
                    j.setdefault("results", {})[p] = {"state": "done", "note": "已发（中断前完成）"}
                elif j.get("results", {}).get(p, {}).get("state") != "done":
                    j.setdefault("results", {})[p] = {"state": "failed", "note": "小工人中断，请到平台确认是否已发出，再点重试"}
            j["status"] = "done" if all(j["results"].get(p, {}).get("state") == "done" for p in j["platforms"]) else "failed"
            log(f"恢复：{j['title']} 上次没做完，标为 {j['status']}")


def next_job():
    with locked() as q:
        for j in q:
            if j["status"] == "pending" and j.get("not_before", 0) <= time.time():
                return dict(j)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip worker publish")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if not C.publish_enabled():
        print("publishing is disabled (config publish.enabled: false) — the publish worker does nothing", flush=True)
        return 0
    PIDLOCK.parent.mkdir(parents=True, exist_ok=True)
    pid_fh = open(PIDLOCK, "a+")
    try:
        fcntl.flock(pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)      # 同一时间只能有一个小工人
    except BlockingIOError:
        print("已经有一个发布小工人在跑，退出", flush=True)
        return 0
    pid_fh.seek(0), pid_fh.truncate(), pid_fh.write(str(os.getpid())), pid_fh.flush()
    log(f"发布小工人启动（pid {os.getpid()}）")
    recover()
    while True:
        try:
            job = next_job()
            if job:
                process(job)
                continue
        except Exception as e:          # 单条出错不让整个小工人退出
            log(f"出错：{e}")
            time.sleep(10)
        if STOPFLAG.exists():           # 想重启小工人：touch 这个文件，它做完手上的活、队列空了就退出
            STOPFLAG.unlink()
            log("收到停止标记，队列空了，退出")
            return 0
        if a.once:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
