from datetime import datetime, timedelta
import os, urllib, json, time
from Queue import Queue
import threading

from google.appengine.ext.db import BadKeyError
from google.appengine.api import channel, users
import webapp2
import jinja2
import models

class ViewLogs(webapp2.RequestHandler):
    template = jenv.get_template('logview.html')
    def get(self):
        key = self.request.get('key')
        client = models.Client.get(key)
        if not client:
            self.response.out.write('Error: invalid/missing key')
            return

        logs = models.MotionizeLog.all().filter('client =', client).order('-timestamp').run(limit=1500)
        logfiles = models.LogFile.all().filter('client = ', client)
        self.response.out.write(ViewLogs.template.render({
            'companyName': client.companyName,
            'logs': logs,
            'logfiles': logfiles,
            'client_key': client.key()}))

class MapLogs(webapp2.RequestHandler):

    # query constants + GAE limits
    QUERY_LIMIT = 150           # matching channels' 32Kb per message
    GAE_QUERY_LIMIT = 1000
    SHOW_RESULTS_DAYS_AGO = 3
    MAX_VISIBLE_MARKERS = 1050  # GAE default: 1000. Increase to 9999 or move controller to frontend.

    template = jenv.get_template('mapview.html')
    current_user = users.get_current_user().email()
    logreasons = (
        # should be part of new model choices / 13 event types w/matching icons
        ('1HOUR',                '1HOUR.png'),
        ('1KM',                  '1KM.png'),
        ('APPROACHING',          'APPROACHING.png'),
        ('DRIVING',              'DRIVING.png'),
        ('MOVINGAWAY',           'MOVINGAWAY.png'),
        ('PARKING',              'PARKING.png'),
        ('READY',                'READY.png'),
        ('STATE_CHANGE',         'STATE_CHANGE.png'),
        ('DrivingLoc',           'DrivingLoc.png'),
        ('ChargerConnected',     'ChargerConnected.png'),
        ('ChargerDisconnected',  'ChargerDisconnected.png'),
        ('WifiConnected',        'WifiConnected.png'),
        ('WifiDisconnected',     'WifiDisconnected.png'),
        # ('Default',              'Default.png'),
    )
    userdict = dict(logreasons=logreasons)

    def motionize_query(self, raw_post_data):
        """ Filter queryset """

        terminally_ill = queryset = models.MotionizeLog.all()
        messages, filters = {}, set(raw_post_data.keys())

        # by timestamp
        try:
            timestamp_from = ('timestampFrom' in filters
                and bool(raw_post_data['timestampFrom'])
                and datetime.strptime(raw_post_data['timestampFrom'], '%B %d, %Y ~ %H:%M'))
            timestamp_to = ('timestampTo' in filters
                and (raw_post_data['timestampTo'])
                and datetime.strptime(raw_post_data['timestampTo'], '%B %d, %Y ~ %H:%M'))
            if not timestamp_from or not timestamp_to:
                raise ValueError()
        except ValueError:
            if not timestamp_from: # init or error
                timestamp_from = datetime.now() - timedelta(days=self.SHOW_RESULTS_DAYS_AGO)
            if not timestamp_to:
                timestamp_to = datetime.now()
        finally:
            messages.update({'timestampFrom': timestamp_from.isoformat(), 'timestampTo': timestamp_to.isoformat()})
            queryset = queryset.filter('timestamp >=', timestamp_from).filter('timestamp <=', timestamp_to)



        # by logreason
        if ('eventtype' in filters
                and set(self.request.get_all("eventtype")) <= {key for (key, value) in self.logreasons}):
            messages.update({'logReason': self.request.get_all('eventtype')})   # remove
            queryset = queryset.filter("logReason IN ", self.request.get_all('eventtype'))



        # by affiliate (company)
        if self.request.get('key') or 'companyname' in filters and bool(raw_post_data['companyname']):
            try:
                client = models.client_or_exception(self.request.GET.get('key')) if self.request.get('key') \
                    else models.Client.gql("WHERE companyName = :name", name=raw_post_data['companyname']).get()

                if not client: # custom ZeroResultsError
                    raise BadKeyError()

            except BadKeyError:
                messages.update({'client': u'Affiliate with provided key was not found'})
                queryset = queryset.filter('client =', 'ABCDEF') # zero results
            else:
                messages.update({'client': client.companyName})
                queryset = queryset.filter('client =', client)



        # by limit
        try:
            limit = (0 < int(self.request.get('limit', self.QUERY_LIMIT) < 1000)
                     and int(self.request.get('limit', self.QUERY_LIMIT))
                     or self.QUERY_LIMIT)
        except ValueError:
            limit = self.QUERY_LIMIT



        # by registered device:
        if 'deviceid' in filters and raw_post_data['deviceid']:
            device_id = raw_post_data['deviceid']
            messages.update({
                'deviceid': (terminally_ill.filter('user =', device_id).count()
                             and device_id
                             or u'No entries for device \'{0}\' found'.format(device_id))})
            queryset = queryset.filter('user =', device_id)



        query_results = queryset.count(limit=self.MAX_VISIBLE_MARKERS)
        messages.update({'matching_entities': query_results})

        offset_queue = Queue()

        if query_results > limit:
            offset = 0
            while query_results > 0:
                offset_queue.put(queryset.order('-timestamp').run(limit=limit, offset=offset))
                offset += limit
                query_results -= limit
        else:
            offset_queue.put(queryset.order('-timestamp').run(limit=limit))

        return offset_queue, messages



    def post(self):

        filtered_queue_with_messages = self.motionize_query(self.request.POST)

        # notifications message
        channel.send_message(users.get_current_user().email(),
                             json.dumps(dict(messages=filtered_queue_with_messages[1])))

        # single thread
        # while not filtered_queue_with_messages[0].empty():
        #
        #     processed_list = map(lambda entity: {
        #                             'ts': entity.timestamp.isoformat(),
        #                             #'unix_timestamp': time.mktime(entity.timestamp.timetuple()) + entity.timestamp.microsecond/1e6,
        #                             'usr': entity.user,
        #                             'cmp': entity.client.companyName,
        #                             'lat': entity.location.lat,
        #                             'lon': entity.location.lon,
        #                             'acc': entity.locAccuracy,
        #                             'res': entity.logReason,
        #                             'mvs': entity.movementState,
        #     }, filtered_queue_with_messages[0].get())
        #
        #     # size of entity in bytes
        #     # for elem in processed_list: elem['Es'] = len(str(elem))
        #
        #     # payload
        #     channel.send_message(users.get_current_user().email(),
        #         json.dumps({'map_data': processed_list, 'message_size': len(json.dumps(dict(map_data=processed_list)))
        #     }))
        #     print "Message Sent."
        #     filtered_queue_with_messages[0].task_done()



        # multithread
        def single_batch_worker(entities):
            processed_list = map(lambda entity: {
                                'ts': entity.timestamp.isoformat(),
                                #'unix_timestamp': time.mktime(entity.timestamp.timetuple()) + entity.timestamp.microsecond/1e6,
                                'usr': entity.user,
                                'cmp': entity.client.companyName,
                                'lat': entity.location.lat,
                                'lon': entity.location.lon,
                                'acc': entity.locAccuracy,
                                'res': entity.logReason,
                                'mvs': entity.movementState,
            }, entities)

            #  payload
            channel.send_message(users.get_current_user().email(), json.dumps({
                'map_data': processed_list,
                'message_size': len(json.dumps(dict(map_data=processed_list)))
            }))
            print "Message Sent."


        exitFlag = 0

        class MotionizeThread(threading.Thread):

            def __init__(self, threadID, name, motionize_queue):
                threading.Thread.__init__(self)
                self.threadID, self.name, self.que = threadID, name, motionize_queue

            def run(self):
                print "Starting " + self.name
                process_entities(self.name, self.que)
                print "Exiting " + self.name


        def process_entities(thread_name, que):
            while not exitFlag:
                queueLock.acquire()
                if not entities.empty():
                    single_batch_worker(que.get());
                    queueLock.release()
                else:
                    queueLock.release()

        entities = filtered_queue_with_messages[0]
        queueLock = threading.Lock()
        threads = []
        thread_id = 1

        # Create threads
        for thread_title in ["Thread-1", "Thread-2", "Thread-3", "Thread-4", "Thread-5"]:
            thread = MotionizeThread(thread_id, thread_title, entities)
            thread.start()
            threads.append(thread)
            thread_id += 1

        # Wait for queue to empty
        while not entities.empty():
            pass

        # Notify threads it's time to exit
        exitFlag = 1

        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        print "Exiting Main Thread"


    def get(self):
        if 'X-Requested-With' in self.request.headers or self.request.is_xhr:
            self.response.set_status(code=400, message='Wrong method')
        self.userdict.update({'token': channel.create_channel(self.current_user)})

        if self.request.get('key'):
            try:
                client = models.Client.get(self.request.get('key')) # company
                self.userdict.update({'client': client})
            except BadKeyError:
                self.userdict.update({'user_not_found': 'User with this key was not found'})
        self.response.out.write(self.template.render(self.userdict))



class ClientApi(webapp2.RequestHandler):
    def get(self):
        return self.post()

    def post(self):
        if self.request.get('mode') is None: return

        self.response.headers['Content-Type'] = 'application/json'
        if self.request.get('mode') == 'create_new':
            if not self.request.get('name') or not self.request.get('key'):
                res = {'status': 'error', 'msg': 'Malformed message' }
            else:
                try:
                    name = str(self.request.get('name'))
                    key = str(self.request.get('key'))
                    client = models.Client(companyName=name, apiKey=key)
                    client.put()
                except RuntimeError as e:
                    res = {'status': 'error', 'msg': str(e)}
                else:
                    res = {'status': 'ok'}
            self.response.out.write(json.dumps(res))

        elif self.request.get('mode') == 'update':
            if not self.request.get('key') or not self.request.get('name'):
                res = {
                    'status': 'error',
                    'msg': 'Malformed message'}
            else:
                client = models.Client.get(str(self.request.get('key')))
                if not client:
                    res = {
                        'status': 'error',
                        'msg': 'Invalid client'}
                else:
                    try:
                        client.companyName = self.request.get('name')
                        client.put()
                    except RuntimeError as e:
                        res = {
                            'status': 'error',
                            'msg': str(e)}
                    else:
                        res = {'status': 'ok'}
            self.response.out.write(json.dumps(res))


app = webapp2.WSGIApplication([('...', MapLogs),
                               ('...', ClientApi)], debug=True)

