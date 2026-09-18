"""Git service for repository management and release information retrieval."""

import ipaddress
import logging
import re
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import git
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import settings

logger = logging.getLogger(__name__)

# Private networks — must match the list in schemas.py. The schema
# validator is the primary gate; this is defense-in-depth so the
# service is safe even if called directly (e.g. from a Celery task).
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(hostname: str) -> bool:
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(hostname))
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except (socket.gaierror, ValueError):
        return False


def _assert_safe_git_url(git_url: str) -> None:
    """Raise ValueError if ``git_url`` resolves to a private/internal address.

    Called before any credential is injected into the URL to prevent
    SSRF: a staff-controlled ``git_link`` must not be able to reach
    internal infrastructure (metadata endpoints, Postgres, RabbitMQ…).
    """
    parsed = urlparse(git_url if "://" in git_url else f"https://{git_url.replace(':', '/', 1).replace('git@', '')}")
    hostname = parsed.hostname or ""
    if _is_private_host(hostname):
        raise ValueError(
            f"git_link hostname {hostname!r} resolves to a private address; "
            "refusing to inject credentials"
        )


class GitService:
    """Service for Git operations and release management."""

    # Sparse-checkout allowlist for the variable scan (non-cone mode,
    # ``.gitignore`` pathspecs). ``packer/`` matches the whole subtree so
    # both the flat layout (``packer/variables.pkr.hcl``) and per-template
    # layout (``packer/<key>/variables.pkr.hcl``) plus the
    # ``template.pkr.hcl`` files are fetched. ``terraform/variables.tf``
    # is a single file since only the variables file is needed.
    SPARSE_CHECKOUT_FILES = [
        'terraform/variables.tf',
        'packer/',
    ]

    def __init__(self) -> None:
        """Initialize Git service with settings."""
        self.base_path = Path(settings.TEMP_REPO_BASE_PATH)
        self.token = settings.GIT_ACCESS_TOKEN
        self._session = self._create_http_session()

    def _create_http_session(self) -> requests.Session:
        """Create requests session with retry strategy."""
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _get_authenticated_url(self, git_url: str) -> str:
        """Convert Git URL to HTTPS format with token authentication."""
        _assert_safe_git_url(git_url)
        url = git_url
        if url.startswith('git@'):
            url = url.replace('git@', '').replace(':', '/', 1)
        elif url.startswith(('https://', 'http://')):
            url = re.sub(r'^https?://', '', url)

        if '@' in url:
            url = url.split('@', 1)[1]

        if self.token:
            return f"https://{self.token}@{url}"
        return f"https://{url}"

    def _parse_git_url(self, git_url: str) -> dict[str, str] | None:
        """Parse Git URL and extract components."""
        patterns = [
            r'^git@([^:]+):([^/]+)/(.+?)(?:\.git)?$',
            r'^https?://([^/]+)/([^/]+)/(.+?)(?:\.git)?$',
        ]

        for pattern in patterns:
            match = re.match(pattern, git_url)
            if match:
                host, owner, repo = match.groups()
                platform = 'github' if 'github' in host.lower() else 'gitlab' if 'gitlab' in host.lower() else 'unknown'
                return {'host': host, 'owner': owner, 'repo': repo, 'platform': platform}

        logger.warning(f"Could not parse git URL: {git_url}")
        return None

    def _request_tags(self, parsed: dict[str, str]) -> requests.Response:
        """Build and perform the tags request for the parsed repository.

        Constructs the provider-specific tags URL and headers (GitHub vs.
        GitLab, including auth token handling) and issues the GET request,
        returning the raw response for the caller to interpret.
        """
        if parsed['platform'] == 'github':
            api_url = f"https://api.github.com/repos/{parsed['owner']}/{parsed['repo']}/tags"
            headers = {
                'Authorization': f"token {self.token}",
                'Accept': 'application/vnd.github.v3+json',
            }
        else:  # gitlab
            project_path = quote(f"{parsed['owner']}/{parsed['repo']}", safe='')
            api_url = f"https://{parsed['host']}/api/v4/projects/{project_path}/repository/tags"
            headers = {'PRIVATE-TOKEN': self.token}

        return self._session.get(api_url, headers=headers, timeout=10)

    def _fetch_github_tags(self, parsed: dict[str, str]) -> list[dict[str, Any]]:
        """Fetch all tags from GitHub API."""
        response = self._request_tags(parsed)

        if response.status_code == 404:
            return []

        response.raise_for_status()
        return [{'version': tag['name'], 'commit': tag['commit']['sha'][:8], 'type': 'tag'} for tag in response.json()]

    def _fetch_github_releases(self, parsed: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Fetch releases from GitHub API."""
        api_url = f"https://api.github.com/repos/{parsed['owner']}/{parsed['repo']}/releases"
        response = self._session.get(
            api_url,
            headers={'Authorization': f"token {self.token}", 'Accept': 'application/vnd.github.v3+json'},
            timeout=10
        )

        if response.status_code == 404:
            return {}

        response.raise_for_status()
        return {
            release['tag_name']: {
                'name': release.get('name') or release['tag_name'],
                'description': release.get('body', ''),
                'author': release.get('author', {}).get('login', ''),
                'published_at': release.get('published_at', ''),
                'prerelease': str(release.get('prerelease', False)),
                'html_url': release.get('html_url', ''),
            }
            for release in response.json() if release.get('tag_name')
        }

    def _fetch_gitlab_tags(self, parsed: dict[str, str]) -> list[dict[str, Any]]:
        """Fetch all tags from GitLab API."""
        response = self._request_tags(parsed)

        if response.status_code == 404:
            return []

        response.raise_for_status()
        return [{'version': tag['name'], 'commit': tag['commit']['id'][:8], 'type': 'tag'} for tag in response.json()]

    def _fetch_gitlab_releases(self, parsed: dict[str, str]) -> dict[str, dict[str, Any]]:
        """Fetch releases from GitLab API."""
        project_path = quote(f"{parsed['owner']}/{parsed['repo']}", safe='')
        api_url = f"https://{parsed['host']}/api/v4/projects/{project_path}/releases"
        response = self._session.get(
            api_url,
            headers={'PRIVATE-TOKEN': self.token},
            timeout=10
        )

        if response.status_code == 404:
            return {}

        response.raise_for_status()
        return {
            release['tag_name']: {
                'name': release.get('name') or release['tag_name'],
                'description': release.get('description', ''),
                'author': release.get('author', {}).get('username', ''),
                'published_at': release.get('released_at', ''),
                'prerelease': 'False',
                'html_url': release.get('_links', {}).get('self', ''),
            }
            for release in response.json() if release.get('tag_name')
        }

    def _accept_github_invite(self, parsed: dict[str, str]) -> dict[str, Any]:
        """Check for and accept pending GitHub repository invitations."""
        try:
            repo_full_name = f"{parsed['owner']}/{parsed['repo']}"

            # Get all pending invitations
            api_url = "https://api.github.com/user/repository_invitations"
            response = self._session.get(
                api_url,
                headers={
                    'Authorization': f'token {self.token}',
                    'Accept': 'application/vnd.github.v3+json'
                },
                timeout=10
            )
            response.raise_for_status()
            invitations = response.json()

            # Find invitation for this repository
            invitation = next(
                (inv for inv in invitations if inv.get('repository', {}).get('full_name') == repo_full_name),
                None
            )

            if not invitation:
                return {
                    'success': False,
                    'message': f"No pending invitation found for repository {repo_full_name}. Please ask the repository owner to invite the app store user."
                }

            # Accept the invitation
            invitation_id = invitation['id']
            accept_url = f"https://api.github.com/user/repository_invitations/{invitation_id}"
            accept_response = self._session.patch(
                accept_url,
                headers={
                    'Authorization': f'token {self.token}',
                    'Accept': 'application/vnd.github.v3+json'
                },
                timeout=10
            )

            if accept_response.status_code == 204:
                logger.info(f"Successfully accepted GitHub invitation for {repo_full_name}")
                return {
                    'success': True,
                    'message': f"Invitation accepted successfully for {repo_full_name}"
                }
            else:
                return {
                    'success': False,
                    'message': f"Failed to accept invitation (HTTP {accept_response.status_code})"
                }

        except Exception as e:
            logger.error(f"Failed to accept GitHub invitation: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f"Error processing invitation: {str(e)}"
            }

    def _accept_gitlab_invite(self, parsed: dict[str, str]) -> dict[str, Any]:
        """Check for and accept pending GitLab project invitations."""
        try:
            project_path = quote(f"{parsed['owner']}/{parsed['repo']}", safe='')

            # Try to get project ID first
            project_url = f"https://{parsed['host']}/api/v4/projects/{project_path}"
            project_response = self._session.get(
                project_url,
                headers={'PRIVATE-TOKEN': self.token},
                timeout=10
            )

            if project_response.status_code == 404:
                return {
                    'success': False,
                    'message': f"Repository not found or no pending invitation exists. Please ask the repository owner to invite the app store user to: {parsed['owner']}/{parsed['repo']}"
                }

            # GitLab automatically grants access when invited, so if we can access the project, we're good
            if project_response.status_code == 200:
                logger.info(f"GitLab access already granted for {parsed['owner']}/{parsed['repo']}")
                return {
                    'success': True,
                    'message': f"Access granted for {parsed['owner']}/{parsed['repo']}"
                }

            return {
                'success': False,
                'message': "No pending invitation found. Please ask the repository owner to add the app store user as a member."
            }

        except Exception as e:
            logger.error(f"Failed to check GitLab invitation: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f"Error processing invitation: {str(e)}"
            }

    def verify_repository_access(self, git_url: str) -> dict[str, Any]:
        """
        Verify that the app store user has read access to the repository.
        Returns dict with 'success' bool and 'message' str.
        """
        try:
            parsed = self._parse_git_url(git_url)

            if not parsed or parsed['platform'] == 'unknown':
                return {
                    'success': False,
                    'message': f"Unable to parse repository URL or unsupported platform. Supported: GitHub, GitLab. URL: {git_url}"
                }

            if not self.token:
                return {
                    'success': False,
                    'message': "Git access token is not configured. Please contact the administrator."
                }

            # Try to fetch tags to verify access
            logger.info(f"Verifying access to {git_url} via {parsed['platform'].upper()} API")

            response = self._request_tags(parsed)

            if response.status_code == 401:
                return {
                    'success': False,
                    'message': "Authentication failed. The Git access token is invalid or expired."
                }
            elif response.status_code == 403 or response.status_code == 404:
                # Try to accept invitation automatically
                logger.info(f"Access denied, attempting to accept invitation for {git_url}")

                if parsed['platform'] == 'github':
                    invite_result = self._accept_github_invite(parsed)
                else:  # gitlab
                    invite_result = self._accept_gitlab_invite(parsed)

                if not invite_result['success']:
                    return invite_result

                # Retry access check after accepting invitation
                logger.info(f"Invitation accepted, retrying access check for {git_url}")
                retry_response = self._request_tags(parsed)

                if retry_response.status_code >= 400:
                    return {
                        'success': False,
                        'message': "Invitation was accepted but access still denied. Please wait a moment and try again."
                    }

                logger.info(f"Access verified successfully after accepting invitation for {git_url}")
                return {
                    'success': True,
                    'message': "Repository invitation accepted and access verified successfully"
                }

            elif response.status_code >= 400:
                return {
                    'success': False,
                    'message': f"Failed to access repository (HTTP {response.status_code}). Please check the repository URL and permissions."
                }

            response.raise_for_status()
            logger.info(f"Access verified successfully for {git_url}")
            return {
                'success': True,
                'message': "Repository access verified successfully"
            }

        except requests.exceptions.Timeout:
            return {
                'success': False,
                'message': "Request timeout. The Git provider did not respond in time. Please try again."
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to verify repository access: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f"Failed to connect to Git provider: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error during repository access verification: {str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f"Unexpected error: {str(e)}"
            }

    def clone_release_vars(self, git_url: str, tag: str, deployment_id: str) -> str:
        """Clone specific files from a release using sparse checkout."""
        repo_path = self.base_path / f"deploy_{deployment_id}"

        if repo_path.exists():
            shutil.rmtree(repo_path)

        try:
            auth_url = self._get_authenticated_url(git_url)
            logger.info(f"Cloning {git_url} tag {tag} (sparse checkout)")

            repo = git.Repo.init(repo_path)
            origin = repo.create_remote('origin', auth_url)

            # Configure sparse checkout
            git_dir = repo_path / '.git'
            sparse_file = git_dir / 'info' / 'sparse-checkout'
            sparse_file.parent.mkdir(parents=True, exist_ok=True)
            sparse_file.write_text('\n'.join(self.SPARSE_CHECKOUT_FILES) + '\n')

            with (git_dir / 'config').open('a') as f:
                f.write('[core]\n\tsparseCheckout = true\n')

            origin.fetch(refspec=f'refs/tags/{tag}:refs/tags/{tag}', depth=1)
            repo.git.checkout(f'refs/tags/{tag}', force=True)

            logger.info(f"Cloned successfully: {repo.head.commit.hexsha[:8]}")
            return str(repo_path)

        except Exception as e:
            if repo_path.exists():
                shutil.rmtree(repo_path)
            raise Exception(f"Failed to clone: {str(e)}") from e

    def get_versions(self, git_url: str) -> list[dict[str, Any]]:
        """Get all versions via REST API."""
        parsed = self._parse_git_url(git_url)

        if not parsed or parsed['platform'] == 'unknown':
            raise Exception(f"Unable to parse URL or unsupported platform: {git_url}")

        if not self.token:
            raise Exception("Git access token not configured")

        try:
            logger.info(f"Fetching versions from {parsed['platform'].upper()} API")

            if parsed['platform'] == 'github':
                versions = self._fetch_github_tags(parsed)
                releases = self._fetch_github_releases(parsed)
            else:  # gitlab
                versions = self._fetch_gitlab_tags(parsed)
                releases = self._fetch_gitlab_releases(parsed)

            # Merge release info into versions
            for v in versions:
                if v['version'] in releases:
                    v.update(releases[v['version']])

            # Sort by semantic versioning
            def sort_key(v):
                try:
                    return tuple(map(int, v['version'].lstrip('v').split('.')))
                except (ValueError, AttributeError):
                    return (v['version'],)

            versions.sort(key=sort_key, reverse=True)
            logger.info(f"Found {len(versions)} versions")
            return versions

        except Exception as e:
            logger.error(f"Failed to fetch versions: {str(e)}", exc_info=True)
            raise Exception(f"Failed to fetch versions: {str(e)}") from e

    def cleanup_repository(self, repo_path: str) -> None:
        """Clean up cloned repository."""
        path = Path(repo_path)
        if path.exists():
            logger.info(f"Cleaning up {repo_path}")
            shutil.rmtree(path)


# Singleton instance
git_service = GitService()
