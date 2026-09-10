"""
output.py - Shared Rich console and output helpers for all modules
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

from rich.console import Console, Group
from rich.errors import MarkupError
from rich.padding import Padding
from rich.panel import Panel
from rich.style import Style
from rich.table import Table

STYLES = {
    "header": Style(color="cyan", bold=True),
    "success": Style(color="green", bold=True),
    "warning": Style(color="yellow", bold=True),
    "error": Style(color="red", bold=True),
    "info": Style(color="blue", bold=True),
    "dim": Style(dim=True),
    "prompt": Style(color="magenta", bold=True),
    "device": Style(color="cyan"),
}

console = Console()
console_err = Console(stderr=True)

_REDACT_RE = re.compile(r"^(set\s+pass\b)\s+\S+(.*)$", re.IGNORECASE)


def make_tracer(
    level: int, t0: Optional[float] = None
) -> Optional[Callable[[str, str], None]]:
    """Build a `DeviceLink` trace callback for `-v`/`-vv`, or `None` at level 0.

    Level 1 prints `> `/`< ` lines (tx cyan, rx dim) to stderr. Level 2 adds an
    elapsed-time stamp and byte count ahead of the line. `set pass ...` lines are
    redacted regardless of level, since they carry the device's Wi-Fi/relay
    secret in plain text.
    """
    if level <= 0:
        return None
    start = t0 if t0 is not None else time.monotonic()

    def trace(direction: str, text: str) -> None:
        text = _REDACT_RE.sub(r"\1 (redacted)\2", text)
        arrow = ">" if direction == "tx" else "<"
        style = "cyan" if direction == "tx" else "dim"
        prefix = f"{arrow} "
        if level >= 2:
            elapsed = time.monotonic() - start
            nbytes = len(text.encode("ascii", "replace"))
            prefix = f"{arrow} [{elapsed:8.3f}s] ({nbytes:>3}B) "
        console_err.print(f"[{style}]{prefix}{text}[/{style}]")

    return trace


def safe_print(text: str, *, err: bool = False, **kwargs) -> None:
    """`console.print`, but never crash on malformed Rich markup.

    Several call sites interpolate externally-derived text (device serial
    lines, exception messages, remote-API strings) into markup templates. A
    stray closing tag in that text (e.g. a device log line containing
    something like ``[/x]``) makes Rich's markup parser raise `MarkupError`
    and would otherwise kill a live stream (`monitor`, `capture`) outright.
    Fall back to printing the same text with markup disabled instead.

    `err=True` routes through `console_err` (stderr) instead of `console`
    (stdout), so `print_error`/`print_warning` don't pollute piped/redirected
    stdout (e.g. `bombercat tags read --json | jq` or `2>/dev/null`).
    """
    out = console_err if err else console
    try:
        out.print(text, **kwargs)
    except MarkupError:
        out.print(text, markup=False, **kwargs)


def print_success(message: str) -> None:
    safe_print(f"[green]✓[/green] {message}", style=STYLES["success"])


def print_warning(message: str) -> None:
    safe_print(f"[yellow]⚠[/yellow] {message}", style=STYLES["warning"], err=True)


def print_error(message: str) -> None:
    safe_print(f"[red]✗[/red] {message}", style=STYLES["error"], err=True)


def print_info(message: str) -> None:
    safe_print(f"[blue]ℹ[/blue] {message}", style=STYLES["info"])


def print_dim(message: str) -> None:
    safe_print(f"  {message}", style=STYLES["dim"])


def print_subtitle(message: str) -> None:
    console.print(f"\n  [yellow]{message}[/yellow]")


def print_example(command: str, description: str = "") -> None:
    if description:
        # Align descriptions somewhat manually if needed, or just print
        console.print(f"  [green]{command}[/green] {description}")
    else:
        console.print(f"  [green]{command}[/green]")


def fmt_command(text: str) -> str:
    """Mark up `text` as a copy-pasteable command (for use inside messages)."""
    return f"[green]{text}[/green]"


def numbered_steps(steps: list[str]) -> Padding:
    """Ordered steps laid out so wrapped lines hang under the text, not the number.

    A long step used to wrap back to the panel's left edge, which read as a new
    step. The grid keeps every continuation line — wrapped or explicit — in the
    text column.
    """
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style="white", no_wrap=True)
    grid.add_column(overflow="fold")
    for i, step in enumerate(steps, 1):
        # Callers hand-indent their own line breaks for the old flat layout;
        # the grid owns that alignment now, so collapse it back out.
        grid.add_row(f"{i}.", re.sub(r"\n[ \t]+", "\n", step))
    return Padding(grid, (0, 0, 0, 2))


def print_error_panel(
    title: str,
    problem: str,
    why: str = "",
    fix: list[str] | None = None,
    fix_title: str = "How to fix it",
    notes: list[str] | None = None,
) -> None:
    """A framed error: what failed, why, and the numbered steps that fix it.

    For failures the user has to *act* on — a missing dependency, a permission
    that must be granted — where a one-line `print_error` would leave them to
    guess the next move. Ordinary failures should stay on `print_error`.

    `problem` is the one-line summary, `why` the paragraph explaining it, `fix`
    the ordered steps (mark commands with `fmt_command` so they stand out), and
    `notes` the trailing asides. All accept Rich markup.
    """
    body: list = [f"[red bold]✗  {problem}[/red bold]"]

    if why:
        body += ["", why]

    if fix:
        body += ["", f"[yellow bold]{fix_title}[/yellow bold]", "", numbered_steps(fix)]

    notes = [n for n in (notes or []) if n]
    if notes:
        body += [""] + [f"[dim]{note}[/dim]" for note in notes]

    console.print("")
    console.print(
        Panel(
            Group(*body),
            title=f"[red bold]{title}[/red bold]",
            title_align="left",
            border_style=STYLES["error"],
            padding=(1, 2),
            expand=False,
        )
    )
    console.print("")


def print_success_panel(
    title: str,
    summary: str,
    why: str = "",
    notes: list[str] | None = None,
) -> None:
    """The mirror of `print_error_panel` for a check that came back clean.

    Same frame, same reading order — headline, then the paragraph explaining
    what was proven — so a pass and a fail of the same command look like two
    outcomes of one report instead of two unrelated screens.
    """
    body: list = [f"[green bold]✓  {summary}[/green bold]"]

    if why:
        body += ["", why]

    for note in [n for n in (notes or []) if n]:
        body += ["", f"[dim]{note}[/dim]"]

    console.print("")
    console.print(
        Panel(
            Group(*body),
            title=f"[green bold]{title}[/green bold]",
            title_align="left",
            border_style=STYLES["success"],
            padding=(1, 2),
            expand=False,
        )
    )
    console.print("")
