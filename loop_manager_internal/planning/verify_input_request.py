"""Missing-list requests are prepared only at the normal planning boundary."""
import json
from pathlib import Path

from loop_manager_internal.catalog import manager_sources as contract
from loop_manager_internal.storage import verify_input_requests
from loop_manager_internal.verification.inputs import MANIFEST, parse


def instruction(connection, repo: str, base: str, active_sha: str, *, git, git_bytes) -> str:
    repository = Path(repo)
    revision = git(repository, 'rev-parse', '--verify', f'origin/{base}^{{commit}}').stdout.strip()
    if revision != active_sha:
        return ''  # Rebase/publication gates will handle the stale planning branch.
    tree = git_bytes(repository, 'ls-tree', '-r', '-z', '--full-tree', revision)
    entries = {}
    for record in tree.split(b'\0'):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b'\t')
        if not separator or len(metadata.split()) != 3:
            raise RuntimeError('invalid remote verify-inputs tree')
        mode, kind, oid = metadata.split()
        entries[raw_path.decode('utf-8', 'surrogateescape')] = (mode, kind, oid)
    item = entries.get(MANIFEST)
    if item is not None and item[0] in (b'100644', b'100755') and item[1] == b'blob':
        try:
            paths = parse(git_bytes(repository, 'cat-file', 'blob', item[2].decode('ascii')))
            if any(not any(c in path for c in '*?') and path not in entries for path in paths):
                raise ValueError('remote manifest has missing literal inputs')
        except (ValueError, UnicodeError, RecursionError):
            pass
        else:
            return ''
    rows = connection.execute('SELECT plan_id,title,sources,status FROM exec_plans WHERE repo=?', (repo,)).fetchall()
    unfinished = {str(row['plan_id']): row for row in rows if row['status'] not in ('MERGED', 'CANCELLED')}
    for row in unfinished.values():
        if (contract.SOURCE in json.loads(row['sources']) or 'verify-inputs' in row['title']
                or 'verify 입력 목록' in row['title'] or 'verify input' in row['title'].lower()):
            return ''
    # Remote active plans include drafts and not-yet-registered plans. Existing
    # product-backed work mentioning the manifest also satisfies deduplication.
    for path, (mode, kind, oid) in entries.items():
        if (path.startswith('docs/plans/') and path.count('/') == 2 and path.endswith('.md')
                and mode in (b'100644', b'100755') and kind == b'blob'):
            raw = git_bytes(repository, 'cat-file', 'blob', oid.decode('ascii'))
            from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text
            try:
                text = raw.decode('utf-8')
                plan = parse_execplan_text(path, text, validate_sources=False)
            except (UnicodeError, RuntimeError):
                # The authoritative catalog owns declaration-error isolation.
                # An unreadable declaration cannot disprove related pending work;
                # defer only this optional request, preserving normal planning.
                return ''
            row = next((r for r in rows if r['plan_id'] == plan['id']), None)
            if row is not None and row['status'] in ('MERGED', 'CANCELLED'):
                continue
            if contract.SOURCE in plan['sources'] or MANIFEST in text:
                return ''
    verify_input_requests.ensure_schema(connection)
    saved = connection.execute('SELECT request_id,revision,purpose,version FROM verify_input_requests '
                               'WHERE repo=? AND purpose=? AND version=? AND rule=? ORDER BY rowid LIMIT 1',
                               (repo, contract.PURPOSE, contract.VERSION, contract.source_content(contract.SOURCE))).fetchone()
    request = (dict(zip(('id', 'revision', 'purpose', 'version'), saved)) if saved is not None
               else verify_input_requests.register(connection, repo, revision))
    return ('\n\nManager가 최신 원격 base의 verify 입력 목록 부재/오류를 확인했습니다. '
            '기존 미완료 계획·draft·PR과 중복하지 않는 생성 계획 하나를 요청합니다. '
            '전체 구조와 실제 verify 스크립트를 확인하고 목록을 유지할 계획만 작성하세요. '
            'sources는 [manager:verify-inputs]만 사용하고 Purpose는 다음 본문 그대로 유지하세요:\n'
            + contract.RULE + '\nDecision Log에 다음 요청을 그대로 기록하세요:\n'
            + contract.PREFIX + json.dumps(request, sort_keys=True))
