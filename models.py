from datetime import timedelta
import json
import util

from google.appengine.ext import db
from google.appengine.ext import blobstore

# The *proper* thing to do would be to set up a tzinfo class, but...
# Israel time for now = UTC + 3 hours
# Keep updated when needed!
IL_OFFSET = timedelta(hours=3)

# Avoid replicating this all over the place
PARKO_TIME_FMT = '%Y-%m-%d %H:%M:%S'
PARKO_TIME_FMT_FILEFRIENDLY = '%Y_%m_%d_%H_%M_%S'


class Client(db.Model):
    companyName = db.StringProperty()
    apiKey = db.StringProperty()
    permissions = db.ListProperty(db.Key)

    def __repr__(self):
        return '%s | %s | %s' % (self.apiKey, self.companyName, self.get_permissions())

    def get_permissions(self):
        """Get client permissions"""
        return ['%s' % Permission.get(_) for _ in self.permissions]

    show_clients = classmethod(lambda cls: '\n'.join('%s' % _ for _ in cls.all()))

    @classmethod
    def get_client_or_exception(cls, key):
        client = cls.get(key)
        if not client:
            raise db.BadKeyError()
        return client

    @classmethod
    def reset_permissions(cls):
        for _ in cls.all():
            del _.permissions[:]
            _.put()
        return 'Successfully reset all client permissions (no permissions set)'


class Permission(db.Model):
    value = db.StringProperty()
    description = db.TextProperty()

    @property
    def members(self):
        """init = models.Permission.all().filter('value = ','init').get()
        init_members = init.members <- not callable because of being a property"""
        return Client.gql("WHERE permissions = :1", self.key())

    __str__ = lambda self: '%s' % self.value
    __repr__ = lambda self: str(self)


class Message(db.Model):
    MESSAGE_CLASS_CHOICES = ('batch', "single")

    timestamp = db.DateTimeProperty(auto_now_add=True)
    message = util.JsonProperty()
    type = db.StringProperty(choices=MESSAGE_CLASS_CHOICES)

    def __str__(self):
        return '%s message id# %d' % (self.type.capitalize(),
                                      self.message['log']['id'])
    __repr__ = lambda self: str(self)

    def put(self):
        """Override default put method for automatic
        message type (message class) calculation"""
        self.type = self.message['log']['class']
        super(self.__class__, self).put()


class CrashLog(db.Model):
    timestamp = db.DateTimeProperty(auto_now_add=True)
    crash = util.JsonProperty()


class MotionizeLog(db.Model):
    # Primary
    timestamp = db.DateTimeProperty(auto_now_add=True)
    client = db.ReferenceProperty(Client)

    location = db.GeoPtProperty() # has .lat and .lon
    locAccuracy = db.FloatProperty()
    locMinsOld = db.FloatProperty()
    stepCount = db.IntegerProperty()
    upTimeHours = db.FloatProperty()
    distanceToCar = db.FloatProperty() 

    # Possibly refactor these - data duplication
    logReason = db.StringProperty()
    phoneType = db.StringProperty() # ???
    movementState = db.StringProperty()
    report = db.ReferenceProperty(Message, required=False)

    user = db.StringProperty() # <- email address? if so change to EmailProperty later
    temp_user = db.StringProperty(default="Anonymous User") # will be changed every 24hrs

    # Escape hatch
    blob = db.TextProperty()

    @classmethod
    def get_last_init_for(cls, user):
        '''Return last init event for a user or False if not found'''
        query = 'where user= :1 and logReason= :2 order by timestamp desc'
        reason = 'init'
        last_init = cls.gql(query, user, reason).fetch(limit=1)
        return len(last_init) > 0 and last_init[0]


class LogFile(db.Model):
    instigator = db.EmailProperty()
    time_created = db.DateTimeProperty()
    client = db.ReferenceProperty(Client)

    time_from = db.DateTimeProperty()
    time_to = db.DateTimeProperty()
    num_rows = db.IntegerProperty()
    num_rows_requested = db.IntegerProperty()

    file_download = blobstore.BlobReferenceProperty()

    status = db.IntegerProperty()
    class Status:
        processing, done, failed = range(3)

