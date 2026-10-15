"""Trusted offline maintenance: fence jobs and queue all documents for a new index."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import delete, func, select


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-all", action="store_true",
                        help="Apply to ALL tenants; stop traffic/workers and back up first")
    args = parser.parse_args()
    from sourcedesk.config import get_settings
    from sourcedesk.db import session_factory
    from sourcedesk.models import Chunk, Document, IngestionJob, Page
    from sourcedesk.storage import LocalStorage

    settings = get_settings()
    factory = session_factory()
    with factory() as session:
        count = session.scalar(select(func.count()).select_from(Document))
        versions = session.scalars(select(Document.processing_version).distinct()).all()
    preview = {"mode": "apply" if args.confirm_all else "dry_run", "documents": count,
               "existing_processing_versions": sorted(versions),
               "target_processing_version": settings.processing_version,
               "warning": "ALL tenants; API and workers must load new settings before restart"}
    if not args.confirm_all:
        print(json.dumps(preview, indent=2))
        return
    if count and settings.processing_version in versions:
        parser.exit(2, "Choose a new SOURCEDESK_PROCESSING_VERSION first; refusing same-version reindex.\n")
    storage = LocalStorage(settings.upload_dir)
    with factory.begin() as session:
        # Match worker finalization/deletion: document lock before its job lock.
        documents = session.scalars(select(Document).order_by(Document.id).with_for_update()).all()
        now = datetime.now(UTC)
        for document in documents:
            if not storage.path(document.storage_key).is_file():
                raise RuntimeError("Missing original PDF: restore uploads before reindexing")
            job = session.scalar(select(IngestionJob)
                                 .where(IngestionJob.document_id == document.id).with_for_update())
            if job is None:
                job = IngestionJob(document_id=document.id)
                session.add(job)
            job.lease_token = None
            job.lease_expires_at = None
            job.status = "queued"
            job.attempts = 0
            job.available_at = now
            job.started_at = None
            job.completed_at = None
            job.error_code = None
            job.error_message = None
            session.execute(delete(Chunk).where(Chunk.document_id == document.id))
            session.execute(delete(Page).where(Page.document_id == document.id))
            document.status = "queued"
            document.page_count = 0
            document.processing_version = settings.processing_version
            document.error_code = None
            document.error_message = None
            document.updated_at = now
        ids = [document.id for document in documents]
    # Read the exact targets after commit before declaring maintenance success.
    # Keep workers stopped until verification finishes or queued may advance.
    with factory() as session:
        indexed = session.scalar(select(func.count()).select_from(Chunk)
                                 .where(Chunk.document_id.in_(ids)))
        pages = session.scalar(select(func.count()).select_from(Page).where(Page.document_id.in_(ids)))
        records = session.execute(select(Document, IngestionJob)
                                  .join(IngestionJob, Document.id == IngestionJob.document_id)
                                  .where(Document.id.in_(ids))).all()
        if indexed or pages or len(records) != len(ids) or any(
                doc.status != "queued" or doc.processing_version != settings.processing_version
                or job.status != "queued" or job.lease_token is not None
                or job.lease_expires_at is not None or job.attempts != 0
                for doc, job in records):
            raise RuntimeError("Reindex verification failed; keep traffic stopped and inspect job state")
    print(json.dumps({**preview, "documents": len(ids), "verified_queued": True,
                      "remaining_chunks": indexed, "remaining_pages": pages}, indent=2))


if __name__ == "__main__":
    main()
