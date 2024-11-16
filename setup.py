from setuptools import setup

# Metadata goes in setup.cfg. These are here for GitHub's dependency graph.
setup(
    name="sweetrpg-api-core",
    install_requires=[
        "sweetrpg-model-core",
        "sweetrpg-db",
        "sweetrpg-common",
    ],
    extras_require={},
)
