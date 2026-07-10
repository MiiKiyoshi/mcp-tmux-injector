"""Output filtering: tqdm stripping, grep-style filters, dedupe, save."""
import os
import re

# tqdm progress bar detection patterns
_TQDM_INDICATOR = re.compile(r'\d+%\|')
_TQDM_FRAC = re.compile(r'\|\s*\d+/\d+')
_TQDM_SPEED = re.compile(r'(?:it|s)/(?:s|it)[\]\s]')
_TQDM_BLOCK_ONLY = re.compile(r'^[\s█▏▎▍▌▋▊▉]+$')
# Line is a tqdm progress bar — changes every iteration, useless as fingerprint anchor
TQDM_PROGRESS_LINE = re.compile(r'\d+%\||\d+:\d+<\d+:\d+|(?:it|s)/(?:s|it)')


def _is_tqdm_line(line: str) -> bool:
    """Check if a line is part of tqdm progress output."""
    if _TQDM_INDICATOR.search(line):
        return True
    if _TQDM_FRAC.search(line):
        return True
    if _TQDM_SPEED.search(line):
        return True
    if line.strip() and _TQDM_BLOCK_ONLY.match(line):
        return True
    return False


def _trim_tqdm_group(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """Find the last progress update within a tqdm group.

    In narrow panes, each tqdm update wraps across multiple lines.
    Only the final update (e.g., 100%) matters.
    A new update starts at a line matching _TQDM_INDICATOR (\\d+%\\|).
    """
    last_update_start = start
    for idx in range(start, end):
        if _TQDM_INDICATOR.search(lines[idx]):
            last_update_start = idx
    return last_update_start, end


def filter_tqdm(lines: list[str]) -> list[str]:
    """Remove tqdm progress lines, keeping only the final state of the last group."""
    is_tqdm = [_is_tqdm_line(line) for line in lines]

    # Find consecutive tqdm groups
    groups = []  # [(start, end), ...]
    group_start = None
    for idx, flag in enumerate(is_tqdm):
        if flag and group_start is None:
            group_start = idx
        elif not flag and group_start is not None:
            groups.append((group_start, idx))
            group_start = None
    if group_start is not None:
        groups.append((group_start, len(lines)))

    if not groups:
        return lines

    # Remove all tqdm groups except the last
    remove = set()
    for start, end in groups[:-1]:
        remove.update(range(start, end))

    # Trim last group to final progress update only
    last_start, last_end = groups[-1]
    trim_start, trim_end = _trim_tqdm_group(lines, last_start, last_end)
    remove.update(range(last_start, trim_start))

    return [line for idx, line in enumerate(lines) if idx not in remove]


def apply_dedupe(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate lines."""
    if not lines:
        return lines
    result = [lines[0]]
    for line in lines[1:]:
        if line != result[-1]:
            result.append(line)
    return result


def apply_grep_with_context(lines: list[str], pattern: re.Pattern, before: int = 0, after: int = 0) -> list[str]:
    """Apply grep with context lines (like grep -B/-A)."""
    if before <= 0 and after <= 0:
        return [line for line in lines if pattern.search(line)]

    matches = set()
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, i - before)
            end = min(len(lines), i + after + 1)
            for j in range(start, end):
                matches.add(j)

    return [lines[i] for i in sorted(matches)]


def save_to_file(content: str, file_path: str, append: bool) -> str:
    """Save content to file."""
    mode = 'a' if append else 'w'
    with open(os.path.expanduser(file_path), mode) as f:
        f.write(content)
        if not content.endswith('\n'):
            f.write('\n')
    return file_path


def parse_rel_range(rel_range: str) -> tuple[int, int]:
    """Parse relative range string like '100:50' or '-100:-50'.
    Always returns negative indices (from end)."""
    parts = rel_range.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid rel_range format: {rel_range}. Use 'START:END' (e.g., '100:50')")

    start_str, end_str = parts
    start = -abs(int(start_str)) if start_str.strip() else None
    end = -abs(int(end_str)) if end_str.strip() and int(end_str) != 0 else None
    return start, end


def apply_output_filters(
    lines: list[str],
    grep: str = None,
    v: str = None,
    i: bool = False,
    w: bool = False,
    F: bool = False,
    m: int = None,
    A: int = None,
    B: int = None,
    C: int = None,
    n: bool = False,
    uniq: bool = True,
    save: str = None,
    append: bool = True,
    n_negative: bool = False,
    prefix: str = None,
    suffix: str = None,
    strip_tqdm: bool = False
) -> str:
    """Apply common output filters and optionally save to file.

    Args:
        lines: Input lines to filter
        grep: Filter lines matching this regex pattern
        v: Exclude lines matching this regex pattern (like grep -v)
        i: Case insensitive matching (like grep -i)
        w: Word match - pattern must match whole word (like grep -w)
        F: Fixed string - treat pattern as literal, not regex (like grep -F)
        m: Max count - return at most N matching lines (like grep -m)
        A: Lines after grep match (like grep -A)
        B: Lines before grep match (like grep -B)
        C: Lines before and after grep match (like grep -C)
        n: Show line numbers
        uniq: Remove consecutive duplicate lines (like uniq, default: True)
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
        n_negative: If True, show negative line numbers (for capture_pane)
        prefix: Text to prepend when saving (optional)
        suffix: Text to append when saving (optional)
        strip_tqdm: Remove tqdm progress lines, keeping only the last group

    Returns:
        Filtered output as string
    """
    # strip_tqdm: applied before grep
    if strip_tqdm:
        lines = filter_tqdm(lines)

    # Build regex flags
    flags = re.IGNORECASE if i else 0

    # Apply grep filter with context (A/B/C)
    if grep:
        pat = re.escape(grep) if F else grep.replace(r'\|', '|')
        pat = rf'\b{pat}\b' if w else pat
        pattern = re.compile(pat, flags)
        before = B if B is not None else (C or 0)
        after = A if A is not None else (C or 0)
        lines = apply_grep_with_context(lines, pattern, before, after)
        if m is not None and m > 0:
            lines = lines[:m]

    # Apply exclude filter (grep -v)
    if v:
        pat_v = re.escape(v) if F else v
        pat_v = rf'\b{pat_v}\b' if w else pat_v
        exc_pattern = re.compile(pat_v, flags)
        lines = [line for line in lines if not exc_pattern.search(line)]

    # Apply uniq
    if uniq:
        lines = apply_dedupe(lines)

    # Apply line numbers
    if n:
        if n_negative:
            total = len(lines)
            lines = [f"{idx - total}: {line}" for idx, line in enumerate(lines)]
        else:
            lines = [f"{idx}: {line}" for idx, line in enumerate(lines)]

    result = '\n'.join(lines)

    # Save to file if requested
    if save:
        # Process escape sequences (\n, \t) in prefix/suffix
        eff_prefix = (prefix or '').replace('\\n', '\n').replace('\\t', '\t')
        eff_suffix = (suffix or '').replace('\\n', '\n').replace('\\t', '\t')
        save_content = eff_prefix + result + eff_suffix
        save_to_file(save_content, save, append)
        return f"Saved to {save} ({len(lines)} lines)"

    return result
