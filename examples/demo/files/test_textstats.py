import unittest

from textstats import count_words


class TextStatsTest(unittest.TestCase):
    def test_count_words(self):
        self.assertEqual(count_words('one two  three'), 3)


if __name__ == '__main__':
    unittest.main()
