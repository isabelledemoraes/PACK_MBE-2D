"""Read a directory of MBE sample sheets and hand them to `pack_kit.learn`.

Declared in `sources.yaml` as
``fetcher: "{here}/fetchers/sample_doc_fetcher.py::fetch_sample_docs"``; the
contract is the one `pack_kit.learn._pages_from_fetcher` reads — a list of dicts
carrying ``url``, ``content``, ``section``, ``changed`` and ``error``.

The sheets are FREE NOTES and stay free: nothing in the body is recognised,
reordered, grouped or interpreted here. Reading them is the extractor's work,
guided by `ontology/schema.ttl`. This module does only what a language model
cannot do, or must not be paid to do on every sheet:

1. **A Word file is not text.** Everything goes through the `.docx` form,
   because it is the only one in which the next three points can be seen at
   all. A legacy `.doc` is converted first (LibreOffice, or Word on Windows) —
   see `_doc_to_docx`. `catdoc` / `antiword` remain as a last resort and say so:
   they give the words and lose the rest.
2. **Yellow means "not filled in".** The sheets start from a template whose
   default values are highlighted in yellow; an operator who fills a field
   removes the highlight. What is still yellow is therefore a value nobody
   wrote, and it is REMOVED before anything reads it — otherwise the template's
   molyblock becomes this growth's molyblock. Every removal is printed, because
   a field filled in and left yellow disappears the same way.
3. **The pictures are kept.** The images embedded in a sheet (mostly RHEED
   patterns) are written under `data/images/<sample>/`, and a marker
   ``[IMAGE: <path>]`` is left in the text exactly where the picture stood.
   Nothing analyses them yet; the marker is what lets the memory say which
   picture belongs to which step, so that something can later.
4. **The header is a TABLE.** Its cells are read as cells — label and value in
   one cell, in the cell to the right, or in the cell below — and written back
   as ``Label: value`` lines. A cell that carries no known label is passed on
   as it is rather than dropped.
5. **Change detection.** A sheet that has not moved since the last run is
   skipped before any LLM call.

The hash is taken on the RENDERED TEXT, never on the file bytes: re-saving a
Word file rewrites its metadata and would report every sheet as changed while
none of its content had. A file whose size and modification time are those of
the last run is not even opened — converting several hundred `.doc` files to
find that nothing moved is the slow part of a weekly run.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

#: Where the per-file state of the change detection is kept. Set in
#: `sources.yaml` under `fetcher_env:`, read at import exactly as
#: agent-slapsci's `env:` block does for a native tool.
HASH_FILE = os.environ.get("SAMPLE_DOC_HASH_FILE", "")

#: Bump when what this module WRITES for an unchanged file changes (a new
#: marker, a new header line). It invalidates the size-and-time shortcut, which
#: would otherwise keep skipping files whose rendering is no longer the same.
RENDER_VERSION = 2

#: The suffixes recognised. `.txt` and `.md` are there for a sheet typed
#: outside Word; they have no highlight and no picture, and are read as they are.
SUFFIXES = (".doc", ".docx", ".txt", ".md")

#: The pack's own directory — images are named relative to it in the markers,
#: so that a memory built on one machine still points at the right file on
#: another.
PACK_ROOT = Path(__file__).resolve().parent.parent


class ExtractionUnavailable(RuntimeError):
    """Nothing on this machine can read this file."""


class Sheet:
    """One sheet, read but not yet rendered.

    A plain class and NOT a dataclass: `pack_kit.learn` loads this file with
    `exec_module` without registering it in `sys.modules`, and `@dataclass`
    looks its own module up there — the import then fails with "'NoneType'
    object has no attribute '__dict__'" and not one sheet is read.
    """

    def __init__(self, header: list | None = None, body: str = "", warning: str = ""):
        #: The header table: rows -> cells -> paragraphs. Empty when the sheet
        #: does not open with a table.
        self.header: list[list[list[str]]] = header or []
        #: Everything after the header, one paragraph per line, with a
        #: placeholder (see `_placeholder`) wherever a picture stood.
        self.body = body
        #: The embedded pictures, in the order they appear: (suffix, bytes).
        self.images: list[tuple[str, bytes]] = []
        #: The yellow fragments that were removed.
        self.dropped: list[str] = []
        #: Something the reader of the sheet must be told (a degraded conversion).
        self.warning = warning


# ---------------------------------------------------------------------------
# Reading a .docx
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

#: Property blocks. They are skipped whole: `<w:tabs><w:tab .../></w:tabs>`
#: declares tab STOPS with the very element name a typed tab uses.
_PROPERTIES = {_W + n for n in ("pPr", "rPr", "tblPr", "tblGrid", "trPr", "tcPr", "sectPr")}

#: The two ways Word (and LibreOffice, converting a `.doc`) write a yellow
#: highlight: the highlighter pen, and a character shading of pure yellow.
_YELLOW_FILLS = {"FFFF00", "YELLOW"}


def _placeholder(index: int) -> str:
    return f"\x00{index}\x00"


_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _is_yellow(run: ET.Element) -> bool:
    props = run.find(_W + "rPr")
    if props is None:
        return False
    highlight = props.find(_W + "highlight")
    if highlight is not None and highlight.get(_W + "val", "").lower() == "yellow":
        return True
    shading = props.find(_W + "shd")
    return shading is not None and shading.get(_W + "fill", "").upper() in _YELLOW_FILLS


class _DocxReader:
    """Walks `word/document.xml` in reading order."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.names = set(archive.namelist())
        self.targets = self._relationships()
        self.sheet = Sheet()
        self._image_index: dict[str, int] = {}

    def _relationships(self) -> dict[str, str]:
        """Relationship id -> part name inside the archive, embedded parts only."""
        try:
            root = ET.fromstring(self.archive.read("word/_rels/document.xml.rels"))
        except KeyError:
            return {}
        out: dict[str, str] = {}
        for rel in root.iter(_REL + "Relationship"):
            if rel.get("TargetMode") == "External":
                continue        # a LINKED picture: there is nothing to keep
            target = rel.get("Target", "")
            name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                posixpath.join("word", target))
            out[rel.get("Id", "")] = name
        return out

    def _image(self, rel_id: str | None) -> str:
        name = self.targets.get(rel_id or "")
        if not name or name not in self.names:
            return ""
        if name not in self._image_index:
            self.sheet.images.append((Path(name).suffix.lower(), self.archive.read(name)))
            self._image_index[name] = len(self.sheet.images) - 1
        return _placeholder(self._image_index[name])

    def _text(self, element: ET.Element, out: list[str], top: bool = True) -> None:
        tag = element.tag
        if tag in _PROPERTIES or tag == _MC + "Fallback":
            # Fallback: Word writes a drawing twice, once for itself and once
            # for older readers. Reading both would double every picture.
            return
        if tag == _W + "r" and _is_yellow(element):
            dropped: list[str] = []
            for child in element:
                self._text(child, dropped, top=False)
            fragment = _PLACEHOLDER_RE.sub("", "".join(dropped)).strip()
            if fragment:
                self.sheet.dropped.append(fragment)
            return
        if tag == _W + "t":
            out.append(element.text or "")
        elif tag == _W + "tab":
            out.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            out.append("\n")
        elif tag == _W + "noBreakHyphen":
            out.append("-")
        elif tag == _A + "blip":
            out.append(self._image(element.get(_R + "embed")))
        elif tag == _V + "imagedata":
            out.append(self._image(element.get(_R + "id")))
        for child in element:
            self._text(child, out, top=False)
        if tag == _W + "p" and not top:
            out.append("\n")        # a paragraph inside a text box

    def _paragraph(self, paragraph: ET.Element) -> str:
        out: list[str] = []
        self._text(paragraph, out)
        return "".join(out)

    def _table(self, table: ET.Element) -> list[list[list[str]]]:
        rows = []
        for row in table.findall(_W + "tr"):
            cells = []
            for cell in row.findall(_W + "tc"):
                cells.append([self._paragraph(p) for p in cell.iter(_W + "p")])
            rows.append(cells)
        return rows

    def _blocks(self, parent: ET.Element, lines: list[str]) -> None:
        for element in parent:
            if element.tag == _W + "p":
                lines.append(self._paragraph(element))
            elif element.tag == _W + "tbl":
                rows = self._table(element)
                if not self.sheet.header and not any(line.strip() for line in lines):
                    # The table the sheet OPENS with is its header. A table
                    # further down is part of the notes, and stays in them.
                    self.sheet.header = rows
                    continue
                for row in rows:
                    lines.append(" | ".join(" ".join(" ".join(p.split()) for p in cell if p.strip())
                                            for cell in row))
            elif element.tag == _W + "sdt":
                content = element.find(_W + "sdtContent")
                if content is not None:
                    self._blocks(content, lines)

    def read(self) -> Sheet:
        root = ET.fromstring(self.archive.read("word/document.xml"))
        body = root.find(_W + "body")
        lines: list[str] = []
        if body is not None:
            self._blocks(body, lines)
        self.sheet.body = "\n".join(lines)
        return self.sheet


def _read_docx(path: Path) -> Sheet:
    with zipfile.ZipFile(path) as archive:
        return _DocxReader(archive).read()


# ---------------------------------------------------------------------------
# Turning a legacy .doc into a .docx
# ---------------------------------------------------------------------------

_SOFFICE_PLACES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def _soffice() -> str:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((p for p in _SOFFICE_PLACES if Path(p).is_file()), "")


def _doc_to_docx_libreoffice(path: Path, workdir: Path) -> Path | None:
    soffice = _soffice()
    if not soffice:
        return None
    outdir = workdir / hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    outdir.mkdir(parents=True, exist_ok=True)
    # Its own profile: a headless conversion launched while somebody has
    # LibreOffice open hands the job to that instance and writes nothing.
    profile = (workdir / "lo-profile").as_uri()
    try:
        subprocess.run(
            [soffice, f"-env:UserInstallation={profile}", "--headless",
             "--convert-to", "docx", "--outdir", str(outdir), str(path)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = outdir / (path.stem + ".docx")
    return out if out.is_file() else None


def _doc_to_docx_word(path: Path, workdir: Path) -> Path | None:
    """Through a hidden Word instance (Windows, pywin32)."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    outdir = workdir / hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / (path.stem + ".docx")
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(out), FileFormat=16)      # wdFormatDocumentDefault
            finally:
                doc.Close(False)
        finally:
            word.Quit()
    except Exception:
        return None
    finally:
        pythoncom.CoUninitialize()
    return out if out.is_file() else None


def _doc_text_only(path: Path) -> str:
    """The words of a `.doc` and nothing else — the last resort.

    `-w` matters: without it catdoc hard-wraps at ~72 characters, and a wrap
    lands mid-value. `-d utf-8` matters as much: the sheets are full of `°C`.
    """
    for command in (["catdoc", "-w", "-d", "utf-8"], ["antiword", "-w", "0"]):
        if not shutil.which(command[0]):
            continue
        out = subprocess.run(command + [str(path)], capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return out.stdout
        raise ExtractionUnavailable(f"{command[0]} failed: {out.stderr.strip()[:200]}")
    raise ExtractionUnavailable(
        "nothing here can read a legacy .doc. Install LibreOffice (preferred: it "
        "keeps the highlights and the pictures), or on Windows `pip install pywin32` "
        "next to Word."
    )


# ---------------------------------------------------------------------------
# The header: putting the labels back on the values
# ---------------------------------------------------------------------------

#: Each field, as the label written on the sheet. French and English, singular
#: and plural: the template has been retyped by several hands.
_LABELS: tuple[tuple[str, str], ...] = (
    ("description", r"description"),
    ("sample", r"(?:sample|[ée]chantillon)\s*(?:n\s*[°ºo.]?|number|#)?"),
    ("date", r"date"),
    ("substrate", r"substrat[e]?s?"),
    ("size", r"(?:sizes?|tailles?|dimensions?)\s*(?:\(\s*mm\s*\))?"),
    ("moly", r"moly\w*"),
    ("operator", r"op[ée]rat(?:or|eur)s?"),
)

_LABEL_RE = {
    name: re.compile(rf"^\s*(?:{pattern})(?![^\W\d_])\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    for name, pattern in _LABELS
}

#: Where the header stops and the growth starts, for a sheet that has NO table
#: (a text file, or a `.doc` read through catdoc). A `.docx` does not need it.
_BODY_START = re.compile(
    r"^\s*(pre[\s-]*growth|growth\b|croissance\b|p\s*ch\b|ts\b|power_ramp|temperature_ramp)",
    re.IGNORECASE,
)

#: `ech1-0764.doc` -> `1-0764`.
_SAMPLE_NUMBER = re.compile(r"(?<!\d)(\d+\s*[-_]\s*\d+)(?!\d)")

#: The order fields are written out in, with the label the extractor reads and
#: the unit where the sheet leaves it implicit: "15*15" becomes
#: "Substrate size (mm): 15*15".
_HEADER_OUT: tuple[tuple[str, str], ...] = (
    ("sample", "Sample number"),
    ("description", "Stack description"),
    ("date", "Date"),
    ("substrate", "Substrate"),
    ("size", "Substrate size (mm)"),
    ("moly", "Molyblock"),
    ("operator", "Operator(s)"),
)


def _clean(text: str) -> str:
    return " ".join(_PLACEHOLDER_RE.sub(" ", text).split())


def _label_of(text: str) -> tuple[str, str] | None:
    """(field, rest of the text) when *text* opens with a known label."""
    for name, pattern in _LABEL_RE.items():
        match = pattern.match(text)
        if match:
            return name, match.group(1).strip()
    return None


def _parse_header(rows: list[list[list[str]]], path: Path) -> tuple[dict[str, str], list[str]]:
    """(field -> value, cells that carry no known label), from the header table.

    A label's value is looked for in the cell itself, then in the cell to its
    right, then in the cell below — the three ways a small table gets filled.
    The operator is the one field the sheets often write with no label at all,
    in the cell after the molyblock; it is read there, and only there.
    """
    grid = [[[c for c in (_clean(p) for p in cell) if c] for cell in row] for row in rows]
    fields: dict[str, str] = {}
    used: set[tuple[int, int]] = set()
    sample_rest = ""
    moly_at: tuple[int, int] | None = None

    def free_value(r: int, c: int) -> str:
        if r >= len(grid) or c >= len(grid[r]) or (r, c) in used or not grid[r][c]:
            return ""
        if _label_of(grid[r][c][0]):
            return ""
        used.add((r, c))
        return " ".join(grid[r][c])

    for r, row in enumerate(grid):
        for c, paragraphs in enumerate(row):
            if not paragraphs or (r, c) in used:
                continue
            found = _label_of(paragraphs[0])
            if not found or found[0] in fields:
                continue
            name, first = found
            used.add((r, c))
            rest = paragraphs[1:]
            if name == "sample":
                # "Sample n° : 1-0812" then, in the same cell, the stack it is.
                value, sample_rest = first, " ".join(rest)
            else:
                value = " ".join([first] + rest).strip()
            value = value or free_value(r, c + 1) or free_value(r + 1, c)
            if value:
                fields[name] = value
            if name == "moly":
                moly_at = (r, c)

    if "description" not in fields and sample_rest:
        fields["description"] = sample_rest
        sample_rest = ""
    if "operator" not in fields and moly_at:
        value = free_value(moly_at[0], moly_at[1] + 1)
        if value:
            fields["operator"] = value

    extras = [sample_rest] if sample_rest else []
    extras += [" ".join(cell) for r, row in enumerate(grid)
               for c, cell in enumerate(row) if cell and (r, c) not in used]

    from_name = _SAMPLE_NUMBER.search(path.stem)
    if "sample" not in fields and from_name:
        fields["sample"] = from_name.group(1).replace(" ", "")
    return fields, extras


def _split_flat(text: str) -> tuple[list[list[list[str]]], str]:
    """(header as one-paragraph cells, body), for a sheet that has no table."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _BODY_START.match(line):
            header = [[[cell] for cell in row.split("\t")] for row in lines[:index]]
            return header, "\n".join(lines[index:])
    # No cue found: everything is body. Losing the growth would be worse than
    # losing the header, and a missing header shows in the output.
    return [], text


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_") or "sheet"


def _shown(path: Path) -> str:
    """The path as the marker writes it: relative to the pack when inside it."""
    try:
        return path.resolve().relative_to(PACK_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _place_images(sheet: Sheet, sample: str, source: Path, image_dir: Path, save: bool) -> str:
    """The body with every placeholder turned into ``[IMAGE: <path>]``.

    One directory per SAMPLE, file names prefixed by the SOURCE file: two files
    of the same sample then share a directory without overwriting each other.
    """
    if not sheet.images:
        return sheet.body
    folder = image_dir / _safe(sample)
    prefix = f"{_safe(source.stem)}_img"
    targets = [folder / f"{prefix}{i:02d}{suffix or '.bin'}"
               for i, (suffix, _) in enumerate(sheet.images, 1)]
    if save:
        folder.mkdir(parents=True, exist_ok=True)
        for target, (_, data) in zip(targets, sheet.images):
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
        # A picture removed from the sheet must not outlive it here.
        keep = {t.name for t in targets}
        for stale in folder.glob(f"{prefix}*"):
            if stale.name not in keep:
                stale.unlink()
    return _PLACEHOLDER_RE.sub(
        lambda m: f"[IMAGE: {_shown(targets[int(m.group(1))])}]", sheet.body)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _normalise_body(body: str) -> str:
    """Trailing whitespace gone, runs of blank lines down to one.

    One blank line and not none: `pack_kit.learn._chunks` splits a long page on
    blank lines, and a body collapsed to a single block could only ever be cut
    at a character count — mid-value.
    """
    out: list[str] = []
    for line in (raw.rstrip() for raw in body.replace("\xa0", " ").splitlines()):
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _render(path: Path, fields: dict[str, str], extras: list[str], body: str,
            sheet: Sheet) -> str:
    head = [f"Sample sheet: {path.name}"]
    head += [f"{label}: {fields[key]}" for key, label in _HEADER_OUT if fields.get(key)]
    head += [f"Other header cell, as written: {extra}" for extra in extras]
    missing = [label for key, label in _HEADER_OUT if not fields.get(key)]
    if missing:
        # Stated rather than left blank: a field nobody filled in is a fact
        # about the sheet, and an extractor told nothing would invent it.
        head.append(f"Header fields not filled in on this sheet: {', '.join(missing)}")
    from_name = _SAMPLE_NUMBER.search(path.stem)
    if from_name and fields.get("sample"):
        written = _SAMPLE_NUMBER.search(fields["sample"])
        name_says = re.sub(r"\s", "", from_name.group(1)).replace("_", "-")
        if written and re.sub(r"\s", "", written.group(1)).replace("_", "-") != name_says:
            head.append(f"Caution: the file name says sample {name_says}, the header says "
                        f"{fields['sample']} — a sheet copied from another and half edited?")
    if sheet.warning:
        head.append(sheet.warning)
    if sheet.images:
        head.append(
            f"This sheet embeds {len(sheet.images)} picture(s). Each stands in the notes "
            "below as a marker [IMAGE: <path>], at the place the picture was.")
    head.append(
        "The lines below are the operator's notes, free text, in the order they were "
        "written — which is the order things happened. A clock time or a chrono is the "
        "operator's when written; otherwise a line happens after the line above it. One "
        "step often spans several consecutive lines."
    )
    return "\n".join(head) + "\n\n" + body + "\n"


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------


def _read_sheet(path: Path, workdir: Path) -> Sheet:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".doc":
        converted = _doc_to_docx_libreoffice(path, workdir) or _doc_to_docx_word(path, workdir)
        if converted:
            return _read_docx(converted)
        header, body = _split_flat(_doc_text_only(path))
        return Sheet(header=header, body=body, warning=(
            "Caution: this sheet was read without its formatting. Template values the "
            "operator never filled in (yellow on the sheet) may still be present, and "
            "its pictures are missing."))
    header, body = _split_flat(path.read_text(encoding="utf-8", errors="replace"))
    return Sheet(header=header, body=body)


def render_sheet(path: Path, workdir: Path, image_dir: Path,
                 save_images: bool = True) -> tuple[str, str, Sheet]:
    """(rendered text, sample number, the sheet as read) for one file."""
    sheet = _read_sheet(path, workdir)
    fields, extras = _parse_header(sheet.header, path)
    sample = fields.get("sample") or path.stem
    number = _SAMPLE_NUMBER.search(sample)
    folder = re.sub(r"\s", "", number.group(1)) if number else sample
    body = _normalise_body(_place_images(sheet, folder, path, image_dir, save_images))
    return _render(path, fields, extras, body, sheet), sample, sheet


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, dict]:
    """key -> {digest, size, mtime, v}. A file written by an earlier version of
    this module holds bare digests; they are read, and simply never match."""
    if not HASH_FILE:
        return {}
    try:
        raw = json.loads(Path(HASH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: (v if isinstance(v, dict) else {"digest": v}) for k, v in raw.items()}


def _save_state(state: dict[str, dict]) -> None:
    if not HASH_FILE:
        return
    path = Path(HASH_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:      # never fail a run over its own bookkeeping
        print(f"    [hash-file] could not write {path}: {exc}")


# ---------------------------------------------------------------------------
# The entry point `sources.yaml` names
# ---------------------------------------------------------------------------

_READ_ERRORS = (ExtractionUnavailable, OSError, zipfile.BadZipFile, KeyError,
                ET.ParseError, subprocess.SubprocessError)


def fetch_sample_docs(
    directory: str = "",
    recursive: bool = True,
    detect_changes: bool = True,
    max_files: int = 0,
    image_dir: str = "",
    save_images: bool = True,
) -> list[dict]:
    """Every sample sheet under *directory*, as pages for the learn pipeline.

    *directory* is an argument and not a constant: the sheets live wherever the
    laboratory keeps them. *image_dir* is where the embedded pictures are
    written, one sub-directory per sample; it defaults to `data/images` in the
    pack.

    *detect_changes* writes its state as it goes, so a sheet whose INGESTION
    then fails is skipped by the next run. That is the same trade every fetcher
    in this platform makes; `--append` on `learn.sh` and `detect_changes: false`
    are the two ways out of it.
    """
    root = Path(directory or os.environ.get("SAMPLE_DOC_DIR", "")).expanduser()
    if not root.is_dir():
        return [{"url": str(root), "error": f"not a directory: {root}"}]
    images = Path(image_dir).expanduser() if image_dir else PACK_ROOT / "data" / "images"

    files = sorted(
        p for p in (root.rglob("*") if recursive else root.glob("*"))
        if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith("~$")
        # The pictures this module writes may live under the same directory.
        and images.resolve() not in p.resolve().parents
    )
    if max_files:
        files = files[:max_files]

    state = _load_state() if detect_changes else {}
    pages: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="sample_doc_") as tmp:
        for path in files:
            key = str(path.resolve())
            url = f"file://{key}"
            stat = path.stat()
            stamp = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "v": RENDER_VERSION}
            known = state.get(key, {})
            if detect_changes and all(known.get(k) == v for k, v in stamp.items()):
                pages.append({"url": url, "changed": False})
                continue

            try:
                content, sample, sheet = render_sheet(path, Path(tmp), images, save_images)
            except _READ_ERRORS as exc:
                pages.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
                continue

            if sheet.dropped:
                print(f"    [yellow] {path.name}: {len(sheet.dropped)} unfilled template "
                      f"value(s) removed — {' | '.join(sheet.dropped)[:200]}")
            if sheet.warning:
                print(f"    [degraded] {path.name}: read without highlights or pictures "
                      "(no LibreOffice, no Word)")

            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            unchanged = detect_changes and known.get("digest") == digest
            state[key] = {"digest": digest, **stamp}
            if unchanged:
                pages.append({"url": url, "changed": False})
                continue

            pages.append({
                "url": url,
                "content": content,
                # The section becomes the page title in the memory's provenance
                # header, so it must name the sample and not the file: two
                # sheets of one sample under two filenames are the same growth.
                "section": f"Sample {sample}",
                "changed": True,
            })

    if detect_changes:
        _save_state(state)
    return pages


if __name__ == "__main__":      # a human's check, never imported
    # python fetchers/sample_doc_fetcher.py <directory> [--images]
    # Prints what the extractor would read. Writes nothing unless --images.
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: sample_doc_fetcher.py <directory of sheets> [--images]")
    for page in fetch_sample_docs(args[0], detect_changes=False,
                                  save_images="--images" in sys.argv):
        print("=" * 70)
        print(page.get("url"))
        print(page.get("error") or page.get("content", ""))
