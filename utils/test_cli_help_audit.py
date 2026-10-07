"""Behavior checks for the experimental parser/help contract checker."""
import unittest

from cli_help_audit import audit, extract_options


SOURCE = '''static int parseOptions(int argc, char **argv) {
    if (!strcmp(argv[i], "--visible")) {}
    else if (!strcmp(argv[i], "--new")) {}
    return i;
}'''


class HelpAuditTests(unittest.TestCase):
    def test_new_parser_option_missing_from_help_is_reported(self):
        result = audit(SOURCE, "  --visible   Visible\n", "", {}, {})
        self.assertEqual(result["missing_options"], ["--new"])

    def test_help_option_not_accepted_by_parser_is_reported(self):
        result = audit(SOURCE, "  --visible x\n  --new x\n  --typo x\n", "", {}, {})
        self.assertEqual(result["unrecognized_help_options"], ["--typo"])

    def test_prose_mention_does_not_document_an_option(self):
        result = audit(SOURCE, "  --visible  See --new for details\n", "", {}, {})
        self.assertEqual(result["missing_options"], ["--new"])

    def test_alias_and_internal_exceptions_are_explicit(self):
        exceptions = {"--new": {"kind": "alias", "canonical": "--visible", "reason": "Legacy name"}}
        result = audit(SOURCE, "  --visible x\n", "", exceptions, {})
        self.assertEqual(result["missing_options"], [])
        self.assertEqual(result["exception_errors"], [])

    def test_stale_exception_is_an_error(self):
        result = audit(SOURCE, "", "", {"--removed": {"kind": "internal", "reason": "test"}}, {})
        self.assertTrue(result["exception_errors"])

    def test_option_in_wrong_cluster_section_does_not_satisfy_contract(self):
        source = SOURCE.replace("--new", "--cluster-new")
        help_text = "  reshard        host\n                 --cluster-new\n  rebalance      host\n"
        result = audit(source, "  --visible x\n", help_text, {}, {"--cluster-new": ["reshard", "rebalance"]})
        self.assertEqual(result["missing_options"], [])
        self.assertEqual(result["missing_command_entries"], [{"command": "rebalance", "option": "--cluster-new"}])

    def test_unknown_parser_shape_fails_instead_of_reporting_zero_options(self):
        with self.assertRaises(ValueError):
            extract_options("void changed_parser(void) {}")

    def test_new_literal_with_unhandled_parser_style_fails(self):
        source = SOURCE.replace('!strcmp(argv[i], "--new")', 'accept(argv[i], "--new")')
        with self.assertRaises(ValueError):
            extract_options(source)


if __name__ == "__main__":
    unittest.main()
