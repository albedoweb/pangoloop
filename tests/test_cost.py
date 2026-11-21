"""Fault 4 (November 20, 2026): a table of prices in the configuration file, a cost per worker and per run (partial where a
model has no price), `max_cost_usd` (no new worker starts over it; the run ends not verified, reason `cost limit`, exit 1),
all segments of a resumed run counted, and the cost on the page. Stubs stand in for the model."""
import os
import unittest

import engine_stub
import helpers
import support
from pangoloop import config, costs, view
from test_resume import Scene, chain

TABLE = {'big-model-5': {'input': 3, 'output': 15, 'cache_read': 0.3, 'cache_write': 3.75},
         'big-model-5-5': {'input': 4, 'output': 20, 'cache_read': 0.4, 'cache_write': 5}}


class Prices(unittest.TestCase):
    def test_exact_beats_prefix_and_the_longest_prefix_wins(self):
        self.assertEqual(costs.find_price(TABLE, 'big-model-5-5')[0], 'big-model-5-5')
        self.assertEqual(costs.find_price(TABLE, 'big-model-5-5-20261101')[0], 'big-model-5-5')
        self.assertEqual(costs.find_price(TABLE, 'big-model-5-1')[0], 'big-model-5')
        self.assertIsNone(costs.find_price(TABLE, 'other-model-1'))
        self.assertEqual(costs.find_price(TABLE, None, 'model-s', 'big-model-5')[0], 'big-model-5', 'first model with a price')

    def test_the_cost_of_a_worker(self):
        usage = {'input': 1_000_000, 'output': 100_000, 'cache_read': 2_000_000, 'cache_write': 400_000}
        record = {'model': 'model-s', 'reported_model': 'big-model-5-1', 'usage': usage}
        self.assertEqual(costs.worker_cost(record, TABLE), (3 + 1.5 + 0.6 + 1.5, 'price table'))

    def test_no_price_means_none_unless_the_runner_stated_a_cost(self):
        usage = {'input': 5, 'output': 5}
        self.assertEqual(costs.worker_cost({'model': 'cheap-model', 'usage': usage}, TABLE), (None, None))
        self.assertEqual(costs.worker_cost({'model': 'cheap-model', 'usage': dict(usage, cost_usd=0.25)}, TABLE),
                         (0.25, 'runner'))
        self.assertEqual(costs.worker_cost({'model': 'big-model-5', 'usage': None}, TABLE), (None, None),
                         'a priced model with no token counts is unknown, not free')

    def test_the_run_is_partial_and_names_the_models_without_a_price(self):
        usage = {'input': 1_000_000, 'output': 0, 'cache_read': 0, 'cache_write': 0}
        records = [{'model': 'big-model-5', 'usage': usage}, {'model': 'other-model-1', 'usage': usage},
                   {'model': 'cheap-model', 'usage': usage}, {'model': 'script'}]
        total = costs.run_cost(records, TABLE, 1.0)
        self.assertEqual((total['usd'], total['partial'], total['models_without_price']),
                         (3.0, True, ['cheap-model', 'other-model-1']))
        self.assertEqual((total['workers'], total['workers_with_cost'], total['limit_usd']), (3, 1, 1.0), 'script is no worker')
        whole = costs.run_cost(records[:1], TABLE)
        self.assertEqual((whole['usd'], whole['partial'], whole['models_without_price']), (3.0, False, []))
        self.assertEqual(costs.run_cost([], TABLE)['usd'], 0.0)
        self.assertIsNone(costs.run_cost(records[1:3], TABLE)['usd'], 'none known: unknown, not zero')

    def test_a_mistake_in_the_table(self):
        for bad, text in (({'m': 3}, 'expected'), ({'m': {'input': 1}}, 'missing'),
                          ({'m': {'input': 1, 'output': 1, 'cache_read': 1, 'cache_write': -1}}, 'cache_write'),
                          ({'m': {'input': 1, 'output': 1, 'cache_read': 1, 'cache_write': 1, 'x': 1}}, 'unknown keys')):
            with self.assertRaises(SystemExit) as stop:
                costs.parse_prices(bad)
            self.assertIn(text, str(stop.exception.code))


class Configuration(unittest.TestCase):
    def load(self, text='', **environ):
        home = support.temp_dir(self)
        path = os.path.join(home, 'pangoloop.toml')
        with open(path, 'w') as handle:
            handle.write(text)
        return config.load({'PANGOLOOP_CONFIG': path, **environ}, home)

    def test_prices_and_the_limit_from_the_file(self):
        cfg = self.load('[limits]\nmax_cost_usd = 2.5\n[prices]\n"big-model-5" = { input = 3, output = 15, '
                        'cache_read = 0.3, cache_write = 3.75 }\n')
        self.assertEqual(cfg.limits['max_cost_usd'], 2.5)
        self.assertEqual(cfg.prices['big-model-5']['output'], 15.0)
        text = cfg.describe()
        self.assertIn('limits.max_cost_usd', text)
        self.assertIn('prices.big-model-5', text)

    def test_the_limit_from_the_environment(self):
        self.assertIsNone(self.load().limits['max_cost_usd'])
        both = self.load('[limits]\nmax_cost_usd = 9\n', PANGOLOOP_MAX_COST_USD='2')
        self.assertEqual(both.limits['max_cost_usd'], 2.0)
        self.assertEqual(both.sources['limits.max_cost_usd'], 'env PANGOLOOP_MAX_COST_USD')
        self.assertEqual(self.load(PANGOLOOP_MAX_COST_USD='').limits['max_cost_usd'], None)

    def test_a_bad_limit_or_price_stops_with_a_message(self):
        for text in ('[limits]\nmax_cost_usd = -1\n', '[limits]\nmax_cost_usd = "lots"\n', '[prices]\nm = 4\n'):
            with self.assertRaises(SystemExit, msg=text):
                self.load(text)
        with self.assertRaises(SystemExit):
            self.load(PANGOLOOP_MAX_COST_USD='x')


def worker_with(usage_by_model):
    """A stub worker: model and usage from the node's id."""
    def behave(sg):
        class Stub(sg.Run):
            def worker(self, node_id, category, prompt, cwd=None):
                tag = self.next_tag(node_id)
                os.makedirs(f'{self.out}/workers/{tag}')   # the page lists the folders
                for name, text in (('prompt.txt', prompt), ('answer.md', 'done')):
                    with open(f'{self.out}/workers/{tag}/{name}', 'w') as handle:
                        handle.write(text)
                model, usage = usage_by_model[node_id]
                record = {'worker': tag, 'node': node_id, 'category': category, 'model': model, 'reported_model': None,
                          'started': 0, 'seconds': 1, 'exit': 0, 'limit_waits': 0, 'ok': True, 'usage': usage}
                record['cost_usd'], source = costs.worker_cost(record, sg.PRICES)
                if source:
                    record['cost_source'] = source
                self.records.append(dict(record))
                self.save_state()
                return dict(record, answer='VERDICT: PASS' if category == 'review' else 'done')
        return Stub
    return behave


MILLION = {'input': 1_000_000, 'output': 0, 'cache_read': 0, 'cache_write': 0}


class InTheEngine(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.repo = helpers.make_repo(self.home)

    def run_chain(self, usage_by_model, limit=None, prices=TABLE, nodes=('a', 'b', 'c')):
        sg = engine_stub.engine(self, self.home)
        sg.PRICES, sg.MAX_COST_USD = prices, limit
        plan = helpers.write_plan(self.home, *[helpers.node_text(n, deps=nodes[i - 1] if i else 'none') for i, n in enumerate(nodes)])
        run = worker_with(usage_by_model)(sg)(plan, self.repo, 'r')
        sg.CURRENT = run
        verdict = run.execute()
        sg.write_result(run, verdict, run.status)
        return run, verdict, helpers.read_json(f'{self.home}/runs/r/result.json')

    def test_the_cost_per_worker_and_per_run_and_partial_with_a_model_without_a_price(self):
        run, verdict, result = self.run_chain({'a': ('big-model-5', MILLION), 'b': ('other-model-1', MILLION),
                                               'c': ('big-model-5', MILLION)})
        self.assertEqual(verdict, 'verified')
        self.assertEqual([w['cost_usd'] for w in result['workers']], [3.0, None, 3.0])
        self.assertEqual(result['workers'][0]['cost_source'], 'price table')
        self.assertEqual((result['cost']['usd'], result['cost']['partial'], result['cost']['models_without_price']),
                         (6.0, True, ['other-model-1']))
        self.assertNotIn('reason', result)
        state = helpers.read_json(f'{self.home}/runs/r/state.json')
        self.assertEqual(state['cost']['usd'], 6.0, 'the page of a running run reads it from the state')

    def test_nothing_configured_is_as_before(self):
        run, verdict, result = self.run_chain({n: ('stub', {'input': 1, 'output': 1}) for n in 'abc'}, prices={})
        self.assertEqual(verdict, 'verified')
        self.assertEqual((result['cost']['usd'], result['cost']['partial'], result['cost']['limit_usd']), (None, True, None))
        self.assertEqual(result['cost']['models_without_price'], ['stub'])
        self.assertEqual(result['usage_totals']['cost_usd'], None)

    def test_over_the_limit_no_new_worker_starts(self):
        run, verdict, result = self.run_chain({n: ('big-model-5', MILLION) for n in 'abcd'}, limit=5.0,
                                              nodes=('a', 'b', 'c', 'd'))
        # a: known 0 -> runs (3); b: 3 <= 5 -> runs (6); c: 6 > 5 -> refused
        self.assertEqual(verdict, 'not_verified')
        self.assertEqual(run.status, {'a': 'accepted', 'b': 'accepted', 'c': 'failed', 'd': 'waiting'})
        self.assertEqual(len(result['workers']), 2)
        self.assertEqual(result['reason'], 'cost limit')
        self.assertEqual(result['node_reasons'], {'c': 'cost limit'})
        self.assertEqual((result['cost']['usd'], result['cost']['limit_usd']), (6.0, 5.0))

    def test_equal_to_the_limit_is_not_over(self):
        run, verdict, result = self.run_chain({n: ('big-model-5', MILLION) for n in 'ab'}, limit=3.0, nodes=('a', 'b'))
        self.assertEqual(verdict, 'verified')

    def test_a_cost_limit_with_no_known_cost_blocks_nothing(self):
        run, verdict, _ = self.run_chain({n: ('other-model-1', MILLION) for n in 'abc'}, limit=0.01)
        self.assertEqual(verdict, 'verified', 'unknown is not counted as money')

    def test_the_page_shows_the_cost_and_the_reason(self):
        self.run_chain({n: ('big-model-5', MILLION) for n in 'abcd'}, limit=5.0, nodes=('a', 'b', 'c', 'd'))
        saved = view.RUNS
        view.RUNS = f'{self.home}/runs'
        self.addCleanup(setattr, view, 'RUNS', saved)
        data = view.build_run('r')
        self.assertEqual(data['run_state'], 'finished: not verified (cost limit)')
        self.assertEqual(data['facts']['run'], 'not verified (cost limit)')
        self.assertEqual(data['reason'], 'cost limit')
        self.assertEqual(data['cost']['usd'], 6.0)
        self.assertEqual(sorted(w['cost_usd'] for w in data['workers']), [3.0, 3.0])
        node = next(n for n in data['nodes'] if n['id'] == 'c')
        self.assertEqual((node['state'], node['state_text']), ('failed', 'cost limit'))
        slot = data['by_model']['big-model-5']
        self.assertEqual((slot['cost'], slot['cost_unknown']), (6.0, 0))
        for text in ('cost of the run', 'stopped: cost limit', 'w/o price'):
            self.assertIn(text, view.PAGE if hasattr(view, 'PAGE') else open(view.__file__).read())


class AcrossSegments(unittest.TestCase):
    """The engine in a process of its own (tests/resume_driver.py: each stub worker reports $0.01)."""

    def test_exit_1_the_limit_counts_every_segment_of_a_resumed_run(self):
        scene = Scene(self, *chain(helpers.node_text('d', deps='c')), env={'PANGOLOOP_MAX_COST_USD': '0.015'})
        first = scene.run('r')
        self.assertEqual(first.code, 1, first.out + first.err)
        result = scene.result('r')
        self.assertEqual((result['verdict'], result['reason'], result['node_reasons']), ('not_verified', 'cost limit', {'c': 'cost limit'}))
        self.assertEqual([call['node'] for call in scene.calls()], ['a', 'b'])
        self.assertEqual(result['cost']['usd'], 0.02)
        second = scene.run('r', '--resume', env={'PANGOLOOP_MAX_COST_USD': '0.025'})
        self.assertEqual(second.code, 1, second.out + second.err)
        self.assertEqual([call['node'] for call in scene.calls()], ['a', 'b', 'c'], 'a and b were not run again; c was, d was refused')
        result = scene.result('r')
        self.assertEqual((result['reason'], result['node_reasons'], result['cost']['usd']), ('cost limit', {'d': 'cost limit'}, 0.03))
        self.assertEqual(result['resumes'], 1)
        third = scene.run('r', '--resume', env={'PANGOLOOP_MAX_COST_USD': '5'})
        self.assertEqual(third.code, 0, third.out + third.err)
        result = scene.result('r')
        self.assertEqual((result['verdict'], result['cost']['usd']), ('verified', 0.04))
        self.assertNotIn('reason', result)

    def test_the_limit_variable_stops_a_run(self):
        scene = Scene(self, *chain(), env={'PANGOLOOP_MAX_COST_USD': '0.005'})
        done = scene.run('r')
        self.assertEqual(done.code, 1, done.out + done.err)
        self.assertEqual(scene.result('r')['reason'], 'cost limit')

    def test_without_a_limit_it_is_as_before(self):
        scene = Scene(self, *chain())
        done = scene.run('r')
        self.assertEqual(done.code, 0, done.out + done.err)
        self.assertEqual(scene.result('r')['cost']['limit_usd'], None)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
