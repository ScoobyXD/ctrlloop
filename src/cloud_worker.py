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
from .actions import action_instruction
from .driver import CuaDriver
from .desktop_stream import InputRecoveryPersistenceError, desktop_stream
from .selection import PINNED_MODEL


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
        action_lock = asyncio.Lock()
        try:
            await settle_journal()
            await send('/api/worker/recover', {})
            async with desktop_stream(endpoint, os.environ['WORKER_TOKEN'], assignment, action_lock) as gate:
                while True:
                    action_acquired = False
                    try:
                        await action_lock.acquire()
                        action_acquired = True
                        gate.ensure_input_ready()
                        await settle_journal()
                        if recovery_needed:
                            await send('/api/worker/recover', {})
                            recovery_needed = False
                        try:
                            payload = await send('/api/worker/poll', {'revision': revision, 'capabilities': ['manual_actions_v1']})
                        except Exception:
                            # A lost poll response may have committed delivery in the DB.
                            recovery_needed = True
                            await send('/api/worker/recover', {})
                            recovery_needed = False
                            raise
                        run = payload.get('run')
                        if not run:
                            action_lock.release()
                            action_acquired = False
                            await asyncio.sleep(2)
                            continue
                        identity = validate_run_context(run)
                        recovery_needed = True
                        gate.begin_execution()
                        # Release any held human keys/buttons before agent actions.
                        # The same lock has excluded input since before run dispatch.
                        await gate.release_inputs()
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

                        async def execute_steps():
                            """One server grant and one durable completion per authored step."""
                            expected_step = int(run.get('next_step', 0))
                            results = []
                            final = {'status': 'interrupted', 'steps': results, 'run_id': identity,
                                     'start_step': run.get('start_step', 0),
                                     'end_step': run.get('end_step') if run.get('end_step') is not None else len(run['steps']),
                                     'completion': {'verified': False, 'source': 'authored_sequence'}}
                            driver = None
                            prior_control = None
                            report.append('run_started', run_id=identity, request=run['instruction'],
                                          backend='cua_driver', worker='local_omarchy', jev_model=PINNED_MODEL,
                                          mode='manual_actions', steps=run['steps'],
                                          start_step=final['start_step'], end_step=final['end_step'])

                            async def emit_trace(step_id, step_index):
                                records = report.snapshot_events(step_id=step_id)
                                chunk, size, chunk_index = [], 0, 0
                                for record in records:
                                    encoded_size = len(json.dumps(record, ensure_ascii=False).encode('utf-8'))
                                    if encoded_size > 900_000:
                                        record = {key: record[key] for key in ('timestamp', 'elapsed_ms', 'step_id', 'step_index', 'iteration') if key in record} | {
                                            'event': 'capture_gap', 'original_event': record.get('event'),
                                            'reason': 'This record exceeds the live inspector limit; its full contents remain in the final evidence archive.',
                                            'original_bytes': encoded_size,
                                        }
                                        encoded_size = len(json.dumps(record).encode('utf-8'))
                                    if chunk and size + encoded_size > 900_000:
                                        await emit({'stage': 'step_trace', 'message': 'Captured step evidence',
                                                    'step_id': step_id, 'step_index': step_index, 'chunk_index': chunk_index,
                                                    'events': chunk})
                                        chunk, size, chunk_index = [], 0, chunk_index + 1
                                    chunk.append(record)
                                    size += encoded_size
                                if chunk:
                                    await emit({'stage': 'step_trace', 'message': 'Captured step evidence',
                                                'step_id': step_id, 'step_index': step_index, 'chunk_index': chunk_index,
                                                'events': chunk})
                            try:
                                driver = CuaDriver(local_endpoint=os.environ.get('CUA_LOCAL_MCP_URL', 'http://127.0.0.1:3000/mcp'),
                                                   local_token=os.environ.get('CUA_LOCAL_MCP_TOKEN'), report=report)
                                with report.span('driver_initialization'):
                                    await driver.start()
                                report.append('driver_connected', **driver.session_info)
                                while True:
                                    # A lost response ends this execution. Retrying a grant
                                    # could replay an action that was already authorized.
                                    boundary = await send('/api/worker/poll', {
                                        'revision': revision, 'run_id': identity, 'expected_step': expected_step,
                                    })
                                    control = boundary.get('control')
                                    if control == 'pause':
                                        await asyncio.sleep(1)
                                        continue
                                    if control == 'stop':
                                        final.update(error='Stopped at an authored step boundary; completed actions were not replayed.',
                                                     code='sequence_stopped')
                                        break
                                    if control == 'complete':
                                        final.update(status='succeeded')
                                        break
                                    step = boundary.get('step')
                                    step_index = boundary.get('step_index')
                                    if control != 'run' or not isinstance(step, dict) or step_index != expected_step:
                                        raise WorkerAssignmentError('Invalid authored-step delivery; no command was sent')
                                    step_id = step['id']
                                    pending['active_step'] = {'step_id': step_id, 'step_index': step_index,
                                                              'state': 'granted_not_replayable'}
                                    write_journal(pending)
                                    started = perf_counter_ns()
                                    started_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
                                    with report.span('workbench.step', step_id=step_id, step_index=step_index):
                                        report.append('step_started', step_id=step_id, step_index=step_index,
                                                      action=step['action'])
                                        await emit({'stage': 'step_started', 'message': f'Executing step {step_index + 1}',
                                                    'step_id': step_id, 'step_index': step_index, 'action': step['action']})
                                        step_result = await execute_instruction(
                                            action_instruction(step['action']), emit, run_id=identity, report=report,
                                            action=step['action'], step_id=step_id, step_index=step_index, finalize_report=False,
                                            driver=driver, prior_control=prior_control,
                                        )
                                        succeeded = step_result.get('status') == 'succeeded'
                                        prior_control = step_result.get('pipeline', {}).get('last_control', prior_control)
                                        finished_at = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
                                        completed = {'step_id': step_id, 'step_index': step_index,
                                                     'status': 'succeeded' if succeeded else 'error', 'result': step_result,
                                                     'error': step_result.get('error'),
                                                     'started_at': started_at, 'finished_at': finished_at,
                                                     'duration_ms': (perf_counter_ns() - started) / 1_000_000}
                                        results.append(completed)
                                        report.append('step_finished', **completed)
                                        pending['active_step']['state'] = 'completed'
                                        pending['active_step']['result'] = step_result
                                        write_journal(pending)
                                    await emit_trace(step_id, step_index)
                                    await emit({'stage': 'step_finished',
                                                'message': f'Step {step_index + 1} ' + (
                                                    'sent (application effect unverified)' if succeeded and
                                                    step_result.get('pipeline', {}).get('action_result', {}).get('effect') == 'unverifiable'
                                                    else 'completed' if succeeded else 'stopped'),
                                                **completed})
                                    if not succeeded:
                                        final.update(status=step_result.get('status', 'error'),
                                                     error=step_result.get('error', 'The authored action could not be confirmed.'),
                                                     code=step_result.get('code', 'step_failed'))
                                        break
                                    expected_step += 1
                            except BaseException as error:
                                final.update(status='interrupted' if isinstance(error, asyncio.CancelledError) else 'error',
                                             error='The sequence stopped; no authored action was retried.',
                                             code='sequence_interrupted', error_type=type(error).__name__)
                                raise
                            finally:
                                if driver is not None:
                                    try:
                                        with report.span('driver_cleanup'):
                                            await driver.close()
                                    except Exception as error:
                                        final['cleanup_error'] = getattr(error, 'code', type(error).__name__)
                                        report.append('driver_cleanup_error', code=final['cleanup_error'])
                                report.append('run_finished', result=final)
                                final['report_path'] = str(report.path)
                                final['report_directory'] = str(report.requests_path)
                                final['timing'] = await asyncio.to_thread(report.close)
                            return final

                        try:
                            await emit({'stage': 'delivery_acknowledged', 'message': 'VM received and journaled the persisted instruction',
                                        'instruction': run['instruction'], 'revision': revision})
                            result = (await execute_steps() if run.get('steps') else
                                      await execute_instruction(run['instruction'], emit, run_id=identity, report=report))
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
                        if action_acquired:
                            gate.executing = False
                            action_lock.release()
                            action_acquired = False
                        await asyncio.sleep(5)
                    finally:
                        if action_acquired:
                            gate.executing = False
                            action_lock.release()
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except* InputRecoveryPersistenceError:
        # Paired with the deployed service's RestartPreventExitStatus=78.
        raise SystemExit(78)
