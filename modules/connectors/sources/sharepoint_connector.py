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
    Supports enterprise client-credentials OAuth2 authentication, multi-drive discovery,
    recursive folder traversal, and change-data-capture metadata extraction.
    """
    def __init__(self, connection_config: Dict[str, Any]):
        self.config = connection_config
        self.tenant_id = connection_config.get("tenant_id", "").strip()
        self.client_id = connection_config.get("client_id", "").strip()
        self.client_secret = connection_config.get("client_secret", "").strip()
        self.site_url = connection_config.get("site_url", "").strip()
        self.site_id = connection_config.get("site_id", "").strip()
        self.folder_path = connection_config.get("folder_path", "").strip()

        # If site_url is supplied and has comma-separated structure (hostname,spsite_id,spweb_id), it is actually site_id
        if not self.site_id and "," in self.site_url:
            self.site_id = self.site_url

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
        """
        Resolves or validates the SharePoint Site ID using Microsoft Graph API.
        Handles direct Site IDs ('domain,site-guid,web-guid'), full web URLs, or tenant paths.
        """
        # 1. If explicit or detected site_id exists
        if self.site_id:
            try:
                res = requests.get(f"https://graph.microsoft.com/v1.0/sites/{self.site_id}", headers=headers, timeout=15)
                if res.status_code == 200:
                    verified_id = res.json().get("id", self.site_id)
                    logger.info(f"[SHAREPOINT] Verified site_id directly: {verified_id}")
                    return verified_id
                else:
                    logger.warning(f"[SHAREPOINT] Direct site_id check returned {res.status_code}: {res.text[:120]}")
            except Exception as e:
                logger.warning(f"[SHAREPOINT] Error checking site_id '{self.site_id}': {e}")

        if not self.site_url:
            return None

        # 2. Check if site_url is actually formatted as a composite site ID
        if "," in self.site_url:
            candidate_id = self.site_url.strip()
            try:
                res = requests.get(f"https://graph.microsoft.com/v1.0/sites/{candidate_id}", headers=headers, timeout=15)
                if res.status_code == 200:
                    verified_id = res.json().get("id", candidate_id)
                    self.site_id = verified_id
                    logger.info(f"[SHAREPOINT] Verified composite site_id from site_url: {verified_id}")
                    return verified_id
            except Exception as e:
                logger.warning(f"[SHAREPOINT] Error checking composite site_id '{candidate_id}': {e}")

        # 3. Resolve from URL
        raw_url = self.site_url.strip()
        if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
            raw_url = "https://" + raw_url

        try:
            parsed = urlparse(raw_url)
            hostname = parsed.netloc
            path = parsed.path.rstrip("/")
            if not hostname:
                return None

            # Resolve /v1.0/sites/{hostname}:{path}
            if path:
                url = f"https://graph.microsoft.com/v1.0/sites/{hostname}:{path}"
                res = requests.get(url, headers=headers, timeout=15)
                if res.status_code == 200:
                    resolved_id = res.json().get("id")
                    self.site_id = resolved_id
                    logger.info(f"[SHAREPOINT] Resolved site '{self.site_url}' to site_id: {resolved_id}")
                    return resolved_id
                else:
                    logger.warning(f"[SHAREPOINT] Could not resolve site ID via path ({res.status_code}): {res.text[:150]}")

            # Fallback to root site of hostname
            url = f"https://graph.microsoft.com/v1.0/sites/{hostname}"
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                resolved_id = res.json().get("id")
                self.site_id = resolved_id
                logger.info(f"[SHAREPOINT] Resolved hostname '{hostname}' to site_id: {resolved_id}")
                return resolved_id
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

            # 1. Ping organization
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

            # 2. Resolve and validate site
            site_id = self._get_site_id(headers)
            if (self.site_url or self.site_id) and not site_id:
                return {
                    "success": False,
                    "latency_ms": latency,
                    "message": f"Connected to Microsoft Entra tenant '{org_name}', but unable to resolve SharePoint site. Verify your Site URL or Site ID.",
                    "details": {"organization": org_name, "site_url": self.site_url, "site_id": self.site_id}
                }

            # 3. Discover document libraries (drives)
            drive_names = []
            if site_id:
                d_res = requests.get(f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives", headers=headers, timeout=10)
                if d_res.status_code == 200:
                    drive_names = [d.get("name", "Unnamed Library") for d in d_res.json().get("value", [])]

            return {
                "success": True,
                "latency_ms": latency,
                "message": f"SharePoint Handshake Verified ({latency}ms) - {len(drive_names)} libraries found ({', '.join(drive_names) if drive_names else 'Default'})",
                "details": {
                    "organization": org_name,
                    "site_id": site_id or "Root Tenant",
                    "site_url": self.site_url,
                    "folder_path": self.folder_path,
                    "document_libraries": drive_names
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
        Traverses SharePoint document libraries and folders using application permissions.
        Supports multi-library sites, folder path filtering, and recursive traversal.
        """
        token = self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}

        site_id = self._get_site_id(headers)
        if not site_id:
            raise ValueError(f"Unable to resolve SharePoint site ID for '{self.site_url or self.site_id}'.")

        # Discover all document libraries in this site
        drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
        res = requests.get(drives_url, headers=headers, timeout=15)
        if res.status_code != 200:
            raise ValueError(f"Failed to list SharePoint document libraries ({res.status_code}): {res.text[:150]}")

        all_drives = res.json().get("value", [])
        if not all_drives:
            logger.warning(f"[SHAREPOINT] No document libraries found for site {site_id}")
            return

        clean_path = (self.folder_path or "").strip().strip("/")
        # In SharePoint, '/Shared Documents' is the URL slug for the default 'Documents' library
        is_all_libraries = not clean_path or clean_path.lower() in ("shared documents", "documents", "root")

        # Determine target entry points: list of dicts with url, drive_id, drive_name, current_path
        queue: List[Dict[str, Any]] = []

        for drive in all_drives:
            d_id = drive["id"]
            d_name = drive.get("name", "Documents")
            d_slug = drive.get("webUrl", "").split("/")[-1].replace("%20", " ")

            if is_all_libraries:
                # Traverse all libraries starting from root
                queue.append({
                    "url": f"https://graph.microsoft.com/v1.0/drives/{d_id}/root/children",
                    "drive_id": d_id,
                    "drive_name": d_name,
                    "current_path": d_name
                })
            else:
                # Check if path targets this specific library or a subpath inside it
                if clean_path.lower() == d_name.lower() or clean_path.lower() == d_slug.lower():
                    queue.append({
                        "url": f"https://graph.microsoft.com/v1.0/drives/{d_id}/root/children",
                        "drive_id": d_id,
                        "drive_name": d_name,
                        "current_path": d_name
                    })
                elif clean_path.lower().startswith(d_name.lower() + "/") or clean_path.lower().startswith(d_slug.lower() + "/"):
                    subfolder = clean_path.split("/", 1)[1]
                    queue.append({
                        "url": f"https://graph.microsoft.com/v1.0/drives/{d_id}/root:/{subfolder}:/children",
                        "drive_id": d_id,
                        "drive_name": d_name,
                        "current_path": f"{d_name}/{subfolder}"
                    })
                elif drive.get("driveType") == "documentLibrary":
                    queue.append({
                        "url": f"https://graph.microsoft.com/v1.0/drives/{d_id}/root:/{clean_path}:/children",
                        "drive_id": d_id,
                        "drive_name": d_name,
                        "current_path": f"{d_name}/{clean_path}"
                    })

        visited_urls = set()

        while queue:
            entry = queue.pop(0)
            current_url = entry["url"]
            drive_id = entry["drive_id"]
            drive_name = entry["drive_name"]
            current_path = entry["current_path"]

            if current_url in visited_urls:
                continue
            visited_urls.add(current_url)

            res = requests.get(current_url, headers=headers, timeout=20)
            if res.status_code != 200:
                logger.warning(f"[SHAREPOINT] Fetch failed at {current_path} ({res.status_code}): {res.text[:120]}")
                continue

            data = res.json()
            items = data.get("value", [])
            logger.info(f"[SHAREPOINT] Found {len(items)} items in '{current_path}'")

            for item in items:
                item_name = item.get("name", "unnamed")
                item_id = item.get("id")

                # If item is a folder, enqueue recursive traversal
                if "folder" in item:
                    child_url = item.get("@microsoft.graph.childrenUrl") or f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/children"
                    queue.append({
                        "url": child_url,
                        "drive_id": drive_id,
                        "drive_name": drive_name,
                        "current_path": f"{current_path}/{item_name}"
                    })
                    continue

                # If item is a file, stream download
                download_url = item.get("@microsoft.graph.downloadUrl")
                if not download_url and item_id:
                    download_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content"

                if download_url:
                    try:
                        f_res = requests.get(download_url, headers=headers, timeout=30)
                        if f_res.status_code == 200:
                            mime_type = item.get("file", {}).get("mimeType", "application/octet-stream")
                            last_modified = item.get("lastModifiedDateTime")
                            etag = item.get("eTag", "").replace('"', "")
                            rel_path = f"{current_path}/{item_name}"

                            yield RawDocument(
                                doc_id=str(uuid.uuid4()),
                                source_type="SHAREPOINT",
                                filename=item_name,
                                content_bytes=f_res.content,
                                mime_type=mime_type,
                                metadata={
                                    "external_id": item_id,
                                    "sharepoint_id": item_id,
                                    "last_modified": last_modified,
                                    "etag": etag,
                                    "web_url": item.get("webUrl", ""),
                                    "size_bytes": item.get("size", len(f_res.content)),
                                    "drive_name": drive_name,
                                    "relative_path": rel_path
                                }
                            )
                        else:
                            logger.warning(f"[SHAREPOINT] Failed downloading '{item_name}': HTTP {f_res.status_code}")
                    except Exception as e:
                        logger.error(f"[SHAREPOINT] Error downloading item '{item_name}': {e}")

            # Pagination nextLink support
            next_link = data.get("@odata.nextLink")
            if next_link:
                queue.append({
                    "url": next_link,
                    "drive_id": drive_id,
                    "drive_name": drive_name,
                    "current_path": current_path
                })
