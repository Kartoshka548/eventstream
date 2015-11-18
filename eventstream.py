import logging
import json
import itertools
import pprint
import collections
from datetime import datetime
import time
import sys, traceback

import webapp2
from google.appengine.ext import db
from google.appengine.api import memcache
from google.appengine.api import urlfetch

import models
import util

# from google.appengine.ext import deferred
#
# def do_something_expensive(a, b, c=None):
#       logging.info("Doing something expensive!")
#
# deferred.defer(do_something_expensive, "Hello, world!", 42, c=True)
logger = logging.getLogger(__name__)

REPORT_PROCESSING_MACHINE = 'http://api.parko.com/sdk/'
REPORT_PROCESSING_MACHINE_DEV = 'http://dev.parko.com/sdk/'
#REPORT_PROCESSING_MACHINE = 'http://127.0.0.1:9000/sdk/' # also remove/add wait() at call end
QUEUE = 'Event-Location-Stream'


class Policy(object):
    """
    Policy codes:
        1xx: {							# technical information
            100: update to latest
            101: updated
            102: update success
            103: update failed
        }
        2xx: {
        ----REPORTED STATE----					# requests (mobile -> server)
            200: single mode (no batch, no old unsent)
            210: batch mode
            211: single message (middle of batch)
            221: single message (previously unsent data exists)
        }
        3xx: {
        ----SET, DO, SEND-----					# responses (server -> mobile)
            300: set single mode
            310: set batch mode
            302: send metadata (init message)
            321: send previously stored data
            324: send all unsent data from last 24 hours
            348: send all unsent data from last 48 hours

        }
        9xx: {							# disabling functionality
            900: disable reporting
            901: reporting disabled (till next restart)
        }
    """
    pass


class EventMetaClass(type):
    """SDK may send only events which were received from server on init.
     This implies that server can control event types it wants to work with.

    1. Basic Event structure is:
        event_description
            (event priority,
             event code,
            [optional: mixin parameters added to standard response])

    2. Events are sorted by predefined codes which indicate .
        1xx: {              # Stable position(s) - At home / work
            100: ArrivedToDestination
            101: Hourly,... }

        2xx: {              # Slow movement, (Walking, Running)
            200: WalkingTowardsDestination
            210: WalkingTowardsLastParking,... }

        3xx: {              # Normal movement (Driving, Cycling)
            300: Driving
            310: DrivingLocation,... }

        9xx: {              # Meta
            900: ChargerConnected
            901: WifiConnected,... }
    """
    _events = {
        'init':                     (90, 900, ['init_mixin', 'permissions', 'notifications']),
        'parking':                  (80, 800, ['parking_mixin', 'local_offers_mixin']),
        'driving':                  (70, 300, ['driving_mixin', 'fueling_stations_mixin']),
        'drivingLocation':          (30, 310, []),
        'hourly':                   (20, 101, []),
        'ArrivedToDestination':     (20, 100, []),
        'default':                  (20, 911, []),
    }

    def __getattr__(cls, key):
        """Description of the event if it was defined - or 'default'
        Usage: Events.parking"""
        return key in cls._events and key or 'default'

    def get_priority(cls, key):
        """Priority of the event if event defined - or default priority
        Usage: Events.get_priority(Events.driving)"""
        event = cls._events.get(key) or cls._events['default']
        return event[0]

    def get_code(cls, key):
        """Event code  or 'default' event code
        Usage: Events.get_code(Events.driving)"""
        event = cls._events.get(key) or cls._events['default']
        return event[1]

    def get_mixins(cls, key):
        """Optional mixin(s) per dominant event, if defined, or default mixin(s) - if any at all
        Usage: Events.get_mixins(Events.init)"""
        event = cls._events.get(key) or cls._events['default']
        return event[2]


class Events(object):
    """Events have associated mixins  which are sent
    to clients on message acceptance and processing"""
    __metaclass__ = EventMetaClass


class ClientCache(object):
    """Identify client from database or raise ValueError"""

    def __get__(self, inst, inst_cls):
        return memcache.get(inst.__client_key)

    def __set__(self, inst, provider_key):
        inst.__client_key = provider_key or 'EmptyClientKey'
        client_in_cache = self.__get__(inst, inst.__class__)
        if not client_in_cache:
            client_in_cache = models.Client.all().filter('apiKey =', inst.__client_key).get()
            if not client_in_cache:
                raise util.SDK_WrongClientKey_Exception(status=400,
                                                        logger=logger,
                                                        message='Invalid client key: %s' % inst.__client_key)
            memcache.add(key=inst.__client_key, value=client_in_cache, time=24*60*60)


class EventStream(webapp2.RequestHandler):

    client = ClientCache()
    ML_BLOB = ('id', 'speed', 'bearing', 'altitude', 'timestamp', 'legacy')
    MANDATORY_LEGACY_DATA = ('locMinsOld', 'stepCount', 'upTimeHours', 'distanceToCar')

    # Synchronization is the root of most evil in distributed world.
    # Many shards will receive same class variable - with higher request rate duplicate ids may appear.
     # Per-shard, (independent) counter should be done later if tracking is important.
    id_report = 0

    # add_to_queue = util.SQS_Queue(QUEUE)

    exc_or_refusal = lambda self, exc, msg='Refused.': self.debug_mode and exc or msg

    def _priority_sort(self, events):
        """Sort events in a list by their priority  (from lowest to highest)
        and return last one (having highest priority)"""
        events.sort(key=lambda dct: Events.get_priority(dct.get('description', 'default')))
        trigger = events[-1]
        if self.debug_mode:
            logger.info('Message major: %s event (having priority %d)' % (
                trigger['description'],
                Events.get_priority(trigger['description'])))
        return trigger

    @classmethod
    def _flattener(cls, _dict):
        items = []
        for key, value in _dict.items():
            if isinstance(value, collections.MutableMapping):
                # inner = getattr(cls, sys._getframe().f_code.co_name)(value)
                inner = cls._flattener(value)
                items.extend(inner)
            else:
                items.append((key, value))
        return items

    @classmethod
    def dict_recursive_search(cls, _dict, _key):
        if _key in _dict:
            return _dict[_key]
        for dict_value in _dict.itervalues():
            if isinstance(dict_value, dict):
                item = cls.dict_recursive_search(dict_value, _key)
                if item is not None:
                    return item

    def preprocess(self, message_list):
        """Process single message and return generator,
         containing positions array and a mixin"""
        for message in iter(message_list):
            mixin = dict(meta=message['meta'], legacy=message['legacy'])
            yield message['positions'], mixin

    def process(self, position, common, mixin):
        """Event processor. Good place to examine event type as well
        and set relevant flag for message response which is calculated later"""

        cls =           self.__class__
        ml_entry =      models.MotionizeLog()
        dt =            datetime.fromtimestamp(position['timestamp']/1000.0)
        user =          common['user']
        phone_type =    common['phone_type']
        event =         position['status']['event']
        legacy =        mixin['legacy']
        try:
            ml_entry.user =             user
            ml_entry.temp_user =        util.tempuser(user=user, date=dt)
            ml_entry.location =         db.GeoPt(*position['location'])
            ml_entry.locAccuracy =      float(position['accuracy'])
            ml_entry.movementState =    position['status']['state']['description']
            ml_entry.logReason =        event['description']
            ml_entry.client =           common['client']
            ml_entry.report =           common['report_id']
            ml_entry.phoneType =        phone_type

            # required legacy data which may not be sent (optimization policy)
            for _ in cls.MANDATORY_LEGACY_DATA:
                val = legacy.get(_, 0.0)
                try:
                    setattr(ml_entry, _, val)
                except db.BadValueError:
                    setattr(ml_entry, _, 0)
        except Exception as exc:
            logging.warning('Broken data: %s', exc)
            raise

        self.event_list.append(event)        # events encountered in given message
        complete_probe_info = cls._flattener(position) + mixin.items()
        extra = {_:__ for _,__ in complete_probe_info if _ in cls.ML_BLOB}

        if event['description'] == 'init':
            extra['meta'] = mixin['meta']
        ml_entry.blob = json.dumps(extra)

        return ml_entry

    def reveal_phone_type(self, user, message_type):
        """Full user metadata sent only when init generated.
        If message has no relevant metadata, extract it from user's last init."""
        if message_type == models.Message.MESSAGE_CLASS_CHOICES[0]:
            for single in self.report['messages']:                  # batch
                meta = single['meta']
                phone_type = 'environment' in meta and meta['environment']['os']['type']
                if phone_type:
                    break
        else:
            _meta_env = self.report['meta'].get('environment', {})  # single
            phone_type = self.dict_recursive_search(_meta_env, 'type')
        if not phone_type:
            _last_user_init = models.MotionizeLog.get_last_init_for(user)
            phone_type = _last_user_init and _last_user_init.phoneType or 'undefined'
        return phone_type

    def debug_assistant(self, events):
        """On-screen developer help to understand how json got interpreted into single event(s)"""
        response = '\n\n'.join(
            pprint.pformat({_: json.loads(entry.blob)
                if _ == 'blob'
                else getattr(entry, _)
                    for _ in entry._all_properties}, width=100)
                        for entry in list(events))
        yield response

    def response_builder(self, rawmessage, events):
        """Policies & meta"""

        # standard response
        response = self.response_default(rawmessage)

        if rawmessage.type == models.Message.MESSAGE_CLASS_CHOICES[1]:
            response['legacy'] = True

        # high priority messages trigger dynamic response mix-in
        # currently batch message does not have priority setting
        if rawmessage.message['log'].get('priority') > 59:
            response.update(self.response_priority_mixin(events))
        yield json.dumps(response)

    def response_default(self, rawmessage):
        """Returned on every message sent"""
        return {
            "timestamp": int(time.time()*1000),
            "log": {
                "id": rawmessage.message['log']['id'],
                "class": rawmessage.type,
                "response": util.HTTP_CODES['CREATED'],
            },
            "meta": {
                "policy": {
                    "id": 300,
                    "timeout": 600,
                    "interval": 15,
                    "size": 10,
                },
            },
        }

    def response_priority_mixin(self, events):
        """Actual mixin depends on type of specific event"""
        mixin = {}
        trigger = self._priority_sort(events)

        # in verbose mode, response should include triggering event
        if self.verbose_mode:
            mixin['trigger'] = {'description': trigger['description'],
                                'code': trigger['code']}

        for key in Events.get_mixins(trigger['description']):
            if key == 'permissions':
                mixin['permissions'] = {'events': self.client.get_permissions()}
            else:
                mixin[key] = {}
        if self.debug_mode:
            logger.info(json.dumps(mixin))
        return mixin

    def split_and_save(self, rawmessage):
        """Split raw message into individual events and prepare them for storage in a datastore"""
        common = {
            'report_id':    rawmessage,
            'client':       self.client,
            'user':         self.subscription['uid'],
            'phone_type':   self.reveal_phone_type(message_type=rawmessage.type,
                                                   user=self.subscription['uid']),
        }
        iterable = (self.report['messages'] if rawmessage.type == models.Message.MESSAGE_CLASS_CHOICES[0]
                    else [self.report])
        position_gnr = self.preprocess(iterable)

        # itertools.chain not good: required processing in middle
        # flattening into generator idea: (elem for iterable in iterables for elem in iterable)
        #
        # for positions, mixin in pandora_box:
        #     for _ in itertools.imap(self.position_processor,
        #                             positions,
        #                             itertools.repeat(common),
        #                             itertools.repeat(mixin)):
        #         yield _

        entries = (_ for (positions, mixin) in position_gnr for _ in itertools.imap(
            self.process,
                positions,
                itertools.repeat(common),
                itertools.repeat(mixin)))
        return self.save(rawmessage, entries) # passing generator

    def save(self, rawmessage, entities):
        if self.debug_mode:
            entities, _entities_ = itertools.tee(entities, 2)
            response = self.debug_assistant(_entities_)
        else:
            # executed after db.put(): also a generator
            response = self.response_builder(rawmessage, self.event_list)
        db.put(entities)
        return next(response)

    def post(self):
        """Main entrance door for SDK event stream storage facility"""

        # if config:show, config:reset, config:populate
        if util.permission_config(self.request.body, self.response):
            return

        if self.request.body == '':
            return util.return_early(self.response,
                                     message='Empty requst body?',
                                     status=util.HTTP_CODES['BAD_REQUEST'])

        # #------------------------------------------------
        # #--------------- AWS communication --------------
        # #------------------------------------------------
        # rpc, rpc_dev, rpc_sqs = [urlfetch.create_rpc() for _ in range(3)]
        # # async
        # urlfetch.make_fetch_call(rpc, REPORT_PROCESSING_MACHINE, urllib.urlencode(report), "POST") #.wait()
        # # urlfetch.make_fetch_call(rpc_dev, REPORT_PROCESSING_MACHINE_DEV, urllib.urlencode(report), "POST") #.wait()
        #
        # # TODO: make async
        # SQS_Report = util.SQS_Report(id=StoreLog.id_report, content=dict(report))
        # # mess_id = StoreLog.add_to_queue(SQS_Report)
        # #------------------------------------------------

        cls = self.__class__
        cls.id_report += 1
        self.event_list = list() # data pushed into this container in position_processor module
        try:
            try:
                #make report available to other methods
                self.report = json.loads(self.request.body)
                self.report['log']['gae-seq'] = cls.id_report        #TODO: universal ticketing
            except ValueError:
                self.debug_mode = True
                raise util.SDK_BadJson_Exception(
                    status=util.HTTP_CODES['BAD_REQUEST'],
                    logger=logger,
                    message='Invalid JSON object.\nPlease validate with http://www.jsonlint.com and resubmit.')

            self.debug_mode = self.report.pop('debug', None)
            self.verbose_mode = self.report.pop('verbose', None)
            rawmessage = models.Message(message=self.report)
            rawmessage.put()

            # TODO: Message entity may extract subscription information
            self.subscription = self.report['meta'].get('subscription')
            self.client = self.subscription['provider']

            # re-enable to write to console
            # logger.info('%d | %s %d | %s' % (cls.id_report,
            #                                  rawmessage.type,
            #                                  self.report['log']['id'],
            #                                  self.client.companyName))

            response_object = self.split_and_save(rawmessage)
        except util.SDK_Exception as exc:
            # exc.log_error()
            self.response.set_status(exc.status)
            response_object = self.exc_or_refusal(exc, 'Refused (trivial).')
        except Exception:
            # Unpredicted exceptions
            exc = str().join(traceback.format_exception(*sys.exc_info()))
            logger.error(exc)
            self.response.set_status(util.HTTP_CODES['BAD_REQUEST'])
            response_object = self.exc_or_refusal(exc)
        else:
            self.response.set_status(util.HTTP_CODES['CREATED'])
        finally:
            self.response.out.write(response_object)


app = webapp2.WSGIApplication([('/eventstream/', EventStream)])