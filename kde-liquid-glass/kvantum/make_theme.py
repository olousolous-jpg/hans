#!/usr/bin/env python3
"""Build a theme .kvconfig: base theme + overridden values.

    make_theme.py BASE.kvconfig overrides.ini OUTPUT.kvconfig
"""
import configparser
import sys


def load(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",),
                                   comment_prefixes=(";", "#"), inline_comment_prefixes=None)
    cp.optionxform = str  # Kvantum keys are case-sensitive
    with open(path, encoding="utf-8") as f:
        cp.read_file(f)
    return cp


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    base, over = load(sys.argv[1]), load(sys.argv[2])
    for section in over.sections():
        if not base.has_section(section):
            base.add_section(section)
        for key, value in over.items(section):
            base.set(section, key, value)
    with open(sys.argv[3], "w", encoding="utf-8") as f:
        base.write(f, space_around_delimiters=False)


if __name__ == "__main__":
    main()
