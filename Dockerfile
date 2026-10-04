FROM eclipse-temurin:8-jdk-jammy

RUN apt-get update && \
    apt-get install -y --no-install-recommends x11-apps x11-utils python3 && \
    rm -rf /var/cache/apt/archives /var/lib/apt/lists/*

COPY jviewer-starter.py /usr/local/bin/
RUN chmod +x /usr/local/bin/jviewer-starter.py

ENV DISPLAY="host.docker.internal:0"
ENV JVIEWER_JAVA_HOME=""
ENV JVIEWER_JAVA_OPTIONS=""

ENTRYPOINT ["/usr/local/bin/jviewer-starter.py"]
