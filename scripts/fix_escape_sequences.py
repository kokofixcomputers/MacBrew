#!/usr/bin/env python3
"""
Script to find and fix invalid escape sequences in regex patterns.
Specifically: converts f'^\s*{field}\s+...' to rf'^\s*{field}\s+...'
"""

import sys
from pathlib import Path


def fix_escape_sequences(filepath: str) -> int:
    """
    Find and fix invalid escape sequences in regex patterns.
    Returns the number of replacements made.
    """
    path = Path(filepath)

    # Read the file
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    count = 0
    lines = content.split('\n')
    new_lines = []

    for line in lines:
        original_line = line

        # Look for: re.search(f'^\s*{...}\s+
        # Replace f' with rf' when followed by ^\s*
        if "re.search(f'^\\s*" in line:
            line = line.replace("re.search(f'^\\s*", "re.search(rf'^\\s*")
            if line != original_line:
                count += 1

        # Handle double quotes version
        if 're.search(f"^\\s*' in line:
            line = line.replace('re.search(f"^\\s*', 're.search(rf"^\\s*')
            if line != original_line:
                count += 1

        new_lines.append(line)

    new_content = '\n'.join(new_lines)

    if count > 0:
        # Write back
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Fixed {count} occurrence(s) in {filepath}")
    else:
        print(f"No occurrences found in {filepath}")

    return count


def main():
    if len(sys.argv) < 2:
        print("Usage: python fix_escape_sequences.py <filepath>")
        print("       python fix_escape_sequences.py <directory>")
        sys.exit(1)

    target = sys.argv[1]
    total_fixes = 0

    if Path(target).is_file():
        total_fixes = fix_escape_sequences(target)
    elif Path(target).is_dir():
        # Find all .py files in directory
        py_files = list(Path(target).glob('**/*.py'))
        print(f"Found {len(py_files)} Python files in {target}")
        for py_file in py_files:
            total_fixes += fix_escape_sequences(str(py_file))
    else:
        print(f"Error: {target} is not a valid file or directory")
        sys.exit(1)

    print(f"\nTotal fixes: {total_fixes}")


if __name__ == '__main__':
    main()
