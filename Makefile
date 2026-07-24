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
        media-deps build ipxe test-pxe test-usb-ventoy \
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
	@echo "  ipxe          build pixie's custom iPXE -> IPXE_OUT/ipxe.efi (default dist/ipxe/)"
	@echo "  test-pxe      end-to-end PXE bootstrap chain test"
	@echo "                  (needs podman + QEMU + KVM + dnsmasq; a few min wall clock)"
	@echo "  test-pxe-nbdboot  end-to-end PXE nbdboot chain test"
	@echo "                  (same deps as test-pxe + a prior VARIANT=netboot-pc bake)"
	@echo "  test-pxe-inventory  end-to-end PXE pixie-inventory chain test"
	@echo "                  (same deps as test-pxe-nbdboot; no catalog seed)"
	@echo "  test-pxe-flash  end-to-end PXE pixie-flash-once chain test"
	@echo "                  (same deps as test-pxe-inventory; small synthetic image)"
	@echo "  test-pxe-flash-always  end-to-end PXE pixie-flash-always chain test"
	@echo "                  (same deps as test-pxe-flash; asserts no /done flip)"
	@echo "  test-pxe-tui  end-to-end PXE pixie-tui chain test"
	@echo "                  (same deps as test-pxe-flash; asserts wizard entry)"
	@echo "  test-usb-ventoy  structural + Ventoy-boot verify of the usbboot .iso"
	@echo "                  (needs a prior VARIANT=usbboot-pc bake + qemu/KVM/OVMF + ventoy deps)"
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

# Ramboot chain test: bring up pixie, seed the catalog with a bundle
# assembled from the netboot-pc bake artifacts + a synthetic disk
# image, bind the client MAC to boot_mode=nbdboot, PXE-boot QEMU, and
# assert every marker in cijoe/configs/test-pxe-nbdboot.toml shows up
# through the initramfs.s nbdboot script. Depends on a prior
# ``pixie-netboot-pc-x86_64`` bake staged under
# ``~/system_imaging/disk/`` (or PIXIE_NETBOOT_ARTIFACT_DIR).
test-pxe-nbdboot:
	cd cijoe && cijoe tasks/test-pxe-nbdboot.yaml --monitor -c configs/test-pxe-nbdboot.toml

# Inventory chain test: bring up pixie with the netboot-pc bake
# bind-mounted as its live-env dir, bind the client MAC to
# boot_mode=pixie-inventory, PXE-boot QEMU, and assert the live-env
# actually boots + posts an inventory blob back to pixie. Same
# artifact dependency as test-pxe-nbdboot (needs the netboot-pc
# bake).
test-pxe-inventory:
	cd cijoe && cijoe tasks/test-pxe-inventory.yaml --monitor -c configs/test-pxe-inventory.toml

# Flash chain test: bring up pixie with the netboot-pc bake bind-mounted
# as its live-env dir, seed the catalog with a small (16 MiB) synthetic
# image whose first bytes carry a marker, bind the client MAC to
# boot_mode=pixie-flash-once + target_disk_serial=PIXIETEST, PXE-boot
# QEMU, and assert the live-env's pixie CLI auto-flashes the image +
# POSTs status=done. Same artifact dependency as test-pxe-inventory
# (needs the netboot-pc bake).
test-pxe-flash:
	cd cijoe && cijoe tasks/test-pxe-flash.yaml --monitor -c configs/test-pxe-flash.toml

# Flash-always chain test: mirrors test-pxe-flash but binds
# boot_mode=pixie-flash-always. Same wire; the post-chain assertion
# inverts (mode must NOT flip to ipxe-exit on the CLI's /done POST).
test-pxe-flash-always:
	cd cijoe && cijoe tasks/test-pxe-flash-always.yaml --monitor -c configs/test-pxe-flash-always.toml

# TUI chain test: binds boot_mode=pixie-tui, asserts the CLI reaches
# the interactive wizard's SELECT_IMAGE screen (proves the plan
# dispatch AND the /catalog.toml catalog wire between server + CLI).
# Does not drive the wizard's inputs; that would need QMP send-key
# and the flash pipeline past the pick is already covered by
# test-pxe-flash.
test-pxe-tui:
	cd cijoe && cijoe tasks/test-pxe-tui.yaml --monitor -c configs/test-pxe-tui.toml

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
