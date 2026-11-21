"""pyproject.toml: the name, the one version, the console command, and that every package of the folder is listed (a package
left out of the list would be missing from an installed copy and nobody would notice from the repository)."""
import importlib
import os
import tomllib
import unittest

import support

import pangoloop


def pyproject() -> dict:
    with open(os.path.join(support.ROOT, 'pyproject.toml'), 'rb') as handle:
        return tomllib.load(handle)


class Packaging(unittest.TestCase):
    def test_name_version_and_python(self):
        project = pyproject()['project']
        self.assertEqual(project['name'], 'pangoloop')
        self.assertEqual(project['requires-python'], '>=3.11')
        self.assertEqual(project['dependencies'], [])
        self.assertIn('version', project['dynamic'], 'the version is kept in one place, pangoloop/__init__.py')
        self.assertNotIn('version', project)
        self.assertEqual(pyproject()['tool']['setuptools']['dynamic']['version'], {'attr': 'pangoloop.__version__'})
        self.assertRegex(pangoloop.__version__, r'^\d+\.\d+\.\d+$')

    def test_console_command_points_to_the_command_line(self):
        target = pyproject()['project']['scripts']['pangoloop']
        module, _, function = target.partition(':')
        self.assertIs(getattr(importlib.import_module(module), function), importlib.import_module('pangoloop.cli').main)

    def test_every_package_of_the_folder_is_listed(self):
        listed = set(pyproject()['tool']['setuptools']['packages'])
        found = set()
        for folder, _, files in os.walk(os.path.join(support.ROOT, 'pangoloop')):
            if '__init__.py' in files:
                found.add(os.path.relpath(folder, support.ROOT).replace(os.sep, '.'))
        self.assertEqual(listed, found)


if __name__ == '__main__':
    unittest.main()
