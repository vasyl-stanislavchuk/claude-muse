# Local gate. `check` is the one command to run before a commit; the full
# suite takes seconds, so there is no affected-tests shortcut to maintain.
SH_FILES := install.sh uninstall.sh bin/*.sh bin/claude-muse lib/*.sh profile/statusline.sh profile/hooks/continue-gate .githooks/*

.PHONY: check test shell help

check: test shell
	@if command -v ruff >/dev/null 2>&1; then ruff check . && ruff format --check .; else echo "ruff not installed, skipping (pipx install ruff)"; fi

test:
	python3 -m pytest -q

shell:
	@for f in $(SH_FILES); do bash -n "$$f" || exit 1; done
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck -S warning $(SH_FILES); else echo "shellcheck not installed, skipping (brew install shellcheck)"; fi

help:
	@echo "check  run the full local gate (pytest, shell syntax, ruff if installed)"
	@echo "test   run the offline pytest suite"
	@echo "shell  syntax-check every shell file (shellcheck too, if installed)"
