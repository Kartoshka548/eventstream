1. sudo cp logsocket* /etc/init.d/
2. sudo cp ./copy-to-s3-on-shutdown.sh /etc/init.d/
3. cd /etc/init.d/ ; update-rc.d copy-to-s3-on-shutdown.sh stop 99 0 1 6 .
4. cd ~ ; mkdir reports; mkdir dev
5. touch reports/stream.log dev/stream.log
6. sudoo cp logsocket*.py /usr/local/sbin/
7. install s3cmd.
8. sudo cp .s3cfg /root/; cp .s3cfg ~/
9. sudo cp logging-event-stream /etc/logrotate.d/
10. sudo cp crontab /etc/
11. cp -r ./ec2-auto-shutdown ~/
12. open port 9020 and 9022 on ec2 instance
