from pathlib import Path
from runpy import run_path
import json

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithIdentity(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent.resolve()
        collect = run_path(str(root / "src/allday_asr/build_info.py"))["collect_build"]
        destination = Path(self.build_lib) / "allday_asr/_build_info.json"
        destination.write_text(json.dumps(collect(root), indent=2), encoding="utf-8")


setup(cmdclass={"build_py": BuildWithIdentity})
