# Contributing

Ares accepts focused bug reports and pull requests. Describe the expected behavior, what happened, and how to reproduce it.
Use fictional examples. Never attach private workbooks, configurations, source identities, personal data, credentials or logs
containing them. An ignored file may still leak when attached to an issue.

## Development

Use Python 3.11 or newer and a virtual environment. From the checkout:

```sh
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests
python -m ruff format --check src tests
```

Keep changes small and add behavior coverage for a bug fix. Tests must not depend on credentials, private data, installed
models or a live inference server. Include the operating system and Python version when reporting a failure.

The maintainer runs the private checks in [PUBLICATION.md](PUBLICATION.md) before publishing. Contributors do not need the
maintainer's private policy to install the application or run its tests. Never bypass a maintainer's configured Git hook.

Contributions are provided under the project's [MIT License](LICENSE). Dependencies retain their own licenses.
