FROM alpine:3.20

RUN apk add --no-cache     bash     inotify-tools     docker-cli     tzdata     coreutils     unzip     rsync     python3  && cp /usr/share/zoneinfo/Asia/Taipei /etc/localtime  && echo "Asia/Taipei" > /etc/timezone

ENV TZ=Asia/Taipei
WORKDIR /app

COPY watch.sh /app/watch.sh
RUN chmod +x /app/watch.sh

CMD ["bash","-lc","/app/watch.sh"]
