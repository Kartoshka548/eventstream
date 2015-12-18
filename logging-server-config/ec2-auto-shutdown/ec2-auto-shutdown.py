#!/usr/bin/python
import boto.ec2
import croniter
import datetime
import logging


# Check if it is time to act
def time_to_action(sched, now, seconds, logger):
        try:
                cron = croniter.croniter(sched, now)
                d1 = cron.get_prev(datetime.datetime)
                d2 = d1 + datetime.timedelta(0, seconds)
                ret = (now > d1 and now < d2)
                logger.info('Current time   : %s' % now)
                logger.info('Scheduled time : %s' % d1)
                logger.info('Timedelta for action : %s' % d2)
                logger.info('Is time to act : %s' % ret)
        except Exception as e:
                ret = False
                logger.error(e)
        return ret


# Initialize logger
def init_logger():
        logger = logging.getLogger('ec2-auto-shutdown')
        handler = logging.FileHandler('/home/ubuntu/ec2-auto-shutdown/log/ec2-auto-shutdown.log')
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        return logger


# Main
try:
 logger = init_logger()
 now = datetime.datetime.now()
 conn=boto.ec2.connect_to_region('us-east-1')
 reservations = conn.get_all_instances()
 start_list = []
 stop_list = []
 time_delta = 1800        #  Timedelta for action

 for res in reservations:
         for inst in res.instances:
                 name = inst.tags['Name'] if 'Name' in inst.tags else 'Unknown'
                 start_sched = inst.tags['auto:start'] if 'auto:start' in inst.tags else None
                 stop_sched = inst.tags['auto:stop'] if 'auto:stop' in inst.tags else None
                 state = inst.state
                 logger.info('%s (%s) [%s] [%s] [%s]' % (name, inst.id, state, start_sched, stop_sched))
                 if start_sched is not None and state == "stopped" and time_to_action(start_sched, now, time_delta, logger):
                         start_list.append(inst.id)
                 if stop_sched is not None and state == "running" and time_to_action(stop_sched, now, time_delta, logger):
                         stop_list.append(inst.id)
 
 if len(start_list) > 0:
         ret = conn.start_instances(instance_ids=start_list, dry_run=False)
         logger.info('start_instances %s' % ret)
 if len(stop_list) > 0:
         ret = conn.stop_instances(instance_ids=stop_list, dry_run=False)
         logger.info('stop_instances %s' % ret)
except Exception as e:
 logger.error(e)
