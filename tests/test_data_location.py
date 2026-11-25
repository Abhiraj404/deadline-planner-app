"""The one-time move of personal data out of the source folder."""

import pytest

from data_location import migrate_legacy_data, resolve_data_dir


def legacy_project(root):
    """Create a project folder laid out like an older installation."""
    (root / 'deadlines.db').write_bytes(b'db')
    (root / '.env').write_text('MOODLE_URL=', encoding='utf-8')
    (root / 'assessment_pdfs').mkdir()
    (root / 'assessment_pdfs' / 'index.json').write_text('[]', encoding='utf-8')
    (root / 'app.py').write_text('# source stays', encoding='utf-8')


def test_unmigrated_layout_keeps_using_the_project_folder(tmp_path):
    legacy_project(tmp_path)
    assert resolve_data_dir(tmp_path, {}) == tmp_path
    assert resolve_data_dir(tmp_path, {'PLANNER_DATA_DIR': str(tmp_path / 'x')}) == tmp_path / 'x'
    assert resolve_data_dir(tmp_path / 'fresh', {}) == tmp_path / 'fresh' / 'data'


def test_migration_moves_only_runtime_files_and_never_overwrites(tmp_path):
    legacy_project(tmp_path)
    data = tmp_path / 'data'
    (data / 'planner_logs').mkdir(parents=True)
    (data / '.env').write_text('KEEP=1', encoding='utf-8')
    moved = migrate_legacy_data(tmp_path)
    assert set(moved) == {'deadlines.db', '.env', 'assessment_pdfs'}
    assert (data / 'deadlines.db').read_bytes() == b'db'
    assert (data / 'assessment_pdfs' / 'index.json').is_file()
    assert (data / '.env').read_text(encoding='utf-8') == 'KEEP=1'
    assert (tmp_path / '.env').is_file()  # the conflicting file is left in place
    assert (tmp_path / 'app.py').is_file()
    assert resolve_data_dir(tmp_path, {}) == data
    assert migrate_legacy_data(tmp_path) == []


def test_migration_waits_for_a_running_planner(tmp_path):
    import os

    legacy_project(tmp_path)
    lock = tmp_path / '.planner-server.lock'
    with lock.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError):
            migrate_legacy_data(tmp_path)
    assert (tmp_path / 'deadlines.db').is_file()
