"""Outbound worker with durable evidence delivery; native input is never replayed."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID, uuid4

import httpx2
from .reporting import RunReport, package_report
from .worker import execute_instruction


class WorkerAssignmentError(RuntimeError):
    """A credential, delivery, or journal belongs to a different assigned VM."""


async def main():
    endpoint = os.environ['CTRLLOOP_API_URL'].rstrip('/')
    if not endpoint.startswith('https://'):
        raise RuntimeError('Worker delivery requires HTTPS')
    expected_worker_id = os.environ['CTRLLOOP_WORKER_ID']
    expected_environment = os.environ['CTRLLOOP_ENVIRONMENT']
    if not expected_worker_id or expected_environment not in ('dev', 'prod'):
        raise WorkerAssignmentError('An explicit worker identity and environment are required')
    state = Path(os.environ.get('CTRLLOOP_WORKER_STATE', str(Path.home()/'.local/state/ctrlloop-worker')))
    state.mkdir(parents=True, exist_ok=True)
    # A system-wide per-user lock also prevents two revision services racing.
    import fcntl
    lock = (state/'worker.lock').open('a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    journal = state/'active-run.json'

    def write_journal(value):
        temporary = state/'active-run.tmp'
        with temporary.open('w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(journal)
        directory = os.open(state, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def delivery_record(pending, event, **details):
        directory = Path(pending['report_directory'])
        directory.mkdir(parents=True, exist_ok=True)
        with (directory/'delivery.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp': datetime.now(timezone.utc).isoformat(),
                                     'event': event, **details}, ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())

    revision = os.environ.get('CTRLLOOP_REVISION', 'unknown')
    async with httpx2.AsyncClient(base_url=endpoint, headers={'Authorization': 'Bearer '+os.environ['WORKER_TOKEN']}, timeout=25) as client:
        async def send(path, body):
            response = await client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        def validate_assignment(value):
            if (not isinstance(value, dict) or value.get('worker_id') != expected_worker_id
                    or value.get('environment') != expected_environment):
                raise WorkerAssignmentError('Worker credential does not match the configured VM and environment')
            try:
                UUID(str(value['company_id']))
                UUID(str(value['assigned_user_id']))
            except (KeyError, TypeError, ValueError, AttributeError):
                raise WorkerAssignmentError('Worker handshake has no valid company and user assignment') from None

        # A copied token must never redirect this configured VM to another user.
        assignment = await send('/api/worker/heartbeat', {})
        validate_assignment(assignment)

        async def check_assignment():
            current = await send('/api/worker/heartbeat', {})
            validate_assignment(current)
            if any(current[key] != assignment[key] for key in ('company_id', 'assigned_user_id')):
                raise WorkerAssignmentError('Worker assignment changed; explicit operator review is required')

        def validate_run_context(value, *, legacy_journal=False):
            if not isinstance(value, dict):
                raise WorkerAssignmentError('Worker delivery is not a run record')
            try:
                identity = str(UUID(str(value['id'])))
            except (KeyError, TypeError, ValueError, AttributeError):
                raise WorkerAssignmentError('Worker delivery has no valid run identity') from None
            keys = ('company_id', 'worker_id', 'initiated_by')
            if legacy_journal and not any(key in value for key in keys):
                # Old journals are never re-executed. Each evidence/result
                # request still requires the server's per-run worker access.
                return identity
            if (value.get('company_id') != assignment['company_id']
                    or value.get('worker_id') != expected_worker_id
                    or value.get('initiated_by') != assignment['assigned_user_id']):
                raise WorkerAssignmentError('Run or journal does not belong to this assigned company, user, and VM')
            return identity

        async def flush_events(pending, *, terminal=False):
            while pending.get('events'):
                body = pending['events'][0]
                started = perf_counter_ns()
                try:
                    await send(f"/api/worker/runs/{pending['id']}/events", body)
                except httpx2.HTTPStatusError as error:
                    if error.response.status_code != 409 or not terminal:
                        raise
                    delivery_record(pending, 'event_rejected_after_terminal', event_id=body['id'])
                else:
                    delivery_record(pending, 'event_delivered', event_id=body['id'],
                                    duration_ms=(perf_counter_ns()-started)/1_000_000)
                pending['events'].pop(0)
                write_journal(pending)

        # Only evidence/result requests are retried after a crash, never execution.
        async def settle_journal():
            if not journal.exists():
                return
            pending = json.loads(journal.read_text())
            validate_run_context(pending, legacy_journal=True)
            interrupted = not pending.get('finish')
            if not pending.get('report_directory'):
                recovered = RunReport(Path(os.environ.get('CTRLLOOP_REPORT_DIR', 'reports')), run_id=pending['id'])
                recovered.append('run_interrupted', reason='Legacy worker journal contains no evidence directory.')
                recovered.close()
                pending['report_directory'] = str(recovered.requests_path)
            if interrupted:
                pending['finish'] = {'status': 'interrupted',
                                     'error': 'Worker restarted during execution. Actions were not replayed; inspect the desktop before a new run.',
                                     'result': {'run_id': pending['id'], 'report_directory': pending['report_directory']}}
                pending['interrupted'] = True
                write_journal(pending)
            await flush_events(pending, terminal=True)
            upload_failure = None
            permanent_upload_failure = False
            if not pending.get('artifacts_uploaded'):
                try:
                    archive, manifest = await asyncio.to_thread(package_report, Path(pending['report_directory']),
                                                                run_id=pending['id'], interrupted=pending.get('interrupted', False))
                    pending['finish']['result']['capture'] = {key: manifest[key] for key in ('complete', 'missing_capture')}
                    write_journal(pending)
                    if archive.stat().st_size > 100*1024*1024:
                        raise ValueError('Worker evidence archive exceeds the 100 MiB upload limit; full local evidence is retained.')
                    response = await client.post(f"/api/worker/runs/{pending['id']}/artifacts",
                                                 content=await asyncio.to_thread(archive.read_bytes),
                                                 headers={'Content-Type': 'application/zip'}, timeout=120)
                    response.raise_for_status()
                    pending['artifacts_uploaded'] = True
                    pending['finish']['result'].pop('artifact_upload_error', None)
                    write_journal(pending)
                except Exception as error:
                    upload_failure = error
                    permanent_upload_failure = isinstance(error, ValueError) or (
                        isinstance(error, httpx2.HTTPStatusError) and error.response.status_code in (400, 413))
                    pending['finish']['result']['artifact_upload_error'] = (
                        'Worker evidence is retained locally; archive upload requires repair before retry ('
                        if permanent_upload_failure else
                        'Worker evidence is retained locally; archive upload is pending retry (') + type(error).__name__ + ').'
                    pending['artifact_retryable'] = not permanent_upload_failure
                    if isinstance(error, httpx2.HTTPStatusError):
                        pending['artifact_upload_http_status'] = error.response.status_code
                    write_journal(pending)
            if not pending.get('finish_acknowledged'):
                try:
                    await send(f"/api/worker/runs/{pending['id']}/finish", pending['finish'])
                except httpx2.HTTPStatusError as error:
                    if error.response.status_code != 409:
                        raise
                pending['finish_acknowledged'] = True
                write_journal(pending)
            if upload_failure:
                if permanent_upload_failure:
                    # Preserve the failed upload receipt and every local file,
                    # but release the one active-run slot for future jobs. This
                    # archived journal is never treated as work to execute.
                    failed = state / 'failed-evidence'
                    failed.mkdir(mode=0o700, exist_ok=True)
                    destination = failed / (str(UUID(str(pending['id']))) + '.json')
                    journal.replace(destination)
                    for directory_path in (state, failed):
                        descriptor = os.open(directory_path, os.O_RDONLY)
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    print(json.dumps({'stage': 'artifact_upload_requires_repair', 'run_id': pending['id'],
                                      'type': type(upload_failure).__name__}), flush=True)
                    return
                # Completion is visible even when artifact storage is unavailable.
                # The retained journal retries only upload on restart/reconnect.
                raise upload_failure
            journal.unlink()

        async def heartbeat():
            while True:
                try:
                    await send('/api/worker/heartbeat', {})
                except Exception:
                    pass
                await asyncio.sleep(5)

        heartbeat_task = asyncio.create_task(heartbeat())
        recovery_needed = False
        try:
            await settle_journal()
            await send('/api/worker/recover', {})
            while True:
                try:
                    await settle_journal()
                    if recovery_needed:
                        await send('/api/worker/recover', {})
                        recovery_needed = False
                    try:
                        payload = await send('/api/worker/poll', {'revision': revision})
                    except Exception:
                        # A lost poll response may have committed delivery in the DB.
                        recovery_needed = True
                        await send('/api/worker/recover', {})
                        recovery_needed = False
                        raise
                    run = payload.get('run')
                    if not run:
                        await asyncio.sleep(2)
                        continue
                    identity = validate_run_context(run)
                    recovery_needed = True
                    try:
                        await check_assignment()
                    except WorkerAssignmentError:
                        raise
                    except Exception:
                        # Poll committed delivery, but no action or journal has
                        # started. Release it as interrupted before retrying.
                        await send('/api/worker/recover', {})
                        recovery_needed = False
                        raise
                    report = RunReport(Path(os.environ.get('CTRLLOOP_REPORT_DIR', 'reports')), run_id=identity)
                    pending = {'id': identity, 'company_id': run['company_id'], 'worker_id': run['worker_id'],
                               'initiated_by': run['initiated_by'], 'report_directory': str(report.requests_path), 'events': []}
                    write_journal(pending)
                    recovery_needed = False
                    report.append('delivery_received', run=run, revision=revision)

                    async def emit(details):
                        details = dict(details)
                        stage = details.pop('stage', 'worker_progress')
                        message = details.pop('message', stage.replace('_', ' ').capitalize())
                        body = {'id': str(uuid4()), 'stage': stage, 'message': message, 'data': details}
                        pending['events'].append(body)
                        write_journal(pending)
                        delivery_record(pending, 'event_queued', body=body)
                        await flush_events(pending)

                    try:
                        await emit({'stage': 'delivery_acknowledged', 'message': 'VM received and journaled the persisted instruction',
                                    'instruction': run['instruction'], 'revision': revision})
                        result = await execute_instruction(run['instruction'], emit, run_id=identity, report=report)
                        status = 'succeeded' if result.get('verified') is True else result.get('status', 'error')
                        if status not in {'succeeded', 'blocked', 'unverified', 'interrupted', 'error'}:
                            status = 'unverified'
                        finish = {'status': status, 'result': result,
                                  'error': None if status == 'succeeded' else result.get('error', 'Task completion was not verified.')}
                    except Exception as error:
                        finish = {'status': 'error', 'result': {'report_directory': str(report.requests_path)},
                                  'error': f'Worker execution stopped ({type(error).__name__}); GUI actions were not retried.'}
                        if not report._closed:
                            report.append('run_error', **finish)
                            await asyncio.to_thread(report.close)
                    pending['finish'] = finish
                    write_journal(pending)
                    await settle_journal()
                except WorkerAssignmentError:
                    # Fail closed without touching a foreign journal/report or
                    # sending native input. Operator repair must be explicit.
                    raise
                except Exception as error:
                    print(json.dumps({'stage': 'worker_connection_error', 'type': type(error).__name__}), flush=True)
                    await asyncio.sleep(5)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task


if __name__ == '__main__':
    asyncio.run(main())
