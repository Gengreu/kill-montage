# -*- coding: utf-8 -*-
"""KillMontage エンジン: キル検出 / 音はめ / BGM加工 / レンダリング"""
import glob
import math
import os
import random
import shutil
import subprocess
import tempfile

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

NOWIN = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "profiles")

VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv")
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus")

# ---------------------------------------------------------------- ゲーム別プリセット
GAMES = {
    "valorant": {
        "name": "VALORANT", "color": "#FF4655", "card": "#0F1923",
        "desc": "右上のキルフィードで自分のキルを検出\nマルチキル・ACE を優先してピックアップ\nキルの間の待ち時間は自動でジャンプカット",
        # 検出: キル音は音程のはっきりした高音 → 音程感(tonal)を重視
        "band": (1500, 7000), "tonal_w": 1.0, "onset_w": 0.3, "sustain": 6, "gap": 0.6,
        "defaults": dict(pre=1.2, lead=0.9, killpost=0.35, post=0.8, chain=25.0, maxper=1, sens=6, slowlen=0.5, slowspeed=0.5,
                         transition="白フラッシュ", grade="鮮やか", tdur=0.25),
    },
    "apex": {
        "name": "APEX LEGENDS", "color": "#DA292A", "card": "#1B1B1F",
        "desc": "右上のキルフィードで自分のダウン/キルを検出\n部隊壊滅（連続キル）を優先してピックアップ\n撃ち合いは残しつつ移動時間をジャンプカット",
        # 検出: 撃ち合いの中の鋭いアタックも拾う → 立ち上がり(onset)も重視
        "band": (1000, 6000), "tonal_w": 0.6, "onset_w": 0.6, "sustain": 4, "gap": 0.8,
        "defaults": dict(pre=1.8, lead=1.2, killpost=0.45, post=1.0, chain=12.0, maxper=1, sens=6, slowlen=0.6, slowspeed=0.5,
                         transition="スライド", grade="シネマ", tdur=0.3),
    },
    "other": {
        "name": "その他のゲーム", "color": "#7C8CF8", "card": "#1A1C24",
        "desc": "汎用設定\nキル音を登録すればどのゲームでも使えます",
        "band": (1500, 7500), "tonal_w": 0.5, "onset_w": 0.5, "sustain": 4, "gap": 0.7,
        "defaults": dict(pre=1.5, lead=1.0, killpost=0.4, post=0.9, chain=10.0, maxper=1, sens=6, slowlen=0.6, slowspeed=0.5,
                         transition="ランダム", grade="なし", tdur=0.3),
    },
}

TRANSITIONS = {
    "白フラッシュ": ["fadewhite"],
    "フェード": ["fade"],
    "スライド": ["slideleft", "slideright", "slideup", "slidedown"],
    "ズーム": ["zoomin"],
    "ワイプ": ["wipeleft", "wiperight", "circleopen", "radial"],
    "ランダム": ["fadewhite", "slideleft", "slideright", "zoomin", "circleopen",
               "wipeleft", "radial", "smoothleft", "pixelize", "dissolve"],
    "なし(カット)": [],
}
GRADES = {
    "なし": "",
    "鮮やか": "eq=saturation=1.28:contrast=1.07",
    "シネマ": "eq=saturation=1.12:contrast=1.1:gamma=0.96,colorbalance=rs=0.04:bs=-0.05:rh=0.03:bh=-0.03",
    "クール": "eq=saturation=1.15:contrast=1.08,colorbalance=rs=-0.04:bs=0.06:rm=-0.02:bm=0.03",
}
BGM_PRESETS = {  # (ピッチ半音, 速度%, 低音dB, リバーブ)
    "ノーマル": (0, 100, 0, False),
    "Nightcore（速く・高く）": (3, 125, 0, False),
    "Slowed（遅く・低く）": (-2, 85, 2, False),
    "Slowed + Reverb": (-2, 85, 2, True),
    "低音ブースト": (0, 100, 8, False),
}
FONTS = [r"C:\Windows\Fonts\meiryob.ttc", r"C:\Windows\Fonts\YuGothB.ttc",
         r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf"]


# ---------------------------------------------------------------- ffmpeg
def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=NOWIN)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg エラー:\n" + p.stderr.decode("utf-8", "replace")[-800:])
    return p.stdout


def run_progress(cmd, total, cb, cwd=None):
    errf = tempfile.TemporaryFile()
    p = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"], cwd=cwd,
                         stdout=subprocess.PIPE, stderr=errf, creationflags=NOWIN)
    for line in p.stdout:
        line = line.decode("ascii", "ignore").strip()
        if line.startswith("out_time_us=") and total > 0:
            try:
                cb(min(1.0, int(line.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    p.wait()
    if p.returncode != 0:
        errf.seek(0)
        raise RuntimeError("ffmpeg エラー:\n" + errf.read().decode("utf-8", "replace")[-800:])


def probe(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
               "-of", "default=nw=1", path]).decode("utf-8", "replace")
    dur, has_audio = 0.0, False
    for line in out.splitlines():
        if line.startswith("duration="):
            try:
                dur = float(line.split("=")[1])
            except ValueError:
                pass
        elif line == "codec_type=audio":
            has_audio = True
    return dur, has_audio


def measure_lufs(path):
    """クリップのゲーム音の大きさ(LUFS)を測る。測れなければ None"""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "ebur128",
                        "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=NOWIN)
    for line in reversed(p.stderr.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("I:") and "LUFS" in line:
            try:
                v = float(line.split()[1])
                return v if v > -70 else None
            except (ValueError, IndexError):
                return None
    return None


_filters = None


def has_filter(name):
    global _filters
    if _filters is None:
        try:
            _filters = run(["ffmpeg", "-hide_banner", "-filters"]).decode("utf-8", "replace")
        except Exception:
            _filters = ""
    return f" {name} " in _filters


def load_audio(path, sr, start=None, dur=None):
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if dur:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    return np.frombuffer(run(cmd), dtype=np.float32)


def save_snippet(path, start, dur, out_wav):
    """試聴用のwavを書き出す"""
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, start):.3f}", "-t", f"{dur:.3f}", "-i", path,
         "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", out_wav])


def movavg(a, w):
    """時間方向(axis 0)の中心移動平均"""
    w = max(1, int(w))
    if w == 1:
        return a.astype(np.float64)
    padw = [(w // 2, w - w // 2 - 1)] + [(0, 0)] * (a.ndim - 1)
    p = np.pad(a.astype(np.float64), padw, mode="edge")
    c = np.cumsum(p, axis=0)
    c = np.concatenate([np.zeros((1,) + c.shape[1:]), c], axis=0)
    return (c[w:] - c[:-w]) / w


# ---------------------------------------------------------------- 音声解析
SR, HOP, NFFT, NB = 16000, 320, 1024, 48
FPS_A = SR / HOP  # 50 フレーム/秒
_freqs = np.fft.rfftfreq(NFFT, 1 / SR)
_edges = np.geomspace(250, 7600, NB + 1)
FB = np.zeros((len(_freqs), NB), np.float32)
for _b in range(NB):
    _m = (_freqs >= _edges[_b]) & (_freqs < _edges[_b + 1])
    if not _m.any():
        _m[np.argmin(abs(_freqs - (_edges[_b] + _edges[_b + 1]) / 2))] = True
    FB[_m, _b] = 1
TPL_LEN = 20          # テンプレート長 0.4秒
SUB = (4, 44)         # 比較に使う帯域
SHIFTS = range(-4, 5)  # 音程ずれの許容（Valorantは連続キルでキル音が上がる）


def analyze(path, band):
    x = load_audio(path, SR)
    if len(x) < NFFT * 4:
        return None
    win = np.hanning(NFFT).astype(np.float32)
    frames = swv(x, NFFT)[::HOP]
    n = len(frames)
    bins = (_freqs >= band[0]) & (_freqs < band[1])
    bandpow = np.empty((n, NB), np.float32)
    epow = np.empty(n, np.float64)
    flat = np.empty(n, np.float64)
    for s in range(0, n, 4096):
        p = np.abs(np.fft.rfft(frames[s:s + 4096] * win, axis=1)) ** 2
        bandpow[s:s + len(p)] = p @ FB
        pb = p[:, bins] + 1e-12
        epow[s:s + len(p)] = pb.sum(1)
        flat[s:s + len(p)] = np.exp(np.log(pb).mean(1)) / pb.mean(1)
    e = 10 * np.log10(epow + 1e-10)
    return {"bandpow": bandpow, "e": e, "flat": flat, "n": n}


def whiten(bandpow):
    """1.5秒の背景を引いて、背景より盛り上がった部分だけを残す特徴量"""
    f = np.log10(bandpow.astype(np.float64) + 1e-10)
    f = movavg(f - movavg(f, 75), 3)
    return np.maximum(f, 0).astype(np.float32)


def onset_of(A):
    e = A["e"]
    return movavg(np.maximum(0, np.diff(e, prepend=e[0])), 3) * 3


def _pick(score, ok, gap_frames):
    s = score
    idx = np.where(ok[1:-1] & (s[1:-1] >= s[:-2]) & (s[1:-1] >= s[2:]))[0] + 1
    idx = sorted(idx, key=lambda i: -s[i])
    picked = []
    for i in idx:
        if all(abs(i - j) > gap_frames for j in picked):
            picked.append(i)
    return picked


def heuristic_peaks(A, game, sens):
    """プリセットの特徴（帯域・音程感・立ち上がり）でキル音を推定"""
    g = GAMES[game]
    e = A["e"]
    rise = movavg(e - movavg(e, 150), g["sustain"])
    onset = onset_of(A)
    tonal = movavg(1 - A["flat"], g["sustain"])
    tonal = tonal - np.median(tonal)
    score = rise + g["onset_w"] * onset + g["tonal_w"] * 25 * tonal
    ok = (score > 16 - sens) & (e > np.percentile(e, 60))
    out = []
    for i in _pick(score, ok, int(g["gap"] * FPS_A)):
        lo = max(0, i - g["sustain"] - 2)
        j = lo + int(np.argmax(onset[lo:i + 1]))  # 音の出だしに合わせる
        out.append((j / FPS_A + 0.03, float(score[i])))
    return sorted(out)


def template_peaks(A, templates, sens, gap):
    """登録したキル音と似ている場所を探す（音程ずれも許容）"""
    F = whiten(A["bandpow"])
    n = len(F)
    if n <= TPL_LEN + 2:
        return []
    a, b = SUB
    W = swv(F[:, a:b], (TPL_LEN, b - a))[:, 0]
    m = len(W)
    tpls = []
    for t in templates:
        for sh in SHIFTS:
            v = t[:, a - sh:b - sh].ravel().astype(np.float32)
            v = v - v.mean()
            tpls.append(v / (np.linalg.norm(v) + 1e-6))
    T = np.stack(tpls, 1)
    best = np.empty(m, np.float32)
    for s in range(0, m, 3000):
        X = W[s:s + 3000].reshape(-1, TPL_LEN * (b - a))
        X = X - X.mean(1, keepdims=True)
        X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-6
        best[s:s + len(X)] = (X @ T).max(1)
    e = A["e"]
    rise = e - movavg(e, 150)
    rmax = np.array([rise[i:i + 6].max() for i in range(m)])
    ok = (best > 0.86 - 0.04 * sens) & (rmax > 3)
    return sorted(((i + 1) / FPS_A, float(best[i]) * 20) for i in _pick(best, ok, int(gap * FPS_A)))


# ---------------------------------------------------------------- キル音プロファイル
def profile_dir(game):
    d = os.path.join(PROFILE_DIR, game)
    os.makedirs(d, exist_ok=True)
    return d


def list_templates(game):
    return sorted(glob.glob(os.path.join(profile_dir(game), "kill_*.npy")))


def load_templates(game):
    return [np.load(f) for f in list_templates(game)]


def register_template(game, clip, t):
    """clip の t 秒付近のキル音を登録する"""
    A = analyze(clip, GAMES[game]["band"])
    if A is None:
        raise RuntimeError("音声が読めませんでした")
    F = whiten(A["bandpow"])
    on = onset_of(A)
    i0 = int(round(t * FPS_A))
    lo, hi = max(0, i0 - 15), min(len(on), i0 + 15)
    start = max(0, lo + int(np.argmax(on[lo:hi])) - 1)
    start = min(start, len(F) - TPL_LEN)
    if start < 0:
        raise RuntimeError("クリップが短すぎます")
    tpl = F[start:start + TPL_LEN]
    k = 1
    while os.path.exists(os.path.join(profile_dir(game), f"kill_{k:02d}.npy")):
        k += 1
    base = os.path.join(profile_dir(game), f"kill_{k:02d}")
    np.save(base + ".npy", tpl)
    save_snippet(clip, start / FPS_A - 0.1, 1.0, base + ".wav")
    return base + ".npy"


def delete_template(path):
    for ext in (".npy", ".wav"):
        p = os.path.splitext(path)[0] + ext
        if os.path.exists(p):
            os.remove(p)


# ---------------------------------------------------------------- VALORANT キルフィード検出
# 右上のキルフィードで「自分」のキルの行だけ左端に黄色い縦バーが付く。
# そのバーが新しく一番下に増えた瞬間 = 自分のキル。
FEED_W, FEED_H, FEED_FPS = 435, 245, 10   # 720p 換算の右上領域
FEED_BAR = 13                             # 縦バーの最低の長さ(px)


def _feed_bars(fr):
    R, G, B = (fr[..., i].astype(np.int16) for i in range(3))
    m = (R > 185) & (G > 185) & (B < 160) & (np.minimum(R, G) - B > 60)
    cs = np.cumsum(np.pad(m, ((1, 0), (0, 0))).astype(np.int32), axis=0)
    er = (cs[FEED_BAR:] - cs[:-FEED_BAR]) == FEED_BAR
    ys = np.where(er.any(1))[0]
    if len(ys) == 0:
        return []
    out = []
    for g in np.split(ys, np.where(np.diff(ys) > 3)[0] + 1):
        cols = np.where(er[g].any(0))[0]
        if not (2 <= cols.max() - cols.min() + 1 <= 16):  # 細い縦バーだけ（爆発などの大きい黄色は除外）
            continue
        if g[-1] - g[0] + FEED_BAR > 32:   # 行1つ分より長い = ケーブルや柱
            continue
        # 自分のキルの行は「黄色バー → 自分の顔 → 青緑(味方色)のプレート」と並ぶ。
        # 自分がやられた行はバーが右端にあり右に何も無い / 黄色い壁や箱ならプレートが無い
        x0 = cols.max() + 2
        x1 = min(FEED_W, x0 + 90)
        if x1 - x0 < 40:
            continue
        band = fr[g[0]:g[-1] + FEED_BAR, x0:x1].astype(np.int16)
        teal = ((band[..., 1] > band[..., 0] + 25) & (band[..., 1] > 100) & (band[..., 2] > 70)).mean()
        white = ((band[..., 0] > 200) & (band[..., 1] > 200) & (band[..., 2] > 200)).mean()  # 「自分」の文字
        if teal >= 0.12 and white >= 0.01:
            out.append(float(g.mean() + FEED_BAR / 2))
    return out


# ---------------------------------------------------------------- VALORANT キルバナー検出
# 自分がキルすると、照準の下（画面の横50%・縦79.2%）にキルバナーが出る。
# 中のエンブレムやリングの形はスキンごとに違うが「中心のまわりに白い円がある」のは共通。
# いくつかの半径で円周上の白さを測り、「白い円がある半径」と「何も無い半径」の差で判定する
# （雪のように全体が白いときは差が出ない）。
BAN_S = 240
_BAN_PTS = []
for _k in (0.9, 1.0, 1.15, 1.3, 1.45, 1.6, 1.8):
    _r = BAN_S * (0.048 / 0.222) * _k
    _a = np.linspace(0, 2 * np.pi, 180, endpoint=False)[:, None]
    _d = np.arange(-2, 3)[None, :]
    pts = []
    for off in (0, -4, 4):   # 線の上 / すぐ内側 / すぐ外側
        _x = np.clip(np.round(BAN_S / 2 + (_r + _d + off) * np.cos(_a)).astype(int), 0, BAN_S - 1)
        _y = np.clip(np.round(BAN_S / 2 + (_r + _d + off) * np.sin(_a)).astype(int), 0, BAN_S - 1)
        pts.append((_y, _x))
    _BAN_PTS.append(pts)
BANNER_ON = 0.18      # バナーだけで探すとき
BANNER_CONFIRM = 0.12  # キルフィードで見つけたキルの確認


def banner_score(fr):
    fr = fr.astype(np.int16)
    L = (fr[..., 0] * 3 + fr[..., 1] * 6 + fr[..., 2]) // 10   # 明るさ（リングの色は白・水色などスキンで違う）
    cov = []
    for (yc, xc), (yi, xi), (yo, xo) in _BAN_PTS:
        c, ii, oo = L[yc, xc], L[yi, xi], L[yo, xo]
        ridge = (c > 150) & (c > ii + 30) & (c > oo + 30)   # 細い線として明るい
        cov.append(ridge.any(1).mean())
    return float(max(cov) - min(cov))

# ---------------------------------------------------------------- キルバナーの学習
# 使ったクリップの「確認できたキル」のバナーを覚えておき、見比べて判定する（スキンごとの見た目）
BT_N = 64
_yy, _xx = np.mgrid[0:BT_N, 0:BT_N]
BT_MASK = ((_yy - BT_N / 2) ** 2 + (_xx - BT_N / 2) ** 2) <= (BT_N * 0.47) ** 2
BT_CONFIRM, BT_ON = 0.30, 0.40
_bt = None


def bt_feat(fr):
    """形だけを取り出した特徴（背景の明るさの違いは消す）"""
    L = (fr[..., 0].astype(np.float32) * 3 + fr[..., 1] * 6 + fr[..., 2]) / 10
    k = 7
    p = np.pad(L, k // 2, mode="edge")
    c = np.pad(np.cumsum(np.cumsum(p, 0), 1), ((1, 0), (1, 0)))
    blur = (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)
    h = (L - blur)[BT_MASK]
    h = h - h.mean()
    return h / (np.linalg.norm(h) + 1e-6)


def _bt_path(game):
    return os.path.join(profile_dir(game), "banners.npz")


def _bt_own(game="valorant"):
    """自分の学習分だけ"""
    try:
        z = np.load(_bt_path(game))
        return [v for v in z["sums"]], list(z["counts"])
    except (OSError, KeyError, ValueError):
        return [], []


def _shared_dir(game):
    d = os.path.join(profile_dir(game), "shared")
    os.makedirs(d, exist_ok=True)
    return d


def my_uid():
    """このアプリのID（学習データを共有するときに、誰のデータか見分ける）"""
    import uuid
    p = os.path.join(PROFILE_DIR, "uid.txt")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        u = uuid.uuid4().hex[:12]
        with open(p, "w", encoding="utf-8") as f:
            f.write(u)
        return u


def bt_load(game="valorant", force=False):
    """覚えたバナー（自分の分 + 友達から読み込んだ分を合わせたもの）: (合計ベクトル, 回数)"""
    global _bt
    if _bt is None or force:
        sums, counts = _bt_own(game)
        for fn in sorted(glob.glob(os.path.join(_shared_dir(game), "*.npz"))):
            try:
                z = np.load(fn)
                for vec, cnt in zip(z["sums"], z["counts"]):
                    _bt_merge(sums, counts, vec.astype(np.float32), int(cnt))
            except (OSError, KeyError, ValueError):
                pass
        dim = int(BT_MASK.sum())
        _bt = (np.stack(sums).astype(np.float32) if sums else np.zeros((0, dim), np.float32),
               np.array(counts, np.int32))
    return _bt


def friends(game="valorant"):
    """読み込んだ友達の学習データ一覧"""
    import json
    out = []
    for fn in sorted(glob.glob(os.path.join(_shared_dir(game), "*.json"))):
        try:
            with open(fn, encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
    return out

def bt_templates(game="valorant"):
    sums, counts = bt_load(game)
    keep = counts >= 2
    if not keep.any():
        return np.zeros((0, sums.shape[1]), np.float32)
    t = sums[keep]
    return (t / np.linalg.norm(t, axis=1, keepdims=True)).astype(np.float32)


def bt_info(game="valorant"):
    sums, counts = bt_load(game)
    return int((counts >= 2).sum()), int(counts.sum())


def bt_reset(game="valorant"):
    global _bt
    for p in [_bt_path(game), os.path.join(profile_dir(game), "banners_seen.json")] + \
            glob.glob(os.path.join(_shared_dir(game), "*")):
        try:
            os.remove(p)
        except OSError:
            pass
    _bt = None


def _bt_crops(path, t, dur=0.6, fps=5):
    vf = f"fps={fps},crop=ih*0.222:ih*0.222:iw*0.5-ih*0.111:ih*0.792-ih*0.111,scale={BT_N}:{BT_N}"
    raw = run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", path, "-t", f"{dur:.2f}", "-vf", vf,
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    n = len(raw) // (BT_N * BT_N * 3)
    return [np.frombuffer(raw[i * BT_N * BT_N * 3:(i + 1) * BT_N * BT_N * 3], np.uint8).reshape(BT_N, BT_N, 3)
            for i in range(n)]


def learn_banners(clips, cfg, log=lambda s: None):
    """クリップの確認済みキルからバナーの見た目を覚える（前回までの学習に追加していく）"""
    global _bt
    import json
    sums, counts = _bt_own(cfg["game"])   # 自分の学習分だけを更新する
    seen_path = os.path.join(profile_dir(cfg["game"]), "banners_seen.json")
    try:
        with open(seen_path, encoding="utf-8") as f:
            seen = set(json.load(f))
    except (OSError, ValueError):
        seen = set()
    added = 0
    for c in clips:
        try:
            ks = [t for t, _ in detect_kills(c, cfg)[0]]
        except Exception:
            continue
        for t in ks[:4]:
            sid = f"{_cache_key(c, cfg) or os.path.abspath(c)}|{t:.1f}"
            if sid in seen:      # 一度覚えたキルは覚え直さない
                continue
            seen.add(sid)
            fs = _bt_crops(c, t + 0.6)
            if not fs:
                continue
            v = np.mean([bt_feat(f) for f in fs], 0)
            v = v / (np.linalg.norm(v) + 1e-6)
            _bt_merge(sums, counts, v.astype(np.float32), 1)   # 同じスキンならまとめる / 新しいスキンなら追加
            added += 1
    if added:
        np.savez(_bt_path(cfg["game"]), sums=np.stack(sums).astype(np.float32), counts=np.array(counts, np.int32))
        with open(seen_path, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
        _bt = None
    n, tot = bt_info(cfg["game"])
    log(f"キルバナーを学習しました: 新しく+{added}キル → 覚えたバナー {n}種類（合計{tot}キル分）")
    return added


def _bt_merge(sums, counts, vec, cnt):
    """覚えたバナーに1つ足す（似ていればまとめる / 新しいスキンなら追加）"""
    v = vec / (np.linalg.norm(vec) + 1e-6)
    best, bi = -1.0, -1
    for i, sv in enumerate(sums):
        sc = float((sv / np.linalg.norm(sv)) @ v)
        if sc > best:
            best, bi = sc, i
    if best >= 0.6:
        sums[bi] = sums[bi] + vec
        counts[bi] += cnt
    else:
        sums.append(vec.astype(np.float32))
        counts.append(cnt)


def export_learning(out_path, game="valorant", name=None):
    """自分の学習データ（覚えたバナー・参考動画のスタイル）を1つのファイルに書き出す（友達に渡す用）。
    友達から読み込んだ分は入れない（回し合って二重に数えないように）"""
    import json
    import zipfile
    sums, counts = _bt_own(game)
    name = name or os.environ.get("USERNAME", "friend")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("meta.json", json.dumps({"app": "KILL//MONTAGE", "game": game, "uid": my_uid(), "name": name,
                                            "banners": int((np.array(counts) >= 2).sum()) if counts else 0,
                                            "kills": int(sum(counts)), "ver": 2}, ensure_ascii=False))
        if sums:
            buf = __import__("io").BytesIO()
            np.savez(buf, sums=np.stack(sums).astype(np.float32), counts=np.array(counts, np.int32))
            z.writestr("banners.npz", buf.getvalue())
        st = os.path.join(profile_dir(game), "style.json")
        if os.path.exists(st):
            z.write(st, "style.json")
    return int(sum(counts))


def import_learning(path):
    """友達の学習データを読み込む。同じ友達のデータは置き換える（何度読み込んでも二重にならない）"""
    import json
    import zipfile
    global _bt
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("meta.json"))
        if meta.get("app") != "KILL//MONTAGE":
            raise RuntimeError("KILL//MONTAGE の学習データではありません")
        game, uid = meta.get("game", "valorant"), meta.get("uid", "")
        if uid == my_uid():
            raise RuntimeError("これは自分の学習データです")
        before = bt_info(game)
        d = _shared_dir(game)
        if "banners.npz" in z.namelist():
            with open(os.path.join(d, f"{uid}.npz"), "wb") as f:
                f.write(z.read("banners.npz"))
        with open(os.path.join(d, f"{uid}.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        if "style.json" in z.namelist() and not load_style(game):   # 自分のスタイルが無いときだけ使う
            save_style(game, json.loads(z.read("style.json")))
    _bt = None
    return before, bt_info(game), meta

SCAN_HOOK = None   # 解析中の細かい進み具合（0〜1）を知らせる先。build が設定する


def _scan_tick(i, total):
    if SCAN_HOOK and total > 0 and i % 25 == 0:
        SCAN_HOOK(min(1.0, i / total))


def valorant_scan(path):
    """1回の読み込みで、キルフィード（右上）とキルバナー（照準の下）を同時に見る
    戻り値: (キルフィードの行, 円の判定スコア, 覚えたバナーとの一致度)"""
    fh = FEED_H + BAN_S
    sz = FEED_W * fh * 3
    fc = (f"[0:v]fps={FEED_FPS},split=2[a][b];"
          f"[a]crop=iw*0.34:ih*0.34:iw*0.66:ih*0.03,scale={FEED_W}:{FEED_H},format=rgb24[f];"
          f"[b]crop=ih*0.222:ih*0.222:iw*0.5-ih*0.111:ih*0.792-ih*0.111,split=2[b1][b2];"
          f"[b1]scale={BAN_S}:{BAN_S},format=rgb24,pad={FEED_W}:{BAN_S}:0:0[k0];"
          f"[b2]scale={BT_N}:{BT_N},format=rgb24[s];"
          f"[k0][s]overlay={BAN_S + 10}:0:format=rgb[k];[f][k]vstack[o]")
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-filter_complex", fc, "-map", "[o]",
                          "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    T = bt_templates()
    bars, ban, tpl = [], [], []
    expect = probe(path)[0] * FEED_FPS
    while True:
        b = p.stdout.read(sz)
        if len(b) < sz:
            break
        _scan_tick(len(bars), expect)
        fr = np.frombuffer(b, np.uint8).reshape(fh, FEED_W, 3)
        bars.append(_feed_bars(fr[:FEED_H]))
        ban.append(banner_score(fr[FEED_H:, :BAN_S]))
        if len(T):
            tpl.append(float((T @ bt_feat(fr[FEED_H:FEED_H + BT_N, BAN_S + 10:BAN_S + 10 + BT_N])).max()))
        else:
            tpl.append(0.0)
    p.wait()
    if not bars and probe(path)[0] > 1:   # 読み込みに失敗したのに「キル無し」で進まないように止める
        raise RuntimeError(f"動画を読み込めませんでした: {os.path.basename(path)}")
    return bars, ban, tpl


def banner_onsets(ban, tpl=None):
    """キルフィードに何も無い動画用: 覚えたバナーが出た瞬間をキルとする"""
    if tpl is None or not any(tpl):
        return []
    kills, off = [], 0   # 最初の0.5秒は「出ていない」を確認してから（録画前のキルのバナーは数えない）
    for i, v in enumerate(tpl):
        if v >= BT_ON and off >= 5 and all(i + j < len(tpl) and tpl[i + j] >= BT_CONFIRM for j in (1, 2)):
            kills.append((max(0.0, i / FEED_FPS - 0.3), 30.0))   # バナーはキルの少し後に出る
        off = 0 if v >= BT_CONFIRM else off + 1
    return kills

def feed_kills(path, scan=None):
    """キルフィードから自分のキルの時刻を返す [(秒, スコア)]"""
    frames, ban, tpl = scan or valorant_scan(path)
    kills = _feed_events(frames)
    seen = max(ban, default=0) >= BANNER_ON or max(tpl, default=0) >= BT_CONFIRM
    if kills and seen:  # この動画でバナーが見えるときだけ確認する
        # キルバナーで確認: キルの直後1.2秒の間に「円」か「覚えたバナー」が出ていないものは外す
        def ok(t):
            a, b = int(t * FEED_FPS) + 1, int(t * FEED_FPS) + 13
            return max(ban[a:b], default=0) >= BANNER_CONFIRM or max(tpl[a:b], default=0) >= BT_CONFIRM
        kills = [(t, s) for t, s in kills if ok(t)]
    return kills


def _feed_events(frames):

    def near(ys, y, tol):
        return any(abs(v - y) <= tol for v in ys)

    kills = []
    for i, ys in enumerate(frames):
        if i < 3:   # 動画の最初から出ている行 = 録画前のキル
            continue
        recent = [v for f in frames[max(0, i - 5):i] for v in f]
        for y in ys:
            if near(frames[i - 1], y, 6) if i else False:
                continue  # 前のフレームからある行
            if recent and y < max(recent) + 10:
                continue  # 一番下に増えた行ではない（上に詰まっただけ）
            # 0.3秒以上残っているか（一瞬の黄色は無視）。直後に1段上へ詰まるのはOK
            if all(i + j < len(frames) and any(-34 <= v - y <= 6 for v in frames[i + j]) for j in (1, 2)):
                t = i / FEED_FPS
                if not kills or t - kills[-1][0] > 0.3:
                    kills.append((t, 30.0))
    return kills


# ---------------------------------------------------------------- APEX キルフィード検出
# 右上のキルフィードで、自分の名前だけ黄緑色（味方は青緑・他は白）。
# 「自分の名前 → 銃アイコン/[失血死] → 相手の名前」の形の行 = 自分のダウン/キル。
# APEX は新しい行が一番上に出て、古い行が下へ流れる。
AFEED_W, AFEED_H, AFEED_FPS = 518, 356, 5   # 1080p 換算の右上領域


def _apex_rows(fr):
    R, G, B = (fr[..., i].astype(np.int16) for i in range(3))
    green = (G > R + 8) & (G - B > 75) & (R > 95) & (G > 115)
    ys = np.where(green.sum(1) >= 1)[0]
    out = []
    if len(ys) == 0:
        return out
    W = AFEED_W
    for g in np.split(ys, np.where(np.diff(ys) > 3)[0] + 1):
        if not (5 <= g[-1] - g[0] + 1 <= 27):
            continue
        band = green[g[0]:g[-1] + 1]
        if band.sum() < 15:
            continue
        gx = np.where(band.any(0))[0]
        if gx.max() - gx.min() > 180 or gx.max() - gx.min() < 12:
            continue
        if gx.max() >= 0.82 * W:   # 右端 = 自分がやられた行
            continue
        bc = fr[g[0]:g[-1] + 1].astype(np.int16)
        white = ((bc[..., 0] > 170) & (bc[..., 1] > 170) & (bc[..., 2] > 150)).sum(0)
        right = np.where(white[gx.max() + 1:] >= 2)[0]
        if len(right) < 20 or right[0] < 5 or gx.max() + 1 + right[-1] < 0.85 * W:
            continue   # 右に相手の名前が無い（観戦カード等）/ 名前の直後に文字（「○○が…」）
        out.append((float(g.mean()), int(gx.min()), int(gx.max())))
    return out


def apex_feed_kills(path):
    sz = AFEED_W * AFEED_H * 3
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf",
                          f"fps={AFEED_FPS},crop=iw*0.27:ih*0.33:iw*0.68:ih*0.12,scale={AFEED_W}:{AFEED_H}",
                          "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    frames = []
    expect = probe(path)[0] * AFEED_FPS
    while True:
        b = p.stdout.read(sz)
        if len(b) < sz:
            break
        _scan_tick(len(frames), expect)
        frames.append(_apex_rows(np.frombuffer(b, np.uint8).reshape(AFEED_H, AFEED_W, 3)))
    p.wait()
    # 自分の名前の幅はいつも同じ → それより大きく広い行（「○○が3キルで…」のお知らせ）を外す
    widths = [x1 - x0 for f in frames for _, x0, x1 in f]
    if widths:
        wmed = float(np.median(widths))
        frames = [[r for r in f if r[2] - r[1] <= wmed * 1.5] for f in frames]

    def same(r, x0, x1):
        return abs(r[1] - x0) <= 6 and abs(r[2] - x1) <= 6

    kills = []
    for i, rows in enumerate(frames):
        if i < 2:   # 動画の最初から出ている行 = 録画前のキル
            continue
        for y, x0, x1 in rows:
            # 同じ形の行が「この行より上に」いくつあるか。直前1秒より増えていたら新しい行
            k_cur = sum(1 for r in rows if same(r, x0, x1) and r[0] <= y)
            k_prev = max((sum(1 for r in f if same(r, x0, x1) and r[0] <= y + 6)
                          for f in frames[max(0, i - 5):i]), default=0)
            if k_cur <= k_prev:
                continue
            seen = sum(any(same(r, x0, x1) and r[0] >= y - 6 for r in frames[i + j])
                       for j in (1, 2, 3) if i + j < len(frames))
            if seen >= 2:
                t = i / AFEED_FPS
                if not kills or t - kills[-1][0] > 0.3:
                    kills.append((t, 30.0))
    return kills


DETECT_VER = 4   # 検出方法を変えたら上げる（古い保存結果を使わないように）
CACHE_FILE = os.path.join(BASE_DIR, "cache", "detect.json")
_cache = None


def _cache_load():
    global _cache
    if _cache is None:
        import json
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _cache_save():
    import json
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _cache_key(path, cfg):
    if not (cfg["game"] in ("valorant", "apex") and cfg.get("feed", True) and not cfg.get("templates")):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{cfg['game']}|v{DETECT_VER}"


def _cache_hit(key, cfg):
    """保存結果を使えるか。キルが無かったクリップは、覚えたバナーが増えていたら見直す"""
    e = _cache_load().get(key) if key else None
    if not e:
        return None
    nb = bt_info()[0] if cfg["game"] == "valorant" else 0
    if not e[0] and (e[4] if len(e) > 4 else 0) < nb:
        return None
    return e


def is_cached(path, cfg):
    return _cache_hit(_cache_key(path, cfg), cfg) is not None


def detect_kills(path, cfg):
    """キル検出。キルフィード方式の結果は保存しておき、同じファイルなら2回目から一瞬で返す"""
    key = _cache_key(path, cfg)
    e = _cache_hit(key, cfg)
    if e:
        return [tuple(p) for p in e[0]], e[1], e[2], e[3]
    r = _detect_kills_raw(path, cfg)
    if key:
        nb = bt_info()[0] if cfg["game"] == "valorant" else 0
        _cache[key] = [[list(p) for p in r[0]], r[1], r[2], r[3], nb]
        _cache_save()
    return r


def _detect_kills_raw(path, cfg):
    """戻り値: (peaks[(秒,スコア)], dur, has_audio, 方式)"""
    dur, has_audio = probe(path)
    if dur <= 0:
        return [], dur, has_audio, "読めない"
    g = GAMES[cfg["game"]]
    A = analyze(path, g["band"]) if has_audio else None

    if cfg["game"] in ("valorant", "apex") and cfg.get("feed", True):
        how_extra = "キルフィード"
        if cfg["game"] == "valorant":
            scan = valorant_scan(path)
            kills = feed_kills(path, scan)
            if not kills:   # キルフィードに何も無い → 覚えたバナーで探す（学習していなければ何もしない）
                kills = banner_onsets(scan[1], scan[2])
                if kills:
                    how_extra = "キルバナー(学習)"
        else:
            kills = apex_feed_kills(path)
        back = 0.35 if cfg["game"] == "valorant" else 0.5
        if kills:
            if A is not None:  # キル音の立ち上がりに合わせて時刻を微調整
                on = onset_of(A)
                fixed = []
                for t, s in kills:
                    lo, hi = max(0, int((t - back) * FPS_A)), min(len(on), int((t + 0.1) * FPS_A) + 1)
                    fixed.append(((lo + int(np.argmax(on[lo:hi]))) / FPS_A if hi > lo else t, s))
                kills = fixed
            return kills, dur, has_audio, how_extra
        if not cfg.get("templates"):
            return [], dur, has_audio, "キルフィードで見つからず"

    if A is None:
        return [], dur, has_audio, "音声なし" if not has_audio else "短すぎ"
    if cfg.get("templates"):
        return template_peaks(A, cfg["templates"], cfg["sens"], g["gap"]), dur, True, "登録キル音"
    return heuristic_peaks(A, cfg["game"], cfg["sens"]), dur, True, "プリセット"


def kill_ranges(kills, dur, cfg):
    """連続キルから残す区間を作る。キルの間が空いていたらジャンプカットで詰める"""
    tail = cfg["post"] + (cfg["slowlen"] if cfg["slowmo"] else 0)
    if not cfg["gapcut"]:
        return [(max(0.0, kills[0] - cfg["pre"]), min(dur, kills[-1] + tail))]
    rs = []
    for i, k in enumerate(kills):
        last = i == len(kills) - 1
        a = max(0.0, k - (cfg["pre"] if i == 0 else cfg["lead"]))
        b = min(dur, k + (tail if last else cfg["killpost"]))
        if rs and a - rs[-1][1] < 0.35:  # ほぼ繋がっているならそのまま
            rs[-1] = (rs[-1][0], max(b, rs[-1][1]))
        else:
            rs.append((a, b))
    return rs


def auto_segments(peaks, dur, cfg):
    """キルをまとめて（連続キル）、使うカットを決める"""
    groups = [[peaks[0]]]
    for p in peaks[1:]:  # 近いキルはまとめて連続キル扱い
        if p[0] - groups[-1][-1][0] < cfg["chain"]:
            groups[-1].append(p)
        else:
            groups.append([p])
    scored = []
    for g in groups:
        s = kills_quality([p[0] for p in g])  # キル数が多い・連続キルほど優先
        kills = [p[0] for p in g]
        scored.append((s, dict(ranges=kill_ranges(kills, dur, cfg), kill=kills[-1], nk=len(kills),
                              kills=kills, dur=dur)))
    scored.sort(key=lambda t: -t[0])
    segs = sorted((s for _, s in scored[: cfg["maxper"]]), key=lambda s: s["ranges"][0][0])
    merged = []
    for s in segs:
        if merged and s["ranges"][0][0] <= merged[-1]["ranges"][-1][1]:
            m = merged[-1]
            rs = m["ranges"] + s["ranges"]
            out = [rs[0]]
            for a, b in rs[1:]:
                if a <= out[-1][1]:
                    out[-1] = (out[-1][0], max(b, out[-1][1]))
                else:
                    out.append((a, b))
            merged[-1] = dict(ranges=out, kill=s["kill"], nk=m["nk"] + s["nk"],
                              kills=m.get("kills", []) + s.get("kills", []), dur=dur)
        else:
            merged.append(s)
    return merged


INTRO_MAX = 22.0  # 導入として切らずに見せる最大の長さ(秒)


def plan_clip(path, cfg):
    """1クリップから使う区間を決める -> ([{ranges, kill, nk}], has_audio, 方式)"""
    peaks, dur, has_audio, how = detect_kills(path, cfg)
    if dur <= 0:
        return [], has_audio, how
    mode = cfg["mode"]
    tail = cfg["post"] + (cfg["slowlen"] if cfg["slowmo"] else 0)

    if mode == "intro":  # 導入: 切らずに見せる
        if not peaks and cfg.get("skipnokill", True):
            return [], has_audio, how + "・キルなし→スキップ"
        if dur <= INTRO_MAX:
            kill = peaks[-1][0] if peaks else -1.0
            return [dict(ranges=[(0.0, dur)], kill=kill, nk=len(peaks), intro=True,
                         kills=[p[0] for p in peaks])], has_audio, how + "・導入"
        if not peaks:
            return [], has_audio, how + "・キルなし→スキップ"
        # 長いクリップ: 一番いい連続キルを、切らずに見せる（長すぎるときは決めのキルまでの最後の約20秒）
        best = max(auto_segments(peaks, dur, dict(cfg, maxper=99)), key=lambda s: (seg_quality(s), -s["kills"][0]))
        ks = best["kills"]
        b = min(dur, ks[-1] + tail + 0.5)
        a = max(0.0, ks[0] - max(cfg["pre"], 2.0), b - INTRO_MAX)
        inside = [k for k in ks if a <= k <= b]
        return [dict(ranges=[(a, b)], kill=ks[-1], nk=len(inside), intro=True, kills=inside)], has_audio, how + "・導入"

    if mode == "whole":
        best = max(peaks, key=lambda p: p[1])[0] if peaks else dur * 0.6
        return [dict(ranges=[(0.0, dur)], kill=best, nk=len(peaks))], has_audio, how

    if mode == "auto" and peaks:
        return auto_segments(peaks, dur, cfg), has_audio, how

    if mode == "auto" and cfg.get("skipnokill", True):
        return [], has_audio, how + "・キルなし→スキップ"
    a = max(0.0, dur - (cfg["pre"] + tail))
    tl = [p for p in peaks if p[0] >= a]
    kill = max(tl, key=lambda p: p[1])[0] if tl else max(a, dur - tail)
    return [dict(ranges=[(a, dur)], kill=kill, nk=len(tl))], has_audio, how + ("" if mode == "end" else "→未検出:最後を使用")


# ---------------------------------------------------------------- BGM: YouTube / Spotify のリンク
BGM_CACHE = os.path.join(BASE_DIR, "bgm_cache")


def is_url(s):
    return isinstance(s, str) and s.strip().lower().startswith(("http://", "https://"))


def _spotify_query(url):
    """Spotify の曲ページから「アーティスト 曲名」を読み取る（音声は保護されているので取らない）"""
    import json
    import re
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")

    def meta(prop):
        m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % prop, html)
        return m.group(1) if m else ""
    title = meta("og:title")
    desc = meta("og:description")   # 例: "アーティスト · アルバム · Song · 2023"
    artist = desc.split("·")[0].strip() if "·" in desc else ""
    if not title:
        oe = urllib.request.urlopen("https://open.spotify.com/oembed?url=" + urllib.request.quote(url, safe=""),
                                    timeout=15).read()
        title = json.loads(oe).get("title", "")
    if not title:
        raise RuntimeError("Spotify のリンクから曲名を読み取れませんでした")
    return f"{artist} {title}".strip()


def resolve_bgm(src, log=lambda s: None):
    """BGM欄の中身をファイルの場所にする。YouTube/Spotify のリンクなら音声を取ってきて保存する"""
    if not is_url(src):
        return src
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("YouTube/Spotify のリンクを使うには yt-dlp が必要です（pip install yt-dlp）")
    import hashlib
    import json
    os.makedirs(BGM_CACHE, exist_ok=True)
    key = hashlib.md5(src.strip().encode()).hexdigest()[:16]
    idx_path = os.path.join(BGM_CACHE, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except (OSError, ValueError):
        idx = {}
    if key in idx and os.path.exists(idx[key]["path"]):
        log(f"BGM: 保存済みの曲を使います（{idx[key]['title']}）")
        return idx[key]["path"]
    if "spotify.com" in src:
        q = _spotify_query(src)
        log(f"BGM: Spotify の曲「{q}」を YouTube で探します")
        target = f"ytsearch1:{q} audio"
    else:
        target = src.strip()
    opts = {"format": "bestaudio/best", "outtmpl": os.path.join(BGM_CACHE, f"{key}.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True}
    log("BGM: 音声をダウンロード中…")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=True)
        if "entries" in info:   # 検索結果
            info = info["entries"][0]
        path = ydl.prepare_filename(info)
    if not os.path.exists(path):
        raise RuntimeError("BGMのダウンロードに失敗しました")
    title = info.get("title", "")
    idx[key] = {"path": path, "title": title, "src": src, "page": info.get("webpage_url", "")}
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    log(f"BGM: 「{title}」を使います（{info.get('webpage_url', '')}）")
    return path


# ---------------------------------------------------------------- BGM
def bgm_filter(cfg):
    ch = []
    p = 2 ** (cfg["bgmpitch"] / 12.0)
    s = cfg["bgmspeed"] / 100.0
    if abs(p - 1) > 1e-3 or abs(s - 1) > 1e-3:
        if has_filter("rubberband"):
            ch.append(f"rubberband=pitch={p:.5f}:tempo={s:.4f}:pitchq=quality")
        else:
            ch.append(f"asetrate={int(48000 * p)},aresample=48000")
            r = s / p
            while r > 2.0:
                ch.append("atempo=2.0")
                r /= 2
            while r < 0.5:
                ch.append("atempo=0.5")
                r /= 0.5
            ch.append(f"atempo={r:.5f}")
    if cfg["bgmbass"] > 0:
        ch.append(f"bass=g={cfg['bgmbass']:.1f}:f=110")
    if cfg["bgmreverb"]:
        ch.append("aecho=0.85:0.6:70|140|230:0.35|0.25|0.15")
    ch.append("aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo")
    return ",".join(ch)


def process_bgm(cfg, out_wav, length):
    run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-ss", f"{cfg['bgmstart']:.2f}",
         "-i", cfg["bgm"], "-vn", "-af", bgm_filter(cfg), "-t", f"{length:.2f}",
         "-c:a", "pcm_s16le", out_wav])


def detect_beats(wav, bpm=None, max_sec=90):
    """BPMと最初の拍の位置を推定 -> (bpm, 最初の拍の秒)"""
    sr, hop, nfft = 11025, 256, 1024
    x = load_audio(wav, sr, dur=max_sec)
    if len(x) < sr * 4:
        raise RuntimeError("BGMが短すぎます")
    fr = swv(x, nfft)[::hop] * np.hanning(nfft).astype(np.float32)
    mag = np.log1p(100 * np.abs(np.fft.rfft(fr, axis=1)))
    flux = np.maximum(0, np.diff(mag, axis=0)).sum(1)
    env = np.maximum(0, flux - movavg(flux, 43))
    env = env / (env.max() + 1e-9)
    fps = sr / hop
    idx = np.arange(len(env))

    def grid_score(b):
        P = fps * 60 / b
        best = (-1, 0)
        for ph in np.arange(0, P, 0.5):
            pos = np.arange(ph, len(env) - 1, P)
            sc = np.interp(pos, idx, env).sum() / len(pos)
            if sc > best[0]:
                best = (sc, ph)
        return best

    if not bpm:
        lags = np.arange(int(fps * 60 / 200), int(fps * 60 / 60) + 1)
        ac = np.array([np.dot(env[:-l], env[l:]) for l in lags])
        bpms = fps * 60 / lags
        ac *= np.exp(-0.5 * (np.log2(bpms / 120) / 1.0) ** 2)
        coarse = bpms[int(np.argmax(ac))]
        cands = coarse * np.linspace(0.97, 1.03, 61)
        bpm = max(cands, key=lambda b: grid_score(b)[0])
    _, ph = grid_score(bpm)
    # flux はフレーム差分 & 窓の中心ぶん遅れて反応するので補正
    b0 = ph / fps + (hop + nfft / 2) / sr
    return float(bpm), float(b0 % (60.0 / bpm))


def beat_plan(rendered, T, unit, td, post_t):
    """キルが拍に、カットの切り替えが拍(unit拍ごと)に来るように切る長さを決める"""
    out = []
    for i, (f, d, k, pre_t, *_) in enumerate(rendered):
        last = i == len(rendered) - 1
        if pre_t == "intro":  # 導入はほぼ丸ごと。キル(あれば)が拍に乗るよう頭を1拍未満だけ削る
            h = k - int(k // T) * T if k > 0 else 0.0
            beats = max(unit, int((d - h - (0 if last else td)) // (T * unit)) * unit)
            L = beats * T + (0 if last else td)
            out.append(dict(file=f, h=h, L=L, pad=max(0.0, h + L - d)))
            continue
        n = int(k // T)
        if n >= 1:
            n = min(n, max(1, round(pre_t / T)))
        h = k - n * T
        avail = int((d - k) // T)
        mp = max(1, min(max(1, round(post_t / T)), max(1, avail)))
        beats = int(math.ceil((n + mp) / unit) * unit)
        if last:
            beats += unit
        L = beats * T + (0 if last else td)
        out.append(dict(file=f, h=h, L=L, pad=max(0.0, h + L - d)))
    return out


# ---------------------------------------------------------------- 曲の盛り上がり
def analyze_song(wav, bpm=None):
    """曲の音量の流れから「静かなところ → 一気に盛り上がる」瞬間（サビの入り・ドロップ）を探す"""
    sr, hop = 11025, 512
    x = load_audio(wav, sr)
    n = len(x) // hop
    if n < 100:
        raise RuntimeError("BGMが短すぎます")
    fps = sr / hop
    e = 10 * np.log10((x[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-9)
    E = movavg(e, int(fps))            # 1秒でならした音量
    bpm, b0 = detect_beats(wav, bpm)
    T = 60.0 / bpm
    dur = n / fps
    W, gapf = int(6 * fps), int(0.4 * fps)
    loud, med = np.percentile(E, 60), np.median(E)
    cands = []
    t = b0
    while t < dur - 4:
        i = int(t * fps)
        if i > 3 * fps:
            before = E[max(0, i - W):i - gapf].mean()
            after = E[i:i + W].mean()
            rise = after - before
            if rise > 2.5 and after > loud:   # 大きく上がって、しかも曲の中で大きい側に入る
                cands.append((t, float(rise + 0.5 * (after - med))))
        t += T
    picked = []
    for t, s in sorted(cands, key=lambda c: -c[1]):
        if all(abs(t - p) > 12 for p, _ in picked):
            picked.append((t, s))
    step = max(1, n // 600)
    curve = E[::step]
    return dict(bpm=bpm, b0=b0, T=T, dur=dur, drops=sorted(picked),
                curve=np.clip((curve - np.percentile(E, 5)) / (np.percentile(E, 95) - np.percentile(E, 5) + 1e-9),
                              0, 1).tolist())


def drop_plan(rendered, T, unit, td, post_t, drops, log):
    """盛り上がり（drops: 動画の時間軸で[(秒, 強さ)]）に、質の高いカットの最後のキルが来るように並べる"""
    ceil_u = lambda b: int(math.ceil(b / unit) * unit)

    def lead(r):  # キルの前に何拍見せるか (標準, 最大)
        k, lim = r[2], r[3]
        nmax = max(0, int(k // T + 1e-6)) if k > 0 else 0
        if lim == "intro":
            return nmax, nmax
        nmin = min(nmax, max(1, round(lim / T))) if nmax >= 1 else 0
        return nmin, nmax

    def post(r):
        avail = int((r[1] - r[2]) // T)
        return max(1, min(max(1, round(post_t / T)), max(1, avail)))

    def length(r, n):
        if r[3] == "intro":
            h = r[2] - n * T if r[2] > 0 else 0.0
            return max(unit, int((r[1] - h - td) // (T * unit)) * unit)
        return ceil_u(n + post(r))

    head = [i for i, r in enumerate(rendered) if r[3] == "intro"]
    pool = [i for i in range(len(rendered)) if i not in head]
    # 普通に並べたときの長さの中に入る盛り上がりだけ使う（映像を引き延ばしてまで届かせない）
    natural = sum(length(r, lead(r)[0]) for r in rendered)
    drops = [d for d in drops if round(d[0] / T) <= natural - 2]
    strong = sorted(drops, key=lambda x: -x[1])[:min(4, len(pool))]
    best = sorted(pool, key=lambda i: (-rendered[i][5], i))
    anchors = sorted((round(t / T), t, i) for (t, _), i in zip(strong, best))
    used = {i for _, _, i in anchors}
    normal = [i for i in pool if i not in used]
    plan, pos = [], 0
    for i in head:
        n = lead(rendered[i])[0]
        plan.append([i, n, length(rendered[i], n)])
        pos += plan[-1][2]
    for B, t, s in anchors:
        nmin, nmax = lead(rendered[s])
        if pos + nmin > B:   # 間に合わない → 普通のカットとして後ろに回す
            normal.append(s)
            continue
        while normal:   # 盛り上がりの手前まで、普通のカットを並べる
            j = normal[0]
            m = length(rendered[j], lead(rendered[j])[0])
            if pos + m + nmin > B:
                break
            normal.pop(0)
            plan.append([j, lead(rendered[j])[0], m])
            pos += m
        gap = B - pos - nmin
        inc = min(gap, nmax - nmin)   # まずキル前の助走を長めに見せて埋める
        gap -= inc
        if gap >= 2 and normal:       # 次のカットを短く詰めて、すき間にぴったり入れる
            j = normal[0]
            r = rendered[j]
            jmin, jmax = lead(r)
            pav = max(1, int((r[1] - r[2]) // T))
            nj = min(max(1, min(jmin, gap - 1)), jmax) if jmax >= 1 else 0
            if gap - nj > pav:
                nj = min(jmax, gap - pav)
            if 1 <= gap - nj <= pav and nj <= jmax:
                normal.pop(0)
                plan.append([j, nj, gap])
                pos += gap
                gap = 0
        avail = 0
        if plan:                      # 次に前のカットの続きの映像で埋める
            p = plan[-1]
            r = rendered[p[0]]
            h = r[2] - p[1] * T if r[2] > 0 else 0.0
            avail = max(0, int((r[1] - h) // T) - p[2])
        if gap > avail + 1:           # 実際の映像で埋めきれない → この盛り上がりには合わせない
            normal.append(s)
            continue
        if gap > 0:
            plan[-1][2] += gap        # 1拍以内なら最後の1コマを少し止めて合わせる
        n = nmin + inc
        m = n + post(rendered[s])
        plan.append([s, n, m])
        pos += m
        log(f"   盛り上がり {fmt_time(B * T)} に {rendered[s][4]}キルのカットを合わせました")
    for j in normal:
        n = lead(rendered[j])[0]
        plan.append([j, n, length(rendered[j], n)])
    out = []
    for idx, (i, n, m) in enumerate(plan):
        f, d, k = rendered[i][:3]
        last = idx == len(plan) - 1
        h = k - n * T if k > 0 else 0.0
        L = (m + (unit if last else 0)) * T + (0 if last else td)
        out.append(dict(file=f, h=h, L=L, pad=max(0.0, h + L - d)))
    return out


# ---------------------------------------------------------------- 映像
def base_vf(cfg):
    W, H, fps = cfg["W"], cfg["H"], cfg["fps"]
    if cfg["vertical"]:
        sc = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        sc = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    gr = GRADES.get(cfg["grade"], "")
    return f"{sc},setsar=1,fps={fps}" + (f",{gr}" if gr else "") + ",format=yuv420p"


def encoder_args(cfg, final):
    if cfg["encoder"] == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19" if final else "17", "-b:v", "0",
                "-pix_fmt", "yuv420p", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "medium" if final else "veryfast",
            "-crf", "18" if final else "16", "-pix_fmt", "yuv420p", "-profile:v", "high"]


def render_segment(src, has_audio, ranges, kill, out, cfg, gain_db=0.0):
    """1カットを作る（複数区間はジャンプカットで繋ぐ）。戻り値 (長さ, 出力内でのキル位置)"""
    start, end = ranges[0][0], ranges[-1][1]
    span = end - start
    k = kill - start
    W, H, fps = cfg["W"], cfg["H"], cfg["fps"]
    sp = cfg["slowspeed"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", src]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{span:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    ain = "[0:a]" if has_audio else "[1:a]"

    # (開始, 終了, 速度, 種類, 区間の頭?, 区間の終わり?)  種類: n=通常 / r=速度変化 / s=キルのスロー
    pieces = []
    vel = cfg.get("velocity", False)
    for a, b in ranges:
        a, b = a - start, b - start
        if cfg["slowmo"] and a <= k < b - 0.05:
            s_end = min(b, k + cfg["slowlen"])
            if vel:  # ベロシティ: キル直前は速く → 減速 → キルでスロー → ゆっくり戻す
                p1, p2 = max(a, k - 0.75), max(a, k - 0.25)
                r_end = min(b, s_end + 0.35)
                sub = [(a, p1, 1.0, "n"), (p1, p2, 1.6, "r"), (p2, k, 0.8, "r"), (k, s_end, sp, "s"),
                       (s_end, r_end, 0.75, "r"), (r_end, b, 1.0, "n")]
            else:
                sub = [(a, k, 1.0, "n"), (k, s_end, sp, "s"), (s_end, b, 1.0, "n")]
        else:
            sub = [(a, b, 1.0, "n")]
        sub = [p for p in sub if p[1] - p[0] > 0.04]
        for j, (x, y, spd, kind) in enumerate(sub):
            pieces.append((x, y, spd, kind, j == 0, j == len(sub) - 1))

    kout, t = 0.0, 0.0
    for x, y, spd, kind, _, _ in pieces:
        if kind == "s" or (kind == "n" and x <= k < y):
            kout = t + (0.0 if kind == "s" else k - x)
            break
        t += (y - x) / spd
    n = len(pieces)
    multi = len(ranges) > 1
    fc = [f"[0:v]{base_vf(cfg)},split={n}" + "".join(f"[vs{i}]" for i in range(n)),
          f"{ain}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume={gain_db:.2f}dB,asplit={n}"
          + "".join(f"[as{i}]" for i in range(n))]
    zoom = 1.12 if cfg["zoom"] else (1.05 if cfg.get("shake") else 1.0)
    ZW, ZH = int(W * zoom) // 2 * 2, int(H * zoom) // 2 * 2
    amp = W * 0.012
    for i, (a, b, spd, kind, head, tailp) in enumerate(pieces):
        v = f"[vs{i}]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS"
        au = f"[as{i}]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS"
        if multi and head and i > 0:
            au += ",afade=t=in:d=0.015"  # ジャンプカットのプチノイズ防止
        if multi and tailp and i < n - 1:
            au += f",afade=t=out:st={max(0, b - a - 0.015):.3f}:d=0.015"
        if kind == "r":
            v += f",setpts=PTS/{spd},fps={fps}"
            au += f",atempo={spd}"
        if kind == "s":
            v += f",setpts=PTS/{sp},fps={fps}"
            if zoom > 1:
                v += f",scale={ZW}:{ZH}"
                if cfg.get("shake"):  # キルの瞬間に画面を揺らして、すぐ収まる
                    v += (f",crop={W}:{H}:x='(iw-ow)/2+{amp:.1f}*sin(t*70)*exp(-t*7)'"
                          f":y='(ih-oh)/2+{amp:.1f}*cos(t*83)*exp(-t*7)'")
                else:
                    v += f",crop={W}:{H}"
                v += ",setsar=1"
            if cfg["flash"]:
                v += ",fade=t=in:st=0:d=0.25:color=white"
            au += f",asetrate={int(48000 * sp)},aresample=48000"  # 低く重いスロー音
        fc += [v + f"[v{i}]", au + f"[a{i}]"]
    fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vo][ao]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vo]", "-map", "[ao]",
            *encoder_args(cfg, False), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out]
    run(cmd)
    return probe(out)[0], kout


def fmt_time(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def target_length(cfg):
    """BGMの実際の長さ - 余白。BGMが無い/合わせない設定なら None"""
    if not cfg.get("bgm") or not cfg.get("fitbgm", True):
        return None
    dur, _ = probe(cfg["bgm"])
    if dur <= 0:
        return None
    real = (dur - cfg.get("bgmstart", 0.0)) / (cfg.get("bgmspeed", 100) / 100.0)  # 速度を変えると長さも変わる
    return max(10.0, real - cfg.get("endmargin", 5.0))


def burst(kills, w):
    """w秒の間に最大何キルしたか（3秒で3キル など）"""
    ks = sorted(kills)
    best, j = 0, 0
    for i in range(len(ks)):
        while ks[i] - ks[j] > w:
            j += 1
        best = max(best, i - j + 1)
    return best


def kills_quality(kills, n=None):
    """カットの質。
    ・1ラウンドのキル数が多いほど高い（3キル/4キル/ACEはボーナス）
    ・短い時間に連続で倒すほど高い（3秒以内の連続キルを大きく加点、5秒以内も少し加点）"""
    n = len(kills) if n is None else n
    q = n * 10 + (10 if n >= 3 else 0) + (15 if n >= 4 else 0) + (25 if n >= 5 else 0)
    if kills:
        b3, b5 = burst(kills, 3.0), burst(kills, 5.0)
        q += (b3 - 1) * 12 + (b5 - b3) * 4
    return q


def seg_quality(s):
    return kills_quality(s.get("kills") or [], s["nk"])


def seg_desc(s):
    ks = s.get("kills") or []
    b3 = burst(ks, 3.0) if ks else 0
    return f"{s['nk']}キル" + (f"（3秒で{b3}キル）" if b3 >= 2 else "")


def seg_length(s, cfg):
    """書き出し後のおおよその長さ"""
    L = sum(b - a for a, b in s["ranges"])
    if cfg["slowmo"] and s.get("kill", -1) >= 0:
        L += cfg["slowlen"] * (1 / cfg["slowspeed"] - 1)
    return L


def _clip_key(path):
    """「○○ - Trim.mp4」と「○○.mp4」のように、同じ元動画から作られたファイルを同じグループにする"""
    import re
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem = re.sub(r"(\s*-\s*trim(med)?|\s*\(\d+\)|\s*-\s*コピー|\s*copy)+$", "", stem)
    return stem.strip()


def _fingerprint(path, t):
    """キルの瞬間あたりの小さな白黒画像（同じ場面かどうかの見比べ用）"""
    fps = []
    for dt in (-1.0, -0.3):
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t + dt):.3f}", "-i", path, "-frames:v", "1",
                   "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"])
        if len(raw) < 32 * 18:
            return None
        fps.append(np.frombuffer(raw[:32 * 18], np.uint8).astype(np.float32))
    return np.concatenate(fps)


def dedupe(plan, log):
    """同じ場面のカットは1つだけ残す（質の高い方 / 導入は優先）"""
    rank = lambda p: (1 if p[2].get("intro") else 0, seg_quality(p[2]), -seg_len_raw(p[2]))
    # 1) ファイル名で同じグループ（Trim版と元ファイルなど）
    best = {}
    for p in plan:
        k = _clip_key(p[0])
        if k not in best or rank(p) > rank(best[k]):
            best[k] = p
    keep = [p for p in plan if best[_clip_key(p[0])] is p]
    for p in plan:
        if p not in keep:
            log(f"   重複を外しました: {os.path.basename(p[0])}（{os.path.basename(best[_clip_key(p[0])][0])} と同じ動画）")
    # 2) キルの瞬間の映像が同じ（名前が違っても同じ場面）
    fps = [(_fingerprint(p[0], p[2]["kill"]) if p[2].get("kill", -1) >= 0 else None) for p in keep]
    drop = set()
    for i in range(len(keep)):
        for j in range(i + 1, len(keep)):
            if i in drop or j in drop or fps[i] is None or fps[j] is None:
                continue
            if float(np.abs(fps[i] - fps[j]).mean()) < 9.0:
                lo = i if rank(keep[i]) < rank(keep[j]) else j
                hi = j if lo == i else i
                drop.add(lo)
                log(f"   重複を外しました: {os.path.basename(keep[lo][0])}（{os.path.basename(keep[hi][0])} と同じ場面）")
    return [p for i, p in enumerate(keep) if i not in drop]


def seg_len_raw(s):
    return sum(b - a for a, b in s["ranges"])


def select_best(plan, cfg, cap, log):
    td = cfg["tdur"] if TRANSITIONS[cfg["transition"]] else 0.0
    head = [p for p in plan if p[2].get("intro")]
    tail = [p for p in plan if not p[2].get("intro")]
    fac = 1.3 if cfg.get("ramp", True) else 1.0   # テンポ上げで序盤が少し長くなる分の見込み
    total_all = sum(seg_length(p[2], cfg) for p in plan)
    if total_all - td * (len(plan) - 1) <= cap:
        log(f"長さ: 全カット 約{fmt_time(total_all)} ≦ 目標 {fmt_time(cap)}（全部使います）")
        return plan
    used = sum(seg_length(p[2], cfg) for p in head)
    order = sorted(range(len(tail)), key=lambda i: (-seg_quality(tail[i][2]), seg_length(tail[i][2], cfg)))
    keep = set()
    for i in order:
        L = seg_length(tail[i][2], cfg) * (1 + (fac - 1) * 0.5) - td
        if used + L <= cap:
            keep.add(i)
            used += L
    chosen = head + [p for i, p in enumerate(tail) if i in keep]   # 元の順番のまま
    nk_all = sum(p[2]["nk"] for p in plan)
    nk_use = sum(p[2]["nk"] for p in chosen)
    log(f"長さ: 全{len(plan)}カット {fmt_time(total_all)}（{nk_all}キル） → 目標 {fmt_time(cap)} に収まるよう"
        f" 質の高い{len(chosen)}カット（{nk_use}キル）を選びました")
    return chosen


def render_final(items, out, cfg, tmp, progress, T=None, bgm_wav=None, bgm_ss=0.0):
    W, H, fps = cfg["W"], cfg["H"], cfg["fps"]
    trans = TRANSITIONS[cfg["transition"]]
    td = cfg["tdur"] if trans else 0.0
    n = len(items)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for it in items:
        cmd += ["-i", os.path.basename(it["file"])]
    fc = []
    for i, it in enumerate(items):
        h, L, pad = it["h"], it["L"], it["pad"]
        v = f"[{i}:v]settb=AVTB,fps={fps},trim=start={h:.4f},setpts=PTS-STARTPTS"
        a = f"[{i}:a]atrim=start={h:.4f},asetpts=PTS-STARTPTS"
        if pad > 0:
            v += f",tpad=stop_mode=clone:stop_duration={pad + 0.1:.3f}"
            a += f",apad=pad_dur={pad + 0.1:.3f}"
        fc.append(v + f",trim=duration={L:.4f},setpts=PTS-STARTPTS[iv{i}]")
        fc.append(a + f",atrim=duration={L:.4f},asetpts=PTS-STARTPTS[ia{i}]")

    Ls = [it["L"] for it in items]
    if n == 1:
        fc += ["[iv0]null[cv]", "[ia0]anull[ca]"]
        total = Ls[0]
    elif not trans:
        fc.append("".join(f"[iv{i}][ia{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")
        total = sum(Ls)
    else:
        lastv, lasta, acc = "[iv0]", "[ia0]", Ls[0]
        for i in range(1, n):
            nv, na = (f"[xv{i}]", f"[xa{i}]") if i < n - 1 else ("[cv]", "[ca]")
            fc.append(f"{lastv}[iv{i}]xfade=transition={random.choice(trans)}:duration={td:.3f}:offset={acc - td:.4f}{nv}")
            fc.append(f"{lasta}[ia{i}]acrossfade=d={td:.3f}{na}")
            lastv, lasta = nv, na
            acc += Ls[i] - td
        total = acc

    if cfg.get("maxtotal") and total > cfg["maxtotal"]:  # 目標より長くなったら、そこでフェードアウトして終わる
        total = cfg["maxtotal"]
    vch = "[cv]"
    if T and cfg["beatflash"]:  # 拍ごとに光らせる
        fc.append(f"{vch}eq=brightness='0.10*exp(-14*mod(t,{T:.5f}))':eval=frame[bv]")
        vch = "[bv]"
    if cfg["title"].strip():
        font = next((f for f in FONTS if os.path.exists(f)), None)
        if font:
            ext = os.path.splitext(font)[1]
            shutil.copy(font, os.path.join(tmp, "font" + ext))
            with open(os.path.join(tmp, "title.txt"), "w", encoding="utf-8") as f:
                f.write(cfg["title"].strip())
            fs = int(H * (0.07 if cfg["vertical"] else 0.11))
            alpha = "if(lt(t,0.3),t/0.3,if(lt(t,2.3),1,max(0,(2.8-t)/0.5)))"
            fc.append(f"{vch}drawtext=fontfile=font{ext}:textfile=title.txt:fontsize={fs}:fontcolor=white:"
                      f"borderw={max(3, fs // 14)}:bordercolor=black@0.9:shadowx=6:shadowy=6:shadowcolor=black@0.5:"
                      f"x=(w-tw)/2:y=(h-th)/2:alpha='{alpha}':enable='lt(t,2.8)'[tv]")
            vch = "[tv]"
    fo = max(0.0, total - cfg["fadeout"])
    fc.append(f"{vch}fade=t=out:st={fo:.3f}:d={cfg['fadeout']:.2f},format=yuv420p[vout]")

    if bgm_wav:
        cmd += ["-ss", f"{bgm_ss:.4f}", "-i", os.path.basename(bgm_wav)]
        fi = f"afade=t=in:d={cfg['fadein']:.2f}," if cfg["fadein"] > 0 else ""
        fc.append(f"[{n}:a]atrim=0:{total:.3f},volume={cfg['bgmvol']:.2f},{fi}"
                  f"afade=t=out:st={fo:.3f}:d={cfg['fadeout']:.2f}[m]")
        fc.append(f"[ca]volume={cfg['gamevol']:.2f}[g]")
        fc.append("[g][m]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=disabled[aout]")
    else:
        fc.append(f"[ca]volume={cfg['gamevol']:.2f},afade=t=out:st={fo:.3f}:d={cfg['fadeout']:.2f},"
                  "alimiter=limit=0.89:level=disabled[aout]")

    with open(os.path.join(tmp, "graph.txt"), "w", encoding="utf-8") as f:
        f.write(";\n".join(fc))
    cmd += ["-/filter_complex", "graph.txt", "-map", "[vout]", "-map", "[aout]",
            *encoder_args(cfg, True), "-c:a", "aac", "-b:a", "256k",
            "-movflags", "+faststart", "-t", f"{total:.3f}", os.path.abspath(out)]
    run_progress(cmd, total, progress, cwd=tmp)
    return total


def build(clips, out, cfg, log, progress):
    tmp = tempfile.mkdtemp(prefix="killmontage_")
    try:
        cfg = dict(cfg)
        cfg["templates"] = load_templates(cfg["game"])
        if cfg.get("bgm"):   # YouTube/Spotify のリンクなら先に音声を取ってくる
            cfg["bgm"] = resolve_bgm(cfg["bgm"], log)
        log(f"ゲーム: {GAMES[cfg['game']]['name']} / 登録キル音: {len(cfg['templates'])}個")
        plan = []
        intro_on = cfg.get("intro", True) and len(clips) > 1
        # 進み具合を「かかる時間」に近づける。
        #   解析: 動画の長さに比例（保存済みのクリップは一瞬） / カット作成: カット数に比例 / 書き出し: 動画の長さに比例
        durs = [max(1.0, probe(c)[0]) for c in clips]
        wts = [0.3 + (0.0 if is_cached(c, cfg) else d / 14.0) for c, d in zip(clips, durs)]   # 実測: 動画1秒あたり約0.07秒
        cost_a = sum(wts)
        cost_r = 1.5 * len(clips)                                   # 実測: 1クリップあたり約1.5秒
        cost_f = 1.6 * min(sum(durs) * 0.3, target_length(cfg) or 150)   # 実測: 出来上がり1秒あたり約1.6秒
        tot_c = cost_a + cost_r + cost_f
        PA, PR = cost_a / tot_c, (cost_a + cost_r) / tot_c
        tot_d, done_d = sum(wts), 0.0
        global SCAN_HOOK
        for i, c in enumerate(clips):
            log(f"解析中 ({i + 1}/{len(clips)}): {os.path.basename(c)}")
            SCAN_HOOK = (lambda fr, i=i: progress(PA * (done_d + wts[i] * fr) / tot_d))
            cc = dict(cfg)
            if intro_on and i == 0:
                cc["mode"] = "intro"
            segs, ha, how = plan_clip(c, cc)
            if not segs:
                log(f"   → 使いません [{how}]")
            gain = 0.0
            usable = any(s["nk"] >= cfg.get("minkills", 2) for s in segs)
            if ha and cfg.get("gamenorm", True) and usable:   # 使うクリップだけ音量を測る
                lufs = measure_lufs(c)
                if lufs is not None:  # 小さいクリップほど持ち上げて大きさをそろえる
                    gain = max(-10.0, min(24.0, cfg.get("gametarget", -12.0) - lufs))
                    log(f"   ゲーム音 {lufs:.1f} LUFS → {gain:+.1f}dB")
            for s in segs:
                s["gain"], s["how"] = gain, how
                if s["nk"] < cfg.get("minkills", 2):   # 1キルだけのカットは使わない
                    log(f"   → 使いません [{s['nk']}キルだけ]")
                    continue
                log(f"   → {seg_desc(s)}  評価{seg_quality(s)} [{how}]")
                plan.append((c, ha, s))
            done_d += wts[i]
            SCAN_HOOK = None
            progress(PA * done_d / tot_d)
        if not plan:
            raise RuntimeError("使える区間が見つかりませんでした。")

        # 同じ動画・同じ場面のダブりを外す
        log("重複チェック中…")
        plan = dedupe(plan, log)

        # BGMの長さに収まるように、質の高いカットだけ選ぶ
        cap = target_length(cfg)
        if cap:
            plan = select_best(plan, cfg, cap, log)
            cfg["maxtotal"] = cap

        # 並び順を決める（導入は先頭・シャッフル・締めは一番のカット）
        head = [p for p in plan if p[2].get("intro")]
        tail = [p for p in plan if not p[2].get("intro")]
        if cfg["shuffle"]:
            random.shuffle(tail)
        if cfg.get("bestlast", True) and len(tail) > 1:
            best = max(range(len(tail)), key=lambda i: (seg_quality(tail[i][2]), i))
            tail.append(tail.pop(best))
            log(f"締め: {os.path.basename(tail[-1][0])}（{tail[-1][2]['nk']}キル）")

        # 並び順が決まってから、だんだんテンポを上げる（序盤1.6倍ゆったり → 終盤は設定どおり）
        for j, (c, ha, s) in enumerate(tail):
            if cfg.get("ramp", True) and len(tail) > 1 and s.get("kills"):
                fac = 1 + 0.3 * (1 - j / (len(tail) - 1))
                cc = dict(cfg, pre=cfg["pre"] * fac, lead=cfg["lead"] * fac, killpost=cfg["killpost"] * fac)
                s["ranges"] = kill_ranges(s["kills"], s["dur"], cc)
        plan = head + tail
        log(f"構成: {len(plan)}カット / 合計 {fmt_time(sum(seg_len_raw(p[2]) for p in plan))}")
        for c, ha, s in plan:
            rs = " + ".join(f"{a:.1f}〜{b:.1f}s" for a, b in s["ranges"])
            cut = sum(b - a for a, b in s["ranges"])
            log(f"   {os.path.basename(c)}: {s['nk']}キル  {rs}  (使用 {cut:.1f}秒)  [{s['how']}]  {seg_desc(s)} 評価{seg_quality(s)}")

        rendered = []
        for i, (c, ha, s) in enumerate(plan):
            log(f"カット作成 ({i + 1}/{len(plan)})")
            f = os.path.join(tmp, f"seg{i:03d}.mp4")
            d, kout = render_segment(c, ha, s["ranges"], s["kill"], f, cfg, s.get("gain", 0.0))
            if d > cfg["tdur"] * 2 + 0.2:
                # 連続キルのカットは、音はめで頭を削って前のキルが消えないようにする
                if s.get("intro"):
                    lim = "intro"
                else:
                    lim = kout if s["nk"] > 1 else cfg["pre"]
                rendered.append((f, d, kout, lim, s["nk"], seg_quality(s)))
            progress(PA + (PR - PA) * (i + 1) / len(plan))
        if not rendered:
            raise RuntimeError("カットが短すぎます。")

        bgm_wav, T, ss, drops = None, None, 0.0, []
        if cfg["bgm"]:
            log("BGMを加工中…")
            bgm_wav = os.path.join(tmp, "bgm.wav")
            process_bgm(cfg, bgm_wav, sum(r[1] for r in rendered) + 90)
            if cfg["beat"]:
                if cfg.get("drops", True):
                    song = analyze_song(bgm_wav, cfg["bpm"] or None)
                    bpm, b0 = song["bpm"], song["b0"]
                else:
                    bpm, b0 = detect_beats(bgm_wav, cfg["bpm"] or None)
                T = 60.0 / bpm
                ss = b0 + cfg["beatoffset"] / 1000.0
                while ss < 0:
                    ss += T
                log(f"音はめ: BPM {bpm:.1f}（最初の拍 {b0:.2f}秒）")
                if cfg.get("drops", True):
                    # 動画の時間軸に直す（BGMは ss から流れる）
                    drops = [(t - ss, sc) for t, sc in song["drops"] if t - ss > 3]
                    if cfg.get("maxtotal"):
                        drops = [d for d in drops if d[0] < cfg["maxtotal"] - 2]
                    log("曲の盛り上がり: " + (" / ".join(fmt_time(t + ss) for t, _ in drops) or "見つかりませんでした"))
        elif cfg["beat"] and cfg["bpm"]:
            T = 60.0 / cfg["bpm"]
        elif cfg["beat"]:
            log("BGMが無いので音はめはオフにしました")
        progress(PR)

        td = cfg["tdur"] if TRANSITIONS[cfg["transition"]] else 0.0
        if T:
            post_out = (cfg["slowlen"] / cfg["slowspeed"] if cfg["slowmo"] else 0) + cfg["post"]
            if drops:
                items = drop_plan(rendered, T, cfg["beatunit"], td, post_out, drops, log)
            else:
                items = beat_plan(rendered, T, cfg["beatunit"], td, post_out)
        else:
            items = [dict(file=r[0], h=0.0, L=r[1], pad=0.0) for r in rendered]

        log(f"書き出し中… ({len(items)}カット)")
        total = render_final(items, out, cfg, tmp, lambda p: progress(PR + (0.97 - PR) * p), T, bgm_wav, ss)
        log(f"完成！ {fmt_time(total)}（{total:.1f}秒） → {out}")
        # 完成画面用のまとめ: 各カットの決めのキルが、出来上がりの何秒目にあるか
        info = {r[0]: r for r in rendered}
        marks, t = [], 0.0
        for it in items:
            r = info.get(it["file"])
            if r and t < total:
                marks.append(dict(t=min(total - 0.1, t + max(0.0, r[2] - it["h"]) + 0.4), nk=r[4]))
            t += it["L"] - td
        summary = dict(out=out, total=total, cuts=len(items), kills=sum(r[4] for r in rendered),
                       clips=len(clips), marks=marks)
        if cfg["game"] == "valorant" and cfg.get("learn", True):   # 次からさらに正確に
            try:
                learn_banners(clips, cfg, log)
            except Exception as e:
                log(f"学習はスキップしました: {e}")
        progress(1.0)
        return summary
    finally:
        SCAN_HOOK = None
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 参考動画から学ぶ
def analyze_reference(path):
    """他の人のキル集を分析して、カットのリズム・フラッシュの量・音はめの有無を測る"""
    dur, has_audio = probe(path)
    sz = 96 * 54
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "fps=30,scale=96:54,format=gray",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=NOWIN)
    diffs, bright, prev = [], [], None
    while True:
        b = p.stdout.read(sz)
        if len(b) < sz:
            break
        fr = np.frombuffer(b, np.uint8).astype(np.float32)
        bright.append(fr.mean())
        diffs.append(np.abs(fr - prev).mean() if prev is not None else 0.0)
        prev = fr
    p.wait()
    d = np.array(diffs)
    med = np.convolve(d, np.ones(15) / 15, "same")
    cuts = []
    for i in np.where((d > 14) & (d > 3 * med))[0]:
        t = i / 30
        if not cuts or t - cuts[-1] > 0.25:
            cuts.append(t)
    br = np.array(bright)
    flashes = [i / 30 for i in range(1, len(br)) if br[i] > 200 and br[i - 1] <= 200]
    shots = np.diff([0.0] + cuts + [dur])
    r = dict(name=os.path.basename(path), dur=dur, cuts=len(cuts), flashes=len(flashes),
             shot_median=float(np.median(shots)), shot_mean=float(np.mean(shots)),
             first_cut=float(cuts[0]) if cuts else dur,
             fast_ratio=float(np.mean(shots < 1.0)), bpm=None, beat_ratio=None, beat_base=None)
    if has_audio and len(cuts) >= 8:
        tmp = tempfile.mkdtemp(prefix="km_ref_")
        try:
            wav = os.path.join(tmp, "a.wav")
            run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vn", "-ac", "2", "-ar", "48000", wav])
            bpm, b0 = detect_beats(wav, max_sec=min(120, dur))
            T = 60 / bpm
            err = [abs((c - b0) / T - round((c - b0) / T)) * T for c in cuts]
            r.update(bpm=bpm, beat_ratio=float(np.mean(np.array(err) < 0.07)), beat_base=min(1.0, 0.14 / T))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return r


def style_from_refs(refs):
    """分析結果から設定を作る"""
    shot = float(np.median([r["shot_median"] for r in refs]))
    cuts = sum(r["cuts"] for r in refs)
    flash = sum(r["flashes"] for r in refs) / max(1, cuts)
    synced = [r for r in refs if r["beat_ratio"] is not None]
    beat = bool(synced) and bool(np.mean([r["beat_ratio"] for r in synced]) > 1.8 * np.mean([r["beat_base"] for r in synced]))
    clip = lambda x, lo, hi: float(min(hi, max(lo, x)))
    return dict(
        # ショット1つ ≒ 各キルの前 + 後 として配分する
        lead=round(clip(shot * 0.4, 0.6, 1.3), 1),
        killpost=round(clip(shot * 0.18, 0.2, 0.7), 2),
        pre=round(clip(shot * 0.5, 0.8, 1.6), 1),
        transition="なし(カット)" if flash < 0.2 else "白フラッシュ",
        beat=beat,
        intro=any(r["first_cut"] >= 5 for r in refs),
        source=", ".join(r["name"] for r in refs),
    )


def save_style(game, style):
    import json
    with open(os.path.join(profile_dir(game), "style.json"), "w", encoding="utf-8") as f:
        json.dump(style, f, ensure_ascii=False, indent=1)


def load_style(game):
    import json
    p = os.path.join(profile_dir(game), "style.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    return None


def default_cfg(game="valorant"):
    d = GAMES[game]["defaults"]
    return dict(
        game=game, mode="auto", sens=d["sens"], pre=d["pre"], post=d["post"], maxper=d["maxper"],
        chain=d["chain"], gapcut=True, feed=True, intro=True, ramp=True, gamenorm=True, gametarget=-12.0, velocity=False, shake=False, bestlast=True, skipnokill=True, fitbgm=True, drops=True, minkills=2, learn=True, endmargin=5.0, lead=d["lead"], killpost=d["killpost"], slowmo=False, slowlen=d["slowlen"], slowspeed=d["slowspeed"], flash=False, zoom=False,
        grade=d["grade"], transition=d["transition"], tdur=d["tdur"], W=1920, H=1080, vertical=False, fps=60,
        title="", shuffle=False, encoder="x264",
        bgm="", bgmvol=0.8, gamevol=1.0, bgmstart=0.0, bgmpitch=0, bgmspeed=100, bgmbass=0, bgmreverb=False,
        fadein=0.3, fadeout=0.8,
        beat=False, beatunit=1, bpm=0, beatoffset=0, beatflash=False,
    )
