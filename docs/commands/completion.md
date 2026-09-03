# `bombercat completion`

> Install shell tab completion for bombercat. (Linux/macOS only.)

## Quick Start

```sh
bombercat completion install            # auto-detect shell
bombercat completion install --shell zsh
```

Then restart your shell (or source your rc file).

---

## Subcommands

### `completion install`

> Install tab completion for your shell.

| Option | Description |
|---|---|
| `--shell [bash\|zsh\|fish]` | Shell to install completion for (auto-detected from `$SHELL` if omitted). |

```sh
bombercat completion install            # auto-detect shell
bombercat completion install --shell zsh
```

```
✓ Completion script written to: /home/user/.local/share/bash-completion/completions/bombercat

Restart your shell or run:
  source /home/user/.local/share/bash-completion/completions/bombercat
```

---

### Notes

- Writes an absolute-path completion script (so completion works whether or not `bombercat` is on `PATH`, including `python bombercat.py <TAB>`).
- For zsh, adds an `fpath` entry to `~/.zshrc` if one isn't there already.
- Besides commands and options, this completes firmware names for [`flash`](flash.md#tab-completion).
- Not supported on Windows.
