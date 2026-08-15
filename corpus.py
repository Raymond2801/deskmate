"""The document store: in-memory corpus backed by the data/ directory."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import ingest

logger = logging.getLogger("deskmate.corpus")

DATA_DIR = Path("data")
DOCS_DIR = DATA_DIR / "docs"
ARCHIVE_DIR = DATA_DIR / "archive"
CORPUS_FILE = DATA_DIR / "corpus.json"
DEMO_DOCS_DIR = Path("demo-corpus") / "docs"


@dataclass
class Document:
    filename: str
    extracted_text: str
    ingested_at: str
    byte_size: int
    source: str = "upload"  # "upload" or "demo"

    @property
    def word_count(self) -> int:
        return ingest.word_count(self.extracted_text)


class CorpusFull(Exception):
    pass


class Corpus:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}

    @property
    def documents(self) -> list[Document]:
        return sorted(self._documents.values(), key=lambda d: d.filename)

    def __len__(self) -> int:
        return len(self._documents)

    def total_word_count(self) -> int:
        return sum(doc.word_count for doc in self._documents.values())

    def get(self, filename: str) -> Document | None:
        return self._documents.get(filename)

    def has_source(self, filename: str) -> bool:
        return filename in self._documents

    @classmethod
    def load(cls) -> "Corpus":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        corpus = cls()

        if not any(DOCS_DIR.iterdir()) and DEMO_DOCS_DIR.exists():
            corpus._seed_demo_corpus()
        elif CORPUS_FILE.exists():
            corpus._load_from_disk()
        else:
            corpus._rebuild_from_docs_dir()

        return corpus

    def _seed_demo_corpus(self) -> None:
        logger.info("data/docs is empty, seeding demo corpus from %s", DEMO_DOCS_DIR)
        for demo_file in sorted(DEMO_DOCS_DIR.iterdir()):
            if not demo_file.is_file():
                continue
            raw_bytes = demo_file.read_bytes()
            dest = DOCS_DIR / demo_file.name
            dest.write_bytes(raw_bytes)
            text = ingest.extract_text(dest)
            self._documents[demo_file.name] = Document(
                filename=demo_file.name,
                extracted_text=text,
                ingested_at=_now_iso(),
                byte_size=len(raw_bytes),
                source="demo",
            )
        self.save()

    def _rebuild_from_docs_dir(self) -> None:
        for path in sorted(DOCS_DIR.iterdir()):
            if not path.is_file():
                continue
            text = ingest.extract_text(path)
            self._documents[path.name] = Document(
                filename=path.name,
                extracted_text=text,
                ingested_at=_now_iso(),
                byte_size=path.stat().st_size,
                source="upload",
            )
        self.save()

    def _load_from_disk(self) -> None:
        raw = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
        for entry in raw:
            doc = Document(**entry)
            self._documents[doc.filename] = doc

    def save(self) -> None:
        payload = [asdict(doc) for doc in self.documents]
        CORPUS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_document(self, filename: str, raw_bytes: bytes) -> Document:
        if len(raw_bytes) > ingest.MAX_FILE_BYTES:
            raise ingest.FileTooLarge(
                f"{filename} is {len(raw_bytes)} bytes, over the {ingest.MAX_FILE_BYTES} byte limit"
            )
        if filename not in self._documents and len(self._documents) >= ingest.MAX_FILES:
            raise CorpusFull(f"Corpus already has {ingest.MAX_FILES} files, the maximum allowed")

        if filename in self._documents:
            self._archive_existing(filename)

        dest = DOCS_DIR / filename
        dest.write_bytes(raw_bytes)
        text = ingest.extract_text(dest)

        doc = Document(
            filename=filename,
            extracted_text=text,
            ingested_at=_now_iso(),
            byte_size=len(raw_bytes),
            source="upload",
        )
        self._documents[filename] = doc
        self.save()
        return doc

    def _archive_existing(self, filename: str) -> None:
        existing_path = DOCS_DIR / filename
        if existing_path.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stem, suffix = existing_path.stem, existing_path.suffix
            archived_path = ARCHIVE_DIR / f"{stem}.{timestamp}{suffix}"
            shutil.move(str(existing_path), str(archived_path))
            logger.info("Archived previous %s to %s", filename, archived_path)

    def reset_demo(self) -> list[str]:
        """Remove documents that came from the shipped demo corpus. Returns
        the list of filenames removed."""
        removed = []
        for filename, doc in list(self._documents.items()):
            if doc.source == "demo":
                self._archive_existing(filename)
                del self._documents[filename]
                removed.append(filename)
        self.save()
        return removed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
