# modules/connectors/sources/google_drive_connector.py
import json
import time
import uuid
import logging
from typing import Generator, Dict, Any, Optional
import requests
from modules.connectors.base import BaseConnector, RawDocument

logger = logging.getLogger("google_drive_connector")

class GoogleDriveConnector(BaseConnector):
    """
    Google Workspace / Google Drive REST API Connector.
    """
    def __init__(self, connection_config: Dict[str, Any]):
        self.config = connection_config
        self.folder_id = connection_config.get("folder_id")
        self.include_shared = connection_config.get("include_shared_drives", True)
        self.sa_json_raw = connection_config.get("service_account_json", "")

    def _get_access_token(self) -> str:
        # If raw bearer token passed directly
        if self.config.get("access_token"):
            return self.config["access_token"]
        # Parse Service Account Key & Generate OAuth2 JWT Bearer
        try:
            sa = json.loads(self.sa_json_raw) if isinstance(self.sa_json_raw, str) else self.sa_json_raw
            # Standard Google Auth or token endpoint
            token_uri = sa.get("token_uri", "https://oauth2.googleapis.com/token")
            # In production environment with google-auth:
            import jwt
            now = int(time.time())
            payload = {
                "iss": sa["client_email"],
                "sub": sa["client_email"],
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
                "scope": "https://www.googleapis.com/auth/drive.readonly"
            }
            signed_jwt = jwt.encode(payload, sa["private_key"], algorithm="RS256")
            res = requests.post(token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt
            }, timeout=10)
            if res.status_code == 200:
                return res.json()["access_token"]
            raise ValueError(f"Google Token error ({res.status_code}): {res.text}")
        except Exception as e:
            raise ValueError(f"Google Service Account Authentication error: {str(e)}")

    def test_connection(self) -> Dict[str, Any]:
        start = time.perf_counter()
        try:
            if not self.sa_json_raw and not self.config.get("access_token"):
                return {
                    "success": False,
                    "latency_ms": 0.0,
                    "message": "Service Account JSON or Access Token is required.",
                    "details": None
                }
            token = self._get_access_token()
            headers = {"Authorization": f"Bearer {token}"}
            url = "https://www.googleapis.com/drive/v3/about?fields=user,storageQuota"
            res = requests.get(url, headers=headers, timeout=8)
            latency = round((time.perf_counter() - start) * 1000, 2)
            if res.status_code == 200:
                data = res.json()
                return {
                    "success": True,
                    "latency_ms": latency,
                    "message": f"Google Workspace Handshake Verified ({latency}ms)",
                    "details": {
                        "user": data.get("user", {}).get("displayName", "Service Account"),
                        "email": data.get("user", {}).get("emailAddress", "")
                    }
                }
            return {
                "success": False,
                "latency_ms": latency,
                "message": f"Google Drive API returned HTTP {res.status_code}: {res.text[:120]}",
                "details": None
            }
        except Exception as e:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return {
                "success": False,
                "latency_ms": latency,
                "message": f"Google Drive Connection Failed: {str(e)}",
                "details": None
            }

    def fetch_documents(self) -> Generator[RawDocument, None, None]:
        token = self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        query = "trashed = false"
        if self.folder_id:
            query += f" and '{self.folder_id}' in parents"

        url = f"https://www.googleapis.com/drive/v3/files?q={query}&fields=files(id,name,mimeType,modifiedTime,version,md5Checksum)"
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            logger.error(f"Google Drive fetch error: {res.text}")
            return

        for item in res.json().get("files", []):
            file_id = item["id"]
            name = item["name"]
            mime = item.get("mimeType", "application/octet-stream")
            # Download file
            download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
            f_res = requests.get(download_url, headers=headers, timeout=30)
            if f_res.status_code == 200:
                yield RawDocument(
                    doc_id=str(uuid.uuid4()),
                    source_type="GOOGLE_DRIVE",
                    filename=name,
                    content_bytes=f_res.content,
                    mime_type=mime,
                    metadata={
                        "external_id": file_id,
                        "google_file_id": file_id,
                        "last_modified": item.get("modifiedTime"),
                        "etag": item.get("md5Checksum") or item.get("version")
                    }
                )

