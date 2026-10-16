from typing import Optional
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    TimeElapsedColumn
)
from rich.console import Console

console = Console()

def create_progress_bar() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(bar_width=40, complete_style="green", finished_style="bold green"),
        TaskProgressColumn(),
        TextColumn("[yellow]•[/yellow]"),
        TimeElapsedColumn(),
        TextColumn("[yellow]•[/yellow]"),
        TimeRemainingColumn(),
        console=console,
        transient=False
    )
