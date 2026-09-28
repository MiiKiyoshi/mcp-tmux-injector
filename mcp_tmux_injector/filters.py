"""Output filtering: grep with context, consecutive-duplicate removal."""
import re

# Line is a tqdm progress bar — changes every iteration, useless as fingerprint anchor
TQDM_PROGRESS_LINE = re.compile(r'\d+%\||\d+:\d+<\d+:\d+|(?:it|s)/(?:s|it)')


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


def apply_output_filters(lines: list[str], grep: str = None, C: int = 0) -> str:
    """Keep lines matching grep (a Python regex, grep's \\| also works) with C
    lines of context, then drop consecutive duplicate lines."""
    if grep:
        pattern = re.compile(grep.replace(r'\|', '|'))
        lines = apply_grep_with_context(lines, pattern, C, C)
    return '\n'.join(apply_dedupe(lines))
