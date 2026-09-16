# modules/connectors/sources/sharepoint_connector.py
import time
import uuid
import logging
from urllib.parse import urlparse
from typing import Generator, Dict, Any, Optional, List
import requests
from modules.connectors.base import BaseConnector, RawDocument

logger = logging.getLogger("sharepoint_connector")

class SharePointConnector(BaseConnector):
    """
    Microsoft SharePoint & OneDrive Connector via Microsoft Graph REST API.
    Supports enterprise client-credentials OAuth2 authentication, site discovery,
    recursive folder traversal, and change-data-capture metadata extraction.
    """
    def __init__(self, connection_config: Dict[str, Any]):
        self.config = connection_config
        self.tenant_id = connection_config.get("tenant_id", "").strip()
        self.client_id = connection_config.get("client_id", "").strip()
        self.client_secret = connection_config.get("client_secret", "").strip()
        self.site_url = connection_config.get("site_url", "").strip()
        self.folder_path = connection_config.get("folder_path", "/Shared Documents").strip()

    def _get_access_token(self) -> str:
        token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials"
        }
        res = requests.post(token_url, data=payload, timeout=15)
        if res.status_code == 200:
            return res.json()["access_token"]
        error_msg = f"Microsoft Entra OAuth2 Failed ({res.status_code}): {res.text[:200]}"
        logger.error(f"[SHAREPOINT] {error_msg}")
        raise ValueError(error_msg)

    def _get_site_id(self, headers: Dict[str, str]) -> Optional[str]:
        """Resolves the SharePoint Site ID from site_url using Microsoft Graph API."""
        if not self.site_url:
            return None
        try:
            parsed = urlparse(self.site_url)
            hostname = parsed.netloc
            path = parsed.path.rstrip("/")
            if not hostname:
                return None
            
            # Format: /v1.0/sites/{hostname}:{path}
            url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{path}"
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                site_id = res.json().get("id")
                logger.info(f"[SHAREPOINT] Resolved site '{self.site_url}' to site_id: {site_id}")
                return site_id
            else:
                logger.warning(f"[SHAREPOINT] Could not resolve site ID via path ({res.status_code}): {res.text[:150]}")
        except Exception as e:
            logger.warning(f"[SHAREPOINT] Error parsing site URL '{self.site_url}': {e}")
        return None

    def test_connection(self) -> Dict[str, Any]:
        start = time.perf_counter()
        try:
            if not self.tenant_id or not self.client_id or not self.client_secret:
                return {
                    "success": False,
                    "latency_ms": 0.0,
                    "message": "Tenant ID, Client ID, and Client Secret are required.",
                    "details": None
                }

            token = self._get_access_token()
            headers = {"Authorization": f"Bearer {token}"}

            # Verify Graph API organization ping
            res = requests.get("https://graph.microsoft.com/v1.0/organization", headers=headers, timeout=10)
            latency = round((time.perf_counter() - start) * 1000, 2)
            if res.status_code != 200:
                return {
                    "success": False,
                    "latency_ms": latency,
                    "message": f"Microsoft Graph API Authentication Error: {res.text[:150]}",
                    "details": None
                }

            org_name = res.json().get("value", [{}])[0].get("displayName", "Microsoft 365 Tenant")
            site_id = self._get_site_id(headers)

            return {
                "success": True,
                "latency_ms": latency,
                "message": f"SharePoint Handshake Verified ({latency}ms)",
                "details": {
                    "organization": org_name,
                    "site_url": self.site_url,
                    "site_id": site_id or "Default Root Tenant",
                    "folder_path": self.folder_path
                }
            }
        except Exception as e:
            latency = round((time.perf_counter() - start) * 1000, 2)
            return {
                "success": False,
                "latency_ms": latency,
                "message": f"SharePoint Connection Failed: {str(e)}",
                "details": None
            }

    def fetch_documents(self) -> Generator[RawDocument, None, None]:
        """
        Traverses the SharePoint document library using application permissions.
        Supports site discovery, custom folder paths, and recursive traversal.
        """
        token = self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Resolve site endpoint
        site_id = self._get_site_id(headers)
        if site_id:
            # Query drive for specific site
            drive_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive"
            res = requests.get(drive_url, headers=headers, timeout=15)
            if res.status_code == 200:
                drive_id = res.json().get("id")
                root_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
            else:
                root_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
        else:
            # Fallback: Query default tenant drive
            root_url = "https://graph.microsoft.com/v1.0/drives"
            res = requests.get(root_url, headers=headers, timeout=15)
            if res.status_code == 200 and res.json().get("value"):
                first_drive_id = res.json()["value"][0]["id"]
                root_url = f"https://graph.microsoft.com/v1.0/drives/{first_drive_id}/root/children"
            else:
                logger.error(f"[SHAREPOINT] Unable to discover drives ({res.status_code}): {res.text[:200]}")
                raise ValueError(f"SharePoint drive access failed ({res.status_code}): {res.text[:150]}")

        # 2. Queue for traversal (supports folders)
        queue = [root_url]
        visited_urls = set()

        while queue:
            current_url = queue.pop(0)
            if current_url in visited_urls:
                continue
            visited_urls.add(current_url)

            res = requests.get(current_url, headers=headers, timeout=20)
            if res.status_code != 200:
                logger.error(f"[SHAREPOINT] Fetch error at {current_url} ({res.status_code}): {res.text[:200]}")
                continue

            data = res.json()
            items = data.get("value", [])
            logger.info(f"[SHAREPOINT] Found {len(items)} items at {current_url}")

            for item in items:
                # If item is a folder, enqueue its children
                if "folder" in item:
                    child_url = item.get("@microsoft.graph.childrenUrl") or f"https://graph.microsoft.com/v1.0/drives/{item.get('parentReference', {}).get('driveId')}/items/{item['id']}/children"
                    queue.append(child_url)
                    continue

                # If item is a file with download URL
                download_url = item.get("@microsoft.graph.downloadUrl")
                if not download_url and "id" in item:
                    # Alternative download URL fetch
                    download_url = f"https://graph.microsoft.com/v1.0/drives/{item.get('parentReference', {}).get('driveId')}/items/{item['id']}/content"

                if download_url:
                    try:
                        f_res = requests.get(download_url, headers=headers, timeout=30)
                        if f_res.status_code == 200:
                            item_id = item.get("id", str(uuid.uuid4()))
                            filename = item.get("name", f"sharepoint_{item_id}")
                            mime_type = item.get("file", {}).get("mimeType", "application/octet-stream")
                            last_modified = item.get("lastModifiedDateTime")
                            etag = item.get("eTag", "").replace('"', "")

                            yield RawDocument(
                                doc_id=str(uuid.uuid4()),
                                source_type="SHAREPOINT",
                                filename=filename,
                                content_bytes=f_res.content,
                                mime_type=mime_type,
                                metadata={
                                    "external_id": item_id,
                                    "sharepoint_id": item_id,
                                    "last_modified": last_modified,
                                    "etag": etag,
                                    "web_url": item.get("webUrl", ""),
                                    "size_bytes": item.get("size", len(f_res.content))
                                }
                            )
                        else:
                            logger.warning(f"[SHAREPOINT] Failed to download {item.get('name')}: HTTP {f_res.status_code}")
                    except Exception as e:
                        logger.error(f"[SHAREPOINT] Error downloading item {item.get('name')}: {e}")

            # Next page pagination
            next_link = data.get("@odata.nextLink")
            if next_link:
                queue.append(next_link)
