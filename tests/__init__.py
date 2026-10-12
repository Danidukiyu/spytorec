# SpytoRec test suite placeholder
import unittest

if not hasattr(unittest.TestCase, 'enterContext'):
    def _enterContext(self, cm):
        val = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return val
    unittest.TestCase.enterContext = _enterContext
