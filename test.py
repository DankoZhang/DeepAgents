from rich.console import Console
import time

console = Console()

with console.status(
    "processing...",
    refresh_per_second=12
):
    time.sleep(10)