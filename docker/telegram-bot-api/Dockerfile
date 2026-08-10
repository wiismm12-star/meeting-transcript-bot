FROM ubuntu:24.04 AS build
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates cmake g++ git gperf libssl-dev make zlib1g-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git .
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --target install -j2

FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates libssl3 zlib1g && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
EXPOSE 8081
ENTRYPOINT ["telegram-bot-api"]
