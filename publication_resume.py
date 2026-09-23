"""One observation boundary for owned publication and PR creation resume."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping


class PublicationNext(str, Enum):
    ATTACH_PR = "attach_pr"
    PUSH = "push"
    CREATE_PR = "create_pr"
    USER_ACTION = "user_action"


@dataclass(frozen=True)
class PublicationObservation:
    branch: str
    local_oid: str
    remote_oid: str | None
    recorded_remote_oid: str | None
    pull_request: Mapping[str, object] | None


def assess_publication(observation: PublicationObservation) -> PublicationNext:
    pr = observation.pull_request
    if pr is not None:
        head_name = pr.get("headRefName")
        head_oid = pr.get("headRefOid")
        if head_name and head_name != observation.branch:
            return PublicationNext.USER_ACTION
        if head_oid and observation.remote_oid and head_oid != observation.remote_oid:
            return PublicationNext.USER_ACTION
        return PublicationNext.ATTACH_PR
    if observation.remote_oid == observation.local_oid:
        return PublicationNext.CREATE_PR
    if observation.remote_oid not in {None, observation.recorded_remote_oid}:
        return PublicationNext.USER_ACTION
    return PublicationNext.PUSH


class PublicationResume:
    """Requery both Git and GitHub at each external publication boundary."""

    def __init__(self, lookup_pr: Callable[..., Mapping[str, object] | None],
                 remote_oid: Callable[..., str | None], git: Callable[..., object]):
        self.lookup_pr = lookup_pr
        self.remote_oid = remote_oid
        self.git = git

    def observe(self, repo, worktree, branch, recorded_remote_oid,
                pr_url=None) -> PublicationObservation:
        local_oid = self.git(worktree, "rev-parse", "HEAD").stdout.strip()
        return PublicationObservation(
            branch=branch, local_oid=local_oid,
            remote_oid=self.remote_oid(repo, branch),
            recorded_remote_oid=recorded_remote_oid,
            pull_request=self.lookup_pr(repo, branch, pr_url),
        )
