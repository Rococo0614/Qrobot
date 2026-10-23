from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_MEMORY_CHARS = 500


@dataclass(slots=True, frozen=True)
class MemoryRecord:
    id: int
    scope_id: str
    user_id: str
    content: str
    kind: str
    source: str
    created_at: str
    updated_at: str


@dataclass(slots=True, frozen=True)
class MemoryCommand:
    action: str
    value: str = ""


_SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\b", re.I),
    re.compile(r"(?:密码|口令|密钥|令牌|验证码|身份证|银行卡|住址|家庭地址)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{15,19}(?!\d)"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{16,}", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)


def normalize_memory(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n，。！？；;,.!?")
    return value.casefold()


def contains_sensitive_data(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def parse_memory_command(text: str) -> MemoryCommand | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if re.fullmatch(r"(?:我的)?记忆(?:列表)?|/memory", normalized, re.I):
        return MemoryCommand("list")
    if re.fullmatch(r"(?:清空|删除全部|忘掉全部)(?:我的)?记忆", normalized):
        return MemoryCommand("clear")
    match = re.fullmatch(r"(?:请)?记住\s*[：:]?\s*(.+)", normalized, re.S)
    if match:
        return MemoryCommand("remember", match.group(1).strip())
    match = re.fullmatch(r"(?:请)?忘记\s*[：:]?\s*(.+)", normalized, re.S)
    if match:
        return MemoryCommand("forget", match.group(1).strip())
    return None


def extract_explicit_facts(text: str) -> list[str]:
    """Conservatively extract a few clearly self-declared, non-sensitive facts."""
    text = unicodedata.normalize("NFKC", text).strip()
    patterns = (
        (r"(?:^|[,，.。!！?？;；\s])(?:我叫|叫我)([^,，.。!！?？;；\n]{1,30})", "我叫"),
        (r"(?:^|[,，.。!！?？;；\s])(我(?:很|非常|最)?喜欢[^,，.。!！?？;；\n]{1,80})", ""),
        (r"(?:^|[,，.。!！?？;；\s])(我不喜欢[^,，.。!！?？;；\n]{1,80})", ""),
        (
            r"(?:^|[,，.。!！?？;；\s])(我最近在(?:做|学|研究)[^,，.。!！?？;；\n]{1,100})",
            "",
        ),
        (
            r"(?:^|[,，.。!！?？;；\s])(我的(?:爱好|工作|专业)是[^,，.。!！?？;；\n]{1,80})",
            "",
        ),
    )
    facts: list[str] = []
    for pattern, prefix in patterns:
        for match in re.finditer(pattern, text):
            fact = prefix + match.group(1).strip()
            if fact and not contains_sensitive_data(fact) and fact not in facts:
                facts.append(fact[:MAX_MEMORY_CHARS])
    return facts[:3]


class MemoryStore:
    """SQLite-backed structured memory; it never stores complete chat transcripts."""

    def __init__(self, path: str | Path, max_records_per_user: int = 200) -> None:
        self.path = Path(path)
        self.max_records_per_user = max(1, max_records_per_user)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()
        self._secure_database_files()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA secure_delete=ON")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    content TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    UNIQUE(scope_id, user_id, normalized)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_owner "
                "ON memories(scope_id, user_id, updated_at DESC)"
            )
            self._connection.execute("PRAGMA user_version=1")

    def _secure_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._connection.close()
            self._secure_database_files()

    def remember(
        self,
        scope_id: str,
        user_id: str,
        content: str,
        *,
        kind: str = "fact",
        source: str = "explicit",
    ) -> tuple[bool, str]:
        content = unicodedata.normalize("NFKC", content).strip()
        if not content:
            return False, "记忆内容不能为空"
        if len(content) > MAX_MEMORY_CHARS:
            return False, f"单条记忆不能超过 {MAX_MEMORY_CHARS} 个字符"
        if contains_sensitive_data(content):
            return False, "这段内容可能包含密码、Token、联系方式或其他敏感信息，我不会保存"
        normalized = normalize_memory(content)
        if not normalized:
            return False, "记忆内容不能为空"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT id FROM memories WHERE scope_id=? AND user_id=? AND normalized=?",
                (scope_id, user_id, normalized),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO memories
                    (scope_id, user_id, kind, content, normalized, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, user_id, normalized) DO UPDATE SET
                    content=excluded.content,
                    kind=excluded.kind,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (scope_id, user_id, kind, content, normalized, source, now, now),
            )
            self._connection.execute(
                """
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE scope_id=? AND user_id=?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (scope_id, user_id, self.max_records_per_user),
            )
        self._secure_database_files()
        return existing is None, content

    def forget(self, scope_id: str, user_id: str, query: str) -> int:
        normalized = normalize_memory(query)
        if not normalized:
            return 0
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM memories
                WHERE scope_id=? AND user_id=?
                  AND (normalized=? OR normalized LIKE ? ESCAPE '\\')
                """,
                (scope_id, user_id, normalized, f"%{escaped}%"),
            )
            return max(cursor.rowcount, 0)

    def clear(self, scope_id: str, user_id: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE scope_id=? AND user_id=?",
                (scope_id, user_id),
            )
            return max(cursor.rowcount, 0)

    def list(self, scope_id: str, user_id: str, limit: int = 50) -> list[MemoryRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, scope_id, user_id, content, kind, source, created_at, updated_at
                FROM memories WHERE scope_id=? AND user_id=?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (scope_id, user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def recall(
        self, scope_id: str, user_id: str, query: str, limit: int = 8
    ) -> list[MemoryRecord]:
        candidates = self.list(scope_id, user_id, limit=100)
        query_tokens = _tokens(query)
        query_normalized = normalize_memory(query)
        scored: list[tuple[float, MemoryRecord]] = []
        for record in candidates:
            value = normalize_memory(record.content)
            value_tokens = _tokens(value)
            overlap = len(query_tokens & value_tokens)
            union = len(query_tokens | value_tokens) or 1
            score = overlap / union
            if query_normalized and (
                query_normalized in value or value in query_normalized
            ):
                score += 1.0
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        selected = [record for _, record in scored[: max(1, limit)]]
        if selected:
            now = datetime.now(timezone.utc).isoformat()
            ids = [record.id for record in selected]
            placeholders = ",".join("?" for _ in ids)
            with self._lock, self._connection:
                self._connection.execute(
                    f"UPDATE memories SET last_used_at=? WHERE id IN ({placeholders})",
                    (now, *ids),
                )
        return selected

    def fingerprint(self) -> str:
        """Non-sensitive diagnostic identity for the database path."""
        return hashlib.sha256(str(self.path).encode()).hexdigest()[:12]


def _tokens(value: str) -> set[str]:
    normalized = normalize_memory(value)
    words = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    for run in cjk_runs:
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=int(row["id"]),
        scope_id=str(row["scope_id"]),
        user_id=str(row["user_id"]),
        content=str(row["content"]),
        kind=str(row["kind"]),
        source=str(row["source"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
