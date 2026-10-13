import unittest


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = [2, 3]

    def test_sum(self):
        self.assertEqual(sum(self.fixture), 5)

    @unittest.skip('example optional scenario')
    def test_optional(self):
        pass

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(1, 2)
