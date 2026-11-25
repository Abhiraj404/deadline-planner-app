"""Single-owner sync jobs and a cooperative automatic scheduler."""

import json
import logging
import os
import threading
from pathlib import Path
from time import monotonic

from configuration import SCHEDULER_POLL_SECONDS
from database import open_database, update_document_registry
from planner_data import SyncSettings, SyncStopped
from sync_runtime import Job, ProcessGate, now

logger = logging.getLogger('deadline_planner.sync')


class JobConflict(RuntimeError):
    """Another job or a stale preview prevents this request."""

    pass


class InvalidSelection(ValueError):
    """The selected documents are not part of the current preview."""

    pass


class Synchronisation:
    """Coordinate manual runs, previews and maintenance with the same operation lock."""

    def __init__(self, root, settings, collect, save, enrich):
        """Store the callbacks used to read settings, collect, save and enrich."""
        self.root = root
        self.settings = settings
        self.collect = collect
        self.save = save
        self.enrich = enrich
        self.lock = threading.Lock()
        self.gate = ProcessGate()
        self.status = {}
        self.active_job: Job | None = None
        self.last_preview: Job | None = None
        self.worker: threading.Thread | None = None

    def acquire(self) -> bool:
        """Take the in-process lock and the cross-process gate, or neither."""
        if not self.lock.acquire(False):
            return False
        try:
            if self.gate.acquire(self.root()):
                return True
        except OSError:
            self.lock.release()
            raise
        self.lock.release()
        return False

    def release(self) -> None:
        """Release both locks."""
        self.gate.release()
        self.lock.release()

    def begin(
        self,
        mode='sync',
        scope: SyncSettings | None = None,
        candidates=(),
        selection=None,
        *,
        automatic=False,
    ) -> Job | None:
        """Start a job in a background thread, or return None if one is running."""
        if not self.acquire():
            return None
        try:
            if automatic:
                # The caller's settings may be older than a reset that just released the gate.
                scope = self.settings()
                if not scope.automatic:
                    self.release()
                    return None
            if selection is not None:
                scope, candidates = self.selected_candidates(selection)
            job = Job(mode, scope or self.settings(), candidates)
            if mode == 'preview':
                self.last_preview = None
            self.active_job = job
            self.worker = threading.Thread(
                target=self.run, args=(job,), name='planner-sync', daemon=True
            )
            self.worker.start()
            return job
        except BaseException:
            self.active_job = None
            self.release()
            raise

    def selected_candidates(self, selection):
        """Resolve a download selection against the latest preview."""
        # Check the preview after locking so a completed reset cannot revive a stale selection.
        preview = self.last_preview
        if not preview or preview.id != selection.preview_id:
            raise JobConflict('Refresh the import preview first')
        keys = set(selection.keys if selection.keys is not None else preview.candidates)
        if not keys or keys - preview.candidates.keys():
            raise InvalidSelection('Select documents from the current preview')
        return preview.scope, [item for key, item in preview.candidates.items() if key in keys]

    def stop(self) -> bool:
        """Ask the active job to stop. Returns False if nothing is running."""
        job = self.active_job
        if job:
            job.stop.set()
        return job is not None

    def schedule(self, done: threading.Event) -> None:
        """Scheduler loop: start an automatic sync whenever the interval has passed."""
        last_run = None
        while not done.is_set():
            try:
                scope = self.settings()
                current = monotonic()
                if scope.automatic and (
                    last_run is None or current - last_run >= scope.minutes * 60
                ):
                    if self.begin(automatic=True):
                        last_run = current
            except Exception as error:
                logger.error('Scheduler could not start a job (%s)', type(error).__name__)
            done.wait(SCHEDULER_POLL_SECONDS)

    def run(self, job: Job) -> None:
        """Worker thread body: run the job, record history and release the locks."""
        outcome = 'Complete'
        logger.info('Started %s job %s', job.mode, job.id)
        try:
            if job.mode == 'download':
                self.download(job)
            else:
                self.import_sources(job)
            if job.statistics.get('warnings') or job.statistics.get('status') in (
                'Partial',
                'Failed',
            ):
                outcome = 'Partial'
            if job.mode == 'preview':
                self.last_preview = job
        except SyncStopped:
            outcome = 'Stopped'
        except Exception as error:
            # A provider exception may carry a private URL. Keep the diagnostic to its type.
            outcome = 'Failed'
            job.errors.append(type(error).__name__)
            logger.error('Sync job failed (%s)', type(error).__name__)
        finally:
            try:
                self.record_result(job, outcome)
            except Exception as error:
                logger.error('Could not record sync history (%s)', type(error).__name__)
            finally:
                self.active_job = None
                self.release()
        logger.info('Finished %s job %s with outcome %s', job.mode, job.id, outcome)

    def download(self, job: Job) -> None:
        """Download only the documents chosen from a preview."""
        root: Path = self.root()
        base = os.getenv('MOODLE_URL')
        if not base or not (root / 'moodle-session.json').is_file():
            raise RuntimeError('Moodle session unavailable')
        self.enrich(
            [], base, job.statistics, root=root, scope=job.scope, job=job, candidates=job.selected
        )

    def import_sources(self, job: Job) -> None:
        """Import each selected source; one failing source does not stop the rest."""
        for source in dict.fromkeys(job.scope.sources):
            job.check()
            try:
                self.import_source(job, source)
            except SyncStopped:
                raise
            except Exception as error:
                job.errors.append(f'{source}: {type(error).__name__}')
                job.statistics['warnings'] = job.statistics.get('warnings', 0) + 1
                self.status[source] = 'Import failed. See Synchronisation History.'
                logger.warning('%s import failed (%s)', source, type(error).__name__)

    def import_source(self, job: Job, source: str) -> None:
        """Collect, filter and save one source, or count matches for a preview."""
        catalog = {}
        rows = self.collect(source, job.statistics, catalog, job.scope, job, root=self.root())
        job.check()
        if rows is None:
            self.status[source] = 'Not configured'
            return
        filtered = [
            row
            for row in rows
            if job.scope.includes(row['due'])
            and (source != 'Moodle' or job.scope.includes_module(row.get('module_id')))
        ]
        job.rows[source] = len(filtered)
        if job.mode == 'preview':
            job.matches.extend(
                dict(
                    source=source,
                    title=row['title'],
                    due=row['due'],
                    module=row.get('category', ''),
                )
                for row in filtered
            )
        if source == 'Moodle':
            job.modules = catalog.get('modules', [])
        if job.mode == 'sync':
            catalog['assignments'] = [
                row for row in catalog.get('assignments', []) if job.scope.includes(row['due'])
            ]
            updated, duplicates = self.save(source, filtered, catalog)
            job.statistics['records_updated'] = job.statistics.get('records_updated', 0) + updated
            job.statistics['duplicates_prevented'] = (
                job.statistics.get('duplicates_prevented', 0) + duplicates
            )
        self.status[source] = f'{len(filtered)} matching records'

    def record_result(self, job: Job, outcome: str) -> None:
        """Refresh the document catalogue and add the job to the history."""
        with open_database(self.root()) as connection:
            if job.mode != 'preview' and (job.mode == 'download' or 'Moodle' in job.scope.sources):
                try:
                    update_document_registry(connection, self.root())
                except (ValueError, OSError):
                    job.errors.append('Document index could not be registered')
                    outcome = 'Partial' if outcome == 'Complete' else outcome
            connection.execute(
                'INSERT INTO sync_history VALUES(?,?,?,?,?,?,?)',
                (
                    job.id,
                    job.started,
                    now(),
                    job.mode,
                    outcome,
                    json.dumps(job.statistics),
                    json.dumps(job.errors),
                ),
            )
