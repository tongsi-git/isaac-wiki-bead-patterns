# Contributing

Thanks for improving Isaac Wiki Bead Patterns.

## Development setup

```bash
git clone https://github.com/tongsi-git/isaac-wiki-bead-patterns.git
cd isaac-wiki-bead-patterns
python -m venv .venv
```

Activate the environment, then install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

Run a small end-to-end download without generating the full catalogue:

```bash
python isaac_beads.py build \
  --categories collectibles,characters \
  --limit 3 \
  --output output/dev \
  --no-zip
```

## Pull requests

- Keep changes focused and explain user-visible behaviour in the PR.
- Add or update tests for processing, palette, or rendering changes.
- Do not commit generated Wiki images, output archives, credentials, or tokens.
- Images fetched from the wiki remain subject to their original rights. Code and
  palette data are licensed under the repository's MIT license.
