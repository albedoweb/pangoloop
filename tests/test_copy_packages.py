"""The copy of installed packages into a node's copy (`graph.copy_packages`) works with the `cp` of the system the suite
runs on, and falls back to a plain copy when `cp` refuses the cheap one. Added November 24, 2026, after the first run of
the suite on Linux: the engine used `cp -Rc`, which only the macOS `cp` knows."""
import os
import unittest
from unittest import mock

import engine_stub  # noqa: F401  (puts the package on the path the way the other tests do)
import support
from pangoloop import graph


class CopyPackages(unittest.TestCase):
    def setUp(self):
        self.home = support.temp_dir(self)
        self.source = os.path.join(self.home, 'node_modules')
        os.makedirs(os.path.join(self.source, 'pkg', 'lib'))
        with open(os.path.join(self.source, 'pkg', 'lib', 'index.js'), 'w') as handle:
            handle.write('module.exports = 1\n')
        os.makedirs(os.path.join(self.source, '.bin'))
        os.symlink('../pkg/lib/index.js', os.path.join(self.source, '.bin', 'pkg'))

    def copied(self, target):
        with open(os.path.join(target, 'pkg', 'lib', 'index.js')) as handle:
            self.assertEqual(handle.read(), 'module.exports = 1\n')
        self.assertTrue(os.path.islink(os.path.join(target, '.bin', 'pkg')), 'a link stays a link')
        self.assertEqual(os.readlink(os.path.join(target, '.bin', 'pkg')), '../pkg/lib/index.js')

    def test_with_the_cp_of_this_system(self):
        target = os.path.join(self.home, 'copy', 'node_modules')
        os.makedirs(os.path.dirname(target))
        graph.copy_packages(self.source, target)
        self.copied(target)

    def test_as_on_a_system_that_is_not_macos(self):
        # On macOS the first command is refused (its cp has no --reflink) and the plain copy does the work; on Linux the
        # first command succeeds. Either way the folder is copied.
        target = os.path.join(self.home, 'copy', 'node_modules')
        os.makedirs(os.path.dirname(target))
        with mock.patch.object(graph.sys, 'platform', 'linux'):
            graph.copy_packages(self.source, target)
        self.copied(target)


if __name__ == '__main__':
    unittest.main()
