"""Money a command reports itself (November 21, 2026): `PANGOLOOP-SPEND: <usd> [label]` on a line of its own output, read from
the whole output of every command the engine captures (a `run` step of a script node, an acceptance command), kept as a record
with the model `script` and `cost_source` `reported`, counted by the run's cost and by the check before each worker starts.
Stubs stand in for the model."""
import glob
import json
import unittest

import helpers
import support
from pangoloop import costs
from test_resume import Scene
from test_script_node import run_plan


def spend_step(*lines):
    """A `run` step that prints the given lines (each a full line of output)."""
    return "run `printf '" + ''.join(line + r'\n' for line in lines) + "'`"


class Parsing(unittest.TestCase):
    def test_a_valid_line_with_and_without_a_label(self):
        found = costs.parse_spend('noise\nPANGOLOOP-SPEND: 0.25\nPANGOLOOP-SPEND: 1.5 the playtester, 3 rounds\n')
        self.assertEqual(found['usd'], 1.75, 'several lines add up')
        self.assertEqual(found['lines'], [{'usd': 0.25, 'label': ''}, {'usd': 1.5, 'label': 'the playtester, 3 rounds'}])
        self.assertEqual(found['warnings'], [])
        self.assertEqual(costs.parse_spend('PANGOLOOP-SPEND: 3\r\n')['usd'], 3.0, 'an integer and a CRLF line end')
        self.assertEqual(costs.parse_spend('PANGOLOOP-SPEND: 0')['usd'], 0.0, 'zero is a valid amount')

    def test_only_at_the_start_of_a_line(self):
        for text in ('echo PANGOLOOP-SPEND: 1.0', ' PANGOLOOP-SPEND: 1.0', 'x PANGOLOOP-SPEND: 1.0\nplain', ''):
            found = costs.parse_spend(text)
            self.assertEqual((found['usd'], found['lines'], found['warnings']), (None, [], []), text)
        self.assertIsNone(costs.parse_spend(None)['usd'])

    def test_a_bad_or_negative_number_is_ignored_with_a_warning(self):
        text = ('PANGOLOOP-SPEND: -1.0\nPANGOLOOP-SPEND: $2.0\nPANGOLOOP-SPEND: abc\nPANGOLOOP-SPEND: 1e3\n'
                'PANGOLOOP-SPEND: 1,5\nPANGOLOOP-SPEND:\nPANGOLOOP-SPEND: 0.5 good\n')
        found = costs.parse_spend(text)
        self.assertEqual(found['usd'], 0.5, 'only the good line counts')
        self.assertEqual(len(found['warnings']), 6)
        self.assertTrue(found['warnings'][0].startswith('line 1:') and '-1.0' in found['warnings'][0])
        self.assertIsNone(costs.parse_spend('PANGOLOOP-SPEND: -2')['usd'], 'no valid line: no amount, not zero')

    def test_run_cost_counts_a_reported_record_and_no_other_script_record(self):
        records = [{'model': 'script', 'cost_usd': 0.5, 'cost_source': 'reported'},
                   {'model': 'script', 'cost_usd': 9.0},   # not reported: not counted
                   {'model': 'script', 'warnings': ['x']}]
        total = costs.run_cost(records, {})
        self.assertEqual((total['usd'], total['reported_usd'], total['workers'], total['partial']), (0.5, 0.5, 0, False))
        self.assertEqual(costs.run_cost([], {})['reported_usd'], 0.0)
        mixed = costs.run_cost(records + [{'model': 'stub', 'usage': {'cost_usd': 0.25}, 'cost_usd': 0.25}], {})
        self.assertEqual((mixed['usd'], mixed['workers'], mixed['workers_with_cost']), (0.75, 1, 1))


class Bounds(unittest.TestCase):
    def test_a_record_stores_at_most_fifty_lines_and_warnings_and_cuts_labels(self):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        sg, run, _ = run_plan(self, home, repo, helpers.script_node('s', "run `true`"))
        text = '\n'.join(['PANGOLOOP-SPEND: 0.01 ' + 'L' * 500] * 200 + ['PANGOLOOP-SPEND: bad'] * 80)
        run.note_spend(text, 's', 'script step', 'x', 0, 1)
        record = run.records[-1]
        self.assertEqual(record['cost_usd'], 2.0, 'every valid line is summed')
        self.assertEqual((len(record['spend_lines']), record['lines_dropped']), (50, 150))
        self.assertEqual(len(record['spend_lines'][0]['label']), 200)
        self.assertEqual((len(record['warnings']), record['warnings_dropped']), (50, 30))


class PageText(unittest.TestCase):
    def test_only_reported_spend_is_said_so(self):
        import inspect
        from pangoloop import view
        source = open(inspect.getsourcefile(view)).read()
        self.assertIn("C.workers > 0 && !C.workers_with_cost && C.reported_usd > 0", source)
        self.assertIn('only what commands reported; the cost of the workers is unknown', source)


class InTheEngine(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)

    def run_it(self, *nodes, limit=None, env=None, behave=None):
        sg, run, calls = run_plan(self, self.home, self.repo, *nodes, env=env, behave=behave)
        sg.MAX_COST_USD = limit
        verdict = run.execute()
        sg.write_result(run, verdict, run.status)
        return run, verdict, calls, helpers.read_json(f'{self.home}/runs/r/result.json')

    def test_a_script_step_reports_spend_and_it_is_in_the_run_cost_and_result(self):
        run, verdict, calls, result = self.run_it(helpers.script_node(
            's', spend_step('start', 'PANGOLOOP-SPEND: 0.5 first', 'mid PANGOLOOP-SPEND: 9', 'PANGOLOOP-SPEND: 0.25',
                            'PANGOLOOP-SPEND: -3', 'PANGOLOOP-SPEND: lots')))
        self.assertEqual(verdict, 'verified')
        self.assertEqual((result['cost']['usd'], result['cost']['reported_usd'], result['cost']['partial']), (0.75, 0.75, False))
        record = next(w for w in result['workers'] if w.get('cost_source') == 'reported')
        self.assertEqual((record['model'], record['node'], record['cost_usd']), ('script', 's', 0.75))
        self.assertEqual([item['label'] for item in record['spend_lines']], ['first', ''])
        self.assertEqual(len(record['warnings']), 2, 'the bad and the negative number: recorded, not counted')
        state = helpers.read_json(f'{self.home}/runs/r/state.json')
        self.assertEqual(state['cost']['usd'], 0.75, '`status --json` and the page read the state')
        self.assertEqual(run.records[0]['cost_usd'], 0.75)

    def test_no_spend_line_leaves_the_cost_as_it_was(self):
        run, verdict, calls, result = self.run_it(helpers.script_node('s', "run `echo PANGOLOOP-SPEND-ish 1.0`"))
        self.assertEqual((verdict, result['cost']['usd'], result['cost']['reported_usd']), ('verified', 0.0, 0.0))
        self.assertEqual(len(result['workers']), 1, 'no extra record')

    def test_the_whole_output_is_scanned_not_the_stored_tail(self):
        # the line comes first, then 6,000 characters: the stored tail is 2,000 and the failure text 1,500
        step = "run `echo 'PANGOLOOP-SPEND: 0.4 early' ; head -c 6000 /dev/zero | tr '\\0' x ; echo`"
        run, verdict, calls, result = self.run_it(helpers.script_node('s', step))
        self.assertEqual(verdict, 'verified')
        self.assertEqual(result['cost']['usd'], 0.4)
        path = glob.glob(f'{self.home}/runs/r/workers/*-s/events.jsonl')[0]
        outputs = [e['output'] for e in map(json.loads, open(path)) if e['type'] == 'tool_result']
        self.assertEqual(len(outputs), 1)
        self.assertNotIn('PANGOLOOP-SPEND', outputs[0], 'the stored tail does not hold the line; the engine still saw it')

    def test_a_command_that_timed_out_still_counts_what_it_printed(self):
        step = "run `echo 'PANGOLOOP-SPEND: 0.3 before the stop' ; sleep 30`"
        run, verdict, calls, result = self.run_it(helpers.script_node('s', step), env={'SG_ACCEPTANCE_SECONDS': '1'})
        self.assertEqual(verdict, 'not_verified')
        self.assertEqual(result['nodes']['s'], 'failed')
        self.assertEqual(result['cost']['usd'], 0.3)
        record = next(w for w in result['workers'] if w.get('cost_source') == 'reported')
        self.assertEqual(record['exit'], 124)

    def test_an_acceptance_command_reports_spend_too(self):
        acceptance = helpers.node_text('acc', role='acceptance', VERIFY="`printf 'PANGOLOOP-SPEND: 0.2 check\\n'`")
        run, verdict, calls, result = self.run_it(acceptance)
        self.assertEqual(verdict, 'verified')
        self.assertEqual(result['cost']['usd'], 0.2)

    def test_the_cap_stops_the_next_worker_when_reported_spend_crosses_it(self):
        nodes = (helpers.script_node('s', spend_step('PANGOLOOP-SPEND: 0.5 big')),
                 helpers.node_text('b', deps='s'), helpers.node_text('c', deps='b'))
        run, verdict, calls, result = self.run_it(*nodes, limit=0.4)
        self.assertEqual(verdict, 'not_verified')
        self.assertEqual(calls, [], 'the spend was seen after the command; the next worker never started')
        self.assertEqual((result['reason'], result['node_reasons']), ('cost limit', {'b': 'cost limit'}))
        self.assertEqual(result['nodes']['s'], 'accepted', 'the command that crossed the limit was not stopped')
        self.assertEqual((result['cost']['usd'], result['cost']['limit_usd']), (0.5, 0.4))

    def test_under_or_at_the_cap_nothing_stops(self):
        nodes = (helpers.script_node('s', spend_step('PANGOLOOP-SPEND: 0.5')), helpers.node_text('b', deps='s'))
        run, verdict, calls, result = self.run_it(*nodes, limit=0.5)
        self.assertEqual((verdict, [c['node'] for c in calls]), ('verified', ['b']))


class AcrossSegments(unittest.TestCase):
    """The engine in a process of its own (tests/resume_driver.py: each stub worker reports $0.01)."""

    def nodes(self):
        return (helpers.node_text('a'), helpers.script_node('s', spend_step('PANGOLOOP-SPEND: 0.5 scoring'), deps='a'),
                helpers.node_text('c', deps='s'))

    def test_a_resume_counts_the_reported_spend_once(self):
        scene = Scene(self, *self.nodes(), env={'PANGOLOOP_MAX_COST_USD': '0.4'})
        first = scene.run('r')
        self.assertEqual(first.code, 1, first.out + first.err)
        result = scene.result('r')
        self.assertEqual((result['reason'], result['node_reasons']), ('cost limit', {'c': 'cost limit'}))
        self.assertEqual(result['cost']['usd'], 0.51, 'a ($0.01) and the reported $0.50')
        self.assertEqual([call['node'] for call in scene.calls()], ['a'])
        second = scene.run('r', '--resume', env={'PANGOLOOP_MAX_COST_USD': '5'})
        self.assertEqual(second.code, 0, second.out + second.err)
        self.assertEqual([call['node'] for call in scene.calls()], ['a', 'c'], 'the script node was not run again')
        result = scene.result('r')
        self.assertEqual(result['verdict'], 'verified')
        self.assertEqual((result['cost']['usd'], result['cost']['reported_usd']), (0.52, 0.5), 'once, not twice')
        self.assertEqual(sum(1 for w in result['workers'] if w.get('cost_source') == 'reported'), 1)

    def test_the_limit_counts_the_spend_of_earlier_segments_on_resume(self):
        scene = Scene(self, *self.nodes(), env={'PANGOLOOP_MAX_COST_USD': '0.4'})
        self.assertEqual(scene.run('r').code, 1)
        again = scene.run('r', '--resume')
        self.assertEqual(again.code, 1, again.out + again.err)
        self.assertEqual(scene.result('r')['reason'], 'cost limit')
        self.assertEqual([call['node'] for call in scene.calls()], ['a'], 'c is still refused')


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
