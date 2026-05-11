#!/usr/bin/env python3
"""gitscan — browse git changes one hunk at a time."""

import difflib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field




from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Click
from textual.widgets import Static, Input
from rich.text import Text
from rich.style import Style as RichStyle

try:
    from pygments import lex as _pyg_lex
    from pygments.lexers import get_lexer_for_filename as _pyg_get_lexer
    from pygments.styles import get_style_by_name as _pyg_get_style
    from pygments.util import ClassNotFound as _PygClassNotFound
    _PYGMENTS_AVAILABLE = True
except ImportError:
    _PYGMENTS_AVAILABLE = False



# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Hunk:
    file_path: str
    file_index: int   # 1-based
    file_total: int
    hunk_index: int   # 1-based, within its file
    hunk_total: int
    header: str       # the @@ line
    lines: list[str] = field(default_factory=list)
    rename_from: str | None = None  # set when file was renamed/moved


# ---------------------------------------------------------------------------
# Git integration
# ---------------------------------------------------------------------------

def _get_repo_root() -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").strip() if r.returncode == 0 else "."

REPO_ROOT = _get_repo_root()


def _run_git_diff(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, cwd=REPO_ROOT)
    except FileNotFoundError:
        print("Error: git not found in PATH.", file=sys.stderr)
        sys.exit(1)
    return r.stdout if r.returncode == 0 else ""


def get_unstaged_diff() -> str:
    return (_run_git_diff(["git", "diff", "--ignore-cr-at-eol"]) or "") + _untracked_diff()


def get_staged_diff() -> str:
    return _run_git_diff(["git", "diff", "--cached", "--ignore-cr-at-eol"])


def get_diff(commit: str | None = None) -> tuple[str | None, str | None]:
    """Return (source_label, diff_text).

    commit=None  → live: unstaged → staged priority
    commit="HEAD~N" or sha → view that commit alone
    Caller typically handles fallback via load_view().
    Line-ending CR(LF/CRLF) differences are always ignored.
    """
    if commit is not None:
        r = subprocess.run(
            ["git", "show", "-m", "--first-parent", "--ignore-cr-at-eol", commit],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, cwd=REPO_ROOT,
        )
        if r.returncode != 0:
            return None, None
        # Include short sha in commit label
        info = subprocess.run(
            ["git", "log", "-1", "--format=%h %s", commit],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, cwd=REPO_ROOT,
        ).stdout.strip() or commit
        return f"commit {info}", r.stdout
    for label, getter in (("unstaged", get_unstaged_diff), ("staged", get_staged_diff)):
        out = getter()
        if out.strip():
            return label, out
    return None, None


# View identifier: "unstaged" | "staged" | int (commit depth, 0=HEAD)
View = "str | int"


def load_view(view) -> tuple[str | None, str | None, object]:
    """Fetch the diff for the given view. If unstaged/staged is empty,
    automatically promote to the HEAD commit view."""
    if view == "unstaged":
        out = get_unstaged_diff()
        if out.strip():
            return "unstaged", out, "unstaged"
        view = "staged"
    if view == "staged":
        out = get_staged_diff()
        if out.strip():
            return "staged", out, "staged"
        view = 0
    depth = int(view)
    source, diff_text = get_diff(f"HEAD~{depth}")
    return source, diff_text, depth


def _untracked_diff() -> str:
    """Return untracked files as 'new file' diffs without touching the worktree."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=False, cwd=REPO_ROOT,
        )
    except FileNotFoundError:
        return ""
    if r.returncode != 0 or not r.stdout:
        return ""
    paths = [p.decode("utf-8", "replace") for p in r.stdout.split(b"\x00") if p]
    chunks: list[str] = []
    for p in paths:
        d = subprocess.run(
            ["git", "diff", "--no-index", "--", os.devnull, p],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, cwd=REPO_ROOT,
        )
        # --no-index exits with 1 when there are differences
        if d.stdout:
            chunks.append(d.stdout)
    return "".join(chunks)


def _extract_hunk_patch(hunk: Hunk) -> tuple[bytes | None, str]:
    r = subprocess.run(["git", "diff", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
    if r.returncode != 0 or not r.stdout.strip():
        return None, "no unstaged changes found for this file"
    content = r.stdout
    hunk_starts = [m.start() for m in re.finditer(rb"^@@", content, re.MULTILINE)]
    if not hunk_starts:
        return None, "no hunks found in diff"
    idx = hunk.hunk_index - 1
    if idx >= len(hunk_starts):
        return None, f"hunk {hunk.hunk_index} not found (diff has {len(hunk_starts)})"
    hunk_end = hunk_starts[idx + 1] if idx + 1 < len(hunk_starts) else len(content)
    return content[: hunk_starts[0]] + content[hunk_starts[idx]:hunk_end], ""


def git_stage_hunk(hunk: Hunk) -> tuple[bool, str]:
    if not hunk.header.startswith("@@"):
        r = subprocess.run(["git", "add", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
        return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()
    patch, err = _extract_hunk_patch(hunk)
    if patch is None:
        return False, err
    r = subprocess.run(["git", "apply", "--cached", "-"], input=patch, capture_output=True, cwd=REPO_ROOT)
    return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()


def git_discard_hunk(hunk: Hunk) -> tuple[bool, str]:
    if not hunk.header.startswith("@@"):
        r = subprocess.run(["git", "checkout", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
        return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()
    patch, err = _extract_hunk_patch(hunk)
    if patch is None:
        return False, err
    r = subprocess.run(["git", "apply", "--reverse", "-"], input=patch, capture_output=True, cwd=REPO_ROOT)
    return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()


def git_unstage_hunk(hunk: Hunk) -> tuple[bool, str]:
    # Virtual hunks (rename / binary / mode change) have no @@ header —
    # git apply can't reverse them, so fall back to resetting the whole file.
    if not hunk.header.startswith("@@"):
        r = subprocess.run(["git", "reset", "HEAD", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
        return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()
    r = subprocess.run(["git", "diff", "--cached", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
    if r.returncode != 0 or not r.stdout.strip():
        return False, "no staged changes found for this file"
    content = r.stdout
    hunk_starts = [m.start() for m in re.finditer(rb"^@@", content, re.MULTILINE)]
    if not hunk_starts:
        # No @@ hunks in cached diff (e.g. binary) — reset the whole file
        r = subprocess.run(["git", "reset", "HEAD", "--", hunk.file_path], capture_output=True, cwd=REPO_ROOT)
        return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()
    idx = hunk.hunk_index - 1
    if idx >= len(hunk_starts):
        return False, f"hunk {hunk.hunk_index} not found (staged diff has {len(hunk_starts)})"
    hunk_end = hunk_starts[idx + 1] if idx + 1 < len(hunk_starts) else len(content)
    patch = content[: hunk_starts[0]] + content[hunk_starts[idx]:hunk_end]
    r = subprocess.run(["git", "apply", "--cached", "--reverse", "-"], input=patch, capture_output=True, cwd=REPO_ROOT)
    return r.returncode == 0, (r.stderr + r.stdout).decode("utf-8", errors="replace").strip()


def git_stage_file(file_path: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["git", "add", "--", file_path],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO_ROOT,
    )
    return r.returncode == 0, r.stderr.strip()


def git_commit(message: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO_ROOT,
    )
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _decode_git_path(raw: str) -> str:
    """Decode git's C-string escaping (octal bytes → UTF-8) from a quoted path."""
    buf = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == '\\' and i + 1 < len(raw):
            c = raw[i + 1]
            if c == 'n':   buf.extend(b'\n'); i += 2
            elif c == 't': buf.extend(b'\t'); i += 2
            elif c == '\\':buf.extend(b'\\'); i += 2
            elif c == '"': buf.extend(b'"');  i += 2
            elif c.isdigit() and i + 4 <= len(raw) and all(d in '01234567' for d in raw[i + 1:i + 4]):
                buf.append(int(raw[i + 1:i + 4], 8)); i += 4
            else:
                buf.extend(raw[i].encode()); i += 1
        else:
            buf.extend(raw[i].encode()); i += 1
    return buf.decode('utf-8', errors='replace')


def _parse_diff_git_path(line: str) -> str | None:
    """Extract the b-path from a 'diff --git' header (handles quoted paths)."""
    # Quoted: diff --git "a/..." "b/..."
    m = re.match(r'^diff --git "a/[^"]*" "b/([^"]*)"', line)
    if m:
        return _decode_git_path(m.group(1))
    # Unquoted: diff --git a/foo b/foo
    parts = line.split(" b/", 1)
    return parts[1].strip() if len(parts) > 1 else None


def parse_diff(text: str) -> list[Hunk]:
    """Parse unified diff text into a flat list of Hunks."""

    # file_path → [(header, [lines]), ...]
    files: dict[str, list[tuple[str, list[str]]]] = {}
    file_rename_from: dict[str, str] = {}   # file_path → original path (for top-bar display)
    cur_file: str | None = None
    cur_header: str | None = None
    cur_lines: list[str] = []
    # per-file metadata — reset only on new diff --git, NOT on each @@
    cur_rename_from: str | None = None
    cur_new_file: bool = False
    cur_deleted_file: bool = False
    cur_binary: bool = False
    cur_old_mode: str | None = None
    cur_new_mode: str | None = None

    # Metadata line prefixes that appear between diff --git and @@ — skip these
    _SKIP_PREFIXES = ("--- ", "+++ ", "index ", "similarity ")

    def flush_hunk() -> None:
        """Save the current @@ hunk to files dict."""
        nonlocal cur_header, cur_lines
        if cur_header is not None and cur_file is not None:
            lines = cur_lines[:]
            while lines and not lines[-1]:   # strip trailing blank lines
                lines = lines[:-1]
            if lines:
                files.setdefault(cur_file, []).append((cur_header, lines))
        cur_header = None
        cur_lines = []

    def end_file() -> None:
        """Called when a file block ends. Saves metadata and synthesises
        a display hunk when there are no @@ content hunks."""
        nonlocal cur_rename_from, cur_new_file, cur_deleted_file
        nonlocal cur_binary, cur_old_mode, cur_new_mode
        flush_hunk()
        if cur_file is not None:
            # Record rename_from so every content hunk for this file can show it
            if cur_rename_from is not None:
                file_rename_from[cur_file] = cur_rename_from
            # Synthesise a display hunk only when there are no @@ content hunks
            if cur_file not in files:
                info_lines: list[str] = []
                synth_header = ""
                if cur_rename_from is not None:
                    info_lines = [f" {cur_rename_from}", f" → {cur_file}"]
                    synth_header = "(renamed)"
                elif cur_new_file and cur_binary:
                    info_lines = [" (new binary file)"]
                    synth_header = "(new binary file)"
                elif cur_deleted_file and cur_binary:
                    info_lines = [" (deleted binary file)"]
                    synth_header = "(deleted binary file)"
                elif cur_binary:
                    info_lines = [" (binary file changed)"]
                    synth_header = "(binary)"
                elif cur_new_file:
                    info_lines = [" (new empty file)"]
                    synth_header = "(new file)"
                elif cur_deleted_file:
                    info_lines = [" (deleted empty file)"]
                    synth_header = "(deleted file)"
                elif cur_old_mode and cur_new_mode:
                    info_lines = [f" mode: {cur_old_mode} → {cur_new_mode}"]
                    synth_header = "(mode change)"
                if info_lines:
                    files[cur_file] = [(synth_header, info_lines)]
        # Always reset — even if cur_file was None (e.g. commit message noise)
        cur_rename_from = None
        cur_new_file = False
        cur_deleted_file = False
        cur_binary = False
        cur_old_mode = None
        cur_new_mode = None

    for line in text.split("\n"):
        if line.startswith("diff --git "):
            end_file()
            cur_file = _parse_diff_git_path(line)
        elif line.startswith("new file mode "):
            cur_new_file = True
        elif line.startswith("deleted file mode "):
            cur_deleted_file = True
        elif line.startswith("old mode "):
            cur_old_mode = line[len("old mode "):].strip()
        elif line.startswith("new mode "):
            cur_new_mode = line[len("new mode "):].strip()
        elif line.startswith("rename from "):
            raw = line[len("rename from "):].strip()
            if raw.startswith('"') and raw.endswith('"'):
                cur_rename_from = _decode_git_path(raw[1:-1])
            else:
                cur_rename_from = raw
        elif line.startswith("rename to "):
            pass  # destination already captured from diff --git header
        elif line.startswith("Binary "):
            cur_binary = True
        elif line.startswith("@@ "):
            flush_hunk()
            cur_header = line
        elif cur_header is not None and not line.startswith(_SKIP_PREFIXES):
            cur_lines.append(line)

    end_file()

    # Build flat Hunk list preserving file order
    hunks: list[Hunk] = []
    file_paths = list(files.keys())
    for fi, fp in enumerate(file_paths):
        file_hunks = files[fp]
        rename_from = file_rename_from.get(fp)
        for hi, (header, lines) in enumerate(file_hunks):
            hunks.append(Hunk(
                file_path=fp,
                file_index=fi + 1,
                file_total=len(file_paths),
                hunk_index=hi + 1,
                hunk_total=len(file_hunks),
                header=header,
                lines=lines,
                rename_from=rename_from,
            ))
    return hunks


# ---------------------------------------------------------------------------
# Syntax highlighting (full-file lex + per-line map)
# ---------------------------------------------------------------------------

_MAX_HIGHLIGHT_LINES = 20000
_PYG_STYLE = _pyg_get_style("monokai") if _PYGMENTS_AVAILABLE else None
_STYLE_CACHE: dict = {}
_HL_CACHE: dict = {}


def _style_for_token(tok):
    if not _PYGMENTS_AVAILABLE:
        return None
    cached = _STYLE_CACHE.get(tok, False)
    if cached is not False:
        return cached
    s = _PYG_STYLE.style_for_token(tok)
    color = f"#{s['color']}" if s.get('color') else None
    bold = bool(s.get('bold'))
    italic = bool(s.get('italic'))
    style = RichStyle(color=color, bold=bold, italic=italic) if (color or bold or italic) else None
    _STYLE_CACHE[tok] = style
    return style


def _git_show(rev_path: str) -> str | None:
    r = subprocess.run(
        ["git", "show", rev_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, cwd=REPO_ROOT,
    )
    return r.stdout if r.returncode == 0 else None


def _read_worktree(file_path: str) -> str | None:
    try:
        with open(os.path.join(REPO_ROOT, file_path), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _get_source_content(view, file_path: str, side: str, pre_path: str | None) -> str | None:
    """Fetch pre- or post-image for a file under a given view. side='pre'|'post'."""
    path = pre_path if (side == "pre" and pre_path) else file_path
    if view == "unstaged":
        return _git_show(f":{path}") if side == "pre" else _read_worktree(path)
    if view == "staged":
        return _git_show(f"HEAD:{path}") if side == "pre" else _git_show(f":{path}")
    if isinstance(view, int):
        rev = f"HEAD~{view}^" if side == "pre" else f"HEAD~{view}"
        return _git_show(f"{rev}:{path}")
    return None


def _highlight_content(content: str, file_path: str):
    """Tokenize full file and return list of per-line [(text, Style|None), ...]."""
    if not _PYGMENTS_AVAILABLE or content is None:
        return None
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if content.count("\n") > _MAX_HIGHLIGHT_LINES:
        return None
    try:
        lexer = _pyg_get_lexer(file_path, stripnl=False, stripall=False, ensurenl=False)
    except _PygClassNotFound:
        return None
    lines: list[list[tuple[str, object]]] = [[]]
    for tok_type, text in _pyg_lex(content, lexer):
        if not text:
            continue
        style = _style_for_token(tok_type)
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if part:
                lines[-1].append((part, style))
            if i < len(parts) - 1:
                lines.append([])
    return lines


def _get_highlighted(view, file_path: str, side: str, pre_path: str | None = None):
    key = (str(view), file_path, side, pre_path)
    if key in _HL_CACHE:
        return _HL_CACHE[key]
    content = _get_source_content(view, file_path, side, pre_path)
    result = _highlight_content(content, pre_path or file_path) if content is not None else None
    _HL_CACHE[key] = result
    return result


def _clear_highlight_cache() -> None:
    _HL_CACHE.clear()


def _compute_line_numbers(hunk: "Hunk") -> list[tuple[int | None, int | None]]:
    """For each line in hunk.lines, return (pre_line_no, post_line_no) — 1-based, or None."""
    m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk.header)
    if not m:
        return [(None, None)] * len(hunk.lines)
    pre_no = int(m.group(1))
    post_no = int(m.group(2))
    result: list[tuple[int | None, int | None]] = []
    for line in hunk.lines:
        if line.startswith("+"):
            result.append((None, post_no if post_no >= 1 else None))
            post_no += 1
        elif line.startswith("-"):
            result.append((pre_no if pre_no >= 1 else None, None))
            pre_no += 1
        elif line.startswith("\\"):
            result.append((None, None))
        else:
            result.append((
                pre_no if pre_no >= 1 else None,
                post_no if post_no >= 1 else None,
            ))
            pre_no += 1
            post_no += 1
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _word_tokens(text: str) -> list[str]:
    """Split text into word/non-word tokens for fine-grained diffing."""
    return re.findall(r'\w+|\W+', text) or ['']


_REM_STYLE = "bold on rgb(136,46,50)"
_ADD_STYLE = "bold on rgb(42,116,48)"
_REM_PLAIN = "on rgb(85,29,33)"
_ADD_PLAIN = "on rgb(25,73,32)"


def _apply_fg(out: Text, content_start: int, content_len: int, fg_tokens) -> None:
    """Overlay per-segment fg styles onto the content range [start, start+len) of `out`."""
    if not fg_tokens:
        return
    pos = content_start
    end_limit = content_start + content_len
    for text, style in fg_tokens:
        if pos >= end_limit:
            break
        seg_end = min(pos + len(text), end_limit)
        if style is not None and seg_end > pos:
            out.stylize(style, pos, seg_end)
        pos = seg_end


def _append_paired_lines(out: Text, rem_line: str, add_line: str,
                         opcodes: list, rem_tokens: list[str], add_tokens: list[str],
                         width: int, rem_fg=None, add_fg=None) -> None:
    """Render a paired -/+ line with pre-computed intra-line word diff."""
    rem_content = rem_line[1:]
    add_content = add_line[1:]

    # removed line
    rem_start = len(out) + 1  # position right after the "-" prefix
    out.append("-", style=_REM_PLAIN)
    for op, i1, i2, _j1, _j2 in opcodes:
        chunk = ''.join(rem_tokens[i1:i2])
        if chunk:
            out.append(chunk, style=_REM_PLAIN if op == 'equal' else _REM_STYLE)
    pad = width - 1 - len(rem_content)
    if pad > 0:
        out.append(" " * pad, style=_REM_PLAIN)
    out.append("\n")
    _apply_fg(out, rem_start, len(rem_content), rem_fg)

    # added line
    add_start = len(out) + 1
    out.append("+", style=_ADD_PLAIN)
    for op, _i1, _i2, j1, j2 in opcodes:
        chunk = ''.join(add_tokens[j1:j2])
        if chunk:
            out.append(chunk, style=_ADD_PLAIN if op == 'equal' else _ADD_STYLE)
    pad = width - 1 - len(add_content)
    if pad > 0:
        out.append(" " * pad, style=_ADD_PLAIN)
    out.append("\n")
    _apply_fg(out, add_start, len(add_content), add_fg)


def render_diff(lines: list[str], width: int = 0, fg_tokens: list | None = None) -> Text:
    """Color-code diff lines as a Rich Text object with intra-line word diff.

    fg_tokens (optional): parallel list to `lines`, each entry either None or a
    list of (text_segment, RichStyle|None) whose texts concatenate to the line
    content (without the +/-/space prefix). Used to overlay syntax highlighting
    foreground colors on top of the diff backgrounds.
    """
    out = Text(no_wrap=True, overflow="fold")

    def fg(idx: int):
        return fg_tokens[idx] if (fg_tokens is not None and 0 <= idx < len(fg_tokens)) else None

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("-"):
            removed: list[str] = []
            rem_base = i
            while i < len(lines) and lines[i].startswith("-"):
                removed.append(lines[i])
                i += 1
            added: list[str] = []
            add_base = i
            while i < len(lines) and lines[i].startswith("+"):
                added.append(lines[i])
                i += 1
            pairs = min(len(removed), len(added))
            for j in range(pairs):
                rem_tokens = _word_tokens(removed[j][1:])
                add_tokens = _word_tokens(added[j][1:])
                opcodes = difflib.SequenceMatcher(None, rem_tokens, add_tokens, autojunk=False).get_opcodes()
                if any(op == 'equal' and ''.join(rem_tokens[i1:i2]).strip()
                       for op, i1, i2, _j1, _j2 in opcodes):
                    _append_paired_lines(out, removed[j], added[j], opcodes, rem_tokens, add_tokens, width,
                                         rem_fg=fg(rem_base + j), add_fg=fg(add_base + j))
                else:
                    start = len(out) + 1
                    out.append(removed[j].ljust(width) + "\n", style=_REM_PLAIN)
                    _apply_fg(out, start, len(removed[j]) - 1, fg(rem_base + j))
                    start = len(out) + 1
                    out.append(added[j].ljust(width) + "\n", style=_ADD_PLAIN)
                    _apply_fg(out, start, len(added[j]) - 1, fg(add_base + j))
            for j in range(pairs, len(removed)):
                start = len(out) + 1
                out.append(removed[j].ljust(width) + "\n", style=_REM_PLAIN)
                _apply_fg(out, start, len(removed[j]) - 1, fg(rem_base + j))
            for j in range(pairs, len(added)):
                start = len(out) + 1
                out.append(added[j].ljust(width) + "\n", style=_ADD_PLAIN)
                _apply_fg(out, start, len(added[j]) - 1, fg(add_base + j))
        elif line.startswith("+"):
            start = len(out) + 1
            out.append(line.ljust(width) + "\n", style=_ADD_PLAIN)
            _apply_fg(out, start, len(line) - 1, fg(i))
            i += 1
        elif line.startswith("\\"):
            out.append(line + "\n", style="dim italic")
            i += 1
        else:
            if line.startswith(" "):
                start = len(out) + 1
                out.append(line + "\n")
                _apply_fg(out, start, len(line) - 1, fg(i))
            else:
                out.append(line + "\n")
            i += 1
    return out


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class GitscanApp(App[None]):

    CSS = """
    Screen { background: $surface; }

    #top {
        color: white;
        height: 1;
        padding: 0 1;
    }
    #top.mode-unstaged { background: #1a3a5c; }
    #top.mode-staged   { background: #1a4a1a; }
    #top.mode-commit   { background: #3a2a0a; }

    #hunk-hdr {
        background: #1e1e2e;
        color: cyan;
        height: 1;
        padding: 0 1;
    }
    #content {
        height: 1fr;
        padding: 0 1;
        overflow-y: hidden;
    }
    #bottom {
        color: white;
        height: 1;
        padding: 0 1;
    }
    #bottom.mode-unstaged { background: #0d2b45; }
    #bottom.mode-staged   { background: #0d3318; }
    #bottom.mode-commit   { background: #2a1e08; }
    #commit-input {
        display: none;
        height: 1;
        border: none;
        padding: 0 1;
        background: #1e1e2e;
        color: white;
    }
    """

    BINDINGS = [
        Binding("pagedown",             "next_page",    "Next page → hunk", show=False, priority=True),
        Binding("pageup, w",            "prev_page",    "Prev page → hunk", show=False, priority=True),
        Binding("down",                 "scroll_down",  "Scroll down",      show=False, priority=True),
        Binding("up",                   "scroll_up",    "Scroll up",        show=False, priority=True),
        Binding("space",                "scroll_down_line", "Scroll down 1 line", show=False, priority=True),
        Binding("`",                    "scroll_up_line",   "Scroll up 1 line",   show=False, priority=True),
        Binding("right, d",             "next_hunk",    "Next hunk",        show=False),
        Binding("left, a",              "prev_hunk",    "Prev hunk",        show=False),
        Binding("tab",                  "next_file",    "Next file",        show=False, priority=True),
        Binding("shift+tab",            "prev_file",    "Prev file",        show=False, priority=True),
        Binding("ctrl+s",           "stage_hunk",   "Stage hunk",       show=False),
        Binding("ctrl+shift+s",     "stage_file",   "Stage file",       show=False),
        Binding("ctrl+u",           "unstage_hunk", "Unstage hunk",     show=False),
        Binding("delete",               "discard_hunk", "Discard hunk",     show=False),
        Binding("ctrl+j, ctrl+enter",           "commit",       "Commit",           show=False, priority=True),
        Binding("escape",               "quit",         "Quit",             show=False),
        Binding("f5, ctrl+r",                   "refresh",      "Refresh",          show=False),
        Binding("ctrl+up",                      "older_commit", "Older commit",     show=False, priority=True),
        Binding("ctrl+down",                    "newer_commit", "Newer commit",     show=False, priority=True),
    ]

    _EMPTY_HUNK = Hunk(
        file_path="(no changes)", file_index=1, file_total=1,
        hunk_index=1, hunk_total=1, header="",
        lines=["  no changes — focus window or press ctrl+r to refresh"],
    )

    def __init__(self, hunks: list[Hunk], source: str | None,
                 initial_view=None) -> None:
        super().__init__()
        self.hunks = hunks or [self._EMPTY_HUNK]
        self.source = source
        self.hunk_idx = 0
        self.scroll_offset = 0  # line index within current hunk
        self._status: str | None = None
        self._status_timer: object | None = None
        # "unstaged" | "staged" | int (commit depth, 0=HEAD)
        self.view = initial_view if initial_view is not None else "unstaged"

    def _set_status(self, msg: str) -> None:
        self._status = msg
        if self._status_timer:
            self._status_timer.stop()
        def _expire() -> None:
            self._status_timer = None
            self._draw()
        self._status_timer = self.set_timer(2, _expire)

    # ---- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="top")
        yield Static("", id="hunk-hdr")
        yield Static("", id="content")
        yield Static("", id="bottom")
        yield Input(placeholder="Commit message  (Enter to confirm, Esc to cancel)", id="commit-input", disabled=True)

    def on_mount(self) -> None:
        self._draw()

    def on_resize(self) -> None:
        self._draw()

    @on(Click, "#top")
    def _open_in_vscode(self, event: Click) -> None:
        if self.source not in ("unstaged", "staged"): return
        hunk = self.hunks[self.hunk_idx]
        m = re.search(r'\+(\d+)', hunk.header)
        line = m.group(1) if m else "1"
        abs_path = os.path.abspath(hunk.file_path)
        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True,
        ).stdout.strip()
        code_exe = shutil.which("code") or "code"
        subprocess.Popen([code_exe, repo_root, "--goto", f"{abs_path}:{line}"])

    def on_app_focus(self) -> None:
        if self.query_one("#commit-input", Input).display:
            return
        hunk = self.hunks[self.hunk_idx]
        self._refresh(target_hunk_idx=self.hunk_idx,
                      target_file=hunk.file_path,
                      target_header=hunk.header,
                      target_offset=self.scroll_offset)

    # ---- helpers -----------------------------------------------------------

    def _page_size(self) -> int:
        # top + hunk-hdr + bottom = 3 rows
        return max(1, self.size.height - 3)

    def _total_pages(self, hunk: Hunk) -> int:
        ps = self._page_size()
        return max(1, (len(hunk.lines) + ps - 1) // ps)

    def _max_page_offset(self, hunk: Hunk) -> int:
        return (self._total_pages(hunk) - 1) * self._page_size()

    def _max_line_offset(self, hunk: Hunk) -> int:
        return max(0, len(hunk.lines) - self._page_size())

    def _clamp_offset(self, hunk: Hunk) -> None:
        self.scroll_offset = max(0, min(self.scroll_offset, self._max_page_offset(hunk)))

    # ---- drawing -----------------------------------------------------------

    def _draw(self) -> None:
        hunk = self.hunks[self.hunk_idx]
        ps = self._page_size()
        total_pages = self._total_pages(hunk)
        self._clamp_offset(hunk)

        page_lines = hunk.lines[self.scroll_offset : self.scroll_offset + ps]

        # ── mode-based styling (top + bottom bar background) ────────────────
        mode_css = {
            "unstaged":    "mode-unstaged",
            "staged":      "mode-staged",
            "last commit": "mode-commit",
        }
        mode_class = mode_css.get(self.source or "", "mode-commit")
        top_w = self.query_one("#top", Static)
        bot_w = self.query_one("#bottom", Static)
        for cls in mode_css.values():
            top_w.remove_class(cls)
            bot_w.remove_class(cls)
        top_w.add_class(mode_class)
        bot_w.add_class(mode_class)

        # ── top bar ─────────────────────────────────────────────────────────
        source_badges = {
            "unstaged":    (" UNSTAGED ", "bold white on #1a6090"),
            "staged":      (" STAGED ",   "bold white on #1a6a1a"),
            "last commit": (" COMMIT ",   "bold white on #6a4a0a"),
        }
        commit_subject: str | None = None
        if self.source and self.source.startswith("commit "):
            rest = self.source[len("commit "):]
            sha, _, subj = rest.partition(" ")
            badge_text = f" COMMIT {sha} "
            badge_style = "bold white on #6a4a0a"
            commit_subject = subj or None
        else:
            badge_text, badge_style = source_badges.get(self.source or "", (" ? ", "bold white"))

        if self._status:
            t = Text(no_wrap=True)
            t.append(badge_text, style=badge_style)
            t.append("  ")
            t.append(self._status, style="bold yellow")
            top_w.update(t)
            self._status = None
        else:
            t = Text(no_wrap=True)
            t.append(badge_text, style=badge_style)
            t.append("  ")
            t.append(hunk.file_path, style="bold white underline")
            if hunk.rename_from:
                t.append(f"  ← {hunk.rename_from}", style="yellow")
            if hunk.file_total > 1:
                t.append(f"  file [{hunk.file_index}/{hunk.file_total}]", style="white")
            if hunk.hunk_total > 1:
                t.append(f"  hunk [{hunk.hunk_index}/{hunk.hunk_total}]", style="white")
            if commit_subject:
                t.append(f"  · {commit_subject}", style="italic #d0b070")
            top_w.update(t)

        # ── hunk header ─────────────────────────────────────────────────────
        current_page = self._current_page(hunk)
        page_info = f"  · page [{current_page}/{total_pages}]" if total_pages > 1 else ""
        h = Text(no_wrap=True)
        h.append(hunk.header + page_info, style="cyan")
        self.query_one("#hunk-hdr", Static).update(h)

        # ── diff content ────────────────────────────────────────────────────
        content_width = self.size.width - 2  # padding: 0 1
        fg_tokens = self._build_fg_tokens(hunk, page_lines)
        diff_text = render_diff(page_lines, content_width, fg_tokens=fg_tokens)
        # pad with blank lines so the last page fills the screen
        blank_lines = ps - len(page_lines)
        if blank_lines > 0:
            diff_text.append("\n" * blank_lines)
        self.query_one("#content", Static).update(diff_text)

        # ── bottom bar ──────────────────────────────────────────────────────
        b = Text(no_wrap=True)
        b.append(f"[{self.hunk_idx + 1}/{len(self.hunks)}]  ", style="bold")
        hints = [
            ("space/backtick", "line"),
            ("↑/↓", "scroll"),
            ("pgdn/pgup", "page"),
            ("left/right", "hunk"),
            ("tab/shift+tab", "file"),
        ]
        if self.source == "unstaged":
            hints += [("ctrl+s", "stage hunk"), ("ctrl+shift+s", "stage file"), ("del", "discard hunk")]
        if self.source == "staged":
            hints += [("ctrl+u", "unstage hunk")]
        if self.source in ("unstaged", "staged"):
            hints += [("ctrl+enter", "commit")]
        hints += [("ctrl+↑/↓", "older/newer commit"), ("ctrl+r/f5", "refresh"), ("esc", "quit")]
        for key, label in hints:
            b.append(key, style="bold yellow")
            b.append(f":{label}  ", style="dim")
        bot_w.update(b)

    def _build_fg_tokens(self, hunk: Hunk, page_lines: list[str]) -> list | None:
        """Build per-line syntax-highlight fg tokens for the current page."""
        if not _PYGMENTS_AVAILABLE or not hunk.header.startswith("@@"):
            return None
        line_nos = _compute_line_numbers(hunk)
        pre_hl = _get_highlighted(self.view, hunk.file_path, "pre", hunk.rename_from)
        post_hl = _get_highlighted(self.view, hunk.file_path, "post")
        if pre_hl is None and post_hl is None:
            return None
        tokens: list = []
        base = self.scroll_offset
        for k in range(len(page_lines)):
            idx = base + k
            if idx >= len(hunk.lines) or idx >= len(line_nos):
                tokens.append(None)
                continue
            line = hunk.lines[idx]
            pre_no, post_no = line_nos[idx]
            tok = None
            if line.startswith("-") and pre_hl and pre_no and pre_no - 1 < len(pre_hl):
                tok = pre_hl[pre_no - 1]
            elif line.startswith("+") and post_hl and post_no and post_no - 1 < len(post_hl):
                tok = post_hl[post_no - 1]
            elif line.startswith(" ") and post_hl and post_no and post_no - 1 < len(post_hl):
                tok = post_hl[post_no - 1]
            tokens.append(tok)
        return tokens

    def _current_page(self, hunk: Hunk) -> int:
        """Return the page number with the highest visible-line ratio (display only)."""
        ps = self._page_size()
        total = len(hunk.lines)
        off = self.scroll_offset
        first = off // ps + 1
        best_page, best_ratio = first, -1.0
        for p in range(first, min(first + 2, self._total_pages(hunk) + 1)):
            page_start = (p - 1) * ps
            page_end = min(p * ps, total)
            page_lines = page_end - page_start
            if page_lines == 0:
                continue
            overlap = max(0, min(off + ps, page_end) - max(off, page_start))
            ratio = overlap / page_lines
            if ratio > best_ratio:
                best_ratio, best_page = ratio, p
        return best_page

    # ---- actions -----------------------------------------------------------

    def action_next_page(self) -> None:
        hunk = self.hunks[self.hunk_idx]
        ps = self._page_size()
        max_offset = self._max_page_offset(hunk)
        if self.scroll_offset < max_offset:
            self.scroll_offset = min(self.scroll_offset + ps, max_offset)
        elif self.hunk_idx < len(self.hunks) - 1:
            self.hunk_idx += 1
            self.scroll_offset = 0
        # already at the very last page of the last hunk — do nothing
        self._draw()

    def action_prev_page(self) -> None:
        ps = self._page_size()
        if self.scroll_offset > 0:
            self.scroll_offset = max(0, self.scroll_offset - ps)
        elif self.hunk_idx > 0:
            self.hunk_idx -= 1
            self.scroll_offset = self._max_page_offset(self.hunks[self.hunk_idx])
        # already at the very first page of the first hunk — do nothing
        self._draw()

    def action_scroll_down(self) -> None:
        self._scroll_by_lines(max(1, self._page_size() // 3))

    def action_scroll_up(self) -> None:
        self._scroll_by_lines(-max(1, self._page_size() // 3))

    def action_scroll_down_line(self) -> None:
        self._scroll_by_lines(1)

    def action_scroll_up_line(self) -> None:
        self._scroll_by_lines(-1)

    def _scroll_by_lines(self, n: int) -> None:
        hunk = self.hunks[self.hunk_idx]
        max_offset = self._max_line_offset(hunk)
        # page navigation may have placed us beyond line max — clamp and stop
        if self.scroll_offset > max_offset:
            self.scroll_offset = max_offset
            self._draw()
            return
        new_offset = self.scroll_offset + n
        if 0 <= new_offset <= max_offset:
            self.scroll_offset = new_offset
        elif new_offset > max_offset:
            if self.scroll_offset < max_offset:
                self.scroll_offset = max_offset
            elif self.hunk_idx < len(self.hunks) - 1:
                self.hunk_idx += 1
                self.scroll_offset = 0
        else:  # new_offset < 0
            if self.scroll_offset > 0:
                self.scroll_offset = 0
            elif self.hunk_idx > 0:
                self.hunk_idx -= 1
                self.scroll_offset = self._max_line_offset(self.hunks[self.hunk_idx])
        self._draw()

    def action_next_hunk(self) -> None:
        if self.hunk_idx < len(self.hunks) - 1:
            self.hunk_idx += 1
            self.scroll_offset = 0
        self._draw()

    def action_prev_hunk(self) -> None:
        if self.hunk_idx > 0:
            self.hunk_idx -= 1
            self.scroll_offset = 0
        self._draw()

    def action_next_file(self) -> None:
        cur = self.hunks[self.hunk_idx].file_path
        for i in range(self.hunk_idx + 1, len(self.hunks)):
            if self.hunks[i].file_path != cur:
                self.hunk_idx = i
                self.scroll_offset = 0
                break
        self._draw()

    def _step_view(self, older: bool) -> None:
        # Order: unstaged → staged → HEAD(0) → HEAD~1 → ...
        order_older = {"unstaged": "staged", "staged": 0}
        order_newer = {"staged": "unstaged", 0: "staged"}
        cur = self.view
        if older:
            if isinstance(cur, int):
                new = cur + 1
            else:
                new = order_older[cur]
        else:
            if isinstance(cur, int):
                new = cur - 1 if cur > 0 else order_newer[0]
            else:
                if cur == "unstaged":
                    self._set_status("No newer changes available")
                    self._draw()
                    return
                new = order_newer[cur]
        # Skip empty views (including commit existence check)
        while True:
            if isinstance(new, int):
                r = subprocess.run(
                    ["git", "rev-parse", "--verify", f"HEAD~{new}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False, cwd=REPO_ROOT,
                )
                if r.returncode != 0:
                    self._set_status("No older commits available" if older else "No newer changes available")
                    self._draw()
                    return
                break
            else:
                out = get_unstaged_diff() if new == "unstaged" else get_staged_diff()
                if out.strip():
                    break
                # If empty, step one more in the same direction
                if older:
                    new = order_older[new]
                else:
                    if new == "unstaged":
                        self._set_status("No newer changes available")
                        self._draw()
                        return
                    new = order_newer[new]
        self.view = new
        self.hunk_idx = 0
        self.scroll_offset = 0
        self._refresh()

    def action_older_commit(self) -> None:
        self._step_view(older=True)

    def action_newer_commit(self) -> None:
        self._step_view(older=False)

    def action_refresh(self) -> None:
        hunk = self.hunks[self.hunk_idx]
        self._refresh(target_file=hunk.file_path,
                      target_header=hunk.header,
                      target_offset=self.scroll_offset)

    def _refresh(self, *,
                 target_hunk_idx: int | None = None,
                 target_file: str | None = None,
                 target_header: str | None = None,
                 target_offset: int = 0) -> None:
        _clear_highlight_cache()
        source, diff_text, self.view = load_view(self.view)
        hunks = parse_diff(diff_text) if diff_text else []
        self.hunks = hunks or [self._EMPTY_HUNK]
        self.source = source

        # 1) exact hunk match (same file + same @@ header)
        if target_file and target_header:
            for i, h in enumerate(hunks):
                if h.file_path == target_file and h.header == target_header:
                    self.hunk_idx = i
                    self.scroll_offset = target_offset
                    self._draw()
                    return

        # 2) same file, first hunk
        if target_file:
            for i, h in enumerate(hunks):
                if h.file_path == target_file:
                    self.hunk_idx = i
                    self.scroll_offset = 0
                    self._draw()
                    return

        # 3) clamp to nearest index (used after staging)
        if target_hunk_idx is not None:
            self.hunk_idx = max(0, min(target_hunk_idx, len(self.hunks) - 1))
        else:
            self.hunk_idx = 0
        self.scroll_offset = 0
        self._draw()

    def action_stage_hunk(self) -> None:
        if self.source != "unstaged":
            return
        hunk = self.hunks[self.hunk_idx]
        saved_idx = self.hunk_idx
        ok, err = git_stage_hunk(hunk)
        if ok:
            self._set_status(f"staged hunk {hunk.hunk_index}/{hunk.hunk_total} of {hunk.file_path}")
            self._refresh(target_hunk_idx=saved_idx)
        elif "no unstaged changes" in err:
            self._set_status("diff is stale — refreshing")
            self._refresh(target_file=hunk.file_path, target_header=hunk.header)
        else:
            self._set_status(f"stage failed: {err}")
            self._draw()

    def action_discard_hunk(self) -> None:
        if self.source != "unstaged":
            return
        hunk = self.hunks[self.hunk_idx]
        saved_idx = self.hunk_idx
        ok, err = git_discard_hunk(hunk)
        if ok:
            self._set_status(f"discarded hunk {hunk.hunk_index}/{hunk.hunk_total} of {hunk.file_path}")
            self._refresh(target_hunk_idx=saved_idx)
        else:
            self._set_status(f"discard failed: {err}")
            self._draw()

    def action_stage_file(self) -> None:
        if self.source != "unstaged":
            return
        hunk = self.hunks[self.hunk_idx]
        first_of_file = self.hunk_idx - (hunk.hunk_index - 1)
        ok, err = git_stage_file(hunk.file_path)
        if ok:
            self._set_status(f"staged {hunk.file_path}")
            self._refresh(target_hunk_idx=first_of_file)
        else:
            self._set_status(f"stage failed: {err}")
            self._draw()

    def action_unstage_hunk(self) -> None:
        if self.source != "staged":
            return
        hunk = self.hunks[self.hunk_idx]
        saved_idx = self.hunk_idx
        ok, err = git_unstage_hunk(hunk)
        if ok:
            self._set_status(f"unstaged hunk {hunk.hunk_index}/{hunk.hunk_total} of {hunk.file_path}")
            self._refresh(target_hunk_idx=saved_idx)
        else:
            self._set_status(f"unstage failed: {err}")
            self._draw()

    def action_commit(self) -> None:
        if self.source not in ("unstaged", "staged"): return
        inp = self.query_one("#commit-input", Input)
        self.query_one("#bottom", Static).display = False
        inp.disabled = False
        inp.display = True
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        if not message:
            return
        ok, out = git_commit(message)
        if ok:
            self.exit()
            return
        self._set_status(f"commit failed: {out}")
        self._cancel_commit()

    def _cancel_commit(self) -> None:
        inp = self.query_one("#commit-input", Input)
        inp.clear()
        inp.disabled = True
        inp.display = False
        self.query_one("#bottom", Static).display = True
        self._draw()

    def on_key(self, event) -> None:
        if self.query_one("#commit-input", Input).display:
            return
        if event.key in ("space", " "):
            self.action_next_page()
            event.stop()
        elif event.key in ("grave_accent", "`"):
            self.action_prev_page()
            event.stop()

    def on_mouse_scroll_down(self, event) -> None:
        if getattr(event, "ctrl", False):
            self.action_newer_commit()
            event.stop()
            return
        self.action_next_page()

    def on_mouse_scroll_up(self, event) -> None:
        if getattr(event, "ctrl", False):
            self.action_older_commit()
            event.stop()
            return
        self.action_prev_page()

    def action_quit(self) -> None:
        inp = self.query_one("#commit-input", Input)
        if inp.display:
            self._cancel_commit()
        else:
            self.exit()

    def action_prev_file(self) -> None:
        cur = self.hunks[self.hunk_idx].file_path
        # find start of current file
        start = self.hunk_idx
        while start > 0 and self.hunks[start - 1].file_path == cur:
            start -= 1
        if start > 0:
            prev = self.hunks[start - 1].file_path
            while start > 0 and self.hunks[start - 1].file_path == prev:
                start -= 1
            self.hunk_idx = start
            self.scroll_offset = 0
        self._draw()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Must be inside a git repo
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        cwd = os.getcwd()
        try:
            ans = input(f"Not a git repository.\nRun 'git init' in {cwd}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if ans not in ("y", "yes"):
            sys.exit(1)
        init = subprocess.run(
            ["git", "init"], capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if init.returncode != 0:
            print(f"git init failed: {(init.stderr or '').strip()}", file=sys.stderr)
            sys.exit(1)
        print((init.stdout or "").strip())
        global REPO_ROOT
        REPO_ROOT = _get_repo_root()

    source, diff_text, initial_view = load_view("unstaged")
    hunks = parse_diff(diff_text) if diff_text else []
    GitscanApp(hunks, source, initial_view=initial_view).run()


if __name__ == "__main__":
    main()
