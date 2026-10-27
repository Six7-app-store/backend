"""Resolve release tags to immutable commits for review and dispatch."""
import re
from uuid import uuid4

import git
from fastapi import HTTPException

from app.services.git_service import git_service


def resolve_tag(git_url: str, tag: str) -> str:
    """Resolve a tag by fetching it; refuse unresolved or malformed commits."""
    repo_path = None
    try:
        repo_path = git_service.clone_release_vars(git_url, tag, str(uuid4()))
        sha = git.Repo(repo_path).head.commit.hexsha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid Git commit")
        return sha
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"reason": "release_commit_unavailable"}) from exc
    finally:
        if repo_path:
            git_service.cleanup_repository(repo_path)
