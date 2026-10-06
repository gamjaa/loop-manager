"""Expose reserved manual goals to Planner without adopting arbitrary branches."""
import json


def protect(connection, repo, prompt):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_groups'").fetchone():
        return prompt
    groups=connection.execute("SELECT state,members,requirements,documents FROM manual_work_groups "
                              "WHERE repo=? AND state!='MERGED'",(repo,)).fetchall()
    if not groups:
        return prompt
    return (prompt+'\n\nSQLite에 예약된 수동 작업/제출 묶음의 읽기 전용 문맥입니다. '
        '아직 base에 없는 G도 명시적 수동 제출의 같은 ID로 추적 중입니다. '
        '이 ID와 목표를 중복 생성하거나 수동 G를 수정·삭제·archive하지 마세요. '
        '생략이나 PR 생성은 실제 Merge 완료가 아닙니다. 독립 목표만 기존 권한으로 처리하세요.\n'
        +json.dumps([dict(row) for row in groups],ensure_ascii=False))
