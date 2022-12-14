import urllib2
import ast

from ckan.lib.base import c
from ckan import model
from ckan.model import Session, Package
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.helpers import json
from ckan.lib.munge import munge_name

from ckanext.harvest.model import HarvestJob, HarvestObject, HarvestGatherError, \
                                    HarvestObjectError

import logging
log = logging.getLogger(__name__)

from base import HarvesterBase

class CKANHarvester(HarvesterBase):
    '''
    A Harvester for CKAN instances
    '''
    config = None

    api_version = 3
    action_api_version = 3

    def _get_action_api_offset(self):
        return '/api/%d/action' % self.action_api_version

    def _get_search_api_offset(self):
        return '/api/2/search'

    def _get_rest_api_offset(self):
        return '/api/2/rest'

    def _get_content(self, url):
        http_request = urllib2.Request(
            url = url,
        )

	http_request.add_header("User-Agent", "ckanext_harvest")

        api_key = self.config.get('api_key',None)
        if api_key:
            http_request.add_header('Authorization',api_key)

        try:
            http_response = urllib2.urlopen(http_request)
        except urllib2.URLError, e:
            if e.code == 403:
                raise ContentNotFoundError('Package is no longer publicly available, HTTP 403 response for %s' % url)
            else:
                raise ContentFetchError(
                    'Could not fetch url: %s, error: %s' %
                    (url, str(e))
                )
        return http_response.read()

    def _get_group(self, base_url, group_name):
        url = base_url + self._get_action_api_offset() + '/group_show?id=' + munge_name(group_name)
        try:
            content = self._get_content(url)
            return json.loads(content)
        except (ContentFetchError, ValueError):
            log.debug('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote group')

    def _get_organization(self, base_url, org_name):
        url = base_url + self._get_action_api_offset() + '/organization_show?id=' + org_name
        try:
            content = self._get_content(url)
            content_dict = json.loads(content)
            return content_dict['result']
        except (ContentFetchError, ValueError, KeyError):
            log.debug('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote organization')

    def _set_config(self,config_str):
        if config_str:
            self.config = json.loads(config_str)
            if 'api_version' in self.config:
                self.api_version = int(self.config['api_version'])

            log.debug('Using config: %r', self.config)
        else:
            self.config = {}

    def info(self):
        return {
            'name': 'ckan',
            'title': 'CKAN',
            'description': 'Harvests remote CKAN instances',
            'form_config_interface':'Text'
        }

    def validate_config(self,config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'api_version' in config_obj:
                try:
                    int(config_obj['api_version'])
                except ValueError:
                    raise ValueError('api_version must be an integer')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'],list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'],list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model':model,'user':c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        group = get_action('group_show')(context,{'id':group_name})
                    except NotFound,e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'],dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'user' in config_obj:
                # Check if user exists
                context = {'model':model,'user':c.user}
                try:
                    user = get_action('user_show')(context,{'id':config_obj.get('user')})
                except NotFound,e:
                    raise ValueError('User not found')

            for key in ('read_only','force_all'):
                if key in config_obj:
                    if not isinstance(config_obj[key],bool):
                        raise ValueError('%s must be boolean' % key)

        except ValueError,e:
            raise e

        return config


    def gather_stage(self,harvest_job):
        log.debug('In CKANHarvester gather_stage (%s)' % harvest_job.source.url)
        get_all_packages = True
        package_ids = []

        self._set_config(harvest_job.source.config)
        # Check if this source has been harvested before, and when
        previous_jobs = Session.query(HarvestJob) \
                        .filter(HarvestJob.source==harvest_job.source) \
                        .filter(HarvestJob.gather_finished!=None) \
                        .filter(HarvestJob.id!=harvest_job.id) \
                        .order_by(HarvestJob.gather_finished.desc()) \
                        .limit(10)

        previous_job = None
        for prev_job in previous_jobs:
            if(prev_job and not prev_job.gather_errors):
                previous_job = prev_job
                break

        # Get source URL
        base_url = harvest_job.source.url.rstrip('/')
        base_rest_url = base_url + self._get_rest_api_offset()
        base_package_list_url = base_url + self._get_action_api_offset()
        base_search_url = base_url + self._get_search_api_offset()

        if (previous_job):
            if not self.config.get('force_all',False):
                get_all_packages = False
                # Request only the packages modified since last harvest job
                last_time = previous_job.gather_finished.isoformat()
                url = base_search_url + '/revision?since_time=%s' % last_time
                log.debug('Getting package updates since %s' % last_time)
                try:
                    content = self._get_content(url)

                    revision_ids = json.loads(content)
                    if len(revision_ids):
                        for revision_id in revision_ids:
                            url = base_rest_url + '/revision/%s' % revision_id
                            try:
                                content = self._get_content(url)
                            except ContentFetchError,e:
                                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                                continue

                            revision = json.loads(content)
                            for package_id in revision['packages']:
                                if not package_id in package_ids:
                                    package_ids.append(package_id)
                    else:
                        log.info('No packages have been updated on the remote CKAN instance since the last harvest job')
                        return None

                except urllib2.HTTPError,e:
                    if e.getcode() == 400:
                        log.info('CKAN instance %s does not suport revision filtering' % base_url)
                        get_all_packages = True
                    else:
                        self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                        return None



        if get_all_packages:
            # Request all remote packages
            url = base_package_list_url + '/package_list'
            try:
                content = self._get_content(url)
            except ContentFetchError,e:
                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                return None

            package_ids = json.loads(content)['result']

        try:
            object_ids = []
            if len(package_ids):
                for package_id in package_ids:
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid = package_id, job = harvest_job)
                    obj.save()
                    object_ids.append(obj.id)

                return object_ids

            else:
               self._save_gather_error('No packages received for URL: %s' % url,
                       harvest_job)
               return None
        except Exception, e:
            self._save_gather_error('%r'%e.message,harvest_job)


    def fetch_stage(self,harvest_object):
        log.debug('In CKANHarvester fetch_stage')

        self._set_config(harvest_object.job.source.config)

        # Get source URL
        url = harvest_object.source.url.rstrip('/')
        url = url + self._get_action_api_offset() + '/package_show?id=' + harvest_object.guid

        # Get contents
        try:
            content = self._get_content(url)
        except ContentNotFoundError,e:
            # Remove package, as it no longer exists in the source:
            self._remove_package({"id": harvest_object.guid})
            harvest_object.report_status = 'deleted'
            harvest_object.save()
            return True
        except ContentFetchError,e:
            self._save_object_error('Unable to get content for package: %s: %r' % \
                                        (url, e),harvest_object)
            return None
        # Save the fetched contents in the HarvestObject
        harvest_object.content = json.dumps(json.loads(content)['result'])
        harvest_object.save()
        return True

    def import_stage(self,harvest_object):
        log.debug('In CKANHarvester import_stage: %s' % harvest_object.id)

        if(harvest_object.report_status == 'deleted'):
            log.debug('Dataset removed as expected, ignoring import for %s' % harvest_object.id)
            return True

        context = {'model': model, 'session': Session, 'user': self._get_user_name()}
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,
                    harvest_object, 'Import')
            return False

        self._set_config(harvest_object.job.source.config)

        try:
            package_dict = json.loads(harvest_object.content)

            if package_dict.get('type') == 'harvest':
                log.warn('Remote dataset is a harvest source, ignoring...')
                return True

            # Set default tags if needed
            default_tags = self.config.get('default_tags',[])
            if default_tags:
                if not 'tags' in package_dict:
                    package_dict['tags'] = []
                package_dict['tags'].extend([t for t in default_tags if t not in package_dict['tags']])
                for extra_key in package_dict['extras']:
                    if extra_key['key'] == 'tags':
                        extra_key['value'].extend([t for t in default_tags if t not in package_dict['tags']])

            remote_groups = self.config.get('remote_groups', None)

            log.debug('Default tags setup for: %s' % harvest_object.id)

            if not remote_groups in ('only_local', 'create'):
                # Ignore remote groups
                package_dict.pop('groups', None)
            else:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []

                # check if remote groups exist locally, otherwise remove
                validated_groups = []

                for group_name in package_dict['groups']:
                    try:
                        data_dict = {'id': group_name}
                        group = get_action('group_show')(context, data_dict)
                        if self.api_version == 1:
                            validated_groups.append(group['name'])
                        else:
                            validated_groups.append(group['id'])
                    except NotFound, e:
                        log.info('Group %s is not available' % group_name)
                        if remote_groups == 'create':
                            try:
                                group = self._get_group(harvest_object.source.url, group_name)
                            except RemoteResourceError:
                                log.error('Could not get remote group %s' % group_name)
                                continue

                            for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name']:
                                group.pop(key, None)

                            get_action('group_create')(context, group)
                            log.info('Group %s has been newly created' % group_name)
                            if self.api_version == 1:
                                validated_groups.append(group['name'])
                            else:
                                validated_groups.append(group['id'])

                package_dict['groups'] = validated_groups

            log.debug('Starting to get harvest source info for: %s' % harvest_object.id)
            # Local harvest source organization
            source_dataset = get_action('package_show')(context, {'id': harvest_object.source.id})
            local_org = source_dataset.get('owner_org')

            log.debug('Have harvest source info for: %s' % harvest_object.id)

            remote_orgs = self.config.get('remote_orgs', None)
            if not remote_orgs in ('only_local', 'create'):
                # Assign dataset to the source organization
                package_dict['owner_org'] = local_org
            else:
                if not 'owner_org' in package_dict:
                    package_dict['owner_org'] = None

                # check if remote org exist locally, otherwise remove
                validated_org = None
                remote_org = package_dict['owner_org']

                if remote_org:
                    try:
                        data_dict = {'id': remote_org}
                        org = get_action('organization_show')(context, data_dict)
                        validated_org = org['id']
                    except NotFound, e:
                        log.info('Organization %s is not available' % remote_org)
                        if remote_orgs == 'create':
                            try:
                                try:
                                    org = self._get_organization(harvest_object.source.url, remote_org)
                                except RemoteResourceError:
                                    # fallback if remote CKAN exposes organizations as groups
                                    # this especially targets older versions of CKAN
                                    org = self._get_group(harvest_object.source.url, remote_org)

                                for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name', 'type']:
                                    org.pop(key, None)
                                get_action('organization_create')(context, org)
                                log.info('Organization %s has been newly created' % remote_org)
                                validated_org = org['id']
                            except (RemoteResourceError, ValidationError):
                                log.error('Could not get remote org %s' % remote_org)

                package_dict['owner_org'] = validated_org or local_org

            log.debug('Organization owner setup for: %s' % harvest_object.id)
            # Set default groups if needed
            default_groups = self.config.get('default_groups', [])
            if default_groups:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []
                package_dict['groups'].extend([g for g in default_groups if g not in package_dict['groups']])

	    # Download full metadata link if applicable
            harvest_source_id = None
            for key in package_dict['extras']:
                if key['key'] == 'harvest_object_id':
                    harvest_source_id = key['value']
            if harvest_source_id != None:
                # Store the full metadata link
                url = harvest_object.source.url.rstrip('/') + '/harvest/object/' + harvest_source_id
                full_metadata = self._get_content(url)
                harvest_object.content = full_metadata
                harvest_object.save()

            log.debug('Got full metadata content for: %s' % harvest_object.id)
            # Find any extras whose values are not strings and try to convert
            # them to strings, as non-string extras are not allowed anymore in
            # CKAN 2.0.
            for key in package_dict['extras']:
                if not isinstance(key['value'], basestring):
                    try:
                        key['value'] = json.dumps(key['value'])
                    except TypeError:
                        # If converting to a string fails, just delete it.
                        del key

            # Flip extras to correct format
            package_dict_extras = {}
            for key in package_dict['extras']:
                package_dict_extras[key['key']] = key['value']
            package_dict['extras'] = package_dict_extras

            # Flip tags to correct format
            package_dict_tags = []
            for key in package_dict['tags']:
                package_dict_tags.append(key['name'])
            package_dict['tags'] = package_dict_tags

            # Update old harvest information with current harvest info
            package_dict['extras']['harvest_object_id'] = harvest_object.id
            package_dict['extras']['harvest_source_id']= harvest_object.job.source.id
            package_dict['extras']['harvest_source_title'] = harvest_object.job.source.title

            # Allow CKAN automation to handle name creation
            if('name' in package_dict):
                del package_dict['name']

            if('bureauCode' in package_dict['extras']):
                package_dict['extras']['bureauCode'] = ast.literal_eval(package_dict['extras']['bureauCode'])
	    if('programCode' in package_dict['extras']):
                package_dict['extras']['programCode'] = ast.literal_eval(package_dict['extras']['programCode'])

            log.debug('Import cleanup complete for: %s' % harvest_object.id)

            # Set default extras if needed
            default_extras = self.config.get('default_extras',{})
            if default_extras:
                override_extras = self.config.get('override_extras',False)
                if not 'extras' in package_dict:
                    package_dict['extras'] = {}
                for key,value in default_extras.iteritems():
                    if not key in package_dict['extras'] or override_extras:
                        # Look for replacement strings
                        if isinstance(value,basestring):
                            value = value.format(harvest_source_id=harvest_object.job.source.id,
                                     harvest_source_url=harvest_object.job.source.url.strip('/'),
                                     harvest_source_title=harvest_object.job.source.title,
                                     harvest_job_id=harvest_object.job.id,
                                     harvest_object_id=harvest_object.id,
                                     dataset_id=package_dict['id'])

                        package_dict['extras'][key] = value

            # Clear remote url_type for resources (eg datastore, upload) as we
            # are only creating normal resources with links to the remote ones
            for resource in package_dict.get('resources', []):
                resource.pop('url_type', None)

            result = self._create_or_update_package(package_dict,harvest_object)

            if result and self.config.get('read_only',False) == True:

                package = model.Package.get(package_dict['id'])

                # Clear default permissions
                model.clear_user_roles(package)

                # Setup harvest user as admin
                user_name = self.config.get('user',u'harvest')
                user = model.User.get(user_name)
                pkg_role = model.PackageRole(package=package, user=user, role=model.Role.ADMIN)

                # Other users can only read
                for user_name in (u'visitor',u'logged_in'):
                    user = model.User.get(user_name)
                    pkg_role = model.PackageRole(package=package, user=user, role=model.Role.READER)

            log.debug('Import complete for: %s' % harvest_object.id)

            return True
        except ValidationError,e:
            self._save_object_error('Invalid package with GUID %s: %r' % (harvest_object.guid, e.error_dict),
                    harvest_object, 'Import')
        except Exception, e:
            self._save_object_error('%r'%e,harvest_object,'Import')
            log.debug('ImportError %r' % e)

class ContentFetchError(Exception):
    pass

class ContentNotFoundError(Exception):
    pass

class RemoteResourceError(Exception):
    pass
