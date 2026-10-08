#!/usr/bin/env python3
# Copyright (c) Valkey Contributors
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

"""Check valkey-cli option names against its help output.

Run `make check-cli-help` after a Make build to reuse its build settings.
Aliases joined by || in one condition in valkey-cli's parseOptions() need one
help entry per group. Runs valkey-cli --help and valkey-cli --cluster help.
Checks option names in help, not argument counts, descriptions or placement
under individual Cluster Manager commands. Recognizes literal comparisons
with strcmp(argv[i], ...); unsupported conditions fail the check.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


# Internal options for testing CLI hints.
INTERNAL_OPTIONS = {"--test_hint", "--test_hint_file"}
# Keep comments and quoted literals whole: punctuation inside them is not C syntax.
# In quoted literals, consume a backslash and its following character together.
TOKEN = re.compile(r"""
    (?P<block_comment>     /\* .*? \*/ )
  | (?P<line_comment>      // [^\n]* )
  | (?P<string_literal>    " (?: \\ . | [^"\\] )* " )
  | (?P<character_literal> ' (?: \\ . | [^'\\] )* ' )
  | (?P<identifier>        [A-Za-z_] [A-Za-z_0-9]* )
  | (?P<boolean_operator>  && | \|\| )
  | (?P<other>             \S )
""", re.VERBOSE | re.DOTALL)

OPTION_NAME = re.compile(r"""
    -{1,2}              # One or two leading dashes.
    [A-Za-z0-9_]         # Digits include short options such as -2 and -3.
    [A-Za-z0-9_-]*       # Remaining characters, as in --cluster-primary-id.
""", re.VERBOSE)


def option_literal(token):
    """Return the name in a C string such as "--mono", otherwise None."""
    if token.startswith('"') and token.endswith('"'):
        name = token[1:-1]
        if OPTION_NAME.fullmatch(name):
            return name
    return None


def help_options(text):
    """Return option names at the start of indented help entries."""
    options = set()
    for line in text.splitlines():
        words = line.split()
        if words and line[0].isspace() and OPTION_NAME.fullmatch(words[0]):
            options.add(words[0])
    return options


def find_closing_token(tokens, start):
    """Return the index of the matching closing parenthesis or brace."""
    opening, closing_token = tokens[start], {"(": ")", "{": "}"}[tokens[start]]
    depth = 0
    for end in range(start, len(tokens)):
        if tokens[end] == opening:
            depth += 1
        elif tokens[end] == closing_token:
            depth -= 1
            if depth == 0:
                return end
    raise ValueError("Unmatched parentheses or braces in C source")


def strip_outer_parentheses(tokens):
    """Remove parentheses that enclose the entire expression."""
    while (tokens and tokens[0] == "("
           and find_closing_token(tokens, 0) == len(tokens) - 1):
        tokens = tokens[1:-1]
    return tokens


def split_condition(tokens, operator):
    """Split at operators outside parentheses."""
    parts, start, depth = [], 0, 0
    for i, token in enumerate(tokens):
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif token == operator and depth == 0:
            parts.append(tokens[start:i])
            start = i + 1
    return parts + [tokens[start:]]


def option_group(tokens):
    """Recognize equality tests joined by OR, without per-alias conditions."""
    tokens = strip_outer_parentheses(tokens)
    alternatives = split_condition(tokens, "||")
    if len(alternatives) > 1:
        return frozenset().union(*(option_group(part) for part in alternatives))
    # A single option test has the form !strcmp(argv[i], "--option").
    prefix = ["!", "strcmp", "(", "argv", "[", "i", "]", ","]
    if (len(tokens) == len(prefix) + 2
            and tokens[:len(prefix)] == prefix and tokens[-1] == ")"):
        name = option_literal(tokens[-2])
        if name:
            return frozenset([name])
    raise ValueError("Unsupported option condition: " + " ".join(tokens))


def extract_option_groups(source):
    """Read options and shared aliases from valkey-cli's parseOptions()."""
    tokens = [match[0] for match in TOKEN.finditer(source)
              if match.lastgroup not in ("block_comment", "line_comment")]
    # Match the definition, not a forward declaration or a name inside a string.
    signature = ["static", "int", "parseOptions", "(", "int", "argc", ",",
                 "char", "*", "*", "argv", ")", "{"]
    definitions = [i for i, token in enumerate(tokens)
                   if token == "static" and tokens[i:i + len(signature)] == signature]
    if len(definitions) != 1:
        raise ValueError("parseOptions signature not recognized or ambiguous")
    start = definitions[0] + len(signature) - 1
    tokens = tokens[start + 1:find_closing_token(tokens, start)]
    groups, depth, dispatch_depth = set(), 0, None
    for i, token in enumerate(tokens):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        if token != "if" or tokens[i + 1:i + 2] != ["("]:
            continue
        end = find_closing_token(tokens, i + 1)
        condition = tokens[i + 2:end]
        if not any(option_literal(token) for token in condition):
            continue
        if dispatch_depth is None:
            dispatch_depth = depth
        if depth != dispatch_depth:
            raise ValueError("Option comparison outside the main dispatch chain")
        condition = strip_outer_parentheses(condition)
        parts = split_condition(condition, "&&")
        # In A || B && guard, the guard applies only to B.
        if (len(parts) == 2
                and len(split_condition(condition, "||")) == 1
                and strip_outer_parentheses(parts[1]) in (["lastarg"], ["!", "lastarg"])):
            condition = parts[0]
        group = option_group(condition)
        if tokens[end + 1:end + 2] != ["{"]:
            raise ValueError("Option branch without braces")
        branch = tokens[end + 2:find_closing_token(tokens, end + 1)]
        if len(group) > 1 and any(
            branch[j:j + 4] == ["argv", "[", "i", "]"] for j in range(len(branch))
        ):
            raise ValueError(
                "Alias branch inspects argv[i] again: " + ", ".join(sorted(group)))
        if len({name.startswith("--cluster-") for name in group}) != 1:
            raise ValueError(
                "Alias group contains both general and Cluster Manager options")
        if any(group != previous and group & previous for previous in groups):
            raise ValueError("Option appears in conflicting alias groups")
        groups.add(group)
    parsed = set().union(*groups)
    literals = {name for token in tokens if (name := option_literal(token))}
    if not parsed or literals - parsed:
        raise ValueError("Unhandled option literals: " + repr(sorted(literals - parsed)))
    return sorted(groups, key=lambda group: sorted(group))


def check_help(source, general_help, cluster_help, internal=INTERNAL_OPTIONS):
    groups = extract_option_groups(source)
    parsed = set().union(*groups)
    general = help_options(general_help)
    cluster = help_options(cluster_help)
    errors = []
    for name in sorted(internal):
        if name not in parsed:
            errors.append("Internal option no longer found in parseOptions: " + name)
        elif name in general | cluster:
            errors.append("Internal option now has help; remove its exception: " + name)
    for group in groups:
        public = group - internal
        help_entries = cluster if next(iter(group)).startswith("--cluster-") else general
        if public and not public & help_entries:
            errors.append("Missing help for option group: " + " / ".join(sorted(public)))
    for name in sorted((general | cluster) - parsed):
        errors.append("Unknown option in help: " + name)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="C source preprocessed with the CLI build settings")
    parser.add_argument("--binary", type=Path, required=True,
                        help="matching valkey-cli executable")
    args = parser.parse_args()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VALKEY", "REDIS"))}
    env.update(LC_ALL="C", TERM="dumb")
    outputs = []
    for flags, expected in [(["--help"], 0), (["--cluster", "help"], 1)]:
        result = subprocess.run([str(args.binary.resolve()), *flags], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=10, env=env)
        if result.returncode != expected or result.stderr or not result.stdout.strip():
            raise ValueError(
                f"Unexpected result from valkey-cli {' '.join(flags)}: "
                f"exit={result.returncode}, stderr={result.stderr!r}")
        outputs.append(result.stdout)
    errors = check_help(args.source.read_text(), *outputs)
    for error in errors:
        print(error)
    if not errors:
        print("CLI option groups are covered by help.")
    return int(bool(errors))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"CLI help check incomplete: {error}", file=sys.stderr)
        sys.exit(2)
