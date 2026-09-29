FROM python:3.12-slim

# pytest is the only extra tool the agent gets; everything else is the stock slim image.
RUN pip install --no-cache-dir pytest==8.4.2 \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

COPY init.py fsops.py runcmd.py egress_proxy.py /opt/codenode/
RUN chmod 0555 /opt/codenode/*.py && mkdir -p /workspace /home/agent

WORKDIR /workspace
USER 65534:65534
CMD ["/usr/local/bin/python3", "-I", "-S", "/opt/codenode/init.py"]
