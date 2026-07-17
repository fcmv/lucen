# Releasing

Releases are cut from `main` by pushing a version tag. CI does the rest: it
builds the wheels, generates a software bill of materials, signs the artifacts,
and publishes to PyPI with trusted publishing. No long-lived token is stored
anywhere.

## Versioning

Lucen follows [Semantic Versioning](https://semver.org/); what a major,
minor, or patch bump means for each part of the public surface is defined in
[STABILITY.md](STABILITY.md).

The version appears in three places, which must agree:

- `pyproject.toml`, `[project].version`
- `lucen/__init__.py`, `__version__` (the pure-Python wheel reads its version
  from here)
- `lucen_core/Cargo.toml`, `[package].version`

## Cutting a release

1. Confirm `main` is green (the full CI matrix, the formal checks, and the
   nightly fuzz and perf lanes).
2. Update the three version fields to the new version in one commit, and add the
   release's notable changes to the release notes draft.
3. Open a pull request, get it reviewed, and merge.
4. Tag the merge commit and push the tag:

   ```bash
   git tag v<version>
   git push origin v<version>
   ```

5. The `Release` workflow runs on the `v*` tag and:
   - builds the native `abi3` wheels for Linux, macOS, and Windows;
   - builds the `py3-none-any` pure-Python wheel and sanity-checks that it
     imports with the native core absent;
   - builds the source distribution;
   - generates CycloneDX software bills of materials for the Python and Rust
     dependency trees;
   - publishes the wheels and sdist to PyPI with trusted publishing (OIDC) and
     PEP 740 attestations, so every artifact is Sigstore-signed;
   - creates a GitHub Release with the wheels, the SBOMs, and their Sigstore
     signature bundles attached.

## After a release

- Verify the release installs on a GIL build (`pip install lucen`, which
  takes the native wheel) and on a free-threaded build (which takes the pure
  wheel and runs the fallback).
- Confirm the docs site redeployed from `main`.

## What is signed, and how to verify

The PyPI artifacts carry PEP 740 attestations, produced by Sigstore during the
trusted-publishing step; PyPI verifies and displays them. The GitHub Release
artifacts carry `.sigstore` bundles. A consumer can verify a downloaded wheel
against its bundle with the `sigstore` tool:

```bash
python -m pip install sigstore
python -m sigstore verify identity \
  --cert-identity "https://github.com/fcmv/lucen/.github/workflows/release.yml@refs/tags/v<version>" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  lucen-<version>-*.whl
```
