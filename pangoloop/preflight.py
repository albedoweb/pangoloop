"""A consistency check of instructions before agents get them, ported from a shell script of the project the package came out of.

An independent reviewer model reads the instructions exactly as an agent will see them and lists contradictions,
sentences with two readings, and rules that cannot be met. With several readers they run in parallel, each through the
runner (pangoloop/runners/), and the answers are gathered in one summary.

    python3 -m pangoloop preflight <file> --reader quick --reader careful --out <folder>

A reader is a name you choose in the `[readers]` table of the configuration file (`quick = "model@provider"`), or
`model@provider` written on the command line. No reader name is built in.

Writes into <folder>: <reader>.md (that reader's answer, as the model wrote it), <reader>/ (the raw prompt, event stream
and stderr of its session) and summary.md.

Before any model is called the file itself is checked for what a reader cannot read, and the warnings (never a refusal;
the exit code is the readers') are printed and put at the top of summary.md: a line longer than 1,200 characters (a
reader's file tool cut one of 2,600 and the reader could not finish), a placeholder left unfilled (`{{...}}`, `@@...@@`),
and, when the file has a `## Nodes` section, a field of a node that the plan parser would reject. `--strict` turns any
warning into exit code 2 before a reader runs.
"""
import concurrent.futures
import os
import re
import time

from pangoloop import config, paths, roles

from pangoloop.plan import parse_nodes
from pangoloop.runners import get_runner

LONG_LINE = 1200   # a reader's file tool cuts a line much longer than this
PLACEHOLDER = re.compile(r'\{\{[^{}\n]*\}\}|@@[^@\n]+@@')
ENGINE_PLACEHOLDERS = ('{{REPO}}', '{{BRANCH}}')   # the engine fills these in a plan before any node reads it

NO_READERS = ('pangoloop preflight: no reader is named. Write a [readers] table in the configuration file, one line for each '
              'reader, e.g. `quick = "model@provider"` (see examples/presets/), or give --reader model@provider.')
TOOLSETS = 'file'         # as the shell script: `-t file`
MAX_TURNS = 40            # as the shell script: `--max-turns 40`

# The prompt of the shell script this was ported from, word for word; $FILE stands where the shell script's heredoc had it.
PROMPT = '''You are checking written instructions before they are given to coding agents and to a reviewer of their work. Read the file $FILE in full. It is the complete text one agent will get. Do not do the task and do not judge whether the task is a good one.

Report only these three kinds of fault:
1. CONTRADICTION: two places where one forbids or excludes what the other requires or allows. A rule of one part that, read literally, forbids what another part asks for counts.
2. TWO READINGS: a sentence that two careful readers could understand differently, with different work or a different verdict as the result.
3. CANNOT BE MET: a rule that cannot be satisfied as written, or that depends on something the text never defines.

For each fault: its kind; both places quoted word for word with their line numbers; why they clash; and one concrete situation in which an agent or a reviewer would be stuck or would make a wrong finding. Do not report style, wording you merely dislike, or missing features.

Your first line is exactly "COUNT: <number of faults>". Then the faults, numbered, most damaging first. If a kind has none, say so in one line at the end.
'''


def prompt_for(path: str) -> str:
    return PROMPT.replace('$FILE', path)


def parse_reader(spec: str, table: dict | None = None) -> tuple[str, str, str, str | None]:
    """A name of `[readers]` (`table`: name -> (model, provider, runner or None)) or `model@provider` ->
    (label, model, provider, runner or None)."""
    table = table or {}
    if spec in table:
        model, provider, own = table[spec]
        return spec, model, provider, own
    if '@' in spec:
        model, provider = spec.rsplit('@', 1)
        if model and provider:
            return re.sub(r'[^A-Za-z0-9._-]+', '_', model), model, provider, None
    known = ', '.join(sorted(table)) or 'none'
    raise ValueError(f'unknown reader {spec!r}: the readers of the configuration file are {known}; or write model@provider')


def count_line(answer: str) -> str:
    for line in answer.splitlines():
        if re.match(r'^\W*COUNT:', line):
            return line.strip()
    return '(no COUNT line)'


def check_file(path: str) -> list[str]:
    """Warnings about what a reader cannot read in the file, before any model is called: a line over 1,200 characters
    (line number and length), a placeholder left unfilled (`{{...}}` or `@@...@@`; the engine's own `{{REPO}}` and
    `{{BRANCH}}` of a plan are not), and, when the file has a `## Nodes` section, the first field of a node that the plan
    parser rejects (the parser's own message). A file that cannot be read gives none (preflight reports that)."""
    try:
        with open(path, errors='replace') as handle:
            text = handle.read()
    except OSError:
        return []
    found = []
    is_plan = '\n## Nodes\n' in text
    for number, line in enumerate(text.split('\n'), 1):
        if len(line) > LONG_LINE:
            found.append(f'line {number}: {len(line)} characters, more than {LONG_LINE}; a reader\'s file tool may cut it '
                         'and the reader could not finish (split the line)')
        for match in PLACEHOLDER.finditer(line):
            if is_plan and match.group(0) in ENGINE_PLACEHOLDERS:
                continue
            found.append(f'line {number}: placeholder left unfilled: {match.group(0)}')
    if is_plan:
        try:
            parse_nodes(text, roles=roles.load(paths.roles_folder()))   # a role the plan names must exist, as the run checks it
        except SystemExit as fault:
            found += [f'plan: {line}' for line in str(fault.code).split('\n')]
    return found


def preflight(path: str, readers: list[str], out: str, runner=None, warnings: list[str] | None = None,
              table: dict | None = None) -> dict:
    """Runs the readers in parallel, writes the files, returns {'readers': [...], 'summary': path, 'warnings': [...]}.
    `warnings` are those of check_file (computed here when not given); they head summary.md. `table` is the `[readers]`
    of the configuration (loaded when not given); with no `readers` every reader of the table runs, and with an empty
    table there is nothing to run (ValueError). `runner`, when given, runs every reader."""
    cfg = None
    if table is None or runner is None:
        cfg = config.load()
        table = cfg.readers if table is None else table
    if not readers and not table:
        raise ValueError(NO_READERS)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    warnings = check_file(path) if warnings is None else warnings
    parsed, seen = [], {}
    for spec in readers or list(table):
        label, model, provider, own = parse_reader(spec, table)
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f'{label}-{seen[label]}'
        parsed.append((label, model, provider, own))
    os.makedirs(out, exist_ok=True)
    prompt = prompt_for(path)
    stem = re.sub(r'[^A-Za-z0-9]+', '-', os.path.basename(path)).strip('-')
    born = format(int(time.time()), 'x')

    def runner_for(own: str | None):
        if runner is not None:
            return runner
        name = own or cfg.runner
        return get_runner(name, cfg.runner_settings[name.replace('-', '_')])

    def one(item: tuple[str, str, str, str | None]) -> dict:
        label, model, provider, own = item
        started = time.time()
        result = runner_for(own).run(session=f'preflight-{stem}-{label}-{born}', model=model, provider=provider, prompt=prompt,
                            cwd=os.getcwd(), toolsets=TOOLSETS, max_turns=MAX_TURNS, folder=f'{out}/{label}')
        seconds = round(time.time() - started)
        with open(f'{out}/{label}.md', 'w') as handle:
            handle.write(result.text)
        return {'label': label, 'model': model, 'provider': provider, 'seconds': seconds,
                'exit': result.exit_code, 'answer': result.text, 'count': count_line(result.text),
                'reported_model': result.reported_model}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(parsed)) as pool:
        results = list(pool.map(one, parsed))
    lines = [f'# Preflight: {path}', '']
    if warnings:
        lines += [f'## Warnings ({len(warnings)}), found in the file before any reader ran', ''] + [f'- {item}' for item in warnings] + ['']
    for item in results:
        lines.append(f"- {item['label']}: model {item['model']} ({item['provider']}), {item['seconds']} s, "
                     f"{item['count']}" + ('' if item['exit'] == 0 and item['answer'].strip()
                                           else f"  (exit {item['exit']}, " + ('no answer)' if not item['answer'].strip() else 'answer kept)')))
    for item in results:
        lines += ['', f"## {item['label']}", '', item['answer'].strip() or '(no answer)']
    summary = f'{out}/summary.md'
    with open(summary, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    return {'readers': results, 'summary': summary, 'warnings': warnings}


def main(argv: list[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog='pangoloop preflight', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='example:\n  python3 -m pangoloop preflight plan.md --reader quick --reader careful --out pf/\n\n'
                                            'the readers are the names of [readers] in the configuration file, or model@provider; '
                                            'with no --reader, every reader of the table runs')
    parser.add_argument('file', help='the instructions as an agent will see them')
    parser.add_argument('--reader', action='append', default=[], help='a name from [readers] of the configuration file, or '
                        'model@provider; repeat for several (default: every reader of the table)')
    parser.add_argument('--out', default='preflight-out', help='folder for the answers and summary.md (default: ./preflight-out)')
    parser.add_argument('--strict', action='store_true',
                        help='warnings about the file (a line over 1,200 characters, a placeholder left unfilled, a node '
                             'field the plan parser rejects) end with exit code 2 before any reader runs')
    args = parser.parse_args(argv)
    warnings = check_file(os.path.abspath(args.file))
    for item in warnings:
        print(f'warning: {item}')
    if warnings and args.strict:
        print(f'{len(warnings)} warning{"" if len(warnings) == 1 else "s"} and --strict: no reader was run')
        return 2
    try:
        table = config.load().readers
        if not args.reader and not table:
            parser.error(NO_READERS)
        done = preflight(args.file, args.reader, args.out, warnings=warnings, table=table)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    for item in done['readers']:
        print(f"{item['label']:<12}{item['model']:<18}{item['seconds']:>5}s  {item['count']}")
    print(done['summary'])
    return 0 if all(item['exit'] == 0 and item['answer'].strip() for item in done['readers']) else 1
