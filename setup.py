import os
import sys
from lbry import __name__, __version__
from setuptools import setup, find_packages

BASE = os.path.dirname(__file__)
with open(os.path.join(BASE, 'README.md'), encoding='utf-8') as fh:
    long_description = fh.read()

setup(
    name=__name__,
    version=__version__,
    author="LBRY Inc.",
    maintainer="LBRY NG contributors",
    url="https://github.com/kodxana/lbry-sdk-ng",
    description="Community-maintained LBRY SDK for decentralized content applications",
    long_description=long_description,
    long_description_content_type="text/markdown",
    keywords="lbry protocol media",
    license='MIT',
    python_requires='>=3.13,<3.14',
    packages=find_packages(exclude=('tests', 'tests.*')),
    zip_safe=False,
    entry_points={
        'console_scripts': [
            'lbrynet=lbry.extras.cli:main',
            'orchstr8=lbry.wallet.orchstr8.cli:main'
        ],
    },
    install_requires=[
        'aiohttp==3.14.4',
        'aioupnp==0.0.18',
        'appdirs==1.4.4',
        'asn1crypto==1.5.1',
        'packaging==26.3',
        'certifi==2026.10.19',
        'colorama==0.4.6',
        'distro==1.9.0',
        'base58==2.1.1',
        'cffi==2.1.1',
        'cryptography==50.0.2',
        'protobuf==7.36.2',
        'prometheus_client==0.26.0',
        'pyyaml==6.0.3',
        'docopt==0.6.2',
        'hachoir==3.4.0',
        'coincurve==21.0.0',
        'pbkdf2==1.3',
        'filetype==1.2.0',
        'libtorrent==2.0.15',
    ],
    extras_require={
        'lint': [
            'pylint==4.1.2'
        ],
        'test': [
            'coverage',
            'jsonschema==4.26.0',
        ],
        'hub': [
            'hub@git+https://github.com/kodxana/lbry-hub-ng.git@87d96e0244ac696562c44c916bf95a5cb3b3dae8'
        ]
    },
    classifiers=[
        'Framework :: AsyncIO',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Operating System :: OS Independent',
        'Topic :: Internet',
        'Topic :: Software Development :: Testing',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Distributed Computing',
        'Topic :: Utilities',
    ],
)
