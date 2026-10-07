import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.catalog import execplan_archive
from loop_manager_internal.storage import execplan_archive_state
from loop_manager_internal.catalog import execplan_catalog
import loop_manager


def candidate(plan_id: str) -> execplan_archive_state.MergedArchiveCandidate:
    title = f"Plan {plan_id}"
    return execplan_archive_state.MergedArchiveCandidate(
        plan_id,
        f"docs/plans/[{plan_id}] {title}.md",
        f"docs/plans/archive/[{plan_id}] {title}.md",
        f"hash-{plan_id}",
        "base",
    )


def entry(
    plan_id: str,
    *,
    mode: str = "100644",
    content_hash: str | None = None,
) -> execplan_archive.ArchiveTreeEntry:
    return execplan_archive.ArchiveTreeEntry(
        mode,
        "blob",
        f"blob-{plan_id}",
        content_hash or f"hash-{plan_id}",
    )


class FakeRepository:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)
        self.pending = {}
        self.operation_pending = []
        self.archived = set()
        self.prepared = {}
        self.success_results = {}

    def merged_candidates(self, _repo):
        return self.candidates

    def candidates_current(self, _repo, candidates):
        return all(item in self.candidates for item in candidates)

    def record_pending(self, _repo, item, *, reason, **_kwargs):
        self.pending[item.plan_id] = reason

    def record_operation_started(self, *_args):
        return None

    def prepared_operation(self, _repo, operation_id):
        return self.prepared.get(operation_id)

    def record_prepared(
        self,
        _repo,
        operation_id,
        base_sha,
        final_hash,
        commit_oid,
        _candidates,
    ):
        self.prepared[operation_id] = (
            execplan_archive_state.PreparedArchiveOperation(
                operation_id, base_sha, final_hash, commit_oid
            )
        )

    def record_operation_pending(
        self, _repo, operation_id, candidates, *, reason, **kwargs
    ):
        self.operation_pending.append((operation_id, reason, kwargs))
        for item in candidates:
            self.pending[item.plan_id] = reason

    def record_success(
        self,
        _repo,
        candidates,
        *,
        push_result,
        **_kwargs,
    ):
        for item in candidates:
            self.archived.add(item.plan_id)
            self.pending.pop(item.plan_id, None)
            self.success_results[item.plan_id] = push_result


class FakeGit:
    def __init__(
        self,
        observations,
        *,
        bases=None,
        pushes=None,
        commit_match_results=None,
    ):
        self.observations = observations
        self.bases = list(bases or ["base"])
        self.pushes = list(
            pushes or [execplan_archive.ArchivePushResult(True, "commit-base")]
        )
        self.fetch_count = 0
        self.prepared = []
        self.prepared_moves = []
        self.pushed = []
        self.match_count = 0
        self.commit_match_results = list(commit_match_results or [])
        self.cleaned = []

    def fetch_remote_base(self, _base_branch):
        index = min(self.fetch_count, len(self.bases) - 1)
        self.fetch_count += 1
        return self.bases[index]

    def observe(self, revision, candidates):
        states = self.observations[revision]
        return tuple(
            execplan_archive.ArchiveCandidateObservation(
                item,
                states[item.plan_id][0],
                states[item.plan_id][1],
            )
            for item in candidates
        )

    def prepare_commit(self, base_sha, _base_branch, operation_id, moves):
        self.prepared.append((base_sha, tuple(move.plan_id for move in moves)))
        self.prepared_moves.append(tuple(moves))
        return execplan_archive.PreparedArchive(
            operation_id, base_sha, f"tree-{base_sha}", f"commit-{base_sha}"
        )

    def commit_matches(self, _prepared, _moves):
        self.match_count += 1
        if self.commit_match_results:
            result = self.commit_match_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return True

    def cleanup_prepared(self, prepared):
        self.cleaned.append(prepared.commit_oid)

    def push_fast_forward(self, prepared, _base_branch):
        self.pushed.append(prepared.commit_oid)
        return self.pushes.pop(0)


class DefinitionValidatingFakeGit(FakeGit):
    def __init__(self, observations, *, definitions_result=True, definitions_error=None, **kwargs):
        super().__init__(observations, **kwargs)
        self.definitions_result = definitions_result
        self.definitions_error = definitions_error
        self.definition_checks = []

    def definitions_current(self, base_sha, candidates):
        self.definition_checks.append((base_sha, tuple(item.plan_id for item in candidates)))
        if self.definitions_error is not None:
            raise self.definitions_error
        return self.definitions_result


class ExecPlanArchiveTest(unittest.TestCase):
    def test_stored_commit_mismatch_is_reprepared_before_push(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        operation_id = execplan_archive.archive_operation_id(
            "base",
            (execplan_archive.ArchiveMove(
                item.plan_id, item.active_path, item.archive_path,
                item.content_hash, item.revision_sha,
            ),),
        )
        repository.prepared[operation_id] = (
            execplan_archive_state.PreparedArchiveOperation(
                operation_id, "base", "stale-tree", "stale-commit"
            )
        )
        git = FakeGit(
            {"base": {"G-001": (entry("G-001"), None)}},
            commit_match_results=[False, True],
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.match_count, 2)
        self.assertEqual(git.prepared, [("base", ("G-001",))])
        self.assertEqual(git.pushed, ["commit-base"])

    def test_stored_commit_validation_exception_is_reprepared_before_push(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        operation_id = execplan_archive.archive_operation_id(
            "base",
            (execplan_archive.ArchiveMove(
                item.plan_id, item.active_path, item.archive_path,
                item.content_hash, item.revision_sha,
            ),),
        )
        repository.prepared[operation_id] = (
            execplan_archive_state.PreparedArchiveOperation(
                operation_id, "base", "stale-tree", "stale-commit"
            )
        )
        git = FakeGit(
            {"base": {"G-001": (entry("G-001"), None)}},
            commit_match_results=[RuntimeError("stored commit is invalid"), True],
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.match_count, 2)
        self.assertEqual(git.prepared, [("base", ("G-001",))])
        self.assertEqual(git.pushed, ["commit-base"])

    def test_new_commit_rename_only_validation_failure_is_pending_without_push(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = FakeGit(
            {"base": {"G-001": (entry("G-001"), None)}},
            commit_match_results=[False],
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

        reason = "commit_preparation_failed:prepared archive commit failed rename-only validation"
        self.assertEqual(outcome.kind, execplan_archive.ArchiveDispatchKind.TRANSIENT)
        self.assertEqual(dict(outcome.pending), {"G-001": reason})
        self.assertEqual(repository.pending["G-001"], reason)
        self.assertEqual(repository.operation_pending[0][1], reason)
        self.assertEqual(git.match_count, 1)
        self.assertEqual(git.pushed, [])

    def test_optional_definition_validation_is_skipped_for_legacy_port(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = FakeGit({"base": {"G-001": (entry("G-001"), None)}})

        outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))

    def test_optional_definition_validation_accepts_and_rejects_explicit_capability(self):
        item = candidate("G-001")
        states = {"base": {"G-001": (entry("G-001"), None)}}
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                repository = FakeRepository((item,))
                git = DefinitionValidatingFakeGit(states, definitions_result=accepted)

                outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

                self.assertEqual(bool(outcome.archived_plan_ids), accepted)
                self.assertEqual(len(git.definition_checks), 2 if accepted else 3)
                self.assertEqual(bool(git.pushed), accepted)

    def test_definition_validation_exception_uses_pending_recovery_path(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = DefinitionValidatingFakeGit(
            {"base": {"G-001": (entry("G-001"), None)}},
            definitions_error=RuntimeError("definition read failed"),
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(repository, git).dispatch("/repo", "main")

        self.assertEqual(outcome.kind, execplan_archive.ArchiveDispatchKind.TRANSIENT)
        self.assertEqual(dict(outcome.pending), {"G-001": "commit_preparation_failed:definition read failed"})
        self.assertEqual(git.prepared, [])
        self.assertEqual(git.pushed, [])

    def test_push_uses_all_resolved_urls_and_preserves_remote_result_checks(self):
        prepared = execplan_archive.PreparedArchive('archive', 'base', 'tree', 'commit')
        urls = ('https://example.test/repo.git', 'ssh://example.test/repo.git')
        for remote, succeeded, changed in (('commit', True, False),
                                            ('base', False, False),
                                            ('raced', False, True),
                                            (RuntimeError('network unavailable'), False, False)):
            with self.subTest(remote=remote):
                port = loop_manager._ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), '/repo')
                calls = []
                def git(repo, *args, **kwargs):
                    calls.append(args)
                    if args == ('remote', 'get-url', '--push', '--all', 'origin'):
                        return SimpleNamespace(stdout='\n'.join(urls), returncode=0)
                    self.assertEqual(args, ('push', '--', urls[len(calls) - 2], 'commit:refs/heads/main'))
                    return SimpleNamespace(stdout='push rejected', returncode=1)
                with (mock.patch.object(loop_manager, 'git', side_effect=git),
                      mock.patch.object(port, 'fetch_remote_base',
                          side_effect=remote if isinstance(remote, Exception) else None,
                          return_value=remote) as fetch):
                    result = port.push_fast_forward(prepared, 'main')
                self.assertEqual(result.success, succeeded)
                self.assertEqual(result.base_changed, changed)
                fetch.assert_called_once_with('main')
                self.assertEqual(len(calls), 3)

    def test_missing_push_url_refuses_archive_before_any_push(self):
        port = loop_manager._ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), '/repo')
        with (mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(stdout='', returncode=0)) as git,
              mock.patch.object(port, 'fetch_remote_base') as fetch):
            with self.assertRaisesRegex(RuntimeError, 'origin URL'):
                port.push_fast_forward(execplan_archive.PreparedArchive('archive', 'base', 'tree', 'commit'), 'main')
        git.assert_called_once_with(Path('/repo'), 'remote', 'get-url', '--push', '--all', 'origin')
        fetch.assert_not_called()

    def test_empty_candidate_set_is_checked_after_remote_refresh(self):
        repository = FakeRepository(())
        git = FakeGit({})

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git
        ).dispatch("/repo", "main")

        self.assertEqual(
            outcome.kind, execplan_archive.ArchiveDispatchKind.NO_CANDIDATES
        )
        self.assertEqual(git.fetch_count, 1)

    def test_remote_refresh_finds_candidate_after_empty_cache(self):
        item = candidate("G-001")
        repository = FakeRepository(())
        git = FakeGit({"base": {"G-001": (entry("G-001"), None)}})

        def refresh(_base):
            repository.candidates = (item,)
            return True

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git, refresh_candidates=refresh,
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.prepared, [("base", ("G-001",))])

    def test_other_archive_path_conflict_does_not_hold_safe_candidate(self):
        conflicted = candidate("G-001")
        safe = candidate("G-002")
        repository = FakeRepository((conflicted, safe))

        class ConflictGit(FakeGit):
            def observe(self, revision, candidates):
                observations = super().observe(revision, candidates)
                return tuple(
                    execplan_archive.ArchiveCandidateObservation(
                        item.candidate, item.source, item.destination,
                        "docs/plans/archive/[G-001] Old.md"
                        if item.candidate.plan_id == "G-001" else None,
                    )
                    for item in observations
                )

        git = ConflictGit({"base": {
            "G-001": (entry("G-001"), None),
            "G-002": (entry("G-002"), None),
        }})
        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git,
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-002",))
        self.assertEqual(dict(outcome.pending), {"G-001": "archive_id_conflict"})
        self.assertEqual(git.prepared, [("base", ("G-002",))])

    def test_git_observation_identifies_other_archive_path_for_same_id(self):
        item = candidate("G-001")
        other = "docs/plans/archive/[G-001] Old.md"
        port = loop_manager._ExecPlanArchiveGitPort(
            Path("/repo"), Path("/state"), "/repo"
        )
        with mock.patch.object(
            port, "_manifest", return_value={item.active_path: (), other: ()}
        ), mock.patch.object(port, "_entry", side_effect=(entry("G-001"), None)):
            observation = port.observe("base", (item,))[0]

        self.assertEqual(observation.other_archive_path, other)
        self.assertEqual(
            execplan_archive.validate_candidate(observation)[0],
            "archive_id_conflict",
        )

    def test_candidate_validation_rejects_unsafe_states_without_overwrite(self):
        item = candidate("G-001")
        cases = (
            ((None, None), "source_missing"),
            ((entry("G-001", mode="120000"), None), "source_not_regular"),
            ((entry("G-001", content_hash="changed"), None), "source_hash_mismatch"),
            ((entry("G-001"), entry("G-001")), "source_and_destination_exist"),
            ((None, entry("G-001", content_hash="changed")), "archive_hash_mismatch"),
            ((None, entry("G-001", mode="120000")), "archive_not_regular"),
        )
        for (source, destination), expected in cases:
            with self.subTest(expected=expected):
                status, move = execplan_archive.validate_candidate(
                    execplan_archive.ArchiveCandidateObservation(
                        item, source, destination
                    )
                )
                self.assertEqual(status, expected)
                self.assertIsNone(move)

        safe, move = execplan_archive.validate_candidate(
            execplan_archive.ArchiveCandidateObservation(
                item, entry("G-001"), None
            )
        )
        self.assertEqual(safe, "safe")
        self.assertEqual(move.source, item.active_path)

    def test_candidate_requires_reconciled_revision(self):
        item = candidate("G-001")
        missing = execplan_archive_state.MergedArchiveCandidate(
            item.plan_id, item.active_path, item.archive_path,
            item.content_hash, "",
        )
        status, move = execplan_archive.validate_candidate(
            execplan_archive.ArchiveCandidateObservation(
                missing, entry("G-001"), None,
            )
        )
        self.assertEqual(status, "current_revision_missing")
        self.assertIsNone(move)

    def test_safe_subset_is_published_while_bad_candidate_remains_pending(self):
        safe = candidate("G-001")
        changed = candidate("G-002")
        repository = FakeRepository((safe, changed))
        git = FakeGit(
            {
                "base": {
                    "G-001": (entry("G-001"), None),
                    "G-002": (entry("G-002", content_hash="changed"), None),
                }
            }
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.kind, execplan_archive.ArchiveDispatchKind.PENDING)
        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(
            dict(outcome.pending), {"G-002": "source_hash_mismatch"}
        )
        self.assertEqual(git.prepared, [("base", ("G-001",))])
        self.assertEqual(git.pushed, ["commit-base"])

    def test_base_change_discards_old_attempt_and_recalculates_latest_tree(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = FakeGit(
            {
                "base-1": {"G-001": (entry("G-001"), None)},
                "base-2": {"G-001": (entry("G-001"), None)},
            },
            bases=["base-1", "base-2", "base-2", "base-2"],
            pushes=[execplan_archive.ArchivePushResult(True, "commit-base-2")],
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.prepared, [("base-2", ("G-001",))])
        self.assertEqual(git.pushed, ["commit-base-2"])

    def test_commit_boundary_base_change_restarts_without_push(self):
        class CommitRaceGit(FakeGit):
            def __init__(self, observations):
                super().__init__(observations)
                self.prepare_attempts = 0

            def prepare_commit(self, *args):
                self.prepare_attempts += 1
                if self.prepare_attempts == 1:
                    raise execplan_archive.ArchiveRemoteBaseChanged("changed")
                return super().prepare_commit(*args)

        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = CommitRaceGit(
            {"base": {"G-001": (entry("G-001"), None)}}
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.prepare_attempts, 2)
        self.assertEqual(git.pushed, ["commit-base"])

    def test_prepared_commit_is_reused_after_push_failure(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = FakeGit(
            {"base": {"G-001": (entry("G-001"), None)}},
            pushes=[
                execplan_archive.ArchivePushResult(False, error="denied"),
                execplan_archive.ArchivePushResult(True, "commit-base"),
            ],
        )
        dispatcher = execplan_archive.ExecPlanArchiveDispatcher(repository, git)

        failed = dispatcher.dispatch("/repo", "main")
        succeeded = dispatcher.dispatch("/repo", "main")

        self.assertEqual(failed.kind, execplan_archive.ArchiveDispatchKind.TRANSIENT)
        self.assertEqual(succeeded.archived_plan_ids, ("G-001",))
        self.assertEqual(git.prepared, [("base", ("G-001",))])
        self.assertEqual(git.pushed, ["commit-base", "commit-base"])

    def test_remote_archive_after_interruption_converges_without_new_commit(self):
        item = candidate("G-001")
        repository = FakeRepository((item,))
        git = FakeGit(
            {"base": {"G-001": (None, entry("G-001"))}}
        )

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git
        ).dispatch("/repo", "main")

        self.assertEqual(outcome.kind, execplan_archive.ArchiveDispatchKind.CONVERGED)
        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(repository.success_results["G-001"], "observed_remote")
        self.assertEqual(git.prepared, [])
        self.assertEqual(git.pushed, [])

    def test_catalog_classifier_accepts_only_content_preserving_archive_moves(self):
        before = {
            "docs/plans/[G-001] Plan.md": ("100644", "blob", "one"),
            "code.py": ("100644", "blob", "code"),
        }
        after = {
            "docs/plans/archive/[G-001] Plan.md": ("100644", "blob", "one"),
            "code.py": ("100644", "blob", "code"),
        }

        classification = execplan_catalog.classify_archive_only_tree_change(
            before, after
        )

        self.assertTrue(classification.execplan_only)
        changed = dict(after)
        changed["code.py"] = ("100644", "blob", "changed")
        self.assertFalse(
            execplan_catalog.classify_archive_only_tree_change(
                before, changed
            ).archive_only
        )

    def test_operation_identity_changes_with_final_revision(self):
        first = execplan_archive.ArchiveMove(
            "G-001", "docs/plans/[G-001] Plan.md",
            "docs/plans/archive/[G-001] Plan.md", "final-hash", "revision-1",
        )
        second = execplan_archive.ArchiveMove(
            first.plan_id, first.source, first.destination,
            first.content_hash, "revision-2",
        )
        self.assertNotEqual(
            execplan_archive.archive_operation_id("base", (first,)),
            execplan_archive.archive_operation_id("base", (second,)),
        )

    def test_revision_race_recalculates_candidate_before_commit(self):
        first = candidate("G-001")
        second = execplan_archive_state.MergedArchiveCandidate(
            first.plan_id, "docs/plans/[G-001] Revised.md",
            "docs/plans/archive/[G-001] Revised.md",
            "new-final-hash", "base-2",
        )
        repository = FakeRepository((first,))
        git = FakeGit({
            "base": {"G-001": (entry("G-001"), None)},
            "base-2": {"G-001": (
                entry("G-001", content_hash="new-final-hash"), None)},
        }, bases=["base", "base-2", "base-2", "base-2"])

        def refresh(base):
            if base == "base-2":
                repository.candidates = (second,)
            return True

        outcome = execplan_archive.ExecPlanArchiveDispatcher(
            repository, git, refresh_candidates=refresh,
        ).dispatch("/repo", "main")
        self.assertEqual(outcome.archived_plan_ids, ("G-001",))
        self.assertEqual(git.prepared, [("base-2", ("G-001",))])
        self.assertEqual(git.prepared_moves[0][0].source, second.active_path)
        self.assertEqual(git.pushed, ["commit-base-2"])


if __name__ == "__main__":
    unittest.main()
