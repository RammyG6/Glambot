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
from dataclasses import dataclass
from pathlib import Path

from .config import ProjectConfig
from .db import Job, JobStore
from .drive import upload_and_share
from .emailer import send_delivery_email
from .processor import verify_output
from .qr import make_delivery_photo, make_qr_data_uri

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Delivery was refused before anything left the machine — as opposed to
    DriveError/EmailError, which mean an upload or send was attempted and
    failed."""


@dataclass
class DeliveryResult:
    job: Job
    link: str
    link2: str | None
    qr_data_uri: str
    qr_data_uri2: str | None


def deliver(job: Job, config: ProjectConfig, store: JobStore, inbox_dir: Path, *,
            recipient: str | None, subject: str = "", body: str = "",
            delivery_mode: str | None = None) -> DeliveryResult:
    """Upload (both outputs, if a second resolution exists) to Drive, send
    the email or generate the QR-kiosk photo, archive the file(s), and
    mark_sent. Raises DriveError/EmailError on failure — callers decide how
    to surface/retry, but never leaves the job half-delivered."""
    delivery_mode = delivery_mode or job.delivery_mode or "email"
    folder_id = config.drive_folder_id or os.environ.get("DRIVE_FOLDER_ID", "")

    # Last gate before the clip leaves the machine. process_job() already
    # verified this render, but a job can sit as `ready` for a long time and
    # be delivered manually much later, by which point the file may have been
    # truncated, half-synced or replaced. Uploading a broken clip to a
    # customer is far worse than failing here.
    ok, reason = verify_output(Path(job.output_path), None)
    if not ok:
        raise DeliveryError(f"Refusing to deliver job {job.id}: {reason}")

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

    # Lifecycle status lives only in the DB now (see mark_sent below) - no
    # file ever moves on approve. `Thumbnail/` already holds the thumbnail,
    # so the QR+thumbnail "download photo" belongs right alongside it there.
    archived_path = Path(job.output_path)
    archived_thumb = Path(job.thumbnail_path) if job.thumbnail_path else None
    archived_secondary = Path(job.secondary_output_path) if job.secondary_output_path else None
    download_dir = archived_thumb.parent if archived_thumb else None

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
        secondary_output_path=str(archived_secondary) if archived_secondary else job.secondary_output_path,
        secondary_drive_link=link2,
    )

    qr_data_uri = make_qr_data_uri(link)
    qr_data_uri2 = make_qr_data_uri(link2) if link2 else None

    return DeliveryResult(job=updated_job, link=link, link2=link2,
                           qr_data_uri=qr_data_uri, qr_data_uri2=qr_data_uri2)
