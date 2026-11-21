"""The project: finding it, `pangoloop init`, the folders, and the short form of `run`."""
import json
import os
import re
import subprocess
import sys
import tomllib
import unittest

import project_helpers as h
import support

from pangoloop import config, example_roles, initproject, project


class Finding(unittest.TestCase):
    def test_walks_up_to_the_first_pangoloop_toml(self):
        home = support.temp_dir(self)
        outer = os.path.join(home, 'outer')
        inner = os.path.join(outer, 'inner', 'deep')
        os.makedirs(inner)
        for folder in (outer, os.path.join(outer, 'inner')):
            open(os.path.join(folder, 'pangoloop.toml'), 'w').close()
        self.assertEqual(project.find(inner, home=home), os.path.join(outer, 'inner'))
        self.assertEqual(project.find(outer, home=home), outer)

    def test_stops_at_the_home_folder_and_never_takes_it_for_a_project(self):
        home = support.temp_dir(self)
        open(os.path.join(home, 'pangoloop.toml'), 'w').close()
        below = os.path.join(home, 'a', 'b')
        os.makedirs(below)
        self.assertIsNone(project.find(below, home=home))
        self.assertIsNone(project.find(home, home=home))

    def test_no_project_is_none(self):
        home = support.temp_dir(self)
        self.assertIsNone(project.find(home, home=home))

    def test_explicit_path_and_variable_win_over_the_search(self):
        home = support.temp_dir(self)
        named = os.path.join(home, 'named')
        here = os.path.join(home, 'here', 'sub')
        os.makedirs(named)
        os.makedirs(here)
        for folder in (named, os.path.join(home, 'here')):
            open(os.path.join(folder, 'pangoloop.toml'), 'w').close()
        self.assertEqual(project.resolve(named, {}, start=here), named)
        self.assertEqual(project.resolve(None, {'PANGOLOOP_PROJECT': named}, start=here), named)
        self.assertEqual(project.resolve(None, {}, start=here), os.path.join(home, 'here'))

    def test_a_named_folder_without_the_file_is_an_error_with_the_next_step(self):
        home = support.temp_dir(self)
        with self.assertRaises(SystemExit) as stop:
            project.resolve(home, {})
        self.assertIn('pangoloop init', stop.exception.code)

    def test_the_folders_are_the_defaults_or_the_configurations_or_the_variables(self):
        home = support.temp_dir(self)
        with open(os.path.join(home, 'pangoloop.toml'), 'w') as handle:
            handle.write('[paths]\nruns = "work/runs"\nroles = "/elsewhere/roles"\n')
        self.assertEqual(project.folder(home, 'runs', {}), os.path.join(home, 'work', 'runs'))
        self.assertEqual(project.folder(home, 'roles', {}), '/elsewhere/roles')
        self.assertEqual(project.folder(home, 'plans', {}), os.path.join(home, '.pangoloop', 'plans'))
        self.assertEqual(project.folder(home, 'runs', {'PANGOLOOP_RUNS': '/tmp/x'}), '/tmp/x')
        environ = {'PANGOLOOP_PLANS': '/mine'}
        project.apply(home, environ)
        self.assertEqual((environ['PANGOLOOP_PROJECT'], environ['PANGOLOOP_PLANS'], environ['PANGOLOOP_RUNS']),
                         (home, '/mine', os.path.join(home, 'work', 'runs')), 'an explicit variable is kept')

    def test_the_configuration_checks_paths_and_view(self):
        home = support.temp_dir(self)
        path = os.path.join(home, 'pangoloop.toml')
        for text, fragment in (('[paths]\nspam = "x"\n', 'paths'), ('[paths]\nruns = 3\n', 'paths.runs'),
                               ('[view]\nport = "high"\n', 'view.port'), ('[view]\nport = 99999\n', 'view.port'),
                               ('[view]\nhost = "0.0.0.0"\n', 'view')):
            with open(path, 'w') as handle:
                handle.write(text)
            with self.assertRaises(SystemExit, msg=text) as stop:
                config.load({'PANGOLOOP_CONFIG': path}, home)
            self.assertIn(fragment, stop.exception.code)
        with open(path, 'w') as handle:
            handle.write('[paths]\nruns = "r"\n[view]\nport = 5555\n')
        loaded = config.load({'PANGOLOOP_CONFIG': path}, home)
        self.assertEqual((loaded.paths, loaded.view_port), ({'runs': 'r'}, 5555))
        self.assertIn('view.port', loaded.describe())

    def test_the_project_file_is_read_from_the_project_root_in_a_subfolder(self):
        folder, env = h.make_project(self)
        sub = os.path.join(folder, 'sub')
        os.makedirs(sub)
        done = h.pangoloop('config', cwd=sub, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(os.path.join(os.path.realpath(folder), 'pangoloop.toml'), done.stdout)
        self.assertIn('routes.default', done.stdout)
        elsewhere = h.pangoloop('config', '--project', folder, cwd=support.temp_dir(self), env=env)
        self.assertIn('pangoloop.toml', elsewhere.stdout)
        missing = h.pangoloop('config', '--project', support.temp_dir(self), cwd=folder, env=env)
        self.assertEqual(missing.returncode, 2)
        self.assertIn('pangoloop init', missing.stderr)


class Init(unittest.TestCase):
    def folder(self, git: bool = True) -> str:
        home = support.temp_dir(self)
        if git:
            h.git(home, 'init', '-q', '-b', 'main')
        return home

    def init(self, folder: str, *args: str, **kwargs):
        return h.pangoloop('init', *args, cwd=folder, **kwargs)

    def test_makes_the_project_and_says_what_it_did(self):
        folder = self.folder()
        done = self.init(folder)
        self.assertEqual(done.returncode, 0, done.stderr)
        for name in ('runs', 'plans', 'roles'):
            self.assertTrue(os.path.isfile(os.path.join(folder, '.pangoloop', name, 'README.md')), name)
        self.assertEqual(sorted(f for f in os.listdir(os.path.join(folder, '.pangoloop', 'roles')) if f != 'README.md'),
                         sorted(f'{name}.md' for name in example_roles.ROLES))
        loaded = config.load({'PANGOLOOP_CONFIG': os.path.join(folder, 'pangoloop.toml')}, folder)
        self.assertEqual(loaded.runner, 'hermes', 'the default preset is one-model, with the placeholder routes')
        self.assertIn('pangoloop.toml', done.stdout)
        self.assertIn('next:', done.stdout)
        self.assertIn('pangoloop run <plan> --detach --view', done.stdout)
        self.assertIn('placeholders', done.stdout, 'the routes of the default preset are placeholders and it says so')

    def test_runs_are_ignored_by_git_and_plans_and_roles_are_not(self):
        folder = self.folder()
        self.assertEqual(self.init(folder).returncode, 0)
        with open(os.path.join(folder, '.gitignore')) as handle:
            self.assertEqual(handle.read(), '.pangoloop/runs/\n.pangoloop/view.json\n.pangoloop/view.log\n')
        status = h.git(folder, 'status', '--short', '--untracked-files=all')
        self.assertNotIn('.pangoloop/runs', status)
        self.assertIn('.pangoloop/plans/README.md', status)
        self.assertIn('.pangoloop/roles/coder.md', status)
        for name in ('plans', 'roles'):
            with open(os.path.join(folder, '.pangoloop', name, 'README.md')) as handle:
                self.assertIn('does NOT ignore', handle.read())

    def test_appends_to_an_existing_gitignore_once(self):
        folder = self.folder()
        with open(os.path.join(folder, '.gitignore'), 'w') as handle:
            handle.write('node_modules')      # no final newline
        self.init(folder)
        self.init(folder, '--force')
        with open(os.path.join(folder, '.gitignore')) as handle:
            self.assertEqual(handle.read(), 'node_modules\n.pangoloop/runs/\n.pangoloop/view.json\n.pangoloop/view.log\n')

    def test_an_existing_ignore_of_the_state_folder_is_enough(self):
        folder = self.folder()
        with open(os.path.join(folder, '.gitignore'), 'w') as handle:
            handle.write('.pangoloop/\n')
        done = self.init(folder)
        self.assertIn('already ignores', done.stdout)
        with open(os.path.join(folder, '.gitignore')) as handle:
            self.assertEqual(handle.read(), '.pangoloop/\n')

    def test_refuses_to_overwrite_without_force_and_changes_nothing(self):
        folder = self.folder()
        with open(os.path.join(folder, 'pangoloop.toml'), 'w') as handle:
            handle.write('# mine\n')
        done = self.init(folder)
        self.assertEqual(done.returncode, 2)
        self.assertIn('--force', done.stderr)
        with open(os.path.join(folder, 'pangoloop.toml')) as handle:
            self.assertEqual(handle.read(), '# mine\n')
        self.assertFalse(os.path.exists(os.path.join(folder, '.pangoloop')))
        self.assertFalse(os.path.exists(os.path.join(folder, '.gitignore')))

    def test_force_replaces_the_file_and_keeps_the_users_roles(self):
        folder = self.folder()
        self.init(folder)
        role = os.path.join(folder, '.pangoloop', 'roles', 'coder.md')
        with open(role, 'w') as handle:
            handle.write('---\nkind: build\n---\nmine\n')
        done = self.init(folder, '--force', '--preset', 'claude-code-only')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('over the old one', done.stdout)
        self.assertIn('left your roles as they are: coder', done.stdout)
        with open(role) as handle:
            self.assertIn('mine', handle.read())
        self.assertEqual(config.load({'PANGOLOOP_CONFIG': os.path.join(folder, 'pangoloop.toml')}, folder).runner, 'claude-code')

    def test_options_fill_the_configuration(self):
        folder = self.folder()
        done = self.init(folder, '--preset', 'one-model', '--runner', 'claude-code', '--route', 'default=opus',
                         '--route', 'review=fast@anthropic', '--route', 'docs=haiku', '--reader', 'only=haiku')
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(os.path.join(folder, 'pangoloop.toml'), 'rb') as handle:
            data = tomllib.load(handle)
        self.assertEqual(data['runner']['name'], 'claude-code')
        self.assertEqual(data['routes']['default'], 'opus')
        self.assertEqual(data['routes']['review'], 'fast@anthropic')
        self.assertEqual(data['routes']['docs'], 'haiku')
        self.assertEqual(data['routes']['ui'], 'default-model', 'the preset\'s other routes stay, for this runner')
        self.assertEqual(data['readers'], {'only': 'haiku'}, '--reader replaces the preset\'s readers')
        self.assertIn('the routes ui are still placeholders', done.stdout, 'default and review are set; ui is not')

    def test_hermes_routes_need_a_provider_and_a_bad_option_changes_nothing(self):
        folder = self.folder()
        done = self.init(folder, '--runner', 'hermes', '--route', 'default=just-a-model')
        self.assertEqual(done.returncode, 2)
        self.assertIn('nothing was changed', done.stderr)
        self.assertFalse(os.path.exists(os.path.join(folder, 'pangoloop.toml')))
        self.assertFalse(os.path.exists(os.path.join(folder, '.pangoloop')))
        self.assertEqual(self.init(folder, '--route', 'no equals sign').returncode, 2)
        self.assertEqual(self.init(folder, '--reader', '=x').returncode, 2)
        self.assertEqual(self.init(folder, '--preset', 'nonesuch').returncode, 2)

    def test_every_preset_is_a_valid_configuration_for_both_runners(self):
        for preset in initproject.PRESET_NAMES:
            for runner in ('claude-code', 'hermes'):
                folder = self.folder()
                routes = ['default=m@a', 'review=m@a', 'ui=m@a', 'simple-work=m@a'] if runner == 'hermes' else []
                readers = ['q=m@a'] if runner == 'hermes' else []
                args = ['--preset', preset, '--runner', runner] + [x for r in routes for x in ('--route', r)] \
                    + [x for r in readers for x in ('--reader', r)]
                done = self.init(folder, *args)
                self.assertEqual(done.returncode, 0, (preset, runner, done.stderr))
                loaded = config.load({'PANGOLOOP_CONFIG': os.path.join(folder, 'pangoloop.toml')}, folder)
                self.assertEqual(loaded.runner, runner)

    def test_a_folder_that_is_not_a_repository_works_and_says_a_run_needs_one(self):
        folder = self.folder(git=False)
        done = self.init(folder)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('not a git repository', done.stdout)
        self.assertTrue(os.path.isfile(os.path.join(folder, 'pangoloop.toml')))
        self.assertFalse(os.path.exists(os.path.join(folder, '.git')), 'init never runs `git init`')

    def test_a_repository_without_a_commit_and_a_subfolder_are_noted(self):
        folder = self.folder()
        self.assertIn('no commit yet', self.init(folder).stdout)
        sub = os.path.join(self.folder(), 'app')
        os.makedirs(sub)
        self.assertIn('inside the git repository', self.init(sub).stdout)

    def test_a_named_folder_is_made_and_nothing_outside_it_changes(self):
        home = support.temp_dir(self)
        before = set(os.listdir(home))
        done = h.pangoloop('init', os.path.join(home, 'new', 'project'), cwd=home)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(set(os.listdir(home)) - before, {'new'})
        self.assertEqual(sorted(os.listdir(os.path.join(home, 'new'))), ['project'])

    def test_json_form(self):
        folder = self.folder()
        done = self.init(folder, '--json')
        data = json.loads(done.stdout)
        self.assertEqual(sorted(data), ['actions', 'folder', 'next', 'notes'])
        self.assertEqual(data['folder'], os.path.realpath(folder))


class ShortRun(unittest.TestCase):
    def test_run_with_a_bare_plan_name_runs_in_the_project_and_prints_the_name(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        done = h.pangoloop('run', 'checks', cwd=folder, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        name = re.match(r'run: (checks-\d{8}-\d{6})\n', done.stdout)
        self.assertTrue(name, done.stdout)
        run = os.path.join(folder, '.pangoloop', 'runs', name.group(1))
        result = h.read_json(os.path.join(run, 'result.json'))
        self.assertEqual(result['verdict'], 'verified')
        state = h.read_json(os.path.join(run, 'state.json'))
        self.assertEqual(state['repo'], os.path.realpath(folder), 'the repository is the project\'s root')
        self.assertTrue(state['node_worktrees'], 'the short form sets SG_NODE_WORKTREES')
        self.assertIn('verdict=verified', done.stdout)

    def test_a_plan_can_be_run_again_in_the_project_and_the_old_node_branch_is_kept(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        for name in ('first', 'second', 'third'):
            done = h.pangoloop('run', 'checks', '--name', name, cwd=folder, env=env)
            self.assertEqual(done.returncode, 0, name + done.stdout + done.stderr)
        branches = h.git(folder, 'branch', '--list', 'sg/*').split()
        self.assertEqual(sorted(branches), ['sg/one', 'sg/one-earlier-1', 'sg/one-earlier-2'])

    def test_run_from_a_subfolder_with_a_file_name_and_a_name_option(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        sub = os.path.join(folder, 'sub')
        os.makedirs(sub)
        done = h.pangoloop('run', '../.pangoloop/plans/checks.md', '--name', 'mine', cwd=sub, env=env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(os.path.isfile(os.path.join(folder, '.pangoloop', 'runs', 'mine', 'result.json')))

    def test_two_runs_in_one_second_get_different_names(self):
        from pangoloop import cli
        runs = support.temp_dir(self)
        first = cli.run_name('/x/checks.md', runs, now=1700000000)
        os.makedirs(os.path.join(runs, first))
        self.assertEqual(cli.run_name('/x/checks.md', runs, now=1700000000), first + '-2')
        self.assertTrue(re.fullmatch(r'my-plan-\d{8}-\d{6}', cli.run_name('/x/my plan.md', support.temp_dir(self))))

    def test_an_unknown_plan_lists_the_plans_folder(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        done = h.pangoloop('run', 'nonesuch', cwd=folder, env=env)
        self.assertEqual(done.returncode, 2)
        self.assertIn('checks.md', done.stderr)
        self.assertIn('.pangoloop/plans', done.stderr)

    def test_the_short_form_needs_a_project(self):
        done = h.pangoloop('run', 'plan.md', cwd=support.temp_dir(self))
        self.assertEqual(done.returncode, 2)
        self.assertIn('pangoloop init', done.stderr)

    def test_the_full_form_still_works_exactly(self):
        folder, env = h.make_project(self)
        plan = h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        runs = support.temp_dir(self)
        done = h.pangoloop('run', plan, folder, 'full-1', cwd=support.temp_dir(self), env=dict(env, PANGOLOOP_RUNS=runs, PANGOLOOP_CONFIG=os.path.join(folder, 'pangoloop.toml')))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(os.path.isfile(os.path.join(runs, 'full-1', 'result.json')))
        state = h.read_json(os.path.join(runs, 'full-1', 'state.json'))
        self.assertFalse(state['node_worktrees'], 'the full form does not turn the switches on')

    def test_a_run_writes_only_under_the_project(self):
        folder, env = h.make_project(self)
        h.write_plan(folder, 'checks.md', h.SCRIPT_PLAN)
        tmp = support.temp_dir(self)
        before = set(os.listdir(os.path.dirname(folder)))
        done = h.pangoloop('run', 'checks', cwd=folder, env=dict(env, TMPDIR=tmp))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(os.listdir(tmp), [], 'nothing was written to the temporary folder')
        self.assertEqual(set(os.listdir(os.path.dirname(folder))), before, 'nothing beside the project folder')
        self.assertEqual(sorted(os.listdir(os.path.join(folder, '.pangoloop'))), ['plans', 'roles', 'runs'])


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
