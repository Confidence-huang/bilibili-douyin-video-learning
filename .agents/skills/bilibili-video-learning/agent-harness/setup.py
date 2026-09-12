"""Install the Video Learning CLI-Anything harness into a shared tools environment."""
from setuptools import find_namespace_packages, setup  # PEP 420 lets multiple cli_anything packages coexist.


setup(
    name="cli-anything-video-learning",
    version="1.3.4",
    description="Agent-ready CLI for the local Bilibili and Douyin video-learning Skill",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    install_requires=[
        "click>=8.1.7",  # Click supplies composable command groups and consistent help output.
        "prompt-toolkit>=3.0.48",  # The canonical CLI-Anything ReplSkin uses prompt_toolkit.
        "requests>=2.32.4",  # The real Douyin SSR backend imports requests directly.
    ],
    extras_require={"test": ["pytest>=8.4.1"]},
    entry_points={
        "console_scripts": [
            "cli-anything-video-learning=cli_anything.video_learning.video_learning_cli:main",
        ],
    },
    package_data={"cli_anything.video_learning": ["skills/*.md"]},
    python_requires=">=3.10",
)
