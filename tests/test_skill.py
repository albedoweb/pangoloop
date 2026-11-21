"""The skill that lets an agent drive the tool (`skills/pangoloop/`) and the files that package it for Claude Code: its shape, that the
pages it points to are there, that the example plan in its references is a plan the engine accepts, that the rules it condenses are
the stage author's own words, and that every command it names exists."""
import json
import os
import re
import subprocess
import sys
import unittest

import support
from test_docs import document

import pangoloop
from pangoloop import plan, roles

SKILL = os.path.join(support.ROOT, 'skills', 'pangoloop')
REFERENCES = ('plan-format.md', 'cutting-work.md', 'roles.md', 'requests-and-rulings.md', 'commands.md')


def read(*parts: str) -> str:
    with open(os.path.join(SKILL, *parts)) as handle:
        return handle.read()


def skill_text() -> str:
    return '\n'.join(read(name) for name in ['SKILL.md'] + [os.path.join('references', r) for r in REFERENCES])


def front_matter(text: str) -> tuple[dict, str]:
    found = re.match(r'---\n(.*?)\n---\n(.*)\Z', text, re.S)
    assert found, 'SKILL.md must begin with a header between --- lines'
    fields = dict(line.split(': ', 1) for line in found.group(1).splitlines() if ': ' in line)
    return fields, found.group(2)


class Shape(unittest.TestCase):
    def test_the_header_names_the_skill_and_says_when_to_use_it(self):
        fields, body = front_matter(read('SKILL.md'))
        self.assertEqual(fields['name'], 'pangoloop')
        self.assertLessEqual(len(fields['description']), 1024)
        for phrase in ('graph', 'parallel', 'review', '"pangoloop"', 'Do not use'):
            self.assertIn(phrase, fields['description'])
        self.assertLessEqual(len(body.strip().splitlines()), 150, 'the body is a page, the details are in references/')

    def test_the_references_are_there_one_page_each_and_the_body_points_to_them(self):
        body = read('SKILL.md')
        for name in REFERENCES:
            self.assertTrue(os.path.isfile(os.path.join(SKILL, 'references', name)), name)
            self.assertLess(len(read('references', name).splitlines()), 200, f'{name} is one page')
        for named in set(re.findall(r'references/([A-Za-z0-9_.-]+\.md)', skill_text())):
            self.assertIn(named, REFERENCES, f'{named} is named but is not a page of the skill')
        for name in ('plan-format.md', 'cutting-work.md', 'roles.md', 'requests-and-rulings.md', 'commands.md'):
            self.assertIn(f'references/{name}', body)

    def test_the_flow_has_its_nine_steps_in_order_and_the_things_it_must_say_plainly(self):
        body = read('SKILL.md')
        positions = [body.index(f'**{n}. ') for n in range(1, 10)]
        self.assertEqual(positions, sorted(positions))
        flat = ' '.join(body.split())
        for phrase in ('pangoloop version', 'pangoloop init', 'pangoloop preflight', '--detach --view --json', 'requests wait',
                       'pangoloop status <run> --json', 'pangoloop resume', 'You never push, merge or delete branches', 'has no login',
                       'seat belt, not a sandbox', 'one uncommitted change', 'every field of a node is exactly one line'):
            self.assertIn(phrase, flat)

    def test_no_model_or_lab_name_and_no_old_name(self):
        text = skill_text().lower()
        for word in ('sonnet', 'opus', 'haiku', 'gpt-', 'gemini', 'openai', 'pango' + 'lin', 'reference' + '.md'):
            self.assertNotIn(word, text, word)


class Commands(unittest.TestCase):
    def test_every_command_the_skill_names_is_a_command(self):
        known = {'version', 'init', 'config', 'roles', 'preflight', 'run', 'status', 'requests', 'resume', 'cleanup', 'view', 'steps'}
        for command in set(re.findall(r'`pangoloop ([a-z]+)', skill_text())):
            self.assertIn(command, known, command)
        for sub in set(re.findall(r'`pangoloop requests ([a-z]+)', skill_text())):
            self.assertIn(sub, {'list', 'wait', 'answer', 'escalate', 'rulings', 'rule'})
        for sub in set(re.findall(r'^pangoloop requests ([a-z]+)', skill_text(), re.M)):
            self.assertIn(sub, {'list', 'wait', 'answer', 'escalate', 'rulings', 'rule'})

    def test_every_flag_the_skill_names_is_in_the_help_of_its_command(self):
        def help_of(*words: str) -> str:
            done = subprocess.run([sys.executable, '-m', 'pangoloop', *words, '--help'], capture_output=True, text=True,
                                  env=support.clean_environment(), cwd=support.temp_dir(self))
            return done.stdout + done.stderr
        init, run, preflight, resume = help_of('init'), help_of('run'), help_of('preflight'), help_of('resume')
        for flag in ('--preset', '--runner', '--route', '--reader', '--force', '--json'):
            self.assertIn(flag, init, flag)
        for flag in ('--detach', '--view', '--json', '--name'):
            self.assertIn(flag, run, flag)
        for flag in ('--strict', '--reader', '--out'):
            self.assertIn(flag, preflight, flag)
        for flag in ('--reset-rounds', '--accept-plan-changes', '--plan'):
            self.assertIn(flag, resume, flag)
        requests = help_of('requests')
        for sub in ('wait', 'answer', 'escalate', 'rulings', 'rule', 'list'):
            self.assertIn(sub, requests)
        answer = help_of('requests', 'answer')
        for flag in ('--text', '--file', '--abort', '--by'):
            self.assertIn(flag, answer, flag)


class ThePlanExample(unittest.TestCase):
    def example(self) -> str:
        text = read('references', 'plan-format.md')
        block = text[text.index('## One example of each kind'):]
        return re.search(r'```\n(.*?)\n```', block, re.S).group(1)

    def test_the_example_nodes_are_a_plan_the_engine_accepts(self):
        full = '# Plan: an example\n\nBackground.\n\n## Nodes\n\n' + self.example() + '\n\n## Task statement\n\nR1. Something.\n'
        found = roles.load(os.path.join(support.ROOT, 'examples', 'roles'))
        nodes = plan.parse_nodes(full, strict=True, roles=found)
        self.assertEqual([n['kind'] for n in nodes], ['build', 'baseline', 'script', 'acceptance', 'review', 'human'])
        self.assertEqual([n['role'] for n in nodes], ['coder', 'coder', '', '', 'reviewer', ''])
        self.assertTrue(all(len(line) < 1200 for line in full.splitlines()))
        self.assertEqual(sorted(n['id'] for n in nodes if not n['deps']), ['add-parser', 'survey'])

    def test_the_demonstration_plan_is_too(self):
        with open(os.path.join(support.ROOT, 'examples', 'demo', 'plan.md')) as handle:
            text = handle.read()
        found = roles.load(os.path.join(support.ROOT, 'examples', 'roles'))
        nodes = plan.parse_nodes(text, strict=True, roles=found)
        self.assertEqual([(n['id'], n['kind'], n['role']) for n in nodes],
                         [('parser', 'build', 'coder'), ('tests', 'build', 'test-writer'), ('check', 'script', ''),
                          ('accept', 'acceptance', ''), ('review', 'review', 'reviewer')])


class OneSourceOfEachRule(unittest.TestCase):
    def test_the_rules_the_skill_condenses_are_the_stage_authors_own_headings(self):
        author = document('prompts/STAGE_AUTHOR.md')
        headings = re.findall(r'^- \*\*(.+?)\*\*', read('references', 'cutting-work.md'), re.M)
        self.assertGreater(len(headings), 20)
        for heading in headings:
            self.assertIn(heading.rstrip('.:'), author, f'"{heading}" is in the skill and not in the stage author\'s prompt')

    def test_the_two_agree_on_where_plans_live_and_on_the_closed_lists(self):
        author, setup = document('prompts/STAGE_AUTHOR.md'), document('docs/SETUP.md')
        for text in (author, skill_text()):
            self.assertIn('.pangoloop/plans/', text)
            self.assertIn('NEEDS COORDINATOR', text)
            self.assertIn('NEEDS HUMAN', text)
            self.assertIn('1,200', text)
        for kind in ('baseline', 'build', 'script', 'acceptance', 'review', 'human'):
            self.assertIn(f'`{kind}`', read('references', 'roles.md'))
            self.assertIn(f'`{kind}`', author)
            self.assertIn(f'`{kind}`', setup)
        for role in ('coder', 'test-writer', 'interface-builder', 'researcher', 'reviewer', 'security-reviewer', 'simplicity-reviewer'):
            for text in (read('references', 'roles.md'), author, setup):
                self.assertIn(f'`{role}`', text)
        for text in (author, read('references', 'cutting-work.md')):
            self.assertIn('twice', text)
        self.assertIn('skills/pangoloop/', setup)


class Packaging(unittest.TestCase):
    def load(self, name: str) -> dict:
        with open(os.path.join(support.ROOT, '.claude-plugin', name)) as handle:
            return json.load(handle)

    def test_the_plugin_and_the_marketplace_point_at_the_skill(self):
        plugin, market = self.load('plugin.json'), self.load('marketplace.json')
        self.assertEqual(plugin['name'], 'pangoloop')
        self.assertEqual(plugin['version'], pangoloop.__version__)
        self.assertTrue(os.path.isfile(os.path.join(support.ROOT, plugin['skills'], 'pangoloop', 'SKILL.md')))
        self.assertEqual(market['plugins'][0]['name'], plugin['name'])
        self.assertEqual(market['plugins'][0]['source'], './')
        self.assertEqual(market['plugins'][0]['version'], plugin['version'])
        self.assertTrue(market['owner']['name'])
        fields, _ = front_matter(read('SKILL.md'))
        self.assertEqual(fields['version'], pangoloop.__version__)


def setUpModule():
    support.quiet_resource_warnings()


if __name__ == '__main__':
    unittest.main()
