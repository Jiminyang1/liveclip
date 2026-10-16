"""Speech-to-text for long recordings → subtitle JSON {"body": [{"from", "to", "content"[, "spk"]}]}.

Engines (--engine):
- bailian (default, recommended for livestreams): Alibaba Cloud Bailian / DashScope file transcription
  (model from config asr.bailian_model, hot words from asr.hotwords). Word-level timestamps.
  Called directly over DashScope REST: the whole recording is compressed to one opus file (16 kHz mono 24 kbps,
  ~11 MB per hour), uploaded to Bailian's temporary OSS (kept 48 h) and transcribed as one async task.
  Region fallback: Beijing (cn) once more → international (intl) twice → the `bl` CLI in 5-minute chunks (if installed).
  "asr.route" in the output records which path was used. Recordings longer than BAILIAN_PART are split at pauses.
  Raw API results are cached in <out>_raw/ so re-runs are free.
  API key: $DASHSCOPE_API_KEY (and optionally $DASHSCOPE_API_KEY_INTL); falls back to ~/.bailian/config.json.
- sensevoice: local FunASR SenseVoice-Small + fsmn-vad (pip install 'liveclip[asr-local]'); fast, no hot words,
  approximate timestamps; also marks laughter/applause in "event". Used as the fallback when Bailian fails.

--diarize (bailian only): speaker labels. Each line gets "spk" = streamer label / guest label / "其他" (other) / "未知"
  (unknown) — labels come from config streamer.speaker_label / guest_label — and the top level gets "speakers".
  One task: the API's speaker ids are consistent for the whole file. Split into parts: ids differ per part, so parts are
  matched with CAM++ speaker embeddings (needs asr-local) and every sentence is re-checked (match_speakers).
  Who is the streamer: the speaker who talks most while the frame shows a single person full-screen
  (not a split-screen co-stream) — find_host().

Usage:
    liveclip asr videos/x.mp4 --out data/asr/x_bailian.json
    liveclip asr videos/x.mp4 --engine bailian --diarize --out data/asr/x_bailian_spk.json   # 2 speakers by default
    liveclip asr videos/x.mp4 --engine sensevoice --out data/asr/x.json
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import C

SR = 16000
BAILIAN_MODEL = C.get("asr.bailian_model")
BAILIAN_VOCAB = dict(C.get("asr.hotwords") or {})
BAILIAN_CHUNK = 300   # 秒，只给退路 run_bailian（bl 分段）用
BAILIAN_PART = 4 * 3600   # 直连时一个任务最多多少秒音频，更长的在停顿处平均切成几份（实测 2h48m 一个任务成功；4 小时以上没试过，接口上限未知）
DASHSCOPE = {"cn": "https://dashscope.aliyuncs.com/api/v1", "intl": "https://dashscope-intl.aliyuncs.com/api/v1"}
DASHSCOPE_TRIES = ["cn", "cn", "intl", "intl"]   # 北京站失败重试一次，再换国际站（新加坡）


def AutoModel(**kw):
    """FunASR model loader (lazy import: FunASR/torch are only needed for SenseVoice, VAD and speaker embeddings)."""
    try:
        from funasr import AutoModel as _AM
    except ImportError:
        raise SystemExit("This step needs FunASR: pip install 'liveclip[asr-local]'") from None
    return _AM(**kw)


def pick_device(dev):
    if dev and dev != "auto":
        return dev
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def load_audio(path):
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        # 按时间戳对齐、开头缺的声音补静音：抖音直播回放开头常有一段解不出来的声音，
        # 直接连续解码会让所有字幕时间提前（10/6 那场提前 5.7 秒），剪辑却按时间戳跳转，字幕就对不上嘴
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-af", "aresample=async=1:first_pts=0",
                        "-ac", "1", "-ar", str(SR), tmp.name], check=True)
        audio, _ = sf.read(tmp.name, dtype="float32")
    return audio


FILLERS = set("嗯啊呃哦噢唉哎额")
BREAK = set("，,。！？!?；;")
KEEP_END = set("？！?!")
MAX_CHARS = 20


def sentences_from_tokens(offset, words, stamps, spks=None):
    """按字级时间戳切句：遇到标点、字间停顿超过 0.5 秒、或满 MAX_CHARS 个字就断开；
    每句的时间 = 第一个字开始到最后一个字结束（2026-10-08 之前按字数比例分配，长段里字幕会对不上）。
    spks（可选，和 words 一一对应的说话人）：给了就在换人处断开、碎片不并到别人的句子上，每句多带第 4 项说话人。"""
    out, cur = [], []
    spks = spks if spks is not None else [None] * len(words)

    def flush():
        text = "".join(w for w, _, _ in cur).strip()
        if text and not set(text) <= FILLERS | BREAK:
            sent = [offset + cur[0][1][0] / 1000, offset + cur[-1][1][1] / 1000, text]
            out.append(sent + [cur[0][2]] if cur[0][2] is not None else sent)
        cur.clear()

    for w, ts, sp in zip(words, stamps, spks):
        if w in BREAK:
            if w in KEEP_END and cur:
                cur.append((w, (cur[-1][1][1], cur[-1][1][1]), cur[-1][2]))
            flush()
            continue
        if cur and (ts[0] - cur[-1][1][1] > 500 or sp != cur[-1][2]):
            flush()
        cur.append((w, ts, sp))
        if sum(len(x.strip()) for x, _, _ in cur) >= MAX_CHARS:   # 按字数（英文按字母数）算，token 可能是整词
            flush()
    flush()
    # 不超过 3 个字的碎片（满字数切出来的尾巴、单独的"5%"）并回上一句，间隔要小于 0.6 秒；分了说话人的只并同一个人的
    merged = []
    for sent in out:
        if merged and len(sent[2]) <= 3 and sent[0] - merged[-1][1] < 0.6 and sent[3:] == merged[-1][3:]:
            merged[-1][1] = sent[1]
            merged[-1][2] += sent[2]
        else:
            merged.append(sent)
    out = merged
    # 字级时间戳只有发音那一小段，句尾往后延 0.4 秒，但不压到下一句
    for i, sent in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else sent[1] + 0.4
        sent[1] = max(sent[1], min(sent[1] + 0.4, nxt - 0.02))
    return out


def dashscope_key(site):
    """API key: $DASHSCOPE_API_KEY (Beijing) / $DASHSCOPE_API_KEY_INTL (Singapore; falls back to the same key);
    otherwise the `bl` CLI config ~/.bailian/config.json. Never printed or logged."""
    env = os.environ.get("DASHSCOPE_API_KEY_INTL" if site == "intl" else "DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env
    f = Path.home() / ".bailian/config.json"
    if not f.exists():
        raise RuntimeError("no DashScope key: set DASHSCOPE_API_KEY (see .env.example)")
    c = json.loads(f.read_text(encoding="utf-8"))
    return c["api_key"] if site == "cn" else (c.get("intl") or {}).get("api_key") or c["api_key"]


def dashscope_one(path, site, vocab, diarize=0, timeout=3600):
    """一个音频文件交一个转写任务，直接调 DashScope REST（不经过 bl）：
    取上传凭证 → 表单直传百炼临时 OSS（oss://，48 小时）→ 提交异步转写 → 轮询 → 下载结果 JSON。
    实测跨洋直传约 2.9 MB/s（bl 命令行自带的上传慢得多）。失败抛 RuntimeError。"""
    import requests
    base, t0 = DASHSCOPE[site], time.time()
    h = {"Authorization": f"Bearer {dashscope_key(site)}"}
    try:
        r = requests.get(f"{base}/uploads", params={"action": "getPolicy", "model": BAILIAN_MODEL}, headers=h, timeout=30)
        r.raise_for_status()
        p = r.json()["data"]
        key = f"{p['upload_dir']}/{Path(path).name}"
        with open(path, "rb") as f:
            r = requests.post(p["upload_host"], files={"file": (Path(path).name, f)}, data={
                "OSSAccessKeyId": p["oss_access_key_id"], "Signature": p["signature"], "policy": p["policy"], "key": key,
                "x-oss-object-acl": p["x_oss_object_acl"], "x-oss-forbid-overwrite": p["x_oss_forbid_overwrite"],
                "success_action_status": "200"}, timeout=1800)
        r.raise_for_status()
        t_up = time.time() - t0
        # 热词参数名是 vocabulary（词→权重）；hot_words、context 试过没效果（2026-10-11）
        params = {"language_hints": ["zh"], "vocabulary": vocab}
        if diarize:
            params.update({"diarization_enabled": True, "speaker_count": diarize})
        r = requests.post(f"{base}/services/audio/asr/transcription", timeout=60, headers={
            **h, "Content-Type": "application/json", "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
            json={"model": BAILIAN_MODEL, "input": {"file_url": f"oss://{key}"}, "parameters": params})
        r.raise_for_status()
        tid = r.json()["output"]["task_id"]
        while True:
            time.sleep(5)
            o = requests.get(f"{base}/tasks/{tid}", headers=h, timeout=30).json().get("output", {})
            if o.get("task_status") in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
                break
            if time.time() - t0 > timeout:
                raise RuntimeError(f"转写超过 {timeout}s 没完成（task {tid}）")
        if o["task_status"] != "SUCCEEDED":
            raise RuntimeError(f"任务 {o['task_status']}：{o.get('code', '')} {o.get('message', '')}"[:300])
        res = o.get("result") or (o.get("results") or [{}])[0]
        data = requests.get(res["transcription_url"], timeout=120).json() if res.get("transcription_url") else res
        data["transcripts"]     # 结构不对就在这里报错
    except RuntimeError:
        raise
    except Exception as e:      # 网络、HTTP 错误、返回结构不对（报错信息里没有密钥）
        raise RuntimeError(f"{type(e).__name__}: {str(e)[:300]}") from None
    data["_route"] = {"site": site, "upload_sec": round(t_up), "total_sec": round(time.time() - t0)}
    return data


def run_dashscope(audio, vocab, diarize, raw_dir, get_segs):
    """整条音频压成一个 opus（16 kHz 单声道 24 kbps，1 小时约 11 MB）交一个任务；超过 BAILIAN_PART 秒的
    在 VAD 停顿处平均切成最少的几份（各份之间说话人编号不通，要 match_speakers 对）。
    每份依次试 DASHSCOPE_TRIES 里的站点。原始结果存 raw_dir/partN.json，设置相同就直接用，不再付费。
    返回 ([(偏移毫秒, 接口 sentences)], 路线说明)；全部失败抛 RuntimeError。"""
    total = len(audio) / SR
    n = int(np.ceil(total / BAILIAN_PART))
    if n == 1:
        parts = [[0, int(total * 1000)]]
    else:      # 切点放在离等分点最近的停顿（两句之间）
        segs = get_segs()
        gaps = [(segs[i][1] + segs[i + 1][0]) // 2 for i in range(len(segs) - 1)]
        cuts = [min(gaps, key=lambda g: abs(g - total * 1000 * k / n)) for k in range(1, n)]
        bounds = [0, *cuts, int(total * 1000)]
        parts = [[bounds[i], bounds[i + 1]] for i in range(n)]
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta = {"engine": "dashscope", "model": BAILIAN_MODEL, "diarize": diarize, "vocab": vocab, "parts": parts}
    mf = raw_dir / "meta.json"
    if not (mf.exists() and json.loads(mf.read_text(encoding="utf-8")) == meta):
        for f in raw_dir.glob("part*.json"):     # 设置变了，旧结果对不上
            f.unlink()
        mf.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    out, routes = [], []
    with tempfile.TemporaryDirectory() as d:
        for i, (b, e) in enumerate(parts):
            f = raw_dir / f"part{i}.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                print(f"沿用 {f}")
            else:
                ogg = f"{d}/part{i}.ogg"
                x = audio[int(b * SR / 1000):int(e * SR / 1000)]
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                                "-c:a", "libopus", "-b:a", "24k", "-compression_level", "5", ogg], input=x.astype("<f4").tobytes(), check=True)
                print(f"第 {i + 1}/{len(parts)} 份 {(e - b) / 60000:.0f} 分钟，{os.path.getsize(ogg) / 1e6:.1f} MB，上传转写…")
                data, errs = None, []
                for site in DASHSCOPE_TRIES:
                    try:
                        data = dashscope_one(ogg, site, vocab, diarize)
                        break
                    except RuntimeError as err:
                        errs.append(f"{site}: {err}")
                        print(f"  {site} 失败：{err}")
                        time.sleep(5)
                if data is None:
                    raise RuntimeError("；".join(errs))
                f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                try:   # 记账（liveclip.cost）：百炼按 token 实扣
                    from . import cost as costlog
                    u = data.get("usage") or {}
                    tok = {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}
                    costlog.record("bailian", "语音识别", label=costlog.label_of(raw_dir), cny=costlog.bailian_cny(BAILIAN_MODEL, tok),
                                   model=BAILIAN_MODEL, tokens=tok, sec=round((e - b) / 1000))
                except Exception:  # noqa: BLE001
                    pass
                r = data["_route"]
                print(f"  {r['site']} 成功：上传 {r['upload_sec']}s，共 {r['total_sec']}s")
            routes.append(data.get("_route", {}).get("site", "?"))
            out.append((b, data["transcripts"][0].get("sentences", [])))
    sites = {"cn": "北京站", "intl": "国际站"}
    route = f"百炼直连{'/'.join(sorted({sites.get(x, x) for x in routes}))}，整条{'一个任务' if n == 1 else f'切 {n} 份'}"
    return out, route


def bailian_one(path, vocab, tries=3, diarize=0, keep=None):
    """识别一个短文件，返回接口的 sentences。偶尔超时就重试。
    diarize>0 时让接口分说话人（--speaker-count 是预计人数），每句带 speaker_id，只在这一个文件内部一致。
    keep：接口原始结果另存一份到这里（分说话人模式用来断点续跑、重新聚类时不用再付费识别）。"""
    res = f"{path}.json"
    spk = ["--diarization", "--speaker-count", str(diarize)] if diarize else []
    for k in range(tries):
        r = subprocess.run(["bl", "speech", "recognize", "--url", path, "--model", BAILIAN_MODEL, "--language", "zh",
                            "--vocabulary", json.dumps(vocab, ensure_ascii=False), *spk, "--out", res, "--quiet"],
                           capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
        if r.returncode == 0:
            if keep:
                Path(keep).write_text(Path(res).read_text(encoding="utf-8"), encoding="utf-8")
            return json.loads(Path(res).read_text(encoding="utf-8"))["transcripts"][0].get("sentences", [])
        print(f"  {Path(path).name} 第 {k + 1} 次失败：{r.stdout.strip().splitlines()[-1:] or r.stderr[-200:]}")
    raise SystemExit(f"{path} 识别失败 {tries} 次")


def run_bailian(audio, segs, vocab, diarize=0, raw_dir=None):
    """退路（直连失败时用）：经 bl 命令行，切成约 BAILIAN_CHUNK 秒一段（切点放在 VAD 的停顿里），压成 opus 并行识别。
    bl 自己的上传很慢（跨洋几十 KB/s，等待上限约 120 秒），整场传不完，所以切 5 分钟一段。
    返回 [(偏移毫秒, 接口 sentences)]，交给 tokens_from_parts。分说话人时段内编号只在本段内一致，要 match_speakers 对。
    raw_dir 里有同样切段的原始结果就直接用，不重新上传。"""
    from concurrent.futures import ThreadPoolExecutor
    chunks, cur = [], None
    for b, e in segs:
        if cur and e - cur[0] > BAILIAN_CHUNK * 1000:
            chunks.append(cur)
            cur = None
        cur = [cur[0] if cur else max(0, b - 200), e + 200]
    if cur:
        chunks.append(cur)
    results = [None] * len(chunks)
    if raw_dir:
        raw_dir = Path(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        meta = {"model": BAILIAN_MODEL, "diarize": diarize, "vocab": vocab, "chunks": chunks}
        mf = raw_dir / "chunks.json"
        if mf.exists() and json.loads(mf.read_text(encoding="utf-8")) == meta:
            for i in range(len(chunks)):
                f = raw_dir / f"{i:03d}.json"
                if f.exists():
                    results[i] = json.loads(f.read_text(encoding="utf-8"))["transcripts"][0].get("sentences", [])
        else:
            for f in raw_dir.glob("[0-9][0-9][0-9].json"):     # 切段变了，旧结果对不上
                f.unlink()
            mf.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    todo = [i for i, r in enumerate(results) if r is None]
    if len(todo) < len(chunks):
        print(f"沿用 {raw_dir} 里 {len(chunks) - len(todo)} 段的识别结果")
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for i in todo:
            b, e = chunks[i]
            wav, ogg = f"{d}/{i:03d}.wav", f"{d}/{i:03d}.ogg"
            sf.write(wav, audio[int(b * SR / 1000):int(e * SR / 1000)], SR)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "32k", ogg], check=True)
            paths.append(ogg)
        if todo:
            print(f"切成 {len(chunks)} 段，上传识别 {len(todo)} 段…")
        with ThreadPoolExecutor(max_workers=6) as ex:
            got = list(ex.map(lambda ip: bailian_one(ip[1], vocab, diarize=diarize,
                                                     keep=raw_dir / f"{ip[0]:03d}.json" if raw_dir else None),
                              zip(todo, paths)))
    for i, r in zip(todo, got):
        results[i] = r
    return [(off, sents) for (off, _), sents in zip(chunks, results)]


def tokens_from_parts(parts):
    """接口结果 [(偏移毫秒, sentences)] → (字或词, (开始毫秒, 结束毫秒)) 列表，标点单独成一项，可直接给 sentences_from_tokens；
    另返回每个字属于接口的第几句，和接口的整句 [(份号, 份内说话人编号, 开始秒, 结束秒)]（分说话人用）。"""
    words, stamps, spks, spk_sents = [], [], [], []
    for ci, (off, sents) in enumerate(parts):
        for sent in sents:
            sp = len(spk_sents)
            spk_sents.append((ci, sent.get("speaker_id"), (sent["begin_time"] + off) / 1000, (sent["end_time"] + off) / 1000))
            for w in sent.get("words", []):
                t, a, b = w["text"].strip(), w["begin_time"] + off, w["end_time"] + off
                if not t:
                    continue
                prev = words[-1] if words else ""
                # 英文单词之间补空格；don + 't 这种缩写不补。中文词整词一个单位，不拆字（拆了会在词中间断行）
                latin = t[0].isascii() and t[0].isalnum()
                if latin and prev.strip()[-1:].isascii() and prev.strip()[-1:].isalnum():
                    t = " " + t
                words.append(t)
                stamps.append((a, b))
                spks.append(sp)
                for ch in w.get("punctuation", "") or "":
                    words.append({".": "。"}.get(ch, ch))     # 英文句号也要断句
                    stamps.append((b, b))
                    spks.append(sp)
            words.append("。")       # 接口的一句结束就断开
            stamps.append(stamps[-1] if stamps else (0, 0))
            spks.append(sp)
    return words, stamps, spks, spk_sents

SPK_MODEL = "cam++"   # FunASR 别名 → damo/speech_campplus_sv_zh-cn_16k-common（CAM++ 声纹，192 维，已在 ~/.cache/modelscope）
SPK_SIM = 0.5         # 两组声纹平均余弦相似度高于这个才算同一个人（层次聚类的停止线；10/11 同一人的组之间 0.8 以上，主播和嘉宾 0.5 上下）
SPK_FLIP = 0.15       # 逐句校正：和另一个人的中心比和自己的高出这么多才改（10/11 实测 0.1 起单人画面里就没有误改）
SPK_MINOR = 60        # 全场说话不到这么多秒的「人」标「其他」（背景歌、外放视频等），不当嘉宾
SOLO_STEP = 20        # 找单人画面时每隔多少秒抽一帧


def sentence_embeddings(audio, spk_sents):
    """接口的每一句（≥0.5 秒，截前 10 秒）用 CAM++ 算一个单位长度的声纹；太短的是 None。"""
    model = AutoModel(model=SPK_MODEL, disable_update=True, log_level="ERROR")
    embs = []
    for _, _, a, b in spk_sents:
        x = audio[int(a * SR):int(min(b, a + 10) * SR)]
        if len(x) >= 0.5 * SR:
            v = model.generate(input=x, fs=SR, disable_pbar=True)[0]["spk_embedding"][0].cpu().numpy()
            embs.append(v / np.linalg.norm(v))
        else:
            embs.append(None)
    return embs


def refine_speakers(embs, lab, n, flip=SPK_FLIP):
    """逐句校正：每个人的中心 = 归给他的所有句子声纹的平均；某句和另一个人的中心比和自己的高出 flip 以上就改过去，
    没归属的句子归到最像的人。返回 (新编号列表, 每份改了几句)。"""
    from collections import defaultdict
    C = []
    for g in range(n):
        vs = [e for e, l in zip(embs, lab) if l == g and e is not None]
        c = np.mean(vs, 0) if vs else np.zeros(192)
        C.append(c / (np.linalg.norm(c) or 1))
    C = np.array(C)
    out, flips = list(lab), defaultdict(int)
    for k, e in enumerate(embs):
        if e is None:
            continue
        sc = C @ e
        g = int(sc.argmax())
        if lab[k] is None or (g != lab[k] and sc[g] - sc[lab[k]] > flip):
            out[k] = g
            if lab[k] is not None:
                flips[k] += 1
    return out, C, flips


def whole_speakers(spk_sents):
    """整条一个任务时接口的说话人编号全场一致，直接用，只按说话总时长从多到少重新编号。"""
    from collections import Counter
    secs = Counter()
    for _, sid, a, b in spk_sents:
        if sid is not None:
            secs[sid] += b - a
    rank = {sid: g for g, (sid, _) in enumerate(secs.most_common())}
    return [rank.get(sid) for _, sid, _, _ in spk_sents], {"n": len(rank), "api_id": {g: sid for sid, g in rank.items()}}


def match_speakers(audio, spk_sents, sim=SPK_SIM, flip=SPK_FLIP, per_group=8, min_group=8):
    """分成几份识别时（直连切份、或退回 bl 每 5 分钟一段），接口的说话人编号只在一份内部一致（这份的 0 号可能是下份的 1 号），
    这里用 CAM++ 声纹把各份的人对上，再逐句校正：
    1. 接口的每一句各算一个声纹（sentence_embeddings）；
    2. 每个 (份号, 份内编号) 取最长的 per_group 句（≥1 秒）的声纹平均，做平均连接的层次聚类：
       相似度最高的两组先合并，直到最高的也低于 sim。声音不到 min_group 秒的组不参与（太少不准，第 3 步逐句归）；
    3. refine_speakers 逐句校正（bl 分段时 10/11 第 3 段 595–885 秒接口把嘉宾的话几乎全归给了主播）。
    返回 (每句的全场编号，没法判断的是 None；说明)。全场编号按聚类时的说话总时长从多到少排。"""
    from collections import defaultdict
    dur = [b - a for _, _, a, b in spk_sents]
    embs = sentence_embeddings(audio, spk_sents)
    groups = defaultdict(list)
    for k, (ci, sid, _, _) in enumerate(spk_sents):
        if sid is not None:
            groups[(ci, sid)].append(k)
    secs = {key: sum(dur[k] for k in ks) for key, ks in groups.items()}
    keys, E = [], []
    for key, ks in sorted(groups.items()):
        use = sorted((k for k in ks if embs[k] is not None and dur[k] >= 1.0), key=lambda k: -dur[k])[:per_group]
        if sum(min(dur[k], 10) for k in use) < min_group:
            continue
        m = np.mean([embs[k] for k in use], 0)
        keys.append(key)
        E.append(m / np.linalg.norm(m))
    E = np.array(E)
    S = E @ E.T
    clusters = [[i] for i in range(len(keys))]
    while len(clusters) > 1:
        best, pair = -2, None
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                v = S[np.ix_(clusters[x], clusters[y])].mean()
                if v > best:
                    best, pair = v, (x, y)
        if best < sim:
            break
        x, y = pair
        clusters[x] += clusters.pop(y)
    clusters.sort(key=lambda c: -sum(secs[keys[i]] for i in c))
    mapping = {keys[i]: g for g, c in enumerate(clusters) for i in c}
    lab = [mapping.get((ci, sid)) for ci, sid, _, _ in spk_sents]
    out, C, fl = refine_speakers(embs, lab, len(clusters), flip)
    flips = defaultdict(int)
    for k in fl:
        flips[spk_sents[k][0]] += 1
    fit = {k: round(float(E[i] @ C[mapping[k]]), 2) for i, k in enumerate(keys)}
    pair = {f"{x}-{y}": round(float(C[x] @ C[y]), 2) for x in range(len(C)) for y in range(x + 1, len(C))}
    return out, {"n": len(clusters), "mapping": mapping, "secs": secs, "fit": fit, "flips": dict(flips),
                 "centroid_sim": pair}


def find_host(media, spk_sents, total, step=SOLO_STEP):
    """主播：单人满屏（不是连麦分屏）的时候只有主播在说。每隔 step 秒抽一帧，用 liveclip.layout.split_band 判断单人/分屏，
    连续 2 帧以上单人算一段单人画面，这些时段里说话最多的全场说话人就是主播；再和总说话时长最多的人核对。
    没有画面或没有单人段时退回「说得最多的是主播」，标低可信度。spk_sents 是 [(全场编号, 开始秒, 结束秒)]。"""
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    talk = Counter()
    for g, a, b in spk_sents:
        talk[g] += b - a
    longest = talk.most_common(1)[0][0]
    solo_ivs, size = [], None
    try:
        from .layout import split_band
        from .cut import probe_size
        size = probe_size(media)
    except Exception:
        pass
    if size:
        ts = list(np.arange(step / 2, total, step))
        with ThreadPoolExecutor(max_workers=8) as ex:
            bands = list(ex.map(lambda t: split_band(media, t, size), ts))
        solo = [b is None for b in bands]
        i = 0
        while i < len(ts):
            j = i
            while j < len(ts) and solo[j]:
                j += 1
            if j - i >= 2:
                solo_ivs.append((ts[i] - step / 2, ts[j - 1] + step / 2))
            i = max(j, i + 1)
        print(f"抽 {len(ts)} 帧：单人 {sum(solo)}、分屏 {sum(b not in (None, False) for b in bands)}、"
              f"抽帧失败 {sum(b is False for b in bands)}；单人段 {len(solo_ivs)} 个")
    solo_talk = Counter()
    for g, a, b in spk_sents:
        for x, y in solo_ivs:
            solo_talk[g] += max(0, min(b, y) - max(a, x))
    solo_sum = sum(solo_talk.values())
    info = {"talk_sec": {g: round(v) for g, v in talk.items()}, "solo_sec": {g: round(v) for g, v in solo_talk.items()},
            "solo_spans": [[round(x), round(y)] for x, y in solo_ivs]}
    if solo_sum >= 30:
        host, n = solo_talk.most_common(1)[0]
        share = n / solo_sum
        conf = "高" if share >= 0.8 and host == longest else "中" if share >= 0.6 else "低"
        how = (f"单人画面 {sum(y - x for x, y in solo_ivs) / 60:.0f} 分钟里 {share:.0%} 的说话是他"
               f"{'，也是全场说得最多的' if host == longest else '（但全场说得最多的是另一人，要人工核对）'}")
    else:
        host, conf = longest, "低"
        how = "没找到单人画面（或没有视频），按全场说得最多的人算"
    return host, conf, how, info


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveclip asr", description="speech-to-text → subtitle JSON")
    ap.add_argument("media")
    ap.add_argument("--out")
    ap.add_argument("--device", default=C.get("asr.device", "auto"), help="SenseVoice device: auto|cpu|cuda|mps")
    ap.add_argument("--engine", choices=["sensevoice", "bailian"], default=C.get("asr.engine", "bailian"))
    ap.add_argument("--diarize", type=int, nargs="?", const=2, default=0, metavar="N",
                    help="（仅 bailian）分说话人，N 是预计人数，默认 2；每句加 spk（主播/嘉宾…），顶层加 speakers 说明")
    ap.add_argument("--spk-refine", action="store_true",
                    help="整条一个任务时也做 CAM++ 逐句声纹校正（默认不做：10/11 实测只多对 1/56 句，多花约 1 分钟）；分份识别时总会做")
    ap.add_argument("--spk-sim", type=float, default=SPK_SIM, help="分份识别时跨份对声纹的相似度阈值")
    ap.add_argument("--via-bl", action="store_true", help="不直连，直接用旧的 bl 每 5 分钟一段上传（调试/对照用）")
    args = ap.parse_args(argv)
    if args.diarize and args.engine != "bailian":
        ap.error("--diarize 只支持 --engine bailian")

    media = Path(args.media)
    out = Path(args.out) if args.out else C.ASR / f"{media.stem}{'_bailian' if args.engine == 'bailian' else ''}.json"
    fx = os.environ.get("LIVECLIP_ASR_FIXTURES")
    if fx:   # tests / dry runs: <dir>/<media stem>.json is the transcript; no audio is uploaded, nothing is billed
        f = Path(fx) / f"{media.stem}.json"
        if not f.exists():
            raise SystemExit(f"LIVECLIP_ASR_FIXTURES is set but {f} does not exist")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"写入 {out}（fixture）")
        return
    t0 = time.time()
    audio = load_audio(media)
    print(f"音频 {len(audio) / SR / 60:.1f} 分钟")
    segs = None

    def get_segs():
        nonlocal segs
        if segs is None:
            vad = AutoModel(model="fsmn-vad", max_single_segment_time=15000, disable_update=True, log_level="ERROR")
            segs = vad.generate(input=audio, fs=SR, disable_pbar=True)[0]["value"]
            print(f"VAD 切出 {len(segs)} 段，用时 {time.time() - t0:.0f}s")
        return segs

    body, speakers, meta = [], None, {}
    if args.engine == "bailian":
        raw = out.with_name(out.stem + "_raw")
        route = None
        if not args.via_bl:
            try:
                parts, route = run_dashscope(audio, BAILIAN_VOCAB, args.diarize, raw, get_segs)
            except RuntimeError as e:
                print(f"百炼直连全部失败（{e}），退回 bl 分段上传")
        if route is None:
            import shutil
            if not shutil.which("bl"):
                raise SystemExit("百炼直连失败，也没有装 bl 命令行可退回；检查 DASHSCOPE_API_KEY 和网络")
            parts = run_bailian(audio, get_segs(), BAILIAN_VOCAB, args.diarize, raw)
            route = "bl 命令行每 5 分钟一段" + ("（直连失败后的退路）" if not args.via_bl else "")
        meta = {"route": route, "parts": len(parts)}
        print(f"识别完成 {time.time() - t0:.0f}s：{route}")
        words, stamps, spks, spk_sents = tokens_from_parts(parts)
        if not args.diarize:
            body = [{"from": round(a, 2), "to": round(b, 2), "content": t} for a, b, t in sentences_from_tokens(0, words, stamps)]
        else:
            chunk_map, cl = {}, {}
            if len(parts) == 1:
                glab, cl = whole_speakers(spk_sents)
                how_spk = f"百炼整条分说话人（diarization，speaker_count {args.diarize}），编号全场一致"
                if args.spk_refine:
                    glab, _, fl = refine_speakers(sentence_embeddings(audio, spk_sents), glab, cl["n"])
                    how_spk += f" + CAM++ 逐句校正（差 {SPK_FLIP} 以上改判，改了 {len(fl)} 句）"
            else:
                print("分份识别，各份说话人编号不通，用声纹对上…")
                glab, cl = match_speakers(audio, spk_sents, args.spk_sim)
                how_spk = (f"百炼每份内分说话人 + CAM++ 声纹跨份聚类（相似度阈值 {args.spk_sim}）"
                           f"+ 逐句声纹校正（差 {SPK_FLIP} 以上改判）")
                for (c, sid), sec in sorted(cl["secs"].items()):
                    g = cl["mapping"].get((c, sid))
                    chunk_map.setdefault(c, {})[str(sid)] = {"全场": g, "说话秒": round(sec), "声纹贴合": cl["fit"].get((c, sid))}
                for c, n in cl["flips"].items():
                    chunk_map.setdefault(c, {})["逐句改判"] = n
            g_sents = [(g, a, b) for g, (_, _, a, b) in zip(glab, spk_sents) if g is not None]
            host, conf, how, info = find_host(media, g_sents, len(audio) / SR)
            others = sorted((g for g in range(cl["n"]) if g != host), key=lambda g: -info["talk_sec"].get(g, 0))
            names, k = {host: C.host_label}, 0
            for g in others:
                if info["talk_sec"].get(g, 0) < SPK_MINOR:
                    names[g] = "其他"
                else:
                    names[g], k = C.guest_label if k == 0 else f"{C.guest_label}{k + 1}", k + 1
            sents = sentences_from_tokens(0, words, stamps, [glab[x] for x in spks])
            body = [{"from": round(x[0], 2), "to": round(x[1], 2), "content": x[2],
                     "spk": names.get(x[3], "未知") if len(x) > 3 else "未知"} for x in sents]
            speakers = {
                "method": how_spk + " + 单人画面定主播",
                "host": host, "host_by": how, "confidence": conf,
                "list": [{"id": g, "label": names[g], "talk_sec": info["talk_sec"].get(g, 0),
                          "solo_sec": info["solo_sec"].get(g, 0),
                          **({"api_id": cl["api_id"][g]} if "api_id" in cl else {})} for g in range(cl["n"])],
                "solo_spans": info["solo_spans"],
                **({"centroid_sim": cl["centroid_sim"], "chunks": chunk_map} if chunk_map else {}),
            }
            for g in speakers["list"]:
                print(f"  全场 {g['id']} → {g['label']}：说话 {g['talk_sec'] / 60:.1f} 分钟，单人画面里 {g['solo_sec'] / 60:.1f} 分钟")
            print(f"{C.host_label} = 全场 {host}（可信度{conf}）：{how}")
    else:
        segs = get_segs()
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        asr = AutoModel(model="iic/SenseVoiceSmall", device=pick_device(args.device), disable_update=True, log_level="ERROR")
        for i, (b, e) in enumerate(segs):
            chunk = audio[int(b * SR / 1000):int(e * SR / 1000)]
            if len(chunk) < SR * 0.3:
                continue
            r = asr.generate(input=chunk, fs=SR, language="auto", use_itn=True, disable_pbar=True,
                             output_timestamp=True)[0]
            events = re.findall(r"<\|(Laughter|Applause|Cry|Cough|Sneeze|BGM)\|>", r["text"])
            if not rich_transcription_postprocess(r["text"]).strip():
                continue
            for s, en, t in sentences_from_tokens(b / 1000, r.get("words", []), r.get("timestamp", [])):
                body.append({"from": round(s, 2), "to": round(en, 2), "content": t,
                             **({"event": sorted(set(events))} if events else {})})
            if i % 100 == 0:
                print(f"  {i}/{len(segs)}")

    out.parent.mkdir(parents=True, exist_ok=True)
    source = BAILIAN_MODEL if args.engine == "bailian" else "SenseVoiceSmall"
    meta["sec"] = round(time.time() - t0)
    out.write_text(json.dumps({"source": source, **({"asr": meta} if args.engine == "bailian" else {}), "body": body,
                               **({"speakers": speakers} if speakers else {})}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {out}：{len(body)} 句，总用时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
