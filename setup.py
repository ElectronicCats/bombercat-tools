#!/usr/bin/env python3

# Electronic Cats
# setup.py — `pip install .` for the BomberCat CLI.
#
# VERSION at the repo root is the single source of truth for the version; the
# .deb, the .pkg.tar.zst and the Inno Setup script all read the same file.
# Distributed as-is; no warranty is given.

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).parent


def read_version() -> str:
    return (HERE / "VERSION").read_text(encoding="utf-8").strip()


def read_requirements() -> list[str]:
    """Runtime dependencies, straight from requirements.txt.

    Keeps one list instead of two: the vendoring step of the Linux packages and
    `pip install .` install exactly the same things.
    """
    lines = (HERE / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]


setup(
    name="bombercat",
    version=read_version(),
    description=(
        "BomberCat CLI — NFC relay, tag/reader detection, magspoof and UF2 flashing"
    ),
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Electronic Cats",
    author_email="support@electroniccats.com",
    url="https://github.com/ElectronicCats/bombercat-tools",
    license="GPL-3.0",
    packages=find_packages(include=["modules", "modules.*"]),
    py_modules=["bombercat"],
    package_data={"modules.tags.mifare": ["data/*.keys"]},
    install_requires=read_requirements(),
    python_requires=">=3.12",
    entry_points={"console_scripts": ["bombercat=bombercat:main_cli"]},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
        "Topic :: Security",
        "Topic :: System :: Hardware",
    ],
)
