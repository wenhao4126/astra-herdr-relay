import importlib.util, pathlib, tempfile, unittest
P=pathlib.Path(__file__).parents[1]/"lib"/"relay.py"
spec=importlib.util.spec_from_file_location("relay",P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class RelayContractTests(unittest.TestCase):
    def test_prob_rejects_bool(self):
        with self.assertRaises(RuntimeError):
            m.prob(True,"x")

    def test_roles_frozen(self):
        self.assertEqual(m.ALLOWED_ROLES,{"worker","fixer","reviewer","tester","auditor"})

    def test_deterministic_semantic(self):
        self.assertEqual(m.deterministic_semantic("done"),"COMPLETED")
        self.assertEqual(m.deterministic_semantic("blocked"),"BLOCKED")
        self.assertIsNone(m.deterministic_semantic("working"))
