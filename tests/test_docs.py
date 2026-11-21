"""The documents of the project commands: the JSON contracts are written down, the README's picture and the paths it names exist, and
nothing in the public documents says what it should not. The documents are read where this tree has them: in the public tree
`README.md`, `docs/SETUP.md` and `prompts/STAGE_AUTHOR.md`; in a development checkout they are kept apart from the tree they are exported to."""
import os
import re
import struct
import unittest

import support


def document(name: str) -> str:
    for base in (os.path.join('scripts', 'public'), ''):
        path = os.path.join(support.ROOT, base, name)
        if os.path.isfile(path):
            with open(path) as handle:
                return handle.read()
    raise unittest.SkipTest(f'{name} is not in this tree')


def exists(path: str) -> bool:
    return os.path.exists(os.path.join(support.ROOT, path))


class Contracts(unittest.TestCase):
    def test_every_field_of_the_json_forms_is_in_the_setup_guide(self):
        text = document('docs/SETUP.md')
        fields = ['project', 'page', 'runs_folder', 'runs', 'name', 'state', 'verdict', 'started', 'ended', 'waiting_for',
                  'open_requests', 'folder', 'plan', 'repo', 'branch', 'runner', 'reviews', 'cost', 'nodes', 'requests', 'id',
                  'kind', 'role', 'label', 'depends_on', 'workers', 'node', 'for', 'question', 'asked_at', 'worker_kind',
                  'answered_at', 'answered_by', 'worker', 'run_over', 'log', 'pid', 'page_run', 'usd', 'partial']
        for field in fields:
            self.assertRegex(text, rf'["`]{field}["`]', f'`{field}` is not written in the guide')
        for state in ('running', 'verified', 'not verified', 'aborted', 'stopped', 'waiting for human', 'integrating'):
            self.assertIn(state, text)

    def test_the_guide_has_the_project_the_page_the_roles_and_the_no_login_warning(self):
        text = document('docs/SETUP.md')
        for heading in ('## A project', '### Detached runs', '### Several projects at once', '## The page', '## Kinds and roles',
                        '## Reading a run from a program'):
            self.assertIn(heading, text)
        self.assertIn('has no login', text.lower().replace('**it has no login.**', 'has no login'))
        for role in ('coder', 'test-writer', 'interface-builder', 'researcher', 'reviewer', 'security-reviewer', 'simplicity-reviewer'):
            self.assertIn(f'`{role}`', text)


class Readme(unittest.TestCase):
    def test_the_readme_leads_with_the_flow_a_newcomer_wants(self):
        text = document('README.md')
        order = ['![', '## With your agent', '## By hand', "## The project's layout", '## Plans, kinds and roles', '## The page',
                 '## Status and limits', '## Where to read more', '## Licence']
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(text.index('# Pangoloop'), text.index('!['))
        agent = text[text.index('## With your agent'):text.index('## By hand')]
        for phrase in ('pipx install', 'pangoloop version', 'skills/pangoloop/', '.claude/skills/', 'claude plugin marketplace add',
                       'hermes skills trust', 'Use the pangoloop skill', 'uncommitted change', 'commits nothing to your branch'):
            self.assertIn(phrase, agent)
        by_hand = text[text.index('## By hand'):text.index("## The project's layout")]
        for command in ('pangoloop init', 'cp my-plan.md .pangoloop/plans/', 'pangoloop run my-plan --detach --view',
                        'pangoloop status', 'pangoloop requests list', 'sh examples/demo/run.sh'):
            self.assertIn(command, by_hand)
        self.assertIn('docs/img/view.png', text)
        self.assertIn('no login', text)
        for role in ('coder', 'test-writer', 'interface-builder', 'researcher', 'reviewer', 'security-reviewer', 'simplicity-reviewer'):
            self.assertIn(f'`{role}`', text)
        self.assertIn('not a sandbox', text)

    def test_the_readme_has_a_short_opening_and_names_no_file_the_tree_lacks(self):
        text = document('README.md')
        opening = text[text.index('# Pangoloop'):text.index('![')]
        self.assertLessEqual(len(re.findall(r'[.!?](?:\s|$)', opening)), 4, 'three sentences, with a heading')

    def test_the_picture_is_there_1440_wide_and_under_400_kilobytes(self):
        path = os.path.join(support.ROOT, 'docs', 'img', 'view.png')
        self.assertTrue(os.path.isfile(path))
        self.assertLess(os.path.getsize(path), 400 * 1024)
        with open(path, 'rb') as handle:
            head = handle.read(24)
        self.assertEqual(head[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>I', head[16:20])[0], 1440)

    def test_the_paths_the_readme_names_exist(self):
        text = document('README.md')
        for path in set(re.findall(r'`((?:examples|docs|prompts|skills)/[A-Za-z0-9_./-]+)`', text)):
            if path.endswith('/'):
                self.assertTrue(exists(path.rstrip('/')), path)
            elif path in ('docs/SETUP.md', 'prompts/STAGE_AUTHOR.md'):
                document(path)
            else:
                self.assertTrue(exists(path), path)

    def test_the_toy_stand_in_and_files_the_readme_uses_are_there(self):
        for path in ('examples/demo/run.sh', 'examples/demo/plan.md', 'examples/demo/files/textstats.py',
                     'examples/demo/files/test_textstats.py', 'examples/toy/stub-claude', 'examples/toy/files/calc.py', 'examples/toy/files/test_calc.py', 'examples/toy/plan.md'):
            self.assertTrue(exists(path), path)
        self.assertTrue(os.access(os.path.join(support.ROOT, 'examples/toy/stub-claude'), os.X_OK))


class Wording(unittest.TestCase):
    def test_the_public_documents_and_examples_keep_clear_of_the_old_name_and_of_model_names(self):
        texts = {name: document(name) for name in ('README.md', 'docs/SETUP.md', 'prompts/STAGE_AUTHOR.md')}
        for folder in ('examples/roles', 'examples/toy', 'examples/demo', 'skills'):
            for root, _, files in os.walk(os.path.join(support.ROOT, folder)):
                for name in files:
                    with open(os.path.join(root, name)) as handle:
                        texts[os.path.join(root, name)] = handle.read()
        for name, text in texts.items():
            self.assertNotIn('pango' + 'lin', text.lower(), name)
            for word in ('sonnet', 'opus', 'haiku', 'gpt-', 'gemini', 'openai'):
                self.assertNotIn(word, text.lower(), f'{name}: {word}')


if __name__ == '__main__':
    unittest.main()
