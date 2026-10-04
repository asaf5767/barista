# Publishing Barista

## First release

1. Create or sign in to your PyPI account and enable two-factor authentication.
2. At https://pypi.org/manage/account/publishing/, add a pending GitHub publisher:
   - PyPI project: `barista-coffee`
   - Owner: `asaf5767`
   - Repository: `barista`
   - Workflow: `ci.yml`
   - Environment: leave blank (the current workflow has no environment).
3. Merge the reviewed CI fixes, including the valid publishing action pin, and verify the complete CI matrix passes on `master`. Fork PR workflows may require maintainer approval before running.
4. Verify `pyproject.toml` and `barista/__init__.py` have the intended release version. Build and inspect the wheel, including `barista/ui/index.html`.
5. Create the version tag from the tested `master` commit and push it:

```bash
git switch master
git pull --ff-only
git tag v0.1.0
git push origin v0.1.0
```

The tag triggers tests, distribution validation, and Trusted Publishing. The first successful upload creates the project under your PyPI account. A pending publisher does **not** reserve the name; only publishing does. Do not upload an empty placeholder package.

## Verify the release

In a new virtual environment outside the source checkout:

```bash
python -m pip install --index-url https://pypi.org/simple barista-coffee==0.1.0
barista --help
```

Confirm the PyPI project belongs to your account and links to `asaf5767/barista`. Only then restore the PyPI badge and replace the Git installation commands in the README with `python -m pip install barista-coffee`.

## Future releases

Update both version declarations, run CI, and tag the tested commit. PyPI versions cannot be overwritten. The workflow uses OIDC and does not require `PYPI_API_TOKEN`.

PyPI reference: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
