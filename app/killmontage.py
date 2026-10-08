# -*- coding: utf-8 -*-
"""
KillMontage - たまったクリップを入れるだけで、キルの場面を自動で見つけてキル集にまとめる。
必要: Python 3 + numpy + ffmpeg/ffprobe (PATH上)
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import engine as E
import online

try:
    import winsound
except ImportError:
    winsound = None

# ------------------------------------------------------------------ デザイン
BG, SIDE, PANEL, CARD = "#0A0C10", "#0E1116", "#11151B", "#151A21"
FIELD, LINE, FG, MUTED, DIM = "#1C222B", "#262D38", "#E9ECF1", "#7D8796", "#454E5C"
F_H1 = ("Bahnschrift SemiBold Condensed", 34)
F_H2 = ("Bahnschrift SemiBold Condensed", 18)
F_EN = ("Bahnschrift SemiBold", 9)
F_ENB = ("Bahnschrift SemiBold", 11)
F_JP = ("Yu Gothic UI", 10)
F_JPB = ("Yu Gothic UI Semibold", 10)
F_SM = ("Yu Gothic UI", 9)
F_MONO = ("Cascadia Mono", 9)
SNIP_DIR = tempfile.mkdtemp(prefix="killmontage_snip_")


def mix(c1, c2, t):
    """色を混ぜる（ホバーや光の表現用）"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


def cut_poly(x0, y0, x1, y1, c):
    """左上と右下の角を斜めに切った形"""
    return [x0 + c, y0, x1, y0, x1, y1 - c, x1 - c, y1, x0, y1, x0, y0 + c]


def play_wav(path):
    if winsound:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    else:
        os.startfile(path)


def stop_wav():
    if winsound:
        winsound.PlaySound(None, 0)


def bg_thread(fn, on_done=None, on_err=None, root=None):
    def w():
        try:
            r = fn()
            if on_done:
                root.after(0, lambda: on_done(r))
        except Exception as e:
            msg = str(e)
            if on_err:
                root.after(0, lambda: on_err(msg))
    threading.Thread(target=w, daemon=True).start()


# ======================================================================= 自作部品
class NeoButton(tk.Canvas):
    """角を斜めに切ったボタン。kind: primary(塗り) / ghost(枠) / text(文字だけ)"""

    def __init__(self, parent, text, command=None, kind="ghost", accent="#FF4655", height=34,
                 width=None, font=F_JPB, bg=None, cut=8):
        self._font = tkfont.Font(font=font)
        w = width or self._font.measure(text) + 34
        super().__init__(parent, width=w, height=height, bg=bg or parent["bg"], highlightthickness=0,
                         bd=0, cursor="hand2")
        self.text, self.command, self.kind, self.accent, self.cut = text, command, kind, accent, cut
        self.state, self.hover, self.busy, self.sh = "normal", False, False, 0.0
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<Enter>", lambda _e: self._hov(True))
        self.bind("<Leave>", lambda _e: self._hov(False))
        self.bind("<Button-1>", self._click)
        self.draw()

    def _hov(self, v):
        self.hover = v
        self.draw()

    def _click(self, _e):
        if self.state == "normal" and self.command:
            self.command()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        dis = self.state == "disabled"
        if self.kind == "primary":
            fill = "#2A2F38" if dis else (mix(self.accent, "#FFFFFF", 0.15) if self.hover else self.accent)
            out, fg = fill, (DIM if dis else "#FFFFFF")
        elif self.kind == "text":
            fill = out = self["bg"]
            fg = DIM if dis else (self.accent if self.hover else MUTED)
        else:
            fill = mix(FIELD, self.accent, 0.12) if self.hover and not dis else FIELD
            out = self.accent if self.hover and not dis else LINE
            fg = DIM if dis else (FG if not self.hover else "#FFFFFF")
        if self.busy:   # 処理中: 赤い板の上を光の帯が流れる
            fill, out, fg = mix(self.accent, "#000000", 0.25), self.accent, "#FFFFFF"
        self.create_polygon(cut_poly(1, 1, w - 2, h - 2, self.cut), fill=fill, outline=out, width=1)
        if self.busy:
            bx = -60 + (w + 120) * self.sh
            x0, x1 = max(2, bx), min(w - 2, bx + 40)
            if x1 > x0:
                self.create_polygon(x0, 2, x1, 2, max(2, x1 - 18), h - 2, max(2, x0 - 18), h - 2,
                                    fill=mix(self.accent, "#FFFFFF", 0.25), outline="")
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self._font)

    def set_busy(self, on):
        self.busy = on
        if on:
            self._shimmer()
        self.draw()

    def _shimmer(self):
        if not self.busy or not self.winfo_exists():
            return
        self.sh = (self.sh + 0.025) % 1.0
        self.draw()
        self.after(30, self._shimmer)

    def configure(self, cnf=None, **kw):
        changed = False
        for k in ("text", "state"):
            if k in kw:
                setattr(self, k, kw.pop(k))
                changed = True
        if kw or cnf:
            super().configure(cnf, **kw)
        if changed:
            self.draw()

    config = configure


class Toggle(tk.Frame):
    """ON/OFF スイッチ + 説明"""

    def __init__(self, parent, var, text, hint=None, accent="#FF4655"):
        super().__init__(parent, bg=parent["bg"])
        self.var, self.accent = var, accent
        self.sw = tk.Canvas(self, width=40, height=22, bg=self["bg"], highlightthickness=0, cursor="hand2")
        self.sw.grid(row=0, column=0, rowspan=2 if hint else 1, sticky="n", pady=(2, 0))
        lab = tk.Label(self, text=text, bg=self["bg"], fg=FG, font=F_JP, cursor="hand2", anchor="w", justify="left")
        lab.grid(row=0, column=1, sticky="w", padx=(10, 0))
        ws = [self.sw, lab]
        if hint:
            h = tk.Label(self, text=hint, bg=self["bg"], fg=MUTED, font=F_SM, anchor="w", justify="left")
            h.grid(row=1, column=1, sticky="w", padx=(10, 0))
        for w in ws:
            w.bind("<Button-1>", lambda _e: self.var.set(not self.var.get()))
        tid = var.trace_add("write", lambda *_: self.draw())
        self.sw.bind("<Destroy>", lambda _e: var.trace_remove("write", tid))
        self.draw()

    def draw(self):
        c = self.sw
        c.delete("all")
        on = bool(self.var.get())
        col = self.accent if on else "#2B323D"
        c.create_oval(1, 1, 21, 21, fill=col, outline=col)
        c.create_oval(19, 1, 39, 21, fill=col, outline=col)
        c.create_rectangle(11, 1, 29, 21, fill=col, outline=col)
        x = 29 if on else 11
        c.create_oval(x - 8, 3, x + 8, 19, fill="#FFFFFF" if on else "#8A93A1", outline="")


class Slider(tk.Canvas):
    """細いライン + 丸いつまみのスライダー"""

    def __init__(self, parent, var, lo, hi, res, accent, length=240):
        super().__init__(parent, width=length, height=24, bg=parent["bg"], highlightthickness=0, cursor="hand2")
        self.var, self.lo, self.hi, self.res, self.accent = var, lo, hi, res, accent
        self.drag = False
        self.bind("<Button-1>", self._set)
        self.bind("<B1-Motion>", self._set)
        self.bind("<Configure>", lambda _e: self.draw())
        tid = var.trace_add("write", lambda *_: self.draw())
        self.bind("<Destroy>", lambda _e: var.trace_remove("write", tid))
        self.draw()

    def _set(self, e):
        w = max(1, self.winfo_width() - 16)
        t = min(1.0, max(0.0, (e.x - 8) / w))
        x = round((self.lo + t * (self.hi - self.lo)) / self.res) * self.res
        self.var.set(int(x) if self.res >= 1 else round(x, 2))

    def draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = int(self["width"])
        try:
            v = float(self.var.get())
        except (tk.TclError, ValueError):
            v = self.lo
        t = (min(self.hi, max(self.lo, v)) - self.lo) / (self.hi - self.lo)
        x = 8 + t * (w - 16)
        self.create_line(8, 12, w - 8, 12, fill="#262E3A", width=4, capstyle="round")
        self.create_line(8, 12, x, 12, fill=self.accent, width=4, capstyle="round")
        self.create_oval(x - 8, 4, x + 8, 20, fill="#FFFFFF", outline=self.accent, width=3)


class SongView(tk.Canvas):
    """曲の盛り上がり（音量の流れ）と、見つけた盛り上がりの位置を表示"""

    def __init__(self, parent, accent):
        super().__init__(parent, height=96, bg="#0F1318", highlightthickness=1, highlightbackground=LINE)
        self.accent, self.song = accent, None
        self.bind("<Configure>", lambda _e: self.draw())

    def set(self, song):
        self.song = song
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if not self.song:
            self.create_text(w / 2, h / 2, text="「盛り上がりを解析」を押すと、曲のどこが盛り上がるかを表示します",
                             fill=DIM, font=F_SM)
            return
        c, dur = self.song["curve"], self.song["dur"]
        top, bot = 22, h - 18
        pts = [0, bot]
        for i, v in enumerate(c):
            pts += [i / max(1, len(c) - 1) * w, bot - v * (bot - top)]
        pts += [w, bot]
        self.create_polygon(pts, fill=mix(self.accent, "#0F1318", 0.72), outline="")
        self.create_line(pts[2:-2], fill=mix(self.accent, "#0F1318", 0.25), width=1)
        for m in range(0, int(dur) + 1, 30):  # 30秒ごとの目盛り
            x = m / dur * w
            self.create_text(x + 3, h - 8, text=E.fmt_time(m), fill=DIM, font=("Bahnschrift", 8), anchor="w")
        for t, _ in self.song["drops"]:
            x = t / dur * w
            self.create_line(x, 14, x, bot, fill=self.accent, width=2)
            self.create_polygon(x - 6, 4, x + 6, 4, x, 13, fill=self.accent, outline="")
            self.create_text(x + 8, 9, text=E.fmt_time(t), fill=FG, font=("Bahnschrift SemiBold", 9), anchor="w")


class Bar(tk.Canvas):
    """プログレスバー"""

    def __init__(self, parent, accent):
        super().__init__(parent, height=8, bg=parent["bg"], highlightthickness=0)
        self.accent, self.p = accent, 0.0
        self.bind("<Configure>", lambda _e: self.draw())

    def set(self, p):
        self.p = max(0.0, min(1.0, p))
        self.draw()

    def set_running(self, on):
        self.running, self.off = on, getattr(self, "off", 0)
        if on:
            self._anim()
        self.draw()

    def _anim(self):
        if not getattr(self, "running", False) or not self.winfo_exists():
            return
        self.off = (self.off + 1) % 12
        self.draw()
        self.after(50, self._anim)

    def draw(self):
        self.delete("all")
        w = self.winfo_width()
        self.create_rectangle(0, 1, w, 7, fill="#1A2029", outline="")
        if self.p > 0:
            x = w * self.p
            self.create_rectangle(0, 1, x, 7, fill=self.accent, outline="")
            if getattr(self, "running", False):
                for sx in range(-12 + getattr(self, "off", 0), int(x), 12):
                    self.create_line(sx, 7, sx + 6, 1, fill=mix(self.accent, "#FFFFFF", 0.22), width=3)
            self.create_rectangle(max(0, x - 3), 0, x, 8, fill="#FFFFFF", outline="")


class Section(tk.Frame):
    """カード型のまとまり。英字の見出し + 日本語"""

    def __init__(self, parent, en, jp, accent):
        super().__init__(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=16, pady=(12, 6))
        mk = tk.Canvas(head, width=14, height=14, bg=CARD, highlightthickness=0)
        mk.create_polygon(4, 1, 13, 1, 9, 13, 0, 13, fill=accent, outline="")
        mk.pack(side="left")
        tk.Label(head, text=en, bg=CARD, fg=accent, font=F_EN).pack(side="left", padx=(6, 8))
        tk.Label(head, text=jp, bg=CARD, fg=FG, font=F_JPB).pack(side="left")
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=16, pady=(0, 14))


class ScrollPage(tk.Frame):
    """縦にスクロールできるページ"""

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style="Neo.Vertical.TScrollbar")
        self.inner = tk.Frame(self.cv, bg=BG)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda _e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        if not self.winfo_ismapped():
            return
        x, y = self.winfo_pointerxy()
        w = self.winfo_containing(x, y)
        while w is not None and w is not self:
            if isinstance(w, tk.Listbox):
                return  # リストはリスト自体をスクロール
            w = w.master
        if w is self and self.inner.winfo_height() > self.cv.winfo_height():
            self.cv.yview_scroll(int(-e.delta / 120) * 2, "units")


# ======================================================================= App
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  —  VALORANT Kill Montage Maker")
        self.geometry("1240x880")
        self.minsize(1080, 780)
        self.configure(bg=BG)
        try:
            self.iconbitmap(make_icon())
        except Exception:
            pass
        self.withdraw()   # 起動画面のあとに表示
        self.game = "valorant"
        self.accent = E.GAMES["valorant"]["color"]
        self.clips = []
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._style(self.accent)
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", self.accent)
        self.option_add("*TCombobox*Listbox.font", F_JP)
        self._vars()
        self.start_screen = None
        self.editor = None
        self.show_start()
        Splash(self, self._after_splash)

    def _after_splash(self):
        self.deiconify()
        dark_titlebar(self)
        if online.conf():   # 新しい版があるか裏で確認
            bg_thread(lambda: online.check_update(APP_VER), self._show_update, lambda m: None, self)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            messagebox.showerror("ffmpeg がありません", "セットアップをもう一度実行してください（ffmpeg が見つかりません）")

    # ------------------------------------------------------------ style
    def _style(self, accent):
        self.accent = accent
        s = self.style
        s.configure(".", background=CARD, foreground=FG, fieldbackground=FIELD, bordercolor=LINE,
                    darkcolor=FIELD, lightcolor=FIELD, troughcolor=FIELD, selectbackground=accent,
                    selectforeground="#FFFFFF", insertcolor=FG, arrowcolor=MUTED, font=F_JP)
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            s.configure(w, fieldbackground=FIELD, foreground=FG, background=FIELD, bordercolor=LINE,
                        lightcolor=FIELD, darkcolor=FIELD, padding=(8, 5), arrowsize=13)
            s.map(w, bordercolor=[("focus", accent), ("hover", "#3A4352")],
                  lightcolor=[("focus", accent)], fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", FG)], selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", FG)])
        s.configure("Horizontal.TScale", background=accent, troughcolor="#232A35", bordercolor=CARD,
                    darkcolor=accent, lightcolor=accent, sliderthickness=14, gripcount=0)
        s.map("Horizontal.TScale", background=[("active", mix(accent, "#FFFFFF", 0.25))])
        s.configure("Neo.Vertical.TScrollbar", background="#222A35", troughcolor=BG, bordercolor=BG,
                    arrowcolor=BG, lightcolor="#222A35", darkcolor="#222A35", gripcount=0, arrowsize=0)
        s.map("Neo.Vertical.TScrollbar", background=[("active", accent)])
        self.option_add("*TCombobox*Listbox.selectBackground", accent)

    def _vars(self):
        V = tk.StringVar
        B = tk.BooleanVar
        D = tk.DoubleVar
        self.v = {
            "mode": V(value="自動検出"), "sens": tk.IntVar(value=6),
            "pre": D(value=2.0), "post": D(value=1.2), "maxper": tk.IntVar(value=1), "minkills": tk.IntVar(value=2), "learn": B(value=True), "chain": D(value=15.0),
            "gapcut": B(value=True), "feed": B(value=True), "intro": B(value=True), "bestlast": B(value=True),
            "skipnokill": B(value=True), "fitbgm": B(value=True), "endmargin": D(value=5.0),
            "velocity": B(value=False), "shake": B(value=False), "ramp": B(value=True),
            "lead": D(value=1.0), "killpost": D(value=0.35),
            "slowmo": B(value=False), "slowspeed": V(value="0.5"), "slowlen": D(value=0.6),
            "flash": B(value=False), "zoom": B(value=False),
            "grade": V(value="鮮やか"), "transition": V(value="白フラッシュ"), "tdur": D(value=0.25),
            "aspect": V(value="横 16:9 1080p (YouTube)"), "fps": tk.IntVar(value=60),
            "title": V(value=""), "shuffle": B(value=False), "encoder": V(value="CPU (x264)"),
            "bgm": V(value=""), "bgmpreset": V(value="ノーマル"),
            "bgmvol": D(value=0.8), "gamevol": tk.IntVar(value=100), "gamenorm": B(value=True),
            "bgmstart": D(value=0), "bgmpitch": tk.IntVar(value=0), "bgmspeed": tk.IntVar(value=100),
            "bgmbass": tk.IntVar(value=0), "bgmreverb": B(value=False),
            "fadein": D(value=0.3), "fadeout": D(value=0.8),
            "beat": B(value=False), "drops": B(value=True), "beatunit": V(value="1拍ごと"), "bpm": V(value="自動"),
            "beatoffset": tk.IntVar(value=0), "beatflash": B(value=False),
            "out": V(value=os.path.join(os.path.expanduser("~"), "Videos", "killmontage.mp4")),
        }

    def apply_preset(self, game):
        d = E.GAMES[game]["defaults"]
        for k in ("pre", "post", "lead", "killpost", "maxper", "chain", "sens", "slowlen", "grade", "transition", "tdur"):
            self.v[k].set(d[k])
        self.v["slowspeed"].set(str(d["slowspeed"]))
        st = E.load_style(game)
        if st:  # 参考動画から学んだスタイルがあれば上書き
            self.apply_style(st)

    def apply_style(self, st):
        for k in ("pre", "lead", "killpost", "transition", "beat", "intro"):
            if k in st:
                self.v[k].set(st[k])

    # ------------------------------------------------------------ アップデート
    def _show_update(self, info):
        if not info:
            return
        self.upd_info = info
        bar = tk.Frame(self, bg="#3A0D14")
        bar.place(relx=0, rely=0, relwidth=1, height=46)
        self.upd_bar = bar
        tk.Label(bar, text=f"NEW  v{info['version']}", bg="#3A0D14", fg="#FF4655",
                 font=("Bahnschrift SemiBold", 11)).pack(side="left", padx=(24, 12))
        tk.Label(bar, text="新しい版があります" + (f" — {info['notes']}" if info.get("notes") else ""),
                 bg="#3A0D14", fg=FG, font=F_JP).pack(side="left")
        self.upd_btn = NeoButton(bar, "今すぐ更新", self._do_update, kind="primary", height=32, bg="#3A0D14")
        self.upd_btn.pack(side="right", padx=16)
        NeoButton(bar, "あとで", bar.destroy, kind="text", height=32, bg="#3A0D14").pack(side="right")

    def _do_update(self):
        self.upd_btn.config(state="disabled", text="ダウンロード中…")
        self.upd_btn.set_busy(True)

        def done(_):
            messagebox.showinfo("更新完了", f"v{self.upd_info['version']} に更新しました。再起動します。")
            import subprocess
            subprocess.Popen([sys.executable, os.path.join(E.BASE_DIR, "killmontage.py")], cwd=E.BASE_DIR)
            self.destroy()

        def err(m):
            self.upd_btn.set_busy(False)
            self.upd_btn.config(state="normal", text="今すぐ更新")
            messagebox.showerror("更新できませんでした", m[:400])
        bg_thread(lambda: online.apply_update(self.upd_info), done, err, self)

    # ------------------------------------------------------------ 画面切替
    def show_start(self):
        if self.editor:
            self.editor.destroy()
            self.editor = None
        self.start_screen = StartScreen(self)
        self.start_screen.pack(fill="both", expand=True)

    def choose_game(self, game):
        self.game = game
        self._style(E.GAMES[game]["color"])
        self.apply_preset(game)
        self.start_screen.destroy()
        self.editor = Editor(self)
        self.editor.pack(fill="both", expand=True)

    # ------------------------------------------------------------ cfg
    def cfg(self):
        v = {k: x.get() for k, x in self.v.items()}
        c = E.default_cfg(self.game)
        a = v["aspect"]
        W, H = (1080, 1920) if a.startswith("縦") else (1280, 720) if "720" in a else (1920, 1080)
        bpm = v["bpm"].strip()
        try:
            bpm = float(bpm) if bpm and bpm != "自動" else 0
        except ValueError:
            bpm = 0
        c.update(
            mode={"自動検出": "auto", "クリップの最後": "end", "クリップ全部": "whole"}[v["mode"]],
            sens=int(v["sens"]), pre=float(v["pre"]), post=float(v["post"]), maxper=int(v["maxper"]), minkills=int(v["minkills"]), learn=v["learn"],
            chain=float(v["chain"]), gapcut=v["gapcut"], feed=v["feed"], intro=v["intro"],
            bestlast=v["bestlast"], skipnokill=v["skipnokill"], fitbgm=v["fitbgm"],
            endmargin=float(v["endmargin"]), velocity=v["velocity"], shake=v["shake"], ramp=v["ramp"],
            lead=float(v["lead"]), killpost=float(v["killpost"]),
            slowmo=v["slowmo"], slowspeed=float(v["slowspeed"]),
            slowlen=float(v["slowlen"]), flash=v["flash"], zoom=v["zoom"], grade=v["grade"],
            transition=v["transition"], tdur=float(v["tdur"]), W=W, H=H, vertical=a.startswith("縦"),
            fps=int(v["fps"]), title=v["title"], shuffle=v["shuffle"],
            encoder="nvenc" if "NVENC" in v["encoder"] else "x264",
            bgm=v["bgm"].strip(), bgmvol=float(v["bgmvol"]), gamevol=int(v["gamevol"]) / 100.0,
            gamenorm=v["gamenorm"],
            bgmstart=float(v["bgmstart"]), bgmpitch=int(v["bgmpitch"]), bgmspeed=int(v["bgmspeed"]),
            bgmbass=int(v["bgmbass"]), bgmreverb=v["bgmreverb"], fadein=float(v["fadein"]),
            fadeout=float(v["fadeout"]), beat=v["beat"],
            drops=v["drops"], beatunit={"1拍ごと": 1, "2拍ごと": 2, "1小節ごと(4拍)": 4}[v["beatunit"]],
            bpm=bpm, beatoffset=int(v["beatoffset"]), beatflash=v["beatflash"],
        )
        return c


# ======================================================================= 見た目の仕上げ（アイコン・タイトルバー）
APP_NAME = "KILL//MONTAGE"
APP_VER = "1.0"
ASSET_DIR = os.path.join(E.BASE_DIR, "assets")


def make_icon():
    """アプリのアイコン（.ico）を作る。赤い角切りの板に白い「//」"""
    import struct
    import zlib
    import numpy as np
    os.makedirs(ASSET_DIR, exist_ok=True)
    path = os.path.join(ASSET_DIR, "icon.ico")
    if os.path.exists(path):
        return path
    imgs = []
    for S in (256, 64, 48, 32, 16):
        y, x = np.mgrid[0:S, 0:S].astype(np.float32) / S
        a = np.zeros((S, S), np.float32)
        c = 0.18
        body = (x > 0.06) & (x < 0.94) & (y > 0.06) & (y < 0.94) & (x + y > 0.06 + c + 0.06) & (x + y < 2 - 0.12 - c)
        a[body] = 1
        img = np.zeros((S, S, 4), np.uint8)
        img[body] = (255, 70, 85, 255)
        for off in (-0.13, 0.11):   # 白い斜めの2本線「//」
            d = (x - 0.5 - off) + (y - 0.5) * 0.45
            m = body & (np.abs(d) < 0.055) & (y > 0.2) & (y < 0.8)
            img[m] = (255, 255, 255, 255)
        raw = b"".join(b"\x00" + img[r].tobytes() for r in range(S))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
        imgs.append((S, png))
    head = struct.pack("<HHH", 0, 1, len(imgs))
    off = 6 + 16 * len(imgs)
    ent, data = b"", b""
    for S, png in imgs:
        ent += struct.pack("<BBBBHHII", S % 256, S % 256, 0, 0, 1, 32, len(png), off + len(data))
        data += png
    with open(path, "wb") as f:
        f.write(head + ent + data)
    return path


def dark_titlebar(win, color=BG):
    """Windows のタイトルバーを黒くして、アプリと一体化させる"""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)   # 枠を含むウィンドウ本体の番号
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)   # ダークモード
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        cap = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)  # タイトルバーの色
        txt = ctypes.c_int(0xF1ECE9)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)  # 文字の色
    except Exception:
        pass


def draw_logo(cv, x, y, size, anchor="w", accent="#FF4655"):
    f = tkfont.Font(family="Bahnschrift SemiBold Condensed", size=size)
    parts = [("KILL", FG), ("//", accent), ("MONTAGE", FG)]
    tot = sum(f.measure(t) for t, _ in parts) + 4
    x0 = x - tot / 2 if anchor == "c" else x
    for t, col in parts:
        cv.create_text(x0, y, text=t, fill=col, font=f, anchor="w")
        x0 += f.measure(t) + 2
    return tot


def glow_bg(cv, w, h, cx, cy, col, r, steps=18):
    """ぼんやり光る背景（同心円を重ねて表現）"""
    for k in range(steps, 0, -1):
        t = k / steps
        rr = r * t
        cv.create_oval(cx - rr * 1.6, cy - rr, cx + rr * 1.6, cy + rr, fill=mix(BG, col, (1 - t) * 0.16),
                       outline="")


# ======================================================================= 起動画面
class Splash(tk.Toplevel):
    def __init__(self, root, on_done):
        super().__init__(root)
        self.overrideredirect(True)
        W, H = 620, 340
        x = (self.winfo_screenwidth() - W) // 2
        y = (self.winfo_screenheight() - H) // 2
        self.geometry(f"{W}x{H}+{x}+{y}")
        self.attributes("-topmost", True)
        self.cv = tk.Canvas(self, width=W, height=H, bg=BG, highlightthickness=0)
        self.cv.pack()
        self.W, self.H, self.k, self.on_done = W, H, 0, on_done
        self.tick()

    def tick(self):
        cv, W, H, k = self.cv, self.W, self.H, self.k
        cv.delete("all")
        glow_bg(cv, W, H, W / 2, H / 2 - 10, "#FF4655", 210)
        for xx in range(-H, W, 34):
            cv.create_line(xx, H, xx + H, 0, fill="#0D1015")
        p = min(1.0, k / 22)
        e = 1 - (1 - p) ** 3   # だんだんゆっくり止まる
        draw_logo(cv, W / 2, H / 2 - 18 + (1 - e) * 24, 44, anchor="c")
        cv.create_text(W / 2, H / 2 + 30, text="VALORANT  KILL  MONTAGE  MAKER", fill=mix(BG, MUTED, e),
                       font=("Bahnschrift SemiBold", 10))
        # 赤い光の線が走る
        lx = -120 + (W + 240) * min(1.0, k / 30)
        cv.create_polygon(lx, 0, lx + 30, 0, lx - 60, H, lx - 90, H, fill=mix(BG, "#FF4655", 0.22), outline="")
        bw = 260
        cv.create_rectangle(W / 2 - bw / 2, H - 58, W / 2 + bw / 2, H - 55, fill="#1A2029", outline="")
        cv.create_rectangle(W / 2 - bw / 2, H - 58, W / 2 - bw / 2 + bw * min(1.0, k / 34), H - 55,
                            fill="#FF4655", outline="")
        cv.create_text(W / 2, H - 38, text="LOADING", fill=DIM, font=("Bahnschrift SemiBold", 8))
        cv.create_text(W - 16, H - 14, text=f"v{APP_VER}", fill=DIM, font=("Bahnschrift", 8), anchor="e")
        cv.create_rectangle(0, 0, W, 3, fill="#FF4655", outline="")
        self.k += 1
        if self.k <= 38:
            self.after(30, self.tick)
        else:
            self.destroy()
            self.on_done()


# ======================================================================= ゲーム選択画面
class StartScreen(tk.Canvas):
    """VALORANT を大きく前面に。APEX / その他は BETA として小さく"""

    def __init__(self, app):
        super().__init__(app, bg=BG, highlightthickness=0)
        self.app, self.hover, self.cards, self.anim = app, None, {}, 0
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self._set_hover(None))
        self.bind("<Button-1>", self._click)
        self._loop()

    def _loop(self):   # 背景の光の筋をゆっくり動かす
        if not self.winfo_exists():
            return
        self.anim = (self.anim + 1) % 400
        self.draw()
        self.after(60, self._loop)

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 50:
            return
        acc = "#FF4655"
        glow_bg(self, w, h, w * 0.68, h * 0.45, acc, h * 0.55)
        for x in range(-h, w, 46):
            self.create_line(x, h, x + h, 0, fill="#0D1015")
        sx = -200 + (w + 400) * (self.anim / 400)
        self.create_polygon(sx, 0, sx + 50, 0, sx - 120, h, sx - 170, h, fill=mix(BG, acc, 0.05), outline="")
        self.create_rectangle(0, 0, w, 3, fill=acc, outline="")
        # 上のバー
        draw_logo(self, 40, 40, 18)
        self.create_text(w - 40, 40, text=f"v{APP_VER}", fill=DIM, font=("Bahnschrift SemiBold", 9), anchor="e")
        n, tot = E.bt_info("valorant")
        if n:
            self.create_text(w - 90, 40, text=f"LEARNED {n} BANNERS", fill=MUTED, font=("Bahnschrift SemiBold", 9),
                             anchor="e")
        # 左: 見出し
        lx, ty = 70, h * 0.30
        self.create_text(lx, ty, text="VALORANT", fill=acc, font=("Bahnschrift SemiBold", 12), anchor="w")
        self.create_text(lx - 3, ty + 50, text="KILL MONTAGE", fill=FG, font=("Bahnschrift SemiBold Condensed", 58),
                         anchor="w")
        self.create_text(lx - 3, ty + 112, text="MAKER", fill=FG, font=("Bahnschrift SemiBold Condensed", 58),
                         anchor="w")
        self.create_line(lx, ty + 160, lx + 60, ty + 160, fill=acc, width=3)
        self.create_text(lx, ty + 190, anchor="w", fill=MUTED, font=("Yu Gothic UI", 11),
                         text="たまったクリップを入れるだけ。\nキルを自動で見つけて、連続キルを優先して、BGMに合わせて1本に。")
        for i, (a, b) in enumerate([("FEED + BANNER", "キルフィードとキルバナーの2重チェック"),
                                    ("SELF LEARNING", "使うほどあなたのスキンを覚えて正確に"),
                                    ("BGM FROM LINK", "YouTube / Spotify のリンクでBGM")]):
            yy = ty + 262 + i * 34
            self.create_polygon(lx, yy - 5, lx + 8, yy - 5, lx + 4, yy + 5, lx - 4, yy + 5, fill=acc, outline="")
            self.create_text(lx + 18, yy, text=a, fill=FG, font=("Bahnschrift SemiBold", 10), anchor="w")
            self.create_text(lx + 150, yy, text=b, fill=MUTED, font=F_SM, anchor="w")
        # 右: VALORANT の大きいカード + BETA の2枚
        cw = min(420, w * 0.36)
        cx0 = w - cw - 70
        cy0 = h * 0.18
        ch = min(330, h * 0.46)
        self.cards = {"valorant": (cx0, cy0, cx0 + cw, cy0 + ch)}
        self._hero(cx0, cy0, cw, ch)
        sw = (cw - 16) / 2
        sy = cy0 + ch + 18
        for i, k in enumerate(("apex", "other")):
            x = cx0 + i * (sw + 16)
            self.cards[k] = (x, sy, x + sw, sy + 92)
            self._small(k, x, sy, sw, 92)
        self.create_text(w / 2, h - 22, text="クリップは外部に送信されません  ·  ffmpeg で動作", fill=DIM, font=F_SM)

    def _hero(self, x, y, cw, ch):
        acc, hov = "#FF4655", self.hover == "valorant"
        if hov:
            y -= 4
            self.create_polygon(cut_poly(x - 4, y - 4, x + cw + 4, y + ch + 4, 30), fill="",
                                outline=mix(acc, BG, 0.35), width=2)
        self.create_polygon(cut_poly(x, y, x + cw, y + ch, 28), fill="#141920" if hov else "#11151B",
                            outline=acc if hov else "#2A303B")
        # 赤い斜めの帯
        for i in range(6):
            o = i * 26
            self.create_polygon(x + cw - 170 + o, y, x + cw - 150 + o, y, x + cw - 250 + o, y + ch,
                                x + cw - 270 + o, y + ch, fill=mix("#11151B", acc, 0.05 + 0.03 * i), outline="")
        self.create_polygon(x + 28, y, x + cw, y, x + cw, y + 6, x + 22, y + 6, fill=acc, outline="")
        self.create_text(x + 30, y + 44, text="RECOMMENDED", fill=acc, font=("Bahnschrift SemiBold", 9), anchor="w")
        self.create_text(x + 28, y + 96, text="VALORANT", fill=FG, font=("Bahnschrift SemiBold Condensed", 48),
                         anchor="w")
        self.create_text(x + 30, y + 146, text="キル集メーカー", fill=FG, font=("Yu Gothic UI Semibold", 13),
                         anchor="w")
        self.create_text(x + 30, y + 176, anchor="nw", width=cw - 60, fill=MUTED, font=F_SM,
                         text="キルフィード＋キルバナーで自分のキルだけを検出。\nやられた場面・味方のキルは入りません。")
        bx, by = x + 30, y + ch - 62
        bw = 190
        self.create_polygon(cut_poly(bx, by, bx + bw, by + 40, 10), fill=mix(acc, "#FFFFFF", 0.12) if hov else acc,
                            outline="")
        self.create_text(bx + bw / 2, by + 20, text="START  ▶", fill="#FFFFFF",
                         font=("Bahnschrift SemiBold Condensed", 17))

    def _small(self, k, x, y, cw, ch):
        g = E.GAMES[k]
        hov = self.hover == k
        self.create_polygon(cut_poly(x, y, x + cw, y + ch, 14), fill="#141920" if hov else "#0F1318",
                            outline=g["color"] if hov else "#222833")
        name = "APEX LEGENDS" if k == "apex" else "OTHER GAMES"
        self.create_text(x + 18, y + 30, text=name, fill=FG if hov else MUTED,
                         font=("Bahnschrift SemiBold Condensed", 18), anchor="w")
        self.create_text(x + 18, y + 60, text="検出: キルフィード" if k == "apex" else "検出: キル音の登録",
                         fill=DIM, font=F_SM, anchor="w")
        bx = x + cw - 58
        self.create_polygon(cut_poly(bx, y + 14, bx + 42, y + 32, 4), fill="#2A303B", outline="")
        self.create_text(bx + 21, y + 23, text="BETA", fill=MUTED, font=("Bahnschrift SemiBold", 8))

    def _hit(self, ex, ey):
        for k, (x0, y0, x1, y1) in self.cards.items():
            if x0 <= ex <= x1 and y0 - 6 <= ey <= y1:
                return k
        return None

    def _set_hover(self, k):
        if k != self.hover:
            self.hover = k
            self.configure(cursor="hand2" if k else "")

    def _motion(self, e):
        self._set_hover(self._hit(e.x, e.y))

    def _click(self, e):
        k = self._hit(e.x, e.y)
        if k:
            self.app.choose_game(k)


# ======================================================================= 完成画面
class ResultOverlay(tk.Frame):
    """完成したら表示: 数字のまとめ + 名場面のサムネイル + 再生 / フォルダを開く"""

    def __init__(self, ed, summary, elapsed):
        super().__init__(ed, bg="#07090C")
        self.ed, self.s = ed, summary
        acc = ed.acc
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        box = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=acc)
        box.place(relx=0.5, rely=0.5, anchor="c", width=920, height=500)
        top = tk.Canvas(box, height=110, bg=PANEL, highlightthickness=0)
        top.pack(fill="x")
        top.bind("<Configure>", lambda e: self._head(top, e.width))
        stats = tk.Frame(box, bg=PANEL)
        stats.pack(fill="x", padx=36, pady=(4, 10))
        for en, val in (("DURATION", E.fmt_time(summary["total"])), ("CUTS", str(summary["cuts"])),
                        ("KILLS", str(summary["kills"])), ("FROM CLIPS", str(summary["clips"])),
                        ("RENDER TIME", f"{elapsed // 60}:{elapsed % 60:02d}")):
            c = tk.Frame(stats, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            c.pack(side="left", expand=True, fill="x", padx=5)
            tk.Label(c, text=en, bg=CARD, fg=DIM, font=F_EN).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(c, text=val, bg=CARD, fg=FG, font=("Bahnschrift SemiBold Condensed", 28)).pack(anchor="w",
                                                                                                  padx=12, pady=(0, 8))
        tk.Label(box, text="HIGHLIGHTS", bg=PANEL, fg=acc, font=F_EN).pack(anchor="w", padx=41, pady=(6, 4))
        self.thumbs = tk.Frame(box, bg=PANEL)
        self.thumbs.pack(fill="x", padx=36)
        self.imgs = []
        tk.Label(self.thumbs, text="サムネイルを作成中…", bg=PANEL, fg=DIM, font=F_SM).pack(pady=40)
        btns = tk.Frame(box, bg=PANEL)
        btns.pack(side="bottom", fill="x", padx=41, pady=24)
        NeoButton(btns, "▶  再生する", lambda: os.startfile(summary["out"]), kind="primary", accent=acc,
                  width=170, height=44, bg=PANEL, font=("Yu Gothic UI Semibold", 11)).pack(side="left")
        NeoButton(btns, "フォルダを開く", self._folder, accent=acc, width=150, height=44,
                  bg=PANEL).pack(side="left", padx=10)
        NeoButton(btns, "閉じる", self.destroy, kind="text", accent=acc, width=90, height=44,
                  bg=PANEL).pack(side="right")
        bg_thread(self._make_thumbs, self._show_thumbs, lambda m: None, self)

    def _head(self, cv, w):
        cv.delete("all")
        acc = self.ed.acc
        for i in range(7):   # 右上から流れる斜めの光
            o = i * 22
            cv.create_polygon(w - 420 + o, 0, w - 404 + o, 0, w - 484 + o, 110, w - 500 + o, 110,
                              fill=mix(PANEL, acc, 0.06 + 0.025 * i), outline="")
        cv.create_rectangle(0, 0, w, 3, fill=acc, outline="")
        cv.create_text(41, 46, text="COMPLETE", fill=FG, font=("Bahnschrift SemiBold Condensed", 40), anchor="w")
        cv.create_text(43, 84, text=os.path.basename(self.s["out"]), fill=MUTED, font=F_SM, anchor="w")
        cv.create_polygon(w - 150, 30, w - 41, 30, w - 51, 56, w - 160, 56, fill=acc, outline="")
        cv.create_text(w - 100, 43, text="READY TO POST", fill="#FFFFFF", font=("Bahnschrift SemiBold", 9))

    def _folder(self):
        import subprocess
        subprocess.Popen(["explorer", "/select,", os.path.abspath(self.s["out"])])

    def _make_thumbs(self):
        marks = sorted(self.s["marks"], key=lambda m: -m["nk"])[:6]   # キルの多い名場面から6つ
        marks.sort(key=lambda m: m["t"])
        out = []
        for i, m in enumerate(marks):
            p = os.path.join(SNIP_DIR, f"thumb_{i}.png")
            E.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{m['t']:.2f}", "-i", self.s["out"], "-frames:v", "1",
                   "-vf", "scale=126:71", p])
            out.append((p, m))
        return out

    def _show_thumbs(self, items):
        if not self.winfo_exists():
            return
        for w in self.thumbs.winfo_children():
            w.destroy()
        for p, m in items:
            try:
                img = tk.PhotoImage(file=p)
            except tk.TclError:
                continue
            self.imgs.append(img)
            c = tk.Frame(self.thumbs, bg=PANEL)
            c.pack(side="left", padx=5)
            cv = tk.Canvas(c, width=126, height=71, bg="#000", highlightthickness=1, highlightbackground=LINE)
            cv.pack()
            cv.create_image(0, 0, image=img, anchor="nw")
            tk.Label(c, text=f"{E.fmt_time(m['t'])}  ·  {m['nk']} KILLS", bg=PANEL, fg=MUTED,
                     font=("Bahnschrift SemiBold", 8)).pack(anchor="w", pady=(3, 0))

# ======================================================================= 編集画面
PAGES = [("01", "CLIPS", "クリップ"), ("02", "DETECT", "キル検出・構成"),
         ("03", "FINISH", "仕上げ"), ("04", "MUSIC", "BGM・音はめ")]


class Editor(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app, self.v = app, app.v
        self.acc = app.accent
        self.g = E.GAMES[app.game]
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self._sidebar()
        main = tk.Frame(self, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        self._bottom(main)  # 下のバーは常に見えるように先に固定
        self.head = tk.Frame(main, bg=BG)
        self.head.pack(fill="x", padx=32, pady=(22, 8))
        self.h_num = tk.Label(self.head, bg=BG, fg=self.acc, font=("Bahnschrift SemiBold Condensed", 40))
        self.h_num.pack(side="left")
        ht = tk.Frame(self.head, bg=BG)
        ht.pack(side="left", padx=(14, 0))
        self.h_en = tk.Label(ht, bg=BG, fg=FG, font=F_H1)
        self.h_en.pack(anchor="w")
        self.h_jp = tk.Label(ht, bg=BG, fg=MUTED, font=F_JP)
        self.h_jp.pack(anchor="w")
        chips = tk.Frame(self.head, bg=BG)
        chips.pack(side="right", anchor="n", pady=(8, 0))
        self.chip_clips = self._chip(chips, "CLIPS", "0")
        if app.game == "valorant":
            self.chip_bt = self._chip(chips, "LEARNED", f"{E.bt_info('valorant')[0]}")
        self.stack = tk.Frame(main, bg=BG)
        self.stack.pack(fill="both", expand=True, padx=(32, 18), pady=(0, 8))
        self.pages = []
        for fn in (self.tab_clips, self.tab_detect, self.tab_fx, self.tab_bgm):
            pg = ScrollPage(self.stack)
            fn(pg.inner)
            self.pages.append(pg)
        self.show_page(0)

    def _chip(self, parent, en, val):
        c = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        c.pack(side="left", padx=(8, 0))
        tk.Label(c, text=en, bg=CARD, fg=DIM, font=("Bahnschrift SemiBold", 8)).pack(side="left", padx=(10, 6), pady=6)
        v = tk.Label(c, text=val, bg=CARD, fg=FG, font=("Bahnschrift SemiBold", 11))
        v.pack(side="left", padx=(0, 10))
        return v

    # --------------------------------------------------------- サイドバー
    def _sidebar(self):
        sb = tk.Frame(self, bg=SIDE, width=232)
        sb.grid(row=0, column=0, sticky="ns")
        sb.pack_propagate(False)
        tk.Frame(sb, bg=self.acc, height=3).pack(fill="x")
        logo = tk.Canvas(sb, bg=SIDE, height=70, highlightthickness=0)
        logo.pack(fill="x", padx=22, pady=(18, 0))
        f = tkfont.Font(family="Bahnschrift SemiBold Condensed", size=26)
        x = 0
        for t, c in (("KILL", FG), ("//", self.acc), ("MONTAGE", FG)):
            logo.create_text(x, 24, text=t, fill=c, font=f, anchor="w")
            x += f.measure(t) + 2
        chip_w = tkfont.Font(font=F_ENB).measure(self.g["name"]) + 28
        logo.create_polygon(cut_poly(0, 46, chip_w, 68, 6), fill=self.acc, outline="")
        logo.create_text(chip_w / 2, 57, text=self.g["name"], fill="#FFFFFF", font=F_ENB)


        tk.Label(sb, text="STEPS", bg=SIDE, fg=DIM, font=F_EN).pack(anchor="w", padx=24, pady=(26, 6))
        self.nav = []
        for i, (num, en, jp) in enumerate(PAGES):
            it = tk.Frame(sb, bg=SIDE, cursor="hand2", height=56)
            it.pack(fill="x", pady=1)
            it.pack_propagate(False)
            mark = tk.Frame(it, bg=SIDE, width=4)
            mark.pack(side="left", fill="y")
            n = tk.Label(it, text=num, bg=SIDE, fg=DIM, font=("Bahnschrift SemiBold Condensed", 20), width=3)
            ic = tk.Label(it, text="▣◎✦♫"[i], bg=SIDE, fg=DIM, font=("Segoe UI Symbol", 11))
            ic.pack(side="right", padx=16)
            n.pack(side="left", padx=(14, 0))
            tx = tk.Frame(it, bg=SIDE)
            tx.pack(side="left", padx=4)
            a = tk.Label(tx, text=en, bg=SIDE, fg=MUTED, font=F_ENB, anchor="w")
            a.pack(anchor="w")
            b = tk.Label(tx, text=jp, bg=SIDE, fg=DIM, font=F_SM, anchor="w")
            b.pack(anchor="w")
            ws = [it, mark, n, tx, a, b, ic]
            for w in ws:
                w.bind("<Button-1>", lambda _e, i=i: self.show_page(i))
                w.bind("<Enter>", lambda _e, i=i: self._nav_hover(i, True))
                w.bind("<Leave>", lambda _e, i=i: self._nav_hover(i, False))
            self.nav.append(dict(ws=ws, mark=mark, n=n, a=a, b=b, ic=ic))

        bottom = tk.Frame(sb, bg=SIDE)
        bottom.pack(side="bottom", fill="x", padx=22, pady=20)
        self.tpl_label = tk.Label(bottom, bg=SIDE, fg=DIM, font=F_SM, anchor="w", justify="left", wraplength=190)
        self.tpl_label.pack(anchor="w", pady=(0, 10))
        NeoButton(bottom, "◀  ゲームを変える", self.app.show_start, kind="ghost", accent=self.acc,
                  width=188, bg=SIDE).pack(anchor="w")
        self.refresh_tpl()
        self.cur = 0

    def _nav_paint(self, i, active, hover=False):
        it = self.nav[i]
        bgc = "#161B23" if active else ("#12161C" if hover else SIDE)
        for w in it["ws"]:
            if w is not it["mark"]:
                w.configure(bg=bgc)
        it["mark"].configure(bg=self.acc if active else bgc)
        it["n"].configure(fg=self.acc if active else (MUTED if hover else DIM))
        it["a"].configure(fg=FG if active else MUTED)
        it["b"].configure(fg=MUTED if active else DIM)
        it["ic"].configure(fg=self.acc if active else DIM)

    def _nav_hover(self, i, on):
        if i != self.cur:
            self._nav_paint(i, False, on)

    def show_page(self, i):
        self.cur = i
        for j, pg in enumerate(self.pages):
            pg.pack_forget()
            self._nav_paint(j, j == i)
        self.pages[i].pack(fill="both", expand=True)
        num, en, jp = PAGES[i]
        self.h_num.configure(text=num)
        self.h_en.configure(text=en)
        self.h_jp.configure(text=jp)

    # --------------------------------------------------------- 下のバー
    def _bottom(self, main):
        bar = tk.Frame(main, bg=PANEL)
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, bg=LINE, height=1).pack(fill="x")
        inner = tk.Frame(bar, bg=PANEL)
        inner.pack(fill="x", padx=32, pady=(14, 14))
        inner.columnconfigure(1, weight=1)
        tk.Label(inner, text="OUTPUT", bg=PANEL, fg=DIM, font=F_EN).grid(row=0, column=0, sticky="w")
        out = tk.Frame(inner, bg=PANEL)
        out.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Entry(out, textvariable=self.v["out"]).pack(side="left", fill="x", expand=True)
        NeoButton(out, "変更", self.pick_out, accent=self.acc, bg=PANEL, height=32).pack(side="left", padx=(8, 0))
        self.go = NeoButton(inner, "GENERATE  ▶", self.start, kind="primary", accent=self.acc, width=230,
                            height=52, font=("Bahnschrift SemiBold Condensed", 20), bg=PANEL, cut=12)
        self.go.grid(row=0, column=2, rowspan=2, sticky="e", padx=(24, 0))
        st = tk.Frame(inner, bg=PANEL)
        st.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.status = tk.Label(st, text="READY", bg=PANEL, fg=MUTED, font=F_EN, anchor="w")
        self.status.pack(side="left")
        self.pct = tk.Label(st, text="", bg=PANEL, fg=self.acc, font=F_ENB)
        self.pct.pack(side="right")
        self.pb = Bar(inner, self.acc)
        self.pb.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        self.logbox = tk.Text(inner, height=4, state="disabled", bg="#07090C", fg="#8FA1B5", relief="flat",
                              font=F_MONO, padx=10, pady=6, highlightthickness=1, highlightbackground=LINE)
        self.logbox.grid(row=4, column=0, columnspan=3, sticky="ew")

    def set_progress(self, p):
        self.pb.set(p)
        self.prog = p
        t0 = getattr(self, "t_start", None)
        now = time.time()
        if t0:
            hist = self.__dict__.setdefault("hist", [])
            hist.append((now, p))
            while len(hist) > 2 and now - hist[0][0] > 30:
                hist.pop(0)
        if t0 and 0.02 < p < 1 and now - t0 > 4:
            # 始めてからの平均の速さで残り時間を出す（直近の速さも少しだけ混ぜる）
            rate = p / (now - t0)
            t1, p1 = self.hist[0]
            if now - t1 >= 15 and p > p1:
                rate = rate * 0.75 + (p - p1) / (now - t1) * 0.25
            end = now + (1 - p) / max(rate, 1e-6)
            old = getattr(self, "eta_end", None)
            self.eta_end = end if old is None else old * 0.8 + end * 0.2
        self._tick_eta(once=True)

    def _tick_eta(self, once=False):
        p = getattr(self, "prog", 0)
        t0 = getattr(self, "t_start", None)
        txt = f"{p * 100:.0f}%" if p > 0 else ""
        end = getattr(self, "eta_end", None)
        if t0 and p < 1:
            if end:
                rem = max(0, int(end - time.time()))
                txt += f"   残り 約{rem // 60}分{rem % 60:02d}秒  ·  {time.strftime('%H:%M', time.localtime(end))} ごろ終了"
            else:
                txt += "   残り時間を計算中…"
        self.pct.configure(text=txt)
        if not once and t0 and p < 1:
            self.after(1000, self._tick_eta)

    # --------------------------------------------------------- 共通部品
    def section(self, parent, en, jp, side=None, **pack):
        s = Section(parent, en, jp, self.acc)
        if side:
            s.pack(side=side, fill="both", expand=True, **pack)
        else:
            s.pack(fill="x", pady=(0, 14), **pack)
        return s.body

    def row(self, g, r, label, w, hint=None):
        tk.Label(g, text=label, bg=CARD, fg=FG, font=F_JP, anchor="w").grid(row=r, column=0, sticky="w",
                                                                            padx=(0, 14), pady=5)
        w.grid(row=r, column=1, sticky="w", pady=5)
        if hint:
            tk.Label(g, text=hint, bg=CARD, fg=MUTED, font=F_SM, anchor="w").grid(row=r, column=2, sticky="w",
                                                                                 padx=(12, 0))

    def form(self, parent):
        g = tk.Frame(parent, bg=CARD)
        g.pack(fill="x")
        return g

    def toggle(self, parent, var, text, hint=None):
        t = Toggle(parent, var, text, hint, self.acc)
        t.pack(anchor="w", pady=5)
        return t

    def btn(self, parent, text, cmd, kind="ghost", **kw):
        return NeoButton(parent, text, cmd, kind=kind, accent=self.acc, bg=parent["bg"], **kw)

    def hint(self, parent, text, **pack):
        tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=F_SM, anchor="w", justify="left",
                 wraplength=760).pack(anchor="w", **pack)

    def slider(self, parent, label, var, lo, hi, res, unit="", width=240):
        f = tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=4)
        tk.Label(f, text=label, bg=CARD, fg=FG, font=F_JP, width=12, anchor="w").pack(side="left")
        val = tk.Label(f, width=6, anchor="e", bg=CARD, fg=self.acc, font=F_ENB)

        def fmt(x):
            return (f"{int(x):+d}" if lo < 0 else f"{int(x)}") if res >= 1 else f"{x:.2f}"

        Slider(f, var, lo, hi, res, self.acc, width).pack(side="left", padx=(0, 4))
        val.pack(side="left")
        tid = var.trace_add("write", lambda *_: val.config(text=fmt(float(var.get()))))
        val.bind("<Destroy>", lambda _e: var.trace_remove("write", tid))  # 画面を閉じたら監視も外す
        val.config(text=fmt(float(var.get())))
        if unit:
            tk.Label(f, text=unit, bg=CARD, fg=MUTED, font=F_SM).pack(side="left", padx=4)
        return f

    def combo(self, p, var, values, width=18):
        return ttk.Combobox(p, textvariable=var, values=values, state="readonly", width=width)

    def spin(self, p, var, lo, hi, inc=1.0, width=7):
        return ttk.Spinbox(p, from_=lo, to=hi, increment=inc, textvariable=var, width=width)

    # --------------------------------------------------------- 01 クリップ
    def tab_clips(self, f):
        src = self.section(f, "SOURCE", "素材を入れる")
        drop = tk.Frame(src, bg="#11161D", highlightthickness=1, highlightbackground="#2A3240")
        drop.pack(fill="x", pady=(2, 0))
        dc = tk.Frame(drop, bg="#11161D")
        dc.pack(pady=22)
        tk.Label(dc, text="録画やクリップのフォルダを選ぶだけ", bg="#11161D", fg=FG,
                 font=("Yu Gothic UI Semibold", 12)).pack()
        tk.Label(dc, text="サブフォルダも全部読み込んで、撮った順に並べます。キルが無いクリップは自動で外れます。",
                 bg="#11161D", fg=MUTED, font=F_SM).pack(pady=(2, 14))
        bb = tk.Frame(dc, bg="#11161D")
        bb.pack()
        NeoButton(bb, "＋  フォルダを追加", self.add_folder, kind="primary", accent=self.acc, height=40,
                  bg="#11161D").pack(side="left")
        NeoButton(bb, "＋  ファイルを追加", self.add_files, accent=self.acc, height=40,
                  bg="#11161D").pack(side="left", padx=(10, 0))

        q = Section(f, "QUEUE", "クリップ一覧", self.acc)
        q.pack(fill="both", expand=True, pady=(0, 14))
        top = tk.Frame(q.body, bg=CARD)
        top.pack(fill="x", pady=(0, 8))
        self.count = tk.Label(top, bg=CARD, fg=self.acc, font=("Bahnschrift SemiBold Condensed", 22))
        self.count.pack(side="left")
        tk.Label(top, text="一番上のクリップが導入になります", bg=CARD, fg=MUTED, font=F_SM).pack(side="left", padx=12)
        for t, c in [("全部クリア", self.clear), ("削除", self.remove), ("↓", lambda: self.move(1)),
                     ("↑", lambda: self.move(-1))]:
            self.btn(top, t, c, height=30).pack(side="right", padx=(6, 0))
        lf = tk.Frame(q.body, bg=FIELD, highlightthickness=1, highlightbackground=LINE)
        lf.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lf, selectmode="extended", bg=FIELD, fg=FG, relief="flat", highlightthickness=0,
                             selectbackground=mix(self.acc, FIELD, 0.35), selectforeground="#FFFFFF",
                             font=F_JP, activestyle="none", height=12, bd=0)
        sb = ttk.Scrollbar(lf, command=self.lb.yview, style="Neo.Vertical.TScrollbar")
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        sb.pack(side="right", fill="y")
        tb = tk.Frame(q.body, bg=CARD)
        tb.pack(fill="x", pady=(10, 0))
        self.btn(tb, "検出テスト", self.test_detect, height=30).pack(side="left")
        tk.Label(tb, text="選んだクリップで、キルがどこに見つかるかを下のログに出します", bg=CARD, fg=MUTED,
                 font=F_SM).pack(side="left", padx=10)
        self.refresh()

    def refresh(self):
        self.lb.delete(0, "end")
        for i, c in enumerate(self.app.clips):
            mark = "INTRO  " if i == 0 and self.v["intro"].get() else f"{i + 1:>3}    "
            self.lb.insert("end", f"  {mark}{os.path.basename(c)}")
        if self.app.clips and self.v["intro"].get():
            self.lb.itemconfig(0, fg=self.acc)
        self.count.configure(text=f"{len(self.app.clips)} CLIPS")
        if hasattr(self, "chip_clips"):
            self.chip_clips.configure(text=str(len(self.app.clips)))

    def add_files(self):
        fs = filedialog.askopenfilenames(title="クリップを選択",
                                         filetypes=[("動画", " ".join("*" + e for e in E.VIDEO_EXT)), ("すべて", "*.*")])
        self.app.clips += [f for f in fs if f not in self.app.clips]
        self.refresh()

    def add_folder(self):
        d = filedialog.askdirectory(title="クリップのフォルダを選択")
        if d:
            # サブフォルダも含めて、撮った順（更新日時順）に並べる
            fs = [os.path.join(r, f) for r, _, names in os.walk(d) for f in names if f.lower().endswith(E.VIDEO_EXT)]
            fs.sort(key=os.path.getmtime)
            self.app.clips += [f for f in fs if f not in self.app.clips]
            self.refresh()

    def move(self, dlt):
        sel = list(self.lb.curselection())
        cl = self.app.clips
        if len(sel) == 1 and 0 <= sel[0] + dlt < len(cl):
            i, j = sel[0], sel[0] + dlt
            cl[i], cl[j] = cl[j], cl[i]
            self.refresh()
            self.lb.selection_set(j)

    def remove(self):
        for i in reversed(self.lb.curselection()):
            del self.app.clips[i]
        self.refresh()

    def clear(self):
        self.app.clips.clear()
        self.refresh()

    def selected_clip(self):
        sel = self.lb.curselection()
        if sel:
            return self.app.clips[sel[0]]
        return self.app.clips[0] if self.app.clips else None

    def test_detect(self):
        clip = self.selected_clip()
        if not clip:
            return messagebox.showinfo("検出テスト", "クリップを追加してください")
        cfg = self.app.cfg()
        cfg["templates"] = E.load_templates(self.app.game)
        self.log(f"検出テスト: {os.path.basename(clip)}")

        def done(r):
            peaks, dur, _, how = r
            if not peaks:
                self.log(f"   見つかりませんでした [{how}]")
            for t, s in peaks:
                self.log(f"   キル {t:6.2f}秒  [{how}]")
        bg_thread(lambda: E.detect_kills(clip, cfg), done, lambda m: self.log("エラー: " + m), self)

    # --------------------------------------------------------- 02 キル検出・構成
    def tab_detect(self, f):
        b = self.section(f, "STRUCTURE", "構成")
        if self.app.game in ("valorant", "apex"):
            self.toggle(b, self.v["feed"], "キルフィード（右上）から自分のキルを読み取る",
                        "音より正確。銃声・スキル音・BGMに惑わされません（おすすめ）")
        self.toggle(b, self.v["intro"], "1個目のクリップは導入として切らずに見せる",
                    "20秒以内なら丸ごと、長いクリップなら最初のキルから約20秒")
        self.toggle(b, self.v["ramp"], "だんだんテンポを上げる", "序盤はゆったり → 後半ほどキルの間を詰める")
        self.toggle(b, self.v["bestlast"], "締めは一番キルの多いカット")
        self.toggle(b, self.v["skipnokill"], "キルが見つからないクリップは使わない")
        if self.app.game == "valorant":
            self.toggle(b, self.v["learn"], "作るたびに、使ったクリップからキルバナーを学習する",
                        "あなたのスキンのバナーを覚えて、次からさらに正確に検出します")
            lb = tk.Frame(b, bg=CARD)
            lb.pack(fill="x", padx=(50, 0))
            self.bt_label = tk.Label(lb, bg=CARD, fg=self.acc, font=F_SM)
            self.bt_label.pack(side="left")
            self.btn(lb, "学習をリセット", self.reset_bt, height=26, font=F_SM).pack(side="left", padx=10)
            self.refresh_bt()
        lr = tk.Frame(b, bg=CARD)
        lr.pack(fill="x", pady=(10, 0))
        self.learn_btn = self.btn(lr, "参考動画から学ぶ…", self.learn)
        self.learn_btn.pack(side="left")
        self.style_label = tk.Label(lr, bg=CARD, fg=MUTED, font=F_SM)
        self.style_label.pack(side="left", padx=12)
        st_ = E.load_style(self.app.game)
        self.style_label.config(text=f"学習済み: {st_['source']}" if st_ else
                                "好きなキル集を選ぶと、カットのリズムやつなぎ方を真似します")

        if self.app.game == "valorant":
            b = self.section(f, "SHARE", "学習データを友達と共有")
            self.hint(b, "覚えたキルバナーを1つのファイルにして友達に渡せます。友達のファイルを読み込むと、"
                         "お互いの学習が合わさって検出がもっと正確になります（同じ人のデータは何度読み込んでも二重になりません）")
            r = tk.Frame(b, bg=CARD)
            r.pack(fill="x", pady=(8, 0))
            self.btn(r, "⇪  自分の学習データを書き出す", self.export_learn, kind="primary", height=34).pack(side="left")
            self.btn(r, "⇩  友達のデータを読み込む", self.import_learn, height=34).pack(side="left", padx=8)
            if online.can_share():
                self.sync_btn = self.btn(r, "⟳  オンラインで同期", self.sync_online, kind="primary", height=34)
                self.sync_btn.pack(side="left", padx=(8, 0))
            self.friend_label = tk.Label(b, bg=CARD, fg=MUTED, font=F_SM, anchor="w", justify="left")
            self.friend_label.pack(anchor="w", pady=(8, 0))
            self.refresh_friends()

        cols = tk.Frame(f, bg=BG)
        cols.pack(fill="x", pady=(0, 14))
        b = self.section(cols, "JUMP CUT", "キルの間を詰める", side="left", padx=(0, 7))
        self.toggle(b, self.v["gapcut"], "キルとキルの間の待ち時間・移動をカット")
        g = self.form(b)
        self.row(g, 0, "各キルの前 (秒)", self.spin(g, self.v["lead"], 0.3, 5, 0.1))
        self.row(g, 1, "各キルの後 (秒)", self.spin(g, self.v["killpost"], 0.1, 3, 0.05))
        self.row(g, 2, "最初のキル前 (秒)", self.spin(g, self.v["pre"], 0.3, 15, 0.1))
        self.row(g, 3, "最後のキル後 (秒)", self.spin(g, self.v["post"], 0.2, 10, 0.1))
        self.hint(b, "短いほどテンポが速くなります", pady=(4, 0))

        b = self.section(cols, "DETECTION", "キルの見つけ方", side="left", padx=(7, 0))
        g = self.form(b)
        self.row(g, 0, "見つけ方", self.combo(g, self.v["mode"], ["自動検出", "クリップの最後", "クリップ全部"], 14))
        self.row(g, 1, "連続キル判定 (秒)", self.spin(g, self.v["chain"], 1, 40, 1))
        self.row(g, 2, "1つの動画から使う数", self.spin(g, self.v["maxper"], 1, 50))
        self.row(g, 3, "1カットの最低キル数", self.spin(g, self.v["minkills"], 1, 5))
        self.row(g, 4, "検出感度 (音のとき)", self.spin(g, self.v["sens"], 1, 10))
        self.hint(b, "連続キル判定の秒数以内のキルは1カットにまとめます。\nキル数が多いほど・短い時間に連続で倒すほど優先して選びます（3秒で3キルなど）", pady=(4, 0))

        b = self.section(f, "KILL SOUND", "キル音登録（音で検出するとき用）")
        r = tk.Frame(b, bg=CARD)
        r.pack(fill="x")
        self.btn(r, "クリップから登録…", self.open_register).pack(side="left")
        self.btn(r, "登録を全部削除", self.clear_tpl).pack(side="left", padx=8)
        tk.Label(r, text="キル音を登録すると銃声と区別しやすくなります（その他のゲーム向け）", bg=CARD,
                 fg=MUTED, font=F_SM).pack(side="left", padx=6)

    def learn(self):
        fs = filedialog.askopenfilenames(title="参考にするキル集を選択（複数OK）",
                                         filetypes=[("動画", " ".join("*" + e for e in E.VIDEO_EXT))])
        if not fs:
            return
        self.learn_btn.config(state="disabled")
        self.style_label.config(text="分析中…（動画の長さによって数分かかります）")
        game = self.app.game

        def job():
            refs = []
            for p in fs:
                self.log(f"参考動画を分析中: {os.path.basename(p)}")
                refs.append(E.analyze_reference(p))
            return refs, E.style_from_refs(refs)

        def done(r):
            refs, st = r
            self.learn_btn.config(state="normal")
            for x in refs:
                bs = (f"拍に合ったカット {x['beat_ratio'] * 100:.0f}%（偶然なら{x['beat_base'] * 100:.0f}%）"
                      if x["beat_ratio"] is not None else "音はめ判定なし")
                self.log(f"  {x['name']}: {x['dur']:.0f}秒 / カット{x['cuts']}回 / ショット中央値"
                         f"{x['shot_median']:.1f}秒 / 白フラッシュ{x['flashes']}回 / 最初のカットまで"
                         f"{x['first_cut']:.1f}秒 / {bs}")
            msg = (f"分析結果から次の設定にします:\n\n"
                   f"・最初のキル前 {st['pre']}秒 / 各キルの前 {st['lead']}秒 / 各キルの後 {st['killpost']}秒\n"
                   f"・トランジション: {st['transition']}\n"
                   f"・音はめ: {'する' if st['beat'] else 'しない'}\n"
                   f"・1個目を導入にする: {'はい' if st['intro'] else 'いいえ'}\n\n適用しますか？")
            if messagebox.askyesno("参考動画から学ぶ", msg):
                E.save_style(game, st)
                self.app.apply_style(st)
                self.style_label.config(text=f"学習済み: {st['source']}")
                self.log("学習したスタイルを適用しました（次回このゲームを選んだときも自動で適用）")
            else:
                self.style_label.config(text="適用しませんでした")

        def err(m):
            self.learn_btn.config(state="normal")
            self.style_label.config(text="分析に失敗: " + m[:80])
        bg_thread(job, done, err, self)

    def refresh_friends(self):
        fr = E.friends("valorant")
        if fr:
            names = "、".join(f"{x.get('name', '?')}（{x.get('kills', 0)}キル分）" for x in fr)
            self.friend_label.config(text=f"読み込んだ友達: {names}")
        else:
            self.friend_label.config(text="まだ友達のデータはありません")

    def sync_online(self):
        self.sync_btn.config(state="disabled", text="同期中…")
        self.sync_btn.set_busy(True)

        def done(r):
            up, got, (n, tot) = r
            self.sync_btn.set_busy(False)
            self.sync_btn.config(state="normal", text="⟳  オンラインで同期")
            self.refresh_friends()
            if hasattr(self, "bt_label"):
                self.refresh_bt()
            self.log(f"オンライン同期: 自分の{up}キル分をアップロード / {got}人分を取り込み → 覚えたバナー {n}種類（{tot}キル分）")

        def err(m):
            self.sync_btn.set_busy(False)
            self.sync_btn.config(state="normal", text="⟳  オンラインで同期")
            messagebox.showerror("同期できませんでした", m[:400])
        bg_thread(lambda: online.sync_learning(self.log), done, err, self)

    def export_learn(self):
        p = filedialog.asksaveasfilename(title="学習データを書き出す", defaultextension=".kmlearn",
                                         initialfile=f"{os.environ.get('USERNAME', 'me')}.kmlearn",
                                         filetypes=[("KILL//MONTAGE 学習データ", "*.kmlearn")])
        if p:
            n = E.export_learning(p)
            self.log(f"学習データを書き出しました（{n}キル分）: {p}")
            messagebox.showinfo("書き出し完了", f"{os.path.basename(p)} を作りました（{n}キル分）。\nこのファイルを友達に送ってください。")

    def import_learn(self):
        ps = filedialog.askopenfilenames(title="友達の学習データを読み込む",
                                         filetypes=[("KILL//MONTAGE 学習データ", "*.kmlearn")])
        for p in ps:
            try:
                (b0, b1), (a0, a1), meta = E.import_learning(p)
                self.log(f"{meta.get('name', '友達')} の学習データを読み込みました: キルバナー {b0}→{a0}種類（{b1}→{a1}キル分）")
            except Exception as e:
                messagebox.showerror("読み込めませんでした", f"{os.path.basename(p)}: {e}")
        self.refresh_friends()
        if hasattr(self, "bt_label"):
            self.refresh_bt()

    def refresh_bt(self):
        n, tot = E.bt_info("valorant")
        self.bt_label.config(text=f"覚えたキルバナー: {n}種類（{tot}キル分）" if n else "まだ学習していません")

    def reset_bt(self):
        if messagebox.askyesno("確認", "覚えたキルバナーを全部忘れますか？"):
            E.bt_reset("valorant")
            self.refresh_bt()

    def refresh_tpl(self):
        n = len(E.list_templates(self.app.game))
        feed = self.app.game in ("valorant", "apex")
        self.tpl_label.config(text=("検出: キルフィード" if feed else "検出: 音") +
                              (f"\n登録キル音 {n}個" if n else ""))

    def clear_tpl(self):
        if messagebox.askyesno("確認", "このゲームの登録キル音を全部削除しますか？"):
            for p in E.list_templates(self.app.game):
                E.delete_template(p)
            self.refresh_tpl()

    def open_register(self):
        RegisterDialog(self)

    # --------------------------------------------------------- 03 仕上げ
    def tab_fx(self, f):
        cols = tk.Frame(f, bg=BG)
        cols.pack(fill="x", pady=(0, 14))
        b = self.section(cols, "LOOK", "見た目", side="left", padx=(0, 7))
        g = self.form(b)
        self.row(g, 0, "色補正", self.combo(g, self.v["grade"], list(E.GRADES), 14))
        self.row(g, 1, "トランジション", self.combo(g, self.v["transition"], list(E.TRANSITIONS), 14))
        self.row(g, 2, "トランジションの長さ", self.spin(g, self.v["tdur"], 0.1, 1.0, 0.05))
        self.row(g, 3, "タイトル", ttk.Entry(g, textvariable=self.v["title"], width=20))
        self.hint(b, "タイトルは空なら無し", pady=(2, 6))
        self.toggle(b, self.v["shuffle"], "カットの順番をシャッフル")

        b = self.section(cols, "EXPORT", "書き出し形式", side="left", padx=(7, 0))
        g = self.form(b)
        self.row(g, 0, "サイズ", self.combo(g, self.v["aspect"],
                 ["横 16:9 1080p (YouTube)", "横 16:9 720p", "縦 9:16 (TikTok/Shorts)"], 22))
        self.row(g, 1, "FPS", self.combo(g, self.v["fps"], [30, 60], 6))
        self.row(g, 2, "エンコーダ", self.combo(g, self.v["encoder"], ["CPU (x264)", "NVIDIA (NVENC)"], 16))
        self.hint(b, "NVIDIA のグラボがあるなら NVENC が速い", pady=(4, 0))

        b = self.section(f, "GAME AUDIO", "ゲーム音")
        self.slider(b, "ゲーム音量", self.v["gamevol"], 0, 300, 5, "%", 320)
        self.toggle(b, self.v["gamenorm"], "クリップごとの音量差をそろえて大きくする",
                    "小さいクリップも聞こえるように。音割れしないよう最後にリミッターがかかります")

    # --------------------------------------------------------- 04 BGM・音はめ
    def tab_bgm(self, f):
        b = self.section(f, "TRACK", "BGM")
        a = tk.Frame(b, bg=CARD)
        a.pack(fill="x")
        ttk.Entry(a, textvariable=self.v["bgm"]).pack(side="left", fill="x", expand=True)
        self.btn(a, "選択", self.pick_bgm, kind="primary", height=32).pack(side="left", padx=(8, 0))
        self.btn(a, "なし", lambda: self.v["bgm"].set(""), height=32).pack(side="left", padx=(6, 0))
        self.hint(b, "ファイルの代わりに YouTube / Spotify の曲のリンクを貼ってもOK（Spotify は同じ曲を YouTube から探します）",
                  pady=(4, 0))
        fit = tk.Frame(b, bg=CARD)
        fit.pack(fill="x", pady=(8, 0))
        self.toggle(fit, self.v["fitbgm"], "BGMの長さに合わせる",
                    "入りきらない時は、質の高いカット（連続キル）を優先して選びます")
        m = tk.Frame(fit, bg=CARD)
        m.pack(anchor="w", padx=(50, 0))
        tk.Label(m, text="BGMが終わる", bg=CARD, fg=FG, font=F_JP).pack(side="left")
        self.spin(m, self.v["endmargin"], 0, 30, 1, 4).pack(side="left", padx=6)
        tk.Label(m, text="秒前に終わる", bg=CARD, fg=FG, font=F_JP).pack(side="left")

        b = self.section(f, "SONG MAP", "曲の盛り上がり")
        self.songview = SongView(b, self.acc)
        self.songview.pack(fill="x")
        sr = tk.Frame(b, bg=CARD)
        sr.pack(fill="x", pady=(8, 0))
        self.song_btn = self.btn(sr, "盛り上がりを解析", self.analyze_song, height=30)
        self.song_btn.pack(side="left")
        self.song_label = tk.Label(sr, text="BGMを選んで押すと、BPMと盛り上がりの位置がわかります（加工後の曲で解析）",
                                   bg=CARD, fg=MUTED, font=F_SM)
        self.song_label.pack(side="left", padx=10)

        cols = tk.Frame(f, bg=BG)
        cols.pack(fill="x", pady=(0, 14))
        left = self.section(cols, "SOUND", "BGM加工", side="left", padx=(0, 7))
        right = self.section(cols, "BEAT SYNC", "音はめ", side="left", padx=(7, 0))

        p = tk.Frame(left, bg=CARD)
        p.pack(fill="x", pady=(0, 4))
        tk.Label(p, text="プリセット", bg=CARD, fg=FG, font=F_JP, width=12, anchor="w").pack(side="left")
        cb = self.combo(p, self.v["bgmpreset"], list(E.BGM_PRESETS), 20)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self.apply_bgm_preset)
        self.slider(left, "ピッチ", self.v["bgmpitch"], -12, 12, 1, "半音", 190)
        self.slider(left, "速度", self.v["bgmspeed"], 50, 200, 5, "%", 190)
        self.slider(left, "低音ブースト", self.v["bgmbass"], 0, 15, 1, "dB", 190)
        self.slider(left, "BGM音量", self.v["bgmvol"], 0, 1.5, 0.05, "", 190)
        self.toggle(left, self.v["bgmreverb"], "リバーブ（残響）")
        g = self.form(left)
        self.row(g, 0, "開始位置 (秒)", self.spin(g, self.v["bgmstart"], 0, 900, 1, 6))
        self.row(g, 1, "フェードイン / アウト", self._pair(g, self.v["fadein"], self.v["fadeout"]))
        q3 = tk.Frame(left, bg=CARD)
        q3.pack(fill="x", pady=(10, 0))
        self.prev_btn = self.btn(q3, "▶  試聴 20秒", self.preview_bgm, width=130)
        self.prev_btn.pack(side="left")
        self.btn(q3, "■  停止", stop_wav).pack(side="left", padx=8)

        self.toggle(right, self.v["beat"], "音はめする", "キルの瞬間が拍に合い、カットの切り替えも拍の頭に来ます")
        self.toggle(right, self.v["drops"], "盛り上がりにいいキルを合わせる",
                    "曲が静かになってから一気に盛り上がる瞬間（サビの入り・ドロップ）に、連続キルの決めの1キルを合わせます")
        g = self.form(right)
        self.row(g, 0, "切り替え", self.combo(g, self.v["beatunit"], ["1拍ごと", "2拍ごと", "1小節ごと(4拍)"], 14))
        bp = tk.Frame(g, bg=CARD)
        ttk.Combobox(bp, textvariable=self.v["bpm"], values=["自動"], width=7).pack(side="left")
        self.btn(bp, "BPM検出", self.detect_bpm, height=30).pack(side="left", padx=6)
        self.row(g, 1, "BPM", bp)
        self.bpm_label = tk.Label(g, text="", bg=CARD, fg=self.acc, font=F_SM)
        self.bpm_label.grid(row=2, column=1, sticky="w")
        self.row(g, 3, "タイミング補正 (ms)", self.spin(g, self.v["beatoffset"], -500, 500, 10, 6))


    def analyze_song(self):
        c = self._bgm_ok()
        if not c:
            return
        self.song_btn.config(state="disabled", text="解析中…")
        out = os.path.join(SNIP_DIR, "bgm_song.wav")

        def job():
            self._resolve(c)
            dur, _ = E.probe(c["bgm"])
            real = max(20.0, (dur - c["bgmstart"]) / (c["bgmspeed"] / 100.0))
            E.process_bgm(c, out, real)
            return E.analyze_song(out, c["bpm"] or None)

        def done(song):
            self.song_btn.config(state="normal", text="盛り上がりを解析")
            self.songview.set(song)
            self.v["bpm"].set(f"{song['bpm']:.1f}")
            ds = " / ".join(E.fmt_time(t) for t, _ in song["drops"]) or "見つかりませんでした"
            self.song_label.config(text=f"BPM {song['bpm']:.1f}  ·  盛り上がり: {ds}")

        def err(m):
            self.song_btn.config(state="normal", text="盛り上がりを解析")
            self.song_label.config(text="失敗: " + m[:80])
        bg_thread(job, done, err, self)

    def _pair(self, g, a, b):
        f = tk.Frame(g, bg=CARD)
        self.spin(f, a, 0, 5, 0.1, 4).pack(side="left")
        tk.Label(f, text=" / ", bg=CARD, fg=MUTED, font=F_JP).pack(side="left")
        self.spin(f, b, 0.1, 5, 0.1, 4).pack(side="left")
        return f

    def apply_bgm_preset(self, _=None):
        p, s, b, r = E.BGM_PRESETS[self.v["bgmpreset"].get()]
        self.v["bgmpitch"].set(p)
        self.v["bgmspeed"].set(s)
        self.v["bgmbass"].set(b)
        self.v["bgmreverb"].set(r)

    def pick_bgm(self):
        f = filedialog.askopenfilename(title="BGMを選択", filetypes=[
            ("音楽", " ".join("*" + e for e in E.AUDIO_EXT + E.VIDEO_EXT)), ("すべて", "*.*")])
        if f:
            self.v["bgm"].set(f)

    def _bgm_ok(self):
        c = self.app.cfg()
        if not c["bgm"] or not (E.is_url(c["bgm"]) or os.path.exists(c["bgm"])):
            messagebox.showinfo("BGM", "BGMファイルを選ぶか、YouTube / Spotify のリンクを貼ってください")
            return None
        return c

    def _resolve(self, c):
        """（裏で動く処理の中で）リンクなら音声を取ってきてファイルの場所にする"""
        if E.is_url(c["bgm"]):
            c["bgm"] = E.resolve_bgm(c["bgm"], self.log)
        return c

    def preview_bgm(self):
        c = self._bgm_ok()
        if not c:
            return
        out = os.path.join(SNIP_DIR, "bgm_preview.wav")
        stop_wav()
        self.prev_btn.config(state="disabled", text="加工中…")

        def fin(_=None):
            self.prev_btn.config(state="normal", text="▶  試聴 20秒")

        def done(_):
            fin()
            play_wav(out)

        def err(m):
            fin()
            messagebox.showerror("エラー", m[:500])
        bg_thread(lambda: E.process_bgm(self._resolve(c), out, 20), done, err, self)

    def detect_bpm(self):
        c = self._bgm_ok()
        if not c:
            return
        self.bpm_label.config(text="検出中…")
        out = os.path.join(SNIP_DIR, "bgm_bpm.wav")

        def job():
            E.process_bgm(self._resolve(c), out, 90)
            return E.detect_beats(out)

        def done(r):
            bpm, b0 = r
            self.v["bpm"].set(f"{bpm:.1f}")
            self.bpm_label.config(text=f"検出: {bpm:.1f} BPM（加工後の曲）")
        bg_thread(job, done, lambda m: self.bpm_label.config(text="失敗: " + m[:60]), self)

    # --------------------------------------------------------- 書き出し
    def pick_out(self):
        f = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")],
                                         initialfile=os.path.basename(self.v["out"].get()))
        if f:
            self.v["out"].set(f)

    def log(self, s):
        def f():
            self.logbox.config(state="normal")
            self.logbox.insert("end", s + "\n")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
            self.status.configure(text=s.strip()[:90])
        self.after(0, f)

    def start(self):
        if not self.app.clips:
            self.show_page(0)
            return messagebox.showwarning("クリップなし", "01 CLIPS でクリップを追加してください")
        cfg = self.app.cfg()
        if cfg["bgm"] and not E.is_url(cfg["bgm"]) and not os.path.exists(cfg["bgm"]):
            return messagebox.showwarning("BGM", "BGMファイルが見つかりません")
        if cfg["beat"] and not cfg["bgm"] and not cfg["bpm"]:
            if not messagebox.askyesno("音はめ", "BGMが無いので音はめはオフになります。続けますか？"):
                return
        out = self.v["out"].get()
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        self.go.config(state="disabled", text="RENDERING")
        self.go.set_busy(True)
        self.pb.set_running(True)
        self.t_start, self.eta_end, self.hist = time.time(), None, []
        self.set_progress(0)
        self._tick_eta()
        clips = list(self.app.clips)

        def work():
            return E.build(clips, out, cfg, self.log, lambda p: self.after(0, lambda: self.set_progress(p)))

        def done(summary):
            if hasattr(self, "bt_label"):
                E.bt_load("valorant", force=True)
                self.refresh_bt()
            el = int(time.time() - self.t_start)
            self.t_start = None
            self.go.set_busy(False)
            self.pb.set_running(False)
            self.go.config(state="normal", text="GENERATE  ▶")
            self.status.configure(text="COMPLETE", fg=self.acc)
            self.pct.configure(text=f"100%   かかった時間 {el // 60}分{el % 60:02d}秒")
            if summary:
                ResultOverlay(self, summary, el)

        def err(m):
            self.t_start = None
            self.go.set_busy(False)
            self.pb.set_running(False)
            self.go.config(state="normal", text="GENERATE  ▶")
            self.log("エラー: " + m)
            self.status.configure(fg="#FF6B6B")
            messagebox.showerror("エラー", m[:600])
        self.status.configure(fg=MUTED)
        bg_thread(work, done, err, self)


# ======================================================================= キル音登録ダイアログ
class RegisterDialog(tk.Toplevel):
    def __init__(self, ed):
        super().__init__(ed)
        self.ed, self.app = ed, ed.app
        self.game = self.app.game
        acc = self.app.accent
        self.title(f"キル音登録 - {E.GAMES[self.game]['name']}")
        self.geometry("680x600")
        self.configure(bg=BG)
        self.transient(ed.winfo_toplevel())
        self.cands = []
        self.clip = tk.StringVar(value=ed.selected_clip() or "")

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=18)
        s = Section(wrap, "KILL SOUND", "キル音を登録", acc)
        s.pack(fill="both", expand=True)
        f = s.body
        tk.Label(f, bg=CARD, fg=MUTED, font=F_SM, justify="left", anchor="w", text=(
            "1. キルが入っているクリップを選んで「候補を探す」\n"
            "2. 候補をダブルクリックで試聴 → キル音だったら「登録」\n"
            "   ※候補に無ければ、キルの秒数を直接入力して「追加」")).pack(anchor="w")
        a = tk.Frame(f, bg=CARD)
        a.pack(fill="x", pady=10)
        ttk.Combobox(a, textvariable=self.clip, values=self.app.clips, width=46).pack(side="left", fill="x", expand=True)
        NeoButton(a, "ファイル…", self.pick, accent=acc, height=32).pack(side="left", padx=6)
        self.find_btn = NeoButton(a, "候補を探す", self.find, kind="primary", accent=acc, height=32)
        self.find_btn.pack(side="left")

        mid = tk.Frame(f, bg=CARD)
        mid.pack(fill="both", expand=True)
        self.lb = tk.Listbox(mid, bg=FIELD, fg=FG, relief="flat", highlightthickness=1, highlightbackground=LINE,
                             height=9, selectbackground=mix(acc, FIELD, 0.35), font=F_MONO, activestyle="none")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<Double-Button-1>", lambda _e: self.listen())
        side = tk.Frame(mid, bg=CARD)
        side.pack(side="left", fill="y", padx=(10, 0))
        NeoButton(side, "▶  試聴", self.listen, accent=acc, width=110).pack(pady=2)
        NeoButton(side, "✔  登録", self.register, kind="primary", accent=acc, width=110).pack(pady=2)
        self.manual = tk.DoubleVar(value=0)
        tk.Label(side, text="秒数を直接", bg=CARD, fg=MUTED, font=F_SM).pack(anchor="w", pady=(12, 2))
        ttk.Spinbox(side, from_=0, to=9999, increment=0.1, textvariable=self.manual, width=9).pack()
        NeoButton(side, "追加", self.add_manual, accent=acc, width=110).pack(pady=4)

        tk.Label(f, text="REGISTERED", bg=CARD, fg=acc, font=F_EN).pack(anchor="w", pady=(12, 4))
        bot = tk.Frame(f, bg=CARD)
        bot.pack(fill="x")
        self.reg = tk.Listbox(bot, bg=FIELD, fg=FG, relief="flat", highlightthickness=1, highlightbackground=LINE,
                              height=4, selectbackground=mix(acc, FIELD, 0.35), font=F_MONO, activestyle="none")
        self.reg.pack(side="left", fill="x", expand=True)
        rs = tk.Frame(bot, bg=CARD)
        rs.pack(side="left", padx=(10, 0))
        NeoButton(rs, "▶  試聴", self.listen_reg, accent=acc, width=110).pack(pady=2)
        NeoButton(rs, "削除", self.delete_reg, accent=acc, width=110).pack(pady=2)
        self.status = tk.Label(f, text="", bg=CARD, fg=MUTED, font=F_SM)
        self.status.pack(anchor="w", pady=(8, 0))
        self.refresh_reg()

    def pick(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("動画", " ".join("*" + e for e in E.VIDEO_EXT))])
        if p:
            self.clip.set(p)

    def find(self):
        clip = self.clip.get()
        if not os.path.exists(clip):
            return messagebox.showinfo("登録", "クリップを選んでください", parent=self)
        self.find_btn.config(state="disabled")
        self.status.config(text="解析中…")
        game = self.game

        def job():
            A = E.analyze(clip, E.GAMES[game]["band"])
            if A is None:
                return []
            peaks = E.heuristic_peaks(A, game, 9)  # 候補なので広めに拾う
            return sorted(sorted(peaks, key=lambda p: -p[1])[:20])

        def done(r):
            self.find_btn.config(state="normal")
            self.cands = r
            self.lb.delete(0, "end")
            for t, s in r:
                self.lb.insert("end", f"  {t:7.2f} 秒    強さ {s:5.1f}")
            self.status.config(text=f"{len(r)}個の候補。試聴してキル音を登録してください。")

        def err(m):
            self.find_btn.config(state="normal")
            self.status.config(text="エラー: " + m[:100])
        bg_thread(job, done, err, self)

    def add_manual(self):
        t = float(self.manual.get())
        self.cands.append((t, 0.0))
        self.lb.insert("end", f"  {t:7.2f} 秒    (手動)")
        self.lb.selection_clear(0, "end")
        self.lb.selection_set("end")

    def _sel(self):
        s = self.lb.curselection()
        return self.cands[s[0]][0] if s else None

    def listen(self):
        t = self._sel()
        if t is None:
            return
        out = os.path.join(SNIP_DIR, "cand.wav")
        stop_wav()
        clip = self.clip.get()
        bg_thread(lambda: E.save_snippet(clip, t - 0.4, 1.4, out), lambda _: play_wav(out),
                  lambda m: self.status.config(text=m[:100]), self)

    def register(self):
        t = self._sel()
        if t is None:
            return messagebox.showinfo("登録", "候補を選んでください", parent=self)
        clip, game = self.clip.get(), self.game
        self.status.config(text="登録中…")

        def done(_):
            self.status.config(text=f"{t:.2f}秒 のキル音を登録しました")
            self.refresh_reg()
            self.ed.refresh_tpl()
        bg_thread(lambda: E.register_template(game, clip, t), done,
                  lambda m: self.status.config(text="エラー: " + m[:100]), self)

    def refresh_reg(self):
        self.regs = E.list_templates(self.game)
        self.reg.delete(0, "end")
        for p in self.regs:
            self.reg.insert("end", "  " + os.path.basename(p).replace(".npy", ""))

    def listen_reg(self):
        s = self.reg.curselection()
        if s:
            w = os.path.splitext(self.regs[s[0]])[0] + ".wav"
            if os.path.exists(w):
                play_wav(w)

    def delete_reg(self):
        s = self.reg.curselection()
        if s:
            E.delete_template(self.regs[s[0]])
            self.refresh_reg()
            self.ed.refresh_tpl()


# ======================================================================= CLI
def cli():
    """python killmontage.py clip1.mp4 ... -o out.mp4 --game valorant --bgm m.mp3 --beat"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("-o", "--out", default="killmontage.mp4")
    ap.add_argument("--game", default="valorant", choices=list(E.GAMES))
    ap.add_argument("--bgm", default="")
    ap.add_argument("--mode", default="auto", choices=["auto", "end", "whole"])
    ap.add_argument("--vertical", action="store_true")
    ap.add_argument("--title", default="")
    ap.add_argument("--beat", action="store_true", help="音はめ")
    ap.add_argument("--bpm", type=float, default=0)
    ap.add_argument("--pitch", type=int, default=0, help="BGMピッチ(半音)")
    ap.add_argument("--speed", type=int, default=100, help="BGM速度(%%)")
    ap.add_argument("--bass", type=int, default=0)
    a = ap.parse_args()
    c = E.default_cfg(a.game)
    c.update(mode=a.mode, bgm=a.bgm, title=a.title, beat=a.beat, bpm=a.bpm,
             bgmpitch=a.pitch, bgmspeed=a.speed, bgmbass=a.bass)
    if a.vertical:
        c.update(W=1080, H=1920, vertical=True)
    E.build(a.clips, a.out, c, print, lambda p: None)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        App().mainloop()
