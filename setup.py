#!/usr/bin/env python
from setuptools import setup

setup(
    name='asset_bender',
    version='0.1.4',
    description="A django runtime implementation for Asset Bender",
    long_description=open('Readme.md').read(),
    author='HubSpot Dev Team',
    author_email='devteam+asset_bender_django@hubspot.com',
    url='https://github.com/HubSpot/asset_bender_django',
    # download_url='https://github.com/HubSpot/',
    license='LICENSE.txt',
    packages=['asset_bender'],
    install_requires=[
        'django>=1.3.0',
        'hscacheutils<=1.0.0',
    ],
)