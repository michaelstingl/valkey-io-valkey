# CLI help coverage experiment

The experiment lives in `michaelstingl/valkey-io-valkey`, branch `experiment/cli-help-coverage`. Baseline runs deliberately fail on the upstream omissions; later help-only corrections provide the positive control.

This fork-only experiment asks how many supported CLI options are absent from help, and whether CI can catch the omissions. It is not an upstream-ready generic C analysis tool.

Build the CLI and its preprocessed source with matching settings, then compare the parser inventory with the real executable's help:

```sh
make -j2 valkey-cli BUILD_TLS=yes MALLOC=libc
make -C src valkey-cli.i BUILD_TLS=yes MALLOC=libc
python3 -m unittest discover -s utils -p test_cli_help_audit.py
python3 utils/cli_help_audit.py --source src/valkey-cli.i --binary src/valkey-cli --json audit.json
```

The audit exits 1 for a contract violation, 2 for an incomplete audit, and 0 for no findings within its stated scope. It only invokes `--help` and `--cluster help`, preserving their existing exit codes (0 and 1). No server is started or contacted. Preprocessing avoids counting TLS/RDMA flags that are not compiled into that build.

At upstream `ea4d4e7d561895f64a86238bc587063c79c23da7`, a TLS build accepts 100 option spellings. Twelve are absent from help headings: seven compatibility/short aliases, two internal hint-test controls, and three public options. The public findings are:

- `--cluster-use-atomic-slot-migration`: absent under both reshard and rebalance.
- `--mono`: accepted but absent from general help.
- `--cluster-primary-id`: absent from add-node help, which instead advertises the unaccepted `--cluster-primaries-id`.

Those are three problem cases, not twelve independent bugs. The primary-id spelling error produces both a missing-option and an unrecognized-help finding. Section-level findings also overlap option-level findings and must not be added together as separate bugs.

`cli_help_policy.json` makes the nine exceptions explicit and checks that they still exist. Alias targets must remain accepted. New unknown options are not silently grandfathered. The policy separately asserts known applicability for ASM (reshard/rebalance) and primary-id (add-node).

Limits: the bounded extractor recognizes literal `strcmp(argv[i], "option")` branches in `parseOptions`. It fails on an unknown function shape or unhandled standalone option literal, but is not a complete C parser. It checks actual help headings, not prose accuracy, argument arity, or every command-option applicability relation. It does not cover server commands, environment variables or other executables. A shared option registry would be the structural solution; this experiment measures the current manual implementation first.

CI builds plain, TLS, and TLS+RDMA variants. A green result is a help-contract result, not a full Valkey test-suite claim or a claim that branch protection requires this workflow.
