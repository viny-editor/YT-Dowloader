"""Inicializador do YT Downloader para Windows (compilado como "YT Downloader.exe").

Faz no Windows o mesmo que o inicializador do Mac:
  1. se o app já está aberto, só traz a janela para frente;
  2. na primeira vez, instala só para o app, em %LOCALAPPDATA%\\YT Downloader:
     uv (instalador de Python), Python 3.12, Deno, yt-dlp, ffmpeg e pywebview;
  3. atualiza o motor de download no máximo uma vez por dia;
  4. copia app.py / index.html para uma pasta fixa e abre o app.

Enquanto instala, mostra uma janelinha no mesmo visual do app.
Use --demo para ver a janela de instalação sem instalar nada.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

APP_VERSION = "2.9"
PORT = 8765
NAME = "YT Downloader"
URL = f"http://127.0.0.1:{PORT}"
DEMO = "--demo" in sys.argv

LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
SUPPORT = LOCAL / NAME
BIN = SUPPORT / "bin"
VENV = SUPPORT / "venv"
RUNTIME = SUPPORT / "runtime"
LOG = SUPPORT / "log.txt"
STAMP = SUPPORT / ".ultima_atualizacao"
SHORTCUT_STAMP = SUPPORT / ".atalho"
PY_GUI = VENV / "Scripts" / "pythonw.exe"
PY = VENV / "Scripts" / "python.exe"
UV = BIN / "uv.exe"
DENO = BIN / "deno.exe"
PAYLOAD = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload"
if not PAYLOAD.exists():  # rodando direto do repositório, sem compilar
    _repo = Path(__file__).resolve().parent.parent
    PAYLOAD = _repo / "app" / "Contents" / "Resources"

UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
PACKAGES = ["yt-dlp[default]", "imageio-ffmpeg", "pywebview"]

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

ENV = dict(os.environ)
ENV["PATH"] = os.pathsep.join([str(BIN), ENV.get("PATH", "")])
ENV["UV_PYTHON_INSTALL_DIR"] = str(SUPPORT / "python")
ENV["UV_CACHE_DIR"] = str(SUPPORT / "cache")
ENV["PYTHONUTF8"] = "1"

# Conexões com o próprio app (127.0.0.1) nunca passam por proxy.
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ------------------------------------------------------------------ util
def log(*parts):
    try:
        SUPPORT.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def app_api(path, post=False, timeout=2):
    req = urllib.request.Request(
        URL + path,
        data=b"{}" if post else None,
        headers={"X-YTD": "1", "Content-Type": "application/json"} if post else {},
    )
    with LOCAL_HTTP.open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def run(cmd, what):
    log(">", " ".join(str(c) for c in cmd))
    with open(LOG, "a", encoding="utf-8") as f:
        r = subprocess.run(
            [str(c) for c in cmd], env=ENV, stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
        )
    if r.returncode != 0:
        raise RuntimeError(f"Falhou: {what}.")


def download_zip(url, exe_name, progress):
    """Baixa um .zip e extrai só o executável indicado para a pasta bin."""
    BIN.mkdir(parents=True, exist_ok=True)
    tmp = SUPPORT / (exe_name + ".zip")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    with zipfile.ZipFile(tmp) as z:
        member = next((m for m in z.namelist() if m.replace("\\", "/").split("/")[-1].lower() == exe_name), None)
        if not member:
            raise RuntimeError(f"{exe_name} não encontrado no download.")
        with z.open(member) as src, open(BIN / exe_name, "wb") as dst:
            shutil.copyfileobj(src, dst)
    tmp.unlink(missing_ok=True)


def is_stale():
    try:
        return time.time() - STAMP.stat().st_mtime > 24 * 3600
    except FileNotFoundError:
        return True


def handle_running_instance():
    """True se o app já estava aberto nesta versão (e foi trazido para frente)."""
    try:
        ping = app_api("/api/ping", timeout=1)
    except Exception:
        return False
    if ping.get("app") == APP_VERSION:
        try:
            native = app_api("/api/focus", post=True).get("native")
        except Exception:
            native = False
        if not native:
            webbrowser.open(URL)
        return True
    log("Fechando versão anterior que ainda estava rodando…")
    try:
        app_api("/api/quit", post=True)
    except Exception:
        pass
    time.sleep(1.5)
    return False


def copy_runtime():
    new = SUPPORT / "runtime.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    for name in ("app.py", "index.html", "AppIcon.ico"):
        src = PAYLOAD / name
        if src.exists():
            shutil.copy2(src, new / name)
    shutil.rmtree(RUNTIME, ignore_errors=True)
    new.rename(RUNTIME)


def ensure_shortcut():
    """Cria/atualiza o atalho no Menu Iniciar apontando para este .exe."""
    if not getattr(sys, "frozen", False):
        return
    exe = str(Path(sys.executable).resolve())
    try:
        if SHORTCUT_STAMP.read_text(encoding="utf-8") == exe:
            return
    except Exception:
        pass
    q = exe.replace("'", "''")
    folder = str(Path(exe).parent).replace("'", "''")
    ps = (
        "$p = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\YT Downloader.lnk';"
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($p);"
        f"$s.TargetPath = '{q}'; $s.WorkingDirectory = '{folder}'; $s.IconLocation = '{q},0';"
        "$s.Description = 'Baixar vídeos do YouTube'; $s.Save()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW,
                       capture_output=True, text=True)
    if r.returncode == 0:
        SHORTCUT_STAMP.write_text(exe, encoding="utf-8")
    else:
        log("Aviso: atalho não criado:", r.stderr.strip()[:300])


def launch_app():
    copy_runtime()
    log(f"==== abrindo app {APP_VERSION} ====")
    f = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PY_GUI), str(RUNTIME / "app.py")], cwd=str(RUNTIME), env=ENV,
        stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=DETACHED, close_fds=True,
    )
    # Espera o app responder (ou morrer com erro).
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError("O app fechou ao iniciar.")
        try:
            app_api("/api/ping", timeout=0.5)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("O app demorou demais para abrir.")


# ------------------------------------------------------------------ passos
STEPS = [
    ("uv", "Instalador do Python (uv)"),
    ("deno", "Motor JavaScript (Deno)"),
    ("python", "Python 3.12"),
    ("pkgs", "Motor de download e janela"),
    ("open", "Abrindo o app"),
]


def plan():
    first = not PY_GUI.exists()
    return {
        "uv": not UV.exists(),
        "deno": not DENO.exists(),
        "python": first,
        "pkgs": first or is_stale() or not _has_webview(),
        "first": first,
    }


def _has_webview():
    return (VENV / "Lib" / "site-packages" / "webview").exists()


def do_steps(ui):
    todo = plan()
    first = todo["first"]

    def step(key, fn):
        ui.step(key, "run")
        try:
            fn()
        except Exception:
            ui.step(key, "fail")
            raise
        ui.step(key, "ok")

    if todo["uv"]:
        step("uv", lambda: download_zip(UV_URL, "uv.exe", lambda f: ui.sub("uv", f)))
    else:
        ui.step("uv", "ok")

    if todo["deno"]:
        def deno():
            try:
                download_zip(DENO_URL, "deno.exe", lambda f: ui.sub("deno", f))
            except Exception as exc:  # o app funciona sem, com menos qualidades
                log("Aviso: Deno não instalado:", exc)
        step("deno", deno)
    else:
        ui.step("deno", "ok")

    if todo["python"]:
        step("python", lambda: run([UV, "venv", "--python", "3.12", VENV], "preparar o Python"))
    else:
        ui.step("python", "ok")

    if todo["pkgs"]:
        def pkgs():
            try:
                run([UV, "pip", "install", "--python", PY, "-U", *PACKAGES], "instalar o motor de download")
                STAMP.touch()
            except Exception:
                if first or not _has_webview():
                    raise
                log("Aviso: atualização falhou; abrindo com a versão instalada.")
        step("pkgs", pkgs)
    else:
        ui.step("pkgs", "ok")

    step("open", launch_app)
    try:
        ensure_shortcut()
    except Exception as exc:
        log("Aviso: atalho:", exc)


def demo_steps(ui):
    for key, _ in STEPS:
        ui.step(key, "run")
        for i in range(11):
            ui.sub(key, i / 10)
            time.sleep(0.12)
        ui.step(key, "ok")


# ------------------------------------------------------------------ janela
class SetupWindow:
    BG = "#070C1A"
    PANEL = "#08101F"
    BAR = "#0B1428"
    LINE = "#24375F"
    TEXT = "#E9EEFB"
    MUTED = "#7D8BAB"
    DIM = "#4B5878"
    RED = "#D63B3B"
    OK = "#3DDC84"

    def __init__(self, first):
        import tkinter as tk
        from tkinter import font as tkfont

        self.tk = tk
        self.q = queue.Queue()
        self.state = {k: "wait" for k, _ in STEPS}
        self.frac = {k: 0.0 for k, _ in STEPS}
        self.done = False
        self.error = None

        root = self.root = tk.Tk()
        root.title(NAME)
        root.configure(bg=self.BG)
        root.resizable(False, False)
        w, h = 560, 360
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        try:
            root.iconbitmap(str(PAYLOAD / "AppIcon.ico") if (PAYLOAD / "AppIcon.ico").exists()
                            else str(Path(__file__).with_name("AppIcon.ico")))
        except Exception:
            pass
        families = set(tkfont.families())
        mono = next((f for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New") if f in families), "Courier")
        F = lambda size, bold=False: (mono, size, "bold" if bold else "normal")  # noqa: E731

        # cabeçalho
        top = tk.Frame(root, bg=self.BG, height=48)
        top.pack(fill="x", padx=20, pady=(14, 0))
        tk.Frame(top, bg=self.RED, width=12, height=12).pack(side="left", pady=10)
        tk.Label(top, text="  YT_DOWNLOADER", fg=self.TEXT, bg=self.BG, font=F(12, True)).pack(side="left")
        tk.Label(top, text=f"v{APP_VERSION}", fg=self.DIM, bg=self.BG, font=F(9)).pack(side="left", padx=8)
        self.head = tk.Label(top, text="PREPARANDO" if first else "ATUALIZANDO", fg=self.MUTED, bg=self.BG, font=F(9, True))
        self.head.pack(side="right")

        # painel
        panel = tk.Frame(root, bg=self.PANEL, highlightthickness=1, highlightbackground="#1A2848")
        panel.pack(fill="both", expand=True, padx=20, pady=14)
        bar = tk.Frame(panel, bg=self.BAR, height=34)
        bar.pack(fill="x")
        tk.Label(bar, text="SETUP.EXE", fg="#C9D3EA", bg=self.BAR, font=F(9)).pack(side="left", padx=12, pady=7)
        tk.Label(bar, text="—  □  ×", fg=self.DIM, bg=self.BAR, font=F(9)).pack(side="right", padx=12)
        body = tk.Frame(panel, bg=self.PANEL)
        body.pack(fill="both", expand=True, padx=18, pady=12)
        tk.Label(body, text="// " + ("INSTALANDO NA PRIMEIRA VEZ (1–2 MIN)" if first else "ATUALIZANDO O MOTOR"),
                 fg=self.RED, bg=self.PANEL, font=F(9, True)).pack(anchor="w", pady=(0, 8))
        self.rows = {}
        for key, label in STEPS:
            row = tk.Frame(body, bg=self.PANEL)
            row.pack(fill="x", pady=1)
            mark = tk.Label(row, text="[    ]", fg=self.DIM, bg=self.PANEL, font=F(9, True), width=7, anchor="w")
            mark.pack(side="left")
            txt = tk.Label(row, text=label.upper(), fg=self.MUTED, bg=self.PANEL, font=F(9), anchor="w")
            txt.pack(side="left")
            self.rows[key] = (mark, txt)
        self.canvas = tk.Canvas(body, height=16, bg="#050A17", highlightthickness=1, highlightbackground=self.LINE)
        self.canvas.pack(fill="x", pady=(12, 6))
        self.msg = tk.Label(body, text="> Não feche esta janela.", fg=self.MUTED, bg=self.PANEL, font=F(8), anchor="w")
        self.msg.pack(fill="x")
        self.btns = tk.Frame(body, bg=self.PANEL)
        self.F = F
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(80, self.poll)

    # chamadas vindas da thread de trabalho
    def step(self, key, status):
        self.q.put(("step", key, status))

    def sub(self, key, frac):
        self.q.put(("sub", key, frac))

    def finish(self, error=None):
        self.q.put(("finish", error, None))

    # thread da interface
    def poll(self):
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "step":
                    self.state[a] = b
                    if b == "ok":
                        self.frac[a] = 1.0
                elif kind == "sub":
                    self.frac[a] = max(0.0, min(1.0, b))
                elif kind == "finish":
                    self.done, self.error = True, a
        except queue.Empty:
            pass
        self.render()
        if self.done and not self.error:
            self.root.after(350, self.root.destroy)
            return
        self.root.after(80, self.poll)

    def render(self):
        marks = {"wait": ("[    ]", self.DIM, self.MUTED), "run": ("[ .. ]", self.TEXT, self.TEXT),
                 "ok": ("[ OK ]", self.OK, self.MUTED), "fail": ("[ !! ]", self.RED, self.RED)}
        blink = int(time.time() * 3) % 2
        for key, (mark, txt) in self.rows.items():
            st = self.state[key]
            t, c1, c2 = marks[st]
            if st == "run":
                t = "[ .. ]" if blink else "[  . ]"
            mark.config(text=t, fg=c1)
            txt.config(fg=c2)
        total = sum(self.frac.values()) / len(STEPS)
        cv = self.canvas
        cv.delete("all")
        width = max(cv.winfo_width(), 10)
        n, gap = 30, 3
        seg = (width - 6 - gap * (n - 1)) / n
        filled = int(round(total * n))
        for i in range(n):
            x = 3 + i * (seg + gap)
            if i < filled:
                cv.create_rectangle(x, 4, x + seg, 13, fill=self.RED, width=0)
        if self.error and not self.btns.winfo_ismapped():
            self.head.config(text="ERRO", fg=self.RED)
            self.msg.config(text="! " + self.error, fg="#FF7B7B", wraplength=480, justify="left")
            self.btns.pack(fill="x", pady=(8, 0))
            for label, cmd in (("FECHAR", self.root.destroy), ("ABRIR LOG", self.open_log)):
                b = self.tk.Button(self.btns, text=label, command=cmd, font=self.F(9, True), fg="#FFFFFF",
                                   bg=self.RED if label == "ABRIR LOG" else self.PANEL, activebackground="#E84A4A",
                                   activeforeground="#FFFFFF", relief="flat", bd=0, padx=14, pady=6,
                                   highlightthickness=1, highlightbackground=self.LINE, cursor="hand2")
                b.pack(side="right", padx=(8, 0))

    def open_log(self):
        try:
            os.startfile(str(LOG))  # noqa: S606
        except Exception:
            pass

    def on_close(self):
        if self.done:
            self.root.destroy()
        else:
            self.msg.config(text="> Aguarde terminar a instalação…", fg=self.TEXT)

    def run(self, worker):
        def target():
            try:
                worker(self)
                self.finish()
            except Exception as exc:
                log(traceback.format_exc())
                msg = str(exc) or exc.__class__.__name__
                if isinstance(exc, OSError) and "urlopen" in repr(exc).lower():
                    msg = "Sem conexão com a internet."
                self.finish(f"{msg} Verifique sua internet e tente de novo.")
        threading.Thread(target=target, daemon=True).start()
        self.root.mainloop()


# ------------------------------------------------------------------ main
def main():
    if DEMO:
        SetupWindow(first=True).run(demo_steps)
        return
    SUPPORT.mkdir(parents=True, exist_ok=True)
    log(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} · inicializador {APP_VERSION} ====")
    if handle_running_instance():
        return
    todo = plan()
    if not any(todo[k] for k in ("uv", "deno", "python", "pkgs")):
        # Tudo pronto: abre direto, sem janela de instalação.
        try:
            launch_app()
            ensure_shortcut()
            return
        except Exception:
            log(traceback.format_exc())
            # cai na janela, que mostra o erro com o botão de log
    SetupWindow(first=todo["first"]).run(do_steps)


if __name__ == "__main__":
    main()
