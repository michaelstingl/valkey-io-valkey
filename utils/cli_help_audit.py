#!/usr/bin/env python3
"""Experimental CLI help audit, not an upstream-ready general C parser.

Compare compiled parseOptions string comparisons against actual help headings.
Only --help and --cluster help are executed; no server or credentials are needed.
Input must be preprocessed using the same build settings as the binary.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


STRING = r'"(?:\\.|[^"\\])*"'
TOKEN = re.compile(r'/\*.*?\*/|//[^\n]*|' + STRING + r"|'(?:\\.|[^'\\])*'|[{}]", re.S)
FLAG = re.compile(r'-{1,2}[A-Za-z0-9_][A-Za-z0-9_-]*\Z')
COMPARISON = re.compile(r'strcmp\s*\(\s*argv\s*\[\s*i\s*\]\s*,\s*"(-{1,2}[\w-]+)"\s*\)')
HEADING = re.compile(r'^\s+(-{1,2}[\w-]+)(?:\s|$)')


def extract_options(source):
    start = re.search(r'static\s+int\s+parseOptions\s*\(int\s+argc,\s*char\s*\*\*argv\)\s*\{', source)
    if not start:
        raise ValueError("parseOptions signature not recognized; audit coverage is unknown")
    depth, end = 1, None
    for match in TOKEN.finditer(source, start.end()):
        if match[0] == "{":
            depth += 1
        elif match[0] == "}":
            depth -= 1
            if depth == 0:
                end = match.start()
                break
    if end is None:
        raise ValueError("parseOptions boundary not recognized")
    body = source[start.end():end]
    body = re.sub(r'/\*.*?\*/|//[^\n]*', '', body, flags=re.S)
    options = set(COMPARISON.findall(body))
    literals = {m[0][1:-1] for m in re.finditer(STRING, body) if FLAG.fullmatch(m[0][1:-1])}
    if not options or literals - options:
        raise ValueError("Unhandled parser option literals: " + repr(sorted(literals - options)))
    return options


def help_options(text):
    return {m[1] for line in text.splitlines() if (m := HEADING.match(line))}


def audit(source, general_help, cluster_help, exceptions, required_sections):
    parsed = extract_options(source)
    general = help_options(general_help)
    cluster = help_options(cluster_help)
    listed = general | cluster
    errors = []
    for option, rule in exceptions.items():
        if option not in parsed:
            errors.append(f"Stale exception: {option} is not compiled into this parser")
        if not rule.get("reason") or rule.get("kind") not in ("alias", "internal"):
            errors.append(f"Invalid exception: {option}")
        if rule.get("kind") == "alias" and rule.get("canonical") not in parsed:
            errors.append(f"Alias has no accepted canonical option: {option}")
        if option in listed:
            errors.append(f"Unnecessary exception: {option} now has help")
    sections = {}
    current = None
    for line in cluster_help.splitlines():
        match = re.match(r'^  ([a-z][a-z-]*)\s+', line)
        if match:
            current = match[1]
            sections[current] = set()
        elif current and (match := HEADING.match(line)):
            sections[current].add(match[1])
    missing_sections = []
    for option, commands in required_sections.items():
        if option not in parsed:
            errors.append(f"Stale command contract: {option}")
        for command in commands:
            if option not in sections.get(command, set()):
                missing_sections.append({"command": command, "option": option})
    # Require cluster flags on the cluster help surface, other flags on --help.
    missing = {o for o in parsed - exceptions.keys()
               if o not in (cluster if o.startswith("--cluster-") else general)}
    return {
        "parsed_option_count": len(parsed),
        "listed_option_count": len(listed),
        "raw_missing_options": sorted(parsed - listed),
        "exceptions": exceptions,
        "missing_options": sorted(missing),
        "unrecognized_help_options": sorted(listed - parsed),
        "missing_command_entries": missing_sections,
        "exception_errors": errors,
        "scope": "Literal strcmp(argv[i], option) branches in compiled parseOptions; "
                 "help headings, not prose quality/arity; command applicability only for explicit contracts.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("cli_help_policy.json"))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VALKEY", "REDIS"))}
    env.update(LC_ALL="C", TERM="dumb")
    outputs = []
    for flags, expected in [(["--help"], 0), (["--cluster", "help"], 1)]:
        result = subprocess.run([str(args.binary.resolve()), *flags], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=10, env=env)
        if result.returncode != expected or result.stderr or not result.stdout.strip():
            raise ValueError(f"Help invocation {flags} failed: exit={result.returncode}, stderr={result.stderr!r}")
        outputs.append(result.stdout)
    result = audit(args.source.read_text(), *outputs, policy["exceptions"], policy["required_sections"])
    failed = any(result[k] for k in ("missing_options", "unrecognized_help_options", "missing_command_entries", "exception_errors"))
    result["passed"] = not failed
    result["help_exit_codes"] = [0, 1]
    text = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(text)
    print(text, end="")
    return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Audit incomplete: {error}", file=sys.stderr)
        sys.exit(2)
