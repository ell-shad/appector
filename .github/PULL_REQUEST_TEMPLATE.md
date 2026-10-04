## Summary

Describe the change and its motivation.

## Validation

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `python3 -m compileall -q appector`
- [ ] `sh -n scripts/build-deb.sh`
- [ ] Disposable-system testing performed, or not applicable (explain below)

## Safety and privacy

- [ ] Destructive operations and error paths were reviewed.
- [ ] No credentials, personal logs, or installed-app exports are included.

## Notes

List compatibility concerns, user-visible behavior changes, and deferred tests.
