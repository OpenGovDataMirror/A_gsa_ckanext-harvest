import hashlib
import logging
import datetime
import json

from pylons import config
from paste.deploy.converters import asbool
from sqlalchemy import and_, or_, exc
import ckan.lib.search as search
from ckan.lib.search.index import PackageSearchIndex
from ckan.plugins import PluginImplementations
from ckan.logic import get_action
from ckanext.harvest.interfaces import IHarvester
from ckan.lib.search.common import SearchIndexError, make_connection
from ckan.model import Package
from ckan import logic
from ckan.plugins import toolkit
from ckan.logic import NotFound, check_access
from ckanext.harvest.plugin import DATASET_TYPE_NAME
from ckanext.harvest.queue import get_gather_publisher, resubmit_jobs
from ckanext.harvest.model import HarvestSource, HarvestJob, HarvestObject, HarvestSystemInfo
from ckanext.harvest.logic import HarvestJobExists
from ckanext.harvest.logic.action.get import harvest_source_show, harvest_job_list, _get_sources_for_user
import ckan.lib.mailer as mailer
from ckanext.harvest.logic.dictization import harvest_job_dictize

log = logging.getLogger(__name__)


def harvest_source_update(context, data_dict):
    '''
    Updates an existing harvest source

    This method just proxies the request to package_update,
    which will create a harvest_source dataset type and the
    HarvestSource object. All auth checks and validation will
    be done there .We only make sure to set the dataset type

    Note that the harvest source type (ckan, waf, csw, etc)
    is now set via the source_type field.

    :param id: the name or id of the harvest source to update
    :type id: string
    :param url: the URL for the harvest source
    :type url: string
    :param name: the name of the new harvest source, must be between 2 and 100
        characters long and contain only lowercase alphanumeric characters
    :type name: string
    :param title: the title of the dataset (optional, default: same as
        ``name``)
    :type title: string
    :param notes: a description of the harvest source (optional)
    :type notes: string
    :param source_type: the harvester type for this source. This must be one
        of the registerd harvesters, eg 'ckan', 'csw', etc.
    :type source_type: string
    :param frequency: the frequency in wich this harvester should run. See
        ``ckanext.harvest.model`` source for possible values. Default is
        'MANUAL'
    :type frequency: string
    :param config: extra configuration options for the particular harvester
        type. Should be a serialized as JSON. (optional)
    :type config: string


    :returns: the newly created harvest source
    :rtype: dictionary

    '''
    log.info('Updating harvest source: %r', data_dict)

    data_dict['type'] = DATASET_TYPE_NAME

    context['extras_as_string'] = True
    source = logic.get_action('package_update')(context, data_dict)

    return source


def harvest_source_clear(context, data_dict):
    '''
    Clears all datasets, jobs and objects related to a harvest source, but keeps the source itself.
    This is useful to clean history of long running harvest sources to start again fresh.

    :param id: the id of the harvest source to clear
    :type id: string

    '''
    check_access('harvest_source_clear', context, data_dict)

    harvest_source_id = data_dict.get('id', None)

    source = HarvestSource.get(harvest_source_id)
    if not source:
        log.error('Harvest source %s does not exist', harvest_source_id)
        raise NotFound('Harvest source %s does not exist' % harvest_source_id)

    harvest_source_id = source.id

    # Clear all datasets from this source from the index
    harvest_source_index_clear(context, data_dict)

    model = context['model']

    sql = "select id from related where id in (select related_id from related_dataset where dataset_id in (select package_id from harvest_object where harvest_source_id = '{harvest_source_id}'));".format(
        harvest_source_id=harvest_source_id)
    result = model.Session.execute(sql)
    ids = []
    for row in result:
        ids.append(row[0])
    related_ids = "('" + "','".join(ids) + "')"

    sql = '''begin;
    update package set state = 'to_delete' where id in (select package_id from harvest_object where harvest_source_id = '{harvest_source_id}');'''.format(
        harvest_source_id=harvest_source_id)

    # CKAN-2.3 or above: delete resource views, resource revisions & resources
    if toolkit.check_ckan_version(min_version='2.3'):
        sql += '''
        delete from resource_view where resource_id in (select id from resource where package_id in (select id from package where state = 'to_delete' ));
        delete from resource_revision where package_id in (select id from package where state = 'to_delete' );
        delete from resource where package_id in (select id from package where state = 'to_delete' );
        '''
    # Backwards-compatibility: support ResourceGroup (pre-CKAN-2.3)
    else:
        sql += '''
        delete from resource_revision where resource_group_id in
        (select id from resource_group where package_id in
        (select id from package where state = 'to_delete'));
        delete from resource where resource_group_id in
        (select id from resource_group where package_id in
        (select id from package where state = 'to_delete'));
        delete from resource_group_revision where package_id in
        (select id from package where state = 'to_delete');
        delete from resource_group where package_id  in
        (select id from package where state = 'to_delete');
        '''
    sql += '''
    delete from harvest_object_error where harvest_object_id in (select id from harvest_object where harvest_source_id = '{harvest_source_id}');
    delete from harvest_object_extra where harvest_object_id in (select id from harvest_object where harvest_source_id = '{harvest_source_id}');
    delete from harvest_object where harvest_source_id = '{harvest_source_id}';
    delete from harvest_gather_error where harvest_job_id in (select id from harvest_job where source_id = '{harvest_source_id}');
    delete from harvest_job where source_id = '{harvest_source_id}';
    delete from package_role where package_id in (select id from package where state = 'to_delete' );

    DELETE FROM user_object_role
    USING user_object_role u
    LEFT JOIN package_role p
    ON u.id = p.user_object_role_id
    WHERE
    p.user_object_role_id IS NULL
    AND u.context = 'Package'
    AND user_object_role.id = u.id
    ;

    delete from package_tag_revision where package_id in (select id from package where state = 'to_delete');
    delete from member_revision where table_id in (select id from package where state = 'to_delete');
    delete from package_extra_revision where package_id in (select id from package where state = 'to_delete');
    delete from package_revision where id in (select id from package where state = 'to_delete');
    delete from package_tag where package_id in (select id from package where state = 'to_delete');
    delete from package_extra where package_id in (select id from package where state = 'to_delete');
    delete from member where table_id in (select id from package where state = 'to_delete');
    delete from related_dataset where dataset_id in (select id from package where state = 'to_delete');
    delete from related where id in {related_ids};
    delete from package where id in (select id from package where state = 'to_delete');
    commit;
    '''.format(
        harvest_source_id=harvest_source_id, related_ids=related_ids)

    model.Session.execute(sql)

    # Refresh the index for this source to update the status object
    get_action('harvest_source_reindex')(context, {'id': harvest_source_id})

    return {'id': harvest_source_id}


def harvest_source_index_clear(context, data_dict):
    check_access('harvest_source_clear', context, data_dict)
    harvest_source_id = data_dict.get('id', None)

    source = HarvestSource.get(harvest_source_id)
    if not source:
        log.error('Harvest source %s does not exist', harvest_source_id)
        raise NotFound('Harvest source %s does not exist' % harvest_source_id)

    harvest_source_id = source.id

    conn = make_connection()
    query = ''' +%s:"%s" +site_id:"%s" ''' % ('harvest_source_id', harvest_source_id,
                                              config.get('ckan.site_id'))
    try:
        conn.delete_query(query)
        if asbool(config.get('ckan.search.solr_commit', 'true')):
            conn.commit()
    except Exception, e:
        log.exception(e)
        raise SearchIndexError(e)
    finally:
        conn.close()

    return {'id': harvest_source_id}


def harvest_objects_import(context, data_dict):
    '''
        Reimports the current harvest objects
        It performs the import stage with the last fetched objects, optionally
        belonging to a certain source.
        Please note that no objects will be fetched from the remote server.
        It will only affect the last fetched objects already present in the
        database.
    '''
    log.info('Harvest objects import: %r', data_dict)
    check_access('harvest_objects_import', context, data_dict)

    model = context['model']
    session = context['session']
    source_id = data_dict.get('source_id', None)
    harvest_object_id = data_dict.get('harvest_object_id', None)
    package_id_or_name = data_dict.get('package_id', None)

    segments = context.get('segments', None)

    join_datasets = context.get('join_datasets', True)

    if source_id:
        source = HarvestSource.get(source_id)
        if not source:
            log.error('Harvest source %s does not exist', source_id)
            raise NotFound('Harvest source %s does not exist' % source_id)

        if not source.active:
            log.warn('Harvest source %s is not active.', source_id)
            raise Exception('This harvest source is not active')

        last_objects_ids = session.query(HarvestObject.id) \
            .join(HarvestSource) \
            .filter(HarvestObject.source == source) \
            .filter(HarvestObject.current == True)

    elif harvest_object_id:
        last_objects_ids = session.query(HarvestObject.id) \
            .filter(HarvestObject.id == harvest_object_id)
    elif package_id_or_name:
        last_objects_ids = session.query(HarvestObject.id) \
            .join(Package) \
            .filter(HarvestObject.current == True) \
            .filter(Package.state == u'active') \
            .filter(or_(Package.id == package_id_or_name,
                        Package.name == package_id_or_name))
        join_datasets = False
    else:
        last_objects_ids = session.query(HarvestObject.id) \
            .filter(HarvestObject.current == True)

    if join_datasets:
        last_objects_ids = last_objects_ids.join(Package) \
            .filter(Package.state == u'active')

    last_objects_ids = last_objects_ids.all()

    last_objects_count = 0

    for obj_id in last_objects_ids:
        if segments and str(hashlib.md5(obj_id[0]).hexdigest())[0] not in segments:
            continue

        obj = session.query(HarvestObject).get(obj_id)

        for harvester in PluginImplementations(IHarvester):
            if harvester.info()['name'] == obj.source.type:
                if hasattr(harvester, 'force_import'):
                    harvester.force_import = True
                harvester.import_stage(obj)
                break
        last_objects_count += 1
    log.info('Harvest objects imported: %s', last_objects_count)
    return last_objects_count


def _caluclate_next_run(frequency):
    now = datetime.datetime.utcnow()
    if frequency == 'ALWAYS':
        return now
    if frequency == 'WEEKLY':
        return now + datetime.timedelta(weeks=1)
    if frequency == 'BIWEEKLY':
        return now + datetime.timedelta(weeks=2)
    if frequency == 'DAILY':
        return now + datetime.timedelta(days=1)
    if frequency == 'MONTHLY':
        if now.month in (4, 6, 9, 11):
            days = 30
        elif now.month == 2:
            if now.year % 4 == 0:
                days = 29
            else:
                days = 28
        else:
            days = 31
        return now + datetime.timedelta(days=days)
    raise Exception('Frequency {freq} not recognised'.format(freq=frequency))


def _make_scheduled_jobs(context, data_dict):
    data_dict = {'only_to_run': True,
                 'only_active': True}
    sources = _get_sources_for_user(context, data_dict)

    for source in sources:
        data_dict = {'source_id': source.id}
        try:
            get_action('harvest_job_create')(context, data_dict)
        except HarvestJobExists, e:
            log.info('Trying to rerun job for %s skipping' % source.id)

        source.next_run = _caluclate_next_run(source.frequency)
        source.save()

def set_harvest_system_info(context, key, value):
    ''' save data in the harvest_system_info table '''

    model = context['model']

    obj = None
    try:
        obj = model.Session.query(HarvestSystemInfo).filter_by(key=key).first()
    except exc.ProgrammingError:
        log.debug('No HarvestSystemInfo table. {0} skipped.'.format(key))
        model.Session.rollback()
        return

    if obj:
        obj.value = unicode(value)
    else:
        obj = HarvestSystemInfo()
        obj.key = key
        obj.value = value
    model.Session.add(obj)
    model.Session.commit()


def harvest_jobs_run(context, data_dict):
    log.info('Harvest job run: %r', data_dict)
    check_access('harvest_jobs_run', context, data_dict)

    model = context['model']
    session = context['session']

    source_id = data_dict.get('source_id', None)

    if not source_id:
        _make_scheduled_jobs(context, data_dict)

    context['return_objects'] = False

    set_harvest_system_info(context, 'last_run_time', datetime.datetime.utcnow() )

    # Flag finished jobs as such
    jobs = harvest_job_list(context, {'source_id': source_id, 'status': u'Running'})
    if len(jobs):
        package_index = PackageSearchIndex()
        for job in jobs:
            if job['gather_finished']:
                objects = session.query(HarvestObject.id) \
                    .filter(HarvestObject.harvest_job_id == job['id']) \
                    .filter(and_(
                            (HarvestObject.state != u'COMPLETE'),
                            (HarvestObject.state != u'ERROR'),
                            (HarvestObject.state != u'STUCK')
                            )) \
                    .order_by(HarvestObject.import_finished.desc())

                if objects.count() == 0:
                    msg = '' # message to be emailed for fixed packages
                    job_obj = HarvestJob.get(job['id'])

                    # look for packages with no current harvest objects
                    # and relink them by marking last complete harvest object
                    # current
                    pkgs_no_current = set()
                    sql = '''
                        WITH temp_ho AS (
                          SELECT DISTINCT package_id
                                  FROM harvest_object
                                  WHERE current
                        )
                        SELECT DISTINCT harvest_object.package_id
                        FROM harvest_object
                        LEFT JOIN temp_ho
                        ON harvest_object.package_id = temp_ho.package_id
                        JOIN package
                        ON harvest_object.package_id = package.id
                        WHERE
                            package.state = 'active'
                        AND
                            temp_ho.package_id IS NULL
                        AND
                            harvest_object.state = 'COMPLETE'
                        AND
                            harvest_object.harvest_source_id = :harvest_source_id
                        '''
                    results = model.Session.execute(sql,
                            {'harvest_source_id': job_obj.source_id})

                    for row in results:
                        pkgs_no_current.add(row['package_id'])
                    if len(pkgs_no_current) > 0:
                        log_message = '%s packages to be relinked for ' \
                                'source %s' % (len(pkgs_no_current),
                                job_obj.source_id)
                        msg += log_message + '\n'
                        log.info(log_message)

                    # set last complete harvest object to be current
                    sql = '''
                        UPDATE harvest_object
                        SET current = 't'
                        WHERE
                            package_id = :id
                        AND
                            state = 'COMPLETE'
                        AND
                            import_finished = (
                                SELECT MAX(import_finished)
                                FROM harvest_object
                                WHERE
                                    state = 'COMPLETE'
                                AND
                                    package_id = :id
                            )
                        RETURNING 1
                    '''
                    for id in pkgs_no_current:
                        result = model.Session.execute(sql, {'id': id}).fetchall()
                        model.Session.commit()
                        if result:
                            search.rebuild(id)
                            log_message = '%s relinked' % id
                            msg += log_message + '\n'
                            log.info(log_message)
                        else:
                            log_message = '%s has no valid harvest object.' % id
                            msg += log_message + '\n'
                            log.info(log_message)

                    # look for packages with no harvest object and remove them
                    pkgs_no_harvest_object = set()
                    source_dataset = model.Package.get(job_obj.source_id)
                    owner_org = source_dataset.owner_org
                    sql = '''
                        SELECT package.id
                        FROM package
                        LEFT JOIN harvest_object
                        ON package.id = harvest_object.package_id
                        LEFT JOIN package_extra
                        ON package.id = package_extra.package_id
                        AND package_extra.key = 'metadata-source'
                        AND package_extra.value = 'dms'
                        WHERE
                            harvest_object.package_id is null
                        AND
                            package_extra.package_id is null
                        AND
                            package.type='dataset'
                        AND
                            package.state='active'
                        AND
                            package.owner_org=:owner_org
                    '''
                    results = model.Session.execute(sql,
                            {'owner_org': owner_org})

                    for row in results:
                        pkgs_no_harvest_object.add(row['id'])
                    if len(pkgs_no_harvest_object) > 0:
                        log_message = '%s packages to be removed for source %s' % (
                                len(pkgs_no_harvest_object),
                                job_obj.source_id
                        )
                        msg += log_message + '\n'
                        log.info(log_message)

                    for id in pkgs_no_harvest_object:
                        try:
                            logic.get_action('package_delete')(context,
                                    {"id": id})
                        except Exception, e:
                            log_message = 'Error deleting %s' % id
                            msg += log_message + '\n'
                            log.info(log_message)
                        else:
                            log_message = '%s removed' % id
                            msg += log_message + '\n'
                            log.info(log_message)

                    # email a list of fixed packages
                    if msg:
                        email_address = config.get('email_to')
                        email = {'recipient_name': email_address,
                                 'recipient_email': email_address,
                                 'subject': 'Packages fixed ' + \
                                        str(datetime.datetime.now()),
                                 'body': msg,
                                 }
                        try:
                            mailer.mail_recipient(**email)
                        except Exception, e:
                            log.error('Error: %s; email: %s' % (e, email))


                    # finally we can call this job finished
                    job_obj.status = u'Finished'
                    last_object = session.query(HarvestObject) \
                        .filter(HarvestObject.harvest_job_id == job['id']) \
                        .filter(HarvestObject.import_finished != None) \
                        .order_by(HarvestObject.import_finished.desc()) \
                        .first()
                    if last_object and last_object.import_finished:
                        job_obj.finished = last_object.import_finished
                    else:
                        job_obj.finished = datetime.datetime.utcnow()
                    job_obj.save()

                    # recreate job for datajson collection or the like.
                    source = job_obj.source
                    source_config = json.loads(source.config or '{}')
                    datajson_collection = source_config.get(
                        'datajson_collection')
                    if datajson_collection == 'parents_run':
                        new_job = HarvestJob()
                        new_job.source = source
                        new_job.save()
                        source_config['datajson_collection'] = 'children_run'
                        source.config = json.dumps(source_config)
                        source.save()
                    elif datajson_collection:
                        # reset the key if 'children_run', or anything.
                        source_config.pop("datajson_collection", None)
                        source.config = json.dumps(source_config)
                        source.save()

                    if config.get('ckanext.harvest.email', 'on') == 'on':
                        # email body

                        sql = '''select name from package where id = :source_id;'''

                        q = model.Session.execute(sql, {'source_id': job_obj.source_id})

                        for row in q:
                            harvest_name = str(row['name'])

                        job_url = config.get('ckan.site_url') + '/harvest/' + harvest_name + '/job/' + job_obj.id

                        msg = 'Here is the summary of latest harvest job for your organization in Data.gov\n\n'

                        sql = '''select g.title as org, s.title as job_title from member m
                               join public.group g on m.group_id = g.id
                               join harvest_source s on s.id = m.table_id
                               where table_id = :source_id;'''

                        q = model.Session.execute(sql, {'source_id': job_obj.source_id})

                        for row in q:
                            msg += 'Organization: ' + str(row['org']) + '\n\n'
                            msg += 'Harvest Job Title: ' + str(row['job_title']) + '\n\n'

                        msg += 'Date of Harvest: ' + str(job_obj.created) + ' GMT\n\n'

                        out = {
                            'last_job': None,
                        }

                        out['last_job'] = harvest_job_dictize(job_obj, context)

                        msg += 'Records in Error: ' + str(out['last_job']['stats'].get('errored', 0)) + '\n'
                        msg += 'Records Added: ' + str(out['last_job']['stats'].get('added', 0)) + '\n'
                        msg += 'Records Updated: ' + str(out['last_job']['stats'].get('updated', 0)) + '\n'
                        msg += 'Records Deleted: ' + str(out['last_job']['stats'].get('deleted', 0)) + '\n\n'

                        obj_error = ''
                        job_error = ''
                        all_updates = ''

                        sql = '''select hoe.message as msg from harvest_object ho
                              inner join harvest_object_error hoe on hoe.harvest_object_id = ho.id
                              where ho.harvest_job_id = :job_id;'''

                        q = model.Session.execute(sql, {'job_id' : job_obj.id})
                        for row in q:
                            obj_error += row['msg'] + '\n'

                        #get all packages added, updated and deleted by harvest job
                        sql = '''select ho.package_id as ho_package_id, ho.harvest_source_id, ho.report_status as ho_package_status, package.title as package_title
                                from harvest_object ho
                                inner join package on package.id = ho.package_id
                                where ho.harvest_job_id = :job_id and (ho.report_status = 'added' or ho.report_status = 'updated' or ho.report_status = 'deleted')
                                order by ho.report_status ASC;'''

                        q = model.Session.execute(sql, {'job_id': job_obj.id})
                        for row in q:
                            if row['ho_package_status'] is not None and row['ho_package_id'] is not None and row['package_title'] is not None:
                                all_updates += row['ho_package_status'] + ' , ' + row['ho_package_id'] + ', ' + row['package_title'] + '\n'

                        if(all_updates != ''):
                            msg += 'Summary\n\n' + all_updates + '\n\n'

                        # log.info('message in email:',all_updates)
                        sql = '''select message from harvest_gather_error where harvest_job_id = :job_id; '''
                        q = model.Session.execute(sql, {'job_id' : job_obj.id})
                        for row in q:
                            job_error += row['message'] + '\n'

                        if (obj_error != '' or job_error != ''):
                            msg += 'Error Summary\n\n'

                        if (obj_error != ''):
                            msg += 'Document Error\n' + obj_error + '\n\n'

                        if (job_error != ''):
                            msg += 'Job Errors\n' + job_error + '\n\n'

                        msg += '\n--\nYou are receiving this email because you are currently the administrator for your organization in Data.gov. Please do not reply to this email as it was sent from a non-monitored address. Please feel free to contact us at www.data.gov/contact for any questions or feedback.'
                        msg += '\n\nIf you have an admin/editor account in Data.gov catalog, you can view the detailed job report at the following url. You will need to log in first using link https://catalog.data.gov/user/login, then go to this url:'
                        msg += '\n\nhttps://admin-' + job_url

                        # get recipients
                        sql = '''select group_id from member where table_id = :source_id;'''
                        q = model.Session.execute(sql, {'source_id': job_obj.source_id})

                        for row in q:
                            all_emails = []

                            # emails from org admin
                            sql = '''select email from public.user u
                                  join member m on m.table_id = u.id
                                  where m.capacity = 'admin' and m.state = 'active' and u.state = 'active' and m.group_id = :group_id;'''
                            q1 = model.Session.execute(sql, {'group_id': row['group_id']})
                            for row1 in q1:
                                _email = str(row1['email']).lower()
                                if _email:
                                    all_emails.append(_email)

                            # emails from org email_list
                            sql = '''SELECT value FROM group_extra
                                   WHERE state = 'active' AND key = 'email_list'
                                   AND group_id = :group_id'''
                            result = model.Session.execute(sql,
                                                           {'group_id': row['group_id']}).fetchone()

                            if result:
                                org_emails = result[0].strip()
                                if org_emails:
                                    org_email_list = org_emails.replace(';', ' ').replace(',', ' ').split()
                                    for org_email in org_email_list:
                                        all_emails.append(org_email.lower())

                            if all_emails:
                                email = {
                                    'recipient_emails': all_emails,
                                     'subject': 'Data.gov Latest Harvest Job Report for ' + harvest_name.capitalize(),
                                     'body': msg
                                }
                                try:
                                    mailer.bcc_recipients(**email)
                                except Exception:
                                    pass

                    # Reindex the harvest source dataset so it has the latest
                    # status
                    # get_action('harvest_source_reindex')(context,
                    #     {'id': job_obj.source.id})
                    if 'extras_as_string' in context:
                        del context['extras_as_string']
                    context.update({'validate': False, 'ignore_auth': True})
                    package_dict = logic.get_action('package_show')(context,
                                                                    {'id': job_obj.source.id})

                    if package_dict:
                        package_index.index_package(package_dict)

    # Check if there are pending harvest jobs
    jobs = harvest_job_list(context, {'source_id': source_id, 'status': u'New'})
    if len(jobs) == 0:
        log.info('No new harvest jobs.')

    # Send each job to the gather queue
    publisher = get_gather_publisher()
    sent_jobs = []
    for job in jobs:
        context['detailed'] = False
        source = harvest_source_show(context, {'id': job['source_id']})
        # source = harvest_source_show(context,{'id':source_id})
        if source['active']:
            job_obj = HarvestJob.get(job['id'])
            job_obj.status = job['status'] = u'Running'
            job_obj.save()
            publisher.send({'harvest_job_id': job['id']})
            log.info('Sent job %s to the gather queue' % job['id'])
            sent_jobs.append(job)

    publisher.close()

    return sent_jobs


@logic.side_effect_free
def harvest_sources_reindex(context, data_dict):
    '''
        Reindexes all harvest source datasets with the latest status
    '''
    log.info('Reindexing all harvest sources')
    check_access('harvest_sources_reindex', context, data_dict)

    model = context['model']

    packages = model.Session.query(model.Package) \
        .filter(model.Package.type == DATASET_TYPE_NAME) \
        .filter(model.Package.state == u'active') \
        .all()

    package_index = PackageSearchIndex()

    reindex_context = {'defer_commit': True}
    for package in packages:
        get_action('harvest_source_reindex')(reindex_context, {'id': package.id})

    package_index.commit()

    return True


@logic.side_effect_free
def harvest_source_reindex(context, data_dict):
    '''Reindex a single harvest source'''

    harvest_source_id = logic.get_or_bust(data_dict, 'id')
    defer_commit = context.get('defer_commit', False)

    if 'extras_as_string' in context:
        del context['extras_as_string']
    context.update({'ignore_auth': True})
    package_dict = logic.get_action('harvest_source_show')(context,
                                                           {'id': harvest_source_id})
    log.debug('Updating search index for harvest source {0}'.format(harvest_source_id))

    # Remove configuration values
    new_dict = {}
    if package_dict.get('config'):
        config = json.loads(package_dict['config'])
        for key, value in package_dict.iteritems():
            if key not in config:
                new_dict[key] = value
    package_index = PackageSearchIndex()
    package_index.index_package(new_dict, defer_commit=defer_commit)

    return True

