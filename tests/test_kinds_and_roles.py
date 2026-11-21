"""Kinds and roles: a node's `kind` is what the engine does, its `role` is who the worker is (a file in the roles folder); the
example roles; where a role's prompt goes; plans written before kinds."""
import json
import os
import re
import subprocess
import sys
import unittest

import project_helpers as h
import helpers
import support
from test_runs_and_pages import start_view

from pangoloop import example_roles, plan, roles

CODER = '---\nkind: build\ncategory: ui\ndescription: Writes code.\n---\nYou are the coder in {{REPO}} on {{BRANCH}}.\n'
JUDGE = '---\nkind: review\ndescription: Judges.\n---\nYou judge.\n'
BOTH = '---\nkind: build, review\n---\nYou do either.\n'


def role_set(folder: str, **files: str) -> roles.RoleSet:
    os.makedirs(folder, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(folder, name.replace('_', '-') + '.md'), 'w') as handle:
            handle.write(text)
    return roles.load(folder)


def one_node(**fields) -> str:
    """A plan with one node; `fields` are the lines of the node (None leaves out)."""
    base = {'label': 'n', 'depends_on': 'none', 'write_scope': 'x', 'TASK': 't', 'DELIVERABLE': 'd', 'SCOPE': 's',
            'VERIFY': 'v', 'STOP WHEN': 'w'}
    base.update({k.replace('_', ' ') if k == 'STOP_WHEN' else k: v for k, v in fields.items()})
    return '# P\n\n## Nodes\n\n### n\n' + ''.join(f'- {k}: {v}\n' for k, v in base.items() if v is not None) + '\n## Task statement\n\nx\n'


class RoleFiles(unittest.TestCase):
    def test_a_role_file_has_a_header_and_a_prompt(self):
        found = roles.parse_role('coder', CODER, 'coder.md')
        self.assertEqual((found.kinds, found.category, found.description), (('build',), 'ui', 'Writes code.'))
        self.assertEqual(found.prompt, 'You are the coder in {{REPO}} on {{BRANCH}}.')
        both = roles.parse_role('x', BOTH, 'x.md')
        self.assertEqual((both.kinds, both.category, both.description), (('build', 'review'), None, ''))
        self.assertTrue(both.allows('build') and both.allows('review') and both.allows('baseline'))
        self.assertFalse(roles.parse_role('j', JUDGE, 'j.md').allows('build'))

    def test_faults_name_what_is_wrong(self):
        for text, fragment in (('no header\n', 'must begin with a header'), ('---\nkind: build\nno end\n', 'not closed'),
                               ('---\ndescription: x\n---\nbody\n', 'no `kind:`'), ('---\nkind: script\n---\nbody\n', 'kind'),
                               ('---\nkind: build\nkind: review\n---\nb\n', 'twice'), ('---\nkind: build\ntools: Bash\n---\nb\n', 'tools'),
                               ('---\nkind: build\n---\n\n', 'no prompt'), ('---\nkind: build\ncategory: a b\n---\nb\n', 'category')):
            with self.assertRaises(roles.RoleFault, msg=text) as fault:
                roles.parse_role('r', text, 'r.md')
            self.assertIn(fragment, str(fault.exception), text)

    def test_the_folder_is_read_and_a_bad_file_stops_only_a_plan_that_names_it(self):
        folder = support.temp_dir(self)
        found = role_set(folder, coder=CODER, judge=JUDGE, broken='nothing here\n')
        with open(os.path.join(folder, 'README.md'), 'w') as handle:
            handle.write('# not a role\n')
        with open(os.path.join(folder, 'notes.txt'), 'w') as handle:
            handle.write('not a role either\n')
        found = roles.load(folder)
        self.assertEqual(sorted(found), ['coder', 'judge'])
        self.assertEqual(sorted(found.problems), ['broken'])
        self.assertEqual(roles.load(os.path.join(folder, 'missing')), {})
        plan.parse_nodes(one_node(kind='build', role='coder'), roles=found)
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(one_node(kind='build', role='broken'), roles=found)
        self.assertIn('broken.md exists but cannot be used', stop.exception.code)

    def test_the_example_roles_are_shipped_valid_and_as_the_spec_says(self):
        folder = os.path.join(support.ROOT, 'examples', 'roles')
        found = roles.load(folder)
        self.assertEqual(found.problems, {})
        self.assertEqual(sorted(found), sorted(['coder', 'test-writer', 'interface-builder', 'researcher', 'reviewer',
                                                'security-reviewer', 'simplicity-reviewer']))
        for name in ('coder', 'test-writer', 'interface-builder', 'researcher'):
            self.assertEqual(found[name].kinds, ('build',), name)
        for name in ('reviewer', 'security-reviewer', 'simplicity-reviewer'):
            self.assertEqual(found[name].kinds, ('review',), name)
            self.assertEqual(found[name].category, 'review')
        self.assertEqual(found['interface-builder'].category, 'ui')
        self.assertIn('document', found['researcher'].prompt)
        for name, role in found.items():
            with open(role.path) as handle:
                lines = handle.read().splitlines()
            self.assertTrue(10 <= len(lines) <= 22, f'{name}: {len(lines)} lines')
            self.assertTrue(role.description, name)
            self.assertIn('NEEDS COORDINATOR:', role.prompt, name)
            self.assertIn('NEEDS HUMAN:', role.prompt, name)
            self.assertNotIn('pango' + 'lin', role.prompt.lower())

    def test_review_prompts_leave_the_verdict_to_the_engine(self):
        for name in ('reviewer', 'security-reviewer', 'simplicity-reviewer'):
            prompt = roles.load(os.path.join(support.ROOT, 'examples', 'roles'))[name].prompt
            self.assertNotIn('VERDICT', prompt, 'a role cannot break what the engine reads: the engine adds the contract')

    def test_the_module_init_copies_matches_the_files(self):
        folder = os.path.join(support.ROOT, 'examples', 'roles')
        self.assertEqual(sorted(example_roles.ROLES), sorted(n[:-3] for n in os.listdir(folder) if n.endswith('.md')))
        for name, text in example_roles.ROLES.items():
            with open(os.path.join(folder, name + '.md')) as handle:
                self.assertEqual(handle.read(), text, f'{name}: run python3 scripts/gen_example_roles.py')


class KindsAndRoles(unittest.TestCase):
    def setUp(self):
        self.found = role_set(support.temp_dir(self) + '/roles', coder=CODER, judge=JUDGE, both=BOTH)

    def node(self, **fields):
        return plan.parse_nodes(one_node(**fields), roles=self.found)[0]

    def test_a_node_has_a_kind_and_a_role(self):
        node = self.node(kind='build', role='coder', category=None)
        self.assertEqual((node['kind'], node['role'], node['legacy']), ('build', 'coder', False))
        self.assertEqual((node['category'], node['route_category']), ('ui', 'ui'), 'the role\'s category')
        self.assertIn('{{REPO}}', node['role_prompt'])
        self.assertEqual(node['role_description'], 'Writes code.')

    def test_the_nodes_own_category_wins_and_a_default_fills_the_gap(self):
        self.assertEqual(self.node(kind='build', role='coder', category='deep')['category'], 'deep')
        self.assertEqual(self.node(kind='build', role='both', category=None)['category'], 'default')
        self.assertEqual(self.node(kind='review', role='both', category=None)['category'], 'review')
        self.assertEqual(self.node(kind='build', category=None)['role'], '', 'a kind alone is a node with no named role')

    def test_a_node_with_a_kind_may_leave_out_the_role_and_the_category(self):
        node = self.node(kind='build', category=None)
        self.assertEqual((node['kind'], node['role'], node['category'], node['role_prompt']), ('build', '', 'default', ''))

    def test_plans_written_before_kinds_read_as_before(self):
        for word in plan.KINDS:
            extra = {'STEPS': 'run `true`'} if word == 'script' else {}
            node = self.node(role=word, category='deep', **extra)
            self.assertEqual((node['kind'], node['role'], node['legacy'], node['role_prompt']), (word, word, True, ''), word)
        self.assertEqual(self.node(role='review', category='deep')['route_category'], 'review', 'a legacy review ignored its category')
        self.assertEqual(self.node(role='build', category='deep')['route_category'], 'deep')
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(one_node(role='build', category=None), roles=self.found)
        self.assertIn("['category']", stop.exception.code)
        with self.assertRaises(SystemExit) as stop:
            plan.parse_nodes(one_node(role=None, category='deep'), roles=self.found)
        self.assertIn("['role']", stop.exception.code)

    def test_a_role_word_that_is_no_kind_and_has_no_file_stays_a_plain_session(self):
        node = self.node(role='verify', category='deep')
        self.assertEqual((node['kind'], node['legacy'], node['other_word']), ('baseline', True, True))

    def test_a_role_with_one_kind_may_stand_alone_one_with_two_must_say_which(self):
        node = self.node(role='judge', category=None)
        self.assertEqual((node['kind'], node['role'], node['legacy']), ('review', 'judge', False))
        with self.assertRaises(SystemExit) as stop:
            self.node(role='both')
        self.assertIn('kinds build, review', stop.exception.code)

    def test_an_unknown_role_names_the_folder_and_lists_the_roles(self):
        with self.assertRaises(SystemExit) as stop:
            self.node(kind='build', role='nonesuch')
        message = stop.exception.code
        self.assertIn("unknown role 'nonesuch'", message)
        self.assertIn(self.found.folder, message)
        self.assertIn('both, coder, judge', message)

    def test_a_kind_without_a_worker_has_no_role(self):
        for kind, extra in (('script', {'STEPS': 'run `true`'}), ('acceptance', {}), ('human', {})):
            with self.assertRaises(SystemExit, msg=kind) as stop:
                self.node(kind=kind, role='coder', **extra)
            self.assertIn('has no model worker', stop.exception.code)
            self.assertEqual(self.node(kind=kind, **extra)['kind'], kind)

    def test_a_role_must_allow_the_kind(self):
        for kind, role in (('review', 'coder'), ('build', 'judge'), ('baseline', 'judge')):
            with self.assertRaises(SystemExit, msg=(kind, role)) as stop:
                self.node(kind=kind, role=role)
            self.assertIn('is for kind', stop.exception.code)
        self.assertEqual(self.node(kind='baseline', role='coder')['kind'], 'baseline')

    def test_a_wrong_kind_word_is_a_plan_error(self):
        with self.assertRaises(SystemExit) as stop:
            self.node(kind='coding')
        self.assertIn('baseline, build, script, acceptance, review, human', stop.exception.code)

    def test_without_a_roles_set_a_named_role_is_accepted_unchecked(self):
        node = plan.parse_nodes(one_node(kind='build', role='whatever'))[0]
        self.assertEqual((node['kind'], node['role'], node['role_prompt']), ('build', 'whatever', ''))

    def test_helpers_that_read_a_node_by_role_alone_still_work(self):
        self.assertTrue(plan.must_change({'role': 'build', 'write_scope': 'x'}))
        self.assertTrue(plan.must_change({'kind': 'build', 'role': 'coder', 'write_scope': 'x'}))
        self.assertFalse(plan.must_change({'kind': 'review', 'role': 'build', 'write_scope': 'x'}))
        self.assertEqual(plan.kind_of({'role': 'review'}), 'review')

    def test_placeholder_routes_follow_the_route_category(self):
        nodes = [{'id': 'a', 'kind': 'review', 'route_category': 'review', 'category': 'review'}]
        self.assertEqual(plan.placeholder_routes(nodes, {'review': ('review-model', 'p')}, ('m', 'p')), ['review'])


class InThePrompt(unittest.TestCase):
    """The role's prompt in the worker's prompt, through an engine run with the stand-in of tests/resume_driver.py."""

    def run_plan(self, plan_text: str, roles_files: dict | None = None, extra_env: dict | None = None):
        home = support.temp_dir(self)
        repo = helpers.make_repo(home)
        roles_folder = os.path.join(home, 'roles')
        role_set(roles_folder, **(roles_files or {}))
        plan_file = os.path.join(home, 'plan.md')
        with open(plan_file, 'w') as handle:
            handle.write(plan_text)
        env = support.clean_environment(STUB_DIR=home, PANGOLOOP_RUNS=os.path.join(home, 'runs'), PANGOLOOP_ROLES=roles_folder,
                                        SG_NODE_WORKTREES='1', **(extra_env or {}))
        done = subprocess.run([sys.executable, os.path.join(support.ROOT, 'tests', 'resume_driver.py'), plan_file, repo, 'r1'],
                              capture_output=True, text=True, env=env, cwd=home, timeout=120)
        calls = []
        if os.path.exists(os.path.join(home, 'calls.jsonl')):
            calls = [json.loads(line) for line in open(os.path.join(home, 'calls.jsonl'))]
        return done, calls, home

    PLAN = '''# P

Repository: `{{REPO}}`, branch `{{BRANCH}}`.

## Nodes

### make
- label: Make it
- kind: build
- role: coder
- depends_on: none
- write_scope: x
- TASK: do make
- DELIVERABLE: d
- SCOPE: s
- VERIFY: v
- STOP WHEN: w

### look
- label: Review it
- kind: review
- role: judge
- depends_on: make
- write_scope: nothing
- TASK: Review the change.
- DELIVERABLE: findings
- SCOPE: read only
- VERIFY: none
- STOP WHEN: done

### old
- label: A review the old way
- category: review
- role: review
- depends_on: look
- write_scope: nothing
- TASK: Review it again, ending with VERDICT.
- DELIVERABLE: findings
- SCOPE: read only
- VERIFY: none
- STOP WHEN: done

## Task statement

x
'''

    def test_the_role_goes_after_the_node_line_and_before_the_contract(self):
        done, calls, home = self.run_plan(self.PLAN, dict(coder=CODER, judge=JUDGE))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        make = next(call for call in calls if call['node'] == 'make')['prompt']
        order = [make.index(text) for text in ('Your node is `make`', 'Your role: coder (Writes code.).', 'You are the coder in',
                                              'TASK: do make')]
        self.assertEqual(order, sorted(order))
        self.assertIn('wt/make', make.split('You are the coder in ')[1].split(' on ')[0],
                      '{{REPO}} is the folder the node works in: its own copy')
        self.assertIn('on sg/make.', make, '{{BRANCH}} is the node\'s branch')
        self.assertNotIn('{{', make.split('TASK:')[0])
        self.assertNotIn('How to close your review', make, 'only a review gets the engine\'s review paragraph')

    def test_a_review_gets_the_engines_verdict_paragraph_after_its_role(self):
        done, calls, _ = self.run_plan(self.PLAN, dict(coder=CODER, judge=JUDGE))
        look = next(call for call in calls if call['node'] == 'look')['prompt']
        self.assertEqual(look.count('How to close your review'), 1)
        self.assertLess(look.index('You judge.'), look.index('How to close your review'))
        self.assertLess(look.index('How to close your review'), look.index('TASK: Review the change.'))
        for needle in ('VERDICT: PASS', 'VERDICT: CHANGES REQUESTED', 'FINDING 1:'):
            self.assertIn(needle, look)
        self.assertEqual(roles.REVIEW_CONTRACT in look, True)
        self.assertEqual(next(call for call in calls if call['node'] == 'look')['action'], 'pass', 'the review route was `review`')

    def test_a_plan_of_the_old_form_gets_the_prompt_it_always_got(self):
        done, calls, _ = self.run_plan(self.PLAN, dict(coder=CODER, judge=JUDGE))
        old = next(call for call in calls if call['node'] == 'old')['prompt']
        self.assertNotIn('Your role', old)
        self.assertNotIn('How to close your review', old)
        self.assertEqual(old.splitlines()[2], 'Your node is `old`: A review the old way. Do only this node.')
        self.assertEqual(old.splitlines()[4], 'TASK: Review it again, ending with VERDICT.')

    def test_an_unknown_role_stops_before_anything_starts(self):
        done, calls, home = self.run_plan(self.PLAN, dict(coder=CODER))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("unknown role 'judge'", done.stderr)
        self.assertIn(os.path.join(home, 'roles'), done.stderr)
        self.assertIn('coder', done.stderr)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(os.path.join(home, 'runs', 'r1')))


class Commands(unittest.TestCase):
    def test_roles_lists_the_projects_roles_and_show_prints_one(self):
        folder, env = h.make_project(self)
        listed = h.pangoloop('roles', cwd=folder, env=env)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(os.path.join(os.path.realpath(folder), '.pangoloop', 'roles'), listed.stdout)
        for name in example_roles.ROLES:
            self.assertRegex(listed.stdout, rf'(?m)^  {re.escape(name)}\s')
        self.assertRegex(listed.stdout, r'(?m)^  reviewer\s+review\s+review\s+Reads the whole change')
        self.assertIn('coder.md', listed.stdout)
        data = json.loads(h.pangoloop('roles', '--json', cwd=folder, env=env).stdout)
        self.assertEqual(sorted(data), ['folder', 'problems', 'roles'])
        self.assertEqual(sorted(data['roles'][0]), ['category', 'description', 'file', 'kinds', 'name'])
        shown = h.pangoloop('roles', 'show', 'researcher', cwd=folder, env=env)
        self.assertEqual(shown.returncode, 0)
        self.assertIn('kind: build', shown.stdout)
        self.assertIn('document', shown.stdout)
        missing = h.pangoloop('roles', 'show', 'nonesuch', cwd=folder, env=env)
        self.assertEqual(missing.returncode, 2)
        self.assertIn('coder', missing.stderr)

    def test_a_role_the_user_adds_is_listed_and_usable(self):
        folder, env = h.make_project(self)
        with open(os.path.join(folder, '.pangoloop', 'roles', 'doc-writer.md'), 'w') as handle:
            handle.write('---\nkind: build\ndescription: Writes the docs.\n---\nYou write documentation.\n')
        self.assertRegex(h.pangoloop('roles', cwd=folder, env=env).stdout, r'(?m)^  doc-writer\s+build\s')
        with open(os.path.join(folder, '.pangoloop', 'roles', 'bad.md'), 'w') as handle:
            handle.write('no header\n')
        self.assertIn('NOT USABLE', h.pangoloop('roles', cwd=folder, env=env).stdout)

    def test_the_page_shows_the_role_beside_the_model(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'roles.md', InThePrompt.PLAN.replace('judge', 'reviewer'))
        done = h.pangoloop('run', 'roles', '--name', 'r1', cwd=folder, env=env, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        process, line = start_view(self, folder, env)
        data = json.loads(h.fetch(line + 'api/run/r1')[1])
        nodes = {n['id']: n for n in data['nodes']}
        self.assertEqual((nodes['make']['kind'], nodes['make']['role']), ('build', 'coder'))
        self.assertEqual((nodes['look']['kind'], nodes['look']['role'], nodes['look']['color_role']), ('review', 'reviewer', 'reviewer'))
        self.assertEqual((nodes['old']['kind'], nodes['old']['role']), ('review', 'review'))
        workers = {w['node']: w for w in data['workers']}
        self.assertEqual((workers['make']['role_name'], workers['make']['role']), ('coder', 'coder'))
        self.assertEqual((workers['look']['role_name'], workers['look']['role']), ('reviewer', 'reviewer'))
        self.assertEqual(workers['old']['role_name'], '', 'a plan of the old form shows no role name')
        page = h.fetch(line)[1]
        self.assertIn('role_name', page)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
