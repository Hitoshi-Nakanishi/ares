# Third-party software

Ares's original source is under the [MIT License](LICENSE). That license does not replace the licenses of its dependencies.
The source distribution and wheel contain the launcher, not its dependencies, inference engines or model weights. pip
installs dependencies separately with their upstream license files.

| Package | Upstream license |
| --- | --- |
| [httpx](https://pypi.org/project/httpx/), [httpcore](https://pypi.org/project/httpcore/), [idna](https://pypi.org/project/idna/) and [psutil](https://pypi.org/project/psutil/) | BSD-3-Clause |
| [PyYAML](https://pypi.org/project/PyYAML/), [anyio](https://pypi.org/project/anyio/) and [h11](https://pypi.org/project/h11/) | MIT |
| [certifi](https://pypi.org/project/certifi/) | MPL-2.0 |
| [typing_extensions](https://pypi.org/project/typing-extensions/) | PSF-2.0 |

The table reflects the metadata of the dependencies checked for this source release. Resolved dependencies vary with Python
and platform. Retain the actual packages' copyright notices and license files when redistributing them.

The selected inference engine, Docker image and model have independent licenses. Installing Ares does not download them or
grant rights to redistribute them. Review those upstream licenses before supplying an engine, image or weights to others.
