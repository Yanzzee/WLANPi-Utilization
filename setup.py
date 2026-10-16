from pathlib import Path

from setuptools import find_packages
from setuptools import setup


README = Path(__file__).with_name("README.md").read_text(encoding="utf-8")


setup(
    name="wlanpi-beacon-live",
    version="2.1.0.dev0",
    description="Utilization live WLAN metrics and beacon replay tools.",
    long_description=README,
    long_description_content_type="text/markdown",
    python_requires=">=3.13",
    packages=find_packages(include=["beacon_live", "beacon_live.*"]),
    package_data={"beacon_live": ["templates/*.html", "web_static/*.css", "web_static/*.js"]},
    install_requires=[],
    extras_require={"core": ["websockets==15.0.1"], "web": ["Flask>=3.1.3,<4", "waitress>=3.0.2,<4"], "dev": ["pytest>=8.3.5,<9"]},
    entry_points={
        "console_scripts": [
            "wlanpi-utilization=beacon_live.device:main",
            "utilization=beacon_live.cli:main",
            "beacon-live=beacon_live.cli:main",
            "wlanpi-beacon-live=beacon_live.device:main",
        ]
    },
)
