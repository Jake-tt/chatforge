"""A conversion worker that never finishes: writes its pid where asked, then hangs."""

import json
import os
import sys
import time

if __name__ == "__main__":
    params = json.loads(sys.argv[2])
    with open(params["pid_file"], "w") as f:
        f.write(str(os.getpid()))
    while True:
        time.sleep(1)
