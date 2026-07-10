from pathlib import Path

from setuptools import find_packages
from setuptools import setup


README = Path(__file__).with_name("README.md").read_text(encoding="utf-8")


setup(
    name="wlanpi-beacon-live",
    version="0.1.0",
    description="CLI tools for replaying WLAN beacon QBSS channel utilization samples.",
    long_description=README,
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(include=["beacon_live", "beacon_live.*"]),
    install_requires=[],
    extras_require={"dev": ["pytest>=7.4,<9"]},
    entry_points={"console_scripts": ["beacon-live=beacon_live.cli:main"]},
)
