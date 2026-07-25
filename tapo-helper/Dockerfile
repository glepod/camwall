FROM python:3.12-slim
RUN pip install --no-cache-dir pytapo==3.4.18
COPY osd.py /osd.py
ENTRYPOINT ["python", "/osd.py"]
