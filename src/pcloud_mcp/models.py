"""
Data models for pCloud MCP Server.

Contains dataclasses for tracking download and upload tasks.
"""

import asyncio
from dataclasses import dataclass, field


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
