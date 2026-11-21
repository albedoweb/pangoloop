"""Test helper, not a test: a stand-in for the `claude` command that prints a stream in the format RECORDED from Claude
Code 2.1.327 (tests/fixtures/claude_code/3-edit, with the answer replaced). Used through PANGOLOOP_CLAUDE_COMMAND.

    claude_stub.py -p <prompt> [flags]

It appends one JSON line to $STUB_LOG (argv, cwd), writes `<node>.txt` into its working folder for a node whose prompt
says "Your node is `<node>`" (so that a build node has work to integrate), and answers `done`, or `VERDICT: PASS` to a
reviewer's prompt. STUB_CLAUDE=ask makes it end with `NEEDS HUMAN: which one?`; STUB_CLAUDE=fail makes it print the
recorded turn-limit stream and exit 1; STUB_CLAUDE=refuse prints an API overload error as the result, exit 1, and
succeeds from the third call on (counted in $STUB_COUNT). STUB_CLAUDE=none ends with
`NEEDS COORDINATOR: none \u2014 part C is complete and verified.` (a closing line that is not a request). The log also has
the TMPDIR the stub was started with. STUB_DELAY=<seconds> makes each node take a few of them, STUB_ECHO_MODEL=1 reports the model that
`--model` asked for, STUB_NO_COST=1 takes the cost out of the stream (the recording carries one, a stand-in has none).
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, 'fixtures', 'claude_code')


def drop_costs(value):
    """Removes every cost the recorded stream carries (`total_cost_usd`, `costUSD`), wherever it stands."""
    if isinstance(value, dict):
        for key in ('total_cost_usd', 'costUSD'):
            value.pop(key, None)
        for item in value.values():
            drop_costs(item)
    elif isinstance(value, list):
        for item in value:
            drop_costs(item)


def stream(case: str, answer: str | None = None) -> list:
    with open(os.path.join(FIXTURES, case, 'events.jsonl')) as handle:
        lines = [json.loads(line) for line in handle]
    if os.environ.get('STUB_NO_COST'):   # a stand-in has no price: leave out the cost the recorded stream carried
        for event in lines:
            drop_costs(event)
    if answer is not None:
        for event in lines:
            if event['type'] == 'result':
                event['result'] = answer
            if event['type'] == 'assistant':
                for block in event['message']['content']:
                    if block.get('type') == 'text':
                        block['text'] = answer
    return lines


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[1] if argv[:1] == ['-p'] and len(argv) > 1 else ''
    with open(os.environ['STUB_LOG'], 'a') as log:
        log.write(json.dumps({'argv': argv, 'cwd': os.getcwd(), 'stdin_tty': sys.stdin.isatty(),
                              'tmpdir': os.environ.get('TMPDIR')}) + '\n')
    mode = os.environ.get('STUB_CLAUDE', '')
    node = re.search(r'Your node is `([^`]+)`', prompt)
    if node and 'VERDICT' not in prompt:
        with open(f'{node.group(1)}.txt', 'w') as handle:
            handle.write('made by the stub\n')
    if mode == 'fail':
        out, code = stream('5-turn-limit'), 1
    elif mode == 'refuse':
        count = 1
        counter = os.environ['STUB_COUNT']
        if os.path.exists(counter):
            count = int(open(counter).read()) + 1
        open(counter, 'w').write(str(count))
        if count < 3:
            out = stream('1-plain', 'API Error: 529 {"type":"overloaded_error"}')
            out[-1]['is_error'] = True
            code = 1
        else:
            out, code = stream('3-edit', 'done'), 0
    else:
        answer = ('VERDICT: PASS' if 'VERDICT' in prompt else 'NEEDS HUMAN: which one?' if mode == 'ask'
                  else 'Part C is complete.\n\nNEEDS COORDINATOR: none \u2014 part C is complete and verified.' if mode == 'none'
                  else 'done')
        out, code = stream('3-edit', answer), 0
    delay = float(os.environ.get('STUB_DELAY') or 0)
    if delay and node:   # each node a little different, so that a picture of the run has bars of several lengths
        time.sleep(delay * (1 + sum(map(ord, node.group(1))) % 3 / 2))
    asked = argv[argv.index('--model') + 1] if '--model' in argv and argv.index('--model') + 1 < len(argv) else None
    for event in out:
        text = json.dumps(event)
        if asked and os.environ.get('STUB_ECHO_MODEL'):   # report the model the engine asked for, as a real session does
            text = text.replace('small-model-5-5', asked)
        print(text, flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
