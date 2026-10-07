# Contributing to AITS

Thank you for your interest in contributing to AITS.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin (DCO)](DCO.md) to certify that contributors have the right to submit their work under the project's [Apache License 2.0](LICENSE).

Every commit in a contribution must include a `Signed-off-by` line in the commit message:

```
Signed-off-by: Jane Doe <jane.doe@example.com>
```

Add the sign-off when you commit:

```bash
git commit -s -m "Your commit message"
```

If you forgot to sign off on earlier commits in a branch, you can amend or rebase to add it before opening a pull request.

## Pull requests

When this project opens for external contributions, pull requests will be required to include DCO sign-offs on all commits. Automated checks and branch protection will enforce that requirement at that time.

## Claude Code hooks

If you open this repository in Claude Code, the hooks in `.claude/settings.json` run automatically and send session and tool-call records to the AITS ledger at `AITS_URL` (default: `http://127.0.0.1:8000`); see [.claude/README.md](.claude/README.md).

## Documentation

Organize maintained docs by reader need, following [Diátaxis](https://diataxis.fr/):

- `docs/tutorials/`: guided learning through a worked example.
- `docs/howto/`: steps to complete a specific task.
- `docs/reference/`: supported interfaces, defaults, and constraints.
- `docs/explanation/`: concepts and design rationale.

Link new pages from the [documentation index](docs/README.md). Keep versioned
release changes in [CHANGELOG.md](CHANGELOG.md), with links to migration guides.
When implementation plans are complete, preserve durable rationale and contracts
in maintained docs and remove the superseded plans; Git history retains them.
Verify documentation against the implementation and OpenAPI, and check relative
links when moving or removing pages.

Execute tutorial and how-to code against a fresh disposable ledger with a
regenerated SDK. Compare responses with the documented output, including
failure cases. The tampering tutorial changes stored rows; do not validate it
against a ledger you need to retain.
