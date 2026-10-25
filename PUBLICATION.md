# Publication checks

Ares source is under the [MIT License](LICENSE). Resolve every privacy finding before publication and retain the
[third-party notices](THIRD_PARTY_NOTICES.md). Model and engine licenses are separate from the launcher's source.

The following is the maintainer's privacy gate; users and contributors do not need the private policy to install or test Ares.
Argus runs locally from a separate guard checkout with the owner's private policy. Keep that policy outside this repository.
With that environment activated, run from its root:

```sh
python -m apps.argus --source check --repo <source-checkout>
python -m apps.argus --source history --all --repo <source-checkout>
python -m apps.argus --source install --repo <source-checkout>
```

Both checks must pass. Source mode retains the private policy, scans archive members and complete historical file snapshots,
commit messages and annotated tags, and refuses shallow history and unreadable binary content. It accepts documentation
command blocks; those are expected in a source repository. A passing scan only means configured rules found no matches.
Inspect examples, dependency licenses and distributable package contents as well.

The installed hooks check staged changes and commit messages. Before every push, they inspect all locally referenced history,
including commits already on a remote. Install them again in each clone. Missing policy blocks the operation. CI tests do not
replace this private local check; never upload the policy or detailed findings into public CI artifacts.

Deleted text remains in earlier commits. Keep an existing private history private until findings are resolved. Any shared
history rewrite or publication needs explicit owner authorization. Never use `--no-verify` to publish around a finding.
