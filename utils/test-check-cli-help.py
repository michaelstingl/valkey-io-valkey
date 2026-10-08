# Copyright (c) Valkey Contributors
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

"""Tests for option extraction and help entries."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


CHECKER_PATH = Path(__file__).with_name("check-cli-help.py")
CHECKER = runpy.run_path(str(CHECKER_PATH))
check_help = CHECKER["check_help"]
extract_option_groups = CHECKER["extract_option_groups"]


def source(condition, body=""):
    return ('static int parseOptions(int argc, char **argv) {\n'
            'for (i = 1; i < argc; i++) {\n'
            f'if ({condition}) {{ {body} }}\n'
            '} return i; }')


class CliHelpTests(unittest.TestCase):
    def check(self, condition, general="", cluster="", body="", internal=frozenset()):
        return check_help(source(condition, body), general, cluster, internal)

    def test_new_option_requires_help(self):
        errors = self.check('!strcmp(argv[i], "--new")')
        self.assertEqual(errors, ["Missing help for option group: --new"])

    def test_new_alias_needs_no_exception(self):
        for condition in [
            '!strcmp(argv[i], "--new") || !strcmp(argv[i], "--alias")',
            '((!strcmp(argv[i], "--new") || !strcmp(argv[i], "--alias"))) && (!lastarg)',
            '!strcmp(argv[i], "--new") || '
            '(!strcmp(argv[i], "--alias") || !strcmp(argv[i], "-n"))',
        ]:
            with self.subTest(condition=condition):
                self.assertEqual(
                    self.check(condition, "  --new <arg>  Description",
                               body='use(argv[++i]);'), [])

    def test_missing_alias_group_is_one_finding(self):
        errors = self.check('!strcmp(argv[i], "--new") || !strcmp(argv[i], "--alias")')
        self.assertEqual(errors, ["Missing help for option group: --alias / --new"])

    def test_unknown_help_spelling_is_reported(self):
        errors = self.check('!strcmp(argv[i], "--new")',
                            "  --new  Valid\n  --typo  Invalid")
        self.assertEqual(errors, ["Unknown option in help: --typo"])

    def test_prose_does_not_count_as_a_help_entry(self):
        self.assertTrue(self.check('!strcmp(argv[i], "--new")', "See --new for details"))

    def test_cluster_option_needs_cluster_help(self):
        condition = '!strcmp(argv[i], "--cluster-new")'
        self.assertTrue(self.check(condition, general="  --cluster-new  Description"))
        self.assertEqual(
            self.check(condition, cluster="  reshard host\n    --cluster-new"), [])

    def test_internal_exceptions_must_be_current_and_hidden(self):
        condition = '!strcmp(argv[i], "--internal")'
        self.assertEqual(self.check(condition, internal={"--internal"}), [])
        self.assertIn("Internal option no longer found in parseOptions: --removed",
                      self.check(condition, internal={"--removed"}))
        self.assertTrue(
            self.check(condition, "  --internal Description", internal={"--internal"}))

    def test_context_dependent_h_does_not_hide_missing_help(self):
        text = '''static int parseOptions(int argc, char **argv) {
            if (!strcmp(argv[i], "-h") && !lastarg) { host = argv[++i]; }
            else if (!strcmp(argv[i], "-h") && lastarg) { usage(0); }
            else if (!strcmp(argv[i], "--help")) { usage(0); }
        }'''
        self.assertEqual(check_help(text, "  --help Description", "", set()),
                         ["Missing help for option group: -h"])

    def test_separate_identical_bodies_are_not_assumed_to_be_aliases(self):
        text = '''static int parseOptions(int argc, char **argv) {
            if (!strcmp(argv[i], "--old")) { mode = 1; }
            else if (!strcmp(argv[i], "--new")) { mode = 1; }
        }'''
        self.assertEqual(check_help(text, "  --new Description", "", set()),
                         ["Missing help for option group: --old"])

    def test_unsupported_conditions_fail_explicitly(self):
        for condition in [
            '(!strcmp(argv[i], "--new") && !lastarg) || '
            '(!strcmp(argv[i], "--alias") && lastarg)',
            'accept(argv[i], "--new")',
            '!strcmp(argv[i], "--new") && enabled',
        ]:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                self.check(condition)

    def test_argument_guard_must_apply_to_all_aliases(self):
        for guard in ("lastarg", "!lastarg"):
            for condition in (
                f'!strcmp(argv[i], "--new") || !strcmp(argv[i], "--alias") && {guard}',
                f'!strcmp(argv[i], "--new") || (!strcmp(argv[i], "--alias") && {guard})',
            ):
                with self.subTest(condition=condition):
                    with self.assertRaisesRegex(
                        ValueError, "Unsupported option condition"
                    ):
                        self.check(condition, "  --new Description")
            condition = (
                f'(!strcmp(argv[i], "--new") || !strcmp(argv[i], "--alias")) && {guard}'
            )
            self.assertEqual(self.check(condition, "  --new Description"), [])

    def test_alias_branch_cannot_distinguish_spellings(self):
        with self.assertRaises(ValueError):
            self.check('!strcmp(argv[i], "--new") || !strcmp(argv[i], "--old")',
                       body='if (argv[i][2] == \'n\') mode = 1;')

    def test_overlapping_alias_groups_fail_instead_of_hiding_a_case(self):
        text = '''static int parseOptions(int argc, char **argv) {
            if (!strcmp(argv[i], "-h") && !lastarg) { host = argv[++i]; }
            else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) { usage(0); }
        }'''
        with self.assertRaises(ValueError):
            extract_option_groups(text)

    def test_nested_option_comparison_is_not_a_new_dispatch_branch(self):
        with self.assertRaises(ValueError):
            self.check('!strcmp(argv[i], "--new")',
                       body='if (!strcmp(argv[i], "--other")) { mode = 1; }')

    def test_comments_and_quoted_braces_do_not_change_discovery(self):
        self.assertEqual(
            self.check('!strcmp(/* "--fake" */ argv[i], "--new")',
                       "  --new Description", body='puts("https://host/{text}");'), [])

    def test_unknown_function_or_unhandled_literal_fails(self):
        for text in [
            'void changed(void) {}',
            source('!strcmp(argv[i], "--new")', 'other("--unknown");'),
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_option_groups(text)


class CliHelpCommandTests(unittest.TestCase):
    def run_checker(self, responses):
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "cli.c"
            source_path.write_text('''static int parseOptions(int argc, char **argv) {
                if (!strcmp(argv[i], "--new")) {}
                else if (!strcmp(argv[i], "--test_hint")) {}
                else if (!strcmp(argv[i], "--test_hint_file")) {}
            }''')
            argv = [str(CHECKER_PATH), "--source", str(source_path),
                    "--binary", str(Path(directory) / "valkey-cli")]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), \
                    redirect_stderr(stderr), \
                    patch("subprocess.run", side_effect=responses), \
                    self.assertRaises(SystemExit) as result:
                runpy.run_path(str(CHECKER_PATH), run_name="__main__")
        return result.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_help_results_determine_exit_status(self):
        for general, expected in (("  --new Description", 0), ("Usage: valkey-cli", 1)):
            with self.subTest(expected=expected):
                code, stdout, stderr = self.run_checker([
                    subprocess.CompletedProcess(["--help"], 0, general, ""),
                    subprocess.CompletedProcess(["--cluster", "help"], 1,
                                                "Cluster Manager Commands:", ""),
                ])
                self.assertEqual(code, expected)
                self.assertEqual(stderr, "")
                if expected == 1:
                    self.assertIn("Missing help", stdout)

    def test_failed_help_commands_are_not_coverage_results(self):
        for flags, expected in ((["--help"], 0), (["--cluster", "help"], 1)):
            for failure in (
                OSError("Cannot execute valkey-cli"),
                subprocess.TimeoutExpired(flags, 10),
                subprocess.CompletedProcess(flags, 1 - expected, "  --new", ""),
                subprocess.CompletedProcess(flags, expected, "  --new", "error"),
                subprocess.CompletedProcess(flags, expected, " \n", ""),
            ):
                with self.subTest(flags=flags, failure=failure):
                    responses = [] if flags == ["--help"] else [
                        subprocess.CompletedProcess(["--help"], 0, "  --new", "")]
                    code, stdout, stderr = self.run_checker(responses + [failure])
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn("CLI help check incomplete:", stderr)


if __name__ == "__main__":
    unittest.main()
