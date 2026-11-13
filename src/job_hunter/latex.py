"""LaTeX helpers: escaping untrusted text, locating Tectonic, compiling, PDF inspection."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import zlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&",
    "#": r"\#", "%": r"\%", "_": r"\_", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


class LatexUnavailable(RuntimeError):
    pass


class LatexError(RuntimeError):
    pass


def escape(text: str) -> str:
    """Escape text so it is inert LaTeX. Control characters collapse to spaces."""
    cleaned = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in text)
    return " ".join("".join(_ESCAPES.get(c, c) for c in cleaned).split())


def find_tectonic(root: Path = PROJECT_ROOT) -> Path:
    candidates = []
    if os.environ.get("JH_TECTONIC"):
        candidates.append(Path(os.environ["JH_TECTONIC"]))
    for base in dict.fromkeys([Path(root), PROJECT_ROOT]):
        candidates.append(base / ".runtime" / "tectonic" / "tectonic.exe")
        candidates.append(base / ".runtime" / "tectonic" / "tectonic")
    found = shutil.which("tectonic")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LatexUnavailable("Tectonic not found; set JH_TECTONIC or install it in .runtime/tectonic")


def compile_pdf(tex_path: Path, *, root: Path = PROJECT_ROOT, timeout: int = 300) -> Path:
    """Compile ``tex_path`` with Tectonic in untrusted mode (no shell escape)."""
    tex_path = Path(tex_path)
    exe = find_tectonic(root)
    try:
        proc = subprocess.run([str(exe), "--untrusted", "--chatter", "minimal", tex_path.name],
                              cwd=tex_path.parent, capture_output=True, text=True,
                              timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise LatexError(f"LaTeX compilation timed out for {tex_path.name}") from exc
    pdf = tex_path.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.is_file():
        tail = (proc.stdout + proc.stderr).strip()[-800:]
        raise LatexError(f"LaTeX compilation failed for {tex_path.name}: {tail}")
    return pdf


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pdf_page_count(path: Path) -> int | None:
    """Best-effort page count, also looking inside compressed object streams."""
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF"):
        return None
    pattern = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")
    count = len(pattern.findall(data))
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            count += len(pattern.findall(zlib.decompress(match.group(1))))
        except zlib.error:
            continue
    return count or None
