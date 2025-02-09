#########
# Copyright (c) 2020 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.

import os
import zipfile
import tempfile
import requests
import shutil
from retrying import retry
from contextlib import closing
from urllib.parse import parse_qs

from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import parse as parse_version

from cloudify.exceptions import InvalidBlueprintImport
from cloudify_rest_client.exceptions import CloudifyClientError

from dsl_parser import parser
from dsl_parser.version import DSL_VERSION_PREFIX
from dsl_parser.import_resolver.default_import_resolver import (
    DefaultImportResolver)


FILE_SERVER_PLUGINS_FOLDER = 'plugins'
FILE_SERVER_BLUEPRINTS_FOLDER = 'blueprints'

PLUGIN_PREFIX = 'plugin:'
BLUEPRINT_PREFIX = 'blueprint:'
EXTRA_VERSION_CONSTRAINT = 'additional_version_constraint'


class ResolverWithCatalogSupport(DefaultImportResolver):
    """A resolver which translates plugin-catalog style urls to file:// urls.

    For example:
    The URL: `plugin:cloudify-openstack-plugin/2.0.1` will be
    translated to: `file:///opt/manager/resources/<PLUGINS_FOLDER>/<id>`,
    where <id> is the id of the plugin looked up for the current tenant.
    The version is optional.

    The URL: `blueprint:hello_world` will be translated to:
    `file:///opt/manager/resources/<BLUEPRINTS_FOLDER>/<tenant>/<id>`,
    where <id> is the id of the blueprint looked up for the current tenant.
    """

    def __init__(self, rules=None, fallback=True,
                 plugin_version_constraints=None,
                 plugin_mappings=None,
                 file_server_root=None,
                 marketplace_api_url=None,
                 client=None):
        super(ResolverWithCatalogSupport, self).__init__(rules, fallback)
        self.version_constraints = plugin_version_constraints or {}
        self.mappings = plugin_mappings or {}
        self.file_server_root = file_server_root
        self.marketplace_api_url = marketplace_api_url
        self.client = client

    @staticmethod
    def _is_plugin_url(import_url):
        return import_url.startswith(PLUGIN_PREFIX)

    @staticmethod
    def _is_blueprint_url(import_url):
        return import_url.startswith(BLUEPRINT_PREFIX)

    def fetch_import(self, import_url, dsl_version=None, **kwargs):
        if self.mappings:
            import_url = self._rewrite_from_mappings(import_url)
        if self._is_blueprint_url(import_url):
            return self._fetch_blueprint_import(import_url)
        elif self._is_plugin_url(import_url):
            return self._fetch_plugin_import(import_url, dsl_version)
        return super(ResolverWithCatalogSupport, self).fetch_import(import_url)

    def retrieve_plugin(self, import_url, dsl_version=None):
        if not self._is_plugin_url(import_url):
            raise InvalidBlueprintImport(
                'Error retrieving plugin, expected plugin url, got: {0}'
                .format(import_url))
        plugin_spec = import_url.replace(PLUGIN_PREFIX, '', 1).strip()
        name, plugin_filters = self._make_plugin_filters(
            plugin_spec, self.version_constraints, self.mappings)
        return self._find_plugin(name, plugin_filters, dsl_version)

    def _rewrite_from_mappings(self, import_url):
        for plugin_name, mapping in self.mappings.items():
            if import_url == mapping.get('import_url'):
                return 'plugin:{0}?version={1}'.format(
                    plugin_name,
                    mapping.get('version')
                )
        return import_url

    def _fetch_plugin_import(self, import_url, dsl_version):
        plugin = self.retrieve_plugin(import_url, dsl_version)
        plugin_yaml = self.client.plugins.get_yaml(plugin.id, dsl_version)
        return plugin_yaml

    @staticmethod
    def _make_plugin_filters(plugin_spec, version_constraints, mappings):
        """Parse the plugin spec to a dict of filters for the sql query

        >>> _make_plugin_filters('cloudify-openstack-plugin')
        {'package_name': 'cloudify-openstack-plugin'}
        >>> _make_plugin_filters('cool?version=1.0.2')
        {'package_name': 'cool', 'package_version': '1.0.2'}
        >>> _make_plugin_filters('cool?version=1.0.2&distribution=centos')
        {'package_name': 'cool', 'package_version': '1.0.2',
         'distribution': 'centos'}
        """
        filter_renames = {'version': 'package_version',
                          'distribution': 'distribution'}
        name, _, params = plugin_spec.partition('?')
        filters = {}
        for filter_name, value in parse_qs(params).items():
            renamed = filter_renames.get(filter_name)
            if renamed is None:
                raise InvalidBlueprintImport(
                    'Error parsing spec for plugin {0}: invalid parameter {1}'
                    .format(name, filter_name))
            filters[renamed] = value

        # In case a mapping for package_version was provided, use it above
        # all other specs.
        if name in mappings:
            filters['package_version'] = [mappings.get(name).get('version')]
        elif name in version_constraints:
            filters[EXTRA_VERSION_CONSTRAINT] = \
                version_constraints.get(name)
        return name, filters

    @staticmethod
    def _create_zip(source, destination, include_folder=True):
        with closing(zipfile.ZipFile(destination, 'w')) as zip_file:
            for root, _, files in os.walk(source):
                for filename in files:
                    file_path = os.path.join(root, filename)
                    source_dir = os.path.dirname(source) if include_folder \
                        else source
                    zip_file.write(
                        file_path, os.path.relpath(file_path, source_dir))
        return destination

    @staticmethod
    def _download_file(url, dest_path, target_filename=None):
        with requests.get(url, stream=True, timeout=10.0) as resp:
            resp.raise_for_status()
            if not target_filename:
                target_filename = os.path.basename(url)
            file_path = os.path.join(dest_path, target_filename)
            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return file_path

    def _upload_missing_plugin(self, name, specifier_set, distribution,
                               dsl_version=None):
        download_error_msg = \
            'Couldn\'t download plugin {}. Please upload the plugin using ' \
            'the console, or cfy plugins upload. Error: {}'

        plugin_target_path = tempfile.mkdtemp()
        plugins_marketplace = self.marketplace_api_url + "/plugins"

        # Get plugin data from marketplace
        plugin = requests.get(f'{plugins_marketplace}?name={name}')
        if not plugin.ok:
            raise InvalidBlueprintImport(download_error_msg.format(
                name, f"plugins catalog unreachable at "
                      f"{plugins_marketplace}: {plugin.text}"))

        if not plugin.json() or not plugin.json().get('items'):
            raise FileNotFoundError()

        plugin_id = plugin.json()['items'][0].get('id')
        logo_url = plugin.json()['items'][0].get('logo_url')

        plugin_versions = requests.get(
            f'{plugins_marketplace}/{plugin_id}/versions')
        if not plugin_versions.ok or not plugin_versions.json() \
                or not plugin_versions.json().get('items'):
            raise FileNotFoundError()

        # Find maximal matching version
        matching_versions = []
        for version in plugin_versions.json().get('items'):
            parsed_version = parse_version(version.get('version'))
            yaml_urls = version.get('yaml_urls')
            wagon_url = self.matching_distro_wagon(version.get('wagon_urls'),
                                                   distribution)
            if parsed_version in specifier_set and yaml_urls and wagon_url:
                matching_versions.append((
                    parsed_version, yaml_urls, wagon_url))
        if not matching_versions:
            raise FileNotFoundError()

        _, yaml_urls, wagon_url = max(matching_versions, key=lambda v: v[0])

        # Download plugin files
        try:
            yaml_url = None
            if dsl_version:
                dsl_version = DSL_VERSION_PREFIX + dsl_version
                for p_yaml in yaml_urls:
                    if p_yaml['dsl_version'] == dsl_version:
                        yaml_url = p_yaml['url']
                        break
            if not dsl_version or not yaml_url:
                yaml_url = yaml_urls[0]['url']
            self._download_file(yaml_url, plugin_target_path)
            self._download_file(wagon_url, plugin_target_path)
            if logo_url:
                self._download_file(logo_url, plugin_target_path, 'icon.png')
        except Exception as e:
            raise InvalidBlueprintImport(download_error_msg.format(name, e))

        plugin_zip = plugin_target_path + '.zip'
        self._create_zip(plugin_target_path, plugin_zip,
                         include_folder=False)
        shutil.rmtree(plugin_target_path)
        self.client.plugins.upload(plugin_zip)
        os.remove(plugin_zip)

    @staticmethod
    def matching_distro_wagon(wagons, distro):
        matching_wagons = [x['url'] for x in wagons
                           if x['release'].lower() == distro
                           or x['release'].lower().startswith('manylinux')]
        if matching_wagons:
            return matching_wagons[0]

    @staticmethod
    def _find_matching_plugin_versions(plugins, specifier_set,
                                       extra_constraint=None):
        plugin_versions = [(i, parse_version(p.package_version))
                           for i, p in enumerate(plugins)]
        matching_versions = [(i, v) for i, v in plugin_versions
                             if v in specifier_set]
        if extra_constraint:
            matching_versions = [(i, v) for i, v in matching_versions
                                 if v in SpecifierSet(extra_constraint)]
        return matching_versions

    def _find_plugin(self, name, filters, dsl_version=None):
        def _get_specifier_set(package_versions):
            # Flat out the versions, in case one of them contains a few
            # operators/specific versions
            _versions = (v for vs in package_versions for v in vs.split(','))
            specs = SpecifierSet()
            for spec in _versions:
                if not spec:
                    raise InvalidSpecifier()
                try:
                    specs &= SpecifierSet(spec)
                except InvalidSpecifier:
                    # If the code below doesn't raise any exception then it's
                    # the case where a version has been provided with no
                    # operator to prefix it.
                    specs &= SpecifierSet(f'==={spec}')
            return specs

        filters['package_name'] = name
        version_specified = 'package_version' in filters
        versions = filters.pop('package_version', [])
        extra_constraint = filters.pop(EXTRA_VERSION_CONSTRAINT, None)
        if not version_specified:
            specifier_set = SpecifierSet()
        else:
            try:
                specifier_set = _get_specifier_set(versions)
            except InvalidSpecifier:
                raise InvalidBlueprintImport(
                    'Specified version param {0} of the plugin {1} are in an '
                    'invalid form. Please refer to the documentation for '
                    'valid forms of versions'.format(versions, name))
        plugins = self.client.plugins.list(**filters)
        if plugins:  # find whether we have a matching version uploaded
            matching_versions = self._find_matching_plugin_versions(
                plugins, specifier_set, extra_constraint)

        if not plugins or not matching_versions:
            try:
                manager_data = self.client.manager.get_managers()[0]
                distribution = f'{manager_data["distribution"]} ' \
                               f'{manager_data["distro_release"]}'.lower()
                self._upload_missing_plugin(name, specifier_set, distribution,
                                            dsl_version)
            except CloudifyClientError as e:
                if e.status_code == 409:
                    # This may happen if we try to auto-upload the same plugin
                    # simultaneously.
                    self._wait_for_matching_plugin_to_upload(
                        filters, plugins, specifier_set, extra_constraint)
                else:
                    raise
            except FileNotFoundError:
                version_message = ''
                if version_specified:
                    version_message = f' with version {specifier_set}'
                raise InvalidBlueprintImport(
                    'Couldn\'t find plugin "{0}"{1} for {2} in the plugins '
                    'catalog. Please upload the plugin using the console, '
                    'or cfy plugins upload'.format(
                        name, version_message, distribution))
            # update matching versions once the plugin uploaded
            plugins = self.client.plugins.list(**filters)
            matching_versions = self._find_matching_plugin_versions(
                plugins, specifier_set, extra_constraint)

        max_item = max(matching_versions, key=lambda i_v: i_v[1])
        return plugins[max_item[0]]

    def _resolve_blueprint_url(self, import_url):
        blueprint_id = import_url.replace(BLUEPRINT_PREFIX, '', 1).strip()
        try:
            blueprint = self.client.blueprints.get(blueprint_id)
        except CloudifyClientError:
            raise InvalidBlueprintImport(
                'Requested blueprint import `{0}` was not found,'
                'please first upload the blueprint with that id.'
                .format(blueprint_id)
            )

        return self._make_blueprint_url(blueprint)

    def _fetch_blueprint_import(self, import_url):
        """
        :param import_url: blueprint id in the catalog.
        :return: Blueprint of type Holder with all imports already resolved.
        """
        import_url = self._resolve_blueprint_url(import_url)
        main_blueprint = super(ResolverWithCatalogSupport, self).\
            fetch_import(import_url)
        merged_blueprint = parser.parse_from_import_blueprint(
            dsl_string=main_blueprint,
            dsl_location=import_url,
            resources_base_path=self.file_server_root,
            resolver=self)
        return merged_blueprint

    def _make_blueprint_url(self, blueprint):
        blueprint_path = os.path.join(
            self.file_server_root,
            FILE_SERVER_BLUEPRINTS_FOLDER,
            blueprint['tenant_name'],
            blueprint.id)
        filename = os.path.join(blueprint_path, blueprint.main_file_name)
        return 'file://{0}'.format(filename)

    @retry(stop_max_attempt_number=120, wait_fixed=1000)
    def _wait_for_matching_plugin_to_upload(
            self, filters, plugins, specifier_set, extra_constraint):
        plugins = self.client.plugins.list(**filters)
        matching_versions = self._find_matching_plugin_versions(
            plugins, specifier_set, extra_constraint)
        assert matching_versions
