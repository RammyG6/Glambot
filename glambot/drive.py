"""Upload an approved clip to a designated Google Drive folder and return a
shareable link, using the OAuth "installed app" (desktop) flow.

First run opens a browser for a one-time consent; the resulting token is
cached in token.json (gitignored) so subsequent runs are non-interactive.
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_CREDENTIALS_PATH = Path("credentials.json")
_TOKEN_PATH = Path("token.json")


class DriveError(RuntimeError):
    pass


def _get_credentials() -> Credentials:
    creds: Credentials | None = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not _CREDENTIALS_PATH.exists():
        raise DriveError(
            "credentials.json not found. Create an OAuth Desktop app client in "
            "Google Cloud Console (with the Drive API enabled) and save it as "
            "credentials.json in the project root — see README.md."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_PATH.write_text(creds.to_json())
    return creds


def upload_and_share(file_path: Path, folder_id: str) -> str:
    """Upload file_path into the given Drive folder, make it viewable by
    anyone with the link, and return that link."""
    if not folder_id:
        raise DriveError("DRIVE_FOLDER_ID is not set — check your .env file.")

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds)

    mime_type, _ = mimetypes.guess_type(str(file_path))
    media = MediaFileUpload(str(file_path), mimetype=mime_type or "video/mp4", resumable=True)
    file_metadata = {"name": file_path.name, "parents": [folder_id]}

    logger.info("Uploading %s to Drive folder %s", file_path.name, folder_id)
    uploaded = service.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    file_id = uploaded["id"]

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    result = service.files().get(fileId=file_id, fields="webViewLink").execute()
    link = result["webViewLink"]
    logger.info("Uploaded %s -> %s", file_path.name, link)
    return link
