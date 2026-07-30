"""Shared delivery logic: upload to Drive, send the email or generate the QR
kiosk photo, archive the file(s), and mark the job sent.

Used both by the human-triggered Approve route (glambot/app.py) and by the
automatic "full delivery" path (glambot/processor.py, when a project has
`auto_deliver` enabled) — both need to run the exact same sequence, so it
lives here once instead of being duplicated between the two.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .db import Job, JobStore
from .drive import upload_and_share
from .emailer import send_delivery_email
from .processor import QR_APPROVED_SUBDIR, QR_DOWNLOAD_SUBDIR, SENT_SUBDIR, resolve_output_base
from .qr import make_delivery_photo, make_qr_data_uri

logger = logging.getLogger(__name__)


@dataclass
class DeliveryResult:
    job: Job
    link: str
    link2: str | None
    qr_data_uri: str
    qr_data_uri2: str | None


def _archive(job: Job, output_base: Path, subdir: str) -> tuple[Path, Path | None, Path | None]:
    """Move the primary output (+ thumbnail + secondary output, if present)
    into the project's archive subfolder (a sibling of the relocated Output
    folder when a custom output_dir is configured). Returns (primary,
    thumbnail, secondary) destination paths."""
    archive_dir = output_base / subdir
    archive_dir.mkdir(parents=True, exist_ok=True)

    src = Path(job.output_path)
    dest = archive_dir / src.name
    shutil.move(str(src), str(dest))

    archived_thumb: Path | None = None
    if job.thumbnail_path:
        thumb_src = Path(job.thumbnail_path)
        if thumb_src.exists():
            thumb_dest = archive_dir / thumb_src.name
            shutil.move(str(thumb_src), str(thumb_dest))
            archived_thumb = thumb_dest

    archived_secondary: Path | None = None
    if job.secondary_output_path:
        sec_src = Path(job.secondary_output_path)
        if sec_src.exists():
            sec_dest = archive_dir / sec_src.name
            shutil.move(str(sec_src), str(sec_dest))
            archived_secondary = sec_dest

    return dest, archived_thumb, archived_secondary


def deliver(job: Job, config: ProjectConfig, store: JobStore, inbox_dir: Path, *,
            recipient: str | None, subject: str = "", body: str = "",
            delivery_mode: str | None = None) -> DeliveryResult:
    """Upload (both outputs, if a second resolution exists) to Drive, send
    the email or generate the QR-kiosk photo, archive the file(s), and
    mark_sent. Raises DriveError/EmailError on failure — callers decide how
    to surface/retry, but never leaves the job half-delivered."""
    delivery_mode = delivery_mode or job.delivery_mode or "email"
    folder_id = config.drive_folder_id or os.environ.get("DRIVE_FOLDER_ID", "")

    if job.drive_link:
        # Already uploaded (e.g. the qr_only pre-upload-on-ready step) — no
        # need to re-upload, so Approve/auto-delivery is instant.
        link = job.drive_link
    else:
        link = upload_and_share(Path(job.output_path), folder_id)

    link2 = None
    if job.secondary_output_path and Path(job.secondary_output_path).exists():
        link2 = upload_and_share(Path(job.secondary_output_path), folder_id)

    if delivery_mode == "email":
        final_body = body
        if link2 and "{link2}" not in body:
            final_body = f"{body}\n\nAlternate version: {link2}"
        send_delivery_email(recipient=recipient, subject=subject, body=final_body, link=link, link2=link2)

    # Full-auto QR-kiosk mode (qr_only + auto_deliver) keeps everything
    # together in Output/ — there's no manual review step "selecting"
    # anything out of it, so a separate Selected Output/Instant Download
    # split only adds an extra place to look. Manual qr_only Approve, and
    # email mode, keep the existing archive-on-deliver behavior.
    keep_in_output = delivery_mode == "qr_only" and config.auto_deliver
    if keep_in_output:
        archived_path = Path(job.output_path)
        archived_thumb = Path(job.thumbnail_path) if job.thumbnail_path else None
        archived_secondary = Path(job.secondary_output_path) if job.secondary_output_path else None
        download_dir = archived_path.parent
    else:
        project_dir = (config.project_dir or (inbox_dir / job.project)).resolve()
        output_base = resolve_output_base(project_dir, config)
        archive_subdir = QR_APPROVED_SUBDIR if delivery_mode == "qr_only" else SENT_SUBDIR
        archived_path, archived_thumb, archived_secondary = _archive(job, output_base, archive_subdir)
        download_dir = output_base / QR_DOWNLOAD_SUBDIR

    if delivery_mode == "qr_only" and archived_thumb:
        try:
            photo_bytes = make_delivery_photo(
                archived_thumb, link, url2=link2,
                label=config.resolution if link2 else None,
                label2=config.second_resolution if link2 else None,
            )
            download_dir.mkdir(parents=True, exist_ok=True)
            photo_path = download_dir / f"{Path(job.output_path).stem}_download.jpg"
            photo_path.write_bytes(photo_bytes)
        except Exception:
            logger.exception("Failed to generate delivery photo for job %s", job.id)

    updated_job = store.mark_sent(
        job.id, drive_link=link, output_path=str(archived_path),
        recipient_email=recipient or None,
        delivery_mode=delivery_mode,
        thumbnail_path=str(archived_thumb) if archived_thumb else job.thumbnail_path,
        secondary_drive_link=link2,
    )
    if archived_secondary:
        updated_job = store.update_job(job.id, secondary_output_path=str(archived_secondary))

    qr_data_uri = make_qr_data_uri(link)
    qr_data_uri2 = make_qr_data_uri(link2) if link2 else None

    return DeliveryResult(job=updated_job, link=link, link2=link2,
                           qr_data_uri=qr_data_uri, qr_data_uri2=qr_data_uri2)
