# pixie top-level Makefile
#
# All common operations in one place; ``make help`` lists them.
# Operators run everything from the repo root: ``make build
# VARIANT=usbboot-pc``, ``make test``, ``make ci``, etc.

UV      ?= uv
VARIANT ?= usbboot-pc

# Per-variant cijoe workflow file under cijoe/tasks/. The variant
# string carries the hardware family (``pc`` for generic x86 BIOS /
# UEFI boxes) and the boot source (``usbboot`` for direct disk media,
# the bootable .iso for ad-hoc flash-install). arm64 / RPi is out of
# the initial pixie port; when it lands it will follow bty's precedent
# of customising Raspberry Pi OS in place on a native arm64 host.
MEDIA_TASK := tasks/usbboot-pc.yaml

.DEFAULT_GOAL := help

.PHONY: help \
        deps test lint format format-check typecheck ci wheel \
        media-deps build live-env-image ipxe test-pxe test-usb-ventoy \
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
	@echo "  live-env-image  build the pixie-live-env disk image (arch-headless"
	@echo "                  base + pixie CLI/service) nbdbooted by the live-env modes"
	@echo "  ipxe          build pixie's custom iPXE -> IPXE_OUT/ipxe.efi (default dist/ipxe/)"
	@echo "  test-pxe      end-to-end PXE bootstrap chain test"
	@echo "                  (needs podman + QEMU + KVM + dnsmasq; a few min wall clock)"
	@echo "  test-pxe-nbdboot  end-to-end PXE nbdboot chain test"
	@echo "                  (same deps as test-pxe + internet for the real nosi oras pulls)"
	@echo "  test-usb-ventoy  structural + Ventoy-boot verify of the usbboot .iso"
	@echo "                  (needs a prior VARIANT=usbboot-pc bake + qemu/KVM/OVMF + ventoy deps)"
	@echo ""
	@echo "Variant: $(VARIANT)"
	@echo "  usbboot-pc    - bootable USB live ISO via live-build (.iso, x86_64)"
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

# Build the pixie-live-env disk image: pull the nosi arch-headless base
# via ORAS, inject the pixie CLI + pixie-on-tty1 service, slim + gzip,
# and publish the asset (< 2 GiB) plus its sha256 / content-sha256
# sidecars and a catalog fragment. The live-env boot modes nbdboot this
# image; PIXIE_LIVE_ENV_IMAGE_SHA selects it (= the content-sha256).
# Needs qemu-nbd + passwordless sudo + uv + the nbd kernel module.
live-env-image:
	cd cijoe && cijoe tasks/live-env-image.yaml --monitor -c configs/live-env-image.toml

# Build pixie's slim iPXE binary (bin-x86_64-efi/ipxe.efi) with the
# embedded chain-loader baked in. Landed in the container image so a
# fresh deploy gets the one-bootfile chain guarantee without needing
# the operator to touch DHCP beyond pointing PXE clients at pixie.
IPXE_OUT ?= $(CURDIR)/dist/ipxe
ipxe:
	python3 cijoe/scripts/pixie_ipxe_build.py --out "$(IPXE_OUT)"
	@echo "custom ipxe.efi -> $(IPXE_OUT)/ipxe.efi"

# Real-firmware PXE chain test. Brings up pixie in a container +
# QEMU client + bridge/tap/dnsmasq, asserts every chain marker
# in cijoe/configs/test-pxe.toml appears on the client serial log
# or in pixie's container logs. Ramboot + catalog fetch land in a
# follow-up. Wall clock: a few minutes per run.
test-pxe:
	cd cijoe && cijoe tasks/test-pxe.yaml --monitor -c configs/test-pxe.toml

# Nbdboot chain test: bring up pixie, POST /ui/catalog/import with a
# pinned nosi catalog, fetch the real nosi debian-13-headless-netboot
# bundle + disk image over oras, bind the client MAC to
# boot_mode=nbdboot, PXE-boot QEMU, and assert every marker in
# cijoe/configs/test-pxe-nbdboot.toml shows up through the initramfs
# nbdboot script. Needs internet to ghcr.io + github.com/safl/nosi.
test-pxe-nbdboot:
	cd cijoe && cijoe tasks/test-pxe-nbdboot.yaml --monitor -c configs/test-pxe-nbdboot.toml

# Structural + Ventoy-boot verification of the usbboot .iso. Needs a
# prior VARIANT=usbboot-pc bake staged under ~/system_imaging/disk/ (or
# a downloaded pixie-usbboot-pc-x86_64 CI artifact) + qemu/KVM/OVMF +
# ventoy tooling deps (losetup, exfat-fuse, passwordless sudo).
test-usb-ventoy:
	cd cijoe && cijoe tasks/test-usb-ventoy.yaml --monitor -c configs/test-usb-ventoy.toml

# ---------- Cleanup ------------------------------------------------------

clean:
	rm -rf dist/ cijoe/_build cijoe/cijoe-output \
	       .pytest_cache .ruff_cache .mypy_cache
