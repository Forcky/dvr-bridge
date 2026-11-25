# Contributing to DVR Bridge

Thanks for helping! The most useful contributions, roughly in order:

1. **Compatibility reports.** Tried DVR Bridge on a model that is not in the README's table? Open a
   [compatibility report](https://github.com/Forcky/dvr-bridge/issues/new?template=compatibility.yml),
   whether it worked or not.
2. **Bug reports** with clear steps and log lines (scrubbed of secrets).
3. **Pull requests** for bugs, model quirks and the features on the issue tracker.

## Development

You only need Docker:

```sh
docker build --target test .        # the whole test suite (runs inside the image build)
docker build -t dvr-bridge:dev .     # the runtime image
```

No DVR at hand? `tests/sim.py` starts a simulated DVR and Frigate; its docstring has the commands to
run DVR Bridge against it in two containers.

Code layout and how the pieces fit together are described under "How it works" in the README.

## Rules that matter here

- **No vendor code.** Do not submit code that is copied, decompiled, disassembled or translated from
  DVR firmware or vendor software, even partially. Protocol support must come from observed
  behaviour (network captures of your own devices, black-box testing), described in your own words
  - see `docs/protocol-codec.md` for how the port-9000 obfuscation was specified this way.
- **No secrets or identifiers** in code, tests, fixtures or screenshots: passwords, tokens, cookies,
  serial numbers, MAC addresses, public IPs, real camera images. Scrub captured API responses before
  adding them as fixtures (use `192.0.2.x` documentation addresses).
- **DVR writes stay rare.** DVR Bridge talks to the DVR through an allow-list
  (`dvrbridge/dvr/client.py`, pinned by a test). Adding a write endpoint needs a strong reason, a
  read-modify-write that changes only the intended fields, and a test.
- **Tests with every change**, and keep dependencies to a minimum - every package in the image is
  something users have to trust.
- Keep the UI plain HTML/JS/CSS with no build step, and render anything that came from the DVR or a
  user with `textContent`, never `innerHTML`.

## Releases

Maintainers: bump `__version__` in `dvrbridge/__init__.py`, add a section to `CHANGELOG.md`, merge, then
push a tag `vX.Y.Z`. The Release workflow tests, builds linux/amd64 + linux/arm64 images to
`ghcr.io/forcky/dvr-bridge` with SBOM and provenance, and creates the GitHub release from the changelog.

By contributing you agree that your contribution is licensed under the AGPL-3.0, like the rest of the
project.
