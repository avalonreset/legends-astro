Describe the concrete behavior change and its scope.

Validation:

- Offline Python tests (`python -m unittest discover -s tests -v`):
- Source snapshot verification (`node ./scripts/verify-sources.mjs`):
- Doctor matrix (`node ./scripts/test-doctor.mjs`):
- Release checks (`python scripts/check_release.py`):
- Source documentation reviewed:
- Known limitations:

Confirm that examples and test fixtures contain no credentials, private domains, or proprietary site exports.
