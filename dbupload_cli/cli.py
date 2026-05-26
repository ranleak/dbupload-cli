import os
import sys
import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode, UploadSessionCursor, CommitInfo

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
from rich.panel import Panel
from rich.theme import Theme

# Set up custom colors for the Rich console
custom_theme = Theme({
    "info": "cyan",
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red"
})
console = Console(theme=custom_theme)

# OPTIMIZATION: Increased chunk size from 4MB to 32MB to reduce HTTP round-trips on large files
CHUNK_SIZE = 32 * 1024 * 1024 
# OPTIMIZATION: Dropbox threshold for single-request uploads
SINGLE_UPLOAD_LIMIT = 150 * 1024 * 1024 

def authenticate() -> dropbox.Dropbox:
    """Authenticates with Dropbox using an access token."""
    token = os.environ.get("DROPBOX_ACCESS_TOKEN")
    
    if not token:
        console.print("[warning]DROPBOX_ACCESS_TOKEN environment variable not found.[/warning]")
        token = Prompt.ask("Please enter your Dropbox Access Token", password=True)
    
    dbx = dropbox.Dropbox(token)
    
    try:
        account = dbx.users_get_current_account()
        console.print(f"[success]Successfully connected as {account.name.display_name}[/success]")
        return dbx
    except AuthError:
        console.print("[error]Authentication failed. Please check your access token.[/error]")
        sys.exit(1)

def get_file_paths():
    """Prompts user for paths and validates them."""
    while True:
        local_path = Prompt.ask("\nEnter the local path of the file to upload")
        local_path = os.path.expanduser(local_path.strip())
        
        if os.path.isfile(local_path):
            break
        console.print(f"[error]File not found at: {local_path}. Please try again.[/error]")
        
    filename = os.path.basename(local_path)
    remote_dir = Prompt.ask("Enter the remote Dropbox directory (e.g., /MyFolder)", default="/")
    remote_dir = remote_dir.strip()
    
    if remote_dir in ["", "/"]:
        dest_path = f"/{filename}"
    else:
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir
        dest_path = f"{remote_dir.rstrip('/')}/{filename}"
        
    return local_path, dest_path

def upload_file(dbx: dropbox.Dropbox, local_path: str, dest_path: str):
    """Uploads file efficiently using single or chunked sessions based on size."""
    file_size = os.path.getsize(local_path)
    commit = CommitInfo(path=dest_path, mode=WriteMode.overwrite)

    try:
        # OPTIMIZATION: Files under 150MB are uploaded in a single API call (much faster)
        if file_size <= SINGLE_UPLOAD_LIMIT:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console
            ) as progress:
                progress.add_task(description="Uploading file in a single batch...", total=None)
                with open(local_path, "rb") as f:
                    dbx.files_upload(f.read(), dest_path, mode=WriteMode.overwrite)
        
        # Large files use chunked upload session with optimized 32MB chunks
        else:
            with open(local_path, "rb") as f:
                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TimeRemainingColumn(),
                    console=console
                ) as progress:
                    task = progress.add_task(description="Uploading chunks...", total=file_size)
                    
                    # Start upload session
                    res = dbx.files_upload_session_start(f.read(CHUNK_SIZE))
                    cursor = UploadSessionCursor(session_id=res.session_id, offset=f.tell())
                    progress.update(task, advance=CHUNK_SIZE)
                    
                    while f.tell() < file_size:
                        if (file_size - f.tell()) <= CHUNK_SIZE:
                            # Final chunk
                            dbx.files_upload_session_finish(f.read(CHUNK_SIZE), cursor, commit)
                            progress.update(task, completed=file_size)
                        else:
                            # Intermediate chunks
                            dbx.files_upload_session_append_v2(f.read(CHUNK_SIZE), cursor)
                            cursor.offset = f.tell()
                            progress.update(task, advance=CHUNK_SIZE)

        console.print(f"\n[success]✨ File successfully uploaded to Dropbox at:[/success] [cyan]{dest_path}[/cyan]")
        
    except ApiError as err:
        console.print(f"\n[error]Dropbox API Error:[/error] {err}")
    except Exception as err:
        console.print(f"\n[error]An unexpected error occurred:[/error] {err}")

def main():
    console.print(Panel.fit(
        "[bold cyan]Dropbox Interactive Uploader (Optimized)[/bold cyan]\n"
        "Securely upload files from your local machine to Dropbox.",
        border_style="cyan"
    ))
    
    dbx = authenticate()
    
    while True:
        local_path, dest_path = get_file_paths()
        
        console.print(f"\n[bold]Summary:[/bold]")
        console.print(f"  • [cyan]Local file:[/cyan] {local_path} ({os.path.getsize(local_path) / (1024*1024):.2f} MB)")
        console.print(f"  • [cyan]Destination:[/cyan] {dest_path}")
        
        if Confirm.ask("\nProceed with upload?", default=True):
            upload_file(dbx, local_path, dest_path)
        else:
            console.print("[warning]Upload cancelled.[/warning]")
            
        if not Confirm.ask("\nWould you like to upload another file?", default=False):
            console.print("[info]Goodbye![/info]")
            break

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[warning]Operation cancelled by user.[/warning]")
        sys.exit(0)