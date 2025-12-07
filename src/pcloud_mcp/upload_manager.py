"""
Upload manager for pCloud MCP Server.

Handles background file and folder uploads with progress tracking.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from .auth import auth, logger
from .models import FolderUploadTask, UploadTask


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


# Global upload manager instance
upload_manager = UploadManager()
