#!/usr/bin/env python3
"""Writes pangoloop/example_roles.py from the files of examples/roles/ (the one source). `pangoloop init` copies the roles from
that module because an installed package has no examples/ folder; tests/test_kinds_and_roles.py fails when the two differ.

    python3 scripts/gen_example_roles.py
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = ('coder', 'test-writer', 'interface-builder', 'researcher', 'reviewer', 'security-reviewer', 'simplicity-reviewer')


def main() -> int:
    lines = ['"""The example roles, as `pangoloop init` copies them. GENERATED from examples/roles/*.md by',
             'scripts/gen_example_roles.py: edit the files there and run the script, not this module."""', '', 'ROLES = {']
    for name in ORDER:
        with open(os.path.join(ROOT, 'examples', 'roles', f'{name}.md')) as handle:
            lines.append(f'    {name!r}: {handle.read()!r},')
    lines.append('}')
    with open(os.path.join(ROOT, 'pangoloop', 'example_roles.py'), 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
