#!/usr/bin/env python
import select
import pickle
import logging
import logging.handlers
import logging.config
import SocketServer as socketserver
import struct
import os


# Logging configuration
LOG_BASEPATH = os.path.expanduser('~ubuntu') # '/var/log/parko'
REPORT_LOG_PATH = 'dev/stream.log'
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,

    'formatters': {
        'standard': {
            'format': "[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] %(message)s",
            'datefmt': "%d/%b/%Y %H:%M:%S"
        },
        'store': {
            'format': '%(asctime)s\n%(created)f\n%(message)s',
            # unixtimestamp + microseconds - utcdatetime - message
        },
    },
    'handlers': {
        'locally_store_report': {       # save to file
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'formatter': 'store',
            'filename': '%s' % os.path.join(LOG_BASEPATH, REPORT_LOG_PATH),
            'when': 'H', # D
            'interval': 12, # 1
            'backupCount': 7*2, # 4*7
            'utc': True,
        },
    },
    'loggers': {
        'stream': {
            'handlers': ['locally_store_report',],
        },
    }
}


class LogRecordStreamHandler(socketserver.StreamRequestHandler):
    """Handler for a streaming logging request.

    This basically logs the record using whatever logging policy is
    configured locally.
    """

    def handle(self):
        """
        Handle multiple requests - each expected to be a 4-byte length,
        followed by the LogRecord in pickle format. Logs the record
        according to whatever policy is configured locally.
        """
        while True:
            chunk = self.connection.recv(4)
            if len(chunk) < 4:
                break
            slen = struct.unpack('>L', chunk)[0]
            chunk = self.connection.recv(slen)
            while len(chunk) < slen:
                chunk = chunk + self.connection.recv(slen - len(chunk))
            obj = self.unPickle(chunk)
            record = logging.makeLogRecord(obj)
            self.handleLogRecord(record)

    def unPickle(self, data):
        return pickle.loads(data)

    def handleLogRecord(self, record):
        # if a name is specified, we use the named logger rather than the one
        # implied by the record.
	#if self.server.logname is not None:
	#    name = self.server.logname
	#else:
	#    name = record.name
        logger.handle(record)


class LogRecordSocketReceiver(socketserver.ThreadingTCPServer):
    """
    Simple TCP socket-based logging receiver suitable for testing.
    """
    allow_reuse_address = 1

    def __init__(self, host='',
                 port=9022,
                 handler=LogRecordStreamHandler):
        socketserver.ThreadingTCPServer.__init__(self, (host, port), handler)
        self.abort = 0
        self.timeout = 1
        self.logname = None

    def serve_until_stopped(self):
        abort = 0
        while not abort:
            rd, wr, ex = select.select([self.socket.fileno()],
                                       [], [],
                                       self.timeout)
            if rd:
                self.handle_request()
            abort = self.abort

def main():
    logging.config.dictConfig(LOGGING)
    tcpserver = LogRecordSocketReceiver()
    print('Starting logging server.')
    tcpserver.serve_until_stopped()

if __name__ == '__main__':
    logger = logging.getLogger('stream')
    main()


