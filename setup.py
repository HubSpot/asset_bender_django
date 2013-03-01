#!/usr/bin/env python
from setuptools import setup

setup(
    name='asset_bender',
    version='0.1.1',
    description="A django runtime implementation for Asset Bender",
    long_description=open('README.md').read(),
    author='HubSpot Dev Team',
    author_email='devteam+asset_bender_django@hubspot.com',
    url='https://github.com/HubSpot/asset_bender_django',
    # download_url='https://github.com/HubSpot/hapipy/tarball/v2.10.1',
    license='LICENSE.txt',
    packages=['asset_bender'],
    install_requires=[
        # 'nose==1.1.2',
        # 'unittest2==0.5.1',
        # 'simplejson>=2.1.2'
    ],
)