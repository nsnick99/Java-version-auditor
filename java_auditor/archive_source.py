"""Uniform, read-only view over a jar/war/ear/zip archive or a directory."""

import io
import os
import zipfile
from pathlib import Path

MAX_ENTRY_BYTES = 64 * 1024 * 1024


class Source:
    def __init__(self, label, kind):
        self.label = label; self.kind = kind; self._names = []; self._reader = None
    def names(self): return self._names
    def read(self, name): return self._reader(name)
    def exists(self, name): return name in set(self._names)
    def read_text(self, name):
        try: return self.read(name).decode("utf-8", errors="replace")
        except Exception: return ""


def _zip_source(zf, label, kind):
    src = Source(label, kind); src._names = [n for n in zf.namelist() if not n.endswith("/")]
    def reader(name):
        info = zf.getinfo(name)
        return b"" if info.file_size > MAX_ENTRY_BYTES else zf.read(name)
    src._reader = reader; return src


def _dir_source(root: Path):
    src = Source(str(root), "DIRECTORY"); names = []; skip_dirs = {".git", ".svn", ".idea", "node_modules", ".gradle"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames: names.append((Path(dirpath) / fn).relative_to(root).as_posix())
    src._names = names
    def reader(name):
        p = root / name
        return b"" if p.stat().st_size > MAX_ENTRY_BYTES else p.read_bytes()
    src._reader = reader; return src


def open_source(path: str) -> Source:
    p = Path(path)
    if not p.exists(): raise FileNotFoundError(path)
    if p.is_dir(): return _dir_source(p)
    if not zipfile.is_zipfile(p): raise ValueError(f"{path} is not a zip-based archive or a directory")
    kind = {".jar": "JAR", ".war": "WAR", ".ear": "EAR"}.get(p.suffix.lower(), "ZIP")
    return _zip_source(zipfile.ZipFile(p), str(p), kind)


def open_nested(parent: Source, entry_name: str):
    data = parent.read(entry_name)
    if not data: return None
    try: zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile: return None
    return _zip_source(zf, entry_name, "JAR")
