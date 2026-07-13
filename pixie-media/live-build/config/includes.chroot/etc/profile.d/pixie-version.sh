# shellcheck shell=sh
# Show the pixie version on every interactive shell start so operators
# can read it back without invoking ``pixie --version`` themselves. The
# placeholder gets substituted by ``cijoe/scripts/usb_iso_build.py``
# at bake time. PS1 prefix runs in addition: keeps the version
# visible during long shell sessions where the motd has scrolled off.
if [ -n "${PS1:-}" ]; then
    printf 'pixie __PIXIE_VERSION__\n'
    PS1='[pixie __PIXIE_VERSION__] '"${PS1}"
fi
