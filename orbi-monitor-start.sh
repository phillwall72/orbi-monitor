docker rm -f orbi-monitor

docker build -t orbi-monitor .

docker run -d \
 --name orbi-monitor \
 --restart unless-stopped \
 --shm-size=1g \
 -v /mnt/user/appdata/orbi-monitor/data:/data \
 -e ORBI_IP=192.168.0.1 \
 -e ORBI_PASSWORD=2vdZ87.FQ3A- \
 -e GMAIL_USER=phillwall72@gmail.com \
 -e GMAIL_APP_PASS="eipo lkne lajg cuek" \
 -e ALERT_TO=phillwall72@icloud.com \
 -e CHECK_INTERVAL_SECS=600 \
 orbi-monitor
