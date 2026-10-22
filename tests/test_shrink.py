import hermetic  # first: no test sees the real home
import os
import unittest
os.environ["HERDR_ENV"] = ""


class Shrink(unittest.TestCase):
    pass


class Command(unittest.TestCase):
    pass


class Args(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
