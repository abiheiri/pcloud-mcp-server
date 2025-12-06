"""
pCloud MCP Server

An MCP server that provides tools for interacting with the pCloud API.
"""

import os
import sys
import logging
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Configure logging to stderr (critical for MCP servers using stdio transport)
# stdout is reserved for JSON-RPC messages, so all logging must go to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("pcloud-mcp")

# Initialize the MCP server
mcp = FastMCP("pcloud")


class PCloudAuth:
    """
    Manages pCloud authentication with automatic token generation and refresh.

    Authentication strategy:
    1. Store username/password as environment variables
    2. Generate token on first use
    3. Cache token in memory during session
    4. Catch auth errors and regenerate token automatically
    5. Use long expiration times (2 years)
    """

    # pCloud has two API endpoints based on user registration region
    API_HOSTS = {
        "us": "https://api.pcloud.com",
        "eu": "https://eapi.pcloud.com",
    }

    # Token expiration settings (in seconds)
    # authexpire: max 63072000 (2 years), default 31536000 (1 year)
    # authinactiveexpire: max 5356800 (~62 days), default 2678400 (~31 days)
    TOKEN_EXPIRE = 63072000  # 2 years
    TOKEN_INACTIVE_EXPIRE = 5356800  # ~62 days

    def __init__(self):
        self._token: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._api_host: str | None = None
        self._client: httpx.AsyncClient | None = None
        # Cache for path -> folder_id mapping to avoid repeated API calls
        self._folder_id_cache: dict[str, int] = {"/": 0}

    def _load_credentials(self) -> None:
        """Load credentials from environment variables."""
        self._username = os.environ.get("PCLOUD_USERNAME")
        self._password = os.environ.get("PCLOUD_PASSWORD")

        # Determine API host (default to US if not specified)
        region = os.environ.get("PCLOUD_REGION", "us").lower()
        if region not in self.API_HOSTS:
            logger.warning(f"Invalid PCLOUD_REGION '{region}', defaulting to 'us'")
            region = "us"
        self._api_host = self.API_HOSTS[region]

        if not self._username or not self._password:
            raise ValueError(
                "PCLOUD_USERNAME and PCLOUD_PASSWORD environment variables are required"
            )

        logger.info(f"Loaded credentials for {self._username}, using {region.upper()} API")

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _generate_token(self) -> str:
        """
        Generate a new authentication token using userinfo method.

        The userinfo method is recommended as a login entry point according to
        pCloud documentation. We use the getauth parameter to receive a token.
        """
        if self._username is None:
            self._load_credentials()

        client = await self._get_client()

        params = {
            "username": self._username,
            "password": self._password,
            "getauth": 1,  # Request auth token in response
            "authexpire": self.TOKEN_EXPIRE,
            "authinactiveexpire": self.TOKEN_INACTIVE_EXPIRE,
        }

        logger.info("Generating new authentication token...")

        response = await client.get(f"{self._api_host}/userinfo", params=params)
        response.raise_for_status()

        data = response.json()

        # Check for pCloud API errors
        result_code = data.get("result", 0)
        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            logger.error(f"Authentication failed: {error_msg}")
            raise ValueError(f"pCloud authentication failed: {error_msg}")

        token = data.get("auth")
        if not token:
            raise ValueError("No auth token returned from pCloud API")

        self._token = token
        logger.info("Successfully generated authentication token")
        return token

    async def get_token(self) -> str:
        """
        Get the current token, generating a new one if needed.
        """
        if self._token is None:
            self._token = await self._generate_token()
        return self._token

    async def invalidate_token(self) -> None:
        """
        Invalidate the current token, forcing regeneration on next use.
        """
        logger.info("Invalidating current authentication token")
        self._token = None

    async def make_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        retry_on_auth_error: bool = True,
    ) -> dict[str, Any]:
        """
        Make an authenticated request to the pCloud API.

        Automatically handles auth errors by regenerating the token and retrying.

        Args:
            method: The API method to call (e.g., "listfolder")
            params: Additional parameters for the API call
            retry_on_auth_error: Whether to retry on auth errors (default: True)

        Returns:
            The JSON response from the API

        Raises:
            ValueError: If the API returns an error
            httpx.HTTPError: If the HTTP request fails
        """
        token = await self.get_token()
        client = await self._get_client()

        request_params = params.copy() if params else {}
        request_params["auth"] = token

        logger.debug(f"Making request to {method} with params: {request_params.keys()}")

        response = await client.get(f"{self._api_host}/{method}", params=request_params)
        response.raise_for_status()

        data = response.json()
        result_code = data.get("result", 0)

        # Check for auth errors (1000 = login required, 2000 = login failed)
        if result_code in (1000, 2000) and retry_on_auth_error:
            logger.warning(f"Auth error (code: {result_code}), regenerating token...")
            await self.invalidate_token()
            return await self.make_request(method, params, retry_on_auth_error=False)

        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            logger.error(f"API error in {method}: {error_msg}")
            raise ValueError(f"pCloud API error: {error_msg}")

        return data

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def resolve_path_to_folder_id(self, path: str) -> int:
        """
        Resolve a pCloud path to a folder ID, creating folders as needed.

        Uses createfolderifnotexists with folderid + name (not path) as recommended
        by pCloud API documentation.

        Args:
            path: The pCloud path (e.g., "/Documents/Photos/2024")

        Returns:
            The folder ID of the final folder in the path
        """
        # Normalize path
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")

        if not path or path == "/":
            return 0  # Root folder

        # Check cache first
        if path in self._folder_id_cache:
            return self._folder_id_cache[path]

        # Split path and resolve each segment
        segments = [s for s in path.split("/") if s]
        current_folder_id = 0
        current_path = ""

        for segment in segments:
            current_path = f"{current_path}/{segment}"

            # Check cache for this intermediate path
            if current_path in self._folder_id_cache:
                current_folder_id = self._folder_id_cache[current_path]
                continue

            # Create folder if not exists using folderid + name (not path!)
            result = await self.make_request(
                "createfolderifnotexists",
                {"folderid": current_folder_id, "name": segment},
            )

            metadata = result.get("metadata", {})
            current_folder_id = metadata.get("folderid", 0)

            # Cache this path
            self._folder_id_cache[current_path] = current_folder_id

            if result.get("created"):
                logger.info(f"Created folder: {current_path} (id: {current_folder_id})")
            else:
                logger.debug(f"Folder exists: {current_path} (id: {current_folder_id})")

        return current_folder_id

    async def check_file_exists(self, folder_id: int, filename: str) -> dict[str, Any] | None:
        """
        Check if a file exists in a folder.

        Args:
            folder_id: The folder ID to check in
            filename: The filename to look for

        Returns:
            File metadata if exists, None otherwise
        """
        try:
            result = await self.make_request("listfolder", {"folderid": folder_id})
            contents = result.get("metadata", {}).get("contents", [])

            for item in contents:
                if not item.get("isfolder") and item.get("name") == filename:
                    return item

            return None
        except Exception:
            return None

    async def upload_file(
        self,
        local_path: Path,
        folder_id: int,
        filename: str | None = None,
        rename_if_exists: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """
        Upload a file to pCloud using POST multipart/form-data.

        Args:
            local_path: Local path to the file
            folder_id: Target folder ID in pCloud
            filename: Override filename (defaults to local filename)
            rename_if_exists: If True, rename file if it already exists
            progress_callback: Optional callback(uploaded_bytes, total_bytes)

        Returns:
            The upload response with file metadata
        """
        token = await self.get_token()

        if filename is None:
            filename = local_path.name

        file_size = local_path.stat().st_size

        # Build form data
        params = {
            "auth": token,
            "folderid": str(folder_id),
            "filename": filename,
            "nopartial": "1",  # Don't save partial files on failure
        }

        if rename_if_exists:
            params["renameifexists"] = "1"

        # Create a streaming file reader with progress tracking
        async def file_reader():
            uploaded = 0
            with open(local_path, "rb") as f:
                while chunk := f.read(65536):  # 64KB chunks
                    uploaded += len(chunk)
                    if progress_callback:
                        progress_callback(uploaded, file_size)
                    yield chunk

        # Use a custom async client with no timeout for large uploads
        async with httpx.AsyncClient(timeout=None) as client:
            # For httpx, we need to read the file content
            # Using streaming upload with progress tracking
            with open(local_path, "rb") as f:
                files = {"file": (filename, f, "application/octet-stream")}

                response = await client.post(
                    f"{self._api_host}/uploadfile",
                    data=params,
                    files=files,
                )

        response.raise_for_status()
        data = response.json()

        result_code = data.get("result", 0)
        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            raise ValueError(f"Upload failed: {error_msg}")

        return data


# Global auth instance
auth = PCloudAuth()


@dataclass
class DownloadTask:
    """Represents an active or completed download task."""

    download_id: str
    file_id: int | None
    file_path: str | None
    filename: str
    local_path: str
    total_bytes: int = 0
    downloaded_bytes: int = 0
    status: str = "pending"  # pending, downloading, completed, error
    error_message: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def progress_percent(self) -> float:
        """Calculate download progress as a percentage."""
        if self.total_bytes == 0:
            return 0.0
        return (self.downloaded_bytes / self.total_bytes) * 100


@dataclass
class FolderDownloadTask:
    """Represents an active or completed folder download task."""

    download_id: str
    folder_id: int | None
    folder_path: str | None
    folder_name: str
    local_path: str
    total_files: int = 0
    completed_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    status: str = "pending"  # pending, downloading, completed, error
    error_message: str | None = None
    file_tasks: list[DownloadTask] = field(default_factory=list)
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def progress_percent(self) -> float:
        """Calculate download progress as a percentage."""
        if self.total_bytes == 0:
            if self.total_files == 0:
                return 0.0
            return (self.completed_files / self.total_files) * 100
        return (self.downloaded_bytes / self.total_bytes) * 100


@dataclass
class UploadTask:
    """Represents an active or completed file upload task."""

    upload_id: str
    local_path: str
    remote_path: str
    filename: str
    total_bytes: int = 0
    uploaded_bytes: int = 0
    status: str = "pending"  # pending, uploading, completed, skipped, error
    error_message: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def progress_percent(self) -> float:
        """Calculate upload progress as a percentage."""
        if self.total_bytes == 0:
            return 0.0
        return (self.uploaded_bytes / self.total_bytes) * 100


@dataclass
class FolderUploadTask:
    """Represents an active or completed folder upload task."""

    upload_id: str
    local_path: str
    remote_path: str
    folder_name: str
    total_files: int = 0
    completed_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    uploaded_bytes: int = 0
    status: str = "pending"  # pending, uploading, completed, error
    conflict_action: str = "skip"  # skip, overwrite, rename
    error_message: str | None = None
    files_with_conflicts: list[str] = field(default_factory=list)
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def progress_percent(self) -> float:
        """Calculate upload progress as a percentage."""
        if self.total_bytes == 0:
            if self.total_files == 0:
                return 0.0
            return ((self.completed_files + self.skipped_files) / self.total_files) * 100
        return (self.uploaded_bytes / self.total_bytes) * 100


class DownloadManager:
    """
    Manages background file downloads from pCloud to the local system.

    Downloads run asynchronously so users can continue interacting with the LLM
    while files download in the background.
    """

    def __init__(self):
        self._downloads: dict[str, DownloadTask] = {}
        self._folder_downloads: dict[str, FolderDownloadTask] = {}

    def get_download(self, download_id: str) -> DownloadTask | None:
        """Get a download task by ID."""
        return self._downloads.get(download_id)

    def list_downloads(self) -> list[DownloadTask]:
        """List all download tasks."""
        return list(self._downloads.values())

    async def start_download(
        self,
        file_id: int | None = None,
        file_path: str | None = None,
        local_path: str | None = None,
    ) -> DownloadTask:
        """
        Start a background download of a file from pCloud.

        Args:
            file_id: The pCloud file ID to download
            file_path: The pCloud file path to download (alternative to file_id)
            local_path: Local directory or file path to save to (defaults to current dir)

        Returns:
            DownloadTask with download_id for tracking progress
        """
        if file_id is None and file_path is None:
            raise ValueError("Either file_id or file_path must be provided")

        # Get file link from pCloud
        params: dict[str, Any] = {"forcedownload": 1}
        if file_id is not None:
            params["fileid"] = file_id
        else:
            params["path"] = file_path

        logger.info(f"Getting download link for file_id={file_id}, path={file_path}")
        result = await auth.make_request("getfilelink", params)

        # Extract download URL info
        hosts = result.get("hosts", [])
        path = result.get("path", "")

        if not hosts or not path:
            raise ValueError("Failed to get download link from pCloud")

        download_url = f"https://{hosts[0]}{path}"

        # Extract filename from path
        filename = Path(path).name
        # URL decode the filename
        from urllib.parse import unquote
        filename = unquote(filename)

        # Determine local save path
        if local_path is None:
            local_path = os.getcwd()

        local_path_obj = Path(local_path)
        if local_path_obj.is_dir() or not local_path_obj.suffix:
            # It's a directory, append filename
            local_path_obj.mkdir(parents=True, exist_ok=True)
            save_path = local_path_obj / filename
        else:
            # It's a file path
            local_path_obj.parent.mkdir(parents=True, exist_ok=True)
            save_path = local_path_obj

        # Create download task
        download_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            download_id=download_id,
            file_id=file_id,
            file_path=file_path,
            filename=filename,
            local_path=str(save_path),
        )
        self._downloads[download_id] = task

        # Start background download
        task.task = asyncio.create_task(
            self._download_file(task, download_url)
        )

        logger.info(f"Started download {download_id}: {filename} -> {save_path}")
        return task

    async def _download_file(self, task: DownloadTask, url: str) -> None:
        """Background coroutine that performs the actual download."""
        task.status = "downloading"

        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()

                    # Get total size from Content-Length header
                    total = response.headers.get("Content-Length")
                    if total:
                        task.total_bytes = int(total)

                    # Stream to file
                    with open(task.local_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            task.downloaded_bytes += len(chunk)

            task.status = "completed"
            logger.info(
                f"Download {task.download_id} completed: {task.filename} "
                f"({task.downloaded_bytes} bytes)"
            )

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            logger.error(f"Download {task.download_id} failed: {e}")

    def cancel_download(self, download_id: str) -> bool:
        """Cancel a running download."""
        task = self._downloads.get(download_id)
        if task and task.task and not task.task.done():
            task.task.cancel()
            task.status = "error"
            task.error_message = "Cancelled by user"
            logger.info(f"Download {download_id} cancelled")
            return True
        return False

    def clear_completed(self) -> int:
        """Remove completed and errored downloads from the list."""
        to_remove = [
            did for did, task in self._downloads.items()
            if task.status in ("completed", "error")
        ]
        for did in to_remove:
            del self._downloads[did]
        return len(to_remove)

    def get_folder_download(self, download_id: str) -> FolderDownloadTask | None:
        """Get a folder download task by ID."""
        return self._folder_downloads.get(download_id)

    def list_folder_downloads(self) -> list[FolderDownloadTask]:
        """List all folder download tasks."""
        return list(self._folder_downloads.values())

    async def start_folder_download(
        self,
        folder_id: int | None = None,
        folder_path: str | None = None,
        local_path: str | None = None,
        max_concurrent: int = 3,
    ) -> FolderDownloadTask:
        """
        Start a background download of an entire folder from pCloud.

        Args:
            folder_id: The pCloud folder ID to download
            folder_path: The pCloud folder path to download (alternative to folder_id)
            local_path: Local directory to save to (defaults to current dir)
            max_concurrent: Maximum concurrent file downloads (default: 3)

        Returns:
            FolderDownloadTask with download_id for tracking progress
        """
        if folder_id is None and folder_path is None:
            raise ValueError("Either folder_id or folder_path must be provided")

        # Get folder contents recursively
        params: dict[str, Any] = {"recursive": 1}
        if folder_id is not None:
            params["folderid"] = folder_id
        else:
            params["path"] = folder_path

        logger.info(f"Listing folder for download: folder_id={folder_id}, path={folder_path}")
        result = await auth.make_request("listfolder", params)

        metadata = result.get("metadata", {})
        folder_name = metadata.get("name", "download")

        # Determine local save path
        if local_path is None:
            local_path = os.getcwd()

        local_base = Path(local_path) / folder_name
        local_base.mkdir(parents=True, exist_ok=True)

        # Collect all files from the folder tree
        files_to_download: list[dict[str, Any]] = []
        self._collect_files(metadata, "", files_to_download)

        # Calculate total size
        total_bytes = sum(f.get("size", 0) for f in files_to_download)

        # Create folder download task
        download_id = f"folder-{str(uuid.uuid4())[:8]}"
        folder_task = FolderDownloadTask(
            download_id=download_id,
            folder_id=folder_id,
            folder_path=folder_path,
            folder_name=folder_name,
            local_path=str(local_base),
            total_files=len(files_to_download),
            total_bytes=total_bytes,
        )
        self._folder_downloads[download_id] = folder_task

        # Start background folder download
        folder_task.task = asyncio.create_task(
            self._download_folder(folder_task, files_to_download, local_base, max_concurrent)
        )

        logger.info(
            f"Started folder download {download_id}: {folder_name} "
            f"({len(files_to_download)} files, {total_bytes} bytes) -> {local_base}"
        )
        return folder_task

    def _collect_files(
        self,
        folder_metadata: dict[str, Any],
        relative_path: str,
        files: list[dict[str, Any]],
    ) -> None:
        """Recursively collect all files from folder metadata."""
        contents = folder_metadata.get("contents", [])

        for item in contents:
            if item.get("isfolder"):
                # Recurse into subfolder
                subfolder_name = item.get("name", "")
                new_path = f"{relative_path}/{subfolder_name}" if relative_path else subfolder_name
                self._collect_files(item, new_path, files)
            else:
                # It's a file - add to list with relative path info
                item["_relative_path"] = relative_path
                files.append(item)

    async def _download_folder(
        self,
        folder_task: FolderDownloadTask,
        files: list[dict[str, Any]],
        local_base: Path,
        max_concurrent: int,
    ) -> None:
        """Background coroutine that downloads all files in a folder."""
        folder_task.status = "downloading"

        # Use semaphore to limit concurrent downloads
        semaphore = asyncio.Semaphore(max_concurrent)

        async def download_single_file(file_info: dict[str, Any]) -> None:
            async with semaphore:
                file_id = file_info.get("fileid")
                filename = file_info.get("name", "unknown")
                relative_path = file_info.get("_relative_path", "")
                file_size = file_info.get("size", 0)

                # Create subdirectory if needed
                if relative_path:
                    local_dir = local_base / relative_path
                    local_dir.mkdir(parents=True, exist_ok=True)
                else:
                    local_dir = local_base

                local_file_path = local_dir / filename

                try:
                    # Get download link
                    result = await auth.make_request("getfilelink", {"fileid": file_id})
                    hosts = result.get("hosts", [])
                    path = result.get("path", "")

                    if not hosts or not path:
                        raise ValueError(f"Failed to get download link for {filename}")

                    download_url = f"https://{hosts[0]}{path}"

                    # Download the file
                    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
                        async with client.stream("GET", download_url) as response:
                            response.raise_for_status()

                            with open(local_file_path, "wb") as f:
                                async for chunk in response.aiter_bytes(chunk_size=8192):
                                    f.write(chunk)
                                    folder_task.downloaded_bytes += len(chunk)

                    folder_task.completed_files += 1
                    logger.debug(f"Downloaded: {relative_path}/{filename}")

                except Exception as e:
                    folder_task.failed_files += 1
                    logger.error(f"Failed to download {filename}: {e}")

        try:
            # Download all files concurrently (limited by semaphore)
            await asyncio.gather(*[download_single_file(f) for f in files])

            if folder_task.failed_files == 0:
                folder_task.status = "completed"
                logger.info(
                    f"Folder download {folder_task.download_id} completed: "
                    f"{folder_task.completed_files} files"
                )
            else:
                folder_task.status = "completed"
                folder_task.error_message = f"{folder_task.failed_files} files failed to download"
                logger.warning(
                    f"Folder download {folder_task.download_id} completed with errors: "
                    f"{folder_task.completed_files} succeeded, {folder_task.failed_files} failed"
                )

        except Exception as e:
            folder_task.status = "error"
            folder_task.error_message = str(e)
            logger.error(f"Folder download {folder_task.download_id} failed: {e}")

    def cancel_folder_download(self, download_id: str) -> bool:
        """Cancel a running folder download."""
        task = self._folder_downloads.get(download_id)
        if task and task.task and not task.task.done():
            task.task.cancel()
            task.status = "error"
            task.error_message = "Cancelled by user"
            logger.info(f"Folder download {download_id} cancelled")
            return True
        return False


class UploadManager:
    """
    Manages background file uploads from the local system to pCloud.

    Uploads run asynchronously so users can continue interacting with the LLM
    while files upload in the background.
    """

    def __init__(self):
        self._uploads: dict[str, UploadTask] = {}
        self._folder_uploads: dict[str, FolderUploadTask] = {}

    def get_upload(self, upload_id: str) -> UploadTask | None:
        """Get an upload task by ID."""
        return self._uploads.get(upload_id)

    def list_uploads(self) -> list[UploadTask]:
        """List all upload tasks."""
        return list(self._uploads.values())

    def get_folder_upload(self, upload_id: str) -> FolderUploadTask | None:
        """Get a folder upload task by ID."""
        return self._folder_uploads.get(upload_id)

    def list_folder_uploads(self) -> list[FolderUploadTask]:
        """List all folder upload tasks."""
        return list(self._folder_uploads.values())

    async def start_upload(
        self,
        local_path: str,
        remote_path: str = "/",
        conflict_action: str = "skip",
    ) -> UploadTask:
        """
        Start a background upload of a file to pCloud.

        Args:
            local_path: Local path to the file to upload
            remote_path: Remote folder path in pCloud (defaults to root)
            conflict_action: What to do if file exists: "skip", "overwrite", "rename"

        Returns:
            UploadTask with upload_id for tracking progress
        """
        local_file = Path(local_path)
        if not local_file.exists():
            raise ValueError(f"File not found: {local_path}")
        if not local_file.is_file():
            raise ValueError(f"Not a file: {local_path}")

        filename = local_file.name
        file_size = local_file.stat().st_size

        # Create upload task
        upload_id = f"upload-{str(uuid.uuid4())[:8]}"
        task = UploadTask(
            upload_id=upload_id,
            local_path=str(local_file),
            remote_path=remote_path,
            filename=filename,
            total_bytes=file_size,
        )
        self._uploads[upload_id] = task

        # Start background upload
        task.task = asyncio.create_task(
            self._upload_file(task, conflict_action)
        )

        logger.info(f"Started upload {upload_id}: {filename} -> {remote_path}")
        return task

    async def _upload_file(self, task: UploadTask, conflict_action: str) -> None:
        """Background coroutine that performs the actual upload."""
        task.status = "uploading"

        try:
            # Resolve remote path to folder ID
            folder_id = await auth.resolve_path_to_folder_id(task.remote_path)

            # Check if file exists
            existing = await auth.check_file_exists(folder_id, task.filename)

            if existing:
                if conflict_action == "skip":
                    task.status = "skipped"
                    task.error_message = "File already exists (skipped)"
                    logger.info(f"Upload {task.upload_id} skipped: {task.filename} already exists")
                    return
                elif conflict_action == "rename":
                    rename_if_exists = True
                else:  # overwrite
                    rename_if_exists = False
            else:
                rename_if_exists = False

            # Progress tracking callback
            def progress_callback(uploaded: int, total: int):
                task.uploaded_bytes = uploaded

            # Upload the file
            local_file = Path(task.local_path)
            result = await auth.upload_file(
                local_file,
                folder_id,
                rename_if_exists=rename_if_exists,
                progress_callback=progress_callback,
            )

            task.uploaded_bytes = task.total_bytes
            task.status = "completed"

            # Get the actual filename used (might be renamed)
            metadata_list = result.get("metadata", [])
            if metadata_list and len(metadata_list) > 0:
                actual_name = metadata_list[0].get("name", task.filename)
                if actual_name != task.filename:
                    logger.info(f"File renamed to: {actual_name}")

            logger.info(
                f"Upload {task.upload_id} completed: {task.filename} "
                f"({task.uploaded_bytes} bytes)"
            )

        except Exception as e:
            task.status = "error"
            task.error_message = str(e)
            logger.error(f"Upload {task.upload_id} failed: {e}")

    async def start_folder_upload(
        self,
        local_path: str,
        remote_path: str = "/",
        conflict_action: str = "skip",
        max_concurrent: int = 3,
    ) -> FolderUploadTask:
        """
        Start a background upload of an entire folder to pCloud.

        Args:
            local_path: Local path to the folder to upload
            remote_path: Remote folder path in pCloud (defaults to root)
            conflict_action: What to do if file exists: "skip", "overwrite", "rename"
            max_concurrent: Maximum concurrent file uploads (default: 3)

        Returns:
            FolderUploadTask with upload_id for tracking progress
        """
        local_folder = Path(local_path)
        if not local_folder.exists():
            raise ValueError(f"Folder not found: {local_path}")
        if not local_folder.is_dir():
            raise ValueError(f"Not a folder: {local_path}")

        folder_name = local_folder.name

        # Collect all files to upload
        files_to_upload: list[dict[str, Any]] = []
        total_bytes = 0

        for file_path in local_folder.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(local_folder)
                file_size = file_path.stat().st_size

                files_to_upload.append({
                    "local_path": file_path,
                    "relative_path": str(relative_path.parent) if relative_path.parent != Path(".") else "",
                    "filename": file_path.name,
                    "size": file_size,
                })
                total_bytes += file_size

        # Create folder upload task
        upload_id = f"folder-upload-{str(uuid.uuid4())[:8]}"
        task = FolderUploadTask(
            upload_id=upload_id,
            local_path=str(local_folder),
            remote_path=remote_path,
            folder_name=folder_name,
            total_files=len(files_to_upload),
            total_bytes=total_bytes,
            conflict_action=conflict_action,
        )
        self._folder_uploads[upload_id] = task

        # Start background folder upload
        task.task = asyncio.create_task(
            self._upload_folder(task, files_to_upload, remote_path, max_concurrent)
        )

        logger.info(
            f"Started folder upload {upload_id}: {folder_name} "
            f"({len(files_to_upload)} files, {total_bytes} bytes) -> {remote_path}"
        )
        return task

    async def _upload_folder(
        self,
        folder_task: FolderUploadTask,
        files: list[dict[str, Any]],
        remote_base: str,
        max_concurrent: int,
    ) -> None:
        """Background coroutine that uploads all files in a folder."""
        folder_task.status = "uploading"

        # Use semaphore to limit concurrent uploads
        semaphore = asyncio.Semaphore(max_concurrent)

        # Ensure remote base path ends properly
        if not remote_base.endswith("/"):
            remote_base = remote_base + "/"
        remote_base = f"{remote_base}{folder_task.folder_name}"

        async def upload_single_file(file_info: dict[str, Any]) -> None:
            async with semaphore:
                local_path: Path = file_info["local_path"]
                relative_path: str = file_info["relative_path"]
                filename: str = file_info["filename"]
                file_size: int = file_info["size"]

                # Build remote path for this file
                if relative_path:
                    file_remote_path = f"{remote_base}/{relative_path}"
                else:
                    file_remote_path = remote_base

                try:
                    # Resolve remote path to folder ID (creates folders as needed)
                    folder_id = await auth.resolve_path_to_folder_id(file_remote_path)

                    # Check if file exists
                    existing = await auth.check_file_exists(folder_id, filename)

                    if existing:
                        if folder_task.conflict_action == "skip":
                            folder_task.skipped_files += 1
                            folder_task.uploaded_bytes += file_size  # Count as "done" for progress
                            logger.debug(f"Skipped (exists): {relative_path}/{filename}")
                            return
                        elif folder_task.conflict_action == "rename":
                            rename_if_exists = True
                        else:  # overwrite
                            rename_if_exists = False
                    else:
                        rename_if_exists = False

                    # Upload the file
                    await auth.upload_file(
                        local_path,
                        folder_id,
                        rename_if_exists=rename_if_exists,
                    )

                    folder_task.completed_files += 1
                    folder_task.uploaded_bytes += file_size
                    logger.debug(f"Uploaded: {relative_path}/{filename}")

                except Exception as e:
                    folder_task.failed_files += 1
                    folder_task.uploaded_bytes += file_size  # Count for progress even on failure
                    logger.error(f"Failed to upload {filename}: {e}")

        try:
            # Upload all files concurrently (limited by semaphore)
            await asyncio.gather(*[upload_single_file(f) for f in files])

            if folder_task.failed_files == 0:
                folder_task.status = "completed"
                logger.info(
                    f"Folder upload {folder_task.upload_id} completed: "
                    f"{folder_task.completed_files} uploaded, {folder_task.skipped_files} skipped"
                )
            else:
                folder_task.status = "completed"
                folder_task.error_message = f"{folder_task.failed_files} files failed to upload"
                logger.warning(
                    f"Folder upload {folder_task.upload_id} completed with errors: "
                    f"{folder_task.completed_files} uploaded, {folder_task.skipped_files} skipped, "
                    f"{folder_task.failed_files} failed"
                )

        except Exception as e:
            folder_task.status = "error"
            folder_task.error_message = str(e)
            logger.error(f"Folder upload {folder_task.upload_id} failed: {e}")

    def cancel_upload(self, upload_id: str) -> bool:
        """Cancel a running upload."""
        task = self._uploads.get(upload_id)
        if task and task.task and not task.task.done():
            task.task.cancel()
            task.status = "error"
            task.error_message = "Cancelled by user"
            logger.info(f"Upload {upload_id} cancelled")
            return True
        return False

    def cancel_folder_upload(self, upload_id: str) -> bool:
        """Cancel a running folder upload."""
        task = self._folder_uploads.get(upload_id)
        if task and task.task and not task.task.done():
            task.task.cancel()
            task.status = "error"
            task.error_message = "Cancelled by user"
            logger.info(f"Folder upload {upload_id} cancelled")
            return True
        return False


# Global managers
download_manager = DownloadManager()
upload_manager = UploadManager()


@mcp.tool()
async def list_folder(
    folder_id: int = 0,
    path: str | None = None,
    recursive: bool = False,
    show_deleted: bool = False,
    no_files: bool = False,
    no_shares: bool = False,
) -> dict[str, Any]:
    """
    List the contents of a folder in pCloud.

    Args:
        folder_id: The folder ID to list (default: 0 for root folder)
        path: Alternative to folder_id - the path to the folder (discouraged by pCloud)
        recursive: If True, return full directory tree with contents for all directories
        show_deleted: If True, show deleted files and folders that can be restored
        no_files: If True, return only folder structure without files
        no_shares: If True, show only user's own folders and files (no shared content)

    Returns:
        Dictionary containing folder metadata and contents array with metadata
        for each item (files and subfolders)

    Possible errors:
        - 1000: Authentication required
        - 1002: Neither path nor folderid provided
        - 2000: Authentication failed
        - 2003: Insufficient permissions
        - 2005: Folder does not exist
        - 4000: IP address rate-limited
        - 5000: Server error
    """
    params: dict[str, Any] = {}

    # Use path if provided, otherwise use folder_id
    if path is not None:
        params["path"] = path
        logger.info(f"Listing folder by path: {path}")
    else:
        params["folderid"] = folder_id
        logger.info(f"Listing folder by ID: {folder_id}")

    # Add optional parameters
    if recursive:
        params["recursive"] = 1
    if show_deleted:
        params["showdeleted"] = 1
    if no_files:
        params["nofiles"] = 1
    if no_shares:
        params["noshares"] = 1

    result = await auth.make_request("listfolder", params)

    # Log some stats about the result
    metadata = result.get("metadata", {})
    contents = metadata.get("contents", [])
    logger.info(f"Listed {len(contents)} items in folder")

    return result


@mcp.tool()
async def download_file(
    file_id: int | None = None,
    path: str | None = None,
    local_path: str | None = None,
) -> dict[str, Any]:
    """
    Start downloading a file from pCloud to the local system.

    The download runs in the background, allowing you to continue the conversation.
    Use get_download_status to check progress or list_downloads to see all downloads.

    Args:
        file_id: The pCloud file ID to download
        path: The pCloud file path to download (alternative to file_id)
        local_path: Local directory or file path to save to (defaults to current directory)

    Returns:
        Dictionary with download_id and initial status information

    Example:
        download_file(file_id=12345, local_path="/Users/me/Downloads")
        download_file(path="/My Documents/report.pdf")
    """
    task = await download_manager.start_download(
        file_id=file_id,
        file_path=path,
        local_path=local_path,
    )

    return {
        "download_id": task.download_id,
        "filename": task.filename,
        "local_path": task.local_path,
        "status": task.status,
        "message": f"Download started. Use get_download_status('{task.download_id}') to check progress.",
    }


@mcp.tool()
async def get_download_status(download_id: str) -> dict[str, Any]:
    """
    Get the status and progress of a specific download.

    Args:
        download_id: The download ID returned from download_file

    Returns:
        Dictionary with current download status, progress, and any error messages
    """
    task = download_manager.get_download(download_id)

    if task is None:
        return {
            "error": f"Download '{download_id}' not found",
            "available_downloads": [d.download_id for d in download_manager.list_downloads()],
        }

    result = {
        "download_id": task.download_id,
        "filename": task.filename,
        "local_path": task.local_path,
        "status": task.status,
        "downloaded_bytes": task.downloaded_bytes,
        "total_bytes": task.total_bytes,
        "progress_percent": round(task.progress_percent, 1),
    }

    if task.error_message:
        result["error_message"] = task.error_message

    return result


@mcp.tool()
async def list_downloads() -> dict[str, Any]:
    """
    List all active and recent downloads with their status.

    Returns:
        Dictionary containing list of all downloads and summary statistics
    """
    downloads = download_manager.list_downloads()

    download_list = []
    for task in downloads:
        download_list.append({
            "download_id": task.download_id,
            "filename": task.filename,
            "status": task.status,
            "progress_percent": round(task.progress_percent, 1),
            "local_path": task.local_path,
        })

    # Count by status
    status_counts = {}
    for task in downloads:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    return {
        "downloads": download_list,
        "total_count": len(downloads),
        "status_counts": status_counts,
    }


@mcp.tool()
async def cancel_download(download_id: str) -> dict[str, Any]:
    """
    Cancel an in-progress download.

    Args:
        download_id: The download ID to cancel

    Returns:
        Dictionary indicating success or failure
    """
    success = download_manager.cancel_download(download_id)

    if success:
        return {
            "success": True,
            "message": f"Download '{download_id}' has been cancelled",
        }
    else:
        task = download_manager.get_download(download_id)
        if task is None:
            return {
                "success": False,
                "error": f"Download '{download_id}' not found",
            }
        else:
            return {
                "success": False,
                "error": f"Download '{download_id}' is not running (status: {task.status})",
            }


@mcp.tool()
async def download_folder(
    folder_id: int | None = None,
    path: str | None = None,
    local_path: str | None = None,
) -> dict[str, Any]:
    """
    Start downloading an entire folder from pCloud to the local system.

    Downloads all files in the folder (including subfolders) while preserving
    the directory structure. The download runs in the background.

    Args:
        folder_id: The pCloud folder ID to download
        path: The pCloud folder path to download (alternative to folder_id)
        local_path: Local directory to save to (defaults to current directory)

    Returns:
        Dictionary with download_id, file count, and size information
    """
    task = await download_manager.start_folder_download(
        folder_id=folder_id,
        folder_path=path,
        local_path=local_path,
    )

    return {
        "download_id": task.download_id,
        "folder_name": task.folder_name,
        "local_path": task.local_path,
        "total_files": task.total_files,
        "total_bytes": task.total_bytes,
        "status": task.status,
        "message": f"Folder download started. Use get_download_status('{task.download_id}') to check progress.",
    }


@mcp.tool()
async def get_folder_download_status(download_id: str) -> dict[str, Any]:
    """
    Get the status and progress of a folder download.

    Args:
        download_id: The download ID returned from download_folder

    Returns:
        Dictionary with current download status, file counts, and progress
    """
    task = download_manager.get_folder_download(download_id)

    if task is None:
        return {
            "error": f"Folder download '{download_id}' not found",
            "available_downloads": [d.download_id for d in download_manager.list_folder_downloads()],
        }

    result = {
        "download_id": task.download_id,
        "folder_name": task.folder_name,
        "local_path": task.local_path,
        "status": task.status,
        "total_files": task.total_files,
        "completed_files": task.completed_files,
        "failed_files": task.failed_files,
        "total_bytes": task.total_bytes,
        "downloaded_bytes": task.downloaded_bytes,
        "progress_percent": round(task.progress_percent, 1),
    }

    if task.error_message:
        result["error_message"] = task.error_message

    return result


@mcp.tool()
async def upload_file(
    local_path: str,
    remote_path: str = "/",
    if_exists: str = "skip",
) -> dict[str, Any]:
    """
    Upload a file from the local system to pCloud.

    The upload runs in the background. Remote folders are created automatically
    if they don't exist.

    Args:
        local_path: Local path to the file to upload
        remote_path: Remote folder path in pCloud (defaults to root "/")
        if_exists: What to do if file already exists: "skip" (default), "overwrite", or "rename"

    Returns:
        Dictionary with upload_id and initial status information
    """
    if if_exists not in ("skip", "overwrite", "rename"):
        return {
            "error": f"Invalid if_exists value: {if_exists}. Must be 'skip', 'overwrite', or 'rename'",
        }

    try:
        task = await upload_manager.start_upload(
            local_path=local_path,
            remote_path=remote_path,
            conflict_action=if_exists,
        )

        return {
            "upload_id": task.upload_id,
            "filename": task.filename,
            "remote_path": task.remote_path,
            "total_bytes": task.total_bytes,
            "status": task.status,
            "message": f"Upload started. Use get_upload_status('{task.upload_id}') to check progress.",
        }
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def upload_folder(
    local_path: str,
    remote_path: str = "/",
    if_exists: str = "skip",
) -> dict[str, Any]:
    """
    Upload an entire folder from the local system to pCloud.

    Uploads all files in the folder (including subfolders) while preserving
    the directory structure. Remote folders are created automatically.
    The upload runs in the background.

    Args:
        local_path: Local path to the folder to upload
        remote_path: Remote folder path in pCloud (defaults to root "/")
        if_exists: What to do if files already exist: "skip" (default), "overwrite", or "rename"

    Returns:
        Dictionary with upload_id, file count, and size information
    """
    if if_exists not in ("skip", "overwrite", "rename"):
        return {
            "error": f"Invalid if_exists value: {if_exists}. Must be 'skip', 'overwrite', or 'rename'",
        }

    try:
        task = await upload_manager.start_folder_upload(
            local_path=local_path,
            remote_path=remote_path,
            conflict_action=if_exists,
        )

        return {
            "upload_id": task.upload_id,
            "folder_name": task.folder_name,
            "remote_path": task.remote_path,
            "total_files": task.total_files,
            "total_bytes": task.total_bytes,
            "status": task.status,
            "if_exists": if_exists,
            "message": f"Folder upload started. Use get_upload_status('{task.upload_id}') to check progress.",
        }
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def get_upload_status(upload_id: str) -> dict[str, Any]:
    """
    Get the status and progress of an upload (file or folder).

    Args:
        upload_id: The upload ID returned from upload_file or upload_folder

    Returns:
        Dictionary with current upload status, progress, and any error messages
    """
    # Check file uploads first
    task = upload_manager.get_upload(upload_id)
    if task is not None:
        result = {
            "upload_id": task.upload_id,
            "type": "file",
            "filename": task.filename,
            "remote_path": task.remote_path,
            "status": task.status,
            "uploaded_bytes": task.uploaded_bytes,
            "total_bytes": task.total_bytes,
            "progress_percent": round(task.progress_percent, 1),
        }
        if task.error_message:
            result["error_message"] = task.error_message
        return result

    # Check folder uploads
    folder_task = upload_manager.get_folder_upload(upload_id)
    if folder_task is not None:
        result = {
            "upload_id": folder_task.upload_id,
            "type": "folder",
            "folder_name": folder_task.folder_name,
            "remote_path": folder_task.remote_path,
            "status": folder_task.status,
            "total_files": folder_task.total_files,
            "completed_files": folder_task.completed_files,
            "skipped_files": folder_task.skipped_files,
            "failed_files": folder_task.failed_files,
            "uploaded_bytes": folder_task.uploaded_bytes,
            "total_bytes": folder_task.total_bytes,
            "progress_percent": round(folder_task.progress_percent, 1),
        }
        if folder_task.error_message:
            result["error_message"] = folder_task.error_message
        return result

    # Not found
    all_uploads = [u.upload_id for u in upload_manager.list_uploads()]
    all_folder_uploads = [u.upload_id for u in upload_manager.list_folder_uploads()]

    return {
        "error": f"Upload '{upload_id}' not found",
        "available_uploads": all_uploads + all_folder_uploads,
    }


@mcp.tool()
async def list_uploads() -> dict[str, Any]:
    """
    List all active and recent uploads with their status.

    Returns:
        Dictionary containing list of all uploads and summary statistics
    """
    file_uploads = upload_manager.list_uploads()
    folder_uploads = upload_manager.list_folder_uploads()

    upload_list = []

    for task in file_uploads:
        upload_list.append({
            "upload_id": task.upload_id,
            "type": "file",
            "filename": task.filename,
            "status": task.status,
            "progress_percent": round(task.progress_percent, 1),
        })

    for task in folder_uploads:
        upload_list.append({
            "upload_id": task.upload_id,
            "type": "folder",
            "folder_name": task.folder_name,
            "status": task.status,
            "progress_percent": round(task.progress_percent, 1),
            "files": f"{task.completed_files + task.skipped_files}/{task.total_files}",
        })

    # Count by status
    status_counts: dict[str, int] = {}
    for task in file_uploads:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1
    for task in folder_uploads:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    return {
        "uploads": upload_list,
        "total_count": len(upload_list),
        "status_counts": status_counts,
    }


@mcp.tool()
async def cancel_upload(upload_id: str) -> dict[str, Any]:
    """
    Cancel an in-progress upload (file or folder).

    Args:
        upload_id: The upload ID to cancel

    Returns:
        Dictionary indicating success or failure
    """
    # Try file upload first
    if upload_manager.cancel_upload(upload_id):
        return {
            "success": True,
            "message": f"Upload '{upload_id}' has been cancelled",
        }

    # Try folder upload
    if upload_manager.cancel_folder_upload(upload_id):
        return {
            "success": True,
            "message": f"Folder upload '{upload_id}' has been cancelled",
        }

    # Check if it exists but isn't running
    task = upload_manager.get_upload(upload_id)
    folder_task = upload_manager.get_folder_upload(upload_id)

    if task is not None:
        return {
            "success": False,
            "error": f"Upload '{upload_id}' is not running (status: {task.status})",
        }
    elif folder_task is not None:
        return {
            "success": False,
            "error": f"Upload '{upload_id}' is not running (status: {folder_task.status})",
        }
    else:
        return {
            "success": False,
            "error": f"Upload '{upload_id}' not found",
        }


def main():
    """Run the MCP server."""
    logger.info("Starting pCloud MCP server...")
    mcp.run()


if __name__ == "__main__":
    main()
