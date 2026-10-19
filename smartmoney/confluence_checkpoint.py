"""Private, bounded issuer checkpoints for an unfinished CLI refresh.

Only SEC listing metadata is stored here. XML is revalidated from the separate
document cache before reuse; scores are always rebuilt from the current 13F scope.
"""
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


class ConfluencePending(Exception):
    """The work budget expired normally; nothing has been published."""


def utc_now():
    return datetime.now(timezone.utc)


def atomic_json(path, value):
    if path.is_symlink():
        raise ValueError("Confluence checkpoint must not be a symlink")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".checkpoint-", delete=False) as output:
            temporary = output.name
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class ConfluenceCheckpoint:
    def __init__(self, directory, budget_seconds=3000):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise ValueError("Confluence checkpoint directory must not be a symlink")
        self.directory.mkdir(parents=True, mode=0o750, exist_ok=True)
        if not 1 <= budget_seconds <= 6000:
            raise ValueError("Confluence work budget must be between 1 and 6000 seconds")
        self.deadline = time.monotonic() + budget_seconds
        self.state = None

    @contextmanager
    def locked(self, *, skip_busy=False):
        # The 13F ingestion shell takes the same lock. Splitting the jobs must
        # not multiply their combined SEC request rate or race a DB rewrite.
        fd = os.open(self.directory.parent / "edgar-ingest.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if skip_busy:
                    yield False
                    return
                raise RuntimeError("SEC ingestion is already running") from None
            yield True

    def check_budget(self):
        if time.monotonic() >= self.deadline:
            raise ConfluencePending("Work slice complete; issuer checkpoints retained, public caches unchanged.")

    def _read(self, name):
        path = self.directory / name
        if path.is_symlink():
            raise ValueError("Confluence checkpoint must not be a symlink")
        try:
            with path.open("rb") as source:
                raw = source.read(65537)
            if len(raw) <= 65536:
                value = json.loads(raw)
                return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, UnicodeError):
            pass
        return {}

    def prepare(self, context):
        digest = hashlib.sha256(json.dumps(context, sort_keys=True, allow_nan=False).encode()).hexdigest()
        state = self._read("batch.json")
        if (state.get("context") != digest or state.get("complete") is not False
                or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("id", "")))):
            state = {"context": digest, "id": uuid.uuid4().hex, "complete": False}
            atomic_json(self.directory / "batch.json", state)
        self.state = state

    def load(self, cik, client, window_days, as_of):
        record = self._read(self._name(cik))
        excluded_count = len(client.non_issuer_filings)
        cached_count = client.filings_cached
        try:
            if record["batch"] != self.state["id"]:
                return None
            checked = datetime.fromisoformat(record["checked_at"])
            # Leave two hours of margin for final calculation/publication.
            if checked.tzinfo is None or not 0 <= (utc_now() - checked).total_seconds() <= 24 * 3600:
                return None
            since = as_of - timedelta(days=window_days)
            if date.fromisoformat(record["since"]) > since:
                return None
            metas = record["filings"]
            if not isinstance(metas, list) or len(metas) > 60:
                return None
            seen = set()
            for meta in metas:
                accession = meta["accession"]
                if (not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
                        or accession in seen):
                    return None
                seen.add(accession)
                date.fromisoformat(meta["filing_date"])
            selected = [meta for meta in metas if date.fromisoformat(meta["filing_date"]) >= since]
            forms = client._parse_filings(cik, selected, strict=True, cached_only=True)
            return forms, checked.astimezone(timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, AttributeError):
            del client.non_issuer_filings[excluded_count:]
            client.filings_cached = cached_count
            return None

    def save(self, cik, listing):
        # Called only after EVERY filing in this list has passed strict validation.
        atomic_json(self.directory / self._name(cik), {"batch": self.state["id"], **listing})

    @staticmethod
    def _name(cik):
        if not re.fullmatch(r"[0-9]{10}", cik) or int(cik) == 0:
            raise ValueError("Invalid checkpoint issuer")
        return f"issuer-{cik}.json"

    def complete(self):
        if self.state is not None:
            atomic_json(self.directory / "batch.json", {**self.state, "complete": True})
