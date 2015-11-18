import json
import logging
import boto

from collections import namedtuple
from hashlib import sha1
from types import ListType
from datetime import datetime

from google.appengine.ext import db
from boto.sqs.message import RawMessage

import models

# Helper class: Partitions an iterable into equally-sized chunks.
# Useful to avoid weird timeouts while writing to file. Num must be >= 1!
class groupnum:
    def __init__(self,iterable,num):
        self.it = iter(iterable)
        self.num = num

    def __iter__(self):
        return self

    def next(self):
        self.curval = next(self.it) # Raise StopIteration
        return self._grouper()

    def _grouper(self):
        for i in xrange(self.num-1):
            yield self.curval
            self.curval = next(self.it) # Raise StopIteration
        yield self.curval 

# Helper generator: Grab query items in batches to avoid headache-inducing query timeout issues

# Changed to a class so that retrieving the cursor when it's done is possible
# 2013-07-07: Hack to support more than one filter op 
class bigquery:
    def __init__(self, kind, batch_size, num_rows, order = None, filters = None, cursor = None):
        self.kind = kind
        self.batch_size = batch_size
        self.num_rows = num_rows
        self.order = order
        self.filters = filters
        self.numread_total = 0
        self.cursor = cursor
        self._qiter = None

    def __iter__(self):
        return self

    def next(self):
        # Done reading?
        if self.numread_total >= self.num_rows:
            self.cursor = self._query.cursor()
            raise StopIteration

        # Return contents of current query
        if self._qiter:
            try:
                res = self._qiter.next()
                self.numread_total += 1
                return res
            except StopIteration:
                self.cursor = self._query.cursor()
                self._query = None
                self._qiter = None

        # If no current query or current query is done, refresh
        query = db.Query(self.kind)
        if self.order:
            query.order(self.order)
        if self.filters:
            for filter in self.filters:
                query.filter(filter[0], filter[1])
        if self.cursor:
            query.with_cursor(self.cursor)

        # Get the rows
        to_read = min(self.batch_size, self.num_rows - self.numread_total)		
        self._query = query
        self._qiter = iter(self._query.run(limit=to_read))		

        # If this immediately raises StopIteration our query was empty and we should stop here		
        res = self._qiter.next()
        self.numread_total += 1
        return res

# one-way ticket
tempuser = lambda user, date: ''.join(char if i % 5 else "-"
                                      for i, char in enumerate(sha1('%s-%s' % (user, date)).hexdigest(),1))[:-1]

######### SQS QUEUE ###########
SQS_Report = namedtuple('Report', ['id','content'])
AWS_ACCESS_KEY = 'AKIAJJUHUNK7ACIHNYVA'
AWS_SECRET_KEY = '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'

HTTP_CODES = {
    'CREATED' : 201,
    'ACCEPTED': 200,
    'BAD_REQUEST': 400,
}


#monkeypatching ssl
if not boto.config.has_section('Boto'):
    boto.config.add_section('Boto')
boto.config.set('Boto', 'https_validate_certificates', 'False')

logging.getLogger('boto').setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


class SQS_Queue():
    timeout = 0
    max_message_count = 10
    __slots__ = ['timeout', 'max_message_count', 'queue', '_container', 'type']

    def __init__(self, queue_name):
        self.queue = boto.connect_sqs(AWS_ACCESS_KEY, AWS_SECRET_KEY).get_queue(queue_name)
        self._container = []
        self.type = ListType

    def __call__(self, report):
        message = RawMessage(body=json.dumps(report.content))
        message_id = chr(95).join((str(report.id),
                                   datetime.utcnow().strftime('%B-%d-%Y-%H%M-%S-%f')))
        self.append((message_id, message, SQS_Queue.timeout))
        return message_id

    def append(self, message):
        if not isinstance(message[1], boto.sqs.message.RawMessage):
            raise ValueError('Item must be RawMessage instance')
        self._container.append(message)
        if len(self._container) > SQS_Queue.max_message_count:
            # send first 10 elements to SQS
            self.queue.write_batch(self._container[:SQS_Queue.max_message_count])
            del self._container[:SQS_Queue.max_message_count]

def return_early(response, message='', status=HTTP_CODES['BAD_REQUEST']):
    response.set_status(status)
    response.out.write(message)
    return response

#SDK permission operations
def permission_config(request, response):
    if request == 'config:show':
        return return_early(response,
                            message=models.Client.show_clients(),
                            status=200)
    elif request == 'config:reset':
        return return_early(response,
                            message=models.Client.reset_permissions(),
                            status=200)
    elif request == 'config:populate':
        permissions = reset_permission_model()
        res_message = set_client_default_permissions(permissions)
        return return_early(response,
                            message=res_message,
                            status=200)

def reset_permission_model():
    """Drop all existing permissions and recreate a handful required for testing"""
    for _ in models.Permission.all():
        _.delete()
    # create and return new permissions (keys)
    return dict(
        init=models.Permission(
            value='init',
            description='Initialization allowed').put(),
        all=models.Permission(
            value='all',
            description='All allowed').put(),
        silent=models.Permission(
            value='silent',
            description='All restricted').put(),
        driving_parking=models.Permission(
            value='driving_parking',
            description='Driving and parking allowed [parking left, parking found]').put()
    )

def set_client_default_permissions(permissions):
    # victims
    zombie = 'secret'               # Ultimate Client Mk.2
    spartan = 'XXXXXXXXXXXXXXXX'    # Default
    sdk_lite = 'LocTest'            # xxx1xxx1xxx1xxx1

    # create zombie: restrict secret from doing anything
    zombie_company = db.GqlQuery('SELECT * FROM Client WHERE apiKey = :1 LIMIT 1', zombie).get()
    if zombie_company:
        zombie_company.permissions.append(permissions['silent'])
        zombie_company.put()
    else:
        logger.info('client with key %s not found' % zombie)

    # create spartan: driving-parking only
    spartan_company = db.GqlQuery('SELECT * FROM Client WHERE apiKey = :1 LIMIT 1', spartan).get()
    if spartan_company:
        spartan_company.permissions.append(permissions['driving_parking'])
        spartan_company.put()
    else:
        logger.info('client with key %s not found' % spartan)

    # SDK-lite mode: only parking, driving and init events are allowed to be sent
    lite_company = db.GqlQuery('SELECT * FROM Client WHERE companyName = :1 LIMIT 1', sdk_lite).get()
    if lite_company:
        lite_company.permissions.extend([permissions['init'], permissions['driving_parking']])
        lite_company.put()
    else:
        logger.info('client with key %s not found' % sdk_lite)

    # grant all permissions to all other clients
    all_clients = {_.key() for _ in models.Client.all()}
    limited_companies = set([_.key() for _ in zombie_company, spartan_company, lite_company])
    all_access_companies = db.get(all_clients ^ limited_companies)
    for _ in all_access_companies:
        _.permissions.append(permissions['all'])
    db.put(all_access_companies)

    return 'Successfully populated.'
#SDK permission operations


#SDK exceptions
class SDK_Exception(Exception):
    def __init__(self, logger, message='General Error', status=HTTP_CODES['BAD_REQUEST']):
        self.message = message
        self.status = status
        self.logger=logger

    def log_error(self):
        '''Can write to file but for now we will write to console'''
        self.logger.error(self.message)

    __str__ = lambda self: self.__repr__()
    __repr__ = lambda self: self.message

class SDK_BadJson_Exception(SDK_Exception):
    '''Folllowing __init__ method is not required (because it is inherited
    from superclass), but left for explicitness #ZenOfPython'''
    # def __init__(self, logger, message, status):
    #     super(self.__class__, self).__init__(logger, message, status)
    pass

class SDK_WrongClientKey_Exception(SDK_Exception):
    def __init__(self, logger, message, status):
        super(self.__class__, self).__init__(logger, message, status)
#SDK exceptions


class JsonProperty(db.TextProperty):
    """Store JSON message natively in datastore using TextProperty"""

    validate = lambda self, value: value

    def get_value_for_datastore(self, model_instance):
        result = super(JsonProperty, self).get_value_for_datastore(model_instance)
        result = json.dumps(result)
        return db.Text(result, encoding='utf-8')

    def make_value_from_datastore(self, value):
        try:
            value = json.loads(str(value))
        except:
            pass
        return super(JsonProperty, self).make_value_from_datastore(value)

