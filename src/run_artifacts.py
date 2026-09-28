"""Private run evidence archives, using existing run/event dictionaries."""
import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import zipfile

import boto3
from botocore.config import Config

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024


def archive_key(identity):
    return f'runs/{identity}/worker.zip'


def s3():
    return boto3.client('s3', config=Config(connect_timeout=5, read_timeout=60,
                                          retries={'max_attempts': 3, 'mode': 'standard'}))


def inspect_archive(content, identity):
    """Validate bounded evidence without extracting untrusted paths to disk."""
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError('Evidence archive exceeds 100 MiB')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
            raise ValueError('Evidence archive exceeds file or expanded-size limit')
        names = set()
        files = []
        for item in infos:
            path = PurePosixPath(item.filename)
            if (item.filename in names or path.is_absolute() or '..' in path.parts
                    or '\\' in item.filename or ':' in item.filename or '\x00' in item.filename
                    or item.flag_bits & 1 or item.file_size > 50 * 1024 * 1024):
                raise ValueError('Evidence archive contains an invalid entry')
            names.add(item.filename)
            if not item.is_dir():
                files.append({'path': item.filename, 'size_bytes': item.file_size})
        if 'manifest.json' not in names:
            raise ValueError('Evidence manifest is required')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict) or str(manifest.get('run_id')) != str(identity):
            raise ValueError('Evidence manifest does not match this run')
        if not isinstance(manifest.get('missing_capture', []), list) or not all(isinstance(item, str) for item in manifest.get('missing_capture', [])):
            raise ValueError('Evidence manifest capture gaps must be strings')
        if archive.testzip() is not None:
            raise ValueError('Evidence archive checksum failed')
    return {'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content),
            'files': files, 'manifest': manifest}


def store_archive(identity, content):
    bucket = os.environ.get('REPORT_BUCKET')
    if not bucket:
        raise RuntimeError('Private report storage is not configured')
    s3().put_object(Bucket=bucket, Key=archive_key(identity), Body=content,
                    ContentType='application/zip', ServerSideEncryption='AES256')


def load_archive(identity):
    response = s3().get_object(Bucket=os.environ['REPORT_BUCKET'], Key=archive_key(identity))
    try:
        content = response['Body'].read(MAX_ARCHIVE_BYTES + 1)
    finally:
        response['Body'].close()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError('Stored archive exceeds configured limit')
    return content


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n').encode()


def worker_record(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        if 'total_run.json' in names:
            return json.loads(archive.read('total_run.json'))
        if 'events.jsonl' in names:
            records = []
            for line in archive.read('events.jsonl').splitlines():
                try: records.append(json.loads(line))
                except (ValueError, UnicodeError): pass  # A crash can truncate the final line.
            return records
    return []


def evidence_bundle(record, content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('server.json', json_bytes(record))
        bundle.writestr('missing_capture.json', json_bytes(record['missing_capture']))
        bundle.writestr('README.txt', 'CTRL Loop run evidence\n\nserver.json contains the user request, exact available OpenAI input/output, delivery events, outcome and capture gaps.\nworker/ contains the original worker evidence, Jev requests/responses, native calls, timings and screenshots when captured.\nDurations are measured within their originating machine; do not subtract server and worker timestamps for precise latency.\nUsage-based costs are estimates, not invoices. HTTP authentication headers are intentionally excluded.\n')
        if content:
            with zipfile.ZipFile(io.BytesIO(content)) as worker:
                for item in worker.infolist():
                    if not item.is_dir(): bundle.writestr('worker/' + item.filename, worker.read(item))
    return output.getvalue()
