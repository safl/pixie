# pixie top-level Makefile
#
# All common operations in one place; ``make help`` lists them.
# Operators run everything from the repo root: ``make build
# VARIANT=usbboot-pc``, ``make test``, ``make ci``, etc.

UV      ?= uv
VARIANT ?= usbboot-pc

# Per-variant cijoe workflow file under cijoe/tasks/. The variant
# string carries the hardware family (``pc`` for generic x86 BIOS /
# UEFI boxes) and the boot source (``netboot`` for PXE-chain clients,
# ``usbboot`` for direct disk media). arm64 / RPi is out of the
# initial pixie port; when it lands it will follow bty's precedent of
# customising Raspberry Pi OS in place on a native arm64 host.
ifeq ($(VARIANT),netboot-pc)
MEDIA_TASK := tasks/netboot-pc.yaml
else
MEDIA_TASK := tasks/usbboot-pc.yaml
endif

.DEFAULT_GOAL := help

.PHONY: help \
        deps test lint format format-check typecheck ci wheel \
        media-deps build \
        clean

help:
	@echo "pixie top-level Makefile"
	@echo ""
	@echo "Dev (Python package, no sudo, no network beyond uv):"
	@echo "  deps          uv sync --group dev"
	@echo "  test          pytest (excludes integration marker)"
	@echo "  lint          ruff check"
	@echo "  format        ruff format (writes)"
	@echo "  format-check  ruff format --check"
	@echo "  typecheck     mypy src"
	@echo "  ci            lint + format-check + typecheck + test"
	@echo "  wheel         uv build  -> dist/pixie_lab-X.Y.Z-py3-none-any.whl + sdist"
	@echo ""
	@echo "Media (cijoe pipelines under cijoe/; require passwordless sudo):"
	@echo "  media-deps    pipx install cijoe"
	@echo "  build         build a media image (override VARIANT below)"
	@echo "                  -> ~/system_imaging/disk/pixie-<variant>.*"
	@echo ""
	@echo "Variant: $(VARIANT)  (override with VARIANT=netboot-pc, ...)"
	@echo "  usbboot-pc    - bootable USB live ISO via live-build (.iso, x86_64)"
	@echo "  netboot-pc    - kernel + initrd + squashfs trio for PXE-flash clients (x86_64)"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean         remove build artifacts (dist/, cijoe-output, _build, caches)"

# ---------- Python package ----------------------------------------------

deps:
	$(UV) sync --group dev

test:
	$(UV) run pytest -q

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run mypy src

ci: lint format-check typecheck test

wheel:
	$(UV) build

# ---------- Media (pixie-media/ via cijoe) -------------------------------

media-deps:
	pipx install cijoe
	pipx ensurepath

# Build a media image. Pick the variant via ``VARIANT=...``:
#   make build VARIANT=usbboot-pc     - bootable USB live ISO (.iso, x86_64)
#   make build VARIANT=netboot-pc     - kernel + initrd + squashfs for PXE clients
#
# Both variants use live-build (cijoe/tasks/netboot-pc.yaml,
# cijoe/tasks/usbboot-pc.yaml) and need ``live-build`` on the host
# plus passwordless sudo.
build:
	cd cijoe && cijoe $(MEDIA_TASK) --monitor -c configs/$(VARIANT).toml

# ---------- Cleanup ------------------------------------------------------

clean:
	rm -rf dist/ cijoe/_build cijoe/cijoe-output \
	       .pytest_cache .ruff_cache .mypy_cache
