"""
One-shot installer for the chrony helper daemon.

Run as root (e.g. sudo lucid-chrony-helper-installer --install-once).
Copies the helper systemd unit to /etc/systemd/system/, enables and
starts the service. Run once on the device after component install.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HELPER_SERVICE_NAME = "lucid-chrony-helper"
UNIT_DEST = Path(f"/etc/systemd/system/{HELPER_SERVICE_NAME}.service")
DROPIN_DIR = Path("/etc/systemd/system/lucid-agent-core.service.d")
DROPIN_FILE = DROPIN_DIR / "chrony-helper.conf"
DROPIN_CONTENT = "[Unit]\nWants=lucid-chrony-helper.service\nAfter=lucid-chrony-helper.service\n"


def install_once() -> int:
    """Copy unit, daemon-reload, enable, start. Returns 0 on success."""
    try:
        import lucid_component_chrony
        pkg_path = Path(lucid_component_chrony.__path__[0])
        unit_src = pkg_path / "systemd" / f"{HELPER_SERVICE_NAME}.service"
        if not unit_src.is_file():
            logger.error("Unit file not found: %s", unit_src)
            return 1

        # Resolve the helper binary path. The helper is always co-installed in
        # the same venv/bin/ directory as this installer, so look there first.
        # This works even when sudo resets PATH (use: sudo /full/path/to/installer).
        installer_bin = Path(sys.argv[0]).resolve()
        sibling = installer_bin.with_name("lucid-chrony-helper")
        if sibling.is_file():
            helper_bin = str(sibling)
        else:
            found = shutil.which("lucid-chrony-helper")
            if not found:
                logger.error(
                    "lucid-chrony-helper not found next to installer (%s) or on PATH. "
                    "Run: sudo env PATH=\"<venv>/bin:$PATH\" lucid-chrony-helper-installer --install-once",
                    sibling,
                )
                return 1
            helper_bin = found

        unit_text = unit_src.read_text()
        # Replace any ExecStart line with the detected binary path
        import re
        unit_text = re.sub(
            r"^ExecStart=.*$",
            f"ExecStart={helper_bin}",
            unit_text,
            flags=re.MULTILINE,
        )
        UNIT_DEST.write_text(unit_text)
        logger.info("Wrote unit file with ExecStart=%s", helper_bin)
    except Exception:
        logger.exception("Copy failed")
        return 1

    # Create agent-core drop-in so it Wants/After the helper (idempotent)
    try:
        DROPIN_DIR.mkdir(parents=True, exist_ok=True)
        DROPIN_FILE.write_text(DROPIN_CONTENT)
        logger.info("Wrote drop-in %s", DROPIN_FILE)
    except Exception:
        logger.exception("Failed to write agent-core drop-in")
        return 1

    for cmd in [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", HELPER_SERVICE_NAME],
        ["systemctl", "start", HELPER_SERVICE_NAME],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
            logger.error("%s: %s", cmd, err)
            return 1
    logger.info("Helper installed and started")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Install and start lucid-chrony-helper (run as root)")
    ap.add_argument("--install-once", action="store_true", help="Copy unit, enable and start the helper service")
    args = ap.parse_args()
    if not args.install_once:
        ap.print_help()
        sys.exit(0)
    sys.exit(install_once())


if __name__ == "__main__":
    main()
