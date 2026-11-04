"""Grava o número de versão em todos os arquivos do app.

Uso: python scripts/set_version.py 2.5
"""
import pathlib
import re
import sys

v = sys.argv[1].lstrip("v")
root = pathlib.Path(__file__).resolve().parent.parent
RULES = [
    ("app/Contents/Resources/app.py", r'APP_VERSION = "[^"]*"', f'APP_VERSION = "{v}"'),
    ("app/Contents/MacOS/YTDownloader", r'APP_VERSION="[^"]*"', f'APP_VERSION="{v}"'),
    ("app/Contents/Resources/index.html", r'(class="ver[^"]*">)v[^<]*', lambda m: f"{m.group(1)}v{v}"),
    ("app/Contents/Info.plist", r"(<key>CFBundle(?:Short)?Version(?:String)?</key><string>)[^<]*",
     lambda m: m.group(1) + v),
    ("windows/launcher.py", r'APP_VERSION = "[^"]*"', f'APP_VERSION = "{v}"'),
]
for rel, pattern, repl in RULES:
    p = root / rel
    s = p.read_text(encoding="utf-8")
    s2, n = re.subn(pattern, repl, s)
    if not n:
        sys.exit(f"Padrão de versão não encontrado em {rel}")
    p.write_text(s2, encoding="utf-8", newline="\n")
    print(f"{rel}: {v} ({n}x)")
