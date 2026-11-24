"""The user's personal knowledge base: their notes and documents, searchable by the assistant.

This is retrieval (Jay v2, the study RAG): before answering a question about the user's own
material, the assistant looks up the relevant passages here and answers from them, citing the
document and page. PDFs are read page by page, slides (pptx) slide by slide, Word files whole.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

from ..security import check_file_allowed, is_sensitive_path
from ..store import Store
from .registry import Registry

MAX_DOCUMENT = 2_000_000   # characters
FOLDER_SUFFIXES = {".pdf", ".txt", ".md", ".docx", ".pptx", ".py", ".java", ".c", ".cpp", ".ipynb"}
MAX_FOLDER_FILES = 200


def _path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _xml_text(xml: str) -> str:
    paragraphs = re.findall(r"<a:p[ >].*?</a:p>|<w:p[ >].*?</w:p>", xml, flags=re.S) or [xml]
    lines = []
    for para in paragraphs:
        text = "".join(re.findall(r"<(?:a|w):t[^>]*>([^<]*)</(?:a|w):t>", para))
        if text.strip():
            lines.append(re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", text))).strip())
    return "\n".join(lines)


def read_pages(path: str) -> list[tuple[str, str]]:
    """The document as (page label, text) pieces, so answers can cite where they came from."""
    p = _path(path)
    check_file_allowed(p)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return [(f"page {i}", page.extract_text() or "") for i, page in enumerate(PdfReader(str(p)).pages, 1)]
    if suffix == ".pptx":
        with zipfile.ZipFile(p) as z:
            slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            return [(f"slide {i}", _xml_text(z.read(n).decode("utf-8", "replace"))) for i, n in enumerate(slides, 1)]
    if suffix == ".docx":
        with zipfile.ZipFile(p) as z:
            return [("", _xml_text(z.read("word/document.xml").decode("utf-8", "replace")))]
    if suffix == ".ipynb":
        import json

        cells = json.loads(p.read_text(encoding="utf-8", errors="replace")).get("cells", [])
        return [(f"cell {i}", "".join(c.get("source", []))) for i, c in enumerate(cells, 1)]
    return [("", p.read_text(encoding="utf-8", errors="replace"))]


def read_document(path: str) -> str:
    return "\n\n".join(text for _, text in read_pages(path))


def learn_file(store: Store, path: str, name: str | None = None) -> tuple[str, int]:
    """Read one file into the knowledge base. Returns (name, passages) or raises ValueError."""
    pages = read_pages(path)
    total = sum(len(t) for _, t in pages)
    if not total or not any(t.strip() for _, t in pages):
        raise ValueError("no readable text (a scanned PDF needs OCR first)")
    if total > MAX_DOCUMENT:
        raise ValueError(f"{total:,} characters, over the {MAX_DOCUMENT:,} limit")
    name = name or _path(path).name
    return name, store.add_knowledge(name, pages=pages)


def format_hits(rows) -> str:
    out = []
    for r in rows:
        where = f"{r['source']}, {r['page']}" if r["page"] else r["source"]
        out.append(f"[{where}]\n{r['passage']}")
    return "\n\n---\n\n".join(out)


def register(reg: Registry, store: Store) -> None:
    @reg.tool(
        "search_knowledge",
        "Search the user's own notes and documents (course material, slides, project notes, anything "
        "they asked you to learn or note down) and return the most relevant passages, each labelled "
        "with its source and page. Use it before answering any question their own material could "
        "answer, answer from what it returns, and cite as (source, page). If nothing relevant comes "
        "back, say their notes don't cover it rather than answering from memory as if they did.",
        {"query": {"type": "string", "description": "Key words to look for (the concepts, not the whole question)."},
         "source": {"type": "string", "description": "Optional: only documents whose name contains this."},
         "limit": {"type": "integer", "description": "How many passages (default 5, up to 10)."}},
        required=("query",),
    )
    def search_knowledge(query: str, source: str = "", limit: int = 5) -> str:
        rows = store.search_knowledge(query, limit=max(1, min(int(limit), 10)), source=source or None)
        if not rows:
            return "Nothing in the knowledge base matches. (Use list_knowledge to see what is stored.)"
        return format_hits(rows)

    @reg.tool(
        "read_document",
        "Read the text of a file on the PC (PDF, slides, Word, text, markdown) and return it, up to 30,000 "
        "characters from the given offset. Use it to read a timetable, plan or letter the user "
        "points you to; use learn_document to keep it for later searching.",
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "Character to start from (default 0)."}},
        required=("path",),
    )
    def read_document_tool(path: str, offset: int = 0) -> str:
        text = read_document(path)
        if not text.strip():
            return "That file has no readable text (a scanned PDF needs OCR first)."
        chunk = text[offset:offset + 30_000]
        left = len(text) - offset - len(chunk)
        more = f"\n[{left} more characters: call again with offset={offset + len(chunk)}]" if left > 0 else ""
        return chunk + more

    @reg.tool(
        "learn_document",
        "Read a file from the PC (PDF, PowerPoint slides, Word, text, markdown, code) into the knowledge "
        "base page by page so you can answer questions from it later and cite the page. Learning the "
        "same file again replaces the old copy.",
        {"path": {"type": "string"}},
        required=("path",),
    )
    def learn_document(path: str) -> str:
        try:
            name, count = learn_file(store, path)
        except ValueError as problem:
            return f"Couldn't learn that file: {problem}."
        return f"Learned '{name}': {count} passages stored."

    @reg.tool(
        "learn_folder",
        "Learn every note file in a folder (and its sub-folders): PDFs, slides, Word files, text, "
        f"markdown and code, up to {MAX_FOLDER_FILES} files. Use it for a whole subject's notes.",
        {"path": {"type": "string", "description": "The folder."}},
        required=("path",),
    )
    def learn_folder(path: str) -> str:
        root = _path(path)
        if not root.is_dir():
            return f"There is no folder at {path}."
        files = [f for f in sorted(root.rglob("*")) if f.is_file() and f.suffix.lower() in FOLDER_SUFFIXES
                 and "node_modules" not in f.parts and ".git" not in f.parts and not is_sensitive_path(f)][:MAX_FOLDER_FILES]
        done, skipped = [], []
        for f in files:
            try:
                name, count = learn_file(store, str(f), name=f"{root.name}/{f.relative_to(root).as_posix()}")
                done.append(f"{name} ({count})")
            except Exception as problem:
                skipped.append(f"{f.name}: {str(problem)[:60]}")
        text = f"Learned {len(done)} files from '{root.name}'."
        if skipped:
            text += f" Skipped {len(skipped)}: " + "; ".join(skipped[:8])
        return text

    @reg.tool(
        "save_note",
        "Write something down in the knowledge base: meeting notes, an idea, a summary of what you "
        "found online, a daily digest. Saving under an existing title replaces that note.",
        {"title": {"type": "string"}, "text": {"type": "string"}},
        required=("title", "text"),
    )
    def save_note(title: str, text: str) -> str:
        store.add_knowledge(f"note: {title}", text)
        return f"Saved note '{title}'."

    @reg.tool("list_knowledge", "List what is stored in the knowledge base (documents and notes).")
    def list_knowledge() -> str:
        rows = store.knowledge_sources()
        return "\n".join(f"{r['source']} ({r['passages']} passages)" for r in rows) or "The knowledge base is empty."

    @reg.tool(
        "remove_knowledge",
        "Delete a document or note from the knowledge base by its exact name from list_knowledge.",
        {"source": {"type": "string"}},
        required=("source",),
    )
    def remove_knowledge(source: str) -> str:
        return "Removed." if store.remove_knowledge(source) else f"Nothing stored as '{source}'."
