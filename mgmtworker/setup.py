########
# Copyright (c) 2019 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

from setuptools import setup, find_packages

install_requires = [
    'cloudify-common==7.0.0.dev1',
    'cloudify-agent==7.0.0.dev1',
    'packaging==17.1',
    'retrying>1,<2',
    'psycopg2==2.9.3',
    'cryptography>=37,<40',
    'python-dateutil==2.8.2',
    'pytz',
]


setup(
    name='cloudify-mgmtworker',
    version='7.0.0.dev1',
    author='Cloudify',
    author_email='cosmo-admin@cloudify.co',
    packages=find_packages(),
    license='LICENSE',
    description='Cloudify Management Worker',
    install_requires=install_requires
)
