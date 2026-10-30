import os
import posixpath
import sys
import tempfile
from contextlib import closing, suppress

import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode, UploadSessionCursor, CommitInfo

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)
from rich.panel import Panel
from rich.theme import Theme


custom_theme = Theme({
    "info": "cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
})
console = Console(theme=custom_theme)

CHUNK_SIZE = 32 * 1024 * 1024
SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


def authenticate() -> dropbox.Dropbox:
    """Authenticate using an environment variable or an entered token."""
    token = os.environ.get("DROPBOX_ACCESS_TOKEN")

    if not token:
        console.print(
            "DROPBOX_ACCESS_TOKEN environment variable not found.",
            style="warning",
        )
        token = Prompt.ask(
            "Please enter your Dropbox Access Token",
            password=True,
        )

    dbx = dropbox.Dropbox(token.strip())

    try:
        account = dbx.users_get_current_account()
    except Exception:
        dbx.close()
        raise

    console.print(
        f"Successfully connected as {account.name.display_name}",
        style="success",
        markup=False,
    )
    return dbx


def transfer_progress() -> Progress:
    """Create a byte-based progress display for either transfer direction."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def get_upload_paths():
    """Return the local source file and Dropbox destination path."""
    while True:
        local_path = Prompt.ask(
            "\nEnter the local path of the file to upload"
        )
        local_path = os.path.abspath(
            os.path.expanduser(local_path.strip())
        )

        if os.path.isfile(local_path):
            break

        console.print(
            f"File not found at: {local_path}. Please try again.",
            style="error",
            markup=False,
        )

    filename = os.path.basename(local_path)
    remote_dir = Prompt.ask(
        "Enter the remote Dropbox directory (e.g., /MyFolder)",
        default="/",
    ).strip().strip("/")

    dest_path = (
        f"/{remote_dir}/{filename}" if remote_dir else f"/{filename}"
    )

    return local_path, dest_path


def get_download_paths():
    """Return the local destination file and Dropbox source path."""
    while True:
        remote_path = Prompt.ask(
            "\nEnter the Dropbox file path (e.g., /MyFolder/file.zip)"
        ).strip()
        remote_path = "/" + remote_path.lstrip("/")

        # Dropbox paths always use forward slashes, even on Windows.
        filename = posixpath.basename(remote_path)

        if filename not in ("", ".", ".."):
            break

        console.print(
            "Please enter a Dropbox file path, not a directory path.",
            style="error",
        )

    local_dir = Prompt.ask(
        "Enter the local directory to save the file in",
        default=".",
    ).strip()
    local_dir = os.path.abspath(os.path.expanduser(local_dir))

    return os.path.join(local_dir, filename), remote_path


def upload_file(
    dbx: dropbox.Dropbox,
    local_path: str,
    dest_path: str,
):
    """Upload using a single request or a chunked upload session."""
    file_size = os.path.getsize(local_path)

    if file_size <= SINGLE_UPLOAD_LIMIT:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(
                description="Uploading file in a single batch...",
                total=None,
            )

            with open(local_path, "rb") as source:
                dbx.files_upload(
                    source.read(),
                    dest_path,
                    mode=WriteMode.overwrite,
                )
    else:
        commit = CommitInfo(
            path=dest_path,
            mode=WriteMode.overwrite,
        )

        with open(local_path, "rb") as source:
            with transfer_progress() as progress:
                task = progress.add_task(
                    "Uploading chunks...",
                    total=file_size,
                )

                chunk = source.read(CHUNK_SIZE)
                session = dbx.files_upload_session_start(chunk)
                cursor = UploadSessionCursor(
                    session_id=session.session_id,
                    offset=len(chunk),
                )
                progress.advance(task, len(chunk))

                while cursor.offset < file_size:
                    remaining = file_size - cursor.offset
                    chunk = source.read(min(CHUNK_SIZE, remaining))

                    if not chunk:
                        raise OSError(
                            "The local file ended before its expected size. "
                            "It may have changed during upload."
                        )

                    if cursor.offset + len(chunk) == file_size:
                        dbx.files_upload_session_finish(
                            chunk, cursor, commit
                        )
                    else:
                        dbx.files_upload_session_append_v2(chunk, cursor)

                    cursor.offset += len(chunk)
                    progress.advance(task, len(chunk))

    console.print(
        f"\nFile successfully uploaded to Dropbox at: {dest_path}",
        style="success",
        markup=False,
    )


def download_file(
    dbx: dropbox.Dropbox,
    remote_path: str,
    local_path: str,
):
    """Stream a file to a temporary file, then move it into place."""
    local_path = os.path.abspath(local_path)

    if os.path.isdir(local_path):
        raise IsADirectoryError(
            f"The local destination is a directory: {local_path}"
        )

    if os.path.lexists(local_path):
        if not Confirm.ask(
            "The local file already exists. Overwrite it?",
            default=False,
        ):
            console.print("Download cancelled.", style="warning")
            return

    destination_dir = os.path.dirname(local_path)
    os.makedirs(destination_dir, exist_ok=True)

    temporary_path = None

    try:
        metadata, response = dbx.files_download(remote_path)

        # Always close the HTTP response, including on errors or Ctrl+C.
        with closing(response), tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_dir,
            prefix=".dropbox-",
            suffix=".part",
            delete=False,
        ) as destination:
            temporary_path = destination.name
            downloaded = 0

            with transfer_progress() as progress:
                task = progress.add_task(
                    "Downloading...",
                    total=metadata.size,
                )

                # Stream the response rather than loading it all into RAM.
                for chunk in response.iter_content(
                    chunk_size=DOWNLOAD_CHUNK_SIZE
                ):
                    if not chunk:
                        continue

                    destination.write(chunk)
                    downloaded += len(chunk)
                    progress.advance(task, len(chunk))

            if downloaded != metadata.size:
                raise OSError(
                    f"Incomplete download: expected {metadata.size} bytes, "
                    f"received {downloaded}."
                )

        # The temporary file is in the destination directory, allowing an
        # atomic replacement after the download and file close succeed.
        os.replace(temporary_path, local_path)
        temporary_path = None

    finally:
        # Leave an existing destination untouched if the download fails.
        if temporary_path is not None:
            with suppress(OSError):
                os.remove(temporary_path)

    console.print(
        f"\nFile successfully downloaded to: {local_path}",
        style="success",
        markup=False,
    )


def main():
    console.print(Panel.fit(
        "[bold cyan]Dropbox Interactive Uploader / Downloader[/bold cyan]\n"
        "Upload files to Dropbox or download them to your computer.",
        border_style="cyan",
    ))

    dbx = authenticate()

    try:
        while True:
            action = Prompt.ask(
                "\nChoose an action",
                choices=["upload", "download", "quit"],
                default="upload",
            )

            if action == "quit":
                break

            try:
                if action == "upload":
                    local_path, remote_path = get_upload_paths()
                else:
                    local_path, remote_path = get_download_paths()

                console.print(
                    f"\n[bold]{action.title()} summary:[/bold]"
                )
                console.print(
                    f"  • Local file: {local_path}",
                    style="info",
                    markup=False,
                )
                console.print(
                    f"  • Dropbox file: {remote_path}",
                    style="info",
                    markup=False,
                )

                if action == "upload":
                    size_mib = os.path.getsize(local_path) / (1024 * 1024)
                    console.print(
                        f"  • Size: {size_mib:.2f} MiB",
                        style="info",
                    )

                if not Confirm.ask(
                    f"\nProceed with {action}?",
                    default=True,
                ):
                    console.print(
                        f"{action.title()} cancelled.",
                        style="warning",
                    )
                    continue

                if action == "upload":
                    upload_file(dbx, local_path, remote_path)
                else:
                    download_file(dbx, remote_path, local_path)

            except (ApiError, AuthError) as err:
                console.print(
                    f"\nDropbox error: {err}",
                    style="error",
                    markup=False,
                )
            except Exception as err:
                console.print(
                    f"\nOperation failed: {err}",
                    style="error",
                    markup=False,
                )

    finally:
        dbx.close()

    console.print("Goodbye!", style="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(
            "\nOperation cancelled by user.",
            style="warning",
        )
        sys.exit(0)
    except AuthError as err:
        console.print(
            f"Authentication failed. Check your token and scopes.\n{err}",
            style="error",
            markup=False,
        )
        sys.exit(1)
    except Exception as err:
        console.print(
            f"Error: {err}",
            style="error",
            markup=False,
        )
        sys.exit(1)
