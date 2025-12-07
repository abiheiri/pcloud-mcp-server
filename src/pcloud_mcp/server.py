"""
pCloud MCP Server - Tool definitions.

This module defines all the MCP tools for interacting with pCloud.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import auth, logger
from .download_manager import download_manager
from .upload_manager import upload_manager

# Initialize the MCP server
mcp = FastMCP("pcloud")


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


# =============================================================================
# Folder Operations (rename, move, delete)
# =============================================================================


@mcp.tool()
async def rename_folder(
    folder_id: int | None = None,
    path: str | None = None,
    new_name: str | None = None,
    to_path: str | None = None,
    to_folder_id: int | None = None,
) -> dict[str, Any]:
    """
    Rename or move a folder in pCloud.

    You can rename a folder, move it to a different location, or both at once.

    Args:
        folder_id: The folder ID to rename/move
        path: The folder path to rename/move (alternative to folder_id)
        new_name: New name for the folder (for renaming)
        to_path: Destination path (for moving). Must end with "/" if just moving.
        to_folder_id: Destination folder ID (for moving)

    Returns:
        Dictionary with the updated folder metadata

    Examples:
        Rename: rename_folder(folder_id=123, new_name="New Name")
        Move: rename_folder(folder_id=123, to_folder_id=456)
        Both: rename_folder(path="/Old Folder", new_name="New Name", to_path="/Destination/")
    """
    if folder_id is None and path is None:
        return {"error": "Either folder_id or path must be provided"}

    if new_name is None and to_path is None and to_folder_id is None:
        return {"error": "At least one destination parameter required: new_name, to_path, or to_folder_id"}

    params: dict[str, Any] = {}

    if folder_id is not None:
        params["folderid"] = folder_id
    else:
        params["path"] = path

    if new_name is not None:
        params["toname"] = new_name
    if to_path is not None:
        params["topath"] = to_path
    if to_folder_id is not None:
        params["tofolderid"] = to_folder_id

    logger.info(f"Renaming/moving folder: {folder_id or path}")

    try:
        result = await auth.make_request("renamefolder", params)
        logger.info(f"Folder renamed/moved successfully")
        return result
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def delete_folder(
    folder_id: int | None = None,
    path: str | None = None,
    recursive: bool = False,
) -> dict[str, Any]:
    """
    Delete a folder in pCloud (moves to trash, not permanent).

    Deleted folders can be restored from trash using list_trash and restore_from_trash.

    Args:
        folder_id: The folder ID to delete
        path: The folder path to delete (alternative to folder_id)
        recursive: If True, delete folder and all contents. If False (default),
                   folder must be empty.

    Returns:
        Dictionary with deletion result. For recursive delete, includes counts
        of deleted files and folders.

    Note:
        Files are moved to trash, not permanently deleted. Use clear_trash
        for permanent deletion.
    """
    if folder_id is None and path is None:
        return {"error": "Either folder_id or path must be provided"}

    params: dict[str, Any] = {}

    if folder_id is not None:
        params["folderid"] = folder_id
    else:
        params["path"] = path

    method = "deletefolderrecursive" if recursive else "deletefolder"
    logger.info(f"Deleting folder {'recursively ' if recursive else ''}: {folder_id or path}")

    try:
        result = await auth.make_request(method, params)
        logger.info(f"Folder deleted successfully (moved to trash)")
        return {
            "success": True,
            "message": "Folder moved to trash",
            "result": result,
        }
    except ValueError as e:
        return {"error": str(e)}


# =============================================================================
# File Operations (rename, move, delete)
# =============================================================================


@mcp.tool()
async def rename_file(
    file_id: int | None = None,
    path: str | None = None,
    new_name: str | None = None,
    to_path: str | None = None,
    to_folder_id: int | None = None,
) -> dict[str, Any]:
    """
    Rename or move a file in pCloud.

    You can rename a file, move it to a different folder, or both at once.
    If destination file exists, it will be replaced atomically.

    Args:
        file_id: The file ID to rename/move
        path: The file path to rename/move (alternative to file_id)
        new_name: New name for the file (for renaming)
        to_path: Destination path (for moving). Must end with "/" if just moving.
        to_folder_id: Destination folder ID (for moving)

    Returns:
        Dictionary with the updated file metadata

    Examples:
        Rename: rename_file(file_id=123, new_name="new_name.txt")
        Move: rename_file(file_id=123, to_folder_id=456)
        Both: rename_file(path="/old.txt", new_name="new.txt", to_path="/Destination/")
    """
    if file_id is None and path is None:
        return {"error": "Either file_id or path must be provided"}

    if new_name is None and to_path is None and to_folder_id is None:
        return {"error": "At least one destination parameter required: new_name, to_path, or to_folder_id"}

    params: dict[str, Any] = {}

    if file_id is not None:
        params["fileid"] = file_id
    else:
        params["path"] = path

    if new_name is not None:
        params["toname"] = new_name
    if to_path is not None:
        params["topath"] = to_path
    if to_folder_id is not None:
        params["tofolderid"] = to_folder_id

    logger.info(f"Renaming/moving file: {file_id or path}")

    try:
        result = await auth.make_request("renamefile", params)
        logger.info(f"File renamed/moved successfully")
        return result
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def delete_file(
    file_id: int | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """
    Delete a file in pCloud (moves to trash, not permanent).

    Deleted files can be restored from trash using list_trash and restore_from_trash.

    Args:
        file_id: The file ID to delete
        path: The file path to delete (alternative to file_id)

    Returns:
        Dictionary with the deleted file metadata

    Note:
        Files are moved to trash, not permanently deleted. Use clear_trash
        for permanent deletion.
    """
    if file_id is None and path is None:
        return {"error": "Either file_id or path must be provided"}

    params: dict[str, Any] = {}

    if file_id is not None:
        params["fileid"] = file_id
    else:
        params["path"] = path

    logger.info(f"Deleting file: {file_id or path}")

    try:
        result = await auth.make_request("deletefile", params)
        logger.info(f"File deleted successfully (moved to trash)")
        return {
            "success": True,
            "message": "File moved to trash",
            "metadata": result.get("metadata", {}),
        }
    except ValueError as e:
        return {"error": str(e)}


# =============================================================================
# Trash Operations
# =============================================================================


@mcp.tool()
async def list_trash(
    folder_id: int = 0,
    recursive: bool = True,
) -> dict[str, Any]:
    """
    List files and folders in the trash.

    Args:
        folder_id: Folder ID to list trash contents from (default: 0 for root)
        recursive: If True (default), list all trash contents recursively

    Returns:
        Dictionary with trash contents including file/folder metadata
    """
    params: dict[str, Any] = {"folderid": folder_id}

    if recursive:
        params["recursive"] = 1

    logger.info(f"Listing trash contents")

    try:
        result = await auth.make_request("trash_list", params)
        return result
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
async def restore_from_trash(
    file_id: int | None = None,
    folder_id: int | None = None,
    restore_to_path: str | None = None,
) -> dict[str, Any]:
    """
    Restore a file or folder from trash.

    Args:
        file_id: The file ID to restore (from trash listing)
        folder_id: The folder ID to restore (from trash listing)
        restore_to_path: Optional path to restore to. If not provided,
                         restores to original location.

    Returns:
        Dictionary with the restored item metadata
    """
    if file_id is None and folder_id is None:
        return {"error": "Either file_id or folder_id must be provided"}

    params: dict[str, Any] = {}

    if file_id is not None:
        params["fileid"] = file_id
    if folder_id is not None:
        params["folderid"] = folder_id

    # Use restore to path if specified, otherwise restore to original location
    if restore_to_path is not None:
        method = "trash_restorepath"
        params["topath"] = restore_to_path
    else:
        method = "trash_restore"

    logger.info(f"Restoring from trash: file_id={file_id}, folder_id={folder_id}")

    try:
        result = await auth.make_request(method, params)
        logger.info(f"Item restored from trash successfully")
        return {
            "success": True,
            "message": "Item restored from trash",
            "result": result,
        }
    except ValueError as e:
        return {"error": str(e)}
