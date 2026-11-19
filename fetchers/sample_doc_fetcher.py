"""Read a directory of MBE sample sheets and hand them to `pack_kit.learn`.

Declared in `sources.yaml` as
``fetcher: "{here}/fetchers/sample_doc_fetcher.py::fetch_sample_docs"``; the
contract is the one `pack_kit.learn._pages_from_fetcher` reads — a list of dicts
carrying ``url``, ``content``, ``section``, ``changed`` and ``error``.

A hand-written fetcher rather than the generic crawler for three reasons, and
each of them is the reason the same job done generically produces a poor memory:

1. **A `.doc` is not text.** These files are OLE2 compound documents written by
   Word; read as bytes they are mostly binary. The extraction runs through
   `catdoc` (or `antiword`, or LibreOffice) — see `_extract_text`.
2. **The header is a TABLE, and a table flattens badly.** Description, sample
   number, date, substrate, size, molyblock and operator come out of the
   converter as a run of cells with no labels attached to their values. Left as
   they are, an extractor reads "15*15" with nothing saying it is a substrate
   size in millimetres. `_parse_header` puts the labels back, once, here — a
   deterministic parse of a fixed layout, which is exactly the work that must
   not be paid for per page in LLM calls.
3. **Change detection.** A sheet that has not moved since the last run is
   skipped before any LLM call. Sheets are edited long after the growth (the
   two reference files were saved months after their creation date), so a weekly
   re-run over a growing directory must not re-ingest everything.

The hash is taken on the EXTRACTED TEXT, never on the file bytes: re-saving a
`.doc` rewrites its metadata — editing time, revision number — and would report
every sheet as changed while none of its content had.

What this module does NOT do: it never reorders, groups or interprets the body.
The sheets carry no timestamps; their only chronology is the order the lines are
written in, and any regrouping here would be this module's own reading of the
growth passed off as the operator's. The eventual machine-state CSV is what will
put real times against these steps — see the pack's README.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

#: Where the per-file content hashes are kept. Set in `sources.yaml` under
#: `fetcher_env:`, read at import exactly as agent-slapsci's `env:` block does
#: for a native tool.
HASH_FILE = os.environ.get("SAMPLE_DOC_HASH_FILE", "")

#: The suffixes recognised. `.docx` is here because a directory of sheets
#: acquires one the first time somebody saves with a recent Word, and a fetcher
#: that silently ignored it would leave a sample out of the memory with no error.
SUFFIXES = (".doc", ".docx", ".txt", ".md")


# ---------------------------------------------------------------------------
# Turning a Word document into text
# ---------------------------------------------------------------------------


class ExtractionUnavailable(RuntimeError):
    """No converter on this machine can read a legacy `.doc`."""


def _extract_doc(path: Path) -> str:
    """Text of a legacy `.doc`, through whichever converter is installed.

    `-w` matters: without it catdoc hard-wraps at ~72 characters, and a wrap
    lands mid-value — the reference sheets break `y = 10 -13` across two lines.
    A number split in half is not a number, and nothing downstream can tell.

    `-d utf-8` matters as much: the sheets are full of `°C` and of the en dash
    the operators use as a separator, and the default output encoding turns
    both into mojibake the extractor then copies into the graph.
    """
    if shutil.which("catdoc"):
        out = subprocess.run(
            ["catdoc", "-w", "-d", "utf-8", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            return out.stdout
        raise ExtractionUnavailable(f"catdoc failed: {out.stderr.strip()[:200]}")

    if shutil.which("antiword"):
        out = subprocess.run(
            ["antiword", "-w", "0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            return out.stdout
        raise ExtractionUnavailable(f"antiword failed: {out.stderr.strip()[:200]}")

    if sys.platform == "win32":
        # Neither converter is on PATH — the case on a machine where antiword
        # lives inside Git's own mingw64\bin, never added to the system PATH.
        # Office is what actually reads a .doc reliably here, so automate it
        # rather than ask a lab machine to install a Linux-oriented tool.
        return _extract_doc_via_word(path)

    raise ExtractionUnavailable(
        "no .doc converter found. Install one: `sudo apt install catdoc` "
        "(the one this fetcher is written for) or `antiword`."
    )


def _extract_doc_via_word(path: Path) -> str:
    """Text of a `.doc`, through a hidden Word instance (Windows, pywin32).

    Word marks a cell end with `\\a` and a paragraph end with `\\r`; both are
    normalised to the tab/newline shape `_cells` and `_split_header` already
    expect from `_extract_docx`, so the header parses the same way regardless
    of which path extracted it.
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ExtractionUnavailable(
            "no .doc converter found, and pywin32 is not installed for the "
            "Word fallback. Install one: `sudo apt install catdoc` (the one "
            "this fetcher is written for), `antiword`, or `pip install pywin32`."
        ) from exc

    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.DispatchEx("Word.Application")
        except Exception as exc:
            raise ExtractionUnavailable(f"could not start Word: {exc}") from exc
        word.Visible = False
        try:
            try:
                doc = word.Documents.Open(
                    str(path), ReadOnly=True, AddToRecentFiles=False,
                )
            except Exception as exc:
                raise ExtractionUnavailable(f"Word could not open {path.name}: {exc}") from exc
            try:
                text = doc.Content.Text
            finally:
                doc.Close(False)
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()

    return text.replace("\r\a", "\n").replace("\a", "\t").replace("\r", "\n")


#: A `.docx` paragraph break. The XML is unzipped and stripped rather than
#: parsed with python-docx: one dependency for one regex is not a trade worth
#: making, and the sheets carry no styling anyone reads.
_DOCX_BREAK = re.compile(r"</w:p>|<w:br\s*/>|</w:tr>")
_XML_TAG = re.compile(r"<[^>]+>")
_DOCX_CELL = re.compile(r"</w:tc>")


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
    # A cell boundary becomes a tab, so the header table flattens the same way
    # catdoc flattens it and `_parse_header` has one shape to handle, not two.
    xml = _DOCX_CELL.sub("\t</w:tc>", xml)
    xml = _DOCX_BREAK.sub("\n", xml)
    text = _XML_TAG.sub("", xml)
    return (
        text.replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
    )


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".doc":
        return _extract_doc(path)
    if suffix == ".docx":
        return _extract_docx(path)
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The header: putting the labels back on the values
# ---------------------------------------------------------------------------

#: Each field, as the label written on the sheet. The value is either on the
#: same cell after the label, or in the next cell — a table reads either way
#: depending on where the operator pressed Tab, so both are accepted.
_LABELS: tuple[tuple[str, str], ...] = (
    ("description", r"description"),
    ("sample", r"sample\s*n\s*[°ºo]?|échantillon\s*n\s*[°ºo]?|echantillon\s*n\s*[°ºo]?"),
    ("date", r"date"),
    ("substrate", r"substrate|substrat"),
    ("size", r"size\s*\(?\s*mm\s*\)?|taille"),
    ("moly", r"moly\w*"),
    ("operator", r"operator|op[ée]rateur"),
)

_LABEL_RE = {
    field: re.compile(rf"^\s*(?:{pattern})\s*[:\-]?\s*(.*)$", re.IGNORECASE)
    for field, pattern in _LABELS
}

#: Where the header stops and the growth itself starts. Anchored on what the
#: sheets actually open with; a sheet that opens otherwise simply keeps its
#: first lines in the body, which is visible, rather than losing them.
_BODY_START = re.compile(
    r"^\s*(pre[\s-]*growth|growth\b|croissance\b|p\s*ch\b|ts\b|power_ramp|temperature_ramp)",
    re.IGNORECASE,
)

#: `ech1-0764.doc` -> `1-0764`. A fallback for the sample number, and a
#: cross-check on the parsed one: a sheet copied from another and half-edited is
#: the failure this catches, and it is silent in every other respect.
_FROM_FILENAME = re.compile(r"(\d+\s*[-_]\s*\d+)")


def _cells(text: str) -> list[str]:
    """The header region as a flat list of non-empty cells, in reading order."""
    out: list[str] = []
    for line in text.splitlines():
        for cell in line.split("\t"):
            cell = " ".join(cell.split())
            if cell:
                out.append(cell)
    return out


def _split_header(text: str) -> tuple[str, str]:
    """(header, body). The body keeps every line from the first growth cue on."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _BODY_START.match(line):
            return "\n".join(lines[:index]), "\n".join(lines[index:])
    # No cue found: treat everything as body. Losing the growth would be worse
    # than losing the header, and an empty header shows in the output.
    return "", text


def _parse_header(header: str, path: Path) -> dict[str, str]:
    """Field -> value, from the flattened header table.

    The operator's name is the one field the sheets write with no label at all —
    it sits in the cell after the molyblock. It is read positionally for that
    reason, and only when no `Operator:` label was found.
    """
    cells = _cells(header)
    fields: dict[str, str] = {}
    moly_at = -1

    index = 0
    while index < len(cells):
        cell = cells[index]
        for field, pattern in _LABEL_RE.items():
            if field in fields:
                continue
            match = pattern.match(cell)
            if not match:
                continue
            value = match.group(1).strip()
            if not value and index + 1 < len(cells):
                # The label was alone in its cell: the value is the next one,
                # unless that next cell is itself a label.
                nxt = cells[index + 1]
                if not any(p.match(nxt) for p in _LABEL_RE.values()):
                    value = nxt
                    index += 1
            if value:
                fields[field] = value
                if field == "moly":
                    moly_at = index
            break
        index += 1

    if "operator" not in fields and 0 <= moly_at < len(cells) - 1:
        candidate = cells[moly_at + 1]
        if not any(p.match(candidate) for p in _LABEL_RE.values()):
            fields["operator"] = candidate

    from_name = _FROM_FILENAME.search(path.stem)
    if "sample" not in fields and from_name:
        fields["sample"] = from_name.group(1).replace(" ", "")
    return fields


#: The order fields are written out in, with the label the extractor reads and
#: the unit where the sheet leaves it implicit. This mapping is the whole reason
#: the fetcher exists: "15*15" becomes "Substrate size (mm): 15*15".
_HEADER_OUT: tuple[tuple[str, str], ...] = (
    ("sample", "Sample number"),
    ("description", "Stack description"),
    ("date", "Date"),
    ("substrate", "Substrate"),
    ("size", "Substrate size (mm)"),
    ("moly", "Molyblock"),
    ("operator", "Operator"),
)


def _normalise_body(body: str) -> str:
    """Trailing whitespace gone, runs of blank lines down to one.

    One blank line and not none: `pack_kit.learn._chunks` splits a long page on
    blank lines, and a body collapsed to a single block could only ever be cut
    at a character count — mid-value, which is the failure this fetcher already
    avoids at the converter.
    """
    lines = [line.rstrip() for line in body.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _render(path: Path, fields: dict[str, str], body: str) -> str:
    head = [f"Sample sheet: {path.name}"]
    head += [f"{label}: {fields[key]}" for key, label in _HEADER_OUT if fields.get(key)]
    missing = [label for key, label in _HEADER_OUT if not fields.get(key)]
    if missing:
        # Stated rather than left blank: a field this parser failed to find is a
        # fact about the sheet, and an extractor told nothing would invent it.
        head.append(f"Header fields not found on this sheet: {', '.join(missing)}")
    head.append(
        "The lines below are the growth itself, in the order the operator wrote "
        "them, which is chronological. These sheets carry no timestamps: a line "
        "happens after the line above it and before the line below it, and a "
        "single step may span several consecutive lines (a cell, then its "
        "temperature and pressure, then its duration and thickness)."
    )
    return "\n".join(head) + "\n\n" + body + "\n"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _load_hashes() -> dict[str, str]:
    if not HASH_FILE:
        return {}
    try:
        return json.loads(Path(HASH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_hashes(hashes: dict[str, str]) -> None:
    if not HASH_FILE:
        return
    path = Path(HASH_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hashes, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:      # never fail a run over its own bookkeeping
        print(f"    [hash-file] could not write {path}: {exc}")


# ---------------------------------------------------------------------------
# The entry point `sources.yaml` names
# ---------------------------------------------------------------------------


def fetch_sample_docs(
    directory: str = "",
    recursive: bool = True,
    detect_changes: bool = True,
    max_files: int = 0,
) -> list[dict]:
    """Every sample sheet under *directory*, as pages for the learn pipeline.

    *directory* is an argument and not a constant: the sheets live wherever the
    laboratory keeps them, and a path baked into this file would have to be
    edited to run the pipeline over a second directory. `sources.yaml` states
    the one in use.

    *detect_changes* writes the hashes as it goes, so a sheet whose INGESTION
    then fails is skipped by the next run. That is the same trade every fetcher
    in this platform makes; `--append` on `learn.sh` and `detect_changes: false`
    are the two ways out of it.
    """
    root = Path(directory or os.environ.get("SAMPLE_DOC_DIR", "")).expanduser()
    if not root.is_dir():
        return [{"url": str(root), "error": f"not a directory: {root}"}]

    files = sorted(
        p for p in (root.rglob("*") if recursive else root.glob("*"))
        if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith("~$")
    )
    if max_files:
        files = files[:max_files]

    hashes = _load_hashes() if detect_changes else {}
    pages: list[dict] = []

    for path in files:
        key = str(path.resolve())
        try:
            raw = _extract_text(path)
        except (ExtractionUnavailable, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
            pages.append({"url": f"file://{key}", "error": f"{type(exc).__name__}: {exc}"})
            continue

        header, body = _split_header(raw)
        fields = _parse_header(header, path)
        content = _render(path, fields, _normalise_body(body))

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if detect_changes and hashes.get(key) == digest:
            pages.append({"url": f"file://{key}", "changed": False})
            continue
        hashes[key] = digest

        pages.append({
            "url": f"file://{key}",
            "content": content,
            # The section becomes the page title in the memory's provenance
            # header, so it must name the sample and not the file: two sheets of
            # one sample under two filenames are the same growth.
            "section": f"Sample {fields.get('sample', path.stem)}",
            "changed": True,
        })

    if detect_changes:
        _save_hashes(hashes)
    return pages


if __name__ == "__main__":      # a human's check, never imported
    import sys

    where = sys.argv[1] if len(sys.argv) > 1 else "/home/yopla/SampleDocFiles"
    for page in fetch_sample_docs(where, detect_changes=False):
        print("=" * 70)
        print(page.get("url"))
        print(page.get("error") or page.get("content", "")[:4000])
