import os
import uuid
from Queue import Empty
import argparse
import json
import logging
import sys
from multiprocessing import Queue, Process
import time_uuid

import datetime
from cloghandler import ConcurrentRotatingFileHandler
import requests
import traceback
import redis
import time
from sys import platform as _platform

import signal

from requests.auth import HTTPBasicAuth
from usergrid import UsergridQueryIterator
import urllib3
import urllib
import urlparse

__author__ = 'Jeff West @ ApigeeCorporation'

ECID = str(uuid.uuid4())
key_version = 'v4'

logger = logging.getLogger('GraphMigrator')
worker_logger = logging.getLogger('Worker')
collection_worker_logger = logging.getLogger('CollectionWorker')
error_logger = logging.getLogger('ErrorLogger')
audit_logger = logging.getLogger('AuditLogger')
status_logger = logging.getLogger('StatusLogger')

urllib3.disable_warnings()

DEFAULT_CREATE_APPS = False
DEFAULT_RETRY_SLEEP = 10
DEFAULT_PROCESSING_SLEEP = 1

queue = Queue()
QSIZE_OK = False

try:
    queue.qsize()
    QSIZE_OK = True
except:
    pass

session_source = requests.Session()
session_target = requests.Session()

cache = None


def total_seconds(td):
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10 ** 6) / 10 ** 6


def init_logging(stdout_enabled=True):
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.getLevelName(config.get('log_level', 'INFO')))

    # root_logger.setLevel(logging.WARN)

    logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.ERROR)
    logging.getLogger('boto').setLevel(logging.ERROR)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.WARN)

    log_formatter = logging.Formatter(
            fmt='%(asctime)s | ' + ECID + ' | %(name)s | %(processName)s | %(levelname)s | %(message)s',
            datefmt='%m/%d/%Y %I:%M:%S %p')

    stdout_logger = logging.StreamHandler(sys.stdout)
    stdout_logger.setFormatter(log_formatter)
    root_logger.addHandler(stdout_logger)

    if stdout_enabled:
        stdout_logger.setLevel(logging.getLevelName(config.get('log_level', 'INFO')))

    # base log file

    log_file_name = '%s/migrator.log' % config.get('log_dir')

    # ConcurrentRotatingFileHandler
    rotating_file = ConcurrentRotatingFileHandler(filename=log_file_name,
                                                  mode='a',
                                                  maxBytes=404857600,
                                                  backupCount=0)
    rotating_file.setFormatter(log_formatter)
    rotating_file.setLevel(logging.INFO)

    root_logger.addHandler(rotating_file)

    error_log_file_name = '%s/migrator_errors.log' % config.get('log_dir')
    error_rotating_file = ConcurrentRotatingFileHandler(filename=error_log_file_name,
                                                        mode='a',
                                                        maxBytes=404857600,
                                                        backupCount=0)
    error_rotating_file.setFormatter(log_formatter)
    error_rotating_file.setLevel(logging.ERROR)

    root_logger.addHandler(error_rotating_file)


entity_name_map = {
    'users': 'username'
}

config = {}

# URL Templates for Usergrid
org_management_app_url_template = "{api_url}/management/organizations/{org}/applications?client_id={client_id}&client_secret={client_secret}"
org_management_url_template = "{api_url}/management/organizations/{org}/applications?client_id={client_id}&client_secret={client_secret}"
org_url_template = "{api_url}/{org}?client_id={client_id}&client_secret={client_secret}"
app_url_template = "{api_url}/{org}/{app}?client_id={client_id}&client_secret={client_secret}"
collection_url_template = "{api_url}/{org}/{app}/{collection}?client_id={client_id}&client_secret={client_secret}"
collection_query_url_template = "{api_url}/{org}/{app}/{collection}?ql={ql}&client_id={client_id}&client_secret={client_secret}&limit={limit}"
collection_graph_url_template = "{api_url}/{org}/{app}/{collection}?client_id={client_id}&client_secret={client_secret}&limit={limit}"
connection_query_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}/{verb}?client_id={client_id}&client_secret={client_secret}"
connecting_query_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}/connecting/{verb}?client_id={client_id}&client_secret={client_secret}"
connection_create_by_uuid_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}/{verb}/{target_uuid}?client_id={client_id}&client_secret={client_secret}"
connection_create_by_name_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}/{verb}/{target_type}/{target_name}?client_id={client_id}&client_secret={client_secret}"
get_entity_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}?client_id={client_id}&client_secret={client_secret}&connections=none"
get_entity_url_with_connections_template = "{api_url}/{org}/{app}/{collection}/{uuid}?client_id={client_id}&client_secret={client_secret}"
put_entity_url_template = "{api_url}/{org}/{app}/{collection}/{uuid}?client_id={client_id}&client_secret={client_secret}"

user_credentials_url_template = "{api_url}/{org}/{app}/users/{uuid}/credentials"

ignore_collections = ['activities', 'queues', 'events', 'notifications']


class StatusListener(Process):
    def __init__(self, status_queue, worker_queue):
        super(StatusListener, self).__init__()
        self.status_queue = status_queue
        self.worker_queue = worker_queue

    def run(self):
        keep_going = True

        org_results = {
            'name': config.get('org'),
            'apps': {},
        }

        empty_count = 0

        while keep_going:

            try:
                app, collection, status_map = self.status_queue.get(timeout=60)
                status_logger.info('Received status update for app/collection: [%s / %s]' % (app, collection))
                empty_count = 0
                org_results['summary'] = {
                    'max_created': -1,
                    'max_modified': -1,
                    'min_created': 1584946416000,
                    'min_modified': 1584946416000,
                    'count': 0,
                    'bytes': 0
                }

                if app not in org_results['apps']:
                    org_results['apps'][app] = {
                        'collections': {}
                    }

                org_results['apps'][app]['collections'].update(status_map)

                try:
                    for app, app_data in org_results['apps'].iteritems():
                        app_data['summary'] = {
                            'max_created': -1,
                            'max_modified': -1,
                            'min_created': 1584946416000,
                            'min_modified': 1584946416000,
                            'count': 0,
                            'bytes': 0
                        }

                        if 'collections' in app_data:
                            for collection, collection_data in app_data['collections'].iteritems():

                                app_data['summary']['count'] += collection_data['count']
                                app_data['summary']['bytes'] += collection_data['bytes']

                                org_results['summary']['count'] += collection_data['count']
                                org_results['summary']['bytes'] += collection_data['bytes']

                                # APP
                                if collection_data.get('max_modified') > app_data['summary']['max_modified']:
                                    app_data['summary']['max_modified'] = collection_data.get('max_modified')

                                if collection_data.get('min_modified') < app_data['summary']['min_modified']:
                                    app_data['summary']['min_modified'] = collection_data.get('min_modified')

                                if collection_data.get('max_created') > app_data['summary']['max_created']:
                                    app_data['summary']['max_created'] = collection_data.get('max_created')

                                if collection_data.get('min_created') < app_data['summary']['min_created']:
                                    app_data['summary']['min_created'] = collection_data.get('min_created')

                                # ORG
                                if collection_data.get('max_modified') > org_results['summary']['max_modified']:
                                    org_results['summary']['max_modified'] = collection_data.get('max_modified')

                                if collection_data.get('min_modified') < org_results['summary']['min_modified']:
                                    org_results['summary']['min_modified'] = collection_data.get('min_modified')

                                if collection_data.get('max_created') > org_results['summary']['max_created']:
                                    org_results['summary']['max_created'] = collection_data.get('max_created')

                                if collection_data.get('min_created') < org_results['summary']['min_created']:
                                    org_results['summary']['min_created'] = collection_data.get('min_created')

                        if QSIZE_OK:
                            status_logger.warn('CURRENT Queue Depth: %s' % self.worker_queue.qsize())

                        status_logger.warn('UPDATED status of org processed: %s' % json.dumps(org_results))

                except KeyboardInterrupt, e:
                    raise e

                except:
                    print traceback.format_exc()

            except KeyboardInterrupt, e:
                status_logger.warn('FINAL status of org processed: %s' % json.dumps(org_results))
                raise e

            except Empty:
                if QSIZE_OK:
                    status_logger.warn('CURRENT Queue Depth: %s' % self.worker_queue.qsize())

                status_logger.warn('CURRENT status of org processed: %s' % json.dumps(org_results))

                status_logger.warning('EMPTY! Count=%s' % empty_count)

                empty_count += 1

                if empty_count >= 120:
                    keep_going = False

            except:
                print traceback.format_exc()

        logger.warn('FINAL status of org processed: %s' % json.dumps(org_results))


class ErrorListener(Process):
    def __init__(self, worker_queue):
        super(ErrorListener, self).__init__()
        self.work_queue = worker_queue

    def run(self):

        keep_going = True

        empty_count = 0
        error_count = 0

        while keep_going:

            try:
                error_object = self.work_queue.get(timeout=3600)
                error_count += 1
                empty_count = 0

                status_logger.error('ErrorListener - errors=[%s] Writing...' % error_count)

                with open('/mnt/raid/logs/%s_errors.txt' % ECID, 'a') as f:
                    f.write(json.dumps(error_object))

            except KeyboardInterrupt, e:
                status_logger.error('ErrorListener - errors=[%s] Interrupted!' % error_count)
                raise e

            except Empty:
                status_logger.error('EMPTY! empty_count=[%s], error_count=[%s]' % (empty_count, error_count))
                empty_count += 1

                if empty_count >= 24:
                    status_logger.error('STOPPING! empty_count=[%s], error_count=[%s]' % (empty_count, error_count))

            except:
                print traceback.format_exc()

        status_logger.error('FINAL! empty_count=[%s], error_count=[%s]' % (empty_count, error_count))


class EntityWorker(Process):
    def __init__(self, queue, handler_function):
        super(EntityWorker, self).__init__()

        worker_logger.debug('Creating worker!')
        self.queue = queue
        self.handler_function = handler_function

    def run(self):

        worker_logger.info('starting run()...')
        keep_going = True

        count_processed = 0
        empty_count = 0
        start_time = int(time.time())
        while keep_going:

            try:
                app, collection_name, entity = self.queue.get(timeout=120)
                empty_count = 0

                if self.handler_function is not None:
                    try:
                        message_start_time = int(time.time())
                        processed = self.handler_function(app, collection_name, entity)
                        message_end_time = int(time.time())

                        if processed:
                            count_processed += 1

                            total_time = message_end_time - start_time
                            avg_time_per_message = total_time / count_processed
                            message_time = message_end_time - message_start_time

                            worker_logger.debug('Processed [%sth] entity = %s / %s / %s' % (
                                count_processed, app, collection_name, entity.get('uuid')))

                            if count_processed % 1000 == 1:
                                worker_logger.info(
                                        'Processed [%sth] entity = [%s / %s / %s] in [%s]s - avg time/message [%s]' % (
                                            count_processed, app, collection_name, entity.get('uuid'), message_time,
                                            avg_time_per_message))

                    except KeyboardInterrupt, e:
                        raise e

                    except Exception, e:
                        logger.exception('Error in EntityWorker processing message')
                        print traceback.format_exc()

            except KeyboardInterrupt, e:
                raise e

            except Empty:
                worker_logger.warning('EMPTY! Count=%s' % empty_count)

                empty_count += 1

                if empty_count >= 2:
                    keep_going = False

            except Exception, e:
                logger.exception('Error in EntityWorker run()')
                print traceback.format_exc()


class CollectionWorker(Process):
    def __init__(self, work_queue, entity_queue, response_queue):
        super(CollectionWorker, self).__init__()
        collection_worker_logger.debug('Creating worker!')
        self.work_queue = work_queue
        self.response_queue = response_queue
        self.entity_queue = entity_queue

    def run(self):

        collection_worker_logger.info('starting run()...')
        keep_going = True

        counter = 0
        # max_created = 0
        empty_count = 0
        app = 'ERROR'
        collection_name = 'NOT SET'
        status_map = {}
        sleep_time = 10

        try:

            while keep_going:

                try:
                    app, collection_name = self.work_queue.get(timeout=30)

                    status_map = {
                        collection_name: {
                            'iteration_started': str(datetime.datetime.now()),
                            'max_created': -1,
                            'max_modified': -1,
                            'min_created': 1584946416000,
                            'min_modified': 1584946416000,
                            'count': 0,
                            'bytes': 0
                        }
                    }

                    empty_count = 0

                    # added a flag for using graph vs query/index
                    if config.get('graph', False):
                        source_collection_url = collection_graph_url_template.format(org=config.get('org'),
                                                                                     app=app,
                                                                                     collection=collection_name,
                                                                                     limit=config.get('limit'),
                                                                                     **config.get('source_endpoint'))
                    else:
                        source_collection_url = collection_query_url_template.format(org=config.get('org'),
                                                                                     app=app,
                                                                                     collection=collection_name,
                                                                                     limit=config.get('limit'),
                                                                                     ql="select * %s" % config.get(
                                                                                             'ql'),
                                                                                     **config.get('source_endpoint'))

                    # use the UsergridQuery from the Python SDK to iterate the collection
                    q = UsergridQueryIterator(source_collection_url,
                                              page_delay=config.get('page_sleep_time'),
                                              sleep_time=config.get('error_retry_sleep'))

                    for entity in q:

                        # begin entity loop

                        self.entity_queue.put((app, collection_name, entity))
                        counter += 1

                        if 'created' in entity:

                            try:
                                entity_created = long(entity.get('created'))

                                if entity_created > status_map[collection_name]['max_created']:
                                    status_map[collection_name]['max_created'] = entity_created
                                    status_map[collection_name]['max_created_str'] = str(
                                            datetime.datetime.fromtimestamp(entity_created / 1000))

                                if entity_created < status_map[collection_name]['min_created']:
                                    status_map[collection_name]['min_created'] = entity_created
                                    status_map[collection_name]['min_created_str'] = str(
                                            datetime.datetime.fromtimestamp(entity_created / 1000))

                            except ValueError:
                                pass

                        if 'modified' in entity:

                            try:
                                entity_modified = long(entity.get('modified'))

                                if entity_modified > status_map[collection_name]['max_modified']:
                                    status_map[collection_name]['max_modified'] = entity_modified
                                    status_map[collection_name]['max_modified_str'] = str(
                                            datetime.datetime.fromtimestamp(entity_modified / 1000))

                                if entity_modified < status_map[collection_name]['min_modified']:
                                    status_map[collection_name]['min_modified'] = entity_modified
                                    status_map[collection_name]['min_modified_str'] = str(
                                            datetime.datetime.fromtimestamp(entity_modified / 1000))

                            except ValueError:
                                pass

                        status_map[collection_name]['bytes'] += count_bytes(entity)
                        status_map[collection_name]['count'] += 1

                        if counter % 1000 == 1:
                            try:
                                collection_worker_logger.warning(
                                        'Sending FINAL stats for app/collection [%s / %s]: %s' % (
                                            app, collection_name, status_map))

                                self.response_queue.put((app, collection_name, status_map))

                                if QSIZE_OK:
                                    collection_worker_logger.info(
                                            'Counter=%s, collection queue depth=%s' % (
                                                counter, self.work_queue.qsize()))
                            except:
                                pass

                            collection_worker_logger.warn(
                                    'Current status of collections processed: %s' % json.dumps(status_map))

                        if config.get('entity_sleep_time') > 0:
                            collection_worker_logger.debug(
                                    'sleeping for [%s]s per entity...' % (config.get('entity_sleep_time')))
                            time.sleep(config.get('entity_sleep_time'))
                            collection_worker_logger.debug(
                                    'STOPPED sleeping for [%s]s per entity...' % (config.get('entity_sleep_time')))

                    # end entity loop

                    status_map[collection_name]['iteration_finished'] = str(datetime.datetime.now())

                    collection_worker_logger.warning(
                            'Collection [%s / %s / %s] loop complete!  Max Created entity %s' % (
                                config.get('org'), app, collection_name, status_map[collection_name]['max_created']))

                    collection_worker_logger.warning(
                            'Sending FINAL stats for app/collection [%s / %s]: %s' % (app, collection_name, status_map))

                    self.response_queue.put((app, collection_name, status_map))

                    collection_worker_logger.info('Done! Finished app/collection: %s / %s' % (app, collection_name))

                except KeyboardInterrupt, e:
                    raise e

                except Empty:
                    collection_worker_logger.warning('EMPTY! Count=%s' % empty_count)

                    empty_count += 1

                    if empty_count >= 2:
                        keep_going = False

                except Exception, e:
                    logger.exception('Error in CollectionWorker processing collection [%s]' % collection_name)
                    print traceback.format_exc()

        finally:
            self.response_queue.put((app, collection_name, status_map))
            collection_worker_logger.info('FINISHED!')


def use_name_for_collection(collection_name):
    return collection_name in config.get('use_name_for_collection', [])


def include_edge(collection_name, edge_name):
    include_edges = config.get('include_edge', [])

    if include_edges is None:
        include_edges = []

    exclude_edges = config.get('exclude_edge', [])

    if exclude_edges is None:
        exclude_edges = []

    if len(include_edges) > 0 and edge_name not in include_edges:
        logger.debug(
                'Skipping edge [%s] since it is not in INCLUDED list: %s' % (edge_name, include_edges))
        return False

    if edge_name in exclude_edges:
        logger.debug(
                'Skipping edge [%s] since it is in EXCLUDED list: %s' % (edge_name, exclude_edges))
        return False

    if (collection_name in ['users', 'user'] and edge_name in ['roles', 'followers', 'groups',
                                                               'feed', 'activities']) \
            or (collection_name in ['device', 'devices'] and edge_name in ['users']) \
            or (collection_name in ['receipts', 'receipt'] and edge_name in ['device', 'devices']):
        # feed and activities are not retrievable...
        # roles and groups will be more efficiently handled from the role/group -> user
        # followers will be handled by 'following'
        # do only this from user -> device
        return False

    return True


def migrate_out_graph_edge_type(app, collection_name, source_entity, edge_name, depth=0):
    depth += 1

    if not include_edge(collection_name, edge_name):
        return True

    if depth > config.get('graph_depth', 100):
        logger.debug('Reached Max Graph Depth of [%s] in migrate_out_graph_edge_type' % depth)
        return True
    else:
        logger.debug('Processing @ Graph Depth [%s]' % depth)

    source_uuid = source_entity.get('uuid')

    key = '%s:edge:out:%s:%s' % (key_version, source_uuid, edge_name)

    if not config.get('skip_cache_read', False):
        date_visited = cache.get(key)

        if date_visited not in [None, 'None']:
            logger.info('Skipping EDGE [%s / %s --%s-->] - visited at %s' % (
                collection_name, source_uuid, edge_name, date_visited))
            return True
        else:
            cache.delete(key)

    if not config.get('skip_cache_write', False):
        cache.set(name=key, value=str(datetime.datetime.utcnow()), ex=config.get('visit_cache_ttl', 3600 * 12))

    logger.info('Visiting EDGE [%s / %s (%s) --%s-->] at %s' % (
        collection_name, source_uuid, get_uuid_time(source_uuid), edge_name, str(datetime.datetime.utcnow())))

    response = True

    source_identifier = get_source_identifier(source_entity)

    count_edges = 0

    logger.debug(
            'Processing edge type=[%s] of entity [%s / %s / %s]' % (edge_name, app, collection_name, source_identifier))

    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    connection_query_url = connection_query_url_template.format(
            org=config.get('org'),
            app=app,
            verb=edge_name,
            collection=collection_name,
            uuid=source_identifier,
            limit=config.get('limit'),
            **config.get('source_endpoint'))

    connection_query = UsergridQueryIterator(connection_query_url, sleep_time=config.get('error_retry_sleep'))

    connection_stack = []

    for e_connection in connection_query:
        target_connection_collection = config.get('collection_mapping', {}).get(e_connection.get('type'),
                                                                                e_connection.get('type'))

        target_ok = migrate_graph(app, e_connection.get('type'), source_entity=e_connection, depth=depth)

        if not target_ok:
            logger.critical(
                    'Error migrating TARGET entity data for connection [%s / %s / %s] --[%s]--> [%s / %s / %s]' % (
                        app, collection_name, source_identifier, edge_name, app, target_connection_collection,
                        e_connection.get('name', e_connection.get('uuid'))))

        count_edges += 1
        connection_stack.append(e_connection)

    while len(connection_stack) > 0:

        e_connection = connection_stack.pop()

        if collection_name in config.get('exclude_collection', []) or e_connection.get('type') in config.get(
                'exclude_collection', []):
            logger.debug('EXCLUDING Edge (collection): [%s / %s / %s] --[%s]--> [%s / %s / %s]' % (
                app, collection_name, source_identifier, edge_name, target_app, e_connection.get('type'),
                e_connection.get('name')))
            return True

        if e_connection.get('type') != 'device' \
                and 'name' in e_connection \
                and use_name_for_collection(e_connection.get('type')):

            create_connection_url = connection_create_by_name_url_template.format(
                    org=target_org,
                    app=target_app,
                    collection=target_collection,
                    uuid=source_identifier,
                    verb=edge_name,
                    target_type=e_connection.get('type'),
                    target_name=e_connection.get('name', ),
                    **config.get('target_endpoint'))
        else:
            create_connection_url = connection_create_by_uuid_url_template.format(
                    org=target_org,
                    app=target_app,
                    collection=target_collection,
                    uuid=source_identifier,
                    verb=edge_name,
                    target_uuid=e_connection.get('uuid'),
                    **config.get('target_endpoint'))

        if not config.get('skip_cache_read', False):
            processed = cache.get(create_connection_url)

            if processed not in [None, 'None']:
                logger.debug('Skipping visited Edge: [%s / %s / %s] --[%s]--> [%s / %s / %s]: %s ' % (
                    app, collection_name, source_identifier, edge_name, target_app, e_connection.get('type'),
                    e_connection.get('name'), create_connection_url))

                response = True and response
                continue

        logger.info('Connecting entity [%s / %s / %s] --[%s]--> [%s / %s / %s]: %s ' % (
            app, collection_name, source_identifier, edge_name, target_app, e_connection.get('type'),
            e_connection.get('name', e_connection.get('uuid')), create_connection_url))

        attempts = 0

        while attempts < 5:
            attempts += 1

            r_create = session_target.post(create_connection_url)

            if r_create.status_code == 200:

                if not config.get('skip_cache_write', False):
                    cache.set(create_connection_url, create_connection_url)

                response = True and response
                break

            elif r_create.status_code >= 500:

                if attempts < 5:
                    logger.warning('FAILED (will retry) to create connection at URL=[%s]: %s' % (
                        create_connection_url, r_create.text))
                    time.sleep(DEFAULT_RETRY_SLEEP)
                else:
                    response = False
                    connection_stack = []
                    logger.critical(
                            'FAILED [%s] (WILL NOT RETRY - max attempts) to create connection at URL=[%s]: %s' % (
                                r_create.status_code, create_connection_url, r_create.text))

            elif r_create.status_code in [401, 404]:
                logger.critical(
                        'FAILED [%s] (WILL NOT RETRY - 401/404) to create connection at URL=[%s]: %s' % (
                            r_create.status_code, create_connection_url, r_create.text))

                response = False
                connection_stack = []

    return response


def get_source_identifier(source_entity):
    entity_type = source_entity.get('type')

    source_identifier = source_entity.get('uuid')

    if use_name_for_collection(entity_type):

        if entity_type in ['user']:
            source_identifier = source_entity.get('username')
        else:
            source_identifier = source_entity.get('name')

        if source_identifier is None:
            source_identifier = source_entity.get('uuid')
            logger.warn('Using UUID for entity [%s / %s]' % (entity_type, source_identifier))

    return source_identifier


def include_collection(collection_name):
    exclude = config.get('exclude_collection', [])

    if exclude is not None and collection_name in exclude:
        return False

    return True


def migrate_in_graph_edge_type(app, collection_name, source_entity, edge_name, depth=0):
    depth += 1

    if depth > config.get('graph_depth', 100):
        logger.debug('Reached Max Graph Depth of [%s] in migrate_in_graph_edge_type' % depth)
        return True

    source_uuid = source_entity.get('uuid')
    key = '%s:edges:in:%s:%s' % (key_version, source_uuid, edge_name)

    if not config.get('skip_cache_read', False):
        date_visited = cache.get(key)

        if date_visited not in [None, 'None']:
            logger.info('Skipping EDGE [--%s--> %s / %s] - visited at %s' % (
                collection_name, source_uuid, edge_name, date_visited))
            return True
        else:
            cache.delete(key)

    if not config.get('skip_cache_write', False):
        cache.set(name=key, value=str(datetime.datetime.utcnow()), ex=config.get('visit_cache_ttl', 3600 * 12))

    logger.info('Visiting EDGE [--%s--> %s / %s (%s)] at %s' % (
        edge_name, collection_name, source_uuid, get_uuid_time(source_uuid), str(datetime.datetime.utcnow())))

    source_identifier = get_source_identifier(source_entity)

    if not include_collection(collection_name):
        logger.debug('Excluding (Collection) entity [%s / %s / %s]' % (app, collection_name, source_uuid))
        return True

    if not include_edge(collection_name, edge_name):
        return True

    logger.debug(
            'Processing edge type=[%s] of entity [%s / %s / %s]' % (edge_name, app, collection_name, source_identifier))

    logger.debug('Processing IN edges type=[%s] of entity [ %s / %s / %s]' % (
        edge_name, app, collection_name, source_uuid))

    connecting_query_url = connecting_query_url_template.format(
            org=config.get('org'),
            app=app,
            collection=collection_name,
            uuid=source_uuid,
            verb=edge_name,
            limit=config.get('limit'),
            **config.get('source_endpoint'))

    connection_query = UsergridQueryIterator(connecting_query_url, sleep_time=config.get('error_retry_sleep'))

    response = True

    for e_connection in connection_query:
        logger.debug('Triggering IN->OUT edge migration on entity [%s / %s / %s] ' % (
            app, e_connection.get('type'), e_connection.get('uuid')))

        response = migrate_graph(app, e_connection.get('type'), e_connection, depth) and response

    return response


def migrate_graph(app, collection_name, source_entity, depth=0):
    if depth > config.get('graph_depth', 100):
        logger.debug('Reached Max Graph Depth, stopping after [%s]' % depth)
        return True
    else:
        logger.debug('Processing @ Graph Depth [%s]' % depth)

    if not include_collection(collection_name):
        return True

    source_uuid = source_entity.get('uuid')

    key = '%s:graph:%s' % (key_version, source_uuid)
    entity_tag = '[%s / %s / %s (%s)]' % (app, collection_name, source_uuid, get_uuid_time(source_uuid))

    if not config.get('skip_cache_read', False):
        date_visited = cache.get(key)

        if date_visited not in [None, 'None']:
            logger.info('Skipping GRAPH %s at %s' % (entity_tag, date_visited))
            return True
        else:
            cache.delete(key)

    logger.info('Visiting GRAPH %s at %s' % (entity_tag, str(datetime.datetime.utcnow())))

    if not config.get('skip_cache_write', False):
        cache.set(name=key, value=str(datetime.datetime.utcnow()), ex=config.get('visit_cache_ttl', 3600 * 12))

    if collection_name in config.get('exclude_collection', []):
        logger.debug('Excluding (Collection) entity %s' % entity_tag)
        return True

    # migrate data for current node
    response = migrate_data(app, collection_name, source_entity)

    out_edge_names = [edge_name for edge_name in source_entity.get('metadata', {}).get('collections', [])]
    out_edge_names += [edge_name for edge_name in source_entity.get('metadata', {}).get('connections', [])]

    logger.debug('Entity %s has [%s] OUT edges' % (entity_tag, len(out_edge_names)))

    for edge_name in out_edge_names:
        if include_edge(collection_name, edge_name):
            response = migrate_out_graph_edge_type(app, collection_name, source_entity, edge_name,
                                                   depth) and response

    in_edge_names = [edge_name for edge_name in source_entity.get('metadata', {}).get('connecting', [])]

    logger.debug('Entity %s has [%s] IN edges' % (entity_tag, len(in_edge_names)))

    for edge_name in in_edge_names:
        if include_edge(collection_name, edge_name):
            response = migrate_in_graph_edge_type(app, collection_name, source_entity, edge_name,
                                                  depth) and response

    return response


def confirm_user_entity(app, source_entity, attempts=0):
    source_entity_url = get_entity_url_template.format(org=config.get('org'),
                                                       app=app,
                                                       collection='users',
                                                       uuid=source_entity.get('username'),
                                                       limit=config.get('limit'),
                                                       **config.get('source_endpoint'))

    if attempts >= 5:
        logger.error('Punting after [%s] attempts to confirm user at URL [%s], will use the source entity...' % (
            attempts, source_entity_url))

        return source_entity

    r = session_source.get(url=source_entity_url)

    if r.status_code == 200:
        retrieved_entity = r.json().get('entities')[0]

        if retrieved_entity.get('uuid') != source_entity.get('uuid'):
            logger.info(
                    'UUID of Source Entity [%s] differs from uuid [%s] of retrieved entity at URL=[%s] and will be substituted' % (
                        source_entity.get('uuid'), retrieved_entity.get('uuid'), source_entity_url))

        return retrieved_entity

    elif 'service_resource_not_found' in r.text:

        logger.warn('Unable to retrieve user at URL [%s], and will use source entity.  status=[%s] response: %s...' % (
            source_entity_url, r.status_code, r.text))

        return source_entity

    else:
        logger.error('After [%s] attempts to confirm user at URL [%s], received status [%s] message: %s...' % (
            attempts, source_entity_url, r.status_code, r.text))

        time.sleep(DEFAULT_RETRY_SLEEP)

        return confirm_user_entity(app, source_entity, attempts)


def reput(app, collection_name, source_entity, attempts=0):
    source_identifier = source_entity.get('uuid')
    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    try:
        target_entity_url_by_name = put_entity_url_template.format(org=target_org,
                                                                   app=target_app,
                                                                   collection=target_collection,
                                                                   uuid=source_identifier,
                                                                   **config.get('target_endpoint'))

        r = session_source.put(target_entity_url_by_name, data=json.dumps({}))
        if r.status_code != 200:
            logger.info('HTTP [%s]: %s' % (target_entity_url_by_name, r.status_code))
        else:
            logger.debug('HTTP [%s]: %s' % (target_entity_url_by_name, r.status_code))

    except:
        pass


def get_uuid_time(the_uuid_string):
    return time_uuid.TimeUUID(the_uuid_string).get_datetime()


def get_migrated_devices(source_entity, source_app):
    try:
        platform = source_entity['device-platform']
        notifier_key = source_app + '-' + platform + '.notifier.id'

        source_entity['name'] = source_entity['msisdn'] + '-' + source_app
        source_entity['api-version'] = 'v3'
        source_entity['app-name'] = source_app
        source_entity[notifier_key] = source_entity['notifier-id']

    except:
        return source_entity

    return source_entity


def get_devices_from_users(source_entity):
    devices = []
    for key in source_entity:
        if not 'pn-consent-' in key:
            continue

        app = key[11:]

        # TODO: Remove later
        if not app in ['messangerplus']:
            continue

        device = {}
        device['name'] = source_entity['username'] + '-' + app
        device['pn-consent'] = source_entity[key]
        device['type'] = 'device'

        devices.append(device)

    return devices


def migrate_device(device, target_org, target_app, attempts=0):
    device_identifier = get_source_identifier(device)
    target_entity_url_by_name = put_entity_url_template.format(org=target_org,
                                                               app=target_app,
                                                               collection='devices',
                                                               uuid=device_identifier,
                                                               **config.get('target_endpoint'))


    try:

        if attempts >= 5:
            traceback.print_stack()
            logger.critical(
                'ABORT migrate_users_to_devices | success=[%s] | attempts=[%s] %s / %s / %s' % (
                    True, attempts, target_app, 'devices', device_identifier))

            return False

        if attempts > 1:
            logger.warn(traceback.print_stack())
            logger.warn('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                attempts, 'devices', device_identifier, target_entity_url_by_name))
        else:
            logger.debug('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                attempts, 'devices', device_identifier, target_entity_url_by_name))

        r = session_target.put(url=target_entity_url_by_name, data=json.dumps(device))

        if r.status_code == 200:
            # Worked => WE ARE DONE
            logger.debug(
                'migrate_users_to_devices | success=[%s] | attempts=[%s] | entity=[%s / %s / %s]' % (
                    True, attempts, target_org, target_app, device_identifier))

            return True

        else:
            logger.error('Failure [%s] on attempt [%s] to PUT url=[%s], entity=[%s] response=[%s]' % (
                r.status_code, attempts, target_entity_url_by_name, json.dumps(device), r.text))

            return False

    except:
        logger.error(traceback.format_exc())
        logger.error('error in migrate_users_to_devices on entity: %s' % json.dumps(device))

    logger.warn('UNSUCCESSFUL migrate_users_to_devices | success=[%s] | attempts=[%s] | entity=[%s / %s / %s]' % (
        True, attempts, target_org, target_app, device_identifier))

    return migrate_device(device, target_org, target_app, attempts=attempts + 1)


def connect_user_to_device(device, user, target_org, target_app):
    source_identifier = get_source_identifier(user)
    create_connection_url = connection_create_by_uuid_url_template.format(
        org=target_org,
        app=target_app,
        collection='users',
        uuid=source_identifier,
        verb='devices',
        target_uuid=device.get('name'),
        **config.get('target_endpoint'))

    logger.info('Connecting entity [%s / %s / %s] --[%s]--> [%s / %s / %s]: %s ' % (
        target_app, 'users', source_identifier, 'devices', target_app, 'device',
        device.get('name'), create_connection_url))

    attempts = 0
    while attempts < 5:
        attempts += 1

        try:

            r_create = session_target.post(create_connection_url)

            if r_create.status_code == 200:
                return True

            elif r_create.status_code >= 500:
                if attempts < 5:
                    logger.warning('FAILED (will retry) to create connection at URL=[%s]: %s' % (
                        create_connection_url, r_create.text))
                    time.sleep(DEFAULT_RETRY_SLEEP)

                else:
                    logger.critical(
                        'FAILED [%s] (WILL NOT RETRY - max attempts) to create connection at URL=[%s]: %s' % (
                            r_create.status_code, create_connection_url, r_create.text))

                    return False

            elif r_create.status_code in [401, 404]:
                logger.critical(
                    'FAILED [%s] (WILL NOT RETRY - 401/404) to create connection at URL=[%s]: %s' % (
                        r_create.status_code, create_connection_url, r_create.text))

                return False

        except:
            continue

    return True


def migrate_users_to_devices(source_entity, target_org, target_app, attempts=0):
    devices = get_devices_from_users(source_entity)

    for device in devices:
        response = migrate_device(device, target_org, target_app)
        if response:
            connect_user_to_device(device, source_entity, target_org, target_app)

    return


def migrate_device_to_tokenmap(device, target_org, target_app):
    try:
        token_entity = {}
        token_entity['type'] = 'devicetoken'
        token_entity['msisdn'] = device['name']
        token = device['notifier-id']

        if 'http' in token:
            parsed_url = urlparse.urlparse(token)
            query_params = urlparse.parse_qs(parsed_url.query)
            token = query_params.get('token')[0]

        token_entity['name'] = urllib.quote_plus(token)

        source_identifier = get_source_identifier(token_entity)
        target_entity_url_by_name = put_entity_url_template.format(org=target_org,
                                                                   app=target_app,
                                                                   collection='devicetokens',
                                                                   uuid=source_identifier,
                                                                   **config.get('target_endpoint'))

    except:
        return False

    attempts = 0
    while attempts < 5:
        attempts += 1

        try:

            if attempts >= 5:
                traceback.print_stack()
                logger.critical(
                    'ABORT migrate_device_to_tokenmap | success=[%s] | attempts=[%s] | %s / %s / %s' % (
                        True, attempts, target_app, 'devicetokens', source_identifier))

                return False

            if attempts > 1:
                logger.warn(traceback.print_stack())
                logger.warn('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                    attempts, 'devicetokens', source_identifier, target_entity_url_by_name))
            else:
                logger.debug('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                    attempts, 'devicetokens', source_identifier, target_entity_url_by_name))

            r = session_target.put(url=target_entity_url_by_name, data=json.dumps(token_entity))

            if r.status_code == 200:
                # Worked => WE ARE DONE
                logger.debug(
                    'migrate_device_to_tokenmap | success=[%s] | attempts=[%s] | entity=[%s / %s / %s]' % (
                        True, attempts, config.get('org'), target_app, source_identifier))

                return True

            else:
                logger.error('Failure [%s] on attempt [%s] to PUT url=[%s], entity=[%s] response=[%s]' % (
                    r.status_code, attempts, target_entity_url_by_name, json.dumps(token_entity), r.text))

                if r.status_code in [400, 401, 404]:
                    logger.error(
                        'WILL NOT RETRY [%s] attempts to PUT url=[%s], entity=[%s] response=[%s]' % (
                            attempts, target_entity_url_by_name, json.dumps(token_entity), r.text))

                    return False

                logger.warn(
                    'UNSUCCESSFUL migrate_data | success=[%s] | attempts=[%s] | entity=[%s / %s / %s]' % (
                        True, attempts, config.get('org'), target_app, source_identifier))

        except:
            logger.error('Failure on attempt [%s] to PUT url=[%s], entity=[%s]' % (
                attempts, target_entity_url_by_name, json.dumps(token_entity)))

    return True


def migrate_data(app, collection_name, source_entity, attempts=0):
    if not config.get('skip_cache_read', False):
        try:
            str_modified = cache.get(source_entity.get('uuid'))

            if str_modified not in [None, 'None']:

                modified = long(str_modified)

                logger.debug('FOUND CACHE: %s = %s ' % (source_entity.get('uuid'), modified))

                if modified <= source_entity.get('modified'):

                    modified_date = datetime.datetime.utcfromtimestamp(modified / 1000)
                    e_uuid = source_entity.get('uuid')

                    uuid_datetime = time_uuid.TimeUUID(e_uuid).get_datetime()

                    logger.debug('Skipping ENTITY: %s / %s / %s / %s (%s) / %s (%s)' % (
                        config.get('org'), app, collection_name, e_uuid, uuid_datetime, modified, modified_date))
                    return True
                else:
                    logger.debug('DELETING CACHE: %s ' % (source_entity.get('uuid')))
                    cache.delete(source_entity.get('uuid'))
        except:
            logger.error('Error on checking cache for uuid=[%s]' % source_entity.get('uuid'))
            logger.error(traceback.format_exc())

    # handle duplicate user case
    if collection_name in ['users', 'user']:
        source_entity = confirm_user_entity(app, source_entity)

    source_identifier = get_source_identifier(source_entity)

    logger.info('Visiting ENTITY data [%s / %s (%s) ] at %s' % (
        collection_name, source_identifier, get_uuid_time(source_entity.get('uuid')), str(datetime.datetime.utcnow())))

    entity_copy = source_entity.copy()

    if 'metadata' in entity_copy:
        entity_copy.pop('metadata')

    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    # in case of users or devices, consolidate the data before migration
    source_entity_type = source_entity.get('type')
    if source_entity_type in ['device']:
        entity_copy = get_migrated_devices(entity_copy, app)
        source_identifier = get_source_identifier(entity_copy)

    if source_entity_type in ['user'] and target_collection in ['users']:
        entity_copy['api-version'] = 'v3'

    target_entity_url_by_name = put_entity_url_template.format(org=target_org,
                                                               app=target_app,
                                                               collection=target_collection,
                                                               uuid=source_identifier,
                                                               **config.get('target_endpoint'))

    try:

        if attempts >= 5:
            traceback.print_stack()
            logger.critical(
                'ABORT migrate_data | success=[%s] | attempts=[%s] | created=[%s] | modified=[%s] %s / %s / %s' % (
                    True, attempts, source_entity.get('created'), source_entity.get('modified'), app,
                    collection_name, source_identifier))

            return False

        if attempts > 1:
            logger.warn(traceback.print_stack())
            logger.warn('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                attempts, collection_name, source_identifier, target_entity_url_by_name))
        else:
            logger.debug('Attempt [%s] to migrate entity [%s / %s] at URL [%s]' % (
                attempts, collection_name, source_identifier, target_entity_url_by_name))

        r = session_target.put(url=target_entity_url_by_name, data=json.dumps(entity_copy))

        if r.status_code == 200:
            # Worked => WE ARE DONE
            logger.debug(
                'migrate_data | success=[%s] | attempts=[%s] | entity=[%s / %s / %s] | created=[%s] | modified=[%s]' % (
                    True, attempts, config.get('org'), app, source_identifier, source_entity.get('created'),
                    source_entity.get('modified'),))

            if not config.get('skip_cache_write', False):
                logger.debug('SETTING CACHE | uuid=[%s] | modified=[%s]' % (
                    source_entity.get('uuid'), str(source_entity.get('modified'))))

                if not config.get('skip_cache_write', False):
                    cache.set(source_entity.get('uuid'), str(source_entity.get('modified')))

            # migrate devices into deviceTokenMap collection
            if source_entity_type in ['device']:
                migrate_device_to_tokenmap(entity_copy, target_org, target_app)

            # migrate users into devices and create connections
            if source_entity_type in ['user'] and target_collection in ['users']:
                migrate_users_to_devices(entity_copy, target_org, target_app)

            return True

        else:
            logger.error('Failure [%s] on attempt [%s] to PUT url=[%s], entity=[%s] response=[%s]' % (
                r.status_code, attempts, target_entity_url_by_name, json.dumps(source_entity), r.text))

            if r.status_code == 400:

                if target_collection in ['roles', 'role']:
                    return repair_user_role(app, collection_name, source_entity)

                elif target_collection in ['users', 'user']:
                    return handle_user_migration_conflict(app, collection_name, source_entity)

                elif 'duplicate_unique_property_exists' in r.text:
                    logger.error(
                        'WILL NOT RETRY (duplicate) [%s] attempts to PUT url=[%s], entity=[%s] response=[%s]' % (
                            attempts, target_entity_url_by_name, json.dumps(source_entity), r.text))

                    return False

    except:
        logger.error(traceback.format_exc())
        logger.error('error in migrate_data on entity: %s' % json.dumps(source_entity))

    logger.warn(
        'UNSUCCESSFUL migrate_data | success=[%s] | attempts=[%s] | entity=[%s / %s / %s] | created=[%s] | modified=[%s]' % (
            True, attempts, config.get('org'), app, source_identifier, source_entity.get('created'),
            source_entity.get('modified'),))

    return migrate_data(app, collection_name, source_entity, attempts=attempts + 1)


def handle_user_migration_conflict(app, collection_name, source_entity, attempts=0, depth=0):
    if collection_name in ['users', 'user']:
        return False

    username = source_entity.get('username')
    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    target_entity_url = get_entity_url_template.format(org=target_org,
                                                       app=target_app,
                                                       collection=target_collection,
                                                       uuid=username,
                                                       **config.get('target_endpoint'))

    # There is retry build in, here is the short circuit
    if attempts >= 5:
        logger.critical(
                'Aborting after [%s] attempts to audit user [%s] at URL [%s]' % (attempts, username, target_entity_url))

        return False

    r = session_target.get(url=target_entity_url)

    if r.status_code == 200:
        target_entity = r.json().get('entities')[0]

        if source_entity.get('created') < target_entity.get('created'):
            return repair_user_role(app, collection_name, source_entity)

    elif r.status_code / 100 == 5:
        audit_logger.warning(
                'CONFLICT: handle_user_migration_conflict failed attempt [%s] GET [%s] on TARGET URL=[%s] - : %s' % (
                    attempts, r.status_code, target_entity_url, r.text))

        time.sleep(DEFAULT_RETRY_SLEEP)

        return handle_user_migration_conflict(app, collection_name, source_entity, attempts)

    else:
        audit_logger.error(
                'CONFLICT: Failed handle_user_migration_conflict attempt [%s] GET [%s] on TARGET URL=[%s] - : %s' % (
                    attempts, r.status_code, target_entity_url, r.text))

        return False


def get_best_source_entity(app, collection_name, source_entity, depth=0):
    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    target_pk = 'uuid'

    if target_collection in ['users', 'user']:
        target_pk = 'username'
    elif target_collection in ['roles', 'role']:
        target_pk = 'name'

    target_name = source_entity.get(target_pk)

    # there should be no target entity now, we just need to decide which one from the source to use
    source_entity_url_by_name = get_entity_url_template.format(org=config.get('org'),
                                                               app=app,
                                                               collection=collection_name,
                                                               uuid=target_name,
                                                               **config.get('source_endpoint'))

    r_get_source_entity = session_source.get(source_entity_url_by_name)

    # if we are able to get at the source by PK...
    if r_get_source_entity.status_code == 200:

        # extract the entity from the response
        entity_from_get = r_get_source_entity.json().get('entities')[0]

        return entity_from_get

    elif r_get_source_entity.status_code / 100 == 4:
        # wasn't found, get by QL and sort
        source_entity_query_url = collection_query_url_template.format(org=config.get('org'),
                                                                       app=app,
                                                                       collection=collection_name,
                                                                       ql='select * where %s=\'%s\' order by created asc' % (
                                                                           target_pk, target_name),
                                                                       limit=config.get('limit'),
                                                                       **config.get('source_endpoint'))

        logger.info('Attempting to determine best entity from query on URL %s' % source_entity_query_url)

        q = UsergridQueryIterator(source_entity_query_url, sleep_time=config.get('error_retry_sleep'))

        desired_entity = None

        entity_counter = 0

        for e in q:
            entity_counter += 1

            if desired_entity is None:
                desired_entity = e

            elif e.get('created') < desired_entity.get('created'):
                desired_entity = e

        if desired_entity is None:
            logger.warn('Unable to determine best of [%s] entities from query on URL %s' % (
                entity_counter, source_entity_query_url))

            return source_entity

        else:
            return desired_entity

    else:
        return source_entity


def repair_user_role(app, collection_name, source_entity, attempts=0, depth=0):
    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    # For the users collection, there seemed to be cases where a USERNAME was created/existing with the a
    # different UUID which caused a 'collision' - so the point is to delete the entity with the differing
    # UUID by UUID and then do a recursive call to migrate the data - now that the collision has been cleared

    target_pk = 'uuid'

    if target_collection in ['users', 'user']:
        target_pk = 'username'
    elif target_collection in ['roles', 'role']:
        target_pk = 'name'

    target_name = source_entity.get(target_pk)

    target_entity_url_by_name = get_entity_url_template.format(org=target_org,
                                                               app=target_app,
                                                               collection=target_collection,
                                                               uuid=target_name,
                                                               **config.get('target_endpoint'))

    logger.warning('Repairing: Deleting name=[%s] entity at URL=[%s]' % (target_name, target_entity_url_by_name))

    r = session_target.delete(target_entity_url_by_name)

    if r.status_code == 200 or (r.status_code in [404, 401] and 'service_resource_not_found' in r.text):
        logger.info('Deletion of entity at URL=[%s] was [%s]' % (target_entity_url_by_name, r.status_code))

        best_source_entity = get_best_source_entity(app, collection_name, source_entity)

        target_entity_url_by_uuid = get_entity_url_template.format(org=target_org,
                                                                   app=target_app,
                                                                   collection=target_collection,
                                                                   uuid=best_source_entity.get('uuid'),
                                                                   **config.get('target_endpoint'))

        r = session_target.put(target_entity_url_by_uuid, data=json.dumps(best_source_entity))

        if r.status_code == 200:
            logger.info('Successfully repaired user at URL=[%s]' % target_entity_url_by_uuid)
            return True

        else:
            logger.critical('Failed to PUT [%s] the desired entity  at URL=[%s]: %s' % (
                r.status_code, target_entity_url_by_name, r.text))
            return False

    else:
        # log an error and keep going if we cannot delete the entity at the specified URL.  Unlikely, but if so
        # then this entity is borked
        logger.critical(
                'Deletion of entity at URL=[%s] FAILED [%s]: %s' % (target_entity_url_by_name, r.status_code, r.text))
        return False


def get_target_mapping(app, collection_name):
    target_org = config.get('org_mapping', {}).get(config.get('org'), config.get('org'))
    target_app = config.get('app_mapping', {}).get(app, app)
    target_collection = config.get('collection_mapping', {}).get(collection_name, collection_name)

    # handle case of migrating to single target app
    target_app = config.get('target_app', target_app) or target_app
    return target_app, target_collection, target_org


def parse_args():
    parser = argparse.ArgumentParser(description='Usergrid Org/App Migrator')

    parser.add_argument('--log_dir',
                        help='path to the place where logs will be written',
                        default='./',
                        type=str,
                        required=False)

    parser.add_argument('--log_level',
                        help='log level - DEBUG, INFO, WARN, ERROR, CRITICAL',
                        default='INFO',
                        type=str,
                        required=False)

    parser.add_argument('-o', '--org',
                        help='Name of the org to migrate',
                        type=str,
                        required=True)

    parser.add_argument('-a', '--app',
                        help='Name of one or more apps to include, specify none to include all apps',
                        required=False,
                        action='append')

    parser.add_argument('-e', '--include_edge',
                        help='Name of one or more edges/connection types to INCLUDE, specify none to include all edges',
                        required=False,
                        action='append')

    parser.add_argument('--exclude_edge',
                        help='Name of one or more edges/connection types to EXCLUDE, specify none to include all edges',
                        required=False,
                        action='append')

    parser.add_argument('--exclude_collection',
                        help='Name of one or more collections to EXCLUDE, specify none to include all collections',
                        required=False,
                        action='append')

    parser.add_argument('-c', '--collection',
                        help='Name of one or more collections to include, specify none to include all collections',
                        default=[],
                        action='append')

    parser.add_argument('--use_name_for_collection',
                        help='Name of one or more collections to use [name] instead of [uuid] for creating entities and edges',
                        default=[],
                        action='append')

    parser.add_argument('-m', '--migrate',
                        help='Specifies what to migrate: data, connections, credentials, audit or none (just iterate '
                             'the apps/collections)',
                        type=str,
                        choices=['data', 'none', 'reput', 'credentials', 'graph'],
                        default='data')

    parser.add_argument('-s', '--source_config',
                        help='The path to the source endpoint/org configuration file',
                        type=str,
                        default='source.json')

    parser.add_argument('-d', '--target_config',
                        help='The path to the target endpoint/org configuration file',
                        type=str,
                        default='destination.json')

    parser.add_argument('--limit',
                        help='The number of entities to return per query request',
                        type=int,
                        default=100)

    parser.add_argument('-w', '--entity_workers',
                        help='The number of worker processes to do the migration',
                        type=int,
                        default=16)

    parser.add_argument('--visit_cache_ttl',
                        help='The TTL of the cache of visiting nodes in the graph for connections',
                        type=int,
                        default=3600 * 2)

    parser.add_argument('--error_retry_sleep',
                        help='The number of seconds to wait between retrieving after an error',
                        type=float,
                        default=30)

    parser.add_argument('--page_sleep_time',
                        help='The number of seconds to wait between retrieving pages from the UsergridQueryIterator',
                        type=float,
                        default=.5)

    parser.add_argument('--entity_sleep_time',
                        help='The number of seconds to wait between retrieving pages from the UsergridQueryIterator',
                        type=float,
                        default=.1)

    parser.add_argument('--collection_workers',
                        help='The number of worker processes to do the migration',
                        type=int,
                        default=2)

    parser.add_argument('--queue_size_max',
                        help='The max size of entities to allow in the queue',
                        type=int,
                        default=100000)

    parser.add_argument('--graph_depth',
                        help='The graph depth to traverse to copy',
                        type=int,
                        default=100000)

    parser.add_argument('--queue_watermark_high',
                        help='The point at which publishing to the queue will PAUSE until it is at or below low watermark',
                        type=int,
                        default=25000)

    parser.add_argument('--min_modified',
                        help='Break when encountering a modified date before this, per collection',
                        type=int,
                        default=0)

    parser.add_argument('--max_modified',
                        help='Break when encountering a modified date after this, per collection',
                        type=long,
                        default=3793805526000)

    parser.add_argument('--queue_watermark_low',
                        help='The point at which publishing to the queue will RESUME after it has reached the high watermark',
                        type=int,
                        default=5000)

    parser.add_argument('--ql',
                        help='The QL to use in the filter for reading data from collections',
                        type=str,
                        default='select * order by created asc')
    # default='select * order by created asc')

    parser.add_argument('--skip_cache',
                        dest='skip_cache',
                        action='store_true')

    parser.add_argument('--skip_cache_read',
                        dest='skip_cache_read',
                        action='store_true')

    parser.add_argument('--skip_cache_write',
                        dest='skip_cache_write',
                        action='store_true')

    parser.add_argument('--create_apps',
                        dest='create_apps',
                        action='store_true')

    parser.add_argument('--with_data',
                        dest='with_data',
                        action='store_true')

    parser.add_argument('--nohup',
                        dest='specifies not to use stdout for logging',
                        action='store_true')

    parser.add_argument('--repair',
                        help='Attempt to migrate missing data',
                        dest='repair',
                        action='store_true')

    parser.add_argument('--graph',
                        help='Use GRAPH instead of Query',
                        dest='graph',
                        action='store_true')

    parser.add_argument('--su_username',
                        help='Superuser username',
                        required=False,
                        type=str)

    parser.add_argument('--su_password',
                        help='Superuser Password',
                        required=False,
                        type=str)

    parser.add_argument('--inbound_connections',
                        help='Name of the org to migrate',
                        action='store_true')

    parser.add_argument('--map_app',
                        help="Multiple allowed: A colon-separated string such as 'apples:oranges' which indicates to"
                             " put data from the app named 'apples' from the source endpoint into app named 'oranges' "
                             "in the target endpoint",
                        default=[],
                        action='append')

    parser.add_argument('--map_collection',
                        help="One or more colon-separated string such as 'cats:dogs' which indicates to put data from "
                             "collections named 'cats' from the source endpoint into a collection named 'dogs' in the "
                             "target endpoint, applicable globally to all apps",
                        default=[],
                        action='append')

    parser.add_argument('--map_org',
                        help="One or more colon-separated strings such as 'red:blue' which indicates to put data from "
                             "org named 'red' from the source endpoint into a collection named 'blue' in the target "
                             "endpoint",
                        default=[],
                        action='append')

    parser.add_argument('--target_app',
                        help='Name of the (single) target app to migrate to',
                        type=str)

    my_args = parser.parse_args(sys.argv[1:])

    return vars(my_args)


def init():
    global config

    if config.get('migrate') == 'credentials':

        if config.get('su_password') is None or config.get('su_username') is None:
            message = 'ABORT: In order to migrate credentials, Superuser parameters (su_password, su_username) are required'
            print message
            logger.critical(message)
            exit()

    config['collection_mapping'] = {}
    config['app_mapping'] = {}
    config['org_mapping'] = {}

    for mapping in config.get('map_collection', []):
        parts = mapping.split(':')

        if len(parts) == 2:
            config['collection_mapping'][parts[0]] = parts[1]
        else:
            logger.warning('Skipping Collection mapping: [%s]' % mapping)

    for mapping in config.get('map_app', []):
        parts = mapping.split(':')

        if len(parts) == 2:
            config['app_mapping'][parts[0]] = parts[1]
        else:
            logger.warning('Skipping App mapping: [%s]' % mapping)

    for mapping in config.get('map_org', []):
        parts = mapping.split(':')

        if len(parts) == 2:
            config['org_mapping'][parts[0]] = parts[1]
            logger.info('Mapping Org [%s] to [%s] from mapping [%s]' % (parts[0], parts[1], mapping))
        else:
            logger.warning('Skipping Org mapping: [%s]' % mapping)

    with open(config.get('source_config'), 'r') as f:
        config['source_config'] = json.load(f)

    with open(config.get('target_config'), 'r') as f:
        config['target_config'] = json.load(f)

    if config['exclude_collection'] is None:
        config['exclude_collection'] = []

    config['source_endpoint'] = config['source_config'].get('endpoint').copy()
    config['source_endpoint'].update(config['source_config']['credentials'][config['org']])

    target_org = config.get('org_mapping', {}).get(config.get('org'), config.get('org'))

    config['target_endpoint'] = config['target_config'].get('endpoint').copy()
    config['target_endpoint'].update(config['target_config']['credentials'][target_org])


def wait_for(threads, label, sleep_time=60):
    wait = True

    logger.info('Starting to wait for [%s] threads with sleep time=[%s]' % (len(threads), sleep_time))

    while wait:
        wait = False
        alive_count = 0

        for t in threads:

            if t.is_alive():
                alive_count += 1
                logger.info('Thread [%s] is still alive' % t.name)

        if alive_count > 0:
            wait = True
            logger.info('Continuing to wait for [%s] threads with sleep time=[%s]' % (alive_count, sleep_time))
            time.sleep(sleep_time)

    logger.warn('All workers [%s] done!' % label)


def count_bytes(entity):
    entity_copy = entity.copy()

    if 'metadata' in entity_copy:
        del entity_copy['metadata']

    entity_str = json.dumps(entity_copy)

    return len(entity_str)


def migrate_user_credentials(app, collection_name, source_entity, attempts=0):
    # this only applies to users
    if collection_name != 'users':
        return False

    source_identifier = get_source_identifier(source_entity)

    target_app, target_collection, target_org = get_target_mapping(app, collection_name)

    # get the URLs for the source and target users

    source_url = user_credentials_url_template.format(org=config.get('org'),
                                                      app=app,
                                                      uuid=source_identifier,
                                                      **config.get('source_endpoint'))

    target_url = user_credentials_url_template.format(org=target_org,
                                                      app=target_app,
                                                      uuid=source_identifier,
                                                      **config.get('target_endpoint'))

    # this endpoint for some reason uses basic auth...
    r = requests.get(source_url, auth=HTTPBasicAuth(config.get('su_username'), config.get('su_password')))

    if r.status_code != 200:
        logger.error('Unable to migrate credentials due to HTTP [%s] on GET URL [%s]: %s' % (
            r.status_code, source_url, r.text))

        return False

    source_credentials = r.json()

    logger.info('Putting credentials to [%s]...' % target_url)

    r = requests.put(target_url,
                     data=json.dumps(source_credentials),
                     auth=HTTPBasicAuth(config.get('su_username'), config.get('su_password')))

    if r.status_code != 200:
        logger.error(
                'Unable to migrate credentials due to HTTP [%s] on PUT URL [%s]: %s' % (
                    r.status_code, target_url, r.text))
        return False

    logger.info('migrate_user_credentials | success=[%s] | app/collection/name = %s/%s/%s' % (
        True, app, collection_name, source_entity.get('uuid')))

    return True


def check_response_status(r, url, exit_on_error=True):
    if r.status_code != 200:
        logger.critical('HTTP [%s] on URL=[%s]' % (r.status_code, url))
        logger.critical('Response: %s' % r.text)

        if exit_on_error:
            exit()


def main():
    global config, cache

    config = parse_args()
    init()
    init_logging()

    try:
        cache = redis.StrictRedis(host='localhost', port=6379, db=0)
    except:
        logger.error('Error connecting to Redis cache, consider using Redis to be able to optimize the process...')
        logger.error('Error connecting to Redis cache, consider using Redis to be able to optimize the process...')
        logger.error('Error connecting to Redis cache, consider using Redis to be able to optimize the process...')
        logger.error('Error connecting to Redis cache, consider using Redis to be able to optimize the process...')
        time.sleep(5)
        config['use_cache'] = False
        config['skip_cache_read'] = True
        config['skip_cache_write'] = True

    status_map = {}

    org_apps = {
    }

    if len(org_apps) == 0:
        source_org_mgmt_url = org_management_url_template.format(org=config.get('org'),
                                                                 limit=config.get('limit'),
                                                                 **config.get('source_endpoint'))

        print 'Retrieving apps from [%s]' % source_org_mgmt_url
        logger.info('Retrieving apps from [%s]' % source_org_mgmt_url)

        try:
            # list the apps for the SOURCE org
            logger.info('GET %s' % source_org_mgmt_url)
            r = session_source.get(source_org_mgmt_url)

            if r.status_code != 200:
                logger.critical('Abort processing: Unable to retrieve apps from [%s]: %s' % (source_org_mgmt_url, r.text))
                exit()

            logger.info(json.dumps(r.text))

            org_apps = r.json().get('data')

        except Exception, e:
            logger.exception('ERROR Retrieving apps from [%s]' % source_org_mgmt_url)
            print traceback.format_exc()
            logger.critical('Unable to retrieve apps from [%s] and will exit' % source_org_mgmt_url)
            exit()

    if _platform == "linux" or _platform == "linux2":
        entity_queue = Queue(maxsize=config.get('queue_size_max'))
        error_queue = Queue(maxsize=config.get('queue_size_max'))
        collection_queue = Queue(maxsize=config.get('queue_size_max'))
        collection_response_queue = Queue(maxsize=config.get('queue_size_max'))
    else:
        entity_queue = Queue()
        error_queue = Queue()
        collection_queue = Queue()
        collection_response_queue = Queue()

    # Check the specified configuration for what to migrate/audit
    if config.get('migrate') == 'graph':
        operation = migrate_graph
    elif config.get('migrate') == 'data':
        operation = migrate_data
    elif config.get('migrate') == 'credentials':
        operation = migrate_user_credentials
    elif config.get('migrate') == 'reput':
        operation = reput
    else:
        operation = None

    logger.info('Starting entity_workers...')

    status_listener = StatusListener(collection_response_queue, entity_queue)
    status_listener.start()

    # start the worker processes which will do the work of migrating
    entity_workers = [EntityWorker(entity_queue, operation) for x in xrange(config.get('entity_workers'))]
    [w.start() for w in entity_workers]

    # start the worker processes which will iterate the collections
    collection_workers = [CollectionWorker(collection_queue, entity_queue, collection_response_queue) for x in
                          xrange(config.get('collection_workers'))]
    [w.start() for w in collection_workers]

    try:
        apps_to_process = config.get('app')
        collections_to_process = config.get('collection')

        # iterate the apps retrieved from the org
        for org_app in sorted(org_apps.keys()):
            logger.info('Found SOURCE App: %s' % org_app)

        time.sleep(3)

        for org_app in sorted(org_apps.keys()):
            parts = org_app.split('/')
            app = parts[1]

            # if apps are specified and the current app is not in the list, skip it
            if apps_to_process and len(apps_to_process) > 0 and app not in apps_to_process:
                logger.warning('Skipping app [%s] not included in process list [%s]' % (app, apps_to_process))
                continue

            logger.info('Processing app=[%s]' % app)

            status_map[app] = {
                'iteration_started': str(datetime.datetime.now()),
                'max_created': -1,
                'max_modified': -1,
                'min_created': 1584946416000,
                'min_modified': 1584946416000,
                'count': 0,
                'bytes': 0,
                'collections': {}
            }

            # it is possible to map source orgs and apps to differently named targets.  This gets the
            # target names for each
            target_org = config.get('org_mapping', {}).get(config.get('org'), config.get('org'))
            target_app = config.get('app_mapping', {}).get(app, app)

            # handle case of migrating to single target app
            target_app = config.get('target_app', target_app) or target_app

            # Check that the target Org/App exists.  If not, move on to the next
            target_app_url = app_url_template.format(org=target_org,
                                                     app=target_app,
                                                     **config.get('target_endpoint'))
            logger.info('GET %s' % target_app_url)
            r_target_apps = session_target.get(target_app_url)

            if r_target_apps.status_code != 200:

                if config.get('create_apps', DEFAULT_CREATE_APPS):
                    create_app_url = org_management_app_url_template.format(org=target_org,
                                                                            app=target_app,
                                                                            **config.get('target_endpoint'))
                    app_request = {'name': target_app}
                    r = session_target.post(create_app_url, data=json.dumps(app_request))

                    if r.status_code != 200:
                        logger.critical(
                                'Unable to create app [%s] at URL=[%s]: %s' % (target_app, create_app_url, r.text))
                        continue
                    else:
                        logger.warning('Created app=[%s] at URL=[%s]: %s' % (target_app, create_app_url, r.text))
                else:
                    logger.critical(
                            'Target application does not exist at [%s] URL=%s' % (
                                r_target_apps.status_code, target_app_url))
                    continue

            # get the list of collections from the source org/app
            source_app_url = app_url_template.format(org=config.get('org'),
                                                     app=app,
                                                     **config.get('source_endpoint'))
            logger.info('GET %s' % source_app_url)

            r_collections = session_source.get(source_app_url)

            collection_attempts = 0

            # sometimes this call was not working so I put it in a loop to force it...
            while r_collections.status_code != 200 and collection_attempts < 5:
                collection_attempts += 1
                logger.warning('FAILED: GET (%s) [%s] URL: %s' % (r_collections.elapsed, r_collections.status_code,
                                                                  source_app_url))
                time.sleep(DEFAULT_RETRY_SLEEP)
                r_collections = session_source.get(source_app_url)

            if collection_attempts >= 5:
                logger.critical('Unable to get collections at URL %s, skipping app' % source_app_url)
                continue

            app_response = r_collections.json()

            logger.info('App Response: ' + json.dumps(app_response))

            app_entities = app_response.get('entities', [])

            if len(app_entities) > 0:
                app_entity = app_entities[0]
                collections = app_entity.get('metadata', {}).get('collections', {})
                logger.info('Collection List: %s' % collections)

                # iterate the collections which are returned.
                for collection_name, collection_data in collections.iteritems():
                    exclude_collections = config.get('exclude_collection', [])

                    if exclude_collections is None:
                        exclude_collections = []

                    # filter out collections as configured...
                    if collection_name in ignore_collections \
                            or (len(collections_to_process) > 0 and collection_name not in collections_to_process) \
                            or (len(exclude_collections) > 0 and collection_name in exclude_collections) \
                            or (config.get('migrate') == 'credentials' and collection_name != 'users'):

                        logger.warning('Skipping collection=[%s]' % collection_name)

                        continue

                    logger.info('Publishing app / collection: %s / %s' % (app, collection_name))

                    collection_queue.put((app, collection_name))

            status_map[app]['iteration_finished'] = str(datetime.datetime.now())

            logger.info('Finished publishing collections for app [%s] !' % app)

        # allow collection workers to finish
        wait_for(collection_workers, label='collection_workers', sleep_time=30)

        # allow entity workers to finish
        wait_for(entity_workers, label='entity_workers', sleep_time=30)

        status_listener.terminate()

    except KeyboardInterrupt:
        logger.warning('Keyboard Interrupt, aborting...')
        entity_queue.close()
        collection_queue.close()
        collection_response_queue.close()

        [os.kill(super(EntityWorker, p).pid, signal.SIGINT) for p in entity_workers]
        [os.kill(super(CollectionWorker, p).pid, signal.SIGINT) for p in collection_workers]
        os.kill(super(StatusListener, status_listener).pid, signal.SIGINT)

        [w.terminate() for w in entity_workers]
        [w.terminate() for w in collection_workers]
        status_listener.terminate()

    logger.info('entity_workers DONE!')


if __name__ == "__main__":
    main()
