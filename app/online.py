# -*- coding: utf-8 -*-
"""KILL//MONTAGE オンライン機能
  ・アップデート: 公開リポジトリの version.json を見て、新しければアプリのファイルを入れ替える
  ・学習データの共有: 共有用リポジトリに自分の学習データを上げ、みんなのデータを取り込む

設定は online.json（アプリと同じフォルダ）:
  {"owner": "GitHubのユーザー名", "repo": "kill-montage", "branch": "main",
   "data_repo": "kill-montage-data", "token": "共有用リポジトリだけに使える鍵"}
  token は公開リポジトリには置かない（配る zip にだけ入れる）。
"""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request

import engine as E

BASE = E.BASE_DIR
CONF_PATH = os.path.join(BASE, "online.json")
APP_FILES_KEEP = ("online.json", "profiles", "cache", "bgm_cache", "bin")   # 更新しても消さないもの


def conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            c = json.load(f)
        return c if c.get("owner") and c.get("repo") else None
    except (OSError, ValueError):
        return None


def _req(url, data=None, method=None, token=None, timeout=20):
    h = {"User-Agent": "KILL-MONTAGE", "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read()


def _raw_base(c):
    return c.get("raw_base") or f"https://raw.githubusercontent.com/{c['owner']}/{c['repo']}/{c.get('branch', 'main')}"


def _ver_tuple(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


# ---------------------------------------------------------------- アップデート
def check_update(current):
    """新しい版があれば version.json の中身を返す（無ければ None）"""
    c = conf()
    if not c:
        return None
    info = json.loads(_req(f"{_raw_base(c)}/version.json?nocache={os.urandom(4).hex()}"))
    return info if _ver_tuple(info.get("version", "0")) > _ver_tuple(current) else None


def apply_update(info, log=lambda s: None, progress=lambda p: None):
    """新しい版のファイルを全部ダウンロードして確かめてから、まとめて入れ替える（途中で失敗したら何も変えない）"""
    c = conf()
    tmp = tempfile.mkdtemp(prefix="km_update_")
    try:
        files = info["files"]   # {"engine.py": "sha256", ...}
        for i, (name, sha) in enumerate(files.items()):
            data = _req(f"{_raw_base(c)}/app/{urllib.request.quote(name)}?v={info['version']}", timeout=60)
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"{name} のダウンロードが壊れています。もう一度試してください")
            p = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            progress((i + 1) / len(files))
            log(f"ダウンロード: {name}")
        # 全部そろったので入れ替え（古いファイルは .old に退避しておく）
        backup = os.path.join(BASE, ".old")
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        os.makedirs(backup)
        for name in files:
            dst = os.path.join(BASE, name)
            if os.path.exists(dst):
                os.makedirs(os.path.dirname(os.path.join(backup, name)), exist_ok=True)
                shutil.copy2(dst, os.path.join(backup, name))
        for name in files:
            dst = os.path.join(BASE, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(tmp, name), dst)
        log(f"v{info['version']} に更新しました")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 学習データの共有
def _data_api(c, path=""):
    return f"https://api.github.com/repos/{c['owner']}/{c.get('data_repo', 'kill-montage-data')}/contents/{path}"


def _token(c):
    t = (c or {}).get("token", "")
    return t if t.startswith(("github_pat_", "ghp_")) else ""   # ひな形のままなら無効


def can_share():
    return bool(_token(conf()))


def sync_learning(log=lambda s: None):
    """オンラインで同期: 自分の学習データを上げて、みんなの学習データを取り込む"""
    c = conf()
    tok = _token(c)
    if not tok:
        raise RuntimeError("オンライン共有の設定がありません（作者から配られた最新の zip でセットアップしてください）")
    uid = E.my_uid()
    # 1) 自分の分を上げる
    tmp = os.path.join(tempfile.gettempdir(), f"km_{uid}.kmlearn")
    n = E.export_learning(tmp)
    path = f"learning/{uid}.kmlearn"
    sha = None
    try:
        sha = json.loads(_req(_data_api(c, path), token=tok))["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    with open(tmp, "rb") as f:
        body = {"message": f"learning from {os.environ.get('USERNAME', 'user')} ({n} kills)",
                "content": base64.b64encode(f.read()).decode()}
    if sha:
        body["sha"] = sha
    if n:
        _req(_data_api(c, path), data=body, method="PUT", token=tok)
        log(f"自分の学習データをアップロードしました（{n}キル分）")
    # 2) みんなの分を取り込む
    try:
        items = json.loads(_req(_data_api(c, "learning"), token=tok))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            items = []
        else:
            raise
    got = 0
    for it in items:
        if not it["name"].endswith(".kmlearn") or it["name"].startswith(uid):
            continue
        data = _req(it["download_url"] if not tok else _data_api(c, it["path"]), token=tok)
        if tok:   # API 経由だと base64 で返ってくる
            data = base64.b64decode(json.loads(data)["content"])
        p = os.path.join(tempfile.gettempdir(), it["name"])
        with open(p, "wb") as f:
            f.write(data)
        try:
            _, _, meta = E.import_learning(p)
            got += 1
            log(f"取り込み: {meta.get('name', '?')}（{meta.get('kills', 0)}キル分）")
        except Exception as e:
            log(f"スキップ: {it['name']}（{e}）")
    return n, got, E.bt_info("valorant")
