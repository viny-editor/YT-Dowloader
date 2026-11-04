"""YT Downloader — servidor local + janela nativa (macOS e Windows).

Roda só em 127.0.0.1 (o próprio computador). Usa o yt-dlp para baixar e o
ffmpeg para juntar vídeo + áudio ou converter para MP3/WAV.
"""
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8765
MAX_PARALLEL = 3  # downloads ao mesmo tempo; o resto espera na fila
APP_VERSION = "2.9"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: sem piscar console
HERE = Path(__file__).resolve().parent
if IS_WIN:
    SUPPORT = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "YT Downloader"
else:
    SUPPORT = Path.home() / "Library" / "Application Support" / "YT Downloader"
BIN = SUPPORT / "bin"
SETTINGS_FILE = SUPPORT / "settings.json"
HISTORY_FILE = SUPPORT / "baixados.json"
INDEX_HTML = (HERE / "index.html").read_bytes()

SUPPORT.mkdir(parents=True, exist_ok=True)
BIN.mkdir(parents=True, exist_ok=True)
_extra = [] if IS_WIN else ["/opt/homebrew/bin", "/usr/local/bin"]
os.environ["PATH"] = os.pathsep.join([str(BIN), *_extra, os.environ.get("PATH", "")])


def run(cmd, **kw):
    """subprocess.run sem abrir janela de console no Windows."""
    return subprocess.run(cmd, creationflags=NO_WINDOW, **kw)

import yt_dlp  # noqa: E402
from yt_dlp.postprocessor.common import PostProcessor  # noqa: E402
from yt_dlp.utils import download_range_func  # noqa: E402


# ---------------------------------------------------------------- ffmpeg
def setup_ffmpeg():
    """Usa o ffmpeg do sistema se existir; senão, o que vem no imageio-ffmpeg."""
    link = BIN / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
    found = shutil.which("ffmpeg")
    if found and Path(found).resolve() != link.resolve():
        return None
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if IS_WIN:
            # Windows não deixa criar atalho simbólico sem admin: copia o executável.
            if not link.exists() or link.stat().st_size != os.path.getsize(exe):
                shutil.copy2(exe, link)
        else:
            if link.is_symlink() and os.path.realpath(link) != os.path.realpath(exe):
                link.unlink()
            if not link.exists():
                os.symlink(exe, link)
        return str(BIN)
    except Exception as exc:  # pragma: no cover
        print("Aviso: ffmpeg não encontrado:", exc, flush=True)
        return None


FFMPEG_DIR = setup_ffmpeg()
FFMPEG = (str(Path(FFMPEG_DIR) / ("ffmpeg.exe" if IS_WIN else "ffmpeg")) if FFMPEG_DIR
          else shutil.which("ffmpeg") or "ffmpeg")


# ---------------------------------------------------------------- settings
def load_settings():
    data = {"folder": str(Path.home() / "Downloads"), "quality": "1080", "ask_folder": False, "h264": True}
    try:
        data.update(json.loads(SETTINGS_FILE.read_text()))
    except Exception:
        pass
    if not Path(data["folder"]).is_dir():
        data["folder"] = str(Path.home() / "Downloads")
    return data


def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("Não salvei as configurações:", exc, flush=True)


settings = load_settings()

# ---------------------------------------------------------------- qualidades
H264 = "[vcodec^=avc1]"
QUALITIES = {
    "1080": {
        "format": f"bv*[height<=1080]{H264}+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge": "mp4",
    },
    "max": {"format": "bv*+ba/b", "merge": "mp4"},
    "720": {
        "format": f"bv*[height<=720]{H264}+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
        "merge": "mp4",
    },
    "mp3": {"format": "ba/b", "audio": "mp3"},
    "wav": {"format": "ba/b", "audio": "wav"},
}


# ---------------------------------------------------------------- H.264
def probe(path):
    """Codec de vídeo, codec de áudio e duração (s) de um arquivo, lidos pelo ffmpeg."""
    r = run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    err = r.stderr or ""
    v = re.search(r"Stream #\S+.*?: Video: (\w+)", err)
    a = re.search(r"Stream #\S+.*?: Audio: (\w+)", err)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else None
    return (v.group(1) if v else None), (a.group(1) if a else None), dur


def encoders_for(vcodec):
    if vcodec == "h264":
        return [["-c:v", "copy"]]
    q = []
    if IS_MAC:  # chip de vídeo do Mac: muito mais rápido
        q.append(["-c:v", "h264_videotoolbox", "-q:v", "70", "-pix_fmt", "yuv420p", "-tag:v", "avc1"])
    q.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"])
    if IS_WIN:
        q.append(["-c:v", "h264_mf", "-b:v", "25M", "-pix_fmt", "yuv420p"])
    return q


def ensure_h264(job_id, path):
    """Converte para H.264 + AAC em .mp4 se ainda não estiver. Devolve o caminho final."""
    job = jobs[job_id]
    vcodec, acodec, dur = probe(path)
    if vcodec is None:
        return path  # só áudio
    if vcodec == "h264" and acodec in (None, "aac") and path.lower().endswith(".mp4"):
        return path
    final = str(Path(path).with_suffix(".mp4"))
    tmp = str(Path(path).with_suffix(".h264-tmp.mp4"))
    audio = [] if acodec is None else (["-c:a", "copy"] if acodec == "aac" else ["-c:a", "aac", "-b:a", "192k"])
    what = "Convertendo para H.264" if vcodec != "h264" else "Ajustando áudio (AAC)"
    errlog = SUPPORT / "ffmpeg-ultimo-erro.txt"
    for enc in encoders_for(vcodec):
        update(job_id, status="processing", phase=what, progress=0 if dur else None, speed=None, eta=None)
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?",
               *enc, *audio, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", tmp]
        started = time.time()
        with open(errlog, "w", encoding="utf-8", errors="replace") as ferr:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ferr, stdin=subprocess.DEVNULL,
                                    text=True, creationflags=NO_WINDOW)
            for line in proc.stdout:
                if job["cancel"]:
                    proc.kill()
                    break
                if line.startswith("out_time_us=") and dur:
                    try:
                        t = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    pct = max(0.0, min(99.0, t / dur * 100))
                    el = time.time() - started
                    eta = el / pct * (100 - pct) if pct > 1 else None
                    update(job_id, progress=pct, eta=eta)
            proc.wait()
        if job["cancel"]:
            Path(tmp).unlink(missing_ok=True)
            raise Canceled()
        if proc.returncode == 0 and Path(tmp).exists() and Path(tmp).stat().st_size > 0:
            os.replace(tmp, final)
            if final != path:
                Path(path).unlink(missing_ok=True)
            print(f"H.264: {Path(final).name} ({enc[1]})", flush=True)
            return final
        print(f"H.264: {enc[1]} falhou, tentando o próximo…", flush=True)
        Path(tmp).unlink(missing_ok=True)
    raise RuntimeError("Não consegui converter para H.264 (detalhes em ffmpeg-ultimo-erro.txt).")


class H264PP(PostProcessor):
    """Etapa extra do yt-dlp: roda depois de juntar vídeo + áudio."""

    def __init__(self, job_id):
        super().__init__(None)
        self.job_id = job_id

    def run(self, info):
        fp = info.get("filepath")
        if fp and os.path.exists(fp):
            new = ensure_h264(self.job_id, fp)
            if new != fp:
                info["filepath"] = new
                info["ext"] = "mp4"
            update(self.job_id, filepath=new)
        return [], info


def parse_time(text):
    """'83', '1:23', '01:02:03' -> segundos. Vazio -> None."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    if not re.fullmatch(r"\d+(\.\d+)?(:\d+(\.\d+)?){0,2}", text):
        raise ValueError(f"Tempo inválido: {text}")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


# ---------------------------------------------------------------- jobs
jobs = {}
order = []


# ---------------------------------------------------------------- baixados (histórico)
hist_lock = threading.Lock()
hist_rev = [0]


def load_history():
    data = {"next_seq": 1, "items": []}
    try:
        loaded = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded.get("items"), list):
            data.update(loaded)
    except Exception:
        pass
    return data


history = load_history()


def save_history():
    hist_rev[0] += 1
    try:
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)
    except Exception as exc:
        print("Não salvei o histórico:", exc, flush=True)


def next_seq():
    """Número de ordem: a sequência em que os links foram colados."""
    with hist_lock:
        n = int(history.get("next_seq", 1))
        history["next_seq"] = n + 1
        save_history()
        return n


def record_download(job, info, filepath):
    item = {
        "id": uuid.uuid4().hex[:12],
        "seq": job.get("seq", 0),
        "idx": int(info.get("playlist_index") or 0),
        "title": info.get("title") or job.get("title") or Path(filepath).stem,
        "url": info.get("webpage_url") or job.get("url"),
        "thumbnail": info.get("thumbnail") or job.get("thumbnail"),
        "filepath": str(filepath),
        "quality": job.get("quality"),
        "h264": bool(job.get("h264")),
        "added": job.get("created"),
        "finished": time.time(),
    }
    with hist_lock:
        # o mesmo arquivo baixado de novo substitui a entrada antiga
        history["items"] = [i for i in history["items"] if i.get("filepath") != item["filepath"]]
        history["items"].append(item)
        save_history()


def public_history():
    with hist_lock:
        items = sorted(history["items"], key=lambda i: (i.get("seq", 0), i.get("idx", 0), i.get("finished", 0)))
        out = []
        for n, i in enumerate(items, 1):
            fp = Path(i.get("filepath", ""))
            exists = fp.is_file()
            out.append({**i, "n": n, "exists": exists, "name": fp.stem, "ext": fp.suffix,
                        "folder": str(fp.parent), "size": fp.stat().st_size if exists else None})
        return out


def find_item(item_id):
    for i in history["items"]:
        if i.get("id") == item_id:
            return i
    return None


class RecordPP(PostProcessor):
    """Última etapa do yt-dlp: anota o arquivo pronto na lista de baixados."""

    def __init__(self, job_id):
        super().__init__(None)
        self.job_id = job_id

    def run(self, info):
        fp = info.get("filepath")
        if fp and os.path.exists(fp):
            record_download(jobs[self.job_id], info, fp)
        return [], info
lock = threading.Lock()
work = queue.Queue()


class Canceled(Exception):
    pass


def update(job_id, **fields):
    with lock:
        jobs[job_id].update(fields)


def public_jobs():
    with lock:
        return [
            {k: v for k, v in jobs[j].items() if k != "cancel"} for j in order
        ]


def run_job(job_id):
    job = jobs[job_id]
    q = QUALITIES[job["quality"]]
    folder = job["folder"]
    rng = ""
    if job["start"] is not None or job["end"] is not None:
        a = fmt_time(job["start"] or 0)
        b = fmt_time(job["end"]) if job["end"] is not None else "fim"
        rng = f" (trecho {a}-{b})"

    tlen = 110 if IS_WIN else 180
    if job["playlist"]:
        tmpl = os.path.join(
            folder, "%(playlist_title).80B", f"%(playlist_index)03d - %(title).{tlen - 30}B" + rng + ".%(ext)s"
        )
    else:
        tmpl = os.path.join(folder, f"%(title).{tlen}B" + rng + ".%(ext)s")

    def progress_hook(d):
        if job["cancel"]:
            raise Canceled()
        info = d.get("info_dict") or {}
        extra = {}
        if job["title"] == job["url"]:
            t = (info.get("playlist_title") or info.get("playlist")) if job["playlist"] else None
            t = t or info.get("title")
            if t:
                extra["title"] = t
        if not job.get("thumbnail") and info.get("thumbnail"):
            extra["thumbnail"] = info["thumbnail"]
        if job["playlist"] and info.get("playlist_index"):
            extra["item"] = f"{info.get('playlist_index')} de {info.get('n_entries') or '?'}"
            extra["current_title"] = info.get("title")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = None
            if total:
                pct = min(100.0, done / total * 100)
            elif d.get("fragment_count"):
                pct = (d.get("fragment_index") or 0) / d["fragment_count"] * 100
            vcodec = info.get("vcodec")
            phase = "Baixando áudio" if vcodec in (None, "none") and info.get("acodec") not in (None, "none") else "Baixando vídeo"
            if q.get("audio"):
                phase = "Baixando áudio"
            update(
                job_id,
                status="downloading",
                phase=phase,
                progress=pct,
                speed=d.get("speed"),
                eta=d.get("eta"),
                size=total,
                **extra,
            )
        elif d["status"] == "finished":
            update(job_id, progress=100, phase="Download concluído", **extra)

    def pp_hook(d):
        if job["cancel"]:
            raise Canceled()
        if d["status"] == "started":
            names = {
                "Merger": "Juntando vídeo e áudio",
                "FFmpegMerger": "Juntando vídeo e áudio",
                "ExtractAudio": "Convertendo áudio",
                "FFmpegExtractAudio": "Convertendo áudio",
                "H264": "Conferindo codec",
            }
            update(job_id, status="processing", phase=names.get(d.get("postprocessor"), "Finalizando"),
                   progress=None, speed=None, eta=None)
        elif d["status"] == "finished":
            fp = (d.get("info_dict") or {}).get("filepath")
            if fp:
                update(job_id, filepath=fp)

    opts = {
        "format": q["format"],
        "outtmpl": tmpl,
        "noplaylist": not job["playlist"],
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 4,
        "ignoreerrors": "only_download" if job["playlist"] else False,
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if q.get("merge"):
        opts["merge_output_format"] = q["merge"]
    if q.get("audio"):
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": q["audio"], "preferredquality": "0"}
        ]
    if rng:
        start = job["start"] or 0
        end = job["end"] if job["end"] is not None else math.inf
        opts["download_ranges"] = download_range_func(None, [(start, end)])
        opts["force_keyframes_at_cuts"] = True

    update(job_id, status="starting", phase="Lendo vídeo…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if job.get("h264") and not q.get("audio"):
                ydl.add_post_processor(H264PP(job_id), when="post_process")
            ydl.add_post_processor(RecordPP(job_id), when="after_move")
            ydl.download([job["url"]])
        if job["cancel"]:
            raise Canceled()
        update(job_id, status="done", phase="Pronto", progress=100, speed=None, eta=None,
               finished_at=time.time())
    except Canceled:
        update(job_id, status="canceled", phase="Cancelado", speed=None, eta=None)
    except Exception as exc:
        if job["cancel"]:
            update(job_id, status="canceled", phase="Cancelado", speed=None, eta=None)
        else:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")
            update(job_id, status="error", phase="Erro", error=friendly_error(msg), speed=None, eta=None)


def friendly_error(msg):
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "O YouTube pediu verificação (\"não sou um robô\"). Tente de novo mais tarde."
    if "private video" in low:
        return "Esse vídeo é privado."
    if "video unavailable" in low:
        return "Vídeo indisponível (removido, bloqueado na região ou com restrição)."
    if "age" in low and "confirm" in low:
        return "Vídeo com restrição de idade: precisa de login, que este app não faz."
    if "ffmpeg" in low:
        return "Faltou o ffmpeg para juntar/converter. Feche e abra o app de novo."
    if "unsupported url" in low:
        return "Esse link não é suportado."
    return msg[:400]


def worker():
    while True:
        job_id = work.get()
        if not jobs[job_id]["cancel"]:
            run_job(job_id)
        work.task_done()


for _ in range(MAX_PARALLEL):
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- info
def get_info(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "ignore_no_formats_error": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    is_playlist = info.get("_type") == "playlist"
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    if not thumb and is_playlist:
        for e in info.get("entries") or []:
            t = (e or {}).get("thumbnails") or []
            if t:
                thumb = t[-1].get("url")
                break
    return {
        "title": info.get("title") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "is_playlist": is_playlist,
        "count": info.get("playlist_count") or len(info.get("entries") or []) if is_playlist else None,
        "has_list": ("list=" in url) and not is_playlist,
        "url": info.get("webpage_url") or url,
    }


# ---------------------------------------------------------------- janela nativa
WINDOW = None  # janela do pywebview, quando o app roda em modo nativo


def native_choose_folder(current):
    import webview

    kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
    if kind is None:
        kind = getattr(webview, "FOLDER_DIALOG")
    result = WINDOW.create_file_dialog(kind, directory=current)
    if not result:
        return None
    path = result[0] if isinstance(result, (list, tuple)) else result
    return str(path).rstrip("/")


class WindowApi:
    """Funções chamadas pelos botões — □ × da barra de título."""

    def __init__(self):
        self._max = False

    def minimize(self):
        WINDOW.minimize()

    def toggle_max(self):
        if self._max:
            WINDOW.restore()
        else:
            WINDOW.maximize()
        self._max = not self._max

    def close(self):
        WINDOW.destroy()


# ---------------------------------------------------------------- mac helpers
def choose_folder(current):
    if WINDOW is not None:
        try:
            return native_choose_folder(current)
        except Exception as exc:
            print("Diálogo nativo falhou:", exc, flush=True)
    if IS_WIN:
        safe = current.replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = 'Onde salvar os downloads?';"
            f"$d.SelectedPath = '{safe}';"
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
        r = run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8")
        path = r.stdout.strip()
        return path or None
    script = (
        'tell application (path to frontmost application as text)\n'
        f'  set f to choose folder with prompt "Onde salvar os downloads?" '
        f'default location (POSIX file "{current}")\n'
        'end tell\n'
        'POSIX path of f'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    path = r.stdout.strip()
    return path.rstrip("/") if path else None


def read_clipboard():
    if IS_WIN:
        r = run(["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                capture_output=True, text=True, encoding="utf-8")
    elif IS_MAC:
        r = run(["pbpaste"], capture_output=True, text=True)
    else:
        return ""
    return (r.stdout or "").strip()


def reveal_file(path):
    if IS_WIN:
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        run(["open", "-R", path])


BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(name):
    name = BAD_CHARS.sub("", (name or "")).strip().rstrip(". ")
    if not name:
        raise ValueError("Digite um nome para o arquivo.")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", name):
        raise ValueError("Esse nome é reservado pelo sistema. Escolha outro.")
    return name[:180]


def rename_item(item_id, new_name):
    with hist_lock:
        item = find_item(item_id)
        if not item:
            raise ValueError("Esse download não está mais na lista.")
        old = Path(item["filepath"])
        if not old.is_file():
            raise ValueError("O arquivo não foi encontrado. Ele pode ter sido movido ou apagado.")
        new = old.with_name(clean_name(new_name) + old.suffix)
        if new == old:
            return item
        if new.exists() and not os.path.samefile(new, old):
            raise ValueError(f"Já existe um arquivo chamado \"{new.name}\" nessa pasta.")
        try:
            os.rename(old, new)
        except PermissionError:
            raise ValueError("O sistema não deixou renomear. O arquivo está aberto em outro programa?")
        item["filepath"] = str(new)
        save_history()
    with lock:  # mantém o botão Finder/Explorer da fila apontando para o arquivo certo
        for j in jobs.values():
            if j.get("filepath") == str(old):
                j["filepath"] = str(new)
    return item


def open_file(path):
    if IS_WIN:
        os.startfile(path)  # noqa: S606
    else:
        run(["open", path])


def open_folder(path):
    if IS_WIN:
        os.startfile(path)  # noqa: S606
    else:
        run(["open", path])


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/ping":
            self.send_json({"ok": True, "app": APP_VERSION})
        elif self.path == "/api/history":
            self.send_json({"items": public_history(), "rev": hist_rev[0]})
        elif self.path == "/api/state":
            self.send_json({"settings": settings, "jobs": public_jobs(), "version": yt_dlp.version.__version__,
                            "history_rev": hist_rev[0], "history_count": len(history["items"]),
                            "platform": "win" if IS_WIN else "mac"})
        else:
            self.send_error(404)

    def do_POST(self):
        # Cabeçalho próprio: impede que outros sites mandem comandos para o app.
        if self.headers.get("X-YTD") != "1":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json({"error": "Requisição inválida"}, 400)
            return
        try:
            self.route(data)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")
            self.send_json({"error": friendly_error(msg)}, 500)

    def route(self, data):
        p = self.path
        if p == "/api/info":
            url = (data.get("url") or "").strip()
            if not re.match(r"https?://", url):
                raise ValueError("Cole um link que comece com https://")
            self.send_json(get_info(url))

        elif p == "/api/download":
            url = (data.get("url") or "").strip()
            quality = data.get("quality") or "1080"
            if quality not in QUALITIES:
                raise ValueError("Qualidade inválida")
            if not re.match(r"https?://", url):
                raise ValueError("Link inválido")
            start, end = parse_time(data.get("start")), parse_time(data.get("end"))
            if start is not None and end is not None and end <= start:
                raise ValueError("O fim do trecho precisa vir depois do início.")
            folder = (data.get("folder") or "").strip() or settings["folder"]
            Path(folder).mkdir(parents=True, exist_ok=True)
            playlist = data.get("playlist")
            if playlist is None:  # veio pelo Enter, sem prévia
                playlist = "/playlist" in url and "list=" in url
            job_id = uuid.uuid4().hex[:10]
            with lock:
                jobs[job_id] = {
                    "id": job_id, "url": url, "title": data.get("title") or url,
                    "thumbnail": data.get("thumbnail"), "quality": quality,
                    "folder": folder, "start": start, "end": end,
                    "playlist": bool(playlist),
                    "h264": bool(data.get("h264", settings.get("h264", True))),
                    "status": "queued", "phase": "Na fila", "progress": None,
                    "speed": None, "eta": None, "size": None, "filepath": None,
                    "error": None, "cancel": False, "item": None, "current_title": None,
                    "created": time.time(),
                    "seq": next_seq(),
                }
                order.append(job_id)
            settings["quality"] = quality
            save_settings(settings)
            work.put(job_id)
            self.send_json({"id": job_id})

        elif p == "/api/cancel":
            jid = data.get("id")
            if jid in jobs:
                with lock:
                    jobs[jid]["cancel"] = True
                    if jobs[jid]["status"] == "queued":
                        jobs[jid].update(status="canceled", phase="Cancelado")
            self.send_json({"ok": True})

        elif p == "/api/rename":
            item = rename_item(data.get("id"), data.get("name"))
            self.send_json({"ok": True, "name": Path(item["filepath"]).stem})

        elif p in ("/api/file/open", "/api/file/reveal"):
            with hist_lock:
                item = find_item(data.get("id"))
            if not item:
                raise ValueError("Esse download não está mais na lista.")
            fp = item["filepath"]
            if Path(fp).is_file():
                (open_file if p.endswith("open") else reveal_file)(fp)
            elif Path(fp).parent.is_dir():
                open_folder(str(Path(fp).parent))
            else:
                raise ValueError("O arquivo e a pasta não foram encontrados.")
            self.send_json({"ok": True})

        elif p == "/api/file/forget":
            with hist_lock:
                history["items"] = [i for i in history["items"] if i.get("id") != data.get("id")]
                save_history()
            self.send_json({"ok": True})

        elif p == "/api/clear":
            with lock:
                for jid in list(order):
                    if jobs[jid]["status"] in ("done", "error", "canceled"):
                        order.remove(jid)
                        del jobs[jid]
            self.send_json({"ok": True})

        elif p == "/api/choose-folder":
            path = choose_folder(settings["folder"])
            if data.get("save", True):
                if path:
                    settings["folder"] = path
                    save_settings(settings)
                self.send_json({"folder": settings["folder"]})
            else:
                self.send_json({"folder": path})  # None = cancelou

        elif p == "/api/settings":
            if "ask_folder" in data:
                settings["ask_folder"] = bool(data["ask_folder"])
            if "h264" in data:
                settings["h264"] = bool(data["h264"])
            save_settings(settings)
            self.send_json({"settings": settings})

        elif p == "/api/reveal":
            jid = data.get("id")
            job = jobs.get(jid)
            if job and job.get("filepath") and Path(job["filepath"]).exists():
                reveal_file(job["filepath"])
            else:
                open_folder(job["folder"] if job else settings["folder"])
            self.send_json({"ok": True})

        elif p == "/api/open-folder":
            open_folder(settings["folder"])
            self.send_json({"ok": True})

        elif p == "/api/clipboard":
            self.send_json({"text": read_clipboard()})

        elif p == "/api/focus":
            if WINDOW is not None:
                try:
                    WINDOW.restore()
                    WINDOW.show()
                except Exception:
                    pass
            self.send_json({"native": WINDOW is not None})

        elif p == "/api/quit":
            self.send_json({"ok": True})
            threading.Timer(0.4, lambda: os._exit(0)).start()

        else:
            self.send_error(404)


def mac_identity():
    """Nome e ícone corretos no Dock e na barra de menus."""
    try:
        from AppKit import NSApplication, NSBundle, NSImage

        info = NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "YT Downloader"
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(0)  # aparece no Dock
        icon = HERE / "AppIcon.icns"
        if icon.exists():
            app.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
    except Exception as exc:
        print("Aviso (ícone/nome):", exc, flush=True)


def win_identity():
    """Windows: o app aparece como 'YT Downloader' (e não Python) na barra de tarefas."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("local.ytdownloader.app")
    except Exception as exc:
        print("Aviso (AppUserModelID):", exc, flush=True)


def win_set_icon():
    try:
        import ctypes
        from ctypes import wintypes

        ico = HERE / "AppIcon.ico"
        if not ico.exists():
            return
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = None
        for _ in range(20):
            hwnd = user32.FindWindowW(None, "YT Downloader")
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
        big = user32.LoadImageW(None, str(ico), IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        small = user32.LoadImageW(None, str(ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, 1, big)
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, 0, small)
    except Exception as exc:
        print("Aviso (ícone):", exc, flush=True)


def main():
    global WINDOW
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)  # já está rodando
        return
    print(f"YT Downloader em {url} (yt-dlp {yt_dlp.version.__version__})", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    use_browser = "--browser" in sys.argv or "--no-browser" in sys.argv
    if not use_browser:
        try:
            import webview

            if IS_MAC:
                mac_identity()
            elif IS_WIN:
                win_identity()
            WINDOW = webview.create_window(
                "YT Downloader",
                url,
                width=920,
                height=800,
                min_size=(660, 600),
                frameless=True,
                easy_drag=False,
                background_color="#070C1A",
                js_api=WindowApi(),
            )
            if IS_WIN:
                WINDOW.events.shown += lambda: threading.Thread(target=win_set_icon, daemon=True).start()
            webview.start()
            os._exit(0)  # janela fechada = app fechado
        except Exception as exc:
            WINDOW = None
            print("Janela nativa indisponível, abrindo no navegador:", exc, flush=True)

    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
