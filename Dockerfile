FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py \
     image_uploader.py \
     main.py \
     markdown_parser.py \
     notion_api.py \
     notion_to_md.py \
     sync_state.py \
     utils.py \
     version.py \
     ./

# Docs are mounted at runtime: -v /full/host/path:/workspace
RUN mkdir /workspace

ENTRYPOINT ["python", "/app/main.py"]
