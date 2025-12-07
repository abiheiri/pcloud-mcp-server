"""
Download manager for pCloud MCP Server.

Handles background file and folder downloads with progress tracking.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from .auth import auth, logger
from .models import DownloadTask, FolderDownloadTask


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


# Global download manager instance
download_manager = DownloadManager()
