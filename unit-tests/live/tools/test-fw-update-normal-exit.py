# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

#test:device each(D400*)
#test:donotrun:!nightly

import platform
import re
import subprocess

from rspy import repo, test


fw_updater = repo.find_built_exe("tools/fw-update", "rs-fw-update")
if not fw_updater:
    raise RuntimeError("Could not find rs-fw-update")

test.start("Updater owns device handles within main")

if platform.system() == "Linux":
    symbols = subprocess.run(
        ["nm", "-C", fw_updater],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    test.check_equal(symbols.returncode, 0)
    test.check_false(
        re.search(
            r"^[0-9a-fA-F]+\s+[Bb]\s+new_(?:fw_update_)?device$",
            symbols.stdout,
            re.MULTILINE,
        )
    )

test.finish()

test.start("Repeated non-flashing updater exits")

for _ in range(20):
    result = subprocess.run(
        [fw_updater, "--list_devices"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    test.check_equal(result.returncode, 0)

test.finish()
test.print_results_and_exit()
