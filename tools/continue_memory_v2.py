"""Resume the existing stage checkpoint; never reset or create a new budget."""
from pathlib import Path
import os
import sys

if __name__ == "__main__":
    project = Path(__file__).resolve().parents[1]
    os.chdir(project)
    sys.path.insert(0, str(project / "src"))
    if len(sys.argv) == 1:
        sys.argv.append("run")
    from allday_asr.memory_v2.continuation import main

    main()
