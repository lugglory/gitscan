# gitscan

<img width="1543" height="927" alt="image" src="https://github.com/user-attachments/assets/ff3b0a75-c875-41ed-8d0c-2787f970dafc" />

A TUI app for quickly reviewing changes in a Git repository, hunk by hunk.

## Installation

```bash
# Install dependencies and register PATH (first time only)
./setup.sh
source ~/.zshrc   # or ~/.bashrc
```

Windows:

```bat
setup.bat
```

## Usage

In any git repository:

```bash
gitscan
```

### Behavior

If a hunk is longer than the screen, it is automatically paginated.

Clicking the top line with the file path opens it in VSCode.

### Key Bindings

| Key | Action |
|---|---|
| `Mouse wheel` | Scroll |
| `Space`/`Backtick` | Next/previous page·hunk |
| `Right`/`Left` | Next/previous hunk |
| `Tab`/`Shift+Tab` | Next/previous file |
| `Ctrl + Mouse wheel` or `Ctrl+Up`/`Ctrl+Down` | View older/newer commit (history navigation) |
| `Ctrl+S` / `Ctrl+U` / `Del` | Hunk stage / unstage / discard |
| `Ctrl+Shift+S` | Stage file |
| `Ctrl+Enter` | Commit |
| `F5` / `Ctrl+R` | Refresh |
| `Esc` | Quit |


## Requirements

- Python 3.10+
- [Textual](https://github.com/Textualize/textual) `>= 0.80.0`
