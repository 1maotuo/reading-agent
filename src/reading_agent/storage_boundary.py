"""Explicit persistence boundary for the Stage 05 runtime preview.

The current vertical slice still runs against the tested snapshot-backed
contract objects.  This boundary makes that fact explicit while carrying the
normalized PostgreSQL adapters needed for the later cutover.  It prevents a
partially wired adapter from becoming an accidental second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import ReadingProgress, ScopeContext
from .ports import BookRepositoryPort, JobStorePort


class SnapshotStorePort(Protocol):
    def save(self, services: Any) -> None: ...

    def load(self, services: Any) -> bool: ...


@dataclass(frozen=True)
class PersistenceBoundary:
    """Own the selected durable boundary and expose its cutover state."""

    snapshot_store: SnapshotStorePort
    book_repository: BookRepositoryPort | None = None
    job_store: JobStorePort | None = None
    answer_store: object | None = None
    runtime_cutover_enabled: bool = False

    @property
    def normalized_adapters_ready(self) -> bool:
        return (
            self.book_repository is not None
            and self.job_store is not None
            and self.answer_store is not None
        )

    @property
    def status(self) -> dict[str, str | bool]:
        return {
            "mode": (
                "normalized-repositories"
                if self.runtime_cutover_enabled and self.normalized_adapters_ready
                else "preview-snapshot-bridge"
            ),
            "normalized_adapters_ready": self.normalized_adapters_ready,
            "runtime_cutover": self.runtime_cutover_enabled and self.normalized_adapters_ready,
        }

    def save(self, services: Any) -> None:
        """Persist through exactly one selected snapshot boundary for now."""

        self.snapshot_store.save(services)

    def load(self, services: Any) -> bool:
        """Restore through the same boundary used by ``save``."""

        return self.snapshot_store.load(services)

    def require_runtime_cutover(self) -> None:
        """Fail closed until all normalized stores are actually consumed."""

        if not self.status["runtime_cutover"]:
            raise RuntimeError("normalized persistence runtime cutover is not ready")

    def _book_store(self) -> BookRepositoryPort:
        self.require_runtime_cutover()
        if self.book_repository is None:
            raise RuntimeError("normalized book repository is not configured")
        return self.book_repository

    @property
    def progress_storage_ready(self) -> bool:
        """Progress has its own optimistic-lock contract and can cut over first."""

        return self.book_repository is not None

    def get_book(self, scope: ScopeContext):
        return self._book_store().get_book(scope)

    def create_book(self, book):
        if self.book_repository is None:
            raise RuntimeError("normalized book repository is not configured")
        return self.book_repository.create_book(book)

    def get_progress(self, scope: ScopeContext) -> ReadingProgress | None:
        if self.book_repository is None:
            raise RuntimeError("normalized book repository is not configured")
        return self.book_repository.get_progress(scope)

    def put_progress(
        self,
        scope: ScopeContext,
        progress: ReadingProgress,
        expected_row_version: int,
    ) -> ReadingProgress:
        if self.book_repository is None:
            raise RuntimeError("normalized book repository is not configured")
        return self.book_repository.put_progress(scope, progress, expected_row_version)
