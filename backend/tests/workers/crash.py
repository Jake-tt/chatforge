"""A conversion worker that dies the way a parser bug would."""

import sys

if __name__ == "__main__":
    sys.stderr.write("Traceback: parser exploded\n")
    sys.exit(1)
