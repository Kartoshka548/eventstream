#!/bin/bash
### BEGIN INIT INFO
# Provides: 	     copy-to-s3-shutdown.sh
# Required-Start:    $local_fs $network
# Required-Stop:     $local_fs $network
# Default-Start:
# Default-Stop:      0 6 1
# Short-Description: Back up everything left to s3
# Description:       Custom leftovers collector
### END INIT INFO

copy_to_s_tree () {

	BUCKET=logging-event-stream
	NOW=$(date +"%Y-%m-%d-%H:%M")
	HOME_DIR=/home/ubuntu

	#for remainder (live file)
	/usr/bin/s3cmd --mime-type=TEXT/PLAIN --config=${HOME_DIR}/.s3cfg put ${HOME_DIR}/reports/stream.log s3://${BUCKET}/stream.log.${NOW}

	# for all rest which has not been sent to s3 yet
	/usr/bin/s3cmd --mime-type=TEXT/PLAIN --config=${HOME_DIR}/.s3cfg put ${HOME_DIR}/reports/stream.log.20* s3://${BUCKET}/

	# remove all files copied to s3
	rm -f ${HOME_DIR}/reports/stream.log*
}

case "$1" in
  start)
	# No-op
	;;
  restart|reload|force-reload)
	echo "Error: argument '$1' not supported" >&2
	exit 3
	;;
  stop|"")
	copy_to_s_tree
	;;
  terminate)
	copy_to_s_tree
	;;
  *)
	echo "Usage: copy-to-s3-on-shutdown.sh stop" >&2
	exit 3
	;;
esac
:
