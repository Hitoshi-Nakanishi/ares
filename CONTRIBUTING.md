# Contributing

Ares welcomes focused bug reports and pull requests. Include the expected result, actual result, reproduction steps, OS and
Python version. Use fictional examples and remove private data, paths and credentials from attachments.

## Development

Use Python 3.11 or newer and a virtual environment. From the checkout:

```sh
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check src tests
python -m ruff format --check src tests
```

Keep changes small and cover changed behavior with tests. Tests use mocks and tiny processes, without a GPU, installed models,
private data or a live inference server. Windows-specific tests skip on other systems.

For a submodule checkout, commit in Ares first, then update the parent repository's pointer. Keep its virtual environment in the
Ares checkout.

The maintainer runs [publication checks](PUBLICATION.md); contributors do not need the private policy. Keep configured Git hooks
enabled. Contributions use the [MIT License](LICENSE); dependencies retain their own licenses.
